import json
import sqlite3
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime

from pico.llm.types import Message, Role, ToolCall, ToolResult
from pico.session.errors import UnknownEventKindError
from pico.session.events import (
    AssistantMessageRecorded,
    SessionEvent,
    ToolCallRecorded,
    UserMessageRecorded,
)

_EVENT_KINDS: dict[str, type[SessionEvent]] = {
    "UserMessageRecorded": UserMessageRecorded,
    "AssistantMessageRecorded": AssistantMessageRecorded,
    "ToolCallRecorded": ToolCallRecorded,
}


class Session:
    def __init__(self, conn: sqlite3.Connection, session_id: str) -> None:
        self._conn = conn
        self._session_id = session_id

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

    def events(self) -> Iterator[SessionEvent]:
        rows = self._conn.execute(
            "SELECT kind, payload FROM events WHERE session_id = ? ORDER BY seq",
            (self._session_id,),
        ).fetchall()
        for row in rows:
            event_type = _EVENT_KINDS.get(row["kind"])
            if event_type is None:
                raise UnknownEventKindError(row["kind"])
            yield event_type(**json.loads(row["payload"]))

    def messages(self) -> list[Message]:
        messages: list[Message] = []
        for index, event in enumerate(self.events()):
            match event:
                case UserMessageRecorded(content=content):
                    messages.append(Message(role=Role.USER, content=content))
                case AssistantMessageRecorded(content=content):
                    messages.append(Message(role=Role.ASSISTANT, content=content))
                case ToolCallRecorded(
                    name=name, arguments=arguments, result=result, is_error=is_error
                ):
                    call_id = str(index)
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
        return messages
