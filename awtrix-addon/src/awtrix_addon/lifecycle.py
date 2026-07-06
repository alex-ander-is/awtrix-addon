from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .mqtt import Publisher
from .palette import DEFAULT_PALETTE, PaletteStore
from .renderer import AssetAnimation, build_awtrix_payload
from .settings import Settings


Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


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
    duration_seconds: int
    asset: AssetAnimation
    rtttl: str | None = None
    weekdays: bool = True
    frame_index: int = 0
    states: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EventSpec:
    event_id: str | None
    clock_prefixes: tuple[str, ...]
    duration_seconds: int
    asset: AssetAnimation
    rtttl: str | None = None
    weekdays: bool = True


class EventStore:
    def __init__(
        self,
        settings: Settings,
        publisher: Publisher,
        *,
        now: Clock | None = None,
        sleep: Sleeper | None = None,
        palette_store: PaletteStore | None = None,
        start_tasks: bool = True,
    ):
        self.settings = settings
        self.publisher = publisher
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or asyncio.sleep
        self.palette_store = palette_store
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
        created_at = self._now_utc()
        expires_at = created_at.timestamp() + spec.duration_seconds
        async with self._lock:
            snapshot = self._snapshot_locked()
            previous_event_ids: set[str] = set()
            try:
                replaced = self._events.pop(event_id, None) if spec.event_id else None
                if replaced:
                    for prefix, binding in replaced.bindings.items():
                        if self._current.get(prefix) != binding:
                            continue
                        replaced.states[prefix] = "superseded"
                        self._generations[prefix] += 1
                        if prefix not in spec.clock_prefixes:
                            await self.publisher.publish(f"{prefix}/custom/{self.settings.app_name}", "")
                            self._current.pop(prefix)
                bindings: dict[str, Binding] = {}
                for prefix in spec.clock_prefixes:
                    old = self._current.get(prefix)
                    if old and old.event_id in self._events:
                        self._events[old.event_id].states[prefix] = "stale"
                        previous_event_ids.add(old.event_id)
                    self._generations[prefix] += 1
                    binding = Binding(event_id, self._generations[prefix])
                    self._current[prefix] = binding
                    bindings[prefix] = binding
                event = Event(
                    event_id=event_id,
                    clock_prefixes=set(spec.clock_prefixes),
                    bindings=bindings,
                    created_at=created_at,
                    expires_at=datetime.fromtimestamp(expires_at, timezone.utc),
                    duration_seconds=spec.duration_seconds,
                    asset=spec.asset,
                    rtttl=spec.rtttl,
                    weekdays=spec.weekdays,
                    states={prefix: "active" for prefix in spec.clock_prefixes},
                )
                self._events[event_id] = event
                await self._publish_frame_locked(event)
                await self._switch_to_event_locked(event)
                for prefix in spec.clock_prefixes:
                    if spec.rtttl:
                        await self.publisher.publish(f"{prefix}/rtttl", spec.rtttl)
            except Exception:
                self._restore_locked(snapshot)
                raise
            for previous_event_id in previous_event_ids:
                self._forget_event_if_unbound_locked(previous_event_id)
        if self._start_tasks:
            task = asyncio.create_task(self._run_event(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return event_id

    async def render_once(self, event_id: str) -> None:
        async with self._lock:
            event = self._events.get(event_id)
            if event:
                await self._publish_frame_locked(event)

    async def expire_due(self) -> None:
        now = self._now_utc()
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

    async def _run_event(self, event: Event) -> None:
        try:
            while True:
                await self.sleep(1)
                async with self._lock:
                    if self._events.get(event.event_id) is not event:
                        return
                    if not any(self._current.get(p) == b for p, b in event.bindings.items()):
                        return
                    if event.expires_at <= self._now_utc():
                        snapshot = [
                            (prefix, binding)
                            for prefix, binding in event.bindings.items()
                            if self._current.get(prefix) == binding
                        ]
                    else:
                        snapshot = []
                        await self._publish_frame_locked(event)
                if snapshot:
                    await self._restore_snapshot(snapshot, final_state="expired")
                    return
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
        now = self._now_utc()
        render_now = now.astimezone()
        frame = event.asset.frame_at(event.frame_index)
        event.frame_index += 1
        for prefix, binding in event.bindings.items():
            if self._current.get(prefix) == binding:
                palette = self.palette_store.snapshot(prefix) if self.palette_store else DEFAULT_PALETTE
                payload = build_awtrix_payload(
                    frame,
                    render_now,
                    weekdays=event.weekdays,
                    palette=palette,
                    duration=event.duration_seconds,
                )
                await self.publisher.publish(f"{prefix}/custom/{self.settings.app_name}", payload)

    async def _switch_to_event_locked(self, event: Event) -> None:
        """Make the just-created custom page visible without changing AWTRIX settings."""
        payload = json.dumps({"name": self.settings.app_name, "fast": True}, separators=(",", ":"))
        for prefix, binding in event.bindings.items():
            if self._current.get(prefix) == binding:
                await self.publisher.publish(f"{prefix}/switch", payload)

    def snapshot(self) -> dict[str, dict[str, str | int]]:
        return {prefix: {"event_id": binding.event_id, "generation": binding.generation} for prefix, binding in self._current.items()}

    def _forget_event_if_unbound_locked(self, event_id: str) -> None:
        event = self._events.get(event_id)
        if event and not any(self._current.get(prefix) == binding for prefix, binding in event.bindings.items()):
            self._events.pop(event_id, None)

    def _snapshot_locked(self) -> tuple[
        dict[str, Binding], dict[str, int], dict[str, tuple[Event, dict[str, Binding], dict[str, str], int]]
    ]:
        return (
            dict(self._current),
            dict(self._generations),
            {
                event_id: (event, dict(event.bindings), dict(event.states), event.frame_index)
                for event_id, event in self._events.items()
            },
        )

    def _restore_locked(
        self,
        snapshot: tuple[
            dict[str, Binding], dict[str, int], dict[str, tuple[Event, dict[str, Binding], dict[str, str], int]]
        ],
    ) -> None:
        current, generations, events = snapshot
        self._current = current
        self._generations = defaultdict(int, generations)
        self._events = {}
        for event_id, (event, bindings, states, frame_index) in events.items():
            event.bindings = bindings
            event.states = states
            event.frame_index = frame_index
            self._events[event_id] = event

    def _now_utc(self) -> datetime:
        current = self.now()
        if current.tzinfo is None or current.utcoffset() is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)


def response_event(event_id: str, clock_prefixes: tuple[str, ...]) -> str:
    return json.dumps({"event_id": event_id, "clock_prefixes": list(clock_prefixes)}, separators=(",", ":"))
