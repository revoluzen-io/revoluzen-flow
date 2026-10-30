# SOP — Responding to a security report

**Owner:** Revoluzen Private Limited, security responder on duty
**Applies to:** a report of an exploitable flaw in Revoluzen Flow, from the
moment it arrives until the advisory is published.

[`SECURITY.md`](../../SECURITY.md) tells a **reporter** what to do. This tells
**us** what to do, because the published policy commits to timelines that only
mean something if there is a procedure behind them:

| Stage              | Published target     |
| ------------------ | -------------------- |
| Acknowledgement    | 3 business days      |
| Initial assessment | 7 business days      |

That clock starts when the report arrives, not when someone notices it. Check
the reporting channels on every duty day.

> If `SECURITY.md` ever commits to a timeline the team cannot meet, change the
> policy rather than miss it. A missed published SLA is worse than a modest one.

---

## 1. On receipt — the first hour

**Do not discuss it anywhere public.** Not in an issue, not in a pull request,
not in a commit message. A public comment confirming a vulnerability is the
disclosure, regardless of whether a fix exists.

If the report arrived as a public issue, treat it as already disclosed: this
changes the timeline from "coordinated" to "race", and the fix becomes urgent
rather than scheduled. Remove the detail from public view, tell the reporter
why, and move the conversation to a private channel.

Then:

1. **Acknowledge.** A human reply, within the published window, saying the
   report was received and when they will hear next. This is the single most
   important step and it costs five minutes. Most reporters who go public early
   do so because they heard nothing.
2. **Open a private advisory** (GitHub Security Advisories) as the working
   record. Not an issue — advisories are private until published and become the
   disclosure artifact.
3. **Do not promise a severity or a fix date yet.** You have not assessed it.

---

## 2. Assessment

Reproduce it before you classify it. An unreproduced report is a hypothesis.

Work out, and write down in the advisory:

- **What an attacker gets.** Read data they should not see? Write? Execute?
  Nothing, because a precondition is unrealistic?
- **What they need first.** Network reach, an account, an admin account, a
  user's click. Revoluzen Flow is local-first and single-operator by default, so
  "requires an authenticated operator" is a materially different finding from
  "requires network reach" — but do not use that to wave a finding away. Many
  installs are on a shared network.
- **Which versions are affected**, and whether the current release is one.

Use the severity classification already published in `SECURITY.md` rather than
inventing one per report.

### If it is not a vulnerability

Say so, say why, and thank them anyway. "Out of scope" with no reasoning reads
as a brush-off and is how a dismissed reporter becomes a public blog post.
`SECURITY.md` has an out-of-scope list — point at the specific entry.

---

## 3. Fixing

- **Fix in private.** Develop on a private fork or an advisory's private branch.
  A commit titled "fix auth bypass" on a public repository discloses the flaw to
  anyone watching, days before the release.
- **Write the regression test first**, and make it fail. For a security fix the
  test is the thing that stops it coming back in a refactor two years from now.
- **Keep the fix minimal.** A security patch is not the time to refactor around
  it. Reviewers need to see the whole change at once.
- **Check whether the same mistake exists elsewhere.** A path-traversal in one
  handler is a question about every handler, not a bug in one.

---

## 4. Disclosure

Coordinate the timing with the reporter — `SECURITY.md` commits to coordinated
disclosure, which means they get told when it goes out and are not surprised by
it.

1. Publish the patched release first, so the advisory points at a version people
   can actually move to.
2. Publish the advisory, with: affected versions, the fixed version, severity,
   what an attacker could do, and whether there is a workaround for people who
   cannot upgrade today.
3. **Credit the reporter** by the name they ask for, or not at all if they
   prefer. Ask; do not assume.
4. Request a CVE if the finding warrants one.

Say plainly whether you know of exploitation in the wild. If you do not know,
say that you do not know — not that there is none.

---

## 5. Afterwards

Within a week of the advisory, write down — internally, not publicly:

- How it got in. Not who wrote it: what let it through. "A reviewer missed it"
  is not a cause; "we have no test that would have caught this class" is.
- Whether the same class of flaw can exist elsewhere in the codebase, and what
  was done about that.
- Whether the published timelines were met, and if not, which step was slow.

Fix the class, not just the instance. A repeated finding of the same shape is a
process defect, and the process is the thing this document exists to change.
