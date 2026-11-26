from datetime import datetime, timezone

import httpx
import pytest
from textual.widgets import Button, DataTable

from kronbot.state import StateStore
from kronbot.sport import SportClient
from kronbot.tui import KronbotApp, RiskScreen


async def test_risk_gate_and_read_only_refresh(tmp_path):
    now = 1900000000
    calls = []
    start = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 500001, "date": start, "section_name": "Секция", "room_name": "Зал", "section_level": 1, "type_id": 2, "available": 0, "signed": False, "can_sign_in": {"can_sign_in": True}}]}]})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        app = KronbotApp(store, sport=client, now=lambda: now)
        async with app.run_test(size=(80, 24)) as pilot:
            assert isinstance(app.screen, RiskScreen)
            assert not calls
            assert app.screen.focused.id == "exit"
            await pilot.click("#continue")
            await pilot.pause()
            assert len(calls) == 1 and calls[0].method == "GET"
            assert app.query_one("#schedule", DataTable).row_count == 1
            assert not app.auto
        await client.close()


async def test_queue_and_auto_booking(tmp_path):
    clock = {"wall": 1900000000, "mono": 0, "available": 0}
    start = datetime.fromtimestamp(clock["wall"] + 86400, timezone.utc).isoformat()
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": [500001]})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 500001, "date": start, "section_name": "Секция", "room_name": "Зал", "section_level": 1, "type_id": 2, "available": clock["available"], "signed": False, "can_sign_in": {"can_sign_in": True}}]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": clock["wall"] + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: clock["wall"])
        app = KronbotApp(store, sport=client, now=lambda: clock["wall"], monotonic=lambda: clock["mono"])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#continue")
            await pilot.pause()
            app.query_one("#schedule", DataTable).focus()
            await pilot.press("enter")
            assert app.agent.queue()[0]["status"] == "waiting"
            await pilot.press("r", "p")
            await pilot.pause()
            assert not posts and app.agent.queue()[0]["status"] == "waiting"
            clock["available"] = 1
            clock["wall"] += 121
            clock["mono"] += 121
            app._tick()
            await pilot.pause()
            assert posts == [b"[500001]"]
            assert app.agent.queue()[0]["status"] == "booked"
        await client.close()


async def test_login_password_is_masked_and_escape_returns(tmp_path):
    from textual.widgets import Input, Select
    from kronbot.tui import LoginScreen
    with StateStore(tmp_path / "state") as store:
        app = KronbotApp(store)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#continue")
            await pilot.pause()
            assert isinstance(app.screen, LoginScreen)
            app.screen.query_one("#method", Select).value = "password"
            await pilot.pause()
            secret = app.screen.query_one("#secret", Input)
            assert secret.password
            secret.value = "synthetic-password"
            await pilot.press("escape")
            await pilot.pause()
            assert secret.value == ""
