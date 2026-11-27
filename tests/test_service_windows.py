from contextlib import contextmanager
from unittest.mock import MagicMock
import subprocess
import sys
from pathlib import Path

import pytest

from kronbot import _service_windows as windows


class MissingTask(Exception):
    hresult = -2147024894  # HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND)


@pytest.fixture
def scheduler(monkeypatch):
    service, folder, task = MagicMock(), MagicMock(), MagicMock()
    definition = service.NewTask.return_value
    definition.Actions.Count = 1
    definition.Actions.Item.return_value.Type = 0
    task.Definition = definition
    task.State = 3
    folder.GetTask.side_effect = MissingTask()
    folder.RegisterTaskDefinition.return_value = task

    @contextmanager
    def connect():
        yield service, folder, "S-1-5-21-123"

    monkeypatch.setattr(windows, "_scheduler", connect)
    return service, folder, task


def test_install_creates_interactive_logon_task_and_starts(scheduler, tmp_path):
    service, folder, task = scheduler
    executable = str(tmp_path / "Программа с пробелом" / "kronbot.exe")
    state = tmp_path / "Данные с пробелом" / "состояние"
    windows.install(executable, state)
    definition = service.NewTask.return_value
    assert definition.Principal.UserId == "S-1-5-21-123"
    assert definition.Principal.LogonType == 3
    assert definition.Principal.RunLevel == 0
    definition.Triggers.Create.assert_called_once_with(9)
    assert definition.Triggers.Create.return_value.UserId == "S-1-5-21-123"
    definition.Actions.Create.assert_called_once_with(0)
    action = definition.Actions.Create.return_value
    assert action.Path == executable
    assert action.Arguments == subprocess.list2cmdline(["service", "run", "--state-dir", str(state)])
    assert '"' in action.Arguments
    assert definition.Settings.ExecutionTimeLimit == "PT0S"
    assert definition.Settings.MultipleInstances == 2
    assert definition.Settings.DisallowStartIfOnBatteries is False
    folder.RegisterTaskDefinition.assert_called_once_with(
        windows._TASK_NAME, definition, 2, "S-1-5-21-123", None, 3, None
    )
    task.Run.assert_called_once_with("")


def test_existing_task_is_never_overwritten(scheduler, tmp_path):
    _, folder, task = scheduler
    folder.GetTask.side_effect = None
    folder.GetTask.return_value = task
    with pytest.raises(RuntimeError):
        windows.install(str(tmp_path / "kronbot.exe"), tmp_path)
    folder.RegisterTaskDefinition.assert_not_called()
    task.Run.assert_not_called()


def test_create_race_does_not_replace_task(scheduler, tmp_path):
    _, folder, task = scheduler
    folder.RegisterTaskDefinition.side_effect = RuntimeError("private details")
    with pytest.raises(RuntimeError) as error:
        windows.install(str(tmp_path / "kronbot.exe"), tmp_path)
    assert "private details" not in str(error.value)
    task.Run.assert_not_called()
    assert folder.RegisterTaskDefinition.call_args.args[2] == 2


def test_controls_and_status(scheduler):
    service, folder, task = scheduler
    definition = service.NewTask.return_value
    definition.RegistrationInfo.Description = windows._MARKER
    definition.Principal.UserId = "S-1-5-21-123"
    definition.Principal.LogonType = 3
    folder.GetTask.side_effect = None
    folder.GetTask.return_value = task
    assert windows.status() == "stopped"
    windows.start()
    task.Run.assert_called_once_with("")
    task.State = 4
    assert windows.status() == "running"
    windows.stop()
    task.Stop.assert_called_once_with(0)
    task.Stop.reset_mock()
    windows.uninstall()
    task.Stop.assert_called_once_with(0)
    folder.DeleteTask.assert_called_once_with(windows._TASK_NAME, 0)


def test_missing_task(scheduler):
    _, folder, _ = scheduler
    assert windows.status() == "not installed"
    windows.uninstall()
    folder.DeleteTask.assert_not_called()
    with pytest.raises(RuntimeError):
        windows.start()
    with pytest.raises(RuntimeError):
        windows.stop()


@pytest.mark.parametrize("operation", [windows.start, windows.stop, windows.status, windows.uninstall])
def test_foreign_task_is_not_manipulated(scheduler, operation):
    _, folder, task = scheduler
    folder.GetTask.side_effect = None
    folder.GetTask.return_value = task
    task.Definition.RegistrationInfo.Description = "another program"
    with pytest.raises(RuntimeError):
        operation()
    task.Run.assert_not_called()
    task.Stop.assert_not_called()
    folder.DeleteTask.assert_not_called()


@pytest.mark.parametrize("operation", [windows.start, windows.stop, windows.status, windows.uninstall])
def test_scheduler_errors_do_not_expose_details(scheduler, operation):
    _, folder, _ = scheduler
    folder.GetTask.side_effect = RuntimeError("secret-token")
    with pytest.raises(RuntimeError) as error:
        operation()
    assert "secret-token" not in str(error.value)


def test_failed_initial_start_reports_installed_without_secrets(scheduler, tmp_path):
    _, folder, task = scheduler
    task.Run.side_effect = RuntimeError("secret-token")
    with pytest.raises(RuntimeError, match="установлена, но не запущена") as error:
        windows.install(str(tmp_path / "kronbot.exe"), tmp_path)
    assert "secret-token" not in str(error.value)
    folder.DeleteTask.assert_not_called()


def test_relative_paths_are_rejected(scheduler, tmp_path):
    _, folder, _ = scheduler
    with pytest.raises(RuntimeError):
        windows.install("kronbot.exe", tmp_path)
    folder.RegisterTaskDefinition.assert_not_called()


def test_configured_state_dir_missing(scheduler):
    assert windows.configured_state_dir() is None


def test_configured_state_dir_owned(scheduler, monkeypatch, tmp_path):
    _, folder, task = scheduler
    folder.GetTask.side_effect = None
    folder.GetTask.return_value = task
    definition = task.Definition
    definition.RegistrationInfo.Description = windows._MARKER
    definition.Principal.UserId = "S-1-5-21-123"
    definition.Principal.LogonType = 3
    state_dir = tmp_path / "Данные с пробелом"
    arguments = subprocess.list2cmdline(["service", "run", "--state-dir", str(state_dir)])
    definition.Actions.Item.return_value.Arguments = arguments
    parser = MagicMock(return_value=["service", "run", "--state-dir", str(state_dir)])
    monkeypatch.setattr(windows, "_split_arguments", parser)

    assert windows.configured_state_dir() == state_dir
    parser.assert_called_once_with(arguments)
    task.Run.assert_not_called()
    task.Stop.assert_not_called()
    folder.RegisterTaskDefinition.assert_not_called()
    folder.DeleteTask.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("Description", "foreign"),
    ("UserId", "S-1-5-21-other"),
    ("LogonType", 1),
])
def test_configured_state_dir_rejects_foreign_task(scheduler, monkeypatch, field, value):
    _, folder, task = scheduler
    folder.GetTask.side_effect = None
    folder.GetTask.return_value = task
    definition = task.Definition
    definition.RegistrationInfo.Description = windows._MARKER
    definition.Principal.UserId = "S-1-5-21-123"
    definition.Principal.LogonType = 3
    setattr(definition.RegistrationInfo if field == "Description" else definition.Principal, field, value)
    parser = MagicMock()
    monkeypatch.setattr(windows, "_split_arguments", parser)
    with pytest.raises(RuntimeError):
        windows.configured_state_dir()
    parser.assert_not_called()


@pytest.mark.parametrize("parts", [
    ["service", "run", "--state-dir", "relative"],
    ["service", "run", "--state-dir"],
    ["service", "run", "--state-dir", "/absolute", "extra"],
    ["other", "run", "--state-dir", "/absolute"],
])
def test_configured_state_dir_rejects_invalid_arguments(scheduler, monkeypatch, parts):
    _, folder, task = scheduler
    folder.GetTask.side_effect = None
    folder.GetTask.return_value = task
    task.Definition.RegistrationInfo.Description = windows._MARKER
    task.Definition.Principal.UserId = "S-1-5-21-123"
    task.Definition.Principal.LogonType = 3
    task.Definition.Actions.Item.return_value.Arguments = "secret-token"
    monkeypatch.setattr(windows, "_split_arguments", lambda _: parts)
    with pytest.raises(RuntimeError) as error:
        windows.configured_state_dir()
    assert "secret-token" not in str(error.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command-line parser")
def test_split_arguments_windows_unicode_and_quotes():
    state = Path(r"C:\Данные с пробелом\папка")
    parts = ["service", "run", "--state-dir", str(state)]
    assert windows._split_arguments(subprocess.list2cmdline(parts)) == parts
