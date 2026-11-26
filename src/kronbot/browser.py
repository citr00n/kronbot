"""Перехват callback до отправки запроса в изолированном Chromium."""

import asyncio
from urllib.parse import urlsplit


class BrowserError(Exception):
    pass


class BrowserRestricted(BrowserError):
    def __init__(self, status: int, retry_after: str | None):
        super().__init__("Доступ к ITMO.ID ограничен.")
        self.status = status
        self.retry_after = retry_after


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urlsplit(url)
    if p.username is not None or p.password is not None:
        raise ValueError("userinfo")
    return p.scheme, p.hostname or "", p.port


async def capture_callback(page, auth_url: str, callback_url: str, cancel: asyncio.Event) -> str:
    """Перехватить основной frame через Fetch, не загружая callback."""
    if cancel.is_set():
        raise BrowserError("Вход отменён.")
    login_origin, callback_origin = _origin(auth_url), _origin(callback_url)
    callback_path = urlsplit(callback_url).path
    cdp = await page.context.new_cdp_session(page)
    frame_tree = await cdp.send("Page.getFrameTree")
    frame_id = frame_tree["frameTree"]["frame"]["id"]
    result = asyncio.get_running_loop().create_future()
    tasks: set[asyncio.Task] = set()
    restriction = None

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def fail(exc):
        if not result.done():
            result.set_exception(exc)

    async def paused(event):
        request_id = event["requestId"]
        url = event["request"]["url"]
        main = event.get("frameId") == frame_id
        try:
            origin = _origin(url)
            is_callback = origin == callback_origin and urlsplit(url).path == callback_path
            if is_callback:
                await cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
                if main and not cancel.is_set() and not result.done():
                    result.set_result(url)
            elif main and origin != login_origin:
                await cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
                fail(BrowserError("Переход за пределы ITMO.ID запрещён."))
            else:
                await cdp.send("Fetch.continueRequest", {"requestId": request_id})
        except Exception:
            try:
                await cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
            except Exception:
                pass
            fail(BrowserError("Не удалось безопасно перехватить вход."))

    def response_received(response):
        nonlocal restriction
        try:
            if (response.request.is_navigation_request() and response.frame == page.main_frame
                    and _origin(response.url) == login_origin and response.status in (403, 429)):
                restriction = BrowserRestricted(response.status, response.headers.get("retry-after"))
                fail(restriction)
        except Exception:
            fail(BrowserError("Не удалось проверить ответ ITMO.ID."))

    cdp.on("Fetch.requestPaused", lambda event: spawn(paused(event)))
    page.on("response", response_received)
    page.on("close", lambda: fail(BrowserError("Окно входа закрыто.")))
    page.context.browser.on("disconnected", lambda: fail(BrowserError("Браузер закрыт.")))
    await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]})
    try:
        try:
            await page.goto(auth_url, wait_until="commit", timeout=15000)
        except Exception:
            if not result.done():
                # The aborted callback can race the goto rejection.
                await asyncio.sleep(0)
            if not result.done():
                raise BrowserError("Не удалось открыть ITMO.ID.") from None
        async with asyncio.timeout(300):
            while not result.done():
                if cancel.is_set():
                    raise BrowserError("Вход отменён.")
                await asyncio.wait({result}, timeout=0.1)
            await asyncio.sleep(0)
            if restriction is not None:
                raise restriction
            if cancel.is_set():
                raise BrowserError("Вход отменён.")
            return result.result()
    except TimeoutError:
        raise BrowserError("Время входа истекло.") from None
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if not result.done():
            result.cancel()
        elif not result.cancelled():
            result.exception()
        try:
            await cdp.detach()
        except Exception:
            pass


async def browser_login(auth_url: str, callback_url: str, cancel: asyncio.Event) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise BrowserError("Установите Playwright и Chromium или выберите другой способ входа.") from None
    browser = context = page = None
    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=False, chromium_sandbox=True, timeout=15000)
                context = await browser.new_context(service_workers="block", accept_downloads=False)
                page = await context.new_page()
                context.on("page", lambda other: asyncio.create_task(other.close()) if other != page else None)
                return await capture_callback(page, auth_url, callback_url, cancel)
            finally:
                if page and not page.is_closed():
                    await page.close()
                if context:
                    await context.close()
                if browser:
                    await browser.close()
    except BrowserError:
        raise
    except Exception:
        raise BrowserError("Не удалось безопасно открыть Chromium. Установите браузер по README или выберите другой способ входа.") from None
