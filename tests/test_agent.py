from datetime import datetime, timedelta, timezone
import asyncio

import httpx
import pytest

from kronbot.agent import Agent
from kronbot.sport import SportClient, SportError
from kronbot.state import StateStore


async def test_booking_and_uncertain_reconciliation(tmp_path):
    now = 1900000000
    start = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    posts = []
    mode = {"fail": True, "calendar": []}
    def handler(request):
        path = request.url.path
        if path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 500001, "section_name": "Секция", "room_name": "Зал", "date": start, "section_level": 1, "type_id": 2, "available": 1, "signed": False, "intersection": False, "can_sign_in": {"can_sign_in": True}}]}]})
        if path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": mode["calendar"]}]})
        if path.endswith("/personal/have_attempts"):
            return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
        posts.append(request.content)
        if mode["fail"]:
            raise httpx.ReadError("synthetic")
        return httpx.Response(200, json={"error_code": 0, "result": [500001]})
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 1000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        with pytest.raises(SportError):
            await agent.check(allow_booking=True, only_lesson_id=500001)
        assert posts == [b"[500001]"]
        assert agent.queue()[0]["status"] == "uncertain"
        await sport.close()
    with StateStore(directory) as store:
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now + 121)
        agent = Agent(store, sport, now=lambda: now + 121)
        await agent.check(allow_booking=True)
        assert agent.queue()[0]["status"] == "uncertain" and len(posts) == 1
        mode["calendar"] = [{"id": 500001, "signed": False}]
        await agent.check(allow_booking=False)
        assert agent.queue()[0]["status"] == "booked" and len(posts) == 1
        await sport.close()


@pytest.mark.parametrize("status,delay", [(429, 300), (403, 86400)])
async def test_remote_restrictions_survive_restart(tmp_path, status, delay):
    from kronbot.auth import Restricted
    from kronbot.sport import LocalLimit
    now = 1900000000
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "300"})
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 100000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        with pytest.raises(Restricted):
            await client.schedule(datetime.now().date(), datetime.now().date())
        await client.close()
    with StateStore(directory) as store:
        assert store.data["blocked_until"]["my.itmo.ru"] == now + delay
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now + 1)
        with pytest.raises(LocalLimit):
            await client.schedule(datetime.now().date(), datetime.now().date())
        assert len(calls) == 1
        await client.close()


async def test_request_budget_persists(tmp_path):
    from kronbot.sport import LocalLimit
    now = 1900000000
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"error_code": 0, "result": []})
    directory = tmp_path / "state"
    for batch in range(2):
        with StateStore(directory) as store:
            if batch == 0:
                store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
                store.save()
            client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
            for _ in range(30):
                await client.schedule(datetime.now().date(), datetime.now().date())
            if batch:
                with pytest.raises(LocalLimit):
                    await client.schedule(datetime.now().date(), datetime.now().date())
            await client.close()
    assert len(calls) == 60


@pytest.mark.parametrize("body,expected,code", [
    ({"error_code": 137, "error_message": "секрет студента", "result": None}, "rejected", 137),
    ({"error_code": 0, "result": []}, "uncertain", None),
])
async def test_enrollment_results_are_safe(tmp_path, body, expected, code):
    from kronbot.sport import Lesson
    now = 1900000000
    start = datetime.fromtimestamp(now + 86400, timezone.utc)
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=body)
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 1, "date": start.isoformat(), "section_level": 1, "type_id": 2, "available": 1, "signed": False, "can_sign_in": {"can_sign_in": True}}]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, client, now=lambda: now)
        await agent.check(allow_booking=True, only_lesson_id=1)
        assert agent.queue()[0]["status"] == expected
        assert agent.queue()[0]["refusal_code"] == code
        assert "секрет" not in str(agent.queue())
        await client.close()


async def test_pause_during_attempt_check_prevents_post(tmp_path):
    now = 1900000000
    start = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    entered, release = asyncio.Event(), asyncio.Event()
    posts = []
    async def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"error_code": 0, "result": [1]})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 1, "date": start, "section_level": 1, "type_id": 2, "available": 1, "signed": False, "can_sign_in": {"can_sign_in": True}}]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 1000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, client, now=lambda: now)
        task = asyncio.create_task(agent.check(allow_booking=True, only_lesson_id=1))
        await entered.wait()
        agent.booking_enabled = False
        release.set()
        await task
        assert not posts
        assert agent.queue()[0]["status"] == "waiting"
        await client.close()


async def test_401_refreshes_once_and_persists_rotation(tmp_path):
    now = 1900000000
    requests = []
    def sport_handler(request):
        requests.append(request.headers["Authorization"])
        if len(requests) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"error_code": 0, "result": []})
    refreshes = []
    def auth_handler(request):
        refreshes.append(request.content)
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 1800, "refresh_expires_in": 3600})
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = {"sub": "owner", "access_token": "old-access", "access_expires_at": now + 1000, "refresh_token": "old-refresh", "refresh_expires_at": now + 3600}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(sport_handler), auth_transport=httpx.MockTransport(auth_handler), now=lambda: now)
        await client.schedule(datetime.now().date(), datetime.now().date())
        assert requests == ["Bearer old-access", "Bearer new-access"]
        assert len(refreshes) == 1
        await client.close()
    with StateStore(directory) as store:
        assert store.data["session"]["refresh_token"] == "new-refresh"
