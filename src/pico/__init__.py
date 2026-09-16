import argparse
import sys

from pico.app import run_pico
from pico.config import Config, ConfigError, load_config
from pico.session import connect, latest_session_id

RESUME_LATEST = "@latest"


def _resolve_session_id(config: Config, resume: str | None) -> str | None:
    if resume is None:
        return None
    if resume != RESUME_LATEST:
        return resume
    return latest_session_id(connect(config.session_path))


def main() -> None:
    parser = argparse.ArgumentParser(prog="pico")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--resume", nargs="?", const=RESUME_LATEST, default=None)
    args = parser.parse_args()

    debug: bool = args.debug
    resume: str | None = args.resume

    try:
        config = load_config()
    except ConfigError as error:
        print(f"pico: {error}", file=sys.stderr)
        sys.exit(1)
    run_pico(config, debug=debug, session_id=_resolve_session_id(config, resume))
