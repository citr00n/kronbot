import json
import os
import stat
import subprocess
import sys

import pytest

from kronbot.state import StateError, StateStore


def test_state_roundtrip(tmp_path):
    directory = tmp_path / "кириллица test"
    with StateStore(directory) as store:
        store.data["session"] = {"sub": "owner", "access_token": "synthetic-access", "access_expires_at": 100, "refresh_token": "synthetic-refresh", "refresh_expires_at": 200, "isu": "123456"}
        store.save()
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Тест", "starts_at": "2030-01-01T12:00:00+03:00", "status": "waiting", "refusal_code": None}]
        store.save()
    raw = (directory / "state.json").read_bytes()
    if os.name == "nt":
        assert b"synthetic-access" not in raw and b"dpapi" in raw
    else:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "state.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((directory / "kronbot.lock").stat().st_mode) == 0o600
    with StateStore(directory) as store:
        assert store.data["session"]["refresh_token"] == "synthetic-refresh"
        assert store.data["session"]["isu"] == "123456"
        assert store.data["queues"]["owner"][0]["lesson_id"] == 1


def test_corrupt_state_is_not_overwritten(tmp_path):
    directory = tmp_path / "private"
    with StateStore(directory) as store:
        store.save()
    path = directory / "state.json"
    path.write_bytes(b"broken")
    with pytest.raises(StateError):
        with StateStore(directory):
            pass
    assert path.read_bytes() == b"broken"


def test_lock_excludes_other_process(tmp_path):
    directory = tmp_path / "state"
    code = "from pathlib import Path; from kronbot.state import StateStore; import sys; s=StateStore(Path(sys.argv[1])); s.__enter__(); print('ready',flush=True); sys.stdin.readline(); s.__exit__(None,None,None)"
    process = subprocess.Popen([sys.executable, "-c", code, str(directory)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(StateError, match="уже открыт"):
            with StateStore(directory):
                pass
    finally:
        process.stdin.write("\n")
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
    with StateStore(directory) as store:
        assert store.data["queues"] == {}


def test_legacy_session_migrates_and_is_persisted(tmp_path):
    from kronbot.state import empty_state

    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.save()
    legacy = empty_state()
    legacy["session"] = {
        "sub": "owner", "access_token": "access", "access_expires_at": 100,
        "refresh_token": "refresh", "refresh_expires_at": 200,
    }
    raw = json.dumps(legacy).encode()
    if os.name == "nt":
        import base64
        import win32crypt
        blob = win32crypt.CryptProtectData(raw, "kronbot state", None, None, None, 1)
        raw = json.dumps({"version": 1, "dpapi": base64.b64encode(blob).decode()}).encode()
    (directory / "state.json").write_bytes(raw)
    with StateStore(directory) as store:
        assert store.data["session"]["isu"] is None
        store.save()
    with StateStore(directory) as store:
        assert store.data["session"]["isu"] is None
        assert store.data["session"]["refresh_token"] == "refresh"


@pytest.mark.parametrize("isu", ["", "0", "１２３", "12\n", 123, True, []])
def test_invalid_stored_isu_is_rejected(tmp_path, isu):
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {
            "sub": "owner", "access_token": "access", "access_expires_at": 100,
            "refresh_token": None, "refresh_expires_at": None, "isu": isu,
        }
        with pytest.raises(StateError):
            store.save()


@pytest.mark.parametrize("field,value", [
    ("prototype_lesson_id", 0),
    ("section_id", None),
    ("teacher_isu", True),
    ("room_id", 0),
    ("type_id", 5),
    ("bound", "yes"),
    ("predicted_ends_at", "2030-01-15T11:31:00+03:00"),
])
def test_invalid_forecast_state_is_rejected(tmp_path, field, value):
    forecast = {
        "bound": False, "prototype_lesson_id": 1, "section_id": 31,
        "teacher_isu": 200002, "building_id": 13, "room_id": 1001,
        "section_level": 1, "lesson_level": 1, "type_id": 2, "time_slot_id": 2,
        "prototype_starts_at": "2030-01-01T10:00:00+03:00",
        "prototype_ends_at": "2030-01-01T11:30:00+03:00",
        "predicted_ends_at": "2030-01-15T11:30:00+03:00",
        "teacher_name": "Тренер", "room_name": "Зал",
    }
    forecast[field] = value
    with StateStore(tmp_path / "state") as store:
        store.data["queues"]["owner"] = [{
            "lesson_id": 1, "section_name": "Секция",
            "starts_at": "2030-01-15T10:00:00+03:00", "status": "waiting",
            "refusal_code": None, "forecast": forecast,
        }]
        with pytest.raises(StateError):
            store.save()


def test_legacy_state_without_ntfy_migrates_without_losing_data(tmp_path):
    from kronbot.state import empty_state

    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.save()
    legacy = empty_state()
    del legacy["ntfy"]
    legacy["blocked_until"] = {"my.itmo.ru": 12345}
    legacy["session"] = {
        "sub": "owner", "access_token": "access", "access_expires_at": 100,
        "refresh_token": None, "refresh_expires_at": None,
    }
    raw = json.dumps(legacy).encode()
    if os.name == "nt":
        import base64
        import win32crypt
        blob = win32crypt.CryptProtectData(raw, "kronbot state", None, None, None, 1)
        raw = json.dumps({"version": 1, "dpapi": base64.b64encode(blob).decode()}).encode()
    (directory / "state.json").write_bytes(raw)
    with StateStore(directory) as store:
        assert store.data["ntfy"] is None
        assert store.data["blocked_until"] == legacy["blocked_until"]
        assert store.data["session"]["access_token"] == "access"
        store.save()
    with StateStore(directory) as store:
        assert store.data["ntfy"] is None
        assert store.data["session"]["sub"] == "owner"
        assert store.data["blocked_until"] == legacy["blocked_until"]


@pytest.mark.parametrize("settings", [
    {},
    {"url": "http://example.com/topic", "token": None, "last_error": None},
    {"url": "https://user@example.com/topic", "token": None, "last_error": None},
    {"url": "https://example.com/topic?secret=1", "token": None, "last_error": None},
    {"url": "https://example.com/topic#secret", "token": None, "last_error": None},
    {"url": "https://example.com/topic", "token": 1, "last_error": None},
    {"url": "https://example.com/topic", "token": None, "last_error": False},
    {"url": "https://example.com/topic", "token": None, "last_error": None, "extra": 1},
])
def test_invalid_ntfy_state_is_rejected(tmp_path, settings):
    with StateStore(tmp_path / "state") as store:
        store.data["ntfy"] = settings
        with pytest.raises(StateError):
            store.save()
