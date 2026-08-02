"""Debug-only route: inject a synthetic NewsItem into the running pipeline.
For local testing without a TradingNews subscription. Not for production."""

from __future__ import annotations

import time

from fastapi import APIRouter
from pydantic import BaseModel

from openpoly.news.ring_buffer import NewsItem
from openpoly.runtime.orchestrator import get_orchestrator

router = APIRouter(prefix="/api/debug", tags=["debug"])


class InjectNewsRequest(BaseModel):
    content: str
    urgency: str = "high"
    sentiment: str | None = "positive"


@router.post("/inject_news")
def inject_news(req: InjectNewsRequest) -> dict:
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