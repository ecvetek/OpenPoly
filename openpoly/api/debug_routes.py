"""Debug-only route: inject a synthetic NewsItem into the running pipeline.
For local testing without a TradingNews subscription. Not for production."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from openpoly.news.ring_buffer import NewsItem
from openpoly.runtime.orchestrator import get_orchestrator
from openpoly.wallet.runtime_state import runtime_state

router = APIRouter(prefix="/api/debug", tags=["debug"])


class InjectNewsRequest(BaseModel):
    content: str
    urgency: str = "high"
    sentiment: str | None = "positive"


@router.post("/inject_news")
def inject_news(req: InjectNewsRequest) -> dict:
    # This module's own docstring says "not for production" — but nothing
    # enforced that. A fabricated item flows through the exact same real
    # pipeline as genuine news, reaching executor.execute_buy, which
    # dispatches to the **live** broker whenever exec_mode is "live" — a
    # real on-chain buy with no confirmation step. Paper mode (the only
    # mode this route is actually meant for) is unaffected.
    if runtime_state.exec_mode == "live":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "live_mode_active",
                "message": (
                    "debug news injection is disabled while exec_mode=live — "
                    "it can trigger a real on-chain buy with no confirmation step"
                ),
            },
        )
    now = time.time()
    item = NewsItem(
        id=f"debug-{int(now * 1000)}",
        content=req.content,
        urgency=req.urgency,
        sentiment=req.sentiment,
        published_at=now,
        received_at=now,
    )
    enqueued = get_orchestrator().enqueue(item)
    return {"status": "enqueued" if enqueued else "rejected", "id": item.id}