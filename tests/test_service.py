import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from kronbot.service import BackgroundRunner
from kronbot.sport import SportClient
from kronbot.state import StateStore


def session(now):
    return {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000,
            "refresh_token": None, "refresh_expires_at": None}


def lesson(ident, stamp, available):
    return {"id": ident, "date": stamp, "section_name": "Секция", "room_name": "Зал", "section_level": 1,
            "type_id": 2, "available": available, "signed": False, "can_sign_in": {"can_sign_in": True}}


async def test_service_releases_lock_and_books_new_queue_items(tmp_path):
    clock = {"wall": 1900000000, "mono": 100, "places": 0}
    stamp = datetime.fromtimestamp(clock["wall"] + 86400, timezone.utc).isoformat()
    posts, calls = [], []
    def handler(request):
        calls.append(request.url.path)
        if request.method == "POST":
            posts.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": [1]})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [lesson(1, stamp, clock["places"])]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = session(clock["wall"])
        store.save()
    runner = BackgroundRunner(directory, now=lambda: clock["wall"], monotonic=lambda: clock["mono"],
                              client_factory=lambda store, now: SportClient(store, now=now, transport=httpx.MockTransport(handler)))
    await runner.cycle()
    assert not calls
    with StateStore(directory) as store:
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Секция", "starts_at": stamp, "status": "waiting", "refusal_code": None}]
        store.save()
    await runner.cycle()
    assert not posts and len(calls) == 1
    clock["places"] = 1
    await runner.cycle()
    assert len(calls) == 1
    clock["wall"] += 121
    clock["mono"] += 121
    await runner.cycle()
    assert posts == [b"[1]"]
    with StateStore(directory) as store:
        assert store.data["queues"]["owner"][0]["status"] == "booked"
    clock["wall"] += 121
    clock["mono"] += 121
    before = len(calls)
    await runner.cycle()
    assert len(calls) == before


async def test_restriction_pauses_across_service_restart(tmp_path):
    now = 1900000000
    stamp = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "300"})
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = session(now)
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Секция", "starts_at": stamp, "status": "waiting", "refusal_code": None}]
        store.save()
    def make(current):
        return BackgroundRunner(directory, now=lambda: current, client_factory=lambda store, now: SportClient(store, now=now, transport=httpx.MockTransport(handler)))
    await make(now).cycle()
    with StateStore(directory) as store:
        assert store.data["background_paused"] is True
        assert store.data["blocked_until"]["my.itmo.ru"] == now + 300
    await make(now + 301).cycle()
    assert len(calls) == 1


async def test_stop_before_post_prevents_booking(tmp_path):
    now = 1900000000
    stamp = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    reached, release = asyncio.Event(), asyncio.Event()
    posts = []
    async def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"error_code": 0, "result": [1]})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [lesson(1, stamp, 1)]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        reached.set()
        await release.wait()
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = session(now)
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Секция", "starts_at": stamp, "status": "waiting", "refusal_code": None}]
        store.save()
    runner = BackgroundRunner(directory, now=lambda: now, client_factory=lambda store, now: SportClient(store, now=now, transport=httpx.MockTransport(handler)))
    task = asyncio.create_task(runner.cycle())
    await reached.wait()
    runner.stop()
    release.set()
    await task
    with StateStore(directory) as store:
        assert store.data["queues"]["owner"][0]["status"] == "waiting"
    assert not posts


async def test_rotated_tokens_are_kept_until_saved(tmp_path, monkeypatch):
    from kronbot.state import StateError
    now = 1900000000
    stamp = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = {"sub": "owner", "access_token": "old", "access_expires_at": now + 1,
                                 "refresh_token": "old-refresh", "refresh_expires_at": now + 10000}
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Секция", "starts_at": stamp, "status": "waiting", "refusal_code": None}]
        store.save()
    refreshes, requests = [], []
    def auth_handler(request):
        refreshes.append(request)
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "new-refresh", "expires_in": 1800, "refresh_expires_in": 3600})
    def sport_handler(request):
        requests.append(request)
        return httpx.Response(200, json={"error_code": 0, "result": []})
    real_save = StateStore.save
    failures = []
    def flaky_save(store):
        if store.data["session"] and store.data["session"]["access_token"] == "new" and not failures:
            failures.append(True)
            raise StateError("synthetic save failure")
        return real_save(store)
    monkeypatch.setattr(StateStore, "save", flaky_save)
    runner = BackgroundRunner(directory, now=lambda: now, save_retry_delay=0.01,
                              client_factory=lambda store, now: SportClient(store, now=now,
                                  transport=httpx.MockTransport(sport_handler),
                                  auth_transport=httpx.MockTransport(auth_handler)))
    await runner.cycle()
    assert len(refreshes) == 1 and not requests
    with StateStore(directory) as store:
        assert store.data["session"]["refresh_token"] == "new-refresh"
