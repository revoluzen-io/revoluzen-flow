"""Stage bundled markdown docs into the Python package for wheel builds.

Run this BEFORE `python -m build`, alongside `scripts/stage_frontend.py`.

The Help -> Documentation tab reads markdown through backend API endpoints.
In a source checkout those files live at repo-root `docs/`, but a PyPI wheel
does not carry repo-root folders unless we copy them under the import package.
This script stages a read-only copy at `backend/revoluzen_flow/docs`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "docs"
DEST = REPO / "backend" / "revoluzen_flow" / "docs"
PACKAGE_ROOT = REPO / "backend" / "revoluzen_flow"

INCLUDE_SUFFIXES = {".md", ".txt", ".jsonl"}
EXCLUDE_NAMES = {
    "connector-validation-report.md",
    "connector-validation-live-report.md",
    "frontend-design-audit.md",
    "functional-test-requirements.md",
}


def _should_copy(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return False
    return path.suffix.lower() in INCLUDE_SUFFIXES


def main() -> int:
    if not SRC.is_dir():
        raise SystemExit(f"Missing docs source directory: {SRC}")

    if DEST.is_symlink() or DEST.resolve() != REPO.resolve() / "backend" / "revoluzen_flow" / "docs":
        raise SystemExit("Refusing to replace an unexpected staging path")
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True, exist_ok=True)

    copied = 0
    for src in sorted(SRC.rglob("*")):
        if not src.is_file() or not _should_copy(src):
            continue
        rel = src.relative_to(SRC)
        dst = DEST / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    if copied == 0:
        raise SystemExit("No docs staged; refusing to build docs-less wheel.")

    # Repo-root docs the Help catalog serves via `repo_root: True`. In a wheel
    # these must sit beside the staged docs/ directory, because the API
    # resolves them as `_docs_root().parent / path`. Missing one shows the
    # reader "Document file missing on disk" — README.md used to do exactly
    # that, since only CHANGELOG.md was staged here.
    for name in ("CHANGELOG.md", "README.md"):
        src_file = REPO / name
        if not src_file.is_file():
            raise SystemExit(f"Missing {name}; refusing to build incomplete docs bundle.")
        shutil.copy2(src_file, PACKAGE_ROOT / name)

    print(f"Staged {copied} documentation files into {DEST}")
    print(f"Staged CHANGELOG.md and README.md into {PACKAGE_ROOT}")
    print("Ready: python -m build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
