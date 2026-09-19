from dataclasses import dataclass


@dataclass(frozen=True)
class Options:
    names: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    takes_argument: bool = False


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(name="quit", description="exit pico"),
    SlashCommand(
        name="model",
        description="switch the model for the next run",
        takes_argument=True,
    ),
)

KNOWN_COMMANDS = " ".join(f"/{command.name}" for command in COMMANDS)


def find(name: str) -> SlashCommand | None:
    return next((command for command in COMMANDS if command.name == name), None)


def awaits_argument(text: str) -> bool:
    name, separator, _ = text.removeprefix("/").partition(" ")
    command = find(name)
    return not separator and command is not None and command.takes_argument


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
    pending: bool = False

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


def complete(text: str, models: Options | None, current: str | None = None) -> Completion | None:
    if not text.startswith("/") or "\n" in text:
        return None
    name, separator, argument = text.removeprefix("/").partition(" ")
    if not separator:
        return _command_rows(name)
    command = find(name)
    if command is None or not command.takes_argument:
        return None
    if models is None:
        return Completion(command=command, pending=True)
    if models.error is not None:
        return Completion(error=models.error, command=command)
    prefix = argument.lstrip()
    return Completion(
        rows=tuple(
            Row(label=model, marked=model == current)
            for model in models.names
            if model.startswith(prefix)
        ),
        command=command,
    )
