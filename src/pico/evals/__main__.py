from pico.app import build_llm_client
from pico.config import load_config
from pico.evals.runner import render_table, run_suite, write_report
from pico.evals.tasks import SUITE


def main() -> None:
    config = load_config()
    outcomes = run_suite(build_llm_client(config), config, SUITE)
    print(render_table(outcomes))
    print(f"\nreport: {write_report(outcomes, config)}")


if __name__ == "__main__":
    main()
