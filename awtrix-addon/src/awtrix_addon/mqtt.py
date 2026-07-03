from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from threading import Event
from typing import Any, Callable, Protocol


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
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        client_factory: Callable[[], Any] | None = None,
        connect_timeout: float = 10,
    ):
        self.host = host
        self.port = port
        self._username = username
        self._password = password
        self._client_factory = client_factory
        self._connect_timeout = connect_timeout
        self._client = None

    async def start(self) -> None:
        await asyncio.to_thread(self._connect)

    def _connect(self) -> None:
        client = None
        connack_received = Event()
        connack_accepted = False

        def on_connect(_client, _userdata, _flags, reason_code, _properties=None) -> None:
            nonlocal connack_accepted
            connack_accepted = _is_accepted_connack(reason_code)
            connack_received.set()

        try:
            client = self._new_client()
            client.on_connect = on_connect
            client.username_pw_set(self._username, self._password)
            connect_result = client.connect(self.host, self.port, keepalive=30)
            if connect_result not in (None, 0):
                raise RuntimeError("MQTT connect failed")
            client.loop_start()
            if not connack_received.wait(self._connect_timeout):
                raise RuntimeError("MQTT CONNACK timed out")
            if not connack_accepted:
                raise RuntimeError("MQTT CONNACK rejected")
        except Exception:
            if client is not None:
                self._cleanup_unready_client(client)
            raise
        finally:
            self._username = None
            self._password = None
        self._client = client

    def _new_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("paho-mqtt is required for MQTT publishing") from exc
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    @staticmethod
    def _cleanup_unready_client(client: Any) -> None:
        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass

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


def _is_accepted_connack(reason_code: Any) -> bool:
    is_failure = getattr(reason_code, "is_failure", None)
    if is_failure is not None:
        return not bool(is_failure)
    value = getattr(reason_code, "value", reason_code)
    return value == 0
