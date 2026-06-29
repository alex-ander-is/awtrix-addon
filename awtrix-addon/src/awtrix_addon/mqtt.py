from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol


class Publisher(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def publish(self, topic: str, payload: str | bytes) -> None: ...


@dataclass
class MemoryPublisher:
    published: list[tuple[str, str | bytes]] = field(default_factory=list)
    started: bool = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def publish(self, topic: str, payload: str | bytes) -> None:
        self.published.append((topic, payload))


class PahoPublisher:
    def __init__(self, host: str, port: int, username: str | None = None, password: str | None = None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._client = None

    async def start(self) -> None:
        def connect():
            import paho.mqtt.client as mqtt

            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if self.username:
                client.username_pw_set(self.username, self.password)
            client.connect(self.host, self.port, keepalive=30)
            client.loop_start()
            self._client = client

        await asyncio.to_thread(connect)

    async def stop(self) -> None:
        if self._client is None:
            return
        client = self._client
        self._client = None
        await asyncio.to_thread(client.loop_stop)
        await asyncio.to_thread(client.disconnect)

    async def publish(self, topic: str, payload: str | bytes) -> None:
        if self._client is None:
            raise RuntimeError("MQTT publisher is not started")
        result = await asyncio.to_thread(self._client.publish, topic, payload, qos=0, retain=False)
        if result.rc != 0:
            raise RuntimeError(f"MQTT publish failed with rc={result.rc}")
