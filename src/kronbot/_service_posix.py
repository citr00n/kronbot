"""Per-user launchd and systemd service integration.

The service definition contains only paths, never the current environment or credentials.
"""

import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from xml.parsers.expat import ExpatError
import tempfile

_LABEL = "ru.kronbot.agent"
_UNIT = "kronbot.service"
_TIMEOUT = 15
_HEADER = "# Managed by kronbot user service; do not edit.\n"


def _platform() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise RuntimeError("Пользовательская служба не поддерживается на этой платформе.")


def _path(kind: str) -> Path:
    home = Path.home()
    if kind == "launchd":
        return home / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".config"
    return base / "systemd" / "user" / _UNIT


def _quote(value: str) -> str:
    # systemd expands specifiers and environment variables inside quotes too.
    escaped = []
    for char in value:
        if char in ('\\', '"'):
            escaped.append("\\" + char)
        elif char in ("%", "$"):
            escaped.append(char * 2)
        elif ord(char) < 32 or ord(char) == 127:
            escaped.append(f"\\x{ord(char):02x}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _definition(kind: str, executable: str, state_dir: Path) -> bytes:
    args = [executable, "service", "run", "--state-dir", str(state_dir)]
    if kind == "launchd":
        return plistlib.dumps({"Label": _LABEL, "ProgramArguments": args,
                               "RunAtLoad": True, "KeepAlive": False}, sort_keys=True)
    return (_HEADER + "[Unit]\nDescription=kronbot user service\n\n"
            "[Service]\nType=simple\nExecStart=" + " ".join(map(_quote, args)) +
            "\nRestart=no\n\n"
            "[Install]\nWantedBy=default.target\n").encode("utf-8")


def _owned(kind: str, data: bytes) -> bool:
    if kind == "launchd":
        try:
            value = plistlib.loads(data)
            if not isinstance(value, dict) or set(value) != {"Label", "ProgramArguments", "RunAtLoad", "KeepAlive"}:
                return False
            args = value["ProgramArguments"]
            return (value["Label"] == _LABEL and value["RunAtLoad"] is True
                    and value["KeepAlive"] is False and isinstance(args, list)
                    and len(args) == 5 and all(isinstance(a, str) for a in args)
                    and args[1:4] == ["service", "run", "--state-dir"]
                    and Path(args[0]).is_absolute() and Path(args[4]).is_absolute())
        except (ValueError, TypeError, OverflowError, ExpatError):
            return False
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return False
    # Match the complete definition, not merely a comment another file might contain.
    token = r'"(?:[^"\\\n\r]|\\(?:[\\"]|x[0-9a-f]{2}))*"'
    pattern = (re.escape(_HEADER + "[Unit]\nDescription=kronbot user service\n\n"
                         "[Service]\nType=simple\nExecStart=") + token +
               re.escape(' "service" "run" "--state-dir" ') + token +
               re.escape("\nRestart=no\n\n"
                         "[Install]\nWantedBy=default.target\n"))
    return re.fullmatch(pattern, text) is not None


def _require_owned(kind: str, path: Path) -> None:
    try:
        if path.is_symlink() or not path.is_file() or not _owned(kind, path.read_bytes()):
            raise RuntimeError("Конфигурация службы отсутствует или принадлежит другой программе.")
    except OSError:
        raise RuntimeError("Не удалось прочитать конфигурацию службы.") from None


def _unquote(token: str) -> str:
    """Reverse exactly the escaping produced by _quote, in a single pass."""
    result = []
    index = 1
    while index < len(token) - 1:
        char = token[index]
        if char == "\\":
            index += 1
            char = token[index]
            if char == "x":
                char = chr(int(token[index + 1 : index + 3], 16))
                index += 2
        elif char in ("%", "$"):
            if token[index : index + 2] != char * 2:
                raise ValueError
            index += 1
        result.append(char)
        index += 1
    value = "".join(result)
    if _quote(value) != token:
        raise ValueError
    return value


def configured_state_dir() -> Path | None:
    """Read the state directory from this application's installed definition."""
    kind = _platform()
    path = _path(kind)
    try:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError
        data = path.read_bytes()
        if not _owned(kind, data):
            raise ValueError
        if kind == "launchd":
            args = plistlib.loads(data)["ProgramArguments"]
            executable, directory = args[0], args[4]
        else:
            text = data.decode("utf-8")
            line = next(
                line for line in text.split("\n") if line.startswith("ExecStart=")
            )
            token = r'"(?:[^"\\\n\r]|\\(?:[\\"]|x[0-9a-f]{2}))*"'
            match = re.fullmatch(
                r"ExecStart=("
                + token
                + r') "service" "run" "--state-dir" ('
                + token
                + r")",
                line,
            )
            if match is None:
                raise ValueError
            executable, directory = (_unquote(value) for value in match.groups())
        if (
            "\x00" in executable
            or "\x00" in directory
            or not Path(executable).is_absolute()
            or not Path(directory).is_absolute()
        ):
            raise ValueError
        return Path(directory)
    except (
        OSError,
        ValueError,
        TypeError,
        UnicodeError,
        OverflowError,
        ExpatError,
        StopIteration,
        KeyError,
        IndexError,
    ):
        raise RuntimeError(
            "Не удалось прочитать принадлежащую kronbot конфигурацию службы."
        ) from None


def _command(
    args: list[str], *, allowed: tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, shell=False, capture_output=True, text=True,
                                timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise RuntimeError("Не удалось обратиться к диспетчеру пользовательских служб.") from None
    if result.returncode not in allowed:
        raise RuntimeError("Диспетчер пользовательских служб отклонил операцию.")
    return result


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launch_loaded() -> bool:
    result = _command(["launchctl", "print", f"{_domain()}/{_LABEL}"], allowed=(0, 113))
    return result.returncode == 0


def _install_file(path: Path, data: bytes) -> None:
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".kronbot-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link publishes the complete file atomically without replacing a
        # pre-existing file, even if one appears after the initial check.
        os.link(temporary, path)
    except FileExistsError:
        raise RuntimeError("Конфигурация службы уже существует; она не изменена.") from None
    except OSError:
        raise RuntimeError("Не удалось создать конфигурацию службы.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def install(executable: str, state_dir: Path) -> None:
    kind = _platform()
    try:
        exe = os.fspath(executable)
        directory = os.fspath(state_dir)
        if (not isinstance(exe, str) or not isinstance(directory, str)
                or not Path(exe).is_absolute() or not Path(directory).is_absolute()
                or "\x00" in exe or "\x00" in directory):
            raise ValueError
        data = _definition(kind, exe, Path(directory))
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise RuntimeError("Для службы нужны корректные абсолютные пути.") from None
    path = _path(kind)
    _install_file(path, data)
    if kind == "launchd":
        _command(["launchctl", "bootstrap", _domain(), str(path)])
    else:
        _command(["systemctl", "--user", "daemon-reload"])
        _command(["systemctl", "--user", "enable", "--now", _UNIT])


def uninstall() -> None:
    kind = _platform()
    path = _path(kind)
    _require_owned(kind, path)
    if kind == "launchd":
        if _launch_loaded():
            _command(["launchctl", "bootout", _domain(), str(path)])
    else:
        _command(["systemctl", "--user", "disable", "--now", _UNIT])
    try:
        path.unlink()
    except OSError:
        raise RuntimeError("Не удалось удалить конфигурацию службы.") from None
    if kind == "systemd":
        _command(["systemctl", "--user", "daemon-reload"])


def start() -> None:
    kind = _platform()
    path = _path(kind)
    _require_owned(kind, path)
    if kind == "launchd":
        # A stopped LaunchAgent may still be loaded after its process exits.
        if _launch_loaded():
            _command(["launchctl", "kickstart", f"{_domain()}/{_LABEL}"])
        else:
            _command(["launchctl", "bootstrap", _domain(), str(path)])
    else:
        _command(["systemctl", "--user", "start", _UNIT])


def stop() -> None:
    kind = _platform()
    path = _path(kind)
    _require_owned(kind, path)
    if kind == "launchd":
        if _launch_loaded():
            _command(["launchctl", "bootout", _domain(), str(path)])
    else:
        _command(["systemctl", "--user", "stop", _UNIT])


def status() -> str:
    kind = _platform()
    path = _path(kind)
    if not path.exists() and not path.is_symlink():
        return "not installed"
    _require_owned(kind, path)
    if kind == "launchd":
        result = _command(["launchctl", "print", f"{_domain()}/{_LABEL}"], allowed=(0, 113))
        if result.returncode == 113:
            return "stopped"
        return "running" if re.search(r"^\s*state\s*=\s*running\s*$", result.stdout, re.MULTILINE) else "stopped"
    result = _command(["systemctl", "--user", "is-active", "--quiet", _UNIT], allowed=(0, 3, 4))
    return "running" if result.returncode == 0 else "stopped"
