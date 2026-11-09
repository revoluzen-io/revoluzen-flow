"""Command line for the repair evaluation.

    python -m revoluzen_flow.eval.repair --provider oracle --gate
    python -m revoluzen_flow.eval.repair --provider live --record cassette.json --md report.md --gate
    python -m revoluzen_flow.eval.repair --provider replay --cassette cassette.json --gate

Exit code: 0 when the gate passes or was not asked for, 1 when it fails.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="revoluzen_flow.eval.repair", description=__doc__)
    parser.add_argument("--provider", choices=("oracle", "mock", "live", "replay"), default="oracle")
    parser.add_argument("--case", action="append", dest="only", help="Run one case id. Repeatable.")
    parser.add_argument(
        "--suite",
        choices=("regression", "fresh"),
        default="regression",
        help="regression: the cases the product was built against. fresh: cases no fix has been made from.",
    )
    parser.add_argument("--cassette", help="Recording to play back with --provider replay.")
    parser.add_argument("--record", help="Save the model's replies to this file for later replay.")
    parser.add_argument("--out", help="Write the full JSON report here.")
    parser.add_argument("--md", help="Write the scorecard as Markdown here.")
    parser.add_argument("--gate", action="store_true", help="Exit 1 when the scores miss the gates.")
    parser.add_argument(
        "--dry-run-gate",
        action="store_true",
        help="Switch on the product's dry-run gate for write tools, as a running server has it.",
    )
    args = parser.parse_args(argv)

    # The engine and app read these at import, so they are set before those imports.
    # A throwaway data directory keeps a run away from any real install.
    os.environ.setdefault("REVOLUZEN_FLOW_DATA_DIR", tempfile.mkdtemp(prefix="rvz-repair-data-"))
    os.environ.setdefault("REVOLUZEN_FLOW_MODE", "dev")

    from .cases import ALL_CASES
    from .fresh_cases import FRESH_CASES, SEEN_CASES
    from .runner import MODEL_GATES, ORACLE_GATES, check_gates, run_suite, to_markdown

    cases = FRESH_CASES if args.suite == "fresh" else (*ALL_CASES, *SEEN_CASES)
    cassette = None
    if args.provider == "replay":
        if not args.cassette:
            parser.error("--provider replay needs --cassette")
        cassette = json.loads(Path(args.cassette).read_text(encoding="utf-8"))
        # A recording only answers for the cases it holds. Its floors were pinned on those.
        cases = tuple(c for c in cases if c.id in cassette["replies"])
        if not cases:
            parser.error(f"{args.cassette} holds no case from the {args.suite} suite")

    report = run_suite(
        provider=args.provider,
        cases=cases,
        only=args.only,
        cassette=cassette,
        record=bool(args.record),
        dry_run_gate=args.dry_run_gate,
    )
    gates = ORACLE_GATES if args.provider == "oracle" else MODEL_GATES
    if cassette and cassette.get("gates"):
        gates = cassette["gates"]
    failures = check_gates(report["scores"], gates)

    recording = report.pop("cassette", None)
    if args.record and recording is not None:
        Path(args.record).write_text(json.dumps(recording, indent=1), encoding="utf-8")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    markdown = to_markdown(report, failures if args.gate else None)
    if args.md:
        Path(args.md).write_text(markdown, encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(markdown)
    return 1 if args.gate and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
