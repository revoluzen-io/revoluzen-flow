# SOP — Taking Revoluzen Flow public

**Owner:** Revoluzen Private Limited, maintainer on duty
**Applies to:** flipping `revoluzen-io/revoluzen-flow` from private to public, and
the first publish of `revoluzen-flow` to PyPI.
**Status at the time of writing:** 2026-10-30. Target date 2026-11-01.

This is a runbook, not an explainer. Work top to bottom. Every step has a
check you can run; a step is not done until its check passes.

> **Stop condition.** If any **Blocker** below is unresolved, do not flip the
> repository public. A public repository with failing checks and a `pip install`
> command that does not work is harder to recover from than a delayed launch:
> the first visitors are the ones who decide whether the project looks
> maintained.

---

## 0. Blockers — resolve before anything else

### B1. GitHub Actions is not executing

Every workflow job currently fails about two seconds after it starts, with zero
steps recorded and an empty log archive. That is not a code failure — it is the
account-level block on Actions.

```bash
gh api repos/revoluzen-io/revoluzen-flow/actions/runs --jq '.workflow_runs[0:3][]|[.conclusion,.name]|@tsv'
```

Why this blocks the launch, and not cosmetically:

- **PyPI publishing runs inside `release.yml`** using OIDC trusted publishing
  (`permissions: id-token: write`). No Actions means no publish. There is no
  manual fallback that preserves trusted publishing — uploading by hand would
  require creating an API token, which the workflow deliberately tells you not
  to do.
- A public repository shows its check status on every commit and pull request.
  Twenty red crosses on `main` is the first thing a visitor sees.

**Resolve:** settle the GitHub billing/quota on the account that owns
`revoluzen-io`, then re-run one workflow and confirm a job records steps and
produces a non-empty log.

```bash
gh run rerun <run-id> --failed
gh api repos/revoluzen-io/revoluzen-flow/actions/runs/<run-id>/jobs --jq '.jobs[]|[.conclusion,.name,(.steps|length)]|@tsv'
```

A job with `0` steps has still not run. Only a non-zero step count clears B1.

### B2. The PyPI Trusted Publisher must name the distribution, not the brand

The package name in `pyproject.toml` is **`revoluzen-flow`**. Register the
publisher against the brand (`revoluzen`) instead and the upload is rejected
with `invalid-publisher` before a single byte moves — and the error does not
say which field is at fault.

This was a live trap until 2026-10-30: `RELEASING.md` had all three identity
fields carrying pre-rename values, and the note in `release.yml` named the
brand. Both are corrected, and `scripts/check_repo_coordinates.py` now fails
if the documented fields drift from `pyproject.toml` and `app_meta.py` again.
Trust the form in `RELEASING.md`, and confirm against the source:

```bash
python scripts/check_repo_coordinates.py
python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['name'])"
```

Both `revoluzen-flow` and `revoluzen` were unclaimed on PyPI as of 2026-10-30.
Claim the one the wheel declares.

Register on **pypi.org** — not test.pypi.org. They are separate sites with
separate accounts, and registering on the wrong one is the single most common
way this fails. A previous launch lost three attempts to exactly this.

| Field       | Value                 |
| ----------- | --------------------- |
| Project     | `revoluzen-flow`      |
| Owner       | `revoluzen-io`        |
| Repository  | `revoluzen-flow`      |
| Workflow    | `release.yml`         |
| Environment | *(leave blank)*       |

The environment field must be blank. The OIDC token this repository sends
carries no environment claim, and a publisher that expects one will not match.

### B3. Publishing is gated off

```bash
gh api repos/revoluzen-io/revoluzen-flow/actions/variables/PUBLISH_PYPI --jq .value
```

Must be `true`. The gate exists so an unconfigured PyPI leaves a release green
rather than failing every tag push. Set it only after B2 is done, or the first
tag will fail at the upload step.

### B4. What happens to incoming pull requests — settled for launch

`CONTRIBUTING.md` states that external pull requests are not being merged,
because `CLA.md` is a draft that has not had legal review. That was fine while
the repository was private, because nobody could send one. From the moment it
is public, people can and will.

**Resolved for 2026-11-01: the pause stands, and it now reads as a temporary
state with a way out.** Finishing a CLA requires legal review and is not a
launch-week task, so the only honest option was to make the pause navigable:

- `CONTRIBUTING.md` no longer ties the pause to a shipped version, says plainly
  that it is a paperwork problem rather than a judgement on anyone's code, and
  tells a reader how they will hear when it lifts (the section is deleted, the
  CHANGELOG carries it, Watch → Custom → Releases notifies them). It promises
  no date, because we do not have one.
- `maintainer-operations.md` §3 carries the reply template, so the duty
  maintainer is not improvising a refusal at the moment it matters.

**Still open as a business item, not a launch blocker:** legal review of
`CLA.md`, standing up the sign-off flow, and deciding whether a corporate
variant is needed. Until those land, every external pull request is work
someone did that we cannot take — which is a cost, and the argument for
prioritising the review.

Silence is the one option that was never available: a contributor who spends a
weekend on a pull request and then discovers you were never going to merge it
is a contributor you do not get back.

---

## 1. Pre-flight — the day before

### 1.1 Secret sweep of the whole history

Going public exposes every commit ever made, not just the current tree.

```bash
gitleaks detect --source . --config .gitleaks.toml --redact
```

A finding in history is **not** fixed by deleting the file in a new commit — the
blob stays reachable by SHA. It requires a history rewrite, or rotating the
exposed credential and accepting the exposure. Budget a day if this finds
anything.

### 1.2 Confirm what ships is what you think ships

```bash
python tools/package_preflight.py --strict
```

`--strict` fails on a staged frontend bundle older than the build it came from.
If it reports `STALE`, run `python scripts/stage_frontend.py` and re-check —
otherwise the wheel serves a UI from whenever the bundle was last staged.

Then build and look inside the artifact rather than trusting the build log:

```bash
python -m pip wheel --no-deps -w dist .
python tools/verify_wheel.py dist/revoluzen_flow-1.0.1-py3-none-any.whl
```

### 1.3 Run the artifact, do not just build it

A wheel that builds is not a wheel that works. Install it into a clean virtual
environment and serve it:

```bash
python -m venv /tmp/rzcheck && /tmp/rzcheck/bin/pip install dist/revoluzen_flow-1.0.1-py3-none-any.whl
/tmp/rzcheck/bin/revoluzen serve --port 8020
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8020/
```

Expect `200` and a first-run "Create first account" screen. A previous release
shipped a Docker image that built cleanly and died instantly on start, because
nothing had ever *run* it.

### 1.4 Check the public-facing README promises something true

The README's headline install is `pip install revoluzen-flow`. Until B2 and B3
are done that command fails for every reader. Either publish first, or say
plainly in the README that PyPI publication is imminent and give the working
install path.

---

## 2. Go-live sequence

Order matters. Publishing to PyPI works from a private repository — trusted
publishing does not care about visibility — so publish **first** and flip the
repository **after**. That way the install instruction is true the moment anyone
can read it.

1. **Tag the release.** The tag version must equal the `pyproject.toml`
   version or the guard in `release.yml` stops the run before anything is built.

   ```bash
   git tag -a v1.0.1 -m "Revoluzen Flow 1.0.1"
   git push <remote-url> v1.0.1
   ```

   On this machine a plain `git push` hangs on the credential helper; use the
   token-in-URL form documented in the team's push notes, and never `push -u`
   with a tokenised URL (it writes the token into `.git/config`).

2. **Watch the release run.** `release.yml` builds, stages the UI, verifies the
   wheel, publishes to PyPI, and cuts the GitHub release with the Linux
   installers.

   ```bash
   gh run watch --repo revoluzen-io/revoluzen-flow
   ```

   If the publish step fails with `invalid-publisher`, **nothing was uploaded**
   and the version is still claimable. Fix the publisher registration and
   `gh run rerun <id> --failed` — do not re-tag, and do not bump the version.

3. **Verify from the outside.**

   ```bash
   pip download --no-deps -d /tmp/rzpypi revoluzen-flow
   ```

   Then install that downloaded wheel in a clean environment and serve it, as in
   1.3. PyPI showing a page is not the same as the package working.

4. **Flip the repository public.** Settings → General → Danger Zone → Change
   visibility. Confirm immediately afterwards:

   ```bash
   gh api repos/revoluzen-io/revoluzen-flow --jq .private   # expect false
   ```

5. **Walk the public surface as a stranger would.** Open the repository signed
   out. Check the README renders, the licence is detected, the issue templates
   appear when you start a new issue, and the security policy is linked.

---

## 3. First 48 hours

The launch is not the end of the procedure. Assign a named person per day.

- **Watch the issue tracker.** The first issues set the tone publicly. See
  `maintainer-operations.md` for triage targets.
- **Watch the security inbox.** `SECURITY.md` commits to acknowledging a report
  within three business days. That clock starts the moment the repository is
  public. See `security-response.md`.
- **Expect install reports from corporate networks.** A TLS-inspecting proxy
  makes `pip install` fail with `CERTIFICATE_VERIFY_FAILED`. This is universal
  to pip, not specific to this project, and the answer is to trust the
  organisation's root CA or use a prebuilt image. Have the answer ready rather
  than debugging it live.
- **Do not ship a fix on day one unless it is severe.** A rushed patch release
  on launch day reads worse than the bug.

---

## 4. Rollback

Flipping the repository back to private does not unpublish anything — clones,
forks and caches persist. Treat going public as irreversible and get the
pre-flight right instead.

PyPI is also effectively irreversible: a yanked release stays visible and the
version number can never be reused. If a broken wheel ships, yank it and publish
a patch version. Do not attempt to re-upload the same version; PyPI refuses it.
