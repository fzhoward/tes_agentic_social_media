"""CLI entrypoint for the Critic variance harness.

Default behaviour is the dry-mode self-test: runs the harness with a
synthetic fake LLM, prints the report, writes a JSON dump. Zero API
spend. Useful for proving the math + report format work and for the
unit tests.

``--real`` flips to the real OpenAI path. This costs money — K calls per
fixture * len(FIXTURES) — and must never run automatically. Guarded by
an explicit flag and an ``OPENAI_API_KEY`` env-var check.

Examples:
    # Dry-mode self-test (no API)
    python -m evals

    # Real measurement with K=10 across all fixtures
    python -m evals --real --k 10

    # Single fixture, smaller K for a quick smoke
    python -m evals --real --k 3 --fixture str_20260602_fb_01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.fixtures import FIXTURES, get_fixture
from evals.report import format_text_report, write_json_dump
from evals.runner import run_all
from evals.variance import aggregate_fixture


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = PROJECT_ROOT / "evals" / "out"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Critic run-to-run variance harness. Default is dry-mode "
            "(no API). --real opts into the OpenAI path and costs money."
        ),
    )
    parser.add_argument(
        "--k",
        type=int,
        default=10,
        help="Runs per fixture (default: 10).",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help=(
            "Use the real OpenAI path. Requires OPENAI_API_KEY. Costs "
            "K * len(fixtures) chat-completion calls."
        ),
    )
    parser.add_argument(
        "--fixture",
        action="append",
        default=None,
        help=(
            "Restrict to a specific fixture name. Pass multiple times "
            "to include several. Defaults to all fixtures."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help=(
            "Dry-mode PRNG seed so the self-test is reproducible. "
            "Ignored in --real mode."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=(
            "Output directory for the JSON dump (default: evals/out/, "
            "which is gitignored)."
        ),
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Skip writing the JSON dump (text report still prints).",
    )
    return parser.parse_args(argv)


def _select_fixtures(names: list[str] | None) -> tuple:
    if not names:
        return FIXTURES
    return tuple(get_fixture(n) for n in names)


def _progress(name: str, completed: int, total: int) -> None:
    sys.stderr.write(f"\r  {name}: {completed}/{total}    ")
    sys.stderr.flush()
    if completed == total:
        sys.stderr.write("\n")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    fixtures = _select_fixtures(args.fixture)

    mode = "real" if args.real else "dry"
    sys.stderr.write(
        f"[evals] mode={mode}  K={args.k}  "
        f"fixtures={[f.name for f in fixtures]}\n"
    )
    if args.real:
        sys.stderr.write(
            f"[evals] real mode — about to spend "
            f"{args.k * len(fixtures)} API calls. Press Ctrl-C to abort.\n"
        )

    raw_results = run_all(
        k=args.k,
        real=args.real,
        fixtures=fixtures,
        seed=args.seed,
        progress_callback=_progress,
    )

    all_stats = []
    for fixture in fixtures:
        outputs = [
            r.output for r in raw_results[fixture.name]
            if not r.error and r.output
        ]
        errors = [r for r in raw_results[fixture.name] if r.error]
        if errors:
            sys.stderr.write(
                f"[evals] {fixture.name}: {len(errors)} run(s) errored — "
                f"first error: {errors[0].error}\n"
            )
        all_stats.append(aggregate_fixture(fixture.name, outputs))

    report = format_text_report(all_stats)
    print(report)

    if not args.no_json:
        path = write_json_dump(
            all_stats,
            out_dir=args.out,
            mode=mode,
            k=args.k,
            seed=args.seed if not args.real else None,
        )
        sys.stderr.write(f"[evals] JSON dump: {path}\n")

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
