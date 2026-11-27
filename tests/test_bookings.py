from datetime import datetime, timedelta, timezone
import asyncio
import json

import httpx
import pytest

from kronbot.agent import Agent, MSK, QueueError
from kronbot.sport import Booking, LocalLimit, SportClient, SportError
from kronbot.state import StateError, StateStore


NOW = 1900000000
START = datetime.fromtimestamp(NOW + 86400, timezone.utc)


def lesson(ident=101, *, level=1, group=20):
    return {"id": ident, "date": START.isoformat(), "section_name": "Тест\nсекция",
            "room_name": "Зал", "section_level": level, "lesson_group_id": group,
            "signed": False}


def session(store):
    store.data["session"] = {"sub": "owner", "access_token": "token", "access_expires_at": NOW + 10000,
                             "refresh_token": None, "refresh_expires_at": None}
    store.save()


def queued(ident=101):
    return {"lesson_id": ident, "section_name": "Тест", "starts_at": START.isoformat(),
            "status": "waiting", "refusal_code": None}


def response(result):
    return httpx.Response(200, json={"error_code": 0, "result": result})


async def test_calendar_displays_unsigned_and_range(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return response([{"lessons": [lesson()]}, {"lessons": None}])
    with StateStore(tmp_path / "state") as store:
        session(store)
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        try:
            bookings = await agent.bookings()
            assert bookings == [Booking(101, START, "Тест секция", "Зал", 1, 20)]
            today = datetime.fromtimestamp(NOW, MSK).date()
            assert requests[0].url.params["date_start"] == today.isoformat()
            assert requests[0].url.params["date_end"] == (today + timedelta(days=364)).isoformat()
        finally:
            await client.close()


async def test_ongoing_booking_is_visible_but_cannot_be_cancelled(tmp_path):
    ongoing, finished = lesson(101), lesson(102)
    ongoing["date"] = datetime.fromtimestamp(NOW - 60, timezone.utc).isoformat()
    ongoing["date_end"] = datetime.fromtimestamp(NOW + 3600, timezone.utc).isoformat()
    finished["date"] = datetime.fromtimestamp(NOW - 7200, timezone.utc).isoformat()
    finished["date_end"] = datetime.fromtimestamp(NOW - 3600, timezone.utc).isoformat()
    calls = []
    def handler(request):
        calls.append(request)
        return response([{"lessons": [ongoing, finished]}])
    with StateStore(tmp_path / "state") as store:
        session(store)
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        try:
            bookings = await agent.bookings()
            assert [item.id for item in bookings] == [101]
            with pytest.raises(QueueError):
                await agent.cancel_booking(bookings[0])
            assert all(request.method == "GET" for request in calls)
        finally:
            await client.close()


async def test_calendar_displays_optional_level_and_null_room_without_guessing_cancellation(tmp_path):
    item = lesson()
    del item["section_level"]
    item["room_name"] = None
    calls = []
    def handler(request):
        calls.append(request)
        return response([{"lessons": [item]}])
    with StateStore(tmp_path / "state") as store:
        session(store)
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        try:
            booking = (await agent.bookings())[0]
            assert booking.section_level == 0 and booking.room_name == ""
            with pytest.raises(QueueError):
                await agent.cancel_booking(booking)
            assert all(request.method == "GET" for request in calls)
        finally:
            await client.close()


@pytest.mark.parametrize("field,value", [("lesson_group_id", True), ("lesson_group_id", 0),
                                         ("section_level", True), ("date", "2026-01-01T12:00:00")])
async def test_calendar_rejects_invalid_metadata(tmp_path, field, value):
    item = lesson()
    item[field] = value
    with StateStore(tmp_path / "state") as store:
        session(store)
        client = SportClient(store, transport=httpx.MockTransport(lambda request: response([{"lessons": [item]}])), now=lambda: NOW)
        try:
            with pytest.raises(SportError):
                await client.bookings(START.date(), START.date())
        finally:
            await client.close()


@pytest.mark.parametrize("invalid", [True, 0, -1, "101", None])
async def test_calendar_rejects_invalid_ids(tmp_path, invalid):
    item = lesson()
    item["id"] = invalid
    with StateStore(tmp_path / "state") as store:
        session(store)
        client = SportClient(store, transport=httpx.MockTransport(lambda request: response([{"lessons": [item]}])), now=lambda: NOW)
        try:
            with pytest.raises(SportError):
                await client.bookings(START.date(), START.date())
        finally:
            await client.close()


@pytest.mark.parametrize("days", [{}, [None], [{"lessons": {}}], [{"lessons": [lesson(), lesson()]}]])
async def test_calendar_rejects_incomplete_or_ambiguous_structures(tmp_path, days):
    with StateStore(tmp_path / "state") as store:
        session(store)
        client = SportClient(store, transport=httpx.MockTransport(lambda request: response(days)), now=lambda: NOW)
        try:
            with pytest.raises(SportError):
                await client.bookings(START.date(), START.date())
        finally:
            await client.close()


async def test_cancel_removes_queue_only_after_confirmed_delete(tmp_path):
    calls = []
    with StateStore(tmp_path / "state") as store:
        session(store)
        store.data["queues"]["owner"] = [queued(), queued(102)]
        store.save()
        def handler(request):
            calls.append(request)
            if request.method == "DELETE":
                assert store.data["queues"]["owner"][0]["status"] == "uncertain"
                assert json.loads(request.content) == [101]
                return response([101])
            return response([{"lessons": [lesson()]}])
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        try:
            booking = (await agent.bookings())[0]
            await agent.cancel_booking(booking)
            assert [item["lesson_id"] for item in agent.queue()] == [102]
            assert [request.method for request in calls] == ["GET", "GET", "DELETE"]
        finally:
            await client.close()
    with StateStore(tmp_path / "state") as store:
        assert [item["lesson_id"] for item in store.data["queues"]["owner"]] == [102]


@pytest.mark.parametrize("failure", ["empty", "malformed", "network", "unauthorized"])
async def test_unknown_cancel_survives_restart_without_reenrollment(tmp_path, failure):
    calls = []
    directory = tmp_path / "state"
    def handler(request):
        calls.append(request.method)
        if request.method == "DELETE":
            if failure == "network":
                raise httpx.ReadTimeout("secret", request=request)
            if failure == "unauthorized":
                return httpx.Response(401)
            return response([] if failure == "empty" else [True, 101])
        if request.url.path.endswith("sign/schedule"):
            item = lesson()
            item.update(type_id=2, available=1, signed=False, can_sign_in={"can_sign_in": True})
            return response([{"lessons": [item]}])
        if len(calls) > 2:
            return response([])
        return response([{"lessons": [lesson()]}])
    with StateStore(directory) as store:
        session(store)
        store.data["queues"]["owner"] = [queued()]
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        try:
            booking = (await agent.bookings())[0]
            with pytest.raises(SportError, match="Исход отмены неизвестен"):
                await agent.cancel_booking(booking)
            assert agent.queue()[0]["status"] == "uncertain"
            assert calls.count("DELETE") == 1
        finally:
            await client.close()
    with StateStore(directory) as store:
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW + 121)
        agent = Agent(store, client, now=lambda: NOW + 121)
        try:
            assert agent.queue()[0]["status"] == "uncertain"
            await agent.check(allow_booking=True)
            assert agent.queue()[0]["status"] == "uncertain"
            assert "POST" not in calls
            assert calls.count("DELETE") == 1
        finally:
            await client.close()


async def test_group_cancel_protects_and_removes_every_visible_lesson(tmp_path):
    calls = []
    with StateStore(tmp_path / "state") as store:
        session(store)
        store.data["queues"]["owner"] = [queued(), queued(102), queued(103)]
        store.save()
        def handler(request):
            calls.append(request)
            if request.method == "DELETE":
                assert request.url.path.endswith("/lesson_groups/20")
                assert not request.content
                assert [item["status"] for item in store.data["queues"]["owner"]] == ["uncertain", "uncertain", "waiting"]
                return response(None)
            return response([{"lessons": [lesson(level=2), lesson(102, level=3), lesson(103, level=2, group=21)]}])
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        try:
            booking = (await agent.bookings())[0]
            await agent.cancel_booking(booking)
            assert [item["lesson_id"] for item in agent.queue()] == [103]
            assert sum(request.method == "DELETE" for request in calls) == 1
        finally:
            await client.close()


async def test_group_without_id_rejected_without_side_effects(tmp_path):
    calls = []
    with StateStore(tmp_path / "state") as store:
        session(store)
        client = SportClient(store, transport=httpx.MockTransport(lambda request: calls.append(request)), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        try:
            with pytest.raises(QueueError):
                await agent.cancel_booking(Booking(101, START, "Тест", "Зал", 2, None))
            assert not calls
            assert store.data["queues"] == {}
        finally:
            await client.close()


async def test_cancellation_respects_persistent_request_budget(tmp_path):
    calls = []
    with StateStore(tmp_path / "state") as store:
        session(store)
        store.data["sport_request_times"] = [NOW] * 59
        store.save()
        def handler(request):
            calls.append(request)
            return response([{"lessons": [lesson()]}])
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        try:
            with pytest.raises(LocalLimit):
                await agent.cancel_booking(Booking(101, START, "Тест", "Зал", 1, 20))
            assert [request.method for request in calls] == ["GET"]
            assert agent.queue()[0]["status"] == "uncertain"
            assert len(store.data["sport_request_times"]) == 60
        finally:
            await client.close()


async def test_confirmed_cancel_save_failure_keeps_protected_queue(tmp_path, monkeypatch):
    with StateStore(tmp_path / "state") as store:
        session(store)
        deleted = False
        def handler(request):
            nonlocal deleted
            if request.method == "DELETE":
                deleted = True
                return response([101])
            return response([{"lessons": [lesson()]}])
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        original = store.save
        def save():
            if deleted and store.data["queues"].get("owner") == []:
                raise StateError("failure")
            original()
        monkeypatch.setattr(store, "save", save)
        try:
            with pytest.raises(QueueError, match="не удалось сохранить"):
                await agent.cancel_booking(Booking(101, START, "Тест", "Зал", 1, 20))
            assert agent.queue()[0]["status"] == "uncertain"
        finally:
            await client.close()


async def test_changed_group_identity_rejects_before_protecting_or_deleting(tmp_path):
    calls = []
    with StateStore(tmp_path / "state") as store:
        session(store)
        def handler(request):
            calls.append(request.method)
            return response([{"lessons": [lesson(level=2, group=21)]}])
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        try:
            with pytest.raises(QueueError, match="изменилась"):
                await agent.cancel_booking(Booking(101, START, "Тест", "Зал", 2, 20))
            assert calls == ["GET"]
            assert agent.queue() == []
        finally:
            await client.close()


async def test_unknown_group_cancel_keeps_synthetic_guards(tmp_path):
    directory = tmp_path / "state"
    calls = []
    with StateStore(directory) as store:
        session(store)
        def handler(request):
            calls.append(request.method)
            if request.method == "DELETE":
                raise httpx.ReadTimeout("secret", request=request)
            return response([{"lessons": [lesson(level=2), lesson(102, level=2)]}])
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        try:
            with pytest.raises(SportError, match="Исход отмены неизвестен"):
                await agent.cancel_booking(Booking(101, START, "Тест", "Зал", 2, 20))
            assert [(item["lesson_id"], item["status"]) for item in agent.queue()] == [(101, "uncertain"), (102, "uncertain")]
            assert calls == ["GET", "DELETE"]
        finally:
            await client.close()
    with StateStore(directory) as store:
        assert [item["status"] for item in store.data["queues"]["owner"]] == ["uncertain", "uncertain"]


async def test_exit_before_delete_prevents_cancellation(tmp_path):
    reached, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            reached.set()
            await release.wait()
            return response([{"lessons": [lesson()]}])
        return response([101])
    with StateStore(tmp_path / "state") as store:
        session(store)
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, client, now=lambda: NOW)
        task = asyncio.create_task(agent.cancel_booking(Booking(101, START, "Тест", "Зал", 1, 20)))
        await reached.wait()
        agent.cancel_enabled = False
        release.set()
        with pytest.raises(QueueError, match="остановлена"):
            await task
        assert calls == ["GET"]
        assert agent.queue() == []
        await client.close()
