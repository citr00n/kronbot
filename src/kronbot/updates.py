"""Необязательная проверка опубликованной версии без доступа к состоянию."""

import json
import time
from importlib import metadata

from packaging.version import InvalidVersion, Version
import httpx



_URL = "https://pypi.org/pypi/kronbot/json"
_MAX_RESPONSE = 1024 * 1024


def _version(value: object) -> Version | None:
    if not isinstance(value, str) or len(value) > 100:
        return None
    try:
        version = Version(value)
    except InvalidVersion:
        return None
    return version if version.local is None else None


def check_update() -> str | None:
    """Вернуть рекомендацию об обновлении либо незаметно отказаться от проверки."""
    try:
        current = _version(metadata.version("kronbot"))
        if current is None:
            return None
        deadline = time.monotonic() + 1.5
        with httpx.Client(timeout=1.0, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", _URL) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline or len(body) + len(chunk) > _MAX_RESPONSE:
                        return None
                    body.extend(chunk)
        data = json.loads(body)
        releases = data["releases"]
        if not isinstance(releases, dict):
            return None
        versions = (
            version
            for text, files in releases.items()
            if (version := _version(text)) is not None
            and (current.is_prerelease or not version.is_prerelease)
            and isinstance(files, list)
            and any(isinstance(file, dict) and file.get("yanked") is False for file in files)
        )
        latest = max(versions, default=None)
        if latest is not None and latest > current:
            if latest.is_prerelease:
                commands = "uv tool upgrade --prerelease allow kronbot или pipx upgrade --pip-args=--pre kronbot"
            else:
                commands = "uv tool upgrade kronbot или pipx upgrade kronbot"
            return (
                f"Доступна новая версия kronbot {latest}. Обновите: {commands}. "
                f"Изменения: https://github.com/citr00n/kronbot/releases/tag/v{latest}"
            )
    except Exception:
        return None
    return None
