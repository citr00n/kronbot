from datetime import datetime, timezone

import httpx
import pytest

from kronbot.agent import MSK
from kronbot.service import BackgroundRunner
from kronbot.sport import SportClient
from kronbot.state import StateStore


MIDNIGHT = datetime(2030, 1, 16, tzinfo=MSK).timestamp()


class Clock:
    def __init__(self, wall):
        self.wall = wall
        self.mono = 1000.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def prepare(directory, clock, *, waiting=True, paused=False, force=False, expired=False, full=False):
    with StateStore(directory) as store:
        store.data["session"] = {
            "sub": "owner", "access_token": "access",
            "access_expires_at": int(clock.wall + 10000),
            "refresh_token": None, "refresh_expires_at": None,
        }
        if waiting:
            store.data["queues"]["owner"] = [{
                "lesson_id": 1, "section_name": "Секция",
                "starts_at": datetime.fromtimestamp(MIDNIGHT + 86400, timezone.utc).isoformat(),
                "status": "waiting", "refusal_code": None,
            }]
        store.data["background_paused"] = paused
        store.data["force_limits"] = force
        if expired:
            store.data["opening_window"] = {
                "day": datetime.fromtimestamp(clock.wall, MSK).date().isoformat(),
                "until": int(clock.wall - 1),
            }
        if full:
            store.data["sport_request_times"] = [int(clock.wall - 100)] * 60
        store.save()


def runner(directory, clock, handler):
    return BackgroundRunner(
        directory, now=lambda: clock.wall, monotonic=lambda: clock.mono,
        client_factory=lambda store, now: SportClient(
            store, now=now, transport=httpx.MockTransport(handler)),
    )


async def test_midnight_overrides_pending_local_limit_and_releases_lock(tmp_path):
    clock = Clock(MIDNIGHT - 30)
    directory = tmp_path / "state"
    prepare(directory, clock, expired=True, full=True)
    calls = []

    def handler(request):
        calls.append(clock.wall)
        return httpx.Response(200, json={"error_code": 0, "result": []})

    service = runner(directory, clock, handler)
    service.next_check = 1
    await service.cycle()
    assert not calls
    assert service.next_check > clock.mono + 30
    with StateStore(directory) as store:
        assert store.data["background_paused"] is False
    clock.advance(29)
    assert await service.cycle() == 1
    assert not calls
    clock.advance(1)
    await service.cycle()
    assert calls == [MIDNIGHT]
    with StateStore(directory) as store:
        assert store.data["opening_window"] == {"day": "2030-01-16", "until": int(MIDNIGHT + 300)}


async def test_late_opening_intervals_and_return_to_normal(tmp_path):
    clock = Clock(MIDNIGHT + 40)
    directory = tmp_path / "state"
    prepare(directory, clock)
    calls = []

    def handler(request):
        calls.append(clock.wall)
        return httpx.Response(200, json={"error_code": 0, "result": []})

    service = runner(directory, clock, handler)
    await service.cycle()
    clock.advance(9)
    await service.cycle()
    assert calls == [MIDNIGHT + 40]
    clock.advance(1)
    await service.cycle()
    assert calls == [MIDNIGHT + 40, MIDNIGHT + 50]
    clock.advance(289)
    await service.cycle()
    assert calls[-1] == MIDNIGHT + 339
    clock.advance(1)
    await service.cycle()
    assert len(calls) == 3
    clock.advance(119)
    await service.cycle()
    assert calls[-1] == MIDNIGHT + 459
    with StateStore(directory) as store:
        assert store.data["opening_window"]["until"] == MIDNIGHT + 340


async def test_restart_does_not_extend_opening_and_limit_returns(tmp_path):
    clock = Clock(MIDNIGHT + 10)
    directory = tmp_path / "state"
    prepare(directory, clock, full=True)
    calls = []

    def handler(request):
        calls.append(clock.wall)
        return httpx.Response(200, json={"error_code": 0, "result": []})

    await runner(directory, clock, handler).cycle()
    clock.advance(299)
    await runner(directory, clock, handler).cycle()
    assert len(calls) == 2
    clock.advance(1)
    await runner(directory, clock, handler).cycle()
    assert len(calls) == 2
    with StateStore(directory) as store:
        assert store.data["opening_window"] == {"day": "2030-01-16", "until": int(MIDNIGHT + 310)}
        assert len(store.data["sport_request_times"]) == 62


async def test_server_429_pauses_even_during_opening(tmp_path):
    clock = Clock(MIDNIGHT)
    directory = tmp_path / "state"
    prepare(directory, clock, full=True)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "300"})

    service = runner(directory, clock, handler)
    await service.cycle()
    clock.advance(301)
    await service.cycle()
    await runner(directory, clock, handler).cycle()
    assert len(calls) == 1
    with StateStore(directory) as store:
        assert store.data["background_paused"] is True
        assert store.data["blocked_until"]["my.itmo.ru"] == MIDNIGHT + 300


@pytest.mark.parametrize("waiting,paused", [(False, False), (True, True)])
async def test_no_queue_or_paused_never_opens_window_or_requests(tmp_path, waiting, paused):
    clock = Clock(MIDNIGHT + 30)
    directory = tmp_path / "state"
    prepare(directory, clock, waiting=waiting, paused=paused)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"error_code": 0, "result": []})

    await runner(directory, clock, handler).cycle()
    assert not calls
    with StateStore(directory) as store:
        assert store.data["opening_window"] is None


async def test_forced_mode_survives_opening_expiry(tmp_path):
    clock = Clock(MIDNIGHT)
    directory = tmp_path / "state"
    prepare(directory, clock, force=True)
    calls = []

    def handler(request):
        calls.append(clock.wall)
        return httpx.Response(200, json={"error_code": 0, "result": []})

    service = runner(directory, clock, handler)
    await service.cycle()
    clock.advance(299)
    await service.cycle()
    clock.advance(1)
    await service.cycle()
    assert len(calls) == 2
    clock.advance(9)
    await service.cycle()
    assert calls[-1] == MIDNIGHT + 309


async def test_existing_server_block_pauses_without_consuming_window(tmp_path):
    clock = Clock(MIDNIGHT + 20)
    directory = tmp_path / "state"
    prepare(directory, clock)
    with StateStore(directory) as store:
        store.data["blocked_until"]["id.itmo.ru"] = int(clock.wall + 120)
        store.save()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"error_code": 0, "result": []})

    await runner(directory, clock, handler).cycle()
    with StateStore(directory) as store:
        assert store.data["background_paused"] is True
        assert store.data["opening_window"] is None
    assert not calls


async def test_waking_after_midnight_starts_new_window_without_monotonic_advance(tmp_path):
    clock = Clock(MIDNIGHT - 180)
    directory = tmp_path / "state"
    prepare(directory, clock, expired=True)
    calls = []

    def handler(request):
        calls.append(clock.wall)
        return httpx.Response(200, json={"error_code": 0, "result": []})

    service = runner(directory, clock, handler)
    service.next_check = 1
    await service.cycle()
    clock.wall = MIDNIGHT + 40
    await service.cycle()
    assert calls == [MIDNIGHT - 180, MIDNIGHT + 40]
    with StateStore(directory) as store:
        assert store.data["opening_window"]["until"] == MIDNIGHT + 340
