import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess

import pytest

from kronbot import _service_posix as service

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX service definitions require POSIX paths and permissions")


@pytest.fixture
def manager(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(service.os, "getuid", lambda: 1234)

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="state = running\n", stderr="")

    monkeypatch.setattr(service.subprocess, "run", run)
    return calls


def test_launch_agent_arguments_and_lifecycle(manager, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_platform", lambda: "launchd")
    executable = str(tmp_path / 'путь с пробелом' / 'kron"bot')
    state = tmp_path / "данные & <state>"
    service.install(executable, state)
    path = tmp_path / "Library/LaunchAgents/ru.kronbot.agent.plist"
    config = plistlib.loads(path.read_bytes())
    assert config == {"Label": "ru.kronbot.agent", "ProgramArguments": [executable, "service", "run", "--state-dir", str(state)], "RunAtLoad": True, "KeepAlive": False}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert manager[0][0] == ["launchctl", "bootstrap", "gui/1234", str(path)]
    assert service.status() == "running"
    service.stop()
    service.uninstall()
    assert not path.exists()
    assert service.status() == "not installed"
    assert all(kwargs["shell"] is False and kwargs["timeout"] > 0 for _, kwargs in manager)


def _systemd_arguments(line):
    # Decode the subset of systemd.syntax emitted by the generator.
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"', line.removeprefix("ExecStart="))
    result = []
    for token in tokens:
        text = token[1:-1]
        text = re.sub(r'\\x([0-9a-f]{2})', lambda m: chr(int(m.group(1), 16)), text)
        text = re.sub(r'\\([\\"])', r'\1', text)
        result.append(text.replace("%%", "%").replace("$$", "$"))
    return result


def test_systemd_arguments_and_lifecycle(manager, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_platform", lambda: "systemd")
    config_home = tmp_path / "конфиг space"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    executable = str(tmp_path / 'путь %n $HOME "x" \\ app')
    state = tmp_path / "состояние\nновая строка"
    service.install(executable, state)
    path = config_home / "systemd/user/kronbot.service"
    config = path.read_text()
    line = next(line for line in config.splitlines() if line.startswith("ExecStart="))
    assert _systemd_arguments(line) == [executable, "service", "run", "--state-dir", str(state)]
    assert "%%n" in line and "$$HOME" in line
    assert "WantedBy=default.target" in config
    assert "Restart=no\n" in config
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [call[0] for call in manager[:2]] == [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "kronbot.service"]]
    assert service.status() == "running"
    service.stop()
    service.start()
    service.uninstall()
    assert not path.exists()
    assert ["systemctl", "--user", "disable", "--now", "kronbot.service"] in [call[0] for call in manager]
    assert manager[-1][0] == ["systemctl", "--user", "daemon-reload"]


@pytest.mark.parametrize("kind", ["launchd", "systemd"])
def test_collision_and_foreign_uninstall_are_safe(kind, manager, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_platform", lambda: kind)
    path = service._path(kind)
    path.parent.mkdir(parents=True)
    path.write_text("foreign configuration")
    with pytest.raises(RuntimeError):
        service.install(str(tmp_path / "app"), tmp_path / "state")
    with pytest.raises(RuntimeError):
        service.uninstall()
    assert path.read_text() == "foreign configuration"
    assert manager == []


@pytest.mark.parametrize("kind", ["launchd", "systemd"])
def test_collision_with_symlink_never_replaces_target(kind, manager, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_platform", lambda: kind)
    path = service._path(kind)
    path.parent.mkdir(parents=True)
    target = tmp_path / "foreign"
    target.write_text("foreign")
    path.symlink_to(target)
    with pytest.raises(RuntimeError):
        service.install(str(tmp_path / "app"), tmp_path / "state")
    with pytest.raises(RuntimeError):
        service.uninstall()
    assert path.is_symlink() and target.read_text() == "foreign"
    assert manager == []


def test_launchd_stopped_uninstall_and_start(manager, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_platform", lambda: "launchd")
    service.install(str(tmp_path / "app"), tmp_path / "state")
    calls = []

    def not_loaded(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 113 if args[1] == "print" else 0, stdout="", stderr="")

    monkeypatch.setattr(service.subprocess, "run", not_loaded)
    assert service.status() == "stopped"
    service.start()
    assert any(args[1] == "bootstrap" for args in calls)
    calls.clear()
    service.uninstall()
    assert not any(args[1] == "bootout" for args in calls)


def test_safe_manager_failure_and_invalid_paths(manager, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_platform", lambda: "systemd")
    with pytest.raises(RuntimeError):
        service.install("relative", tmp_path / "state")
    assert manager == []

    def fail(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 15, output="secret")

    monkeypatch.setattr(service.subprocess, "run", fail)
    with pytest.raises(RuntimeError) as error:
        service.install(str(tmp_path / "app"), tmp_path / "state")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("kind", ["launchd", "systemd"])
def test_configured_state_dir_reads_owned_definition_without_manager(kind, manager, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_platform", lambda: kind)
    path = service._path(kind)
    assert service.configured_state_dir() is None
    state = tmp_path / 'папка с пробелом "кавычки" %n $HOME \\x41 \\ \u2028'
    if kind == "systemd":
        state = state / "control\n\t\x01\x7f"
    path.parent.mkdir(parents=True)
    path.write_bytes(service._definition(kind, str(tmp_path / "программа % $ \\x41"), state))
    before = path.read_bytes()
    assert service.configured_state_dir() == state
    assert path.read_bytes() == before
    assert manager == []


@pytest.mark.parametrize("kind", ["launchd", "systemd"])
@pytest.mark.parametrize("case", ["foreign", "broken", "symlink", "directory", "relative", "nul"])
def test_configured_state_dir_rejects_foreign_or_invalid_config(kind, case, manager, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_platform", lambda: kind)
    path = service._path(kind)
    path.parent.mkdir(parents=True)
    if case == "directory":
        path.mkdir()
    elif case == "symlink":
        target = tmp_path / "target"
        target.write_bytes(service._definition(kind, str(tmp_path / "app"), tmp_path / "state"))
        path.symlink_to(target)
    elif case == "foreign":
        path.write_bytes(b"secret foreign configuration")
    elif case == "broken":
        path.write_bytes(b"\xffsecret")
    else:
        directory = Path("relative") if case == "relative" else Path("/secret\x00path")
        if kind == "launchd" and case == "nul":
            data = service._definition(kind, str(tmp_path / "app"), Path("/tmp/state"))
            path.write_bytes(data.replace(b"/tmp/state", b"/secret&#0;path"))
        else:
            path.write_bytes(service._definition(kind, str(tmp_path / "app"), directory))
    with pytest.raises(RuntimeError) as error:
        service.configured_state_dir()
    assert "secret" not in str(error.value)
    assert manager == []


@pytest.mark.parametrize("value", [r"\x41", "\\\x01", "%%$$", ' " \\ ', "\n\r\t\x7f", "\u2028кириллица"])
def test_systemd_configured_state_dir_round_trips_escapes(value, manager, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_platform", lambda: "systemd")
    path = service._path("systemd")
    path.parent.mkdir(parents=True)
    state = tmp_path / value
    path.write_bytes(service._definition("systemd", str(tmp_path / "app"), state))
    assert service.configured_state_dir() == state
    assert manager == []


@pytest.mark.parametrize("escaped", ['"/tmp/%n"', '"/tmp/$HOME"', '"/tmp/\\x41"'])
def test_systemd_configured_state_dir_rejects_noncanonical_tokens(escaped, manager, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_platform", lambda: "systemd")
    path = service._path("systemd")
    path.parent.mkdir(parents=True)
    data = service._definition("systemd", str(tmp_path / "app"), Path("/tmp/state"))
    path.write_bytes(data.replace(b'"/tmp/state"', escaped.encode()))
    with pytest.raises(RuntimeError):
        service.configured_state_dir()
    assert manager == []
