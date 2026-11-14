"""Runs the ``git`` program to save files to the customer's repository.

Why the program and not a provider's web API: it works with GitHub, GitLab,
Azure DevOps, Bitbucket and a company's own server alike.

The access token is the thing to protect. It is handed to git as an
environment setting (``http.extraHeader``), never on the command line where
other processes could read it and never in the remote address, so it is not
written to ``.git/config``. Anything git prints is scrubbed of it before it is
stored or shown.

Nothing on the host may interfere or hang a background job: credential helpers
are switched off (a headless Git Credential Manager waits for ever), and hooks,
commit signing and the user's own git config are ignored. Only http and https
are allowed, because git reaches a local path by starting a shell, and on
Windows those can multiply until the machine runs out of memory.

Each save starts again from what the server has, so a failed push leaves
nothing behind to clash with the next one.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
from typing import Callable
from urllib.parse import urlsplit

from revoluzen_flow.gitcopy.settings import GitSettings

logger = logging.getLogger(__name__)

#: Long enough for a slow server, short enough not to hold a worker for ever.
GIT_TIMEOUT_SECONDS = 120
#: Someone else pushed between our fetch and our push: start again this often.
PUSH_ATTEMPTS = 3
#: GIT_CONFIG_COUNT, which keeps the token off the command line, arrived here.
MINIMUM_GIT = (2, 31)

#: Who committed. The person who published is the commit's author.
COMMITTER_NAME = "Revoluzen Flow"
COMMITTER_EMAIL = "flow@revoluzen.invalid"


class GitFailed(Exception):
    """A git step failed. The message is written for the person who reads it,
    and holds no token."""


class WillNotRetry(GitFailed):
    """Trying again cannot help, for example a secret was found in the files."""


def git_program() -> str | None:
    return shutil.which("git")


def git_version() -> tuple[int, int] | None:
    program = git_program()
    if program is None:
        return None
    try:
        done = subprocess.run(
            [program, "--version"], capture_output=True, text=True, timeout=20, **_quiet_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"(\d+)\.(\d+)", done.stdout or "")
    return (int(found.group(1)), int(found.group(2))) if found else None


def _quiet_window() -> dict:
    """On Windows, no console window flashing up for each git command."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


def default_username(repo_url: str) -> str:
    """The name that goes with an access token on the common Git services."""
    host = (urlsplit(repo_url).hostname or "").lower()
    if "github" in host:
        return "x-access-token"
    if "gitlab" in host:
        return "oauth2"
    if "bitbucket" in host:
        return "x-token-auth"
    return "git"


def commit_page(repo_url: str, sha: str) -> str:
    """The web page of a commit on the common Git services, or empty."""
    parsed = urlsplit(repo_url)
    if parsed.scheme not in ("http", "https"):
        return ""
    base = repo_url.rstrip("/")
    if base.endswith(".git"):
        base = base[:-4]
    host = (parsed.hostname or "").lower()
    if "gitlab" in host:
        return f"{base}/-/commit/{sha}"
    if "bitbucket" in host:
        return f"{base}/commits/{sha}"
    if "github" in host or host == "dev.azure.com" or host.endswith(".visualstudio.com") or "gitea" in host:
        return f"{base}/commit/{sha}"
    return ""


def explain(stderr: str) -> str:
    """What git said, as something a person can act on."""
    text = (stderr or "").lower()
    if any(s in text for s in (
        "authentication failed", "could not read username", "could not read password",
        "invalid username or password", "http basic: access denied", "returned error: 401",
        "the requested url returned error: 403", "permission to", "access denied",
    )):
        return "The Git server refused the access token. Check that it is correct, has not expired and can write to this repository."
    if "repository not found" in text or "not found" in text and "repository" in text or "returned error: 404" in text:
        return "The Git server has no repository at that address, or the access token cannot see it."
    if "could not resolve host" in text or "name or service not known" in text:
        return "Could not find that Git server. Check the address and this server's network."
    if "failed to connect" in text or "connection timed out" in text or "couldn't connect" in text or "connection refused" in text:
        return "Could not reach the Git server. Check that this server can connect to it."
    if "ssl" in text or "certificate" in text:
        return "Could not make a secure connection to the Git server. Check its certificate is trusted on this server."
    if "protected branch" in text or "pre-receive hook declined" in text or "gh006" in text:
        return "The Git server refused the change: that branch is protected. Use another branch, or let this token write to it."
    if "returned error: 30" in text or "redirect" in text:
        return "The Git server sent Flow to another address. Use the repository's current address."
    first = next((line.strip() for line in (stderr or "").splitlines() if line.strip()), "")
    return f"Git reported: {first[:300]}" if first else "Git could not finish, and did not say why."


class GitRepo:
    """A working copy, kept only to build the next commit."""

    def __init__(self, directory: str | None, settings: GitSettings):
        self.directory = directory
        self.settings = settings
        program = git_program()
        if program is None:
            raise WillNotRetry("The git program is not installed on this server. Install Git, then try again.")
        version = git_version()
        if version is not None and version < MINIMUM_GIT:
            raise WillNotRetry(
                f"Git {MINIMUM_GIT[0]}.{MINIMUM_GIT[1]} or newer is needed on this server "
                f"to keep the access token protected. This server has {version[0]}.{version[1]}."
            )
        self.program = program

    # ── Running git ─────────────────────────────────────────────────────

    def _basic_credentials(self) -> str:
        username = self.settings.username or default_username(self.settings.repo_url)
        return base64.b64encode(f"{username}:{self.settings.token}".encode()).decode()

    def _scrub(self, text: str) -> str:
        if self.settings.token:
            text = text.replace(self.settings.token, "***").replace(self._basic_credentials(), "***")
        return text

    def _environment(self, author: tuple[str, str] | None = None) -> dict[str, str]:
        env = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("GIT_", "GCM_")) and key.upper() != "SSH_ASKPASS"
        }
        settings = [
            # Off, so git never starts a helper that waits for a person.
            ("credential.helper", ""),
            ("core.askPass", ""),
            # Nothing of the machine's may run or change what is committed.
            ("core.hooksPath", os.devnull),
            ("core.fsmonitor", "false"),
            ("core.autocrlf", "false"),
            ("core.safecrlf", "false"),
            ("commit.gpgSign", "false"),
            ("gc.auto", "0"),
            ("maintenance.auto", "false"),
            # A redirect would carry the token's header to wherever it points.
            ("http.followRedirects", "false"),
        ]
        scheme = urlsplit(self.settings.repo_url).scheme.lower()
        if scheme not in ("http", "https"):
            raise WillNotRetry("Flow only copies to a Git server over http or https.")
        if self.settings.token:
            settings.append(("http.extraHeader", f"Authorization: Basic {self._basic_credentials()}"))
        for index, (key, value) in enumerate(settings):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        env["GIT_CONFIG_COUNT"] = str(len(settings))
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env["GIT_ALLOW_PROTOCOL"] = scheme
        env["LC_ALL"] = "C"
        env["LANGUAGE"] = "C"
        env["GIT_COMMITTER_NAME"] = COMMITTER_NAME
        env["GIT_COMMITTER_EMAIL"] = COMMITTER_EMAIL
        if author is not None:
            env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"] = author
        return env

    def _run(
        self,
        *args: str,
        stdin: str | None = None,
        author: tuple[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        try:
            done = subprocess.run(
                [self.program, *args],
                cwd=self.directory or None,
                env=self._environment(author),
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
                **_quiet_window(),
            )
        except subprocess.TimeoutExpired:
            raise GitFailed("The Git server did not answer in time.") from None
        except OSError:
            raise GitFailed("The git program could not be started on this server.") from None
        done.stdout = self._scrub(done.stdout or "")
        done.stderr = self._scrub(done.stderr or "")
        if check and done.returncode != 0:
            logger.warning("git %s failed: %s", args[0] if args else "", done.stderr[:500])
            raise GitFailed(explain(done.stderr))
        return done

    # ── What Flow asks of it ────────────────────────────────────────────

    def check_access(self) -> bool:
        """Sign in and look for the branch. Returns whether the branch exists
        yet; raises :class:`GitFailed` when the repository cannot be used."""
        done = self._run(
            "ls-remote", "--heads", "--", self.settings.repo_url,
            f"refs/heads/{self.settings.branch}",
        )
        return bool(done.stdout.strip())

    def _start_from_server(self) -> None:
        """Make the working copy exactly what is on the server, or empty if
        the repository or the branch has nothing on it yet."""
        branch = self.settings.branch
        os.makedirs(self.directory, exist_ok=True)
        if not os.path.isdir(os.path.join(self.directory, ".git")):
            self._run("init", "--quiet", "--template=", ".")
        self._run("config", "--local", "remote.origin.url", self.settings.repo_url)
        fetched = self._run(
            "fetch", "--quiet", "--no-tags", "--depth=1", "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            check=False,
        )
        if fetched.returncode == 0:
            self._run("checkout", "--quiet", "--force", "-B", branch, f"refs/remotes/origin/{branch}")
        elif "couldn't find remote ref" in fetched.stderr.lower():
            self._run("update-ref", "-d", f"refs/heads/{branch}", check=False)
            self._run("symbolic-ref", "HEAD", f"refs/heads/{branch}")
            self._run("read-tree", "--empty")
        else:
            raise GitFailed(explain(fetched.stderr))
        self._run("clean", "--quiet", "-f", "-d", "-x")

    def save(
        self,
        write_files: Callable[[str], None],
        message: str,
        author: tuple[str, str],
    ) -> str | None:
        """Commit what ``write_files(working_directory)`` leaves and push it.

        Returns the new commit, or None when the files were already exactly
        as they are on the server.
        """
        for attempt in range(1, PUSH_ATTEMPTS + 1):
            self._start_from_server()
            write_files(self.directory)
            self._run("add", "--all", ".")
            if self._run("diff", "--cached", "--quiet", check=False).returncode == 0:
                return None
            self._run("commit", "--quiet", "--no-verify", "--file=-", stdin=message, author=author)
            sha = self._run("rev-parse", "HEAD").stdout.strip()
            pushed = self._run(
                "push", "--quiet", "origin", f"HEAD:refs/heads/{self.settings.branch}", check=False,
            )
            if pushed.returncode == 0:
                return sha
            text = pushed.stderr.lower()
            someone_else_pushed = "non-fast-forward" in text or "fetch first" in text or "cannot lock ref" in text
            if not someone_else_pushed or attempt == PUSH_ATTEMPTS:
                logger.warning("git push failed: %s", pushed.stderr[:500])
                raise GitFailed(explain(pushed.stderr))
        raise GitFailed("Others kept changing the repository while Flow was saving to it.")
