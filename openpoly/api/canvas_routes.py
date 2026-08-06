"""HTTP routes for the canvas template — frontend ↔ backend sync (canvas-sync v2).

The runtime orchestrator builds its sections from the persisted canvas
(``runtime.canvas_store.load_template``). canvas-sync v2 closes the
two-source-of-truth gap with HTTP-ETag-style optimistic locking + atomic
section hot-reload:

  - ``GET  /api/canvas/template`` — returns the persisted template; the
    rev (SHA-256 of canonical JSON) is in the ``ETag`` header AND the
    response body's ``rev`` field so the frontend can read either.
  - ``PUT  /api/canvas/template`` — requires ``If-Match: <rev>`` header
    whose value matches the current on-disk rev. Mismatch → 409 + body
    ``{error:"stale_rev", current_rev, template}``. On success: persist,
    diff old vs new section configs, hot-rebuild only the changed
    sections in orchestrator + ExitMonitor under a lock — no process
    restart, in-flight LLM calls keep running with old instance (Python
    GC), next call uses the new one.

Backward-compat: an absent ``If-Match`` header is treated as "first write"
and accepted as long as no template is persisted yet; if one is persisted,
a missing If-Match is **rejected** (400) — operator must explicitly opt
in to a force-overwrite with ``If-Match: *``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Response

from openpoly.runtime.canvas_store import (
    load_template_with_rev,
    save_template,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["canvas"])

# Strong references to in-flight hot-reload tasks. asyncio only holds a *weak*
# reference to a running task, so a bare ``create_task(...)`` whose result is
# discarded can be garbage-collected mid-execution — the canvas would save but
# silently never hot-reload. Discarded on completion so this can't grow.
_reload_tasks: set[asyncio.Task] = set()


def _spawn_reload(coro) -> None:
    task = asyncio.create_task(coro)
    _reload_tasks.add(task)
    task.add_done_callback(_reload_tasks.discard)


@router.get("/api/canvas/template")
def get_canvas_template(response: Response) -> dict[str, Any]:
    """Read the persisted canvas. 404 when nothing is on disk yet.

    Adds ``ETag`` header + ``rev`` body field so the frontend can later
    submit ``If-Match: <rev>`` on autosave PUT.
    """
    pair = load_template_with_rev()
    if pair is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_template",
                "message": "no canvas template persisted yet",
            },
        )
    template, rev = pair
    response.headers["ETag"] = rev
    # Caller can either read the header or the body field; mirror to body
    # so a fetch() consumer doesn't have to do two reads.
    return {**template, "rev": rev}


@router.put("/api/canvas/template")
async def put_canvas_template(
    body: dict[str, Any],
    response: Response,
    if_match: str | None = Header(default=None),
) -> dict[str, Any]:
    """Persist a canvas template with optimistic-lock check + hot-reload.

    Shape check is minimal — backend stores as-is so the schema can evolve
    frontend-first. The body's ``rev`` field (if present) is stripped
    before persisting so the persisted file's hash doesn't include the
    rev itself (canonical form: template without rev). Returns the saved
    template + new rev.
    """
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_shape", "message": "body must be a JSON object"},
        )
    if not isinstance(body.get("version"), int):
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_shape", "message": "version must be an integer"},
        )
    if not isinstance(body.get("nodes"), list):
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_shape", "message": "nodes must be an array"},
        )

    # Strip transient rev field if present — it's metadata, not template content.
    incoming = {k: v for k, v in body.items() if k != "rev"}

    # Optimistic-lock check
    current = load_template_with_rev()
    if current is None:
        # First write: If-Match is allowed to be absent OR "*"
        if if_match is not None and if_match not in ("*", ""):
            # A real rev was sent against an empty store — must be stale.
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "stale_rev",
                    "message": "no template on disk yet; you sent If-Match",
                    "current_rev": None,
                    "template": None,
                },
            )
    else:
        current_template, current_rev = current
        if if_match is None:
            # Existing template, no If-Match → reject (operator must opt in).
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "if_match_required",
                    "message": (
                        "a canvas already exists; send If-Match: <rev> or If-Match: * to force"
                    ),
                    "current_rev": current_rev,
                },
            )
        if if_match != "*" and if_match != current_rev:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "stale_rev",
                    "message": "your draft is based on an older canvas",
                    "current_rev": current_rev,
                    "template": current_template,
                },
            )

    try:
        new_rev = save_template(incoming)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "save_failed", "message": str(exc)},
        ) from exc

    # Trigger hot-reload of orchestrator sections affected by config change.
    # Fire-and-forget: PUT returns once persisted; reload runs async.
    # Failures inside reload are logged but don't fail the PUT (operator can
    # observe via /api/<section>/log and act).
    old_template = current[0] if current else None
    _spawn_reload(_apply_canvas_reload(old_template, incoming))

    logger.info(
        "canvas template saved (version=%s, nodes=%d, rev=%s…)",
        incoming.get("version"),
        len(incoming.get("nodes") or []),
        new_rev[:8],
    )
    response.headers["ETag"] = new_rev
    return {**incoming, "rev": new_rev}


async def _apply_canvas_reload(
    old_template: dict[str, Any] | None,
    new_template: dict[str, Any],
) -> None:
    """Diff old vs new section configs, rebuild only changed sections in
    orchestrator + ExitMonitor. Async because section construction may be
    slow (embedding loads sentence-transformers). Errors are logged but
    suppressed — orchestrator keeps using the prior section until the
    next successful reload."""
    logger.info(
        "canvas reload: starting diff (old=%s nodes, new=%s nodes)",
        len(old_template.get("nodes") or []) if old_template else "absent",
        len(new_template.get("nodes") or []),
    )
    # Late import to avoid cycle at module load.
    from openpoly.embedding.manager import manager as embedding_manager
    from openpoly.runtime import orchestrator as orch_mod
    from openpoly.runtime.exit_monitor import exit_monitor

    def _section_node(template: dict | None, section_type: str) -> tuple[dict, dict | None]:
        """(config, impl) of the first node of ``section_type``. ``impl`` is
        included in the comparison below so that switching which
        implementation a node runs — the canvas variant selector — triggers
        a hot-reload even when the config dict itself is unchanged (e.g.
        switching to another impl's defaults that happen to match)."""
        if template is None:
            return {}, None
        for node in template.get("nodes") or []:
            if isinstance(node, dict) and node.get("sectionType") == section_type:
                cfg = node.get("config")
                impl = node.get("impl")
                return (
                    dict(cfg) if isinstance(cfg, dict) else {},
                    dict(impl) if isinstance(impl, dict) else None,
                )
        return {}, None

    # Sections owned by the orchestrator.
    orchestrator_section_types = ("embedding", "analyzer", "entry")
    for stype in orchestrator_section_types:
        old_node = _section_node(old_template, stype)
        new_node = _section_node(new_template, stype)
        if old_node == new_node:
            continue
        try:
            new_inst = _build_section(stype)
        except Exception:  # noqa: BLE001
            logger.exception("canvas reload: failed to build %s section", stype)
            continue
        try:
            await orch_mod.replace_section(stype, new_inst)
            logger.info("canvas reload: rebuilt %s section", stype)
        except Exception:  # noqa: BLE001
            logger.exception("canvas reload: failed to swap %s section", stype)
            continue
        if stype == "embedding":
            # replace_section only swaps the section instance the orchestrator
            # calls .run() on — it doesn't push the new config into the
            # EmbeddingManager singleton that section delegates to, so
            # embedding_model / max_question_chars / warm_interval_seconds
            # would otherwise silently keep using whatever was live at
            # process boot.
            try:
                await embedding_manager.reconfigure(
                    model_name=new_inst.config.embedding_model,
                    max_question_chars=new_inst.config.max_question_chars,
                    warm_interval_seconds=new_inst.config.warm_interval_seconds,
                )
                logger.info("canvas reload: reconfigured embedding manager")
            except Exception:  # noqa: BLE001
                logger.exception("canvas reload: failed to reconfigure embedding manager")

    # Exit section is held by exit_monitor, not orchestrator.
    old_exit = _section_node(old_template, "exit")
    new_exit = _section_node(new_template, "exit")
    if old_exit != new_exit:
        try:
            new_exit_inst = _build_section("exit")
        except Exception:  # noqa: BLE001
            logger.exception("canvas reload: failed to build exit section")
            return
        try:
            await exit_monitor.replace_exit_section(new_exit_inst)
            logger.info("canvas reload: rebuilt exit section")
        except Exception:  # noqa: BLE001
            logger.exception("canvas reload: failed to swap exit section")


def _build_section(section_type: str):
    """Build a fresh section instance from the canvas-resolved impl + config.

    Resolves the concrete class via ``_resolve_section_class`` (falling back
    to the same hardcoded default ``get_orchestrator()`` uses for a canvas
    that predates the variant selector or names an unresolvable impl), then
    builds it from that class's own ``Config`` — generic over whichever impl
    was actually chosen rather than hardcoded per section_type.
    """
    from openpoly.runtime.orchestrator import _canvas_config, _resolve_section_class

    if section_type == "embedding":
        from openpoly.sections.embedding.minilm_v0 import EmbeddingFilterV0

        cls = _resolve_section_class("embedding", EmbeddingFilterV0)
        return cls(_canvas_config(cls.Config, "embedding"))
    if section_type == "analyzer":
        from openpoly.sections.analyzer.llm_v0 import LLMAnalyzerV0

        cls = _resolve_section_class("analyzer", LLMAnalyzerV0)
        return cls(_canvas_config(cls.Config, "analyzer"))
    if section_type == "entry":
        from openpoly.execution import executor
        from openpoly.sections.entry.edge_threshold_v0 import EdgeThresholdEntryV0

        cls = _resolve_section_class("entry", EdgeThresholdEntryV0)
        config = _canvas_config(cls.Config, "entry")
        # Mirror orchestrator's portfolio_provider closure — entry asks for
        # the live PortfolioStore lazily because the executor's portfolio is
        # attached after sections are constructed. Not part of the Section
        # Protocol, so a resolved impl isn't guaranteed to accept it.
        try:
            return cls(config, portfolio_provider=lambda: executor.portfolio)
        except TypeError:
            return cls(config)
    if section_type == "exit":
        from openpoly.sections.exit.threshold_v0 import ThresholdExitV0

        cls = _resolve_section_class("exit", ThresholdExitV0)
        return cls(_canvas_config(cls.Config, "exit"))
    raise ValueError(f"unknown section_type: {section_type}")
