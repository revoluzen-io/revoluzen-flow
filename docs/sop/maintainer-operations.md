# SOP — Maintainer operations

**Owner:** Revoluzen Private Limited, maintainer on duty
**Applies to:** day-to-day running of the public `revoluzen-flow` repository —
issue triage, pull requests, support questions, and cutting releases.

This covers what the *project* does. What a **user** should do is in
[`../quickstart.md`](../quickstart.md) and [`../faq.md`](../faq.md); what a
**contributor** should do is in [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md).
This document does not repeat either — it describes the side of the exchange
that the company owns.

---

## 1. The duty rota

One named maintainer per week. The duty maintainer owns first response, not
resolution — a timely "we have seen this, here is what happens next" is the
deliverable. Resolution can be scheduled.

Record the rota wherever the company keeps its internal schedules. It does not
belong in this file, because the file is public.

---

## 2. Issue triage

**Target: every new issue gets a human response within two business days.**
That number is a promise to yourself, not to the reader — it is deliberately not
published, because an unmet published SLA is worse than none.

On first look, do three things in order:

1. **Is it a security report in the wrong place?** If an issue describes an
   exploitable flaw, do not discuss it in the thread. Delete the issue content
   or make it private, ask the reporter to use the channels in
   [`../../SECURITY.md`](../../SECURITY.md), and follow
   [`security-response.md`](security-response.md). A public comment confirming
   the vulnerability is itself the disclosure.
2. **Is it reproducible?** If the report lacks a version, platform and the steps
   that produced it, ask once, specifically, and label it as waiting. Close it
   only after a reminder and genuine silence — not on a timer.
3. **Label it so it can be found again.** At minimum: the area it touches and
   whether it is a defect or a request. Issues nobody can search are issues that
   get reported twice.

### What not to do

- Do not close an issue because it is old. Age is not a verdict. Close it
  because it is fixed, is not a defect, or the reporter has gone and nobody else
  has hit it — and say which.
- Do not answer "works for me". Ask for the one piece of information that would
  explain the difference.

---

## 3. Pull requests

**Read [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) first — it currently
states that external pull requests are not being merged** while the contributor
licence agreement is in draft. If that is still true, the procedure is:

1. Thank them concretely, naming what they actually did. They spent their time.
2. Explain the reason in one sentence — the CLA is not finalised, and merging
   without one leaves the provenance of their code unclear.
3. Tell them how they will hear when it changes: the section is deleted and the
   CHANGELOG says so, and **Watch → Custom → Releases** notifies them without
   checking back. Do not invent a date.
4. Leave the pull request **open** if the work will be wanted later. Closing it
   reads as a rejection of the work rather than a statement about paperwork.

Reply template — adapt the first line, never skip it:

> Thanks for this — *[name the actual change: "the Oracle connector retry
> handling", not "your contribution"]*.
>
> I can't merge it yet, and the reason is paperwork rather than the code:
> accepting external changes needs a Contributor License Agreement, and ours
> is still in legal review. Merging without one would leave the provenance of
> your work unclear, and asking you to sign an unreviewed agreement would be
> unfair to you.
>
> I'm leaving this open rather than closing it, so it's here when that lands.
> CONTRIBUTING.md is where the notice will disappear from, and the CHANGELOG
> will carry it — Watch → Custom → Releases will tell you without checking back.
> CLA.md lists what's still outstanding if you want to judge the timeline
> yourself.

If their change is a **bug fix for something currently broken**, that is a
different conversation: fix it independently, land it under your own
authorship, and say in the pull request that you have done so and why. Do not
quietly reimplement someone's work without telling them — they will see the
commit.

When the pause lifts, delete this section's caveat and the one in
`CONTRIBUTING.md` in the same change, so the two can never disagree.

### Review standards once PRs are open

- **The branch must be green.** Not "green except the flaky one" — if a check is
  unreliable, fix or remove the check.
- **A behaviour change needs a test that fails without it.** The test is the
  part that survives; the fix is the part that gets rewritten.
- **Ask for the reasoning, not just the change.** A patch whose author cannot
  say why it works is a patch nobody can maintain.

---

## 4. Support questions

Questions are not defects, and answering them in the issue tracker turns the
tracker into a help desk that drowns the defects.

- Point at the documentation if it answers the question. If it does not answer
  the question clearly, **fix the documentation and then point at it.** The
  second person to ask is the signal; the first is an anecdote.
- Recurring questions become FAQ entries. A question asked three times is a
  documentation defect.

### Known recurring answers

| Symptom | Cause | Answer |
| --- | --- | --- |
| `pip install` fails with `CERTIFICATE_VERIFY_FAILED` | A TLS-inspecting corporate proxy, not this project | Trust the organisation's root CA (`pip config set global.cert`, `REQUESTS_CA_BUNDLE`), or use a prebuilt image |
| Blank UI after install | The wheel was built without the frontend staged | `python scripts/stage_frontend.py` then rebuild; `package_preflight.py --strict` catches this before a release |
| Cannot sign in on a fresh install | By design — the first boot has no account | Register the first account; it becomes the operator |

---

## 5. Releases

The mechanics are in [`../../RELEASING.md`](../../RELEASING.md). This section
covers only the judgement around them.

- **A release is a promise that the artifact works.** Build-smoke is not
  run-smoke. Install the built wheel into a clean environment and serve it
  before tagging. A previous release shipped an image that built perfectly and
  died on start, because nothing had ever run it.
- **The tag must match `pyproject.toml`.** A guard in `release.yml` enforces
  this on tag pushes; do not work around it. A drifted tag publishes the wrong
  version to PyPI and advertises a different one on GitHub.
- **PyPI versions are one-shot.** A version number can never be reused, even
  after a yank. If a release is broken, patch forward.
- **If the publish step fails with `invalid-publisher`, nothing was uploaded.**
  The version is still claimable. Fix the trusted-publisher registration and
  re-run the failed job — do not re-tag and do not bump the version.

---

## 6. Dependencies

Dependabot is configured. Treat its pull requests as real work:

- **Security updates** go in promptly, and get the same run-the-artifact check
  as anything else.
- **Routine bumps** can be batched, but not ignored indefinitely. A dependency
  that is two years stale is a migration, not an update.
- **Do not merge a dependency bump on a red branch.** If the suite is failing
  for unrelated reasons, the bump is untested.

---

## 7. What the company must decide, and review

These are business decisions that this document cannot make. Review them at a
fixed interval and record the answer somewhere durable:

- Whether the contributor licence agreement is finalised, and when.
- Who has publish rights to PyPI and to the container registry, and whether that
  list is still correct.
- Whether the published security SLA in `SECURITY.md` is still one the team can
  actually meet. An SLA you miss is a liability; lower it rather than miss it.
- Whether the connector and node counts quoted in the README and on the website
  still match what ships. Two different numbers for the same thing is the fastest
  way to lose a reader's trust.
