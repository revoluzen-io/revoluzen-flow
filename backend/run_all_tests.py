"""Revoluzen Flow CI smoke runner.

The full test matrix lives in the development monorepo. For the OSS launch
build this script does a fast smoke check: it imports the public `revoluzen_flow`
package and reports the version. Real per-module tests are run via
`pytest backend/tests` directly during development.

Every module listed in REQUIRED must import, or the smoke fails. Until
2026-11-09 the API module was "optional" and a missing dependency printed
`SKIP (optional)` while the job stayed green; the CI jobs that run this
script now install backend/requirements.txt, so nothing is optional and a
skip is a failure.

Accepts the same flags the GitHub Actions workflow passes so the CI job
contract stays stable:

    python run_all_tests.py --fast
    python run_all_tests.py --security
    python run_all_tests.py --all
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys

# Make sure `backend/` is on sys.path when this script is run directly
# from inside `backend/`. Lets the smoke run without a `pip install -e .`.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

REQUIRED = [
    "revoluzen_flow",
    "revoluzen_flow.storage.database",
    "revoluzen_flow.api.security_headers",
]
OPTIONAL: list[str] = []


def smoke() -> int:
    import revoluzen_flow
    print(f"revoluzen_flow v{revoluzen_flow.__version__}")

    failed: list[tuple[str, str]] = []
    for mod in REQUIRED + OPTIONAL:
        try:
            importlib.import_module(mod)
            print(f"OK   {mod}")
        except Exception as exc:
            tag = "REQUIRED" if mod in REQUIRED else "optional"
            print(f"SKIP ({tag}) {mod}  {exc!r}")
            if mod in REQUIRED:
                failed.append((mod, repr(exc)))

    if failed:
        print(f"\n{len(failed)} required module(s) failed to import")
        return 1
    print("\nSmoke OK")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fast", action="store_true")
    p.add_argument("--security", action="store_true")
    p.add_argument("--all", action="store_true")
    p.parse_args()
    return smoke()


if __name__ == "__main__":
    sys.exit(main())
