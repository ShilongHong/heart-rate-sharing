"""Windows server launcher: double-click to run the full server (packaged as HeartRateServer.exe).

Settings live in heart-rate-server.json next to the exe (created on first run) and the SQLite
database in data/ next to it. This must set HR_DB_PATH before importing server.app: in a one-file
build the package sits in a temporary folder that is deleted on exit, and the default database
path would be inside it.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

if getattr(sys, "frozen", False):
    HOME_DIR = Path(sys.executable).resolve().parent
else:
    HOME_DIR = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(HOME_DIR))

from server.tunnel import QuickTunnel, find_cloudflared

CONFIG_PATH = HOME_DIR / "heart-rate-server.json"
CLOUDFLARED_LOG = HOME_DIR / "cloudflared.log"
DEFAULT_CONFIG = {"port": 8000, "token": "", "open_browser": True, "cloudflare_tunnel": False}


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        values = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"配置文件格式有误：{CONFIG_PATH}\n{exc}\n修正后重新打开；也可以删除它，下次启动会重新生成默认配置。")
    config = {**DEFAULT_CONFIG, **values}
    if config.keys() != values.keys():
        save_config(config)  # add options introduced by newer versions so they are visible to edit
    return config


def lan_addresses() -> list[str]:
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    try:
        # The address the OS would use for outbound traffic; no packet is actually sent.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    # 127.x loopback, 169.254.x link-local, 198.18/15 = virtual NICs of proxy TUN modes (Clash etc.).
    hidden = ("127.", "169.254.", "198.18.", "198.19.")
    return sorted(address for address in addresses if not address.startswith(hidden))


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test:
        try:
            test.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


LINE = "=" * 64


def print_banner(port: int, token: str, db_path: Path, tunnel_enabled: bool) -> None:
    print(LINE)
    print("  心率实时共享 · 服务端已启动")
    print(LINE)
    print(f"  本机看板：    http://127.0.0.1:{port}/")
    for address in lan_addresses():
        print(f"  局域网地址：  http://{address}:{port}/")
    print(f"  令牌：        {token or '未设置'}")
    print()
    print("  同一局域网的人：采集端“服务端地址”填上面的局域网地址。")
    if tunnel_enabled:
        print("  外网的人：正在创建 Cloudflare 隧道，外网地址稍后显示在下方…")
    else:
        print("  外网的人：把配置文件里的 \"cloudflare_tunnel\" 改成 true 后重新打开，")
        print("            即可自动获得一个外网地址；也可以用其他内网穿透工具（见 docs/tunnel.md）。")
    print()
    print(f"  配置文件：    {CONFIG_PATH}")
    print(f"  心率数据库：  {db_path}")
    print(LINE)
    print("  关闭这个窗口即停止服务（外网隧道也会一起关闭）。")
    print()


def report_tunnel(tunnel: QuickTunnel, token: str) -> None:
    """Runs in the background after the banner: print the public address once Cloudflare assigns it."""
    tunnel.url_found.wait(timeout=45)
    if tunnel.url:
        tunnel.ready.wait(timeout=30)
    print(LINE)
    if tunnel.url and tunnel.ready.is_set() and not tunnel.exited.is_set():
        print("  外网地址（Cloudflare 隧道）已就绪：")
        print(f"      {tunnel.url}")
        print(f"  令牌：{token}")
        print()
        print("  把地址和令牌发给外地的队友：采集端“服务端地址”填这个地址，网页和 OBS 也打开它。")
        print("  注意：这个地址每次启动服务端都会变；它是 Cloudflare 的免费测试隧道，不保证稳定。")
    elif tunnel.url:
        print(f"  已分配外网地址 {tunnel.url}，但隧道还没连上 Cloudflare，可能暂时无法访问。")
        print(f"  如果一直打不开，请查看 {CLOUDFLARED_LOG}")
    else:
        print("  Cloudflare 隧道创建失败，外网暂时无法访问（局域网不受影响）。")
        print("  常见原因：网络无法连接 Cloudflare，或被防火墙/代理拦截。")
        print(f"  详细信息见 {CLOUDFLARED_LOG}")
    print(LINE)
    print()


def main() -> None:
    config = load_config()
    port = int(config["port"])
    tunnel_enabled = bool(config.get("cloudflare_tunnel"))
    token = str(os.environ.get("HR_SERVER_TOKEN") or config.get("token") or "")
    if tunnel_enabled and not token:
        # A public address without a token would let anyone read and write heart rates.
        token = secrets.token_urlsafe(9)
        config["token"] = token
        save_config(config)
    db_path = Path(os.environ.get("HR_DB_PATH") or HOME_DIR / "data" / "heart_rate.db")
    os.environ["HR_SERVER_TOKEN"] = token
    os.environ["HR_DB_PATH"] = str(db_path)

    if not port_available(port):
        raise SystemExit(
            f"端口 {port} 已被占用：可能已经开着一个服务端，或其他程序占用了这个端口。\n"
            f"关闭占用的程序，或修改 {CONFIG_PATH} 里的 \"port\" 后重新打开。"
        )

    tunnel = None
    if tunnel_enabled:
        executable = find_cloudflared(HOME_DIR)
        if executable is None:
            raise SystemExit(
                "已开启 cloudflare_tunnel，但找不到 cloudflared.exe。\n"
                "请使用 Releases 里的 HeartRateServer.exe（已内置），或把 cloudflared.exe 放在服务端旁边。"
            )
        tunnel = QuickTunnel(executable, port, CLOUDFLARED_LOG)
        tunnel.start()

    import uvicorn
    from server.app import app

    print_banner(port, token, db_path, tunnel_enabled)
    if tunnel:
        threading.Thread(target=report_tunnel, args=(tunnel, token), name="tunnel-report", daemon=True).start()
    if config.get("open_browser"):
        threading.Thread(
            target=lambda: (time.sleep(1.5), webbrowser.open(f"http://127.0.0.1:{port}/")),
            daemon=True,
        ).start()
    try:
        # Access logs would print a line per request/WebSocket; keep the console readable.
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", access_log=False)
    finally:
        if tunnel:
            tunnel.stop()


def pause_before_exit(prompt: str) -> None:
    """Keep a double-clicked console window open so the message can be read."""
    if getattr(sys, "frozen", False):
        try:
            input(prompt)
        except EOFError:
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code in (None, 0):
            raise
        print(exc.code if isinstance(exc.code, str) else f"退出代码 {exc.code}")
        pause_before_exit("\n按回车键关闭窗口…")
        sys.exit(1)
    except Exception:
        import traceback

        traceback.print_exc()
        pause_before_exit("\n服务端出错，按回车键关闭窗口…")
        raise
