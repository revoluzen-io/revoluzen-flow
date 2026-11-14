"""python -m revoluzen_flow.eval.intent [--drafter rule|live|replay] ...

Scores request-to-pipeline accuracy (runner.py). Exit code 1 when accuracy
is below ``--min-accuracy``; 0 otherwise, so a release can gate on it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .runner import draft_live, draft_replay, draft_rule, run, to_markdown


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Score request-to-pipeline accuracy")
    p.add_argument("--drafter", choices=["rule", "live", "replay"], default="rule")
    p.add_argument("--replay", help="JSON of recorded drafts (case id → raw planner JSON), for --drafter replay")
    p.add_argument("--record", help="Write the raw drafts of a live run here, to replay later")
    p.add_argument("--out", help="Write the full report (JSON) here")
    p.add_argument("--md", help="Write a Markdown summary here")
    p.add_argument("--min-accuracy", type=float, default=0.0, help="Fail below this share of correct cases, in [0, 1]")
    args = p.parse_args(argv)

    recorded: dict | None = None
    if args.drafter == "rule":
        drafter = draft_rule
    elif args.drafter == "replay":
        if not args.replay:
            p.error("--drafter replay needs --replay FILE")
        drafter = draft_replay(json.loads(Path(args.replay).read_text(encoding="utf-8")))
    else:
        recorded = {}
        drafter = lambda case: draft_live(case, recorded)  # noqa: E731

    report = run(drafter)
    report["drafter"] = args.drafter
    md = to_markdown(report, f"Request-to-pipeline accuracy ({args.drafter} drafter)")
    print(md)

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")
    if args.record and recorded is not None:
        Path(args.record).write_text(json.dumps(recorded, indent=2, default=str), encoding="utf-8")
    try:
        folder = Path(os.environ.get("REVOLUZEN_FLOW_DATA_DIR", "data")) / "eval"
        folder.mkdir(parents=True, exist_ok=True)
        summary = {k: v for k, v in report.items() if k != "results"}
        (folder / f"intent_{args.drafter}_latest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not write the summary: {exc}", file=sys.stderr)

    if report["accuracy"] < args.min_accuracy:
        print(f"GATE FAILED: accuracy {report['accuracy']:.0%} < {args.min_accuracy:.0%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
