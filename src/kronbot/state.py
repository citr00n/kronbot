"""Приватное локальное состояние и межпроцессная блокировка."""

import base64
import binascii
import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path

import portalocker


class StateError(Exception):
    pass


class StateLocked(StateError):
    pass


def state_directory() -> Path:
    override = os.environ.get("KRONBOT_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "kronbot"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kronbot"


def empty_state():
    return {"version": 1, "session": None, "queues": {}, "sport_request_times": [],
            "last_enroll_at": None, "blocked_until": {}, "background_paused": False}


def _integer(value):
    return type(value) is int


def _validate_state(data):
    if not isinstance(data, dict) or set(data) != set(empty_state()) or data["version"] != 1 or type(data["version"]) is not int:
        raise ValueError()
    if type(data["background_paused"]) is not bool:
        raise ValueError()
    session = data["session"]
    if session is not None:
        if not isinstance(session, dict) or set(session) != {"sub", "access_token", "access_expires_at", "refresh_token", "refresh_expires_at"}:
            raise ValueError()
        if not all(isinstance(session[k], str) and session[k] for k in ("sub", "access_token")):
            raise ValueError()
        if not _integer(session["access_expires_at"]):
            raise ValueError()
        if session["refresh_token"] is None:
            if session["refresh_expires_at"] is not None:
                raise ValueError()
        elif not isinstance(session["refresh_token"], str) or not session["refresh_token"] or not _integer(session["refresh_expires_at"]):
            raise ValueError()
    queues = data["queues"]
    if not isinstance(queues, dict):
        raise ValueError()
    for sub, queue in queues.items():
        if not isinstance(sub, str) or not sub or not isinstance(queue, list):
            raise ValueError()
        seen = set()
        for item in queue:
            if not isinstance(item, dict) or set(item) != {"lesson_id", "section_name", "starts_at", "status", "refusal_code"}:
                raise ValueError()
            ident = item["lesson_id"]
            if not _integer(ident) or ident <= 0 or ident in seen:
                raise ValueError()
            seen.add(ident)
            if not isinstance(item["section_name"], str) or not isinstance(item["starts_at"], str):
                raise ValueError()
            parsed = datetime.fromisoformat(item["starts_at"])
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError()
            if item["status"] not in {"waiting", "uncertain", "booked", "rejected", "expired"}:
                raise ValueError()
            if item["refusal_code"] is not None and not _integer(item["refusal_code"]):
                raise ValueError()
    times = data["sport_request_times"]
    if not isinstance(times, list) or any(not _integer(t) for t in times):
        raise ValueError()
    if data["last_enroll_at"] is not None and not _integer(data["last_enroll_at"]):
        raise ValueError()
    blocks = data["blocked_until"]
    if not isinstance(blocks, dict) or any(k not in {"my.itmo.ru", "id.itmo.ru"} or not _integer(v) for k, v in blocks.items()):
        raise ValueError()


def _check_path(path: Path, directory: bool):
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise ValueError("ссылка или reparse point")
    if not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)):
        raise ValueError("неверный тип")
    if os.name == "nt":
        from ._win_state import validate
        validate(path)
    elif st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != (0o700 if directory else 0o600):
        raise ValueError("небезопасный владелец или права")


class StateStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or state_directory()
        self.path = self.directory / "state.json"
        self.lock_path = self.directory / "kronbot.lock"
        self.lock_file = None
        self.data = empty_state()

    def __enter__(self):
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _check_path(self.directory, True)
            if self.lock_path.exists() or self.lock_path.is_symlink():
                _check_path(self.lock_path, False)
            flags = os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.lock_path, flags, 0o600)
            self.lock_file = os.fdopen(fd, "a+b")
            _check_path(self.lock_path, False)
            try:
                portalocker.lock(self.lock_file, portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING)
            except portalocker.exceptions.LockException:
                raise StateLocked("kronbot уже открыт с этим каталогом состояния.") from None
            if self.path.exists() or self.path.is_symlink():
                _check_path(self.path, False)
                raw = self.path.read_bytes()
                outer = json.loads(raw)
                if os.name == "nt":
                    import win32crypt
                    if not isinstance(outer, dict) or set(outer) != {"version", "dpapi"} or outer["version"] != 1:
                        raise ValueError()
                    blob = base64.b64decode(outer["dpapi"], validate=True)
                    _, raw = win32crypt.CryptUnprotectData(blob, None, None, None, 1)
                    outer = json.loads(raw)
                if isinstance(outer, dict) and "background_paused" not in outer:
                    outer["background_paused"] = False
                _validate_state(outer)
                self.data = outer
            return self
        except StateLocked:
            self.__exit__(None, None, None)
            raise
        except Exception as exc:
            self.__exit__(None, None, None)
            raise StateError(f"Небезопасное или повреждённое состояние: {self.directory}") from exc

    def save(self):
        temp = None
        try:
            _validate_state(self.data)
            raw = json.dumps(self.data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if os.name == "nt":
                import win32crypt
                raw = json.dumps({"version": 1, "dpapi": base64.b64encode(win32crypt.CryptProtectData(raw, "kronbot state", None, None, None, 1)).decode("ascii")}).encode("utf-8")
            fd, temp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self.directory)
            with os.fdopen(fd, "wb") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            if os.name == "nt":
                import win32file
                import win32con
                win32file.MoveFileEx(temp, str(self.path), win32con.MOVEFILE_REPLACE_EXISTING | win32con.MOVEFILE_WRITE_THROUGH)
            else:
                os.replace(temp, self.path)
                dirfd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
            temp = None
        except Exception as exc:
            raise StateError(f"Не удалось сохранить состояние: {self.path}") from exc
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except OSError:
                    pass

    def __exit__(self, exc_type, exc, tb):
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None
