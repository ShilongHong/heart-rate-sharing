from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
SERVER_TOKEN = os.getenv("HR_SERVER_TOKEN", "")
STALE_AFTER_SECONDS = 20
DB_PATH = Path(os.getenv("HR_DB_PATH", str(BASE_DIR / "data" / "heart_rate.db")))


@dataclass
class PersonState:
    client_id: str
    name: str
    heart_rate: int | None = None
    online: bool = True
    last_seen: str = ""
    _last_seen_epoch: float = 0.0
    _connection: WebSocket | None = None

    def public(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "name": self.name,
            "heart_rate": self.heart_rate,
            "online": self.online,
            "last_seen": self.last_seen,
        }


app = FastAPI(title="Heart Rate Realtime Sharing", version="0.1.0")
people: dict[str, PersonState] = {}
monitor_connections: set[WebSocket] = set()
state_lock = asyncio.Lock()


@contextmanager
def database_connection() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    with database_connection() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS people (
                client_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS heart_rate_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                name TEXT NOT NULL,
                heart_rate INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                recorded_epoch REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_heart_rate_samples_client_time
                ON heart_rate_samples (client_id, recorded_epoch);
            """
        )


def load_persisted_people() -> None:
    people.clear()
    with database_connection() as connection:
        rows = connection.execute(
            "SELECT client_id, name, updated_at FROM people ORDER BY name COLLATE NOCASE"
        ).fetchall()
    for row in rows:
        try:
            epoch = datetime.fromisoformat(row["updated_at"]).timestamp()
        except (TypeError, ValueError, OverflowError):
            epoch = 0.0
        people[row["client_id"]] = PersonState(
            client_id=row["client_id"],
            name=row["name"],
            online=False,
            last_seen=row["updated_at"],
            _last_seen_epoch=epoch,
        )


def persist_registration(client_id: str, name: str, timestamp: str) -> None:
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO people (client_id, name, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
            """,
            (client_id, name, timestamp),
        )


def persist_sample(client_id: str, name: str, heart_rate: int, timestamp: str, epoch: float) -> None:
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO heart_rate_samples
                (client_id, name, heart_rate, recorded_at, recorded_epoch)
            VALUES (?, ?, ?, ?, ?)
            """,
            (client_id, name, heart_rate, timestamp, epoch),
        )
        connection.execute(
            """
            INSERT INTO people (client_id, name, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
            """,
            (client_id, name, timestamp),
        )


def read_history(client_id: str, cutoff_epoch: float) -> list[sqlite3.Row]:
    with database_connection() as connection:
        return connection.execute(
            """
            SELECT name, heart_rate, recorded_at
            FROM heart_rate_samples
            WHERE client_id = ? AND recorded_epoch >= ?
            ORDER BY recorded_epoch ASC
            """,
            (client_id, cutoff_epoch),
        ).fetchall()


def read_persisted_name(client_id: str) -> str | None:
    with database_connection() as connection:
        row = connection.execute(
            "SELECT name FROM people WHERE client_id = ?", (client_id,)
        ).fetchone()
    return row["name"] if row else None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_token(candidate: str | None) -> bool:
    return not SERVER_TOKEN or candidate == SERVER_TOKEN


def request_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.query_params.get("token")


def require_http_auth(request: Request) -> None:
    if not valid_token(request_token(request)):
        raise HTTPException(status_code=401, detail="invalid token")


def parse_client_id(value: Any) -> str:
    client_id = str(value or "").strip()
    if not client_id or len(client_id) > 100:
        raise ValueError("client_id is required and must be at most 100 characters")
    return client_id


def parse_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 32:
        raise ValueError("name is required and must be at most 32 characters")
    return name


def parse_heart_rate(value: Any) -> int:
    heart_rate = int(value)
    if not 20 <= heart_rate <= 260:
        raise ValueError("heart_rate must be between 20 and 260")
    return heart_rate


async def snapshot() -> dict[str, Any]:
    async with state_lock:
        current_people = sorted(
            (person.public() for person in people.values()),
            key=lambda item: (not item["online"], item["name"].casefold()),
        )
    return {"type": "snapshot", "people": current_people}


async def broadcast_snapshot() -> None:
    if not monitor_connections:
        return
    message = await snapshot()
    disconnected: list[WebSocket] = []
    for connection in tuple(monitor_connections):
        try:
            await connection.send_json(message)
        except Exception:
            disconnected.append(connection)
    for connection in disconnected:
        monitor_connections.discard(connection)


async def cleanup_stale_people() -> None:
    while True:
        await asyncio.sleep(5)
        changed = False
        async with state_lock:
            current_epoch = time.time()
            for client_id, person in people.items():
                is_online = (
                    person._connection is not None
                    and current_epoch - person._last_seen_epoch <= STALE_AFTER_SECONDS
                )
                if person.online != is_online:
                    person.online = is_online
                    changed = True
        if changed:
            await broadcast_snapshot()


@app.on_event("startup")
async def startup_event() -> None:
    initialize_database()
    load_persisted_people()
    app.state.cleanup_task = asyncio.create_task(cleanup_stale_people())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    task = getattr(app.state, "cleanup_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/people")
async def people_api(request: Request) -> dict[str, Any]:
    """Small read-only endpoint useful for diagnostics and future clients."""
    require_http_auth(request)
    return await snapshot()


@app.get("/api/people/{client_id}/history")
async def person_history(
    client_id: str,
    request: Request,
    minutes: int = Query(5),
) -> dict[str, Any]:
    require_http_auth(request)
    if minutes not in {5, 15, 30}:
        raise HTTPException(status_code=400, detail="minutes must be one of 5, 15, or 30")

    rows = read_history(client_id, time.time() - minutes * 60)
    person = people.get(client_id)
    name = person.name if person else (rows[-1]["name"] if rows else read_persisted_name(client_id))
    return {
        "client_id": client_id,
        "name": name or client_id,
        "minutes": minutes,
        "samples": [
            {"heart_rate": row["heart_rate"], "timestamp": row["recorded_at"]}
            for row in rows
        ],
    }


@app.websocket("/ws/monitor")
async def monitor_socket(websocket: WebSocket) -> None:
    if not valid_token(websocket.query_params.get("token")):
        await websocket.close(code=1008, reason="invalid token")
        return

    await websocket.accept()
    monitor_connections.add(websocket)
    await websocket.send_json(await snapshot())
    try:
        while True:
            # Browsers do not need to send application data. Keeping a receive
            # loop lets disconnects be noticed without a separate ping protocol.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        monitor_connections.discard(websocket)


@app.websocket("/ws/ingest")
async def ingest_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    person: PersonState | None = None
    try:
        registration = await websocket.receive_json()
        if registration.get("type") != "register":
            await websocket.close(code=1008, reason="first message must register")
            return
        if not valid_token(registration.get("token")):
            await websocket.close(code=1008, reason="invalid token")
            return

        client_id = parse_client_id(registration.get("client_id"))
        name = parse_name(registration.get("name"))
        timestamp = now_iso()
        async with state_lock:
            person = people.get(client_id) or PersonState(client_id=client_id, name=name)
            person.name = name
            person.online = True
            person.last_seen = timestamp
            person._last_seen_epoch = time.time()
            person._connection = websocket
            people[client_id] = person
        persist_registration(client_id, name, timestamp)
        await websocket.send_json({"type": "registered", "client_id": client_id})
        await broadcast_snapshot()

        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")
            if message_type == "heart_rate":
                heart_rate = parse_heart_rate(message.get("heart_rate"))
                async with state_lock:
                    person.heart_rate = heart_rate
                    person.online = True
                    person.last_seen = now_iso()
                    person._last_seen_epoch = time.time()
                    timestamp = person.last_seen
                    epoch = person._last_seen_epoch
                    person_name = person.name
                persist_sample(client_id, person_name, heart_rate, timestamp, epoch)
                await broadcast_snapshot()
            elif message_type == "heartbeat":
                async with state_lock:
                    person.online = True
                    person.last_seen = now_iso()
                    person._last_seen_epoch = time.time()
            else:
                await websocket.send_json({"type": "error", "message": "unknown message type"})
    except WebSocketDisconnect:
        pass
    except (TypeError, ValueError) as exc:
        try:
            await websocket.close(code=1008, reason=str(exc))
        except Exception:
            pass
    finally:
        if person is not None:
            async with state_lock:
                # Do not mark a newer connection offline when an old socket
                # closes after a reconnect.
                if person._connection is websocket:
                    person._connection = None
                    person.online = False
            await broadcast_snapshot()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
