"""Built-in Cloudflare Quick Tunnel for the Windows server.

HeartRateServer.exe bundles cloudflared.exe (Cloudflare's official client, Apache-2.0). With
"cloudflare_tunnel": true it runs `cloudflared tunnel --url http://127.0.0.1:<port>`, which needs no
account or domain and prints a random https://<name>.trycloudflare.com address.

cloudflared runs without a console of its own, so it would outlive a closed server window. It is
put in a Windows Job Object with KILL_ON_JOB_CLOSE: when this process ends for any reason, Windows
closes the job handle and kills cloudflared with it, so no public tunnel is left behind.
"""
from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
READY_MARKER = "Registered tunnel connection"


def find_cloudflared(home_dir: Path) -> Path | None:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys._MEIPASS) / "cloudflared.exe")
    candidates.append(home_dir / "cloudflared.exe")
    on_path = shutil.which("cloudflared")
    if on_path:
        candidates.append(Path(on_path))
    return next((path for path in candidates if path.is_file()), None)


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kill_with_this_process(process: subprocess.Popen) -> object | None:
    """Return the job handle (keep a reference for the process lifetime), or None if unsupported."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    extended_limit_information = 9
    if not kernel32.SetInformationJobObject(job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)):
        return None
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))):
        return None
    return job


class QuickTunnel:
    def __init__(self, executable: Path, port: int, log_path: Path) -> None:
        self.executable = executable
        self.port = port
        self.log_path = log_path
        self.url: str | None = None
        self.url_found = threading.Event()
        self.ready = threading.Event()
        self.exited = threading.Event()
        self.process: subprocess.Popen | None = None
        self._job = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [str(self.executable), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._job = _kill_with_this_process(self.process)
        threading.Thread(target=self._read_output, name="cloudflared-log", daemon=True).start()

    def _read_output(self) -> None:
        with open(self.log_path, "w", encoding="utf-8") as log:
            for raw in self.process.stdout:
                line = raw.decode("utf-8", errors="replace")
                log.write(line)
                log.flush()
                if not self.url:
                    match = URL_PATTERN.search(line)
                    if match:
                        self.url = match.group(0)
                        self.url_found.set()
                if READY_MARKER in line:
                    self.ready.set()
        self.exited.set()
        self.url_found.set()
        self.ready.set()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
