"""The research package must not import an order client."""

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from pmarb.safety import assert_no_order_client


def test_core_imports_do_not_load_order_modules() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    code = """
import sys
sys.path.insert(0, sys.argv[1])
before = set(sys.modules)
import pmarb
import pmarb.economics
import pmarb.fees
import pmarb.cli
import pmarb.app.queries
import pmarb.app.decisions
import streamlit
pmarb.safety.assert_no_order_client()
loaded = [name for name in sys.modules if name not in before]
bad = [
    name
    for name in loaded
    if name != "streamlit.commands.execution_control"
    and ("orders" in name.lower() or "execution" in name.lower())
]
print("\\n".join(bad))
raise SystemExit(1 if bad else 0)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src)
    completed = subprocess.run(
        [sys.executable, "-c", code, str(Path(__file__).resolve().parents[1] / "src")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == ""


@pytest.mark.parametrize("name", ["pmarb.orders", "pmarb.execution", "broker.orders"])
def test_guard_rejects_an_injected_order_module(name: str) -> None:
    sys.modules[name] = types.ModuleType(name)
    try:
        with pytest.raises(RuntimeError, match="order submission"):
            assert_no_order_client()
    finally:
        del sys.modules[name]


def test_package_source_has_no_order_endpoints() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "pmarb"
    literals = (
        "/portfolio/orders",
        "/portfolio/events/orders",
        "/v1/orders",
        "orders.create",
        "ORDER_INTENT_",
    )
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for literal in literals:
            if literal in text:
                offenders.append(f"{path.name}: {literal}")
    assert offenders == []


def test_no_personal_home_paths() -> None:
    root = Path(__file__).resolve().parents[1]
    pattern = "/Users" + "/|/" + "home/"
    completed = subprocess.run(
        ["git", "grep", "-nE", pattern, "--", ".", ":(exclude)tests/fixtures"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr


async def test_adapters_record_only_get_requests() -> None:
    from datetime import UTC, datetime, timedelta

    import httpx

    from pmarb.config import Settings
    from pmarb.gamestate.kalshi_live import KalshiLiveClient
    from pmarb.http import ObservationHttpClient, TokenBucket
    from pmarb.venues.kalshi.rest import KalshiAdapter
    from pmarb.venues.polymarket_us.rest import PolymarketUSAdapter

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json={})

    buckets = {
        key: TokenBucket(1000, 100)
        for key in (
            ("kalshi", "books"),
            ("kalshi", "discovery"),
            ("kalshi", "gamestate"),
            ("polyus", "books"),
            ("polyus", "discovery"),
        )
    }
    now = datetime(2030, 1, 5, tzinfo=UTC)

    async def drain(stream: object) -> None:
        try:
            async for _item in stream:  # type: ignore[attr-defined]
                pass
        except Exception:  # noqa: BLE001 - record the request even if parsing fails
            return

    async def call(coro: object) -> None:
        try:
            await coro  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - record the request even if parsing fails
            return

    async with ObservationHttpClient(
        Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        buckets=buckets,
        max_retries=0,
    ) as http:
        kalshi = KalshiAdapter(http, series_tickers=["KXNFLGAME"], clock_utc=lambda: now)
        poly = PolymarketUSAdapter(http, leagues=["nfl"], clock_utc=lambda: now)
        live = KalshiLiveClient(http, clock_utc=lambda: now)
        horizon = now + timedelta(days=1)
        await drain(kalshi.list_markets(now, horizon))
        await call(kalshi.get_market("TICKER"))
        await call(kalshi.get_books(["TICKER"]))
        await drain(poly.list_markets(now, horizon))
        await call(poly.get_market("slug"))
        await call(poly.get_books(["slug"]))
        await call(live.resolve_milestones(["TICKER"]))
        await call(live.fetch_live(["milestone"]))
    assert seen
    assert set(seen) == {"GET"}
