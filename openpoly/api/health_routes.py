"""GET /api/health/detail — composite health report across every runtime
subsystem (database, market feed, news/trading feed, market access/broker,
pipeline, exit/settlement monitors, embedding, reconciliation).

This is deliberately a *separate* route from ``@app.get("/api/health")`` in
``main.py`` — that one is a frozen liveness probe (``{"status": "ok"}``)
depended on verbatim by ``scripts/deploy.sh`` and the systemd deploy docs, so
it is never touched here.

Every per-subsystem probe runs through ``_safe_check`` so one subsystem
raising (a bad ``getattr``, a DB hiccup) degrades that check to ``"down"``
rather than 500ing the whole endpoint — the same
``except Exception as exc:  # noqa: BLE001`` degrade-to-null discipline used
throughout ``wallet_routes.py`` and the source managers' own poll loops.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from openpoly.api.wallet_routes import get_wallet_balance_cached
from openpoly.db.manager import DatabaseManager, manager as database_manager
from openpoly.embedding.manager import EmbeddingManager, manager as embedding_manager
from openpoly.execution import ExecutorDispatcher, executor as executor_dispatcher
from openpoly.markets.manager import MarketSourceManager, manager as market_source_manager
from openpoly.news.manager import NewsSourceManager, manager as news_source_manager
from openpoly.runtime.exit_monitor import DEFAULT_TICK_INTERVAL_SECONDS, ExitMonitor, exit_monitor
from openpoly.runtime.orchestrator import DEFAULT_QUEUE_MAXSIZE, PipelineOrchestrator, get_orchestrator
from openpoly.runtime.settlement_monitor import SettlementMonitor, settlement_monitor
from openpoly.wallet.runtime_state import RuntimeState, runtime_state

router = APIRouter(prefix="/api", tags=["health"])

_START_TIME = time.time()

# A stale discovery poll is flagged once it's this many multiples of the
# feed's own configured interval overdue — a fixed short constant would
# false-positive since poll_interval_seconds defaults to 900s (15 min).
_MARKET_FEED_STALE_MULTIPLIER = 2
_MARKET_FEED_DEFAULT_POLL_SECONDS = 900

# News is a push feed (WS) with no fixed cadence — arrival is market-driven,
# not periodic — so this is deliberately generous to avoid flagging ordinary
# quiet periods as degraded.
_NEWS_FEED_STALE_SECONDS = 300.0

# 3x the exit monitor's default 30s tick interval. The monitor's *actual*
# configured interval isn't exposed publicly, so this is an approximation.
_EXIT_MONITOR_STALE_SECONDS = 3 * DEFAULT_TICK_INTERVAL_SECONDS

# Pipeline queue is considered backed-up at 80% of its bounded capacity.
_PIPELINE_QUEUE_DEGRADED_FRACTION = 0.8


class SubsystemCheck(BaseModel):
    status: str  # "ok" | "degraded" | "down" | "stopped" | "disabled"
    detail: dict[str, Any] = {}


class HealthDetailResponse(BaseModel):
    status: str  # overall rollup: "ok" | "degraded" | "down"
    timestamp: float
    checks: dict[str, SubsystemCheck]


_SEVERITY = {"ok": 0, "disabled": 0, "stopped": 1, "degraded": 2, "down": 3}
_ROLLUP = {0: "ok", 1: "degraded", 2: "degraded", 3: "down"}


def _aggregate_status(checks: dict[str, SubsystemCheck]) -> str:
    worst = max((_SEVERITY[c.status] for c in checks.values()), default=0)
    return _ROLLUP[worst]


def _safe_check(fn: Callable[[], SubsystemCheck]) -> SubsystemCheck:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — one bad check must not 500 the endpoint
        return SubsystemCheck(status="down", detail={"error": repr(exc)[:200]})


# ---------- dependency getters (overridable in tests) ----------


def get_database_manager() -> DatabaseManager:
    return database_manager


def get_market_source_manager() -> MarketSourceManager:
    return market_source_manager


def get_news_source_manager() -> NewsSourceManager:
    return news_source_manager


def get_pipeline_orchestrator() -> PipelineOrchestrator:
    return get_orchestrator()


def get_exit_monitor() -> ExitMonitor:
    return exit_monitor


def get_settlement_monitor() -> SettlementMonitor:
    return settlement_monitor


def get_embedding_manager() -> EmbeddingManager:
    return embedding_manager


def get_executor_dispatcher() -> ExecutorDispatcher:
    return executor_dispatcher


def get_runtime_state() -> RuntimeState:
    return runtime_state


def get_reconciliation_monitor() -> Any | None:
    # Re-read the module attribute on every call rather than importing the
    # name at module load — ``reconciliation_monitor`` starts as ``None`` and
    # is only reassigned inside main.py's lifespan() once a wallet is
    # configured, so a frozen `from ... import reconciliation_monitor` would
    # always see the pre-lifespan `None`.
    from openpoly.runtime import reconciliation_monitor as _mod

    return _mod.reconciliation_monitor


# ---------- per-subsystem checks ----------


def _check_app() -> SubsystemCheck:
    return SubsystemCheck(status="ok", detail={"uptime_seconds": time.time() - _START_TIME})


def _check_database(db: DatabaseManager) -> SubsystemCheck:
    status = db.status()
    writers: dict[str, Any] = status.get("writers", {})
    unhealthy = [
        name
        for name, stats in writers.items()
        if stats is not None and (stats.get("dropped", 0) > 0 or stats.get("sink_errors", 0) > 0)
    ]
    return SubsystemCheck(
        status="degraded" if unhealthy else "ok",
        detail={
            "tables": status.get("tables", {}),
            "writers": writers,
            "unhealthy_writers": unhealthy,
        },
    )


def _check_market_feed(mgr: MarketSourceManager) -> SubsystemCheck:
    snap = mgr.status()
    now = time.time()
    if snap.state == "error":
        status = "down"
    elif snap.state == "stopped":
        status = "stopped"
    else:
        poll_interval = (snap.running_config or {}).get(
            "poll_interval_seconds", _MARKET_FEED_DEFAULT_POLL_SECONDS
        )
        stale_after = _MARKET_FEED_STALE_MULTIPLIER * poll_interval
        stale = snap.last_poll_at is None or (now - snap.last_poll_at) > stale_after
        status = "degraded" if stale else "ok"
    return SubsystemCheck(
        status=status,
        detail={
            "state": snap.state,
            "last_poll_at": snap.last_poll_at,
            "seconds_since_last_poll": None if snap.last_poll_at is None else now - snap.last_poll_at,
            "catalog_size": snap.catalog_size,
            "poll_count": snap.poll_count,
            "last_error": snap.last_error,
        },
    )


def _check_news_feed(mgr: NewsSourceManager) -> SubsystemCheck:
    snap = mgr.status()
    now = time.time()
    if snap.state == "error":
        status = "down"
    elif snap.state == "stopped":
        status = "stopped"
    elif snap.state == "connecting":
        status = "degraded"
    else:  # connected
        stale = snap.last_msg_at is None or (now - snap.last_msg_at) > _NEWS_FEED_STALE_SECONDS
        status = "degraded" if stale else "ok"
    return SubsystemCheck(
        status=status,
        detail={
            "state": snap.state,
            "last_msg_at": snap.last_msg_at,
            "seconds_since_last_msg": None if snap.last_msg_at is None else now - snap.last_msg_at,
            "total_recv": snap.total_recv,
            "buffer_size": snap.buffer_size,
            "reconnect_attempts": snap.reconnect_attempts,
            "last_error": snap.last_error,
        },
    )


async def _check_market_access(rstate: RuntimeState, executor: ExecutorDispatcher) -> SubsystemCheck:
    if rstate.wallet is None:
        return SubsystemCheck(
            status="disabled", detail={"configured": False, "exec_mode": rstate.exec_mode}
        )
    # Live mode with no live executor is the worst state this system can be
    # in and the hardest to notice: the UI shows LIVE, the pipeline runs, and
    # every single order is silently skipped as ``live_not_ready``. Report it
    # as down — nothing else here distinguishes it from a healthy live run.
    if rstate.exec_mode == "live" and not executor.live_ready:
        return SubsystemCheck(
            status="down",
            detail={
                "configured": True,
                "exec_mode": rstate.exec_mode,
                "live_ready": False,
                "error": (
                    "exec_mode is live but no live executor is armed — every order "
                    "will skip as live_not_ready; re-save the wallet config or restart"
                ),
            },
        )
    raw_balance = await get_wallet_balance_cached(executor)
    return SubsystemCheck(
        status="ok" if raw_balance is not None else "degraded",
        detail={
            "configured": True,
            "exec_mode": rstate.exec_mode,
            "live_ready": executor.live_ready,
            "usdc": None if raw_balance is None else raw_balance / 1e6,
        },
    )


def _check_pipeline(orch: PipelineOrchestrator) -> SubsystemCheck:
    if orch.state == "stopped":
        status = "stopped"
    elif orch.queue_depth >= _PIPELINE_QUEUE_DEGRADED_FRACTION * DEFAULT_QUEUE_MAXSIZE:
        status = "degraded"
    else:
        status = "ok"
    return SubsystemCheck(
        status=status,
        detail={
            "state": orch.state,
            "queue_depth": orch.queue_depth,
            "queue_maxsize": DEFAULT_QUEUE_MAXSIZE,
        },
    )


def _check_exit_monitor(mon: ExitMonitor) -> SubsystemCheck:
    now = time.time()
    if mon.state == "stopped":
        status = "stopped"
    else:
        stale = mon.last_tick_at is None or (now - mon.last_tick_at) > _EXIT_MONITOR_STALE_SECONDS
        status = "degraded" if (mon.blocked > 0 or stale) else "ok"
    return SubsystemCheck(
        status=status,
        detail={
            "state": mon.state,
            "last_tick_at": mon.last_tick_at,
            "seconds_since_last_tick": None if mon.last_tick_at is None else now - mon.last_tick_at,
            "open_positions": mon.open_positions,
            "blocked": mon.blocked,
            "last_tick": mon.last_tick,
        },
    )


def _check_settlement_monitor(mon: SettlementMonitor) -> SubsystemCheck:
    # Only `.state` is a confirmed public surface on this class today — no
    # tick-heartbeat property exists yet, unlike ExitMonitor.
    status = "stopped" if mon.state == "stopped" else "ok"
    return SubsystemCheck(status=status, detail={"state": mon.state})


def _check_embedding_manager(mgr: EmbeddingManager) -> SubsystemCheck:
    detail = mgr.status()
    status = "stopped" if detail.get("state") == "stopped" else "ok"
    return SubsystemCheck(status=status, detail=detail)


def _check_reconciliation(rmon: Any | None) -> SubsystemCheck:
    if rmon is None:
        return SubsystemCheck(status="disabled", detail={"reason": "no wallet configured"})
    status = "stopped" if getattr(rmon, "state", "stopped") == "stopped" else "ok"
    return SubsystemCheck(status=status, detail={"state": getattr(rmon, "state", None)})


@router.get("/health/detail", response_model=HealthDetailResponse)
async def health_detail(
    db_manager: DatabaseManager = Depends(get_database_manager),
    market_mgr: MarketSourceManager = Depends(get_market_source_manager),
    news_mgr: NewsSourceManager = Depends(get_news_source_manager),
    orchestrator: PipelineOrchestrator = Depends(get_pipeline_orchestrator),
    exit_mon: ExitMonitor = Depends(get_exit_monitor),
    settlement_mon: SettlementMonitor = Depends(get_settlement_monitor),
    embedding_mgr: EmbeddingManager = Depends(get_embedding_manager),
    executor: ExecutorDispatcher = Depends(get_executor_dispatcher),
    rstate: RuntimeState = Depends(get_runtime_state),
    rmon: Any | None = Depends(get_reconciliation_monitor),
) -> HealthDetailResponse:
    checks = {
        "app": _safe_check(_check_app),
        "database": _safe_check(lambda: _check_database(db_manager)),
        "market_feed": _safe_check(lambda: _check_market_feed(market_mgr)),
        "news_feed": _safe_check(lambda: _check_news_feed(news_mgr)),
        "pipeline": _safe_check(lambda: _check_pipeline(orchestrator)),
        "exit_monitor": _safe_check(lambda: _check_exit_monitor(exit_mon)),
        "settlement_monitor": _safe_check(lambda: _check_settlement_monitor(settlement_mon)),
        "embedding": _safe_check(lambda: _check_embedding_manager(embedding_mgr)),
        "reconciliation": _safe_check(lambda: _check_reconciliation(rmon)),
    }
    try:
        checks["market_access"] = await _check_market_access(rstate, executor)
    except Exception as exc:  # noqa: BLE001 — same isolation as the sync checks
        checks["market_access"] = SubsystemCheck(status="down", detail={"error": repr(exc)[:200]})

    return HealthDetailResponse(
        status=_aggregate_status(checks),
        timestamp=time.time(),
        checks=checks,
    )
