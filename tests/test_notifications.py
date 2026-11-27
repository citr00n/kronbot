from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kronbot import notifications
from kronbot.agent import Agent
from kronbot.sport import SportClient, SportError, _lesson
from kronbot.state import StateStore


NOW = 1900000000
TOPIC = "https://ntfy.example/private-topic"
TOKEN = "private-token"


@pytest.mark.parametrize("url", ["http://ntfy.example/topic", "https://user:pass@ntfy.example/topic",
                                "https://ntfy.example/", "https://ntfy.example/a?x=1",
                                "https://ntfy.example/a#fragment", "https://ntfy.example:bad/a",
                                "https://ntfy.example/a\n", "https://ntfy.example\\evil/a"])
def test_invalid_urls_are_safe(url):
    with pytest.raises(ValueError) as exc:
        notifications.validate_url(url)
    assert url not in str(exc.value)


def session():
    return {"sub": "owner", "access_token": "access-secret", "access_expires_at": NOW + 10000000,
            "refresh_token": None, "refresh_expires_at": None}


def row(ident, start, *, signed=False):
    return {"id": ident, "date": start.isoformat(),
            "date_end": (start + timedelta(minutes=90)).isoformat(),
            "section_id": 31, "section_name": "Personal section", "section_level": 1,
            "lesson_level": 1, "type_id": 2, "building_id": 13, "room_id": 1001,
            "room_name": "Room", "time_slot_id": 2, "teacher_isu": 200002,
            "teacher_fio": "Personal name", "available": 1, "signed": signed,
            "can_sign_in": {"can_sign_in": True}}


def configure(store):
    store.data["session"] = session()
    store.data["ntfy"] = {"url": TOPIC, "token": TOKEN, "last_error": None}
    store.save()


def intercept(monkeypatch, requests, *, fail=False):
    original = notifications.send

    def handler(request):
        requests.append(request)
        if fail:
            raise httpx.ConnectError("secret " + TOPIC + TOKEN, request=request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)

    async def send(store, message, **kwargs):
        await original(store, message, transport=transport)

    monkeypatch.setattr(notifications, "send", send)


@pytest.mark.parametrize("confirmation", ["schedule", "calendar", "post"])
async def test_confirmation_after_save_and_booking(tmp_path, monkeypatch, confirmation):
    sent, actions = [], []
    intercept(monkeypatch, sent)
    start = datetime.fromtimestamp(NOW + 86400, timezone.utc)

    def handler(request):
        actions.append(request.method + " " + request.url.path)
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [row(1, start, signed=confirmation == "schedule")]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 1}] if confirmation == "calendar" else []}]})
        if request.method == "POST":
            return httpx.Response(200, json={"error_code": 0, "result": [1]})
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})

    with StateStore(tmp_path / "state") as store:
        configure(store)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, sport, now=lambda: NOW)
        agent.add(_lesson(row(1, start)))
        await agent.check(allow_booking=True)
        assert store.data["queues"]["owner"] == []
        assert len(sent) == 1 and "подтверждена" in sent[0].content.decode()
        assert sent[0].headers["authorization"] == "Bearer " + TOKEN
        assert (any(action.startswith("POST ") for action in actions)) == (confirmation == "post")
        await agent.check(allow_booking=False)
        assert len(sent) == 1
        await sport.close()


async def test_failure_deduplicated_across_restart_and_reset(tmp_path, monkeypatch, capsys):
    sent = []
    intercept(monkeypatch, sent)
    failing = True

    def handler(request):
        if failing:
            raise httpx.ReadError("private " + TOPIC + TOKEN, request=request)
        return httpx.Response(200, json={"error_code": 0, "result": []})

    directory = tmp_path / "state"
    for step in range(4):
        if step == 2:
            failing = False
        if step == 3:
            failing = True
        with StateStore(directory) as store:
            if step == 0:
                configure(store)
            sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
            agent = Agent(store, sport, now=lambda: NOW)
            if failing:
                with pytest.raises(SportError) as exc:
                    await agent.check(allow_booking=False)
                assert TOPIC not in str(exc.value) and TOKEN not in str(exc.value)
            else:
                await agent.check(allow_booking=False)
                assert store.data["ntfy"]["last_error"] is None
            await sport.close()
    assert len(sent) == 2
    assert all(TOKEN.encode() not in request.content and TOPIC.encode() not in request.content
               and b"access-secret" not in request.content for request in sent)
    captured = capsys.readouterr()
    assert TOPIC not in captured.out + captured.err and TOKEN not in captured.out + captured.err


async def test_unmatched_forecast_only_after_start(tmp_path, monkeypatch):
    sent = []
    intercept(monkeypatch, sent)
    current = [NOW]
    prototype = datetime.fromtimestamp(NOW - 86400, timezone.utc)
    def handler(request):
        return httpx.Response(200, json={"error_code": 0, "result": []})
    with StateStore(tmp_path / "state") as store:
        configure(store)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: current[0])
        agent = Agent(store, sport, now=lambda: current[0])
        agent.add_forecast(_lesson(row(1, prototype)))
        await agent.check(allow_booking=False)
        assert not sent
        current[0] = (prototype + timedelta(days=14, seconds=1)).timestamp()
        await agent.check(allow_booking=False)
        assert len(sent) == 1 and "прогноз" in sent[0].content.decode()
        await agent.check(allow_booking=False)
        assert len(sent) == 1
        await sport.close()


async def test_ntfy_failure_does_not_change_booking(tmp_path, monkeypatch, capsys):
    sent = []
    intercept(monkeypatch, sent, fail=True)
    start = datetime.fromtimestamp(NOW + 86400, timezone.utc)
    def handler(request):
        return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [row(1, start, signed=True)]}]})
    with StateStore(tmp_path / "state") as store:
        configure(store)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, sport, now=lambda: NOW)
        agent.add(_lesson(row(1, start)))
        await agent.check(allow_booking=False)
        assert agent.confirmed_ids == {1} and store.data["queues"]["owner"] == []
        await sport.close()
    captured = capsys.readouterr()
    assert TOPIC not in captured.out + captured.err and TOKEN not in captured.out + captured.err


@pytest.mark.parametrize("result", ["uncertain", "rejected"])
async def test_post_errors_are_notified_after_state_save(tmp_path, monkeypatch, result):
    sent = []
    intercept(monkeypatch, sent)
    start = datetime.fromtimestamp(NOW + 86400, timezone.utc)

    def handler(request):
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [row(1, start)]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        if request.method == "POST":
            if result == "uncertain":
                return httpx.Response(200, json={"error_code": 0, "result": []})
            return httpx.Response(200, json={"error_code": 137, "error_message": "personal secret"})
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})

    directory = tmp_path / "state"
    with StateStore(directory) as store:
        configure(store)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, sport, now=lambda: NOW)
        agent.add(_lesson(row(1, start)))
        await agent.check(allow_booking=True)
        assert agent.queue()[0]["status"] == result
        assert len(sent) == 1
        assert b"personal secret" not in sent[0].content
        await sport.close()
    with StateStore(directory) as store:
        assert store.data["queues"]["owner"][0]["status"] == result
        assert store.data["ntfy"]["last_error"] == result


async def test_background_runner_deduplicates_errors_across_new_agents(tmp_path, monkeypatch):
    from kronbot.service import BackgroundRunner

    sent = []
    intercept(monkeypatch, sent)
    directory = tmp_path / "state"
    start = datetime.fromtimestamp(NOW + 86400, timezone.utc)
    with StateStore(directory) as store:
        configure(store)
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Section",
                                           "starts_at": start.isoformat(), "status": "waiting",
                                           "refusal_code": None}]
        store.save()

    def handler(request):
        raise httpx.ConnectError("secret", request=request)

    def factory(store, *, now):
        return SportClient(store, transport=httpx.MockTransport(handler), now=now)

    for _ in range(2):
        runner = BackgroundRunner(directory, now=lambda: NOW, monotonic=lambda: 1000,
                                  client_factory=factory)
        await runner.cycle()
    assert len(sent) == 1


async def test_all_posts_precede_notifications(tmp_path, monkeypatch):
    start = datetime.fromtimestamp(NOW + 86400, timezone.utc)
    actions = []
    original = notifications.send

    async def send(store, message, **kwargs):
        def handler(request):
            actions.append("notify")
            return httpx.Response(200)
        await original(store, message, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(notifications, "send", send)

    def handler(request):
        if request.url.path.endswith("/sign/schedule"):
            lessons = [row(1, start, signed=True), row(2, start)]
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": lessons}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        if request.method == "POST":
            actions.append("post")
            return httpx.Response(200, json={"error_code": 0, "result": [2]})
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})

    with StateStore(tmp_path / "state") as store:
        configure(store)
        sport = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        agent = Agent(store, sport, now=lambda: NOW)
        agent.add(_lesson(row(1, start)))
        agent.add(_lesson(row(2, start)))
        await agent.check(allow_booking=True)
        assert actions == ["post", "notify", "notify"]
        await sport.close()
