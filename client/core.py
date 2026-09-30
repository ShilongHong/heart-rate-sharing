"""BLE collection and upload, independent of the UI.

The UI talks to this module only through `Collector` callbacks and `scan_devices()`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import websockets
from bleak import BleakClient, BleakScanner

if __package__:
    from .action_transport import ActionTransport
else:
    from action_transport import ActionTransport


HEART_RATE_SERVICE_UUID = "0000180D-0000-1000-8000-00805F9B34FB"
HEART_RATE_MEASUREMENT_UUID = "00002A37-0000-1000-8000-00805F9B34FB"
CONFIG_PATH = Path.home() / ".heart-rate-sharing.json"
LOG_PATH = Path.home() / ".heart-rate-sharing.log"

logger = logging.getLogger("heart_rate")


def setup_logging() -> None:
    try:
        handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(values: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def http_base(server_url: str) -> str:
    """Normalise a user-entered server address to http(s)://host[:port][/prefix]."""
    value = server_url.strip().rstrip("/")
    if value.startswith("ws://"):
        value = "http://" + value[len("ws://"):]
    elif value.startswith("wss://"):
        value = "https://" + value[len("wss://"):]
    elif not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value


def websocket_base(server_url: str) -> str:
    parts = urlsplit(http_base(server_url))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path, "", ""))


def websocket_endpoint(server_url: str) -> str:
    return websocket_base(server_url) + "/ws/ingest"


def monitor_endpoint(server_url: str, token: str) -> str:
    return f"{websocket_base(server_url)}/ws/monitor?token={quote(token)}"


def fetch_history(server_url: str, client_id: str, minutes: int, token: str) -> dict:
    url = f"{http_base(server_url)}/api/people/{quote(client_id, safe='')}/history?minutes={int(minutes)}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urlopen(Request(url, headers=headers), timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def decode_heart_rate(data: bytearray) -> int | None:
    if len(data) < 2:
        return None
    flags = data[0]
    if flags & 0x01:
        if len(data) < 3:
            return None
        return int.from_bytes(data[1:3], byteorder="little")
    return data[1]


async def _scan() -> list[dict]:
    found = await BleakScanner.discover(timeout=6, return_adv=True)
    devices = []
    for device, advertisement in found.values():
        is_heart_rate = HEART_RATE_SERVICE_UUID.lower() in [
            service.lower() for service in advertisement.service_uuids
        ]
        name = advertisement.local_name or device.name
        # Unnamed non-HR devices are just noise (phones, beacons, TVs).
        if not is_heart_rate and not name:
            continue
        devices.append({
            "address": device.address,
            "name": name or "未命名设备",
            "rssi": advertisement.rssi,
            "heart_rate": is_heart_rate,
        })
    devices.sort(key=lambda item: (not item["heart_rate"], -item["rssi"]))
    logger.info("扫描到 %d 个设备", len(devices))
    return devices


def scan_devices() -> list[dict]:
    """Blocking ~6s BLE scan (hard limit 20s). Heart-rate devices first, then by signal strength."""
    return asyncio.run(asyncio.wait_for(_scan(), timeout=20))


class WebSocketTransport:
    """Adapts the ingest websocket to the transport interface: send(dict)."""

    def __init__(self, websocket) -> None:
        self.websocket = websocket

    async def send(self, message: dict) -> None:
        await self.websocket.send(json.dumps(message, ensure_ascii=False))


class Collector:
    """Runs BLE → upload on a background thread with its own asyncio loop.

    on_status(text, state) — state is one of: idle, working, live, error
    on_heart_rate(bpm)
    on_stopped()
    """

    def __init__(
        self,
        *,
        mode: str,
        server_url: str,
        name: str,
        token: str,
        device_address: str,
        client_id: str,
        github_token: str,
        on_status: Callable[[str, str], None],
        on_heart_rate: Callable[[int], None],
        on_stopped: Callable[[], None],
    ) -> None:
        self.mode = mode
        self.server_url = server_url
        self.person_name = name
        self.token = token
        self.device_address = device_address.strip()
        self.client_id = client_id
        self.github_token = github_token
        self.on_status = on_status
        self.on_heart_rate = on_heart_rate
        self.on_stopped = on_stopped
        self.running = False
        self.thread: threading.Thread | None = None

    def status(self, text: str, state: str = "working") -> None:
        logger.info(text)
        self.on_status(text, state)

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self.run, name="collector", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.running = False

    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def run(self) -> None:
        try:
            asyncio.run(self.upload_loop())
        except Exception as exc:
            logger.exception("采集线程异常退出")
            self.status(str(exc), "error")
        finally:
            self.status("已停止", "idle")
            self.on_stopped()

    async def upload_loop(self) -> None:
        if self.mode == "action":
            transport = ActionTransport(
                self.server_url, self.client_id, self.person_name, self.status, self.github_token
            )
            while self.running:
                try:
                    await self.receive_from_bluetooth(transport)
                except Exception as exc:
                    logger.exception("Action 模式采集出错")
                    if self.running:
                        self.status(f"采集断开：{exc}", "error")
                        await self.sleep_while_running(4)
            return
        endpoint = websocket_endpoint(self.server_url)
        while self.running:
            try:
                self.status("正在连接服务端…")
                async with websockets.connect(endpoint, ping_interval=20, ping_timeout=20) as websocket:
                    await websocket.send(json.dumps({
                        "type": "register",
                        "client_id": self.client_id,
                        "name": self.person_name,
                        "token": self.token,
                    }, ensure_ascii=False))
                    registration_result = json.loads(await websocket.recv())
                    if registration_result.get("type") != "registered":
                        raise RuntimeError(registration_result.get("message", "服务端注册失败"))
                    self.status("已连接服务端，正在连接心率设备…")
                    await self.receive_from_bluetooth(WebSocketTransport(websocket))
            except Exception as exc:
                logger.exception("完整版连接出错")
                if self.running:
                    self.status(f"连接中断：{exc}，4 秒后重试", "error")
                    await self.sleep_while_running(4)

    async def receive_from_bluetooth(self, transport) -> None:
        self.status("正在查找所选设备…")
        device = await BleakScanner.find_device_by_address(self.device_address, timeout=10)
        if not device:
            raise RuntimeError("未找到所选设备，请确认设备已开启且没有被手机等其他应用连接")

        self.status(f"已找到设备：{device.name or device.address}")
        async with BleakClient(device) as ble_client:
            self.status("正在上传心率", "live")
            loop = asyncio.get_running_loop()

            def on_heart_rate(_sender, data: bytearray) -> None:
                heart_rate = decode_heart_rate(data)
                if heart_rate is not None:
                    self.on_heart_rate(heart_rate)
                    asyncio.run_coroutine_threadsafe(
                        transport.send({"type": "heart_rate", "heart_rate": heart_rate}),
                        loop,
                    )

            await ble_client.start_notify(HEART_RATE_MEASUREMENT_UUID, on_heart_rate)
            try:
                while self.running and ble_client.is_connected:
                    await transport.send({"type": "heartbeat"})
                    await self.sleep_while_running(5)
            finally:
                try:
                    await ble_client.stop_notify(HEART_RATE_MEASUREMENT_UUID)
                except Exception:
                    pass
            if self.running:
                raise RuntimeError("心率设备连接已断开")

    async def sleep_while_running(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while self.running and time.monotonic() < end:
            await asyncio.sleep(0.2)
