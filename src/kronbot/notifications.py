"""Безопасная доставка коротких уведомлений ntfy."""

import asyncio
from urllib.parse import urlsplit

import httpx


def validate_url(url: str) -> str:
    """Проверить HTTPS-адрес топика, не раскрывая его при ошибке."""
    try:
        if not isinstance(url, str) or not url or url != url.strip():
            raise ValueError()
        if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "?" in url or "#" in url:
            raise ValueError()
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or not parsed.path.strip("/")
                or "\\" in url or parsed.port == 0):
            raise ValueError()
        # HTTPX applies the same host/port parsing before opening a connection.
        httpx.URL(url)
    except (ValueError, TypeError, httpx.InvalidURL):
        raise ValueError("Нужен корректный HTTPS URL топика ntfy.") from None
    return url


async def send(store, message: str, *, transport=None) -> None:
    """Уведомления не должны менять исход записи или раскрывать ошибки HTTP."""
    config = store.data.get("ntfy")
    if not config:
        return
    try:
        url = validate_url(config["url"])
        headers = {"Authorization": "Bearer " + config["token"]} if config.get("token") else {}
        async with asyncio.timeout(5):
            async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                         timeout=5.0) as client:
                await client.post(url, content=message.encode("utf-8"), headers=headers)
    except Exception:
        # Исключения HTTPX могут содержать секретный URL и никогда не выводятся.
        pass


async def error(store, kind: str | None, *, transport=None) -> None:
    """Сохранить тип ошибки до отправки; при восстановлении снять дедупликацию."""
    config = store.data.get("ntfy")
    if not config or config.get("last_error") == kind:
        return
    previous = config.get("last_error")
    config["last_error"] = kind
    try:
        store.save()
    except Exception:
        config["last_error"] = previous
        return
    if kind is not None:
        await send(store, "kronbot: ошибка проверки или записи. Проверьте состояние приложения.", transport=transport)
