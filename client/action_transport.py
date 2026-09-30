"""Dispatch latest samples using the operator's own GitHub credential."""
import json
import os
import re
import time
import asyncio
from urllib.request import Request, urlopen


class ActionTransport:
    def __init__(self, repository, client_id, name, status, token=""):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Action 地址应为 owner/repository")
        self.token = token or os.getenv("HR_GITHUB_TOKEN", "")
        if not self.token:
            raise ValueError("请填写 GitHub 令牌（或设置环境变量 HR_GITHUB_TOKEN）")
        self.url = f"https://api.github.com/repos/{repository}/dispatches"
        self.client_id, self.name, self.status = client_id, name, status
        self.next_send = 0
        self.busy = False

    async def send(self, message):
        data = json.loads(message) if isinstance(message, str) else message
        if data.get("type") != "heart_rate" or self.busy or time.monotonic() < self.next_send:
            return
        self.busy = True
        self.next_send = time.monotonic() + 60
        payload = {"event_type": "heart_rate", "client_payload": {
            "client_id": self.client_id, "name": self.name,
            "heart_rate": data["heart_rate"], "sampled_at": time.time()}}
        try:
            await asyncio.to_thread(self.dispatch, payload)
            self.status("已提交 Action，等待排队转发（每 60 秒最多一次）")
        except Exception:
            self.status("Action 提交失败，请检查网络、仓库和个人令牌；60 秒后重试")
        finally:
            self.busy = False

    def dispatch(self, payload):
        request = Request(self.url, data=json.dumps(payload).encode(), method="POST", headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
        with urlopen(request, timeout=15) as response:
            if response.status != 204:
                raise RuntimeError("dispatch rejected")
