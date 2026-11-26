import asyncio
import os

import pytest

from kronbot.browser import BrowserError, BrowserRestricted, capture_callback

pytestmark = pytest.mark.browser


@pytest.fixture
async def browser_page():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        headed = os.environ.get("KRONBOT_TEST_HEADED") == "1"
        browser = await p.chromium.launch(headless=not headed, chromium_sandbox=headed)
        context = await browser.new_context(service_workers="block", accept_downloads=False)
        page = await context.new_page()
        yield page
        await context.close()
        await browser.close()


async def serve(handler):
    return await asyncio.start_server(handler, "127.0.0.1", 0)


def origin(server):
    return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def test_callback_is_never_requested(browser_page):
    paths = []
    async def handle(reader, writer):
        line = await reader.readline()
        path = line.decode().split()[1]
        paths.append(path)
        while await reader.readline() not in (b"\r\n", b""):
            pass
        writer.write(b"HTTP/1.1 302 Found\r\nLocation: /callback?code=test-code&state=test-state\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
    server = await serve(handle)
    async with server:
        base = origin(server)
        result = await capture_callback(browser_page, base + "/login", base + "/callback", asyncio.Event())
        assert result == base + "/callback?code=test-code&state=test-state"
        assert paths == ["/login"]


async def test_restriction_is_reported(browser_page):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 429 Too Many Requests\r\nRetry-After: 300\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
    server = await serve(handle)
    async with server:
        base = origin(server)
        with pytest.raises(BrowserRestricted) as exc:
            await capture_callback(browser_page, base + "/login", base + "/callback", asyncio.Event())
        assert (exc.value.status, exc.value.retry_after) == (429, "300")


async def test_cross_origin_redirect_is_blocked(browser_page):
    hits = []
    async def forbidden(reader, writer):
        hits.append(True)
        writer.close()
    other = await serve(forbidden)
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(f"HTTP/1.1 302 Found\r\nLocation: {origin(other)}/other\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        writer.close()
    server = await serve(handle)
    async with server, other:
        base = origin(server)
        with pytest.raises(BrowserError):
            await capture_callback(browser_page, base + "/login", base + "/callback", asyncio.Event())
        assert not hits


async def test_pre_cancelled_login_does_not_navigate(browser_page):
    hits = []
    async def handle(reader, writer):
        hits.append(True)
        writer.close()
    server = await serve(handle)
    async with server:
        base = origin(server)
        cancelled = asyncio.Event()
        cancelled.set()
        with pytest.raises(BrowserError, match="отменён"):
            await capture_callback(browser_page, base + "/login", base + "/callback", cancelled)
        assert not hits
