"""Atomicity, correlated/daily budgets and hostile-data boundary drills."""

from dataclasses import replace
from pathlib import Path

import pytest

from kalshi_bot import multi_market as mm
from tests.unit.test_multi_market import NOW, book, engine, fill, iso, market
from tests.unit.test_multi_market import released as released


def test_unrelated_database_is_never_adopted_or_changed(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "campaign.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated(value)")
        db.execute("INSERT INTO unrelated VALUES(123)")
    db.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unrelated database"):
        mm.PaperEngine(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        {"status": "closed"},
        {"rules_primary": ""},
        {"close_time": "not-a-time"},
        {"ticker": "OTHER-MARKET"},
        {"open_time": "2050-01-01T00:00:00Z"},
    ],
)
def test_unknown_market_metadata_is_rejected(change: dict) -> None:
    c = market(mm.Category.WEATHER)
    with pytest.raises((ValueError, KeyError)):
        mm.Contract.verify({**c.raw, **change}, c.series, at=NOW, now=NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"fee_multiplier": None},
        {"fee_multiplier": 0},
        {"fee_multiplier": True},
        {"fee_type": "unknown"},
        {"settlement_sources": []},
        {"category": "unmapped"},
    ],
)
def test_unknown_fees_category_and_settlement_source_are_rejected(change: dict) -> None:
    c = market(mm.Category.WEATHER)
    with pytest.raises(ValueError):
        mm.Contract.verify(c.raw, {**c.series, **change}, at=NOW, now=NOW)


def test_two_workers_cannot_duplicate_a_market(tmp_path: Path, released: None) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "trial.db"
    c = market(mm.Category.WEATHER)
    initial = engine(path)
    initial.activate()
    initial.close()
    barrier = Barrier(2)

    def worker() -> None:
        e = engine(path)
        try:
            barrier.wait(timeout=5)
            e.cycle([(c, book(c))], now=NOW)
        finally:
            e.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    e = engine(path)
    try:
        assert e.db.execute("SELECT COUNT(*) FROM mm_intents").fetchone()[0] == 1
        assert e.db.execute("SELECT COUNT(*) FROM mm_orders").fetchone()[0] == 1
        e.cycle([(c, book(c, NOW + 1))], now=NOW + 1)
        assert e.db.execute("SELECT COUNT(*) FROM mm_fills").fetchone()[0] == 1
    finally:
        e.close()


def test_new_trial_is_included_in_offline_backup_restore(tmp_path: Path) -> None:
    from kalshi_bot.hosting import backup, restore
    from kalshi_bot.paper import PaperBroker

    root = tmp_path / "data"
    root.mkdir()
    PaperBroker(root / "paper.db").close()
    e = mm.PaperEngine(root / "multi-paper.db")
    e.shutdown(now=NOW, reason="backup drill stop")
    e.close()
    destination = tmp_path / "backup"
    manifest = backup(root, destination)
    assert "multi-paper.db" in manifest["files"]
    restore(destination, tmp_path / "recovered")
    recovered = mm.PaperEngine(tmp_path / "recovered/multi-paper.db")
    try:
        assert (
            recovered.db.execute("SELECT reason FROM mm_control").fetchone()[0]
            == "backup drill stop"
        )
        assert recovered.db.execute("SELECT cash FROM mm_account").fetchone()[0] == 5000
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_restart_cannot_extend_original_runner_deadline(
    tmp_path: Path, released: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kalshi_bot import multi_feed

    e = engine(tmp_path / "trial.db")
    try:
        e.activate()
        e.db.execute("CREATE TABLE mm_session(id INTEGER PRIMARY KEY,deadline REAL)")
        e.db.execute("INSERT INTO mm_session VALUES(1,?)", (NOW - 1,))
        monkeypatch.setattr(multi_feed.time, "time", lambda: NOW)
        await multi_feed.run(e, seconds=21600)
        assert e.db.execute("SELECT deadline FROM mm_session").fetchone()[0] == NOW - 1
        assert e.db.execute("SELECT killed FROM mm_control").fetchone()[0] == 1
        assert not e.db.execute("SELECT 1 FROM sqlite_master WHERE name='mm_reads'").fetchone()
    finally:
        e.close()


def test_status_cli_does_not_create_a_trial(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from kalshi_bot.cli import app

    path = tmp_path / "missing.db"
    response = CliRunner().invoke(app, ["multi-status", "--ledger", str(path)])
    assert response.exit_code == 0
    assert not path.exists()
    assert "null" in response.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_ok", [True, False])
async def test_bounded_mock_demo_loop_discovers_buys_sells_and_stops(
    tmp_path: Path, released: None, monkeypatch: pytest.MonkeyPatch, quote_ok: bool
) -> None:
    import asyncio
    import time
    from types import SimpleNamespace

    import httpx
    import respx

    from kalshi_bot import multi_feed

    clock = SimpleNamespace(at=NOW)
    monotonic = time.monotonic
    monkeypatch.setattr(
        multi_feed, "time", SimpleNamespace(time=lambda: clock.at, monotonic=monotonic)
    )

    async def advance(delay: float) -> None:
        clock.at += delay
        await asyncio.sleep(0)

    monkeypatch.setattr(
        multi_feed, "asyncio", SimpleNamespace(sleep=advance, wait_for=asyncio.wait_for)
    )
    c = market(mm.Category.WEATHER, closes=NOW + 50)
    e = engine(tmp_path / "trial.db")
    e.activate()
    if not quote_ok:
        multi_feed.MultiFeed(e)
        e.db.execute(
            "INSERT INTO mm_reads VALUES(NULL,?,'/prior-synthetic-idle','{}','{}',NULL)",
            (NOW - 601,),
        )

    def response(request: httpx.Request) -> httpx.Response:
        yes = "0.70" if clock.at >= NOW + 4 else "0.40"
        no = "0.28" if clock.at >= NOW + 4 else "0.58"
        return httpx.Response(
            200,
            json={
                "quote_timestamp": clock.at if quote_ok else None,
                "orderbook_fp": {"yes_dollars": [[yes, "12"]], "no_dollars": [[no, "12"]]},
            },
        )

    base = "https://demo-api.kalshi.co/trade-api/v2"
    try:
        with respx.mock(assert_all_called=True) as api:
            api.get(base + "/markets").respond(200, json={"markets": [c.raw], "cursor": ""})
            api.get(base + "/events/" + c.event).respond(
                200, json={"event": {"event_ticker": c.event, "series_ticker": "TEST"}}
            )
            api.get(base + "/series/TEST").respond(200, json={"series": c.series})
            api.get(base + "/markets/" + c.ticker).respond(200, json={"market": c.raw})
            api.get(base + "/markets/" + c.ticker + "/orderbook").mock(side_effect=response)
            if quote_ok:
                await multi_feed.run(e, seconds=7, interval=2)
            else:
                with pytest.raises(ValueError, match="ten minutes"):
                    await multi_feed.run(e, seconds=7, interval=2)
        if not quote_ok:
            assert e.db.execute("SELECT COUNT(*) FROM mm_fills").fetchone()[0] == 0
            assert e.db.execute("SELECT killed FROM mm_control").fetchone()[0] == 1
            return
        assert clock.at == NOW + 7
        assert e.db.execute("SELECT COUNT(*) FROM mm_fills").fetchone()[0] == 2
        assert e.db.execute("SELECT status FROM mm_intents").fetchone()[0] == "closed"
        assert e.db.execute("SELECT killed FROM mm_control").fetchone()[0] == 1
        assert e.db.execute("SELECT deadline FROM mm_session").fetchone()[0] == NOW + 7
    finally:
        e.close()


@pytest.mark.asyncio
async def test_rollover_reseed_never_resets_full_catalog_cursor(tmp_path: Path) -> None:
    import httpx
    import respx

    from kalshi_bot.exchange.client import KalshiClient
    from kalshi_bot.multi_feed import MultiFeed

    seen = []
    e = engine(tmp_path / "trial.db")

    def response(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor", "")
        seen.append(cursor)
        return httpx.Response(200, json={"markets": [], "cursor": str(int(cursor or 0) + 1)})

    try:
        with respx.mock() as api:
            api.get("https://demo-api.kalshi.co/trade-api/v2/markets").mock(side_effect=response)
            async with KalshiClient("https://demo-api.kalshi.co") as client:
                feed = MultiFeed(e)
                for _ in range(6):
                    await feed.scan(client)
        assert e.db.execute("SELECT cursor FROM mm_feed_state").fetchone()[0] == "6"
        assert seen == ["", "1", "2", "3", "4", "", "5"]
        assert e.db.execute("SELECT COUNT(*) FROM mm_orders").fetchone()[0] == 0
    finally:
        e.close()


@pytest.mark.parametrize("p,expected", [(0.61, None), (0.63, "yes")])
def test_edge_includes_possible_exit_fees_and_slippage(
    tmp_path: Path, released: None, p: float, expected: str | None
) -> None:
    from tests.unit.test_multi_market import FixtureModel

    c = market(mm.Category.SPORTS)
    decision = mm.rank(c, book(c), FixtureModel(p), now=NOW)
    assert decision.side == expected
    if expected:
        assert decision.edge == 0.08


def test_dashboard_is_mobile_readonly_and_honest_when_missing(tmp_path: Path) -> None:
    from kalshi_bot.multi_dashboard import render, snapshot

    html = render(snapshot(tmp_path / "missing.db"))
    assert 'name="viewport"' in html
    assert "UNVERIFIED" in html and "Unknown" in html
    assert "<script" not in html and "$50" not in html


@pytest.mark.parametrize(
    "route,host,expected",
    [
        ("/", "127.0.0.1:8766", 200),
        ("/status.json", "localhost:8766", 200),
        ("/start", "127.0.0.1:8766", 404),
        ("/", "attacker.invalid:8766", 421),
    ],
)
def test_dashboard_routes_and_host_guard_without_a_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, host: str, expected: int
) -> None:
    from io import BytesIO

    from kalshi_bot import multi_dashboard

    statuses = []
    headers = {}

    class FakeServer:
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 8766)
            self.handler = handler

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def serve_forever(self):
            h = self.handler.__new__(self.handler)
            h.path, h.headers, h.wfile = route, {"Host": host}, BytesIO()
            h.send_response = lambda code: statuses.append(code)
            h.send_error = lambda code: statuses.append(code)
            h.send_header = lambda key, value: headers.update({key: value})
            h.end_headers = lambda: None
            h.do_GET()
            if expected == 200:
                assert h.wfile.getvalue()
                assert headers["Cache-Control"] == "no-store"
                assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    monkeypatch.setattr(multi_dashboard, "HTTPServer", FakeServer)
    path = tmp_path / "missing.db"
    multi_dashboard.serve(path)
    assert statuses == [expected]
    assert not path.exists()


def test_mid_fill_failure_rolls_back_money_and_arms_sticky_kill(
    tmp_path: Path, released: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "trial.db"
    c = market(mm.Category.SPORTS)
    e = engine(path)
    e.activate()
    e.cycle([(c, book(c))], now=NOW)
    original = e.event

    def fail(at: float, kind: str, ticker: str, payload: object) -> None:
        if kind == "fill":
            raise RuntimeError("injected power-loss boundary")
        original(at, kind, ticker, payload)

    monkeypatch.setattr(e, "event", fail)
    with pytest.raises(RuntimeError, match="power-loss"):
        e.cycle([(c, book(c, NOW + 1))], now=NOW + 1)
    assert e.db.execute("SELECT cash FROM mm_account").fetchone()[0] == 5000
    assert e.db.execute("SELECT COUNT(*) FROM mm_fills").fetchone()[0] == 0
    e.close()
    resumed = engine(path)
    try:
        assert resumed.db.execute("SELECT killed FROM mm_control").fetchone()[0] == 1
        resumed.cycle([(c, book(c, NOW + 2))], now=NOW + 2)
        assert resumed.db.execute("SELECT COUNT(*) FROM mm_fills").fetchone()[0] == 0
    finally:
        resumed.close()


def test_three_large_exposures_cannot_bypass_daily_budget(tmp_path: Path, released: None) -> None:
    e = engine(tmp_path / "trial.db")
    cs = [
        market(c, ticker=str(i))
        for i, c in enumerate((mm.Category.CRYPTO, mm.Category.SPORTS, mm.Category.WEATHER))
    ]
    try:
        e.activate()
        e.cycle([(c, book(c, yes=0.70)) for c in cs], now=NOW)
        assert e.db.execute("SELECT COUNT(*) FROM mm_intents").fetchone()[0] == 2
        assert e.db.execute("SELECT SUM(risk) FROM mm_intents").fetchone()[0] <= 200
    finally:
        e.close()


def test_daily_gains_never_replenish_gross_loss_budget(tmp_path: Path, released: None) -> None:
    e = engine(tmp_path / "trial.db")
    try:
        e.activate()
        for i, result in enumerate(("yes", "no", "no")):
            at = NOW + i * 20
            c = replace(market(mm.Category.CRYPTO, ticker=str(i), closes=at + 10), metadata_at=at)
            e.cycle([(c, book(c, at, yes=0.70))], now=at)
            e.cycle([(c, book(c, at + 1, yes=0.70))], now=at + 1)
            final = {
                **c.raw,
                "status": "finalized",
                "result": result,
                "settlement_ts": iso(at + 11),
                "settlement_value_dollars": "1.0000" if result == "yes" else "0.0000",
            }
            e.settle(c, final, received_at=at + 11, now=at + 11)
        at = NOW + 65
        c = replace(market(mm.Category.CRYPTO, ticker="new", closes=at + 100), metadata_at=at)
        e.cycle([(c, book(c, at, yes=0.70))], now=at)
        assert e.db.execute("SELECT COUNT(*) FROM mm_intents").fetchone()[0] == 3
    finally:
        e.close()


@pytest.mark.parametrize(
    "change",
    [
        {"status": "closed"},
        {"result": "unknown"},
        {"settlement_value_dollars": "0.5"},
        {"rules_primary": "changed"},
        {"ticker": "wrong"},
    ],
)
def test_bad_settlement_never_credits_money(tmp_path: Path, released: None, change: dict) -> None:
    c = market(mm.Category.WEATHER)
    e = engine(tmp_path / "trial.db")
    try:
        e.activate()
        fill(e, c)
        before = e.db.execute("SELECT cash FROM mm_account").fetchone()[0]
        final = {
            **c.raw,
            "status": "finalized",
            "result": "yes",
            "settlement_ts": iso(c.closes + 10),
            "settlement_value_dollars": "1.0000",
        }
        with pytest.raises(ValueError):
            e.settle(c, {**final, **change}, received_at=c.closes + 10, now=c.closes + 10)
        assert e.db.execute("SELECT cash FROM mm_account").fetchone()[0] == before
        assert e.db.execute("SELECT status FROM mm_intents").fetchone()[0] == "held"
    finally:
        e.close()
