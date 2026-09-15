import sys

from pico.app import run_pico
from pico.config import ConfigError, load_config


def main() -> None:
    try:
        config = load_config()
    except ConfigError as error:
        print(f"pico: {error}", file=sys.stderr)
        sys.exit(1)
    run_pico(config)
