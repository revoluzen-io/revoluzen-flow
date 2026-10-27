"""The password policy, as decided: 8–72 characters with at least 2 of
the 4 character classes.

The policy once had eight ways to fail: 12 characters plus one
lowercase, one uppercase, one digit and one symbol, plus the blocklist,
email/name containment, runs and sequences. That was cut back to NIST SP
800-63B (8 characters, no composition), and then the class requirement
was partly reinstated as `MIN_CLASSES = 2` by product decision on
2026-10-27.

This file therefore pins a policy that deliberately diverges from NIST
on one point. The trade-off is recorded in the `MIN_CLASSES` comment in
password_policy.py and is not re-argued here; these tests exist to stop
the rule drifting by accident. Requiring any TWO rather than all four is
the part that matters — no single class is mandatory, so the policy
never forces the `Password1!` shape.

What NIST does endorse is kept and tested here too: the common-password
blocklist, context containment, and run/sequence checks.

The frontend mirrors these rules to drive the strength meter. If the two
disagree the form either blocks a password the server accepts, or says
"Strong" and then the server returns 400 — so `MIN_LENGTH`, `MIN_CLASSES`
and `MAX_BYTES` are each asserted against the TypeScript constants.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from revoluzen_flow.auth.password_policy import (
    MAX_BYTES,
    MIN_CLASSES,
    MIN_LENGTH,
    validate_password,
)

pytestmark = pytest.mark.unit


def why(pw: str, **kw):
    r = validate_password(pw, **kw)
    return r.ok, r.failures


# ── Length is the rule that remains ──────────────────────────────────

def test_minimum_is_eight():
    assert MIN_LENGTH == 8, "NIST SP 800-63B floor for human-chosen passwords"


def test_seven_characters_is_rejected():
    ok, failures = why("Ab3!xyz")
    assert ok is False
    assert any("8 characters" in f for f in failures)


def test_eight_characters_is_accepted():
    ok, failures = why("tulip-rain-88")
    assert ok is True, f"rejected a reasonable password: {failures}"


# ── At least MIN_CLASSES of the four ─────────────────────────────────

def test_the_requirement_is_two_classes():
    assert MIN_CLASSES == 2


@pytest.mark.parametrize("pw", [
    "quietriverbankmorning",          # lowercase only, 21 chars
    "QUIETRIVERBANKMORNING",          # uppercase only
    "829174650382917465",             # digits only
])
def test_a_single_class_is_rejected_however_long(pw):
    """The rule the product decision asked for. Note the cost, recorded
    so nobody is surprised by it: the first of these is a genuinely
    hard-to-guess passphrase and it is refused."""
    ok, failures = why(pw)
    assert ok is False, f"{pw!r} should need a second character class"
    assert any("at least 2" in f.lower() for f in failures)


@pytest.mark.parametrize("pw", [
    "quietriver8",                    # lower + digit
    "QuietRiverBank",                 # lower + upper
    "quiet-river-bank",               # lower + symbol
    "tulip-rain-88",                  # lower + digit + symbol
    "Xk7@qR2z",                       # all four, at MIN_LENGTH
])
def test_two_or_more_classes_is_accepted(pw):
    ok, failures = why(pw)
    assert ok is True, f"{pw!r} should pass with 2+ classes: {failures}"


def test_no_individual_class_is_mandatory():
    """Any two will do. If some specific class were required, one of
    these would fail — and that is what forces `Password1!` on people."""
    for pw in ("quietriver8", "QuietRiverBank", "quiet-river-bank"):
        assert validate_password(pw).ok is True, (
            f"{pw!r} rejected — a specific class has become mandatory"
        )


# ── What still protects the account ──────────────────────────────────

def test_common_passwords_are_still_blocked():
    ok, failures = why("password")
    assert ok is False
    assert any("common" in f.lower() for f in failures)


def test_email_in_password_is_still_blocked():
    ok, failures = why("marianne-is-here", email="marianne@example.com")
    assert ok is False
    assert any("email" in f.lower() for f in failures)


def test_name_in_password_is_still_blocked():
    ok, failures = why("marianne-is-here", name="Marianne Webb")
    assert ok is False
    assert any("name" in f.lower() for f in failures)


def test_long_runs_are_still_blocked():
    ok, failures = why("aaaaquietriver")
    assert ok is False
    assert any("run" in f.lower() for f in failures)


def test_sequences_are_still_blocked():
    ok, failures = why("quiet12345river")
    assert ok is False
    assert any("sequence" in f.lower() for f in failures)


# ── Advertised policy must match enforcement ─────────────────────────

@pytest.mark.asyncio
async def test_endpoint_no_longer_advertises_composition():
    """A form listing requirements the server does not enforce is noise."""
    from revoluzen_flow.api.auth import password_policy_endpoint as password_policy
    p = await password_policy()
    assert p["min_length"] == MIN_LENGTH
    for key in ("require_lower", "require_upper", "require_digit", "require_symbol"):
        assert p[key] is False, f"{key} still advertised as required"
    assert p["block_common"] is True
    assert p["block_email_in_password"] is True


@pytest.mark.asyncio
async def test_every_advertised_rule_is_really_enforced():
    """Spot-check: each listed rule must correspond to a real rejection."""
    from revoluzen_flow.api.auth import password_policy_endpoint as password_policy
    rules = " ".join((await password_policy())["rules"]).lower()
    assert str(MIN_LENGTH) in rules
    assert str(MAX_BYTES) in rules, "the upper bound must be advertised too"
    assert "common" in rules
    assert "email" in rules


# ── Front end and back end must agree ────────────────────────────────

def test_frontend_mirror_uses_the_same_minimum():
    """Divergence here means the meter and the server disagree about
    what is valid — the exact trap the module docstring warns about."""
    ts = pathlib.Path(
        __file__
    ).parents[2] / "frontend" / "src" / "auth" / "password.ts"
    if not ts.exists():  # pragma: no cover
        pytest.skip(f"frontend mirror not found at {ts}")
    m = re.search(r"export const MIN_LENGTH\s*=\s*(\d+)", ts.read_text(encoding="utf-8"))
    assert m, "MIN_LENGTH not found in the frontend mirror"
    assert int(m.group(1)) == MIN_LENGTH, (
        f"frontend says {m.group(1)}, backend says {MIN_LENGTH}"
    )


def test_frontend_mirror_requires_no_specific_class():
    """Any two classes will do. If the mirror ever demanded a particular
    one, the form would block passwords the server accepts."""
    ts = pathlib.Path(
        __file__
    ).parents[2] / "frontend" / "src" / "auth" / "password.ts"
    if not ts.exists():  # pragma: no cover
        pytest.skip("frontend mirror not found")
    src = ts.read_text(encoding="utf-8")
    for phrase in ("Add at least one lowercase", "Add at least one uppercase",
                   "Add at least one number", "Add at least one symbol"):
        assert phrase not in src, (
            f"frontend pushes {phrase!r} as a FAILURE — that makes one "
            "specific class mandatory, which the server does not require"
        )


def test_frontend_mirror_uses_the_same_class_minimum():
    ts = pathlib.Path(
        __file__
    ).parents[2] / "frontend" / "src" / "auth" / "password.ts"
    if not ts.exists():  # pragma: no cover
        pytest.skip("frontend mirror not found")
    m = re.search(r"export const MIN_CLASSES\s*=\s*(\d+)", ts.read_text(encoding="utf-8"))
    assert m, "MIN_CLASSES not found in the frontend mirror"
    assert int(m.group(1)) == MIN_CLASSES, (
        f"frontend says {m.group(1)}, backend says {MIN_CLASSES}"
    )


# ── Strength score follows length, not character classes ─────────────────

@pytest.mark.parametrize("pw,expected_label", [
    ("Quiet river morning", "Strong"),              # 19 chars, 2 classes
    ("Correct horse battery staple", "Excellent"),  # 28 chars, 2 classes
    ("Trombone ridge", "Good"),                     # 14 chars, 2 classes
    ("tulip-ra", "Fair"),                           # exactly MIN_LENGTH
])
def test_long_passphrases_are_not_called_weak(pw, expected_label):
    """The bug this replaced: `classes + length_bonus - 2` scored a
    19-character passphrase as 0/"Weak" while the rules accepted it, so
    the form said "Looks good" and "Weak" at the same time.

    These all carry two classes now, since one is no longer allowed —
    the point being tested is that LENGTH drives the score, not how many
    classes are stacked on top."""
    r = validate_password(pw)
    assert r.ok is True, f"{pw!r} unexpectedly failed: {r.failures}"
    assert r.label == expected_label, (
        f"{pw!r} ({len(pw)} chars) scored {r.label}, expected {expected_label}"
    )


def test_short_four_class_password_does_not_outrank_a_long_passphrase():
    """`Passw0rd!` has all four classes and scored "Good" under the old
    model. It must not beat a longer, plainer password — both are valid
    under the policy, so this is purely about how they are ranked."""
    gaudy = validate_password("Xk7@qR2z")             # 8 chars, 4 classes
    plain = validate_password("Quiet river morning")  # 19 chars, 2 classes
    assert plain.ok and gaudy.ok, "both should be valid passwords"
    assert plain.score > gaudy.score, (
        f"length lost to composition: {plain.score} vs {gaudy.score}"
    )


def test_a_failing_password_never_scores_above_zero():
    r = validate_password("password")
    assert r.ok is False
    assert r.score == 0, "a rejected password must not show any strength"


def test_score_stays_in_range():
    for pw in ("a", "ab3!", "tulip-rain-88", "x" * 200):
        r = validate_password(pw)
        assert 0 <= r.score <= 4, f"{pw!r} scored out of range: {r.score}"


def test_frontend_mirror_scores_by_length_too():
    """Both meters must agree, so the TypeScript must have lost the
    `classes + lengthBonus - 2` formula as well."""
    ts = pathlib.Path(__file__).parents[2] / "frontend" / "src" / "auth" / "password.ts"
    if not ts.exists():  # pragma: no cover
        pytest.skip("frontend mirror not found")
    src = ts.read_text(encoding="utf-8")
    assert "raw - 2" not in src, "frontend still uses the class-counting score"
    assert "n >= 20" in src, "frontend score is not length-driven"


# ── Upper bound: bcrypt only reads 72 bytes ──────────────────────────────

def test_a_password_over_the_limit_is_rejected():
    """Before this rule, a 115-character passphrase passed validation and
    then 500'd: bcrypt 5.x raises ValueError past 72 bytes instead of
    truncating, and nothing capped the length on the way in."""
    ok, failures = why("correct horse battery staple " * 4)
    assert ok is False
    assert any("too long" in f.lower() for f in failures)


def at_limit() -> str:
    """Exactly MAX_BYTES of ASCII, with no run or sequence long enough to
    trip the other rules."""
    pw = ("quiet-river-" * 7)[:MAX_BYTES]
    assert len(pw.encode("utf-8")) == MAX_BYTES
    return pw


def test_exactly_the_limit_is_accepted():
    ok, failures = why(at_limit())
    assert ok is True, f"{MAX_BYTES} bytes should be allowed: {failures}"


def test_one_byte_over_the_limit_is_rejected():
    pw = at_limit() + "x"
    assert len(pw.encode("utf-8")) == MAX_BYTES + 1
    ok, failures = why(pw)
    assert ok is False
    assert any("too long" in f.lower() for f in failures)


def test_the_limit_counts_bytes_not_characters():
    """"café" is 5 bytes from 4 characters, and an emoji is 4 bytes.
    Counting len() would let this through and still 500 at the hash."""
    pw = "é" * 40                          # 40 characters, 80 bytes
    assert len(pw) < MAX_BYTES < len(pw.encode("utf-8"))
    ok, failures = why(pw)
    assert ok is False, "a 40-character password slipped past a 72-BYTE limit"
    assert any("too long" in f.lower() for f in failures)


def test_everything_the_validator_accepts_can_actually_be_hashed():
    """The property that matters: validate_password must not approve
    something `User.hash_password` then refuses."""
    from revoluzen_flow.auth.models import User

    candidates = [
        "tulip-rain-88",
        "correct horse battery staple",
        at_limit(),                          # exactly MAX_BYTES
        "é" * (MAX_BYTES // 2),              # MAX_BYTES bytes, half as many chars
    ]
    for pw in candidates:
        if not validate_password(pw).ok:
            continue
        User.hash_password(pw)               # must not raise


def test_the_generator_never_exceeds_the_limit():
    """"Generate" used to be capped at 128, so it could mint a password
    the validator rejects and the hash cannot store."""
    from revoluzen_flow.auth.password_policy import generate_strong_password
    from revoluzen_flow.auth.models import User

    for asked in (8, 20, 72, 128, 1000):
        pw = generate_strong_password(asked)
        assert len(pw.encode("utf-8")) <= MAX_BYTES, f"{asked} -> {len(pw)} bytes"
        assert validate_password(pw).ok, f"generated password fails policy: {pw!r}"
        User.hash_password(pw)


@pytest.mark.asyncio
async def test_endpoint_advertises_the_upper_bound():
    from revoluzen_flow.api.auth import password_policy_endpoint as password_policy
    p = await password_policy()
    assert p["max_bytes"] == MAX_BYTES


def test_frontend_mirror_has_the_same_upper_bound():
    ts = pathlib.Path(__file__).parents[2] / "frontend" / "src" / "auth" / "password.ts"
    if not ts.exists():  # pragma: no cover
        pytest.skip("frontend mirror not found")
    m = re.search(r"export const MAX_BYTES\s*=\s*(\d+)", ts.read_text(encoding="utf-8"))
    assert m, "MAX_BYTES not found in the frontend mirror"
    assert int(m.group(1)) == MAX_BYTES, (
        f"frontend says {m.group(1)}, backend says {MAX_BYTES}"
    )
