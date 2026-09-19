import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, cast

from pico.llm.types import Message, Role, ToolCall, ToolResult
from pico.session.events import (
    AssistantMessageRecorded,
    PlanSet,
    PlanStepCompleted,
    SessionEvent,
    ToolCallRecorded,
    UserMessageRecorded,
)

ELIDE_CONTENT_CHARS = 500


class UnknownEventKindError(Exception):
    pass


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


def _rendered_arguments(name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
    content = arguments.get("content")
    if name != "write_file" or not isinstance(content, str) or len(content) <= ELIDE_CONTENT_CHARS:
        return arguments
    stub = f"<{len(content)} chars — on disk at {arguments.get('path')}; read_file to recover>"
    return {**arguments, "content": stub}


def _live_image_index(records: list[tuple[int | None, SessionEvent]]) -> int | None:
    return max(
        (
            index
            for index, (_, event) in enumerate(records)
            if isinstance(event, ToolCallRecorded)
            and event.name == "view_image"
            and not event.is_error
        ),
        default=None,
    )


def _decode(kind: str, payload: str) -> SessionEvent:
    event_type = _EVENT_KINDS.get(kind)
    if event_type is None:
        raise UnknownEventKindError(kind)
    fields = cast(dict[str, Any], json.loads(payload))
    if event_type is PlanSet:
        fields["steps"] = tuple(cast(list[str], fields["steps"]))
    return event_type(**fields)


class Session:
    def __init__(self, conn: sqlite3.Connection, session_id: str) -> None:
        self._conn = conn
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def root_id(self) -> str:
        return self._session_id.split("/", 1)[0]

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def child(self, suffix: str) -> "Session":
        return Session(self._conn, f"{self._session_id}/{suffix}")

    def next_seq(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?",
            (self._session_id,),
        ).fetchone()
        return int(row[0]) + 1

    def append(self, event: SessionEvent) -> None:
        kind = type(event).__name__
        payload = json.dumps(asdict(event))
        created_at = datetime.now(UTC).isoformat()
        root = self.root_id
        with self._conn:
            fact_id = None
            if isinstance(event, ToolCallRecorded):
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(fact_id), 0) FROM events "
                    "WHERE session_id = ? OR session_id LIKE ?",
                    (root, f"{root}/%"),
                ).fetchone()
                fact_id = int(row[0]) + 1
            self._conn.execute(
                "INSERT INTO events (session_id, seq, fact_id, kind, payload, created_at) "
                "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ? FROM events WHERE session_id = ?",
                (self._session_id, fact_id, kind, payload, created_at, self._session_id),
            )

    def records(self) -> Iterator[tuple[int | None, SessionEvent]]:
        rows = self._conn.execute(
            "SELECT fact_id, kind, payload FROM events WHERE session_id = ? ORDER BY seq",
            (self._session_id,),
        ).fetchall()
        for row in rows:
            fact_id = None if row["fact_id"] is None else int(row["fact_id"])
            yield fact_id, _decode(row["kind"], row["payload"])

    def tree_records(self) -> Iterator[tuple[int, SessionEvent]]:
        root = self.root_id
        rows = self._conn.execute(
            "SELECT fact_id, kind, payload FROM events "
            "WHERE fact_id IS NOT NULL AND (session_id = ? OR session_id LIKE ?) "
            "ORDER BY fact_id",
            (root, f"{root}/%"),
        ).fetchall()
        for row in rows:
            yield int(row["fact_id"]), _decode(row["kind"], row["payload"])

    def events(self) -> Iterator[SessionEvent]:
        return (event for _, event in self.records())

    def messages(self) -> list[Message]:
        records = list(self.records())
        live_image = _live_image_index(records)
        messages: list[Message] = []
        for index, (fact_id, event) in enumerate(records):
            match event:
                case UserMessageRecorded(content=content):
                    messages.append(Message(role=Role.USER, content=content))
                case AssistantMessageRecorded(content=content) if content:
                    messages.append(Message(role=Role.ASSISTANT, content=content))
                case ToolCallRecorded(
                    name=name, arguments=arguments, result=result, is_error=is_error
                ):
                    call_id = str(fact_id)
                    messages.append(
                        Message(
                            role=Role.ASSISTANT,
                            tool_calls=(
                                ToolCall(
                                    id=call_id,
                                    name=name,
                                    arguments=_rendered_arguments(name, arguments),
                                ),
                            ),
                        )
                    )
                    content = result
                    images: tuple[str, ...] = ()
                    if name == "view_image" and not is_error:
                        path = str(arguments.get("path", ""))
                        if index == live_image:
                            images = (path,)
                        else:
                            content = (
                                f"viewed {path} — the image has left your sight; "
                                "call view_image again if you need another look"
                            )
                    messages.append(
                        Message(
                            role=Role.TOOL,
                            tool_result=ToolResult(
                                tool_call_id=call_id, content=content, is_error=is_error, name=name
                            ),
                            images=images,
                        )
                    )
                case AssistantMessageRecorded() | PlanSet() | PlanStepCompleted():
                    pass
        return messages
