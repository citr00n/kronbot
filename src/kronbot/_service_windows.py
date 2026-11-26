"""Per-user Task Scheduler integration; no credentials are stored in the task."""

from contextlib import contextmanager
from pathlib import Path
import os
import subprocess


_TASK_NAME = "kronbot-user-background-service"
_MARKER = "kronbot user background service v1"
_NOT_FOUND = 0x80070002


class _ServiceError(Exception):
    """A deliberately safe message intended for the caller."""


@contextmanager
def _scheduler():
    # Imports stay lazy so the module can also be inspected on other platforms.
    import pythoncom
    import win32api
    import win32con
    import win32security
    import win32com.client

    pythoncom.CoInitialize()
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.ConvertSidToStringSid(
                win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            )
        finally:
            token.Close()
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        yield service, service.GetFolder("\\"), sid
    finally:
        pythoncom.CoUninitialize()


def _missing(error: Exception) -> bool:
    code = getattr(error, "hresult", None)
    return isinstance(code, int) and code & 0xFFFFFFFF == _NOT_FOUND


def _get_task(folder):
    try:
        return folder.GetTask(_TASK_NAME)
    except Exception as error:
        if _missing(error):
            return None
        raise


def _owned(task, sid: str) -> bool:
    definition = task.Definition
    actions = definition.Actions
    return (
        definition.RegistrationInfo.Description == _MARKER
        and definition.Principal.UserId == sid
        and definition.Principal.LogonType == 3  # interactive token, no password
        and actions.Count == 1
        and actions.Item(1).Type == 0  # executable action
    )


def _require_owned(folder, sid: str):
    task = _get_task(folder)
    if task is None:
        raise _ServiceError("Служба kronbot не установлена.")
    if not _owned(task, sid):
        raise _ServiceError("Имя службы занято другой задачей.")
    return task


def install(executable: str, state_dir: Path) -> None:
    if not isinstance(executable, str) or not os.path.isabs(executable) or not state_dir.is_absolute():
        raise RuntimeError("Для службы нужны абсолютные пути.")
    try:
        with _scheduler() as (service, folder, sid):
            if _get_task(folder) is not None:
                raise _ServiceError("Имя службы занято существующей задачей.")
            definition = service.NewTask(0)
            definition.RegistrationInfo.Description = _MARKER
            definition.Principal.UserId = sid
            definition.Principal.LogonType = 3  # TASK_LOGON_INTERACTIVE_TOKEN
            definition.Principal.RunLevel = 0  # TASK_RUNLEVEL_LUA
            trigger = definition.Triggers.Create(9)  # TASK_TRIGGER_LOGON
            trigger.UserId = sid
            settings = definition.Settings
            settings.Enabled = True
            settings.StartWhenAvailable = True
            settings.DisallowStartIfOnBatteries = False
            settings.StopIfGoingOnBatteries = False
            settings.ExecutionTimeLimit = "PT0S"
            settings.MultipleInstances = 2  # TASK_INSTANCES_IGNORE_NEW
            action = definition.Actions.Create(0)  # TASK_ACTION_EXEC
            action.Path = executable
            action.Arguments = subprocess.list2cmdline(
                ["service", "run", "--state-dir", str(state_dir)]
            )
            # TASK_CREATE fails atomically if a task appeared after the check.
            task = folder.RegisterTaskDefinition(_TASK_NAME, definition, 2, sid, None, 3, None)
            try:
                task.Run("")
            except Exception:
                raise _ServiceError("Служба установлена, но не запущена.") from None
    except _ServiceError as error:
        raise RuntimeError(str(error)) from None
    except Exception:
        raise RuntimeError("Не удалось установить службу kronbot.") from None


def uninstall() -> None:
    try:
        with _scheduler() as (_, folder, sid):
            task = _get_task(folder)
            if task is None:
                return
            if not _owned(task, sid):
                raise _ServiceError("Имя службы занято другой задачей.")
            if task.State in (2, 4):
                task.Stop(0)
            folder.DeleteTask(_TASK_NAME, 0)
    except _ServiceError as error:
        raise RuntimeError(str(error)) from None
    except Exception:
        raise RuntimeError("Не удалось удалить службу kronbot.") from None


def start() -> None:
    try:
        with _scheduler() as (_, folder, sid):
            _require_owned(folder, sid).Run("")
    except _ServiceError as error:
        raise RuntimeError(str(error)) from None
    except Exception:
        raise RuntimeError("Не удалось запустить службу kronbot.") from None


def stop() -> None:
    try:
        with _scheduler() as (_, folder, sid):
            task = _require_owned(folder, sid)
            if task.State in (2, 4):
                task.Stop(0)
    except _ServiceError as error:
        raise RuntimeError(str(error)) from None
    except Exception:
        raise RuntimeError("Не удалось остановить службу kronbot.") from None


def status() -> str:
    try:
        with _scheduler() as (_, folder, sid):
            task = _get_task(folder)
            if task is None:
                return "not installed"
            if not _owned(task, sid):
                raise _ServiceError("Имя службы занято другой задачей.")
            if task.State == 4:
                return "running"
            if task.State in (1, 2, 3):
                return "stopped"
            raise _ServiceError("Не удалось определить состояние службы kronbot.")
    except _ServiceError as error:
        raise RuntimeError(str(error)) from None
    except Exception:
        raise RuntimeError("Не удалось определить состояние службы kronbot.") from None
