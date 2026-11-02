/**
 * The feedback shown under a field where someone is CHOOSING a password.
 *
 * Extracted because LoginPage rendered it twice — once under the sign-up
 * field (fed by the server's /check-password) and once under the reset
 * field (fed by the local `checkPassword` mirror, since that screen is
 * reached while signed out). The two copies had already drifted on the
 * divisor and the colour thresholds, which is exactly how a meter ends up
 * telling two different users different things about the same password.
 *
 * Deliberately NOT a checklist. We used to render every rule the server
 * advertises as a ✓/✗ row, with pass/fail decided by a substring match
 * against the failure text. That meant a perfectly good passphrase showed
 * a column of red crosses for rules that are only suggestions, which is
 * the fastest way to make someone abandon a sign-up form. The policy is
 * NIST SP 800-63B — length and a blocklist, no composition — so the form
 * states the rule once up front (`hint`) and then reports only what
 * genuinely blocks submit.
 *
 * `score` is 0..4 on both sides of the wire (see password_policy.py), so
 * the bar divides by 4. Dividing by 5 capped a perfect password at 80%
 * and the bar never filled.
 */
import type { ReactNode } from 'react';

/** The shape both the server check and the local mirror return. */
export interface PasswordVerdict {
  ok: boolean;
  score: number;
  label: string;
  failures: string[];
}

interface PasswordStrengthProps {
  /** The current field value. Nothing but `hint` renders while it's empty. */
  password: string;
  /** The verdict, or null while a debounced server check is in flight. */
  check: PasswordVerdict | null;
  /** The policy stated once, shown only while the field is untouched. */
  hint?: ReactNode;
  /**
   * Blockers stay hidden until this many characters, so the field doesn't
   * go red on the first keystroke of a password that will be fine.
   */
  revealAfter?: number;
}

export default function PasswordStrength({
  password,
  check,
  hint,
  revealAfter = 4,
}: PasswordStrengthProps) {
  if (!password) {
    return hint ? <p className="mt-1.5 text-xs text-slate-500">{hint}</p> : null;
  }
  if (!check) return null;

  const strong = check.score >= 3;
  const fair = check.score >= 2;
  const barTone = strong ? 'bg-emerald-500' : fair ? 'bg-amber-400' : 'bg-red-400';
  const textTone = strong ? 'text-emerald-600' : fair ? 'text-amber-600' : 'text-red-500';
  const settled = password.length >= revealAfter;

  return (
    <div className="mt-2 space-y-1.5">
      <div className="flex items-center gap-2">
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
          <div
            className={`h-full rounded-full transition-all ${barTone}`}
            style={{ width: `${Math.min(100, Math.max(8, (check.score / 4) * 100))}%` }}
          />
        </div>
        <span className={`text-xs font-bold ${textTone}`}>{check.label}</span>
      </div>

      {settled && check.failures.length > 0 && (
        <ul className="space-y-0.5">
          {check.failures.map((f, i) => (
            <li key={i} className="flex items-start gap-1.5 text-xs text-red-500">
              <svg
                width="10" height="10" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="3"
                className="mt-[3px] shrink-0" aria-hidden
              >
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
              <span>{f}</span>
            </li>
          ))}
        </ul>
      )}

      {settled && check.failures.length === 0 && (
        <p className="flex items-center gap-1.5 text-xs text-emerald-600">
          <svg
            width="10" height="10" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="3" className="shrink-0" aria-hidden
          >
            <polyline points="20 6 9 17 4 12" />
          </svg>
          <span>Looks good.</span>
        </p>
      )}
    </div>
  );
}
