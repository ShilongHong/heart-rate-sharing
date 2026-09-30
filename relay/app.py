import asyncio
import math
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt
from server.app import WEB_DIR

TTL = 180
latest = {}


def prune():
    cutoff = time.time() - TTL
    for key in list(latest):
        if latest[key]["sampled_at"] <= cutoff:
            del latest[key]


async def cleanup():
    while True:
        await asyncio.sleep(5)
        prune()


@asynccontextmanager
async def lifespan(app):
    if not os.getenv("HR_RELAY_TOKEN") or not os.getenv("HR_SERVER_TOKEN"):
        raise RuntimeError("Set HR_RELAY_TOKEN and HR_SERVER_TOKEN")
    latest.clear()
    task = asyncio.create_task(cleanup())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        latest.clear()


app = FastAPI(title="Heart Rate Actions Relay", lifespan=lifespan)


class Sample(BaseModel):
    client_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=32)
    heart_rate: StrictInt = Field(ge=20, le=260)
    sampled_at: float


def authorized(value, key):
    expected = os.getenv(key, "")
    return bool(expected) and secrets.compare_digest(str(value or ""), expected)


def snapshot():
    prune()
    return {"type": "snapshot", "history_enabled": False, "people": [
        {"client_id": key, "name": value["name"], "heart_rate": value["heart_rate"],
         "online": True, "last_seen": datetime.fromtimestamp(value["sampled_at"], timezone.utc).isoformat()}
        for key, value in sorted(latest.items())
    ]}


@app.post("/api/relay")
async def receive(sample: Sample, request: Request):
    if not authorized(request.headers.get("authorization", "").removeprefix("Bearer "), "HR_RELAY_TOKEN"):
        raise HTTPException(401, "invalid token")
    now = time.time()
    if not math.isfinite(sample.sampled_at) or not now - TTL < sample.sampled_at <= now + 10:
        raise HTTPException(422, "expired or invalid sample time")
    prune()
    previous = latest.get(sample.client_id)
    if previous and sample.sampled_at <= previous["sampled_at"]:
        return {"accepted": False, "reason": "older sample"}
    if len(latest) >= 500 and not previous:
        raise HTTPException(429, "relay capacity reached")
    latest[sample.client_id] = sample.model_dump()
    return {"accepted": True}


@app.websocket("/ws/monitor")
async def monitor(ws: WebSocket):
    if not authorized(ws.query_params.get("token"), "HR_SERVER_TOKEN"):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        while True:
            await ws.send_json(snapshot())
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=2)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
