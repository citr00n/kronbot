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
