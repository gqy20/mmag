"""Command-line entry for validating and explicitly running MMAG evaluations."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from .loader import EvaluationAssetError, EvaluationAssetLoader
from .mattermost import MattermostEvaluationDriver
from .reporting import JSONEvaluationReporter
from .runner import EvaluationRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mmag-eval")
    parser.add_argument("--root", default="evals", help="evaluation asset root")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("validate", help="validate all assets without execution")

    run = subcommands.add_parser("run", help="run one explicitly selected evaluation suite")
    run.add_argument("suite", help="suite path relative to the evaluation root")
    run.add_argument("--profile", required=True, help="profile path relative to the root")
    run.add_argument("--output-dir", default=".eval-runs")
    run.add_argument("--env-file", default=".env")
    run.add_argument(
        "--allow-external",
        action="store_true",
        help="required before any real Mattermost request is allowed",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        loader = EvaluationAssetLoader(args.root)
        if args.command == "validate":
            profiles, suites, cases = loader.validate_tree()
            print(json.dumps({"profiles": profiles, "suites": suites, "cases": cases}))
            return
        if not args.allow_external:
            raise SystemExit(
                "external evaluation is disabled; pass --allow-external explicitly"
            )
        env_file = Path(args.env_file)
        if env_file.is_file():
            load_dotenv(env_file, override=False)
        reporter = JSONEvaluationReporter(args.output_dir)
        driver = MattermostEvaluationDriver(allow_external=args.allow_external)
        result = asyncio.run(
            EvaluationRunner(loader, driver, reporter).run(args.suite, args.profile)
        )
        print(
            json.dumps(
                {
                    "run_id": result.id,
                    "passed": result.passed,
                    "functional_success_rate": result.functional_success_rate,
                    "security_violation_count": result.security_violation_count,
                    "report_path": result.report_path,
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(0 if result.passed else 1)
    except EvaluationAssetError as error:
        raise SystemExit(f"evaluation asset error: {error}") from error


if __name__ == "__main__":
    main()
