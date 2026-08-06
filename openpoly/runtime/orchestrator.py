"""Pipeline orchestrator (v7 / P3, EM4 four-stage).

Event-driven serial worker. ``enqueue(item)`` is sync (called from ws_client's
on_item hook); a single asyncio worker drains the queue, runs ``embedding →
analyzer → entry`` per item, and appends per-step results into the section log
stores.

Concurrency model: **one worker, one queue**. LLM rate limits + LLM cost
favor serial over fan-out (micro-stakes paper). Queue is bounded; overflow drops
the newest item and logs a sentinel error entry so the user can see it on
the Calls tab.

Lifecycle is owned by ``PipelineOrchestrator.start() / stop()``. FastAPI
lifespan wires this in P4 (shutdown order: orchestrator first, manager
second).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

from openpoly.embedding.models import MarketCandidates
from openpoly.news.ring_buffer import NewsItem
from openpoly.runtime.section_log import (
    AnalyzerCall,
    EmbeddingCall,
    EntryDecision,
    SectionLogStore,
)
from openpoly.sections._base import SectionInput, SectionOutput
from openpoly.execution import ExecResult, executor
from openpoly.sections.analyzer.llm_v0 import AnalysisResult
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent

logger = logging.getLogger(__name__)


DEFAULT_QUEUE_MAXSIZE = 100


class _SyncSection(Protocol):
    """Minimal section shape used by the orchestrator."""

    def run(self, input: SectionInput) -> SectionOutput: ...


class _Executor(Protocol):
    """Minimal executor shape used by the orchestrator."""

    def execute_buy(self, intent: OrderIntent, *, news_id: str | None, ts: float) -> ExecResult: ...


State = Literal["stopped", "running"]


class PipelineOrchestrator:
    def __init__(
        self,
        *,
        embedding_section: _SyncSection,
        analyzer_section: _SyncSection,
        entry_section: _SyncSection,
        executor: _Executor,
        embedding_log_store: SectionLogStore[EmbeddingCall],
        analyzer_log_store: SectionLogStore[AnalyzerCall],
        entry_log_store: SectionLogStore[EntryDecision],
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self._embedding = embedding_section
        self._analyzer = analyzer_section
        self._entry = entry_section
        self._executor = executor
        self._embedding_log = embedding_log_store
        self._analyzer_log = analyzer_log_store
        self._entry_log = entry_log_store
        # Persist hooks — optional, set by main.py's lifespan once the DB is
        # up. None means "not wired yet" (e.g. before startup / after
        # shutdown); append still happens to the in-memory ring regardless.
        self._embedding_persist: Callable[[EmbeddingCall], None] | None = None
        self._analyzer_persist: Callable[[AnalyzerCall], None] | None = None
        self._entry_persist: Callable[[EntryDecision], None] | None = None
        self._queue: asyncio.Queue[NewsItem] = asyncio.Queue(maxsize=queue_maxsize)
        self._worker_task: asyncio.Task[None] | None = None
        self._state: State = "stopped"
        # Set by stop() to ask the worker loop to exit; checked only between
        # items (never mid-item) so an item already dispatched to a worker
        # thread (analyzer/entry's asyncio.to_thread calls) always finishes —
        # and gets its persist hook called — before stop() returns. Mirrors
        # openpoly.db.writer.WriteBehindWriter's exact shutdown discipline.
        self._stopping = asyncio.Event()
        # canvas-sync v2: lock for atomic section swap (called from
        # /api/canvas/template PUT handler when a section's config changes).
        # In-flight section.run(...) keeps its own reference; Python GC holds
        # the old instance alive until that call returns. Next call uses the
        # new instance.
        self._sections_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"PipelineOrchestrator(state={self._state}, queue_depth={self.queue_depth})"

    # ---------- read-only properties ----------

    @property
    def state(self) -> State:
        return self._state

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ---------- persist hooks (wired into database_manager by main.py) ----------

    def set_embedding_persist(self, hook: Callable[[EmbeddingCall], None] | None) -> None:
        self._embedding_persist = hook

    def set_analyzer_persist(self, hook: Callable[[AnalyzerCall], None] | None) -> None:
        self._analyzer_persist = hook

    def set_entry_persist(self, hook: Callable[[EntryDecision], None] | None) -> None:
        self._entry_persist = hook

    def _append_embedding(self, call: EmbeddingCall) -> None:
        self._embedding_log.append(call)
        if self._embedding_persist is not None:
            try:
                self._embedding_persist(call)
            except Exception:  # noqa: BLE001 — a bad persist hook must not break the pipeline
                logger.exception("embedding_persist raised; suppressing")

    def _append_analyzer(self, call: AnalyzerCall) -> None:
        self._analyzer_log.append(call)
        if self._analyzer_persist is not None:
            try:
                self._analyzer_persist(call)
            except Exception:  # noqa: BLE001 — a bad persist hook must not break the pipeline
                logger.exception("analyzer_persist raised; suppressing")

    def _append_entry(self, call: EntryDecision) -> None:
        self._entry_log.append(call)
        if self._entry_persist is not None:
            try:
                self._entry_persist(call)
            except Exception:  # noqa: BLE001 — a bad persist hook must not break the pipeline
                logger.exception("entry_persist raised; suppressing")

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        # Fresh Event each start — this instance may be started once after a
        # prior stop() (or, in tests, across distinct event loops).
        self._stopping = asyncio.Event()
        self._state = "running"
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        """Signal the worker loop to stop, then await its natural exit —
        never ``task.cancel()`` here. Cancelling while the worker is inside
        ``await asyncio.to_thread(...)`` (the analyzer's LLM call, or the
        entry section's late-buy-veto HTTP fetch) would return before that
        item's persist hook is guaranteed to have run, silently dropping it.
        The loop only checks ``_stopping`` between items, so this always
        waits for whatever item is already in flight to actually finish.
        Whatever is still queued afterward is logged (not processed) by
        ``_drain_queue_at_shutdown`` — running the real pipeline post-stop
        could dispatch a live trade mid-shutdown."""
        self._stopping.set()
        if self._worker_task is not None:
            await self._worker_task
            self._worker_task = None
        self._state = "stopped"
        self._drain_queue_at_shutdown()

    def _drain_queue_at_shutdown(self) -> None:
        """Log (not process) every item still queued once the worker has
        exited — mirrors ``enqueue()``'s queue-overflow logging so both loss
        paths are symmetric and visible on the Calls tab, instead of a
        silent drop."""
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            depth = self._queue.qsize()
            self._append_embedding(
                EmbeddingCall(
                    ts=time.time(),
                    news_id=item.id,
                    news_content_preview=item.content[:80],
                    urgency=item.urgency,
                    verdict="error",
                    candidate_count=0,
                    top_market_id=None,
                    top_score=None,
                    catalog_size=0,
                    latency_ms=0,
                    error=f"discarded_at_shutdown (depth={depth})",
                )
            )
            logger.warning("orchestrator stop(): discarded queued news_id=%s", item.id)

    # ---------- enqueue (sync, called from ws_client hook) ----------

    def enqueue(self, item: NewsItem) -> bool:
        """Returns True if accepted, False if dropped due to queue full.

        Overflow drops the **newest** (this item) — older items in the queue
        keep their slot. This matches plan §OD1 and is simpler than peeking
        + evicting oldest. The dropped item is recorded as an error entry in
        ``embedding_log`` (the pipeline's first stage) so it's not silently
        lost.
        """
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self._append_embedding(
                EmbeddingCall(
                    ts=time.time(),
                    news_id=item.id,
                    news_content_preview=item.content[:80],
                    urgency=item.urgency,
                    verdict="error",
                    candidate_count=0,
                    top_market_id=None,
                    top_score=None,
                    catalog_size=0,
                    latency_ms=0,
                    error=f"queue_overflow (depth={self._queue.qsize()})",
                )
            )
            logger.warning("orchestrator queue full; dropped news_id=%s", item.id)
            return False

    # ---------- internals ----------

    async def _worker(self) -> None:
        while not self._stopping.is_set():
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            try:
                await self._process(item)
            except Exception:  # noqa: BLE001 — defense-in-depth; _process catches
                logger.exception(
                    "orchestrator: unexpected error after _process for %s",
                    item.id,
                )
            finally:
                self._queue.task_done()

    async def _process(self, item: NewsItem) -> None:
        candidates = self._run_embedding(item)
        if candidates is None:
            return
        ar_payload = await self._run_analyzer(item, candidates)
        if ar_payload is None:
            return
        await self._run_entry(item, ar_payload)

    def _run_embedding(self, item: NewsItem) -> MarketCandidates | None:
        """Stage 1 — narrow the market catalog for this news item.

        Returns the ``MarketCandidates`` on ok, or None on skip / error (the
        pipeline then stops without calling the analyzer).
        """
        ts = time.time()
        start = time.monotonic()
        verdict: str
        error: str | None
        out: SectionOutput | None
        try:
            out = self._embedding.run(SectionInput(tick_type="event", payload=item))
            verdict = str(out.verdict)
            error = out.reason if verdict == "error" else None
        except Exception as exc:  # noqa: BLE001 — section impl is user code
            out = None
            verdict = "error"
            error = repr(exc)[:200]
        latency_ms = int((time.monotonic() - start) * 1000)

        candidates = (
            out.payload if out is not None and isinstance(out.payload, MarketCandidates) else None
        )
        signals = out.signals if out is not None else {}
        top = candidates.candidates[0] if candidates is not None and candidates.candidates else None

        self._append_embedding(
            EmbeddingCall(
                ts=ts,
                news_id=item.id,
                news_content_preview=item.content[:80],
                urgency=item.urgency,
                verdict=verdict,  # type: ignore[arg-type]
                candidate_count=(len(candidates.candidates) if candidates is not None else 0),
                top_market_id=top.market.market_id if top is not None else None,
                top_score=top.score if top is not None else None,
                catalog_size=int(signals.get("catalog_size", 0) or 0),
                latency_ms=latency_ms,
                error=error,
            )
        )

        if verdict != "ok" or candidates is None:
            return None
        return candidates

    async def _run_analyzer(
        self, item: NewsItem, candidates: MarketCandidates
    ) -> AnalysisResult | None:
        """Stage 2 — analyze the narrowed candidate set.

        ``item`` supplies the news fields for the log entry; ``candidates`` is
        the section payload. Returns the AnalysisResult on ok, else None.

        The analyzer makes a blocking LLM API call, so its ``run()`` is
        offloaded to a worker thread — the event loop stays free for the WS
        reconnect / market-poll tasks (docs/architecture/05).
        """
        ts = time.time()
        start = time.monotonic()
        verdict: str
        error: str | None
        out: SectionOutput | None
        try:
            out = await asyncio.to_thread(
                self._analyzer.run,
                SectionInput(tick_type="event", payload=candidates),
            )
            verdict = str(out.verdict)
            error = out.reason if verdict == "error" else None
        except Exception as exc:  # noqa: BLE001 — section impl is user code
            out = None
            verdict = "error"
            error = repr(exc)[:200]
        latency_ms = int((time.monotonic() - start) * 1000)

        ar = out.payload if out is not None and isinstance(out.payload, AnalysisResult) else None

        # PD1: surface the LLM's stated reason for the decision so
        # PositionDetail UI can show it next to the position. The model is
        # required to return a rationale on every tool call, including
        # abstains/filtered-out decisions, so a skip verdict with `out`
        # still set (the LLM was actually called) also carries one via
        # `out.signals["rationale"]` — only a pre-filter skip or an
        # LLM-client exception (no `out` at all) has nothing to report.
        if ar is not None:
            rationale = ar.rationale
            self_check = ar.checks
        elif out is not None:
            rationale = out.signals.get("rationale")
            self_check = out.signals.get("self_check")
        else:
            rationale = None
            self_check = None

        self._append_analyzer(
            AnalyzerCall(
                ts=ts,
                news_id=item.id,
                news_content_preview=item.content[:80],
                urgency=item.urgency,
                verdict=verdict,  # type: ignore[arg-type]
                p_model=ar.p_model if ar is not None else None,
                confidence=ar.confidence if ar is not None else None,
                market_id=ar.market_id if ar is not None else None,
                latency_ms=latency_ms,
                error=error,
                rationale=rationale,
                self_check=self_check,
            )
        )

        # Only forward to entry on ok + valid AR payload.
        if verdict != "ok" or ar is None:
            return None
        return ar

    async def _run_entry(self, item: NewsItem, ar: AnalysisResult) -> None:
        """Stage 3 — entry decision + execution.

        The entry section may do a blocking HTTP fetch (the late-buy veto), so
        its ``run()`` is offloaded to a worker thread. The executor call is
        offloaded too: the paper executor's DB write is sub-millisecond, but
        the live executor submits an order to the CLOB and blocks polling for
        its fill — running that inline would stall the whole pipeline (and any
        concurrently scheduled coroutine, e.g. the exit monitor's tick) for the
        entire trade duration.
        """
        ts = time.time()
        start = time.monotonic()
        verdict: str
        reason: str | None
        error: str | None
        intent: OrderIntent | None = None
        signals_json: str | None = None
        try:
            out = await asyncio.to_thread(
                self._entry.run,
                SectionInput(tick_type="event", payload=ar),
            )
            verdict = str(out.verdict)
            reason = out.reason
            error = out.reason if verdict == "error" else None
            signals_json = json.dumps(out.signals) if out.signals else None
            if verdict == "ok" and isinstance(out.payload, OrderIntent):
                intent = out.payload
        except Exception as exc:  # noqa: BLE001 — section impl is user code
            verdict = "error"
            reason = None
            error = repr(exc)[:200]

        # Execution stage — only when the section produced an OrderIntent.
        fill_status: str | None = None
        fill_price: float | None = None
        fill_qty: float | None = None
        position_id: int | None = None
        if intent is not None:
            try:
                result = await asyncio.to_thread(
                    self._executor.execute_buy, intent, news_id=item.id, ts=ts
                )
                position_id = result.position_id
                if result.filled:
                    fill_status = "filled"
                    fill_price = result.price
                    fill_qty = result.qty
                else:
                    fill_status = result.skip_reason
            except Exception as exc:  # noqa: BLE001 — DB write may raise
                verdict = "error"
                error = repr(exc)[:200]
                fill_status = "error"

        latency_ms = int((time.monotonic() - start) * 1000)

        self._append_entry(
            EntryDecision(
                ts=ts,
                news_id=item.id,
                ar_p_model=ar.p_model,
                ar_market_id=ar.market_id,
                verdict=verdict,  # type: ignore[arg-type]
                side=intent.side if intent is not None else None,
                qty=intent.qty if intent is not None else None,
                price=intent.price if intent is not None else None,
                reason=reason,
                latency_ms=latency_ms,
                error=error,
                fill_status=fill_status,
                fill_price=fill_price,
                fill_qty=fill_qty,
                position_id=position_id,
                signals_json=signals_json,
            )
        )

    # ---------- canvas-sync v2: hot-reload section swap ----------

    async def replace_section(self, section_type: str, new_inst: _SyncSection) -> None:
        """Atomically swap one section instance. An in-flight ``run(...)``
        keeps its own reference via Python GC, so the call already underway
        finishes on the old instance and the next one uses the new.

        Called by the module-level ``replace_section`` below, which is what
        ``api/canvas_routes._apply_canvas_reload`` reaches for after a PUT diff
        detects a section's config changed."""
        async with self._sections_lock:
            if section_type == "embedding":
                self._embedding = new_inst
            elif section_type == "analyzer":
                self._analyzer = new_inst
            elif section_type == "entry":
                self._entry = new_inst
            else:
                raise ValueError(f"unknown orchestrator section_type: {section_type!r}")


# Module-level singleton built lazily so tests can substitute. Real
# wire-up (with manager hook) lives in main.py's lifespan (P4).
_singleton: PipelineOrchestrator | None = None

_C = TypeVar("_C", bound=BaseModel)


def _resolve_section_class(section_type: str, default_cls: type) -> type:
    """Resolve the canvas-recorded implementation for ``section_type``,
    falling back to ``default_cls`` when no canvas node exists, the node
    predates the variant selector (no ``impl`` field), or the recorded impl
    can no longer be resolved (deleted user_section, renamed class, failed
    contract test). Never blocks startup — same "a stale or hand-broken
    canvas can never block startup" stance as ``_canvas_config`` below."""
    from openpoly.runtime.canvas_store import section_impl
    from openpoly.sections._registry import resolve_impl

    rec = section_impl(section_type)
    if rec is None:
        return default_cls
    try:
        return resolve_impl(section_type, *rec)
    except Exception as exc:  # noqa: BLE001 — a bad canvas impl choice must not break startup
        logger.warning(
            "canvas impl %r for section %r unresolvable (%s); using default",
            rec,
            section_type,
            exc,
        )
        return default_cls


def _canvas_config(config_cls: type[_C], section_type: str) -> _C:
    """Build a section Config from the persisted canvas node config, falling
    back to the Config's own defaults on a missing node or invalid values — so
    a stale or hand-broken canvas can never block pipeline startup."""
    from openpoly.runtime.canvas_store import section_config

    raw = section_config(section_type)
    if not raw:
        return config_cls()
    try:
        return config_cls(**raw)
    except Exception as exc:  # noqa: BLE001 — bad canvas config must not break startup
        logger.warning(
            "invalid canvas config for section %r (%s); using defaults",
            section_type,
            exc,
        )
        return config_cls()


def get_orchestrator() -> PipelineOrchestrator:
    """Lazily build the pipeline orchestrator. Each section's params come from
    the persisted canvas (``canvas_store``); restart the backend to apply a
    canvas edit, same as picking up a new section impl."""
    global _singleton
    if _singleton is None:
        from openpoly.runtime.section_log import (
            analyzer_log,
            embedding_log,
            entry_log,
        )
        from openpoly.sections.analyzer.llm_v0 import LLMAnalyzerV0
        from openpoly.sections.embedding.minilm_v0 import EmbeddingFilterV0
        from openpoly.sections.entry.edge_threshold_v0 import EdgeThresholdEntryV0

        # canvas variant selector: resolve the canvas-recorded impl per
        # section type, falling back to the hardcoded defaults above for a
        # canvas that predates the selector or names an unresolvable impl.
        embedding_cls = _resolve_section_class("embedding", EmbeddingFilterV0)
        analyzer_cls = _resolve_section_class("analyzer", LLMAnalyzerV0)
        entry_cls = _resolve_section_class("entry", EdgeThresholdEntryV0)

        # portfolio_provider isn't part of the Section Protocol (only
        # Config/run() are), so a resolved entry impl isn't guaranteed to
        # accept it — fall back to the no-kwarg constructor rather than
        # crashing startup on a well-formed but differently-shaped impl.
        entry_config = _canvas_config(entry_cls.Config, "entry")
        try:
            entry_section = entry_cls(
                entry_config,
                # Lazy: executor's portfolio is configured *after* the
                # orchestrator (and entry section) is built, so we hand
                # entry a closure it calls per run() instead of the store
                # itself. Returns None until executor.configure_paper() lands.
                portfolio_provider=lambda: executor.portfolio,
            )
        except TypeError:
            entry_section = entry_cls(entry_config)

        _singleton = PipelineOrchestrator(
            embedding_section=embedding_cls(_canvas_config(embedding_cls.Config, "embedding")),
            analyzer_section=analyzer_cls(_canvas_config(analyzer_cls.Config, "analyzer")),
            entry_section=entry_section,
            executor=executor,
            embedding_log_store=embedding_log,
            analyzer_log_store=analyzer_log,
            entry_log_store=entry_log,
        )
    return _singleton


def _reset_singleton_for_tests() -> None:
    global _singleton
    _singleton = None


# ---------- canvas-sync v2: hot-reload section swap ----------


async def replace_section(section_type: str, new_inst: _SyncSection) -> None:
    """Module-level entry point — atomically replace a section in the running
    orchestrator (if any). No-op when no singleton has been built yet (e.g.
    PUT arrived before first news; the orchestrator's next lazy build will
    read the updated canvas anyway).

    Callers: ``api/canvas_routes._apply_canvas_reload`` after a PUT diff
    detects a section's config changed. Build the new instance in the
    caller so failures there don't pollute the orchestrator with a
    half-constructed object.
    """
    if _singleton is None:
        return
    await _singleton.replace_section(section_type, new_inst)
