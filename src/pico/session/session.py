import json
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, cast

from pico.llm.types import Message, Role, ToolCall, ToolResult
from pico.session.errors import UnknownEventKindError
from pico.session.events import (
    AssistantMessageRecorded,
    PlanSet,
    PlanStepCompleted,
    SessionEvent,
    ToolCallRecorded,
    UserMessageRecorded,
)

_EVENT_KINDS: dict[str, type[SessionEvent]] = {
    "UserMessageRecorded": UserMessageRecorded,
    "AssistantMessageRecorded": AssistantMessageRecorded,
    "ToolCallRecorded": ToolCallRecorded,
    "PlanSet": PlanSet,
    "PlanStepCompleted": PlanStepCompleted,
}


def new_session_id() -> str:
    return f"{datetime.now(UTC).strftime('%Y-%m-%d_%H-%M-%S')}-{uuid.uuid4().hex[:6]}"


def latest_session_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT session_id FROM events WHERE session_id NOT LIKE '%/%' "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else str(row["session_id"])


class Session:
    def __init__(self, conn: sqlite3.Connection, session_id: str) -> None:
        self._conn = conn
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def child(self, suffix: str) -> "Session":
        return Session(self._conn, f"{self._session_id}/{suffix}")

    def append(self, event: SessionEvent) -> None:
        kind = type(event).__name__
        payload = json.dumps(asdict(event))
        created_at = datetime.now(UTC).isoformat()
        with self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?",
                (self._session_id,),
            ).fetchone()
            next_seq = row[0] + 1
            self._conn.execute(
                "INSERT INTO events (session_id, seq, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._session_id, next_seq, kind, payload, created_at),
            )

    def records(self) -> Iterator[tuple[int, SessionEvent]]:
        rows = self._conn.execute(
            "SELECT seq, kind, payload FROM events WHERE session_id = ? ORDER BY seq",
            (self._session_id,),
        ).fetchall()
        for row in rows:
            event_type = _EVENT_KINDS.get(row["kind"])
            if event_type is None:
                raise UnknownEventKindError(row["kind"])
            payload = cast(dict[str, Any], json.loads(row["payload"]))
            if event_type is PlanSet:
                payload["steps"] = tuple(cast(list[str], payload["steps"]))
            yield int(row["seq"]), event_type(**payload)

    def events(self) -> Iterator[SessionEvent]:
        return (event for _, event in self.records())

    def messages(self) -> list[Message]:
        messages: list[Message] = []
        for seq, event in self.records():
            match event:
                case UserMessageRecorded(content=content):
                    messages.append(Message(role=Role.USER, content=content))
                case AssistantMessageRecorded(content=content):
                    messages.append(Message(role=Role.ASSISTANT, content=content))
                case ToolCallRecorded(
                    name=name, arguments=arguments, result=result, is_error=is_error
                ):
                    call_id = str(seq)
                    messages.append(
                        Message(
                            role=Role.ASSISTANT,
                            tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
                        )
                    )
                    messages.append(
                        Message(
                            role=Role.TOOL,
                            tool_result=ToolResult(
                                tool_call_id=call_id, content=result, is_error=is_error
                            ),
                        )
                    )
                case PlanSet() | PlanStepCompleted():
                    pass
        return messages
