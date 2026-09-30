"""Desktop collector: a pywebview (Edge WebView2) window around client/ui/.

The page is served from a loopback-only HTTP server so it can load both the desktop UI
(/ui/) and the shared dashboard files (/web/app.js, /web/styles.css). JS calls into
`Api` via window.pywebview.api; Python pushes events back with evaluate_js.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import webview

if __package__:
    from .core import (
        LOG_PATH, Collector, fetch_history, load_config, logger, save_config, scan_devices, setup_logging,
    )
else:
    from core import (
        LOG_PATH, Collector, fetch_history, load_config, logger, save_config, scan_devices, setup_logging,
    )


if getattr(sys, "frozen", False):
    RESOURCE_ROOT = Path(sys._MEIPASS)
    UI_DIR, WEB_DIR = RESOURCE_ROOT / "ui", RESOURCE_ROOT / "web"
else:
    UI_DIR = Path(__file__).resolve().parent / "ui"
    WEB_DIR = Path(__file__).resolve().parent.parent / "web"

WINDOW_TITLE = "心率实时共享"
WEBVIEW_DATA_DIR = Path.home() / ".heart-rate-sharing-webview"

SETTING_KEYS = (
    "mode", "server_url", "name", "token", "device_address", "device_name",
    "github_repo", "github_token", "dashboard_url",
)


class ResourceHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        for prefix, directory in (("/ui/", UI_DIR), ("/web/", WEB_DIR)):
            if path.startswith(prefix):
                target = (directory / path[len(prefix):]).resolve()
                if target.is_relative_to(directory.resolve()):
                    return str(target)
        return str(UI_DIR / "__not_found__")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *_args) -> None:
        pass


def start_resource_server() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(ResourceHandler, directory=str(UI_DIR)))
    threading.Thread(target=server.serve_forever, name="ui-server", daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/ui/index.html"


class Api:
    """Exposed to JS as window.pywebview.api. Underscored attributes stay private."""

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._collector: Collector | None = None
        self._config = load_config()
        if not self._config.get("client_id"):
            self._config["client_id"] = str(uuid.uuid4())
            save_config(self._config)

    def _emit(self, function: str, *args) -> None:
        if self._window:
            arguments = ", ".join(json.dumps(value, ensure_ascii=False) for value in args)
            self._window.evaluate_js(f"window.desktop && window.desktop.{function}({arguments})")

    def get_state(self) -> dict:
        return {
            "config": {key: self._config.get(key, "") for key in SETTING_KEYS},
            "client_id": self._config["client_id"],
            "collecting": bool(self._collector and self._collector.is_alive()),
        }

    def save_settings(self, settings: dict) -> None:
        self._config.update({key: str(settings.get(key, "")).strip() for key in SETTING_KEYS if key in settings})
        save_config(self._config)

    def scan(self) -> list[dict]:
        if self._collector and self._collector.is_alive():
            raise RuntimeError("采集中无法扫描，请先停止")
        logger.info("开始扫描 BLE 设备")
        try:
            return scan_devices()
        except TimeoutError as exc:
            logger.exception("扫描 BLE 设备超时")
            raise RuntimeError("扫描超时：蓝牙没有响应，请确认电脑蓝牙已打开后重试") from exc
        except BaseException as exc:
            # BaseException on purpose: asyncio.CancelledError (raised when the Windows BLE watcher is
            # aborted) is not an Exception, slips past pywebview's handler and leaves the JS promise
            # pending forever — the scan button then spins indefinitely.
            logger.exception("扫描 BLE 设备失败")
            raise RuntimeError(f"扫描失败：{exc!r}（请确认电脑蓝牙已打开，且没有其他程序正在使用蓝牙）") from exc

    def start(self, settings: dict) -> dict:
        if self._collector and self._collector.is_alive():
            return {"ok": False, "error": "已经在采集中"}
        self.save_settings(settings)
        config = self._config
        mode = config.get("mode") or "backend"
        target = config.get("github_repo") if mode == "action" else config.get("server_url")
        if not target:
            return {"ok": False, "error": "请填写 GitHub 仓库（owner/repo）" if mode == "action" else "请填写服务端地址"}
        if not config.get("name"):
            return {"ok": False, "error": "请填写显示名字"}
        if not config.get("device_address"):
            return {"ok": False, "error": "请先点击“扫描”并选择心率设备"}
        self._collector = Collector(
            mode=mode,
            server_url=target,
            name=config["name"],
            token=config.get("token", ""),
            device_address=config["device_address"],
            client_id=config["client_id"],
            github_token=config.get("github_token", ""),
            on_status=lambda text, state: self._emit("onStatus", text, state),
            on_heart_rate=lambda bpm: self._emit("onHeartRate", bpm),
            on_stopped=lambda: self._emit("onStopped"),
        )
        self._collector.start()
        return {"ok": True}

    def stop(self) -> None:
        if self._collector:
            self._collector.stop()

    def fetch_history(self, server_url: str, client_id: str, minutes: int, token: str) -> dict:
        # Done in Python rather than JS fetch(): the server sends no CORS headers.
        return fetch_history(server_url, client_id, minutes, token)

    def open_log_folder(self) -> None:
        os.startfile(LOG_PATH.parent)


def acquire_single_instance() -> object | None:
    """Return a mutex handle, or None if another instance already owns it (that window is raised).

    Two instances would upload under the same client_id, and a second WebView2 started with
    different browser arguments on the same data folder fails to initialise (blank window).
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32")
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\HeartRateCollector")
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return handle
    existing = user32.FindWindowW(None, WINDOW_TITLE)
    if existing:
        user32.ShowWindow(existing, 9)  # SW_RESTORE
        user32.SetForegroundWindow(existing)
    return None


def main() -> None:
    setup_logging()
    if sys.platform == "win32":
        instance_lock = acquire_single_instance()
        if instance_lock is None:
            logger.info("已有采集端在运行，切换到已打开的窗口")
            return
    api = Api()
    window = webview.create_window(
        WINDOW_TITLE, start_resource_server(), js_api=api,
        width=1240, height=820, min_size=(960, 640), background_color="#EEF0F4",
    )
    api._window = window
    window.events.closing += api.stop
    data_dir = WEBVIEW_DATA_DIR
    if os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"):
        # WebView2 can't share a data folder between browsers launched with different arguments.
        data_dir = data_dir.with_name(data_dir.name + "-custom-args")
    webview.start(private_mode=False, storage_path=str(data_dir))


if __name__ == "__main__":
    main()
