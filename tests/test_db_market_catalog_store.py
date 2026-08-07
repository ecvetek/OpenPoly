"""Tests for openpoly.db — market_catalog upsert persistence + the
MarketSourceManager -> market_persist wiring that feeds it."""

from __future__ import annotations

from sqlalchemy import select

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.history_query import market_catalog_row
from openpoly.db.market_catalog_store import make_market_catalog_sink, upsert_market_catalog_row
from openpoly.db.tables import MarketCatalogRow
from openpoly.db.writer import WriteBehindWriter
from openpoly.markets.manager import MarketSourceManager
from openpoly.markets.models import normalize_gamma_market


def _engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    return engine


def _raw_pair(market_id: str, *, question: str = "Q?"):
    raw = {
        "id": market_id,
        "conditionId": f"0x{market_id}",
        "question": question,
        "clobTokenIds": f'["yes-{market_id}", "no-{market_id}"]',
        "endDate": "2027-01-01T00:00:00Z",
        "bestBid": 0.40,
        "bestAsk": 0.42,
        "spread": 0.02,
        "volume24hr": 50_000.0,
        "liquidityNum": 20_000.0,
        "feesEnabled": False,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
    }
    return raw, {"id": "e1", "title": "E", "tags": []}


def _market(market_id: str, *, question: str = "Q?"):
    raw, event = _raw_pair(market_id, question=question)
    m = normalize_gamma_market(raw, event=event)
    assert m is not None
    return m


def test_init_db_creates_market_catalog_table(tmp_path):
    engine = _engine(tmp_path)
    with make_session_factory(engine)() as session:
        assert session.execute(select(MarketCatalogRow)).all() == []
    engine.dispose()


def test_upsert_inserts_new_row(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    with factory() as session:
        upsert_market_catalog_row(session, _market("m1"), now=100.0)
        session.commit()

    row = market_catalog_row(factory(), "m1")
    assert row.market_id == "m1"
    assert row.condition_id == "0xm1"
    assert row.yes_token_id == "yes-m1"
    assert row.no_token_id == "no-m1"
    assert row.first_seen_at == 100.0
    assert row.last_seen_at == 100.0
    engine.dispose()


def test_upsert_existing_row_preserves_first_seen_updates_rest(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    with factory() as session:
        upsert_market_catalog_row(session, _market("m1", question="Old?"), now=100.0)
        session.commit()
    with factory() as session:
        upsert_market_catalog_row(session, _market("m1", question="New?"), now=200.0)
        session.commit()

    with factory() as session:
        row = market_catalog_row(session, "m1")
        assert row.question == "New?"
        assert row.first_seen_at == 100.0  # preserved, not overwritten
        assert row.last_seen_at == 200.0
    engine.dispose()


def test_sink_persists_batch(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    make_market_catalog_sink(factory)([_market("a"), _market("b")])
    with factory() as session:
        rows = session.execute(select(MarketCatalogRow)).scalars().all()
    assert {r.market_id for r in rows} == {"a", "b"}
    engine.dispose()


def test_sink_empty_batch_is_noop(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    make_market_catalog_sink(factory)([])
    with factory() as session:
        assert session.execute(select(MarketCatalogRow)).all() == []
    engine.dispose()


def test_market_catalog_row_returns_none_for_unknown_market(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    with factory() as session:
        assert market_catalog_row(session, "nonexistent") is None
    engine.dispose()


# ---------- end-to-end: MarketSourceManager -> persist hook -> DB ----------


async def test_poll_persists_every_kept_market(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    writer = WriteBehindWriter(make_market_catalog_sink(factory))
    await writer.start()

    raw_a, event = _raw_pair("a")
    raw_b, _ = _raw_pair("b")

    async def fetcher(*, limit: int):
        return [(raw_a, event), (raw_b, event)]

    mgr = MarketSourceManager(fetcher=fetcher)
    mgr.set_market_persist(writer.enqueue)
    from openpoly.markets.manager import MarketSourceConfig

    mgr._config = MarketSourceConfig()
    await mgr._poll_once()

    await writer.stop()
    with factory() as session:
        rows = session.execute(select(MarketCatalogRow)).scalars().all()
    assert {r.market_id for r in rows} == {"a", "b"}
    engine.dispose()


async def test_holding_sync_persists_synced_market(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    writer = WriteBehindWriter(make_market_catalog_sink(factory))
    await writer.start()

    class _FakePortfolio:
        def get_open_positions(self):
            from dataclasses import dataclass

            @dataclass
            class _Held:
                market_id: str

            return [_Held(market_id="held-only")]

    async def market_fetcher(market_id: str):
        return _market(market_id)

    mgr = MarketSourceManager(market_fetcher=market_fetcher, portfolio_store=_FakePortfolio())
    mgr.set_market_persist(writer.enqueue)

    synced, failed = await mgr._sync_holdings_once()
    assert (synced, failed) == (1, 0)

    await writer.stop()
    with factory() as session:
        row = market_catalog_row(session, "held-only")
    assert row is not None
    engine.dispose()
