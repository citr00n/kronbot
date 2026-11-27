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
        assert agent.queue() == [] and agent.confirmed_ids == {500001} and len(posts) == 1
        await agent.check(allow_booking=False)
        assert agent.confirmed_ids == set()
        assert agent.queue() == []
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


@pytest.mark.parametrize("confirmation", ["schedule", "calendar", "post"])
async def test_confirmed_entries_removed_and_range_includes_opening_day(tmp_path, confirmation):
    now = int(datetime(2030, 1, 15, 23, 59, tzinfo=timezone(timedelta(hours=3))).timestamp())
    start = datetime.fromtimestamp(now + 14 * 86400, timezone.utc).isoformat()
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"error_code": 0, "result": [1]})
        if request.url.path.endswith("/sign/schedule"):
            row = {"id": 1, "date": start, "section_level": 1, "type_id": 2,
                   "available": 1, "signed": confirmation == "schedule",
                   "can_sign_in": {"can_sign_in": True}}
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [row]}]})
        if request.url.path.endswith("/personal/calendar"):
            lessons = [{"id": 1}] if confirmation == "calendar" else []
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": lessons}]})
        return httpx.Response(200, json={"error_code": 0,
                                         "result": {"free_attempts": 1, "can_sign_in": True}})

    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = {"sub": "owner", "access_token": "access",
                                 "access_expires_at": now + 10000,
                                 "refresh_token": None, "refresh_expires_at": None}
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Секция",
                                          "starts_at": start, "status": "waiting",
                                          "refusal_code": None}]
        store.save()
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        await agent.check(allow_booking=True)
        assert agent.queue() == []
        assert agent.confirmed_ids == {1}
        schedule = next(request for request in calls if request.url.path.endswith("/sign/schedule"))
        assert schedule.url.params["date_start"] == "2030-01-15"
        assert schedule.url.params["date_end"] == "2030-01-29"
        assert sum(request.method == "POST" for request in calls) == (confirmation == "post")
        await sport.close()
    with StateStore(directory) as store:
        assert store.data["queues"]["owner"] == []


async def test_confirmation_save_failure_restores_uncertain_entry(tmp_path, monkeypatch):
    from kronbot.state import StateError

    now = 1900000000
    start = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()

    def handler(request):
        if request.url.path.endswith("/sign/schedule"):
            row = {"id": 1, "date": start, "section_level": 1, "type_id": 2,
                   "available": 1, "signed": True, "can_sign_in": {"can_sign_in": True}}
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [row]}]})
        raise AssertionError("unexpected request")

    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access",
                                 "access_expires_at": now + 10000,
                                 "refresh_token": None, "refresh_expires_at": None}
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Секция",
                                          "starts_at": start, "status": "uncertain",
                                          "refusal_code": None}]
        store.save()
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        real_save = store.save

        def fail_removal():
            if not store.data["queues"]["owner"]:
                raise StateError("synthetic")
            real_save()

        monkeypatch.setattr(store, "save", fail_removal)
        with pytest.raises(StateError):
            await agent.check(allow_booking=False)
        assert agent.queue()[0]["status"] == "uncertain"
        assert agent.confirmed_ids == set()
        await sport.close()


def _forecast_row(ident, starts):
    return {
        "id": ident, "date": starts.isoformat(),
        "date_end": (starts + timedelta(minutes=90)).isoformat(),
        "section_id": 31, "section_name": "Тестовая секция", "section_level": 1,
        "lesson_level": 1, "type_id": 2, "building_id": 13, "room_id": 1001,
        "room_name": "Спортзал 1", "time_slot_id": 2, "teacher_isu": 200002,
        "teacher_fio": "Тренер Тестовый", "available": 1, "signed": False,
        "can_sign_in": {"can_sign_in": True},
    }


def _session(now):
    return {"sub": "owner", "access_token": "access", "access_expires_at": now + 100000,
            "refresh_token": None, "refresh_expires_at": None}


async def test_forecast_survives_restart_binds_and_books(tmp_path):
    from kronbot.sport import _lesson
    now = 1900000000
    prototype = datetime.fromtimestamp(now - 86400, timezone.utc)
    rows = [_forecast_row(10, prototype)]
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": [20]})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": rows}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0,
            "result": {"free_attempts": 1, "can_sign_in": True}})

    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = _session(now)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        agent.add_forecast(_lesson(rows[0]))
        await agent.check(allow_booking=True)
        assert agent.queue()[0]["lesson_id"] == 10
        assert agent.queue()[0]["status"] == "waiting"
        assert not posts
        await sport.close()
    rows[:] = [_forecast_row(20, prototype + timedelta(days=14))]
    rows[0].update(teacher_isu=300003, room_id=1002, time_slot_id=3)
    with StateStore(directory) as store:
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        await agent.check(allow_booking=False)
        assert agent.queue()[0]["lesson_id"] == 20
        assert agent.queue()[0]["forecast"]["bound"]
        await sport.close()
    with StateStore(directory) as store:
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        await agent.check(allow_booking=True)
        assert posts == [b"[20]"]
        assert agent.queue() == []
        await sport.close()


@pytest.mark.parametrize("key,value", [
    ("section_id", 32), ("section_level", 2), ("type_id", 5),
    ("date", "2030-01-01T10:01:00+00:00"),
])
def test_forecast_rejects_each_mismatched_field(tmp_path, key, value):
    from kronbot.sport import _lesson
    now = 1893456000
    prototype = datetime(2029, 12, 18, 10, tzinfo=timezone.utc)
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        agent = Agent(store, None, now=lambda: now)
        agent.add_forecast(_lesson(_forecast_row(10, prototype)))
        real = _forecast_row(20, prototype + timedelta(days=14))
        real[key] = value
        candidate = _lesson(real)
        assert candidate is None or not agent._matches_forecast(agent.queue()[0], candidate)


@pytest.mark.parametrize("change", [
    {"section_id": None}, {"type_id": 5}, {"type_id": 7},
])
def test_forecast_refuses_unprovable_prototypes(tmp_path, change):
    from kronbot.agent import QueueError
    from kronbot.sport import _lesson
    now = 1900000000
    row = _forecast_row(10, datetime.fromtimestamp(now, timezone.utc))
    row.update(change)
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        agent = Agent(store, None, now=lambda: now)
        lesson = _lesson(row)
        if lesson is not None:
            with pytest.raises(QueueError):
                agent.add_forecast(lesson)
        assert agent.queue() == []


def test_forecast_ambiguous_online_and_existing_uncertain(tmp_path):
    from kronbot.sport import _lesson
    now = 1900000000
    prototype = datetime.fromtimestamp(now, timezone.utc)
    row = _forecast_row(10, prototype)
    row.update(building_id=None, room_id=-1)
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        agent = Agent(store, None, now=lambda: now)
        agent.add_forecast(_lesson(row))
        real = _forecast_row(20, prototype + timedelta(days=14))
        real.update(building_id=-1, room_id=-1)
        candidate = _lesson(real)
        duplicate = _lesson({**real, "id": 21})
        agent._bind_forecasts(agent.queue(), [candidate, duplicate])
        assert agent.queue()[0]["lesson_id"] == 10
        agent.queue().append({"lesson_id": 20, "section_name": "Секция",
                              "starts_at": real["date"], "status": "uncertain",
                              "refusal_code": None})
        store.save()
        agent._bind_forecasts(agent.queue(), [candidate])
        assert len(agent.queue()) == 2
        assert agent.queue()[1]["status"] == "uncertain"
        assert not agent.queue()[0]["forecast"]["bound"]
        assert agent.forecast_diagnostics()[0]["status"] == "ambiguous"


@pytest.mark.parametrize("status", ["rejected", "expired"])
def test_forecast_retained_when_matching_terminal_entry(tmp_path, status):
    from kronbot.sport import _lesson
    now = 1900000000
    prototype = datetime.fromtimestamp(now, timezone.utc)
    real = _forecast_row(20, prototype + timedelta(days=14))
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = _session(now)
        agent = Agent(store, None, now=lambda: now)
        agent.add_forecast(_lesson(_forecast_row(10, prototype)))
        agent.queue().append({
            "lesson_id": 20, "section_name": "Секция", "starts_at": real["date"],
            "status": status, "refusal_code": 137 if status == "rejected" else None,
        })
        store.save()
        agent._bind_forecasts(agent.queue(), [_lesson(real)])
        assert len(agent.queue()) == 2
        assert agent.queue()[0]["lesson_id"] == 10
        assert agent.queue()[0]["forecast"]["bound"] is False
        assert agent.queue()[1]["status"] == status
    with StateStore(directory) as store:
        agent = Agent(store, None, now=lambda: now)
        assert len(agent.queue()) == 2
        agent.queue().pop()
        store.save()
        agent._bind_forecasts(agent.queue(), [_lesson(real)])
        assert agent.queue()[0]["lesson_id"] == 20
        assert agent.queue()[0]["forecast"]["bound"] is True


async def test_multiple_candidates_post_without_interval_and_preserve_refusal(tmp_path):
    from kronbot.sport import _lesson
    now = 1900000000
    starts = datetime.fromtimestamp(now + 86400, timezone.utc)
    rows = [_forecast_row(ident, starts) for ident in (1, 2, 3)]
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            if request.content == b"[1]":
                return httpx.Response(200, json={"error_code": 137, "result": None})
            ident = int(request.content[1:-1])
            return httpx.Response(200, json={"error_code": 0, "result": [ident]})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": rows}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0,
            "result": {"free_attempts": 1, "can_sign_in": True}})

    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        for row in rows:
            agent.add(_lesson(row))
        await agent.check(allow_booking=True)
        assert posts == [b"[1]", b"[2]", b"[3]"]
        assert [(x["lesson_id"], x["status"], x["refusal_code"]) for x in agent.queue()] == [(1, "rejected", 137)]
        assert len(store.data["sport_request_times"]) == 8
        await sport.close()


def test_forecast_binding_save_failure_restores_queue(tmp_path, monkeypatch):
    from kronbot.sport import _lesson
    from kronbot.state import StateError
    now = 1900000000
    prototype = datetime.fromtimestamp(now, timezone.utc)
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        agent = Agent(store, None, now=lambda: now)
        agent.add_forecast(_lesson(_forecast_row(10, prototype)))
        real = _lesson(_forecast_row(20, prototype + timedelta(days=14)))
        def fail():
            raise StateError("synthetic")
        monkeypatch.setattr(store, "save", fail)
        with pytest.raises(StateError):
            agent._bind_forecasts(agent.queue(), [real])
        assert agent.queue()[0]["lesson_id"] == 10
        assert agent.queue()[0]["forecast"]["bound"] is False


async def test_booking_prototype_does_not_consume_its_forecast(tmp_path):
    from kronbot.agent import QueueError
    from kronbot.sport import _lesson
    now = 1900000000
    start = datetime.fromtimestamp(now + 86400, timezone.utc)
    row = _forecast_row(10, start)
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": [10]})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [row]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0,
            "result": {"free_attempts": 1, "can_sign_in": True}})

    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = _session(now)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        lesson = _lesson(row)
        agent.add_forecast(lesson)
        agent.add(lesson)
        with pytest.raises(QueueError):
            agent.remove(10)
        await agent.check(allow_booking=True, only_lesson_id=10)
        assert posts == [b"[10]"]
        assert len(agent.queue()) == 1
        assert agent.queue()[0]["forecast"]["bound"] is False
        await sport.close()
    with StateStore(directory) as store:
        assert store.data["queues"]["owner"][0]["lesson_id"] == 10
        assert store.data["queues"]["owner"][0]["forecast"]["bound"] is False


@pytest.mark.parametrize("response", [[], [1, 2], [1, 1], [True], None])
async def test_ambiguous_post_result_stays_uncertain(tmp_path, response):
    from kronbot.sport import _lesson
    now = 1900000000
    row = _forecast_row(1, datetime.fromtimestamp(now + 86400, timezone.utc))
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"error_code": 0, "result": response})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [row]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0,
            "result": {"free_attempts": 1, "can_sign_in": True}})
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = _session(now)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        agent.add(_lesson(row))
        await agent.check(allow_booking=True)
        assert agent.queue()[0]["status"] == "uncertain"
        await sport.close()
    with StateStore(directory) as store:
        assert store.data["queues"]["owner"][0]["status"] == "uncertain"


async def test_incomplete_or_duplicate_snapshot_blocks_forecast_matching(tmp_path):
    from kronbot.sport import _lesson
    now = 1900000000
    start = datetime.fromtimestamp(now, timezone.utc)
    real = _forecast_row(20, start + timedelta(days=14))
    other = {**real, "id": 21}
    rows = [real, other, dict(other)]
    def handler(request):
        return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": rows}]})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        agent.add_forecast(_lesson(_forecast_row(10, start)))
        await agent.check(allow_booking=False)
        assert agent.queue()[0]["forecast"]["bound"] is False
        rows[:] = [real, {**other, "teacher_isu": None}]
        await agent.check(allow_booking=False)
        assert agent.forecast_diagnostics()[0]["status"] == "ambiguous"
        assert agent.queue()[0]["forecast"]["bound"] is False
        await sport.close()


async def test_attempts_are_rechecked_after_each_success(tmp_path):
    from kronbot.sport import _lesson
    now = 1900000000
    starts = datetime.fromtimestamp(now + 86400, timezone.utc)
    rows = [_forecast_row(ident, starts) for ident in (1, 2)]
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": [1]})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": rows}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0,
            "result": {"free_attempts": 0 if posts else 1, "can_sign_in": True}})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        for row in rows:
            agent.add(_lesson(row))
        await agent.check(allow_booking=True)
        assert posts == [b"[1]"]
        assert [(x["lesson_id"], x["status"]) for x in agent.queue()] == [(2, "waiting")]
        await sport.close()


async def test_failed_result_save_keeps_uncertain_in_memory_and_on_disk(tmp_path, monkeypatch):
    from kronbot.sport import _lesson
    from kronbot.state import StateError
    now = 1900000000
    row = _forecast_row(1, datetime.fromtimestamp(now + 86400, timezone.utc))
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"error_code": 135, "result": None})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [row]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0,
            "result": {"free_attempts": 1, "can_sign_in": True}})
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = _session(now)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        agent.add(_lesson(row))
        save = store.save
        reserved = False
        def fail_after_post():
            nonlocal reserved
            item = store.data["queues"]["owner"][0]
            if item["status"] == "uncertain":
                reserved = True
            elif reserved:
                raise StateError("synthetic")
            save()
        monkeypatch.setattr(store, "save", fail_after_post)
        with pytest.raises(StateError):
            await agent.check(allow_booking=True)
        assert agent.queue()[0]["status"] == "uncertain"
        await sport.close()
    with StateStore(directory) as store:
        assert store.data["queues"]["owner"][0]["status"] == "uncertain"


async def test_cancel_booking_preserves_same_id_forecast(tmp_path):
    from kronbot.sport import _lesson
    now = 1900000000
    row = _forecast_row(10, datetime.fromtimestamp(now + 86400, timezone.utc))
    def handler(request):
        if request.method == "DELETE":
            return httpx.Response(200, json={"error_code": 0, "result": [10]})
        return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [row]}]})
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = _session(now)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        agent.add_forecast(_lesson(row))
        agent.add(_lesson(row))
        booking = (await agent.bookings())[0]
        await agent.cancel_booking(booking)
        assert len(agent.queue()) == 1
        assert agent.queue()[0]["forecast"]["bound"] is False
        await sport.close()
    with StateStore(directory) as store:
        assert len(store.data["queues"]["owner"]) == 1
        assert store.data["queues"]["owner"][0]["forecast"]["bound"] is False


async def test_forecast_changed_metadata_books_once_and_persists_report(tmp_path, monkeypatch):
    from kronbot import notifications
    from kronbot.sport import _lesson

    now = 1900000000
    start = datetime.fromtimestamp(now - 86400, timezone.utc)
    prototype = _forecast_row(124906, start)
    real = _forecast_row(125007, start + timedelta(days=14))
    real.update(type_id=6, teacher_isu=300003, teacher_fio="Другой тренер",
                room_id=1002, room_name="Другой зал", time_slot_id=7)
    externship = {**real, "id": 135299, "type_id": 7,
                  "can_sign_in": {"can_sign_in": False}}
    posts, messages, actions = [], [], []

    async def send(store, message):
        actions.append("notification")
        messages.append(message)

    monkeypatch.setattr(notifications, "send", send)

    def handler(request):
        if request.method == "POST":
            actions.append("post")
            posts.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": [125007]})
        if request.url.path.endswith("/sign/schedule"):
            result = [{"lessons": [real, externship]}]
        elif request.url.path.endswith("/personal/calendar"):
            result = []
        else:
            result = {"free_attempts": 1, "can_sign_in": True}
        return httpx.Response(200, json={"error_code": 0, "result": result})

    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = _session(now)
        agent = Agent(store, None, now=lambda: now)
        agent.add_forecast(_lesson(prototype))
        assert agent.forecast_diagnostics()[0]["status"] == "not_checked"
    with StateStore(directory) as store:
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        await agent.check(allow_booking=True)
        assert posts == [b"[125007]"]
        assert actions.index("post") < actions.index("notification")
        differences = [message for message in messages if "изменились" in message]
        assert len(differences) == 1
        assert all(field in differences[0] for field in ("тип занятия (2 → 6)", "преподаватель", "зал", "временной слот"))
        assert agent.queue() == []
        await sport.close()
    with StateStore(directory) as store:
        agent = Agent(store, None, now=lambda: now)
        report = agent.forecast_diagnostics()[0]
        assert report["status"] == "matched" and report["real_id"] == 125007
        assert "временной слот" in report["differences"]
        assert "тип занятия (2 → 6)" in report["differences"]
        store.data["session"] = {**_session(now), "sub": "other"}
        assert agent.forecast_diagnostics() == []


async def test_forecast_ambiguity_and_damaged_rows_are_scoped(tmp_path):
    from kronbot.sport import _lesson
    now = 1900000000
    start = datetime.fromtimestamp(now - 86400, timezone.utc)
    real = _forecast_row(20, start + timedelta(days=14))
    rows = [real, {**real, "id": 21, "type_id": 6, "teacher_isu": 4}]
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": [20]})
        if request.url.path.endswith("/sign/schedule"):
            result = [{"lessons": rows}]
        elif request.url.path.endswith("/personal/calendar"):
            result = []
        else:
            result = {"free_attempts": 1, "can_sign_in": True}
        return httpx.Response(200, json={"error_code": 0, "result": result})

    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        agent.add_forecast(_lesson(_forecast_row(10, start)))
        await agent.check(allow_booking=True)
        assert not posts and not agent.queue()[0]["forecast"]["bound"]
        report = agent.forecast_diagnostics()[0]
        assert report["status"] == "ambiguous" and report["differences"] == []
        rows[:] = [real, {"section_id": 31, "date": real["date"]}]
        await agent.check(allow_booking=True)
        assert not posts and agent.forecast_diagnostics()[0]["status"] == "incomplete"
        rows[:] = [real, {"section_id": 99, "date": real["date"]}]
        await agent.check(allow_booking=True)
        assert posts == [b"[20]"]
        await sport.close()


async def test_forecast_standards_waits_when_server_denies_signin(tmp_path):
    from kronbot.sport import _lesson
    now = 1900000000
    start = datetime.fromtimestamp(now - 86400, timezone.utc)
    real = _forecast_row(125109, start + timedelta(days=14))
    real.update(type_id=6, available=13, can_sign_in={"can_sign_in": False})
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
        return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [real]}]})

    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        agent.add_forecast(_lesson(_forecast_row(124917, start)))
        await agent.check(allow_booking=True)
        assert not posts
        assert agent.queue()[0]["lesson_id"] == 125109
        assert agent.queue()[0]["status"] == "waiting"
        assert agent.forecast_diagnostics()[0]["differences"] == ["тип занятия (2 → 6)"]
        await sport.close()


def test_forecast_accepts_missing_optional_metadata(tmp_path):
    from kronbot.sport import _lesson, forecast_prototypes
    now = 1900000000
    row = _forecast_row(10, datetime.fromtimestamp(now, timezone.utc))
    for key in ("teacher_isu", "teacher_fio", "building_id", "room_id",
                "room_name", "time_slot_id", "lesson_level", "date_end"):
        row.pop(key)
    lesson = _lesson(row)
    assert forecast_prototypes([lesson], now) == [lesson]
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        Agent(store, None, now=lambda: now).add_forecast(lesson)
    with StateStore(tmp_path / "state") as store:
        assert store.data["queues"]["owner"][0]["forecast"]["prototype_ends_at"] is None


def test_opening_window_survives_restart_and_respects_pause(tmp_path):
    from kronbot.agent import opening_window
    now = 1900000000
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = _session(now)
        store.data["queues"]["owner"] = [{
            "lesson_id": 1, "section_name": "Секция",
            "starts_at": datetime.fromtimestamp(now + 86400, timezone.utc).isoformat(),
            "status": "waiting", "refusal_code": None,
        }]
        store.data["background_paused"] = True
        assert not opening_window(store, now)
        assert store.data["opening_window"] is None
        store.data["background_paused"] = False
        assert opening_window(store, now)
        assert store.data["opening_window"]["until"] == now + 300
    with StateStore(directory) as store:
        assert opening_window(store, now + 100)
        assert not opening_window(store, now - 10)
        assert not opening_window(store, now - 86400)
        assert opening_window(store, now)
        assert not opening_window(store, now + 300)
        assert not opening_window(store, now + 301)
        assert store.data["opening_window"]["until"] == now + 300


def test_opening_window_save_failure_rolls_back(tmp_path, monkeypatch):
    from kronbot.agent import opening_window
    from kronbot.state import StateError
    now = 1900000000
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        store.data["queues"]["owner"] = [{
            "lesson_id": 1, "section_name": "Секция",
            "starts_at": datetime.fromtimestamp(now + 86400, timezone.utc).isoformat(),
            "status": "waiting", "refusal_code": None,
        }]
        def fail():
            raise StateError("synthetic")
        monkeypatch.setattr(store, "save", fail)
        with pytest.raises(StateError):
            opening_window(store, now)
        assert store.data["opening_window"] is None


async def test_opening_window_ignores_local_limit_but_not_server_block(tmp_path):
    from kronbot.sport import LocalLimit, NetworkBlocked
    now = 1900000000
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = _session(now)
        store.data["sport_request_times"] = [now - 1] * 60
        sport = SportClient(store, now=lambda: now)
        with pytest.raises(LocalLimit):
            sport.reserve()
        sport.opening_window_active = True
        sport.reserve()
        assert len(store.data["sport_request_times"]) == 61
        store.data["blocked_until"]["my.itmo.ru"] = now + 120
        with pytest.raises(NetworkBlocked):
            sport.reserve()
        assert len(store.data["sport_request_times"]) == 61
        await sport.close()


async def test_bound_forecast_with_changed_metadata_calendar_confirmation(tmp_path):
    from kronbot.sport import _lesson
    now = 1900000000
    start = datetime.fromtimestamp(now - 86400, timezone.utc)
    real = _forecast_row(20, start + timedelta(days=14))
    real.update(teacher_isu=300003, room_id=1002, time_slot_id=3)
    posts = []
    calendar = []

    def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": [20]})
        if request.url.path.endswith("/sign/schedule"):
            result = [{"lessons": [real]}]
        elif request.url.path.endswith("/personal/calendar"):
            result = [{"lessons": calendar}]
        else:
            result = {"free_attempts": 1, "can_sign_in": True}
        return httpx.Response(200, json={"error_code": 0, "result": result})

    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = _session(now)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        agent.add_forecast(_lesson(_forecast_row(10, start)))
        await agent.check(allow_booking=False)
        assert agent.queue()[0]["forecast"]["bound"]
        agent.queue()[0]["status"] = "uncertain"
        store.save()
        await sport.close()
    calendar.append({"id": 20, "signed": False})
    with StateStore(directory) as store:
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        agent = Agent(store, sport, now=lambda: now)
        await agent.check(allow_booking=True)
        assert agent.queue() == [] and not posts
        assert agent.forecast_diagnostics()[0]["real_id"] == 20
        await sport.close()
