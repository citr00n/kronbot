import base64
import json
import os
import time
from datetime import date, datetime, timezone

import httpx
import pytest

from kronbot.agent import MSK, auto_delay
from kronbot.auth import Restricted
from kronbot.service import BackgroundRunner
from kronbot.sport import LocalLimit, NetworkBlocked, SportClient
from kronbot.state import StateError, StateStore, empty_state


NOW = 1900000000


def session():
    return {"sub": "owner", "access_token": "synthetic-secret", "access_expires_at": NOW + 10000,
            "refresh_token": None, "refresh_expires_at": None}


def test_legacy_state_migrates_force_setting(tmp_path):
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        assert store.data["force_limits"] is False
        store.save()
    legacy = empty_state()
    del legacy["force_limits"]
    raw = json.dumps(legacy).encode()
    if os.name == "nt":
        import win32crypt
        blob = win32crypt.CryptProtectData(raw, "kronbot state", None, None, None, 1)
        raw = json.dumps({"version": 1, "dpapi": base64.b64encode(blob).decode()}).encode()
    (directory / "state.json").write_bytes(raw)
    with StateStore(directory) as store:
        assert store.data["force_limits"] is False
        store.data["force_limits"] = True
        store.save()
    with StateStore(directory) as store:
        assert store.data["force_limits"] is True


@pytest.mark.parametrize("value", [0, 1, None, "true", [], {}])
def test_force_setting_requires_boolean(tmp_path, value):
    with StateStore(tmp_path / "state") as store:
        store.data["force_limits"] = value
        with pytest.raises(StateError):
            store.save()


async def test_forced_requests_remain_in_budget_after_disabling(tmp_path):
    clock = [NOW]
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"error_code": 0, "result": []})
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = session()
        store.data["force_limits"] = True
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: clock[0])
        for i in range(62):
            clock[0] = NOW + i
            await client.schedule(date(2030, 1, 1), date(2030, 1, 1))
        item = {"status": "waiting"}
        client.reserve(enrollment=item)
        assert item["status"] == "uncertain"
        assert store.data["last_enroll_at"] == NOW + 61
        store.data["force_limits"] = False
        store.save()
        await client.close()
    with StateStore(directory) as store:
        assert len(store.data["sport_request_times"]) == 63
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: clock[0])
        with pytest.raises(LocalLimit) as error:
            await client.schedule(date(2030, 1, 1), date(2030, 1, 1))
        assert error.value.until == NOW + 3 + 3600
        clock[0] = error.value.until
        await client.schedule(date(2030, 1, 1), date(2030, 1, 1))
        await client.close()
    assert len(calls) == 63


@pytest.mark.parametrize("status,delay", [(403, 86400), (429, 300)])
async def test_force_never_bypasses_server_blocks(tmp_path, status, delay):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "300"})
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = session()
        store.data["force_limits"] = True
        store.data["sport_request_times"] = [NOW] * 60
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        with pytest.raises(Restricted):
            await client.schedule(date(2030, 1, 1), date(2030, 1, 1))
        await client.close()
    with StateStore(directory) as store:
        assert store.data["blocked_until"]["my.itmo.ru"] == NOW + delay
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW + 1)
        with pytest.raises(NetworkBlocked) as error:
            await client.schedule(date(2030, 1, 1), date(2030, 1, 1))
        assert "synthetic-secret" not in str(error.value)
        await client.close()
    assert len(calls) == 1


async def test_forced_post_reserves_uncertain_before_transport(tmp_path):
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = session()
        store.data["force_limits"] = True
        store.data["sport_request_times"] = [NOW] * 60
        item = {"lesson_id": 1, "section_name": "Тест",
                "starts_at": datetime.fromtimestamp(NOW + 3600, timezone.utc).isoformat(),
                "status": "waiting", "refusal_code": None}
        store.data["queues"]["owner"] = [item]
        store.save()
        def handler(request):
            assert request.method == "POST"
            assert item["status"] == "uncertain"
            assert store.data["last_enroll_at"] == NOW
            assert len(store.data["sport_request_times"]) == 61
            raise httpx.ReadError("synthetic failure")
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: NOW)
        from kronbot.sport import SportError
        with pytest.raises(SportError):
            await client._request("POST", "sign/schedule/lessons", json=[1], enrollment=item)
        await client.close()
    with StateStore(tmp_path / "state") as store:
        assert store.data["queues"]["owner"][0]["status"] == "uncertain"
        assert store.data["last_enroll_at"] == NOW
        assert len(store.data["sport_request_times"]) == 61


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="tzset unavailable")
def test_limit_messages_use_local_zone_and_distinguish_blocks(monkeypatch):
    old = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "UTC-05:30")
        time.tzset()
        local = str(LocalLimit(NOW))
        remote = str(NetworkBlocked(NOW))
        assert "+05:30" in local and "+05:30" in remote
        assert "Локальное" in local and "Серверная" in remote
    finally:
        if old is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old)
        time.tzset()


def test_forced_delay_respects_minimum_without_midnight_reservation():
    midnight = datetime(2030, 1, 16, tzinfo=MSK).timestamp()
    assert auto_delay(midnight - 15, 10, forced=True) == 10
    assert auto_delay(midnight - 15, 1, forced=True) == 10
    assert auto_delay(midnight - 350, 300, forced=True) == 300
    assert auto_delay(midnight - 350, 300) == 350


async def test_service_force_skips_opening_wait_and_respects_limit(tmp_path, monkeypatch):
    midnight = datetime(2030, 1, 16, tzinfo=MSK).timestamp()
    wall = midnight - 30
    monotonic = [100.0]
    checks = []
    class FakeAgent:
        def __init__(self, store, client, **kwargs):
            self.booking_enabled = True
        async def check(self, *, allow_booking):
            checks.append(allow_booking)
            if len(checks) == 2:
                raise LocalLimit(int(wall + 300))
    class FakeClient:
        def __init__(self, store, **kwargs):
            self.pending_session = None
        async def close(self):
            pass
    monkeypatch.setattr("kronbot.service.Agent", FakeAgent)
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = session()
        store.data["force_limits"] = True
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Тест",
            "starts_at": datetime.fromtimestamp(midnight + 3600, timezone.utc).isoformat(),
            "status": "waiting", "refusal_code": None}]
        store.save()
    runner = BackgroundRunner(directory, now=lambda: wall, monotonic=lambda: monotonic[0], client_factory=FakeClient)
    await runner.cycle()
    assert checks == [True] and runner.next_check == 110
    monotonic[0] = 109
    await runner.cycle()
    assert len(checks) == 1
    monotonic[0] = 110
    await runner.cycle()
    assert len(checks) == 2 and runner.next_check == 410


async def test_service_force_change_releases_local_budget_wait(tmp_path):
    clock = {"wall": NOW, "mono": 100.0}
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"error_code": 0, "result": []})
    def factory(store, *, now):
        return SportClient(store, transport=httpx.MockTransport(handler), now=now)
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["session"] = session()
        store.data["sport_request_times"] = [NOW] * 60
        store.data["opening_window"] = {
            "day": datetime.fromtimestamp(NOW, MSK).date().isoformat(),
            "until": NOW - 1,
        }
        store.data["queues"]["owner"] = [{
            "lesson_id": 1, "section_name": "Тест",
            "starts_at": datetime.fromtimestamp(NOW + 86400, timezone.utc).isoformat(),
            "status": "waiting", "refusal_code": None,
        }]
        store.save()
    runner = BackgroundRunner(directory, now=lambda: clock["wall"],
                              monotonic=lambda: clock["mono"], client_factory=factory)
    await runner.cycle()
    assert not calls and runner.next_check == 3700
    with StateStore(directory) as store:
        store.data["force_limits"] = True
        store.save()
    clock.update(wall=NOW + 9, mono=109)
    await runner.cycle()
    assert not calls and runner.next_check == 110
    clock.update(wall=NOW + 10, mono=110)
    await runner.cycle()
    assert len(calls) == 1
    with StateStore(directory) as store:
        store.data["force_limits"] = False
        store.save()
    clock.update(wall=NOW + 130, mono=230)
    await runner.cycle()
    assert len(calls) == 1
    assert runner.next_check == 3700
