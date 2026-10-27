"""Password policy — strength rules + a strong-password generator.

Single source of truth for "is this password acceptable?". Both the
register endpoint, the admin reset endpoint, and the (future) self-serve
change-password endpoint MUST call ``validate_password()`` so an attacker
can't bypass the rule by hitting the API directly with curl.

The frontend has a mirror of these rules in ``frontend/src/auth/password.ts``
that drives the live strength meter; the two implementations must agree
on what counts as a valid password or the UX becomes a broken trap (the
form says "Strong" but the server still says 422).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

# Minimum length we'll accept.
#
# 2026-10-27 — lowered 12 → 8. NIST SP 800-63B says verifiers SHALL
# require at least 8 characters and SHOULD allow at least 64, so the old
# 12 was stricter than it needed to be.
#
# The four all-or-nothing composition rules were dropped at the same time
# and then partly reinstated as MIN_CLASSES (any 2 of 4) by product
# decision later the same day — see the MIN_CLASSES comment for the
# trade-off that was weighed. This file therefore does NOT follow NIST on
# composition, and that divergence is deliberate and documented rather
# than accidental.
#
# What carries most of the security either way: the common-password
# blocklist, the email/name containment check, and the run/sequence
# checks, all of which NIST explicitly endorses.
MIN_LENGTH = 8

# How many of the four character classes (lowercase, uppercase, digit,
# symbol) a password must contain.
#
# 2026-10-27, product decision. The trade-off was put explicitly and this
# side was chosen, so the reasoning is recorded rather than re-argued:
#
#   For  — 8 characters of one class ("quietriv") is the weakest thing
#          the policy allowed, and two classes is a cheap floor that
#          costs the user almost nothing.
#   Against — it rejects "quiet river morning", which is far harder to
#          guess than "Summer24", which it accepts. NIST SP 800-63B says
#          verifiers SHALL NOT impose composition rules for exactly that
#          reason.
#
# Requiring ANY two rather than all four is what keeps this tolerable: no
# single class is mandatory, so nobody is forced into the `Password1!`
# shape that rules-based cracking models best. The blocklist, the
# email/name containment check and the run/sequence checks still do the
# heavy lifting.
MIN_CLASSES = 2

# Upper bound, in BYTES of UTF-8 — not characters.
#
# bcrypt (see auth/models.py) only ever reads the first 72 bytes of a
# password. Older bindings truncated silently; bcrypt 5.x raises
# ValueError instead, so without this rule a long passphrase reached
# `User.hash_password` and came back as a 500 from /register,
# /reset-password and /me/password. Nothing capped it and nothing told
# the user, while the sign-up form actively encourages passphrases
# ("a few ordinary words beat one hard-to-type scramble") — which is
# exactly how you end up over 72.
#
# Bytes, because that is what bcrypt counts: "café" is 5 bytes from 4
# characters, and an emoji is 4 bytes on its own. Checking len() would
# let a 70-character accented passphrase through and still 500.
#
# NIST SP 800-63B says verifiers SHOULD allow at least 64 characters,
# and 72 clears that. Raising it further means changing the hash.
MAX_BYTES = 72

# Embedded common-password blocklist. Lowercased; the validator
# lowercases the candidate before checking.
_COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "passw0rd", "p@ssword",
    "admin", "admin123", "administrator", "root", "rootroot",
    "letmein", "welcome", "welcome1", "qwerty", "qwerty123",
    "abc123", "123456", "12345678", "123456789", "1234567890",
    "iloveyou", "monkey", "dragon", "master", "shadow",
    "revoluzen_flow", "revoluzenflow123", "revoluzen-flow", "revoluzen", "pulse123",
    "changeme", "changeme123", "default", "default123",
    "test", "test123", "testing", "user", "user123",
    "super", "superman", "batman", "trustno1", "starwars",
})


@dataclass
class PasswordCheck:
    """Result of running ``validate_password``."""
    ok: bool
    score: int               # 0..4: weak, fair, good, strong, excellent
    label: str               # "Weak" | "Fair" | "Good" | "Strong" | "Excellent"
    failures: list[str]      # human-readable rule violations
    suggestions: list[str]   # concrete fix-it hints


def validate_password(password: str, *, email: str = "", name: str = "") -> PasswordCheck:
    """Run every rule in the password policy and return a structured result."""
    failures: list[str] = []
    suggestions: list[str] = []

    if not isinstance(password, str):
        return PasswordCheck(False, 0, "Weak",
                             ["Password must be a string."],
                             ["Type a password."])

    if len(password) < MIN_LENGTH:
        deficit = MIN_LENGTH - len(password)
        failures.append(f"Password must be at least {MIN_LENGTH} characters (need {deficit} more).")
        suggestions.append(f"Add {deficit} more character{'s' if deficit != 1 else ''}.")

    # Hard upper bound — see MAX_BYTES. Must be a failure, not a
    # suggestion: past this the hash call raises and the request 500s.
    # The limit is measured in bytes, but nobody types bytes — the user
    # gets one plain sentence. The byte/character distinction only shows
    # up for accented or emoji passwords, where "72 characters or fewer"
    # is still the right thing to do, just conservative. The reasoning
    # lives in the MAX_BYTES comment, not on the login screen.
    n_bytes = len(password.encode("utf-8"))
    if n_bytes > MAX_BYTES:
        failures.append(f"Password is too long. Use {MAX_BYTES} characters or fewer.")
        suggestions.append("Shorten it — a few words is plenty.")

    has_lower = any(c.islower() for c in password)
    has_upper = any(c.isupper() for c in password)
    has_digit = any(c.isdigit() for c in password)
    has_symbol = any(not c.isalnum() and not c.isspace() for c in password)

    # At least MIN_CLASSES of the four character types. No specific class
    # is mandatory — any two will do — so there is no forced trailing `!`.
    classes_passed = sum([has_lower, has_upper, has_digit, has_symbol])
    if classes_passed < MIN_CLASSES:
        failures.append(
            f"Use at least {MIN_CLASSES} of these: a lowercase letter, an "
            "uppercase letter, a number, a symbol."
        )
        suggestions.append("Adding a capital or a number is usually the easiest fix.")

    if password.lower() in _COMMON_PASSWORDS:
        failures.append("This password is on the common-passwords blocklist.")
        suggestions.append("Pick something less predictable — avoid dictionary words.")

    pw_lower = password.lower()
    if email:
        local_part = email.split("@", 1)[0].lower()
        if len(local_part) >= 4 and local_part in pw_lower:
            failures.append("Password contains your email address.")
            suggestions.append("Don't use your email or username inside the password.")
    if name:
        first = name.strip().split(" ", 1)[0].lower()
        if len(first) >= 4 and first in pw_lower:
            failures.append("Password contains your name.")
            suggestions.append("Don't use your name inside the password.")

    if _has_long_run(password, 4):
        failures.append("Avoid long runs of the same character (e.g. 'aaaa').")
        suggestions.append("Vary the characters — runs are easy to guess.")

    if _has_sequence(password, 5):
        failures.append("Avoid keyboard or alphabetic sequences (e.g. 'abcde', '12345').")
        suggestions.append("Break up sequences with random characters.")

    # Strength is driven by LENGTH, not by how many character classes the
    # password happens to contain. The old model was
    # `classes + length_bonus - 2`, which scored "quiet river morning" —
    # 19 characters, lowercase only — as 0, i.e. "Weak", while the rules
    # above accept it. Telling someone their good passphrase is weak is
    # what pushes them back to `Passw0rd!` (4 classes, 9 characters, on
    # every cracking list, and it scored "Good"). Length is the term that
    # actually tracks guessing cost, so it sets the floor and variety can
    # only nudge it up by one.
    n = len(password)
    if n >= 20:
        score = 4
    elif n >= 16:
        score = 3
    elif n >= 12:
        score = 2
    elif n >= MIN_LENGTH:
        score = 1
    else:
        score = 0
    # A mix of classes is worth something, but only as a bonus, and only
    # once the password is long enough for it to matter.
    if classes_passed >= 3 and n >= 10:
        score += 1
    score = min(4, score)
    # Anything that fails a rule cannot advertise itself as strong.
    if failures:
        score = 0
    label = ["Weak", "Fair", "Good", "Strong", "Excellent"][score]

    return PasswordCheck(
        ok=not failures,
        score=score,
        label=label,
        failures=failures,
        suggestions=suggestions,
    )


def _has_long_run(s: str, n: int) -> bool:
    if len(s) < n:
        return False
    run = 1
    for i in range(1, len(s)):
        if s[i] == s[i - 1]:
            run += 1
            if run >= n:
                return True
        else:
            run = 1
    return False


def _has_sequence(s: str, n: int) -> bool:
    if len(s) < n:
        return False
    s = s.lower()
    asc = 1
    desc = 1
    for i in range(1, len(s)):
        prev_o = ord(s[i - 1])
        cur_o = ord(s[i])
        if cur_o == prev_o + 1:
            asc += 1
            desc = 1
            if asc >= n:
                return True
        elif cur_o == prev_o - 1:
            desc += 1
            asc = 1
            if desc >= n:
                return True
        else:
            asc = 1
            desc = 1
    return False


_GEN_LOWER = "abcdefghjkmnpqrstuvwxyz"   # no l
_GEN_UPPER = "ABCDEFGHJKMNPQRSTUVWXYZ"   # no I, O
_GEN_DIGIT = "23456789"                  # no 0, 1
_GEN_SYMBOL = "!@#$%^&*-_=+?"


def generate_strong_password(length: int = 20) -> str:
    """Generate a random password that is guaranteed to pass ``validate_password``."""
    # Clamp to BOTH ends. The docstring promises the result passes
    # validate_password, so it has to respect MAX_BYTES too — the output
    # is ASCII, so one character is one byte here.
    length = max(length, MIN_LENGTH)
    length = min(length, MAX_BYTES)
    rng = secrets.SystemRandom()
    chars = [
        rng.choice(_GEN_LOWER),
        rng.choice(_GEN_UPPER),
        rng.choice(_GEN_DIGIT),
        rng.choice(_GEN_SYMBOL),
    ]
    pool = _GEN_LOWER + _GEN_UPPER + _GEN_DIGIT + _GEN_SYMBOL
    chars.extend(rng.choice(pool) for _ in range(length - len(chars)))
    rng.shuffle(chars)
    return "".join(chars)
