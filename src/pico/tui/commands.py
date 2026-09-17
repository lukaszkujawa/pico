from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Options:
    names: tuple[str, ...] = ()
    error: str | None = None


class ModelSwitch(Protocol):
    @property
    def current(self) -> str: ...

    def available(self) -> Options: ...

    def switch_to(self, model: str) -> None: ...


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    arguments: Callable[[ModelSwitch], Options] | None = None


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        name="model",
        description="switch the model for the next run",
        arguments=lambda switch: switch.available(),
    ),
    SlashCommand(name="quit", description="exit pico"),
)

KNOWN_COMMANDS = " ".join(f"/{command.name}" for command in COMMANDS)


def find(name: str) -> SlashCommand | None:
    return next((command for command in COMMANDS if command.name == name), None)


def awaits_argument(text: str) -> bool:
    name, separator, _ = text.removeprefix("/").partition(" ")
    command = find(name)
    return not separator and command is not None and command.arguments is not None


@dataclass(frozen=True)
class Row:
    label: str
    hint: str = ""
    marked: bool = False


@dataclass(frozen=True)
class Completion:
    rows: tuple[Row, ...] = ()
    error: str | None = None
    command: SlashCommand | None = None

    def accepted(self, row: Row) -> str:
        return f"/{row.label}" if self.command is None else f"/{self.command.name} {row.label}"


def _command_rows(prefix: str) -> Completion:
    return Completion(
        rows=tuple(
            Row(label=command.name, hint=command.description)
            for command in COMMANDS
            if command.name.startswith(prefix)
        )
    )


def complete(text: str, switch: ModelSwitch | None) -> Completion | None:
    if not text.startswith("/") or "\n" in text:
        return None
    name, separator, argument = text.removeprefix("/").partition(" ")
    if not separator:
        return _command_rows(name)
    command = find(name)
    if command is None or command.arguments is None or switch is None:
        return None
    options = command.arguments(switch)
    if options.error is not None:
        return Completion(error=options.error, command=command)
    prefix = argument.lstrip()
    return Completion(
        rows=tuple(
            Row(label=model, marked=model == switch.current)
            for model in options.names
            if model.startswith(prefix)
        ),
        command=command,
    )
