#!/usr/bin/env python3
"""Fail if a self-referential GitHub slug disagrees with app_meta.py.

app_meta.py claims to be the single source of truth for where this project
lives on GitHub, but roughly fifty files hardcode the owner/repo anyway. That
has already broken twice: once when a rename left REPO_NAME pointing at a slug
that 404s (every in-app link dead, update-check permanently reporting
"you're on the latest"), and once when a company rename rewrote correct slugs
into a namespace owned by somebody else.

This guard keeps the claim honest. It does not forbid hardcoded links -- it
only requires that every link pointing at *our own* repo agrees with the
constants, and that no doc tells a reader to cd into a directory that `git
clone` never creates. Third-party GitHub URLs are untouched.

Run: python scripts/check_repo_coordinates.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_META = ROOT / "backend" / "revoluzen_flow" / "app_meta.py"
SELF = Path(__file__).resolve()  # this file necessarily spells the bad names out

# Owner names this project has used, or might be mistaken for. A github.com
# URL under any of these must match the canonical slug exactly.
OURS = {
    "hybridyn",
    "revoluzen",
    "revoluzen-technologies",   # org renamed away from this 2026-10-22
    "revoluzen-data-systems",   # ... and away from this 2026-10-23
    "revoluzen-io",             # current — listed so strays here are policed too
    "revoluzenhq",
    "avisdreamer",
}

# Directory names `git clone` never produces. It makes a folder named after
# REPO_NAME, so a doc saying `cd hybridyn-f-pulse-oss` strands the reader at
# step 2 of every install path. That has shipped twice.
STALE_DIR_NAMES = (
    "hybridyn-f-pulse-oss",
    "revoluzen-f-pulse-oss",
)

SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz",
    ".whl", ".so", ".dll", ".dylib", ".woff", ".woff2", ".ttf", ".eot",
    ".lock", ".parquet", ".avro", ".db", ".sqlite", ".sqlite3",
}

SLUG = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


def canonical() -> tuple[str, str]:
    text = APP_META.read_text(encoding="utf-8")
    owner = re.search(r'^REPO_OWNER\s*=\s*"([^"]+)"', text, re.M)
    name = re.search(r'^REPO_NAME\s*=\s*"([^"]+)"', text, re.M)
    if not owner or not name:
        sys.exit(f"could not read REPO_OWNER/REPO_NAME from {APP_META}")
    return owner.group(1), name.group(1)


def files():
    """Tracked files only.

    Going through git is what keeps this cheap and honest: it inherits
    .gitignore, so staged copies, *.egg-info, logs and nested worktrees are
    skipped without a hand-maintained maze of excludes.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True, check=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"could not list tracked files via git: {exc}")

    for rel in out.split("\0"):
        if not rel:
            continue
        path = ROOT / rel
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if path == APP_META or path.resolve() == SELF or not path.is_file():
            continue
        yield path


def actual_remote() -> str | None:
    """`owner/repo` from the `origin` remote, or None if it cannot be read.

    Returns None rather than raising for the cases where there legitimately
    is no remote — a fresh `git init`, a tarball export, a CI checkout that
    has stripped it. Those must not fail the build.
    """
    try:
        url = subprocess.run(
            ["git", "-C", str(ROOT), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None

    # Handles https://github.com/o/r(.git) and git@github.com:o/r(.git)
    m = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", url)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2).removesuffix('.git')}"


def check_remote_matches(want: str) -> list[str]:
    """Compare app_meta's coordinates against where the repo actually lives.

    Added 2026-10-22, after the org was renamed from `revoluzen-technologies`
    to `revoluzen-data-systems` and this guard still reported OK. It only ever
    compared the repo against *itself*, so a repo-wide set of consistently
    wrong URLs passed cleanly — consistency is not correctness.

    GitHub redirects the old name, which is exactly why the drift is easy to
    miss: clones, pushes and browser links all keep working. What does not
    follow a redirect is PyPI Trusted Publishing, whose claim is matched on
    the literal `owner/repo`, so a release fails with an opaque auth error
    long after the rename.

    A mismatch is a warning, not a failure. A fork is a legitimate mismatch
    and must still be able to build.
    """
    actual = actual_remote()
    if actual is None or actual == want:
        return []
    return [
        f"app_meta.py says {want!r} but origin points at {actual!r}.",
        "  GitHub redirects the old name so most things keep working, but a",
        "  PyPI Trusted Publisher claim is matched literally and will fail.",
        "  Either update REPO_OWNER/REPO_NAME in app_meta.py, or ignore this",
        "  if you are working in a fork.",
    ]


def main() -> int:
    owner, name = canonical()
    want = f"{owner}/{name}"
    bad: list[str] = []

    drift = check_remote_matches(want)

    for path in files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            for found_owner, found_repo in SLUG.findall(line):
                if found_owner not in OURS:
                    continue  # third-party link, not ours to police
                found_repo = found_repo.removesuffix(".git")
                if f"{found_owner}/{found_repo}" != want:
                    bad.append(f"  {rel}:{lineno}: {found_owner}/{found_repo}")
            for stale in STALE_DIR_NAMES:
                if stale != name and stale in line:
                    bad.append(f"  {rel}:{lineno}: stale directory name {stale!r}")

    if drift:
        print("WARNING: repo coordinates drift")
        print("\n".join(drift))
        print()

    if bad:
        print(f"Self-referential GitHub slugs disagreeing with app_meta.py ({want}):")
        print("\n".join(bad))
        print(
            f"\n{len(bad)} bad reference(s). Either fix the link, or if the repo "
            f"genuinely moved, update REPO_OWNER/REPO_NAME in app_meta.py first."
        )
        return 1

    suffix = " (see the drift warning above)" if drift else ""
    print(f"OK: every self-referential GitHub slug matches {want}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
