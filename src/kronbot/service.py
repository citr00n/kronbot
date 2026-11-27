"""Фоновая обработка локальной очереди с коротким удержанием блокировки."""

import asyncio
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .agent import MSK, Agent, auto_delay, opening_wait
from .auth import AuthError, LoginRequired, Restricted
from .sport import LocalLimit, NetworkBlocked, SportClient, SportError
from .state import StateError, StateLocked, StateStore


class BackgroundRunner:
    """Одна проверка за раз; между проверками хранилище доступно CLI и TUI."""

    def __init__(self, directory: Path, *, now=time.time, monotonic=time.monotonic,
                 client_factory: Callable = SportClient, save_retry_delay: float = 5.0):
        self.directory = directory
        self.now = now
        self.monotonic = monotonic
        self.client_factory = client_factory
        self.save_retry_delay = save_retry_delay
        self.cancel = asyncio.Event()
        self.active_client = None
        self.active_agent = None
        self.next_check = 0.0
        self.seen_uncertain: set[tuple[str, int]] = set()

    def stop(self):
        self.cancel.set()
        if self.active_agent is not None:
            self.active_agent.booking_enabled = False
        if self.active_client is not None:
            self.active_client.stopped = True

    async def cycle(self) -> float:
        """Вернуть задержку до следующей локальной проверки, не удерживая lock."""
        if self.cancel.is_set():
            return 0
        if self.monotonic() < self.next_check:
            return min(5.0, self.next_check - self.monotonic())
        try:
            with StateStore(self.directory) as store:
                session = store.data["session"]
                if store.data["background_paused"] or session is None:
                    return 5.0
                sub = session["sub"]
                queue = store.data["queues"].get(sub, [])
                waiting = [item for item in queue if item["status"] == "waiting"]
                for item in waiting:
                    self.seen_uncertain.discard((sub, item["lesson_id"]))
                today = datetime.fromtimestamp(self.now(), MSK).date()
                uncertain = [item for item in queue if item["status"] == "uncertain"
                             and (today - datetime.fromisoformat(item["starts_at"]).astimezone(MSK).date()).days <= 7
                             and (sub, item["lesson_id"]) not in self.seen_uncertain]
                if not waiting and not uncertain:
                    return 5.0
                if waiting and self.next_check == 0:
                    delay = opening_wait(self.now())
                    if delay:
                        return min(5.0, delay)
                client = self.client_factory(store, now=self.now)
                agent = Agent(store, client, now=self.now, monotonic=self.monotonic)
                self.active_client, self.active_agent = client, agent
                delay = 120.0
                try:
                    await agent.check(allow_booking=True)
                except StateError:
                    if client.pending_session is None:
                        raise
                    # A successful refresh invalidated the old token. Keep the new
                    # pair under lock and retry only persistence, never the exchange.
                    while not self.cancel.is_set():
                        try:
                            await asyncio.wait_for(self.cancel.wait(), timeout=self.save_retry_delay)
                        except TimeoutError:
                            pass
                        if self.cancel.is_set():
                            raise
                        try:
                            store.save()
                        except StateError:
                            continue
                        client.pending_session = None
                        break
                    else:
                        raise
                except (Restricted, NetworkBlocked) as exc:
                    store.data["background_paused"] = True
                    store.save()
                    print(f"Фоновая запись приостановлена: {exc}", file=sys.stderr, flush=True)
                except LocalLimit as exc:
                    delay = max(delay, exc.until - self.now())
                    print(str(exc), file=sys.stderr, flush=True)
                except LoginRequired:
                    print("Фоновая запись ожидает нового входа.", file=sys.stderr, flush=True)
                except (SportError, AuthError) as exc:
                    if not self.cancel.is_set():
                        print(str(exc), file=sys.stderr, flush=True)
                finally:
                    self.seen_uncertain.update((sub, item["lesson_id"]) for item in queue if item["status"] == "uncertain")
                    await client.close()
                    delay = auto_delay(self.now(), delay)
                    self.next_check = self.monotonic() + delay
                    self.active_agent = None
                    self.active_client = None
                return min(5.0, delay)
        except StateLocked:
            return 2.0

    async def run(self) -> int:
        while not self.cancel.is_set():
            try:
                delay = await self.cycle()
            except StateError as exc:
                # Retrying with an old rotated refresh token would be unsafe.
                print(str(exc), file=sys.stderr, flush=True)
                return 1
            if self.cancel.is_set():
                break
            try:
                await asyncio.wait_for(self.cancel.wait(), timeout=delay)
            except TimeoutError:
                pass
        return 0


async def run_service(directory: Path) -> int:
    runner = BackgroundRunner(directory)
    loop = asyncio.get_running_loop()
    previous = {}

    def handle(signum, frame):
        loop.call_soon_threadsafe(runner.stop)

    signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        signals.append(signal.SIGTERM)
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    try:
        for sig in signals:
            previous[sig] = signal.signal(sig, handle)
        return await runner.run()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
