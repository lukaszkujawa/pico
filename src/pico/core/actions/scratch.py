from collections.abc import Mapping

from pico.core.actions.arguments import require
from pico.core.scratch import Scratch, load_table, query
from pico.core.tools import Tool
from pico.llm.types import ToolSpec

LOAD_TABLE_SPEC = ToolSpec(
    name="load_table",
    description=(
        "Load a CSV file into a scratch SQL database table without reading it into context, "
        "and return the table's schema and row count. "
        "Prefer this over shell pipelines for anything tabular or numeric: "
        "load the file, then compute with the sql tool."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "table": {"type": "string"},
        },
        "required": ["path", "table"],
    },
)


def load_table_tool(scratch: Scratch) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        path = require(arguments, "path", str)
        table = require(arguments, "table", str)
        return load_table(scratch, path, table)

    return Tool(spec=LOAD_TABLE_SPEC, execute=execute)


SQL_SPEC = ToolSpec(
    name="sql",
    description=(
        "Run one SQL statement against the scratch database and return the rows. "
        "Prefer this over shell pipelines for filtering, joins, aggregation, and arithmetic: "
        "one SELECT with GROUP BY is computed exactly, where awk and sort are guesswork. "
        "CREATE and INSERT work too, so you can stage intermediate results. "
        "This is a tool, not a shell command: there is no 'sql' executable, "
        "so never put it in a shell command or in answer's verify."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)


def sql_tool(scratch: Scratch) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        statement = require(arguments, "query", str)
        return query(scratch, statement)

    return Tool(spec=SQL_SPEC, execute=execute)
