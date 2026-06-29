from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .mqtt import Publisher
from .renderer import AssetAnimation, build_awtrix_payload
from .settings import Settings


Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


class DuplicateEventId(ValueError):
    pass


@dataclass(frozen=True)
class Binding:
    event_id: str
    generation: int


@dataclass
class Event:
    event_id: str
    clock_prefixes: set[str]
    bindings: dict[str, Binding]
    created_at: datetime
    expires_at: datetime
    asset: AssetAnimation
    sound: str | None = None
    rtttl: str | None = None
    frame_index: int = 0
    states: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EventSpec:
    event_id: str | None
    clock_prefixes: tuple[str, ...]
    duration_seconds: int
    asset: AssetAnimation
    sound: str | None = None
    rtttl: str | None = None


class EventStore:
    def __init__(
        self,
        settings: Settings,
        publisher: Publisher,
        *,
        now: Clock | None = None,
        sleep: Sleeper | None = None,
        start_tasks: bool = True,
    ):
        self.settings = settings
        self.publisher = publisher
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()
        self._current: dict[str, Binding] = {}
        self._generations: dict[str, int] = defaultdict(int)
        self._events: dict[str, Event] = {}
        self._tasks: set[asyncio.Task] = set()
        self._start_tasks = start_tasks

    async def create(self, spec: EventSpec) -> str:
        if spec.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        event_id = spec.event_id or uuid.uuid4().hex
        created_at = self.now()
        expires_at = created_at.timestamp() + spec.duration_seconds
        async with self._lock:
            if spec.event_id and event_id in self._events:
                raise DuplicateEventId(event_id)
            bindings: dict[str, Binding] = {}
            for prefix in spec.clock_prefixes:
                old = self._current.get(prefix)
                if old and old.event_id in self._events:
                    self._events[old.event_id].states[prefix] = "stale"
                self._generations[prefix] += 1
                binding = Binding(event_id, self._generations[prefix])
                self._current[prefix] = binding
                bindings[prefix] = binding
                if old:
                    self._forget_event_if_unbound_locked(old.event_id)
            event = Event(
                event_id=event_id,
                clock_prefixes=set(spec.clock_prefixes),
                bindings=bindings,
                created_at=created_at,
                expires_at=datetime.fromtimestamp(expires_at, timezone.utc),
                asset=spec.asset,
                sound=spec.sound,
                rtttl=spec.rtttl,
                states={prefix: "active" for prefix in spec.clock_prefixes},
            )
            self._events[event_id] = event
            await self._publish_frame_locked(event)
            for prefix in spec.clock_prefixes:
                if spec.sound:
                    await self.publisher.publish(f"{prefix}/sound", spec.sound)
                if spec.rtttl:
                    await self.publisher.publish(f"{prefix}/rtttl", spec.rtttl)
        if self._start_tasks:
            task = asyncio.create_task(self._run_event(event_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return event_id

    async def render_once(self, event_id: str) -> None:
        async with self._lock:
            event = self._events.get(event_id)
            if event:
                await self._publish_frame_locked(event)

    async def expire_due(self) -> None:
        now = self.now()
        expired = [event_id for event_id, event in self._events.items() if event.expires_at <= now]
        for event_id in expired:
            await self.cancel_event(event_id, final_state="expired")

    async def cancel_current(self, prefixes: tuple[str, ...] | None = None) -> list[str]:
        async with self._lock:
            selected = prefixes or tuple(self._current.keys())
            snapshot = [(prefix, self._current[prefix]) for prefix in selected if prefix in self._current]
        return await self._restore_snapshot(snapshot, final_state="canceled")

    async def cancel_event(self, event_id: str, *, final_state: str = "canceled") -> list[str]:
        async with self._lock:
            event = self._events.get(event_id)
            if not event:
                return []
            snapshot = [
                (prefix, binding)
                for prefix, binding in event.bindings.items()
                if self._current.get(prefix) == binding
            ]
            if not snapshot:
                self._events.pop(event_id, None)
                return []
        return await self._restore_snapshot(snapshot, final_state=final_state)

    async def shutdown(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        async with self._lock:
            snapshot = list(self._current.items())
        await self._restore_snapshot(snapshot, final_state="restored")
        await self.publisher.stop()

    async def _run_event(self, event_id: str) -> None:
        try:
            while True:
                await self.sleep(1)
                await self.expire_due()
                async with self._lock:
                    event = self._events.get(event_id)
                    if not event or not any(self._current.get(p) == b for p, b in event.bindings.items()):
                        return
                    await self._publish_frame_locked(event)
        except asyncio.CancelledError:
            return

    async def _restore_snapshot(self, snapshot: list[tuple[str, Binding]], *, final_state: str) -> list[str]:
        restored: list[str] = []
        async with self._lock:
            touched_event_ids: set[str] = set()
            for prefix, binding in snapshot:
                if self._current.get(prefix) != binding:
                    continue
                await self.publisher.publish(f"{prefix}/custom/{self.settings.app_name}", "")
                self._current.pop(prefix, None)
                restored.append(prefix)
                touched_event_ids.add(binding.event_id)
                event = self._events.get(binding.event_id)
                if event:
                    event.states[prefix] = "restored" if final_state == "restored" else final_state
            for event_id in touched_event_ids:
                self._forget_event_if_unbound_locked(event_id)
        return restored

    async def _publish_frame_locked(self, event: Event) -> None:
        now = self.now()
        frame = event.asset.frame_at(event.frame_index)
        event.frame_index += 1
        payload = build_awtrix_payload(frame, now)
        for prefix, binding in event.bindings.items():
            if self._current.get(prefix) == binding:
                await self.publisher.publish(f"{prefix}/custom/{self.settings.app_name}", payload)

    def snapshot(self) -> dict[str, dict[str, str | int]]:
        return {prefix: {"event_id": binding.event_id, "generation": binding.generation} for prefix, binding in self._current.items()}

    def _forget_event_if_unbound_locked(self, event_id: str) -> None:
        event = self._events.get(event_id)
        if event and not any(self._current.get(prefix) == binding for prefix, binding in event.bindings.items()):
            self._events.pop(event_id, None)


def response_event(event_id: str, clock_prefixes: tuple[str, ...]) -> str:
    return json.dumps({"event_id": event_id, "clock_prefixes": list(clock_prefixes)}, separators=(",", ":"))
