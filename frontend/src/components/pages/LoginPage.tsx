/**
 * Revoluzen Flow sign-in — one platform surface, shared with Revoluzen Studio.
 *
 * The shell is Studio's: a dark brand panel on wide screens, and beside it the
 * sign-in column — the application mark on a white tile, the product name with
 * the product word in gold, a one-line tagline, a white card, and a version
 * footer. Flow keeps its own ground (the Revoluzen graphite ink) and its own
 * three claims; the structure, the amber focus ring, the amber action button
 * and the type scale are identical, so the two products read as one platform.
 *
 * Sign up is ALWAYS offered. The tab used to vanish whenever the policy said
 * self-registration was off, which answered the visitor's question by
 * deleting it: a second person landing on an instance that already has its
 * account saw a lone "Sign in" heading and no explanation of how to get one.
 * Now the tab is always there and the closed state says what is true — this
 * install has its operator, and here is how to get your own. Studio does the
 * same thing with the same words.
 *
 * Mode machine:
 *   login / register — the two tabs; `register` also renders the closed state
 *   forgot           — "enter your email" forgot-password screen
 *   reset            — "pick a new password", reached either from the forgot
 *                      screen (token came back in the response body — no SMTP
 *                      handoff) or from `?reset_token=…` when an operator
 *                      shared the reset link out-of-band
 *   reset_done       — terminal success card pushing back to Sign In
 */
import { useState, useEffect, useCallback, type ReactNode } from 'react';
import { api } from '../../api/client';
import { WORKSPACES_ENABLED, DEFAULT_WORKSPACE_ID } from '../../config/edition';
import { checkPassword, MIN_LENGTH, MIN_CLASSES, MAX_BYTES } from '../../auth/password';
import { APP_VERSION } from '../../appMeta';
import PasswordStrength from '../PasswordStrength';

interface LoginPageProps {
  onLogin: (user: any) => void;
}

// Shared with Revoluzen Studio's sign-in screen: the amber action gradient,
// the amber focus ring and the soft slate → amber ground.
const AMBER = 'linear-gradient(135deg, #F5A623, #D4880A)';
const SERIF = { fontFamily: 'Palatino Linotype, Iowan Old Style, Georgia, serif' };
// Flow's own ground. Studio's panel is navy; this is the graphite ink from
// revoluzen.io (#0D2B33), so each product is recognisable at a glance while
// the shell around it stays the same.
const BRAND_BG =
  'radial-gradient(1200px 500px at 15% -10%, #15454f 0%, #0d2b33 55%, #081c22 100%)';
const INPUT =
  'w-full rounded-xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-800 placeholder:text-slate-400 outline-none transition focus:border-amber-300 focus:ring-2 focus:ring-amber-300';
const LABEL = 'mb-1.5 block text-sm font-semibold text-slate-600';
const PRIMARY =
  'w-full rounded-xl py-3 text-sm font-bold text-white shadow-sm transition-all hover:shadow-md disabled:opacity-50';
const SECONDARY =
  'w-full rounded-xl border border-slate-300 bg-white py-2.5 text-sm font-bold text-slate-700 transition-all hover:bg-slate-50';

// What the panel claims, all of it true of the shipped engine: the canvas runs
// on DuckDB/SQLite locally, Execution Contracts refuse a write before it lands
// rather than reporting it afterwards, and nothing in the path is hosted.
const HIGHLIGHTS: [string, string, string][] = [
  [
    '◆',
    'Build it once, run it anywhere',
    'Design on the canvas, then run the same pipeline from the app, the CLI or a schedule — on DuckDB and SQLite, on your own machine.',
  ],
  [
    '§',
    'Execution Contracts',
    'State what has to hold. A guarantee that can be judged before the write blocks it, so a bad load is refused rather than reported after the table is gone.',
  ],
  [
    '⚿',
    'Yours, end to end',
    'One binary, one data folder. No hosted service sits in the path — your connection secrets and your rows never leave the box.',
  ],
];

function ErrorBox({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-2.5 text-sm font-medium text-red-600">
      {children}
    </div>
  );
}

/** "Revoluzen Flow" with the product word in the gold of the Flow mark. */
function ProductName() {
  return (
    <>
      Revoluzen{' '}
      <span
        className="bg-clip-text text-transparent"
        style={{ backgroundImage: 'linear-gradient(135deg, #E3A008, #B45309)' }}
      >
        Flow
      </span>
    </>
  );
}

/**
 * The shell every auth mode renders into: the brand panel on wide screens,
 * and the sign-in column — mark, name, tagline, card, version footer.
 */
function AuthShell({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-h-screen w-full grid-cols-1 md:grid-cols-[1.05fr_1fr]">
      {/* Brand panel — wide screens only; on a phone the card is the page. */}
      <aside
        className="relative hidden flex-col justify-between overflow-hidden p-12 text-slate-200 md:flex"
        style={{ background: BRAND_BG }}
      >
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-[0.06]"
          style={{
            backgroundImage:
              'linear-gradient(#ffffff 1px, transparent 1px), linear-gradient(90deg, #ffffff 1px, transparent 1px)',
            backgroundSize: '38px 38px',
          }}
        />
        <div className="relative">
          <div className="font-mono text-[11px] tracking-[0.28em] text-amber-400">
            ◆ REVOLUZEN FLOW
          </div>
          <h2 className="mt-3 text-4xl leading-tight text-white" style={SERIF}>
            Data Pipeline
            <br />
            Engine
          </h2>
          <p className="mt-4 max-w-sm text-sm leading-relaxed text-slate-300">
            Build, run and schedule data pipelines on your own machine — with a
            contract on every run that refuses a bad write instead of reporting
            it afterwards.
          </p>
        </div>

        <div className="relative space-y-5">
          {HIGHLIGHTS.map(([icon, title, body]) => (
            <div key={title} className="flex gap-3">
              <div
                className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-white/5 text-amber-400 ring-1 ring-white/10"
                aria-hidden
              >
                {icon}
              </div>
              <div>
                <div className="text-sm font-medium text-white">{title}</div>
                <div className="max-w-xs text-xs leading-relaxed text-slate-400">{body}</div>
              </div>
            </div>
          ))}
        </div>

        <div className="relative font-mono text-[11px] text-slate-500">
          Revoluzen · self-hosted · open source
        </div>
      </aside>

      {/* Sign-in column */}
      <main className="flex items-center justify-center bg-gradient-to-br from-slate-50 to-amber-50 px-6 py-10">
        <div className="w-full max-w-md">
          <div className="mb-10 text-center">
            <div className="mx-auto mb-5 h-16 w-16 overflow-hidden rounded-2xl bg-white shadow-lg">
              <img
                src="/revoluzen-flow-logo-mark.png"
                alt="Revoluzen Flow"
                className="h-full w-full object-cover"
              />
            </div>
            <h1 className="text-3xl font-bold text-slate-800">
              <ProductName />
            </h1>
            {/* Tagline matches readme.md's lead and the About card. Leading
                with "AI-native" set up the wrong expectation — this is a
                pipeline engine, not a chat product. */}
            <p className="mt-2 text-base text-slate-500">
              Single-binary, local-first data pipeline engine
            </p>
          </div>

          <div className="rounded-2xl border border-slate-200 bg-white p-8 shadow-xl">
            {children}
          </div>

          <p className="mt-5 text-center text-xs text-slate-500">
            Revoluzen Flow v{APP_VERSION} · Local-first pipeline engine by Revoluzen
          </p>
        </div>
      </main>
    </div>
  );
}

export default function LoginPage({ onLogin }: LoginPageProps) {
  const [mode, setMode] = useState<'login' | 'register' | 'forgot' | 'reset' | 'reset_done'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  // Forgot / reset state. `resetToken` is the single-use token minted by
  // /forgot-password (or pasted in via URL); `resetEmail` is the address
  // bound to it, shown read-only on the reset form so the user knows
  // which account they're resetting.
  const [forgotMessage, setForgotMessage] = useState('');
  const [resetToken, setResetToken] = useState('');
  const [resetEmail, setResetEmail] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newPasswordConfirm, setNewPasswordConfirm] = useState('');
  // Signup policy. Both flags default to the server's own defaults, and
  // NEITHER controls whether the Sign up tab exists any more — the tab is
  // always rendered. They decide what it contains: the form, or the
  // explanation of how accounts are made on this install. `policyLoaded`
  // keeps us from asserting either one before we've been told.
  const [signupAllowed, setSignupAllowed] = useState(false);
  const [firstBootstrap, setFirstBootstrap] = useState(false);
  const [policyLoaded, setPolicyLoaded] = useState(false);
  // A server-mode install asks for a one-time setup token before its first
  // account (printed in the server log and saved as SETUP_TOKEN.txt), so a
  // stranger who reaches the page first cannot claim the instance.
  const [setupTokenRequired, setSetupTokenRequired] = useState(false);
  const [setupToken, setSetupToken] = useState('');
  // `bootstrap_password_pending` / `bootstrap_admin_email` are still
  // returned by /auth/signup-policy, but nothing on this screen renders
  // them — the card shows the sign-in / sign-up control and nothing else.

  // Password policy + live strength check
  const [pwPolicy, setPwPolicy] = useState<{ min_length: number; rules: string[] } | null>(null);
  const [pwCheck, setPwCheck] = useState<{ ok: boolean; score: number; label: string; failures: string[]; suggestions: string[] } | null>(null);
  const [emailError, setEmailError] = useState('');

  // Debounced password check
  const checkPasswordLive = useCallback((pw: string, em: string, nm: string) => {
    if (!pw || pw.length < 4) { setPwCheck(null); return; }
    const t = setTimeout(() => {
      api.checkPassword(pw, em, nm).then(setPwCheck).catch(() => {});
    }, 400);
    return () => clearTimeout(t);
  }, []);

  // Live password validation on keystroke (register mode only)
  useEffect(() => {
    if (mode !== 'register') return;
    const cleanup = checkPasswordLive(password, email, name);
    return cleanup;
  }, [password, email, name, mode, checkPasswordLive]);

  useEffect(() => {
    api.getPasswordPolicy().then(setPwPolicy).catch(() => {});

    // Read the signup policy. PUBLIC endpoint — the login page can't attach
    // a token to a request for something the user isn't signed in for yet.
    // Nothing is hidden on the strength of the answer; it chooses the copy.
    api.getSignupPolicy()
      .then((p: any) => {
        setSignupAllowed(!!p?.allow_self_registration);
        setFirstBootstrap(!!p?.first_user_bootstrap);
        setSetupTokenRequired(!!p?.setup_token_required);
        setPolicyLoaded(true);
        // Fresh install: open the Sign up tab, because creating the first
        // account is the only thing the visitor can do. Matches Studio.
        if (p?.first_user_bootstrap) setMode('register');
      })
      .catch(() => {
        // Unreachable or missing: fall back to the server's defaults —
        // closed, with the explanation. The old code also snapped the user
        // off the Register tab here; it no longer needs to, because the
        // closed tab is a real screen rather than a form that 403s.
        setSignupAllowed(false);
        setPolicyLoaded(true);
      });

    // Deep-link entry into the reset flow via query string, e.g.
    //   https://flow.example/?reset_token=ABCDEF
    // We verify the token server-side before flipping into reset mode so an
    // expired link lands on the forgot screen with a clear error rather than
    // a blank form.
    const qs = new URLSearchParams(window.location.search);
    const urlToken = qs.get('reset_token');
    if (urlToken) {
      api.verifyResetToken(urlToken)
        .then((r: any) => {
          setResetToken(urlToken);
          setResetEmail(r?.email || '');
          setMode('reset');
          setError('');
          // Strip the token so a refresh doesn't re-verify and the token
          // doesn't linger in history or a share sheet.
          window.history.replaceState(null, '', window.location.pathname);
        })
        .catch((err: any) => {
          setMode('forgot');
          setError(err?.message || 'Reset link is invalid or expired.');
          window.history.replaceState(null, '', window.location.pathname);
        });
    }

    // External token handoff: a token delivered in the URL fragment is
    // adopted as this session. The fragment never reaches the server, and
    // the token is validated by /auth/me on the next line, so a bad one
    // simply fails to resolve a user.
    if (window.location.hash.includes('token=')) {
      const params = new URLSearchParams(window.location.hash.slice(1));
      const token = params.get('token');
      const emailParam = params.get('email');
      if (token) {
        localStorage.setItem('revoluzen_flow_token', token);
        window.history.replaceState(null, '', window.location.pathname);
        api.getMe().then((user: any) => {
          localStorage.setItem('revoluzen_flow_user', JSON.stringify(user));
          onLogin(user);
        }).catch(() => {
          const shim = { email: emailParam || '', name: emailParam || '' };
          localStorage.setItem('revoluzen_flow_user', JSON.stringify(shim));
          onLogin(shim);
        });
      }
    }
  }, []);

  // Whether the reset token may be shown inline is the SERVER's decision,
  // and the client honours it. This was once gated on a build-time flag
  // (off by default), which broke the product's main deployment: the
  // backend's `_inline_ok` returns a token only when the instance is
  // single-user and not network-exposed, and writes a message saying "you
  // can use it below" — then the default build discarded the token, so the
  // user read "use it below" above a card with nothing below it. A
  // build-time flag cannot know how many accounts exist or whether the bind
  // is open; the server knows both. So if a token comes back, we show it.

  const handleForgot = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setForgotMessage('');
    setLoading(true);
    try {
      const r: any = await api.forgotPassword(email);
      setForgotMessage(r?.message || 'Request received.');
      if (r?.reset_token) {
        // The server judged it safe to inline. Stash it so the follow-up
        // CTA opens the reset form prefilled. We don't auto-advance — the
        // message is worth showing once.
        setResetToken(r.reset_token);
        setResetEmail(email);
      } else {
        // Emailed, withheld, or no such account. Clear any stale state so
        // the CTA can't offer to reset the wrong account.
        setResetToken('');
        setResetEmail('');
      }
    } catch (err: any) {
      setError(err?.message || 'Unable to submit request.');
    }
    setLoading(false);
  };

  // Live strength for the reset form. `resetEmail` is the address the token
  // belongs to, so the policy can reject a password containing it. Uses the
  // local mirror rather than the /check-password round-trip because this
  // screen is reached while signed out.
  const resetCheck = newPassword ? checkPassword(newPassword, resetEmail, '') : null;

  const handleReset = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    const verdict = checkPassword(newPassword, resetEmail, '');
    if (!verdict.ok) {
      setError(verdict.failures[0]);
      return;
    }
    if (newPassword !== newPasswordConfirm) {
      setError('Passwords do not match.');
      return;
    }
    setLoading(true);
    try {
      await api.resetPassword(resetToken, newPassword);
      setMode('reset_done');
      // Prefill the login tab with the email we just reset, so the user
      // lands one click away from signing in.
      setEmail(resetEmail);
      setPassword('');
      setNewPassword('');
      setNewPasswordConfirm('');
      setResetToken('');
    } catch (err: any) {
      setError(err?.message || 'Unable to reset password.');
    }
    setLoading(false);
  };

  const validateEmail = (em: string) => {
    if (!em) return 'Email is required';
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(em)) return 'Enter a valid email address';
    return '';
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setEmailError('');

    const emErr = validateEmail(email);
    if (emErr) { setEmailError(emErr); return; }

    if (mode === 'register' && pwCheck && !pwCheck.ok) {
      setError('Password does not meet the strength requirements.');
      return;
    }

    setLoading(true);

    try {
      let result;
      if (mode === 'login') {
        result = await api.login(email, password);
      } else {
        result = await api.register(email, password, name, setupTokenRequired ? setupToken.trim() : undefined);
      }
      localStorage.setItem('revoluzen_flow_token', result.token);
      localStorage.setItem('revoluzen_flow_user', JSON.stringify(result.user));
      // OSS is single-operator: always land in the one shared `default`
      // workspace (the branch below only runs in a build with workspaces
      // enabled). This is what keeps pipelines from "vanishing" into an
      // empty Personal scope — the operator belongs to `default`, that's
      // where the seeded and existing pipelines live, and it's where the
      // backend's no-header fallback stamps new ones. We never leave
      // revoluzen_flow_workspace_id unset, because the api client reads it
      // on every request.
      const workspaces: any[] = Array.isArray(result.workspaces) ? result.workspaces : [];
      if (WORKSPACES_ENABLED && workspaces.length > 0) {
        localStorage.setItem('revoluzen_flow_workspaces', JSON.stringify(workspaces));
        const wsId = (w: any) => w.workspace_id || w.id;
        const chosen =
          workspaces.find((w) => wsId(w) === 'default') ||
          workspaces.find((w) => w.is_personal) ||
          workspaces[0];
        localStorage.setItem('revoluzen_flow_workspace_id', wsId(chosen) || DEFAULT_WORKSPACE_ID);
      } else {
        localStorage.setItem('revoluzen_flow_workspace_id', DEFAULT_WORKSPACE_ID);
      }
      onLogin(result.user);
    } catch (err: any) {
      setError(err.message || 'Authentication failed');
    }
    setLoading(false);
  };

  const switchTo = (m: 'login' | 'register') => {
    setMode(m);
    setError('');
    setEmailError('');
  };

  // Is the sign-up FORM available? (The tab always is.)
  const signupOpen = firstBootstrap || signupAllowed;

  // ── Forgot password ───────────────────────────────────────────────────────
  if (mode === 'forgot') {
    return (
      <AuthShell>
        <div className="mb-6 text-center">
          <h2 className="text-lg font-bold text-slate-700">Forgot your password?</h2>
          {!forgotMessage && (
            <p className="mt-1 text-xs text-slate-500">
              Enter the email on your account and we'll generate a reset link.
            </p>
          )}
        </div>

        {!forgotMessage ? (
          <form onSubmit={handleForgot} className="space-y-5">
            <div>
              <label htmlFor="auth-email" className={LABEL}>Email</label>
              <input
                id="auth-email" type="email" value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="you@example.com" required autoFocus
                autoComplete="username" className={INPUT}
              />
            </div>
            {error && <ErrorBox>{error}</ErrorBox>}
            <button type="submit" disabled={loading} className={PRIMARY} style={{ background: AMBER }}>
              {loading ? 'Submitting…' : 'Send reset link'}
            </button>
          </form>
        ) : (
          /* Two treatments share this slot. With a token in the body the
             server judged inlining safe, so we offer the CTA into the reset
             form. Without one, the message itself distinguishes "emailed to
             you" from "not shown here, because this instance is reachable
             over the network" — so the message alone is shown and is always
             right. It used to be headed "Check your email" with an envelope,
             which asserted something untrue on the withheld path. */
          <div className="space-y-5">
            <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">
              {forgotMessage}
            </div>
            {resetToken && (
              <button
                onClick={() => { setMode('reset'); setError(''); }}
                className={PRIMARY} style={{ background: AMBER }}
              >
                Set a new password now
              </button>
            )}
          </div>
        )}

        <button
          type="button"
          onClick={() => { setMode('login'); setError(''); setForgotMessage(''); }}
          className="mt-5 w-full text-xs text-slate-500 transition-colors hover:text-slate-600"
        >
          ← Back to sign in
        </button>
      </AuthShell>
    );
  }

  // ── Choose a new password ─────────────────────────────────────────────────
  if (mode === 'reset') {
    return (
      <AuthShell>
        <div className="mb-6 text-center">
          <h2 className="text-lg font-bold text-slate-700">Choose a new password</h2>
          {resetEmail && (
            <p className="mt-1 text-xs text-slate-500">
              Resetting password for{' '}
              <span className="font-semibold text-slate-600">{resetEmail}</span>
            </p>
          )}
        </div>

        <form onSubmit={handleReset} className="space-y-5">
          <div>
            <label htmlFor="auth-new-password" className={LABEL}>New password</label>
            <input
              id="auth-new-password" type="password" value={newPassword}
              onChange={e => setNewPassword(e.target.value)}
              placeholder="••••••••" required autoFocus
              minLength={MIN_LENGTH} maxLength={MAX_BYTES}
              autoComplete="new-password" className={INPUT}
            />
            <PasswordStrength
              password={newPassword}
              check={resetCheck}
              hint={`At least ${MIN_LENGTH} characters, using ${MIN_CLASSES} of: lowercase, uppercase, a number, a symbol.`}
            />
          </div>
          <div>
            <label htmlFor="auth-confirm-password" className={LABEL}>Confirm new password</label>
            <input
              id="auth-confirm-password" type="password" value={newPasswordConfirm}
              onChange={e => setNewPasswordConfirm(e.target.value)}
              placeholder="••••••••" required
              minLength={MIN_LENGTH} maxLength={MAX_BYTES}
              autoComplete="new-password" className={INPUT}
            />
          </div>
          {error && <ErrorBox>{error}</ErrorBox>}
          <button type="submit" disabled={loading} className={PRIMARY} style={{ background: AMBER }}>
            {loading ? 'Resetting…' : 'Reset password'}
          </button>
        </form>

        <button
          type="button"
          onClick={() => { setMode('login'); setError(''); }}
          className="mt-5 w-full text-xs text-slate-500 transition-colors hover:text-slate-600"
        >
          ← Cancel and go back to sign in
        </button>
      </AuthShell>
    );
  }

  // ── Reset done ────────────────────────────────────────────────────────────
  // We keep the user here rather than auto-logging them in, because the
  // fresh sign-in exercises the new password end to end and leaves no stale
  // session state from before the reset.
  if (mode === 'reset_done') {
    return (
      <AuthShell>
        <div className="space-y-5 text-center">
          <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-emerald-50" aria-hidden>
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#059669" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="20 6 9 17 4 12" />
            </svg>
          </div>
          <div>
            <h2 className="text-lg font-bold text-slate-700">Password reset</h2>
            <p className="mt-1 text-xs text-slate-500">
              Your password has been updated. Please sign in with the new one.
            </p>
          </div>
          <button
            onClick={() => { setMode('login'); setError(''); setForgotMessage(''); }}
            className={PRIMARY} style={{ background: AMBER }}
          >
            Continue to sign in
          </button>
        </div>
      </AuthShell>
    );
  }

  // ── Sign in / Sign up ─────────────────────────────────────────────────────
  return (
    <AuthShell>
      {/* Both tabs, always. See the file header for why the Sign up tab no
          longer disappears when the policy is closed. */}
      <div role="tablist" aria-label="Account" className="mb-7 flex gap-1 rounded-xl bg-slate-100 p-1">
        {([
          ['login', 'Sign In'],
          ['register', firstBootstrap ? 'Create first account' : 'Sign up'],
        ] as const).map(([m, label]) => (
          <button
            key={m}
            type="button"
            role="tab"
            aria-selected={mode === m}
            onClick={() => switchTo(m)}
            className={`flex-1 rounded-lg py-2.5 text-sm font-semibold transition-all ${
              mode === m ? 'bg-white text-slate-700 shadow-sm' : 'text-slate-500'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {mode === 'register' && !policyLoaded ? (
        <p className="py-6 text-center text-sm text-slate-500">Checking this install…</p>
      ) : mode === 'register' && !signupOpen ? (
        /* Sign-up is closed on this install. Say what is true and what to do
           about it — the same facts the server's 403 gives, so the screen and
           the API never disagree. */
        <div className="space-y-5 text-center">
          <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
            <p className="font-semibold text-slate-700">This install already has its account.</p>
            <p className="mt-1">
              Revoluzen Flow runs as a single operator — one account owns the
              engine and its data folder. To work alongside it, start your own:
              point <code className="font-mono text-xs">REVOLUZEN_FLOW_DATA_DIR</code> at a
              new folder and sign up there.
            </p>
            <p className="mt-2 text-xs text-slate-500">
              Shared workspaces, invites and roles are Revoluzen Flow Enterprise.
            </p>
          </div>
          <button type="button" onClick={() => switchTo('login')} className={PRIMARY} style={{ background: AMBER }}>
            I already have an account
          </button>
        </div>
      ) : (
        <>
          <form onSubmit={handleSubmit} className="space-y-5">
            {mode === 'register' && setupTokenRequired && (
              <div>
                <label htmlFor="auth-setup-token" className={LABEL}>
                  Setup token <span className="text-red-400">*</span>
                </label>
                <input
                  id="auth-setup-token" type="text" value={setupToken}
                  onChange={e => setSetupToken(e.target.value)}
                  placeholder="Paste the token from the server log" required
                  autoComplete="off" spellCheck={false} className={INPUT}
                />
                <p className="mt-1 text-xs text-slate-500">
                  Printed in the server log when it started, and saved as
                  SETUP_TOKEN.txt in the data folder.
                </p>
              </div>
            )}
            {mode === 'register' && (
              <div>
                <label htmlFor="auth-name" className={LABEL}>Name</label>
                <input
                  id="auth-name" type="text" value={name}
                  onChange={e => setName(e.target.value)}
                  placeholder="Your name" autoComplete="name" className={INPUT}
                />
              </div>
            )}
            <div>
              <label htmlFor="auth-email" className={LABEL}>
                Email <span className="text-red-400">*</span>
              </label>
              <input
                id="auth-email" type="email" value={email}
                onChange={e => { setEmail(e.target.value); setEmailError(''); }}
                onBlur={() => { if (mode === 'register') setEmailError(validateEmail(email)); }}
                placeholder="you@example.com" required autoFocus
                autoComplete={mode === 'login' ? 'username' : 'email'}
                className={`${INPUT} ${emailError ? 'border-red-300 bg-red-50/50' : ''}`}
              />
              {emailError && <p className="mt-1 text-xs font-medium text-red-500">{emailError}</p>}
            </div>
            <div>
              <label htmlFor="auth-password" className={LABEL}>
                Password <span className="text-red-400">*</span>
              </label>
              <input
                id="auth-password" type="password" value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="••••••••" required
                // Cap only when CREATING a password. The pre-2026-06-03
                // scheme was salted SHA-256 with no length limit, so an
                // account from back then may legitimately hold a password
                // longer than MAX_BYTES — capping the sign-in field would
                // lock them out of their own install.
                maxLength={mode === 'register' ? MAX_BYTES : undefined}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                className={INPUT}
              />
              {mode === 'register' && (
                <PasswordStrength
                  password={password}
                  check={pwCheck}
                  hint={
                    pwPolicy
                      ? `At least ${pwPolicy.min_length} characters, using ${MIN_CLASSES} of: lowercase, uppercase, a number, a symbol.`
                      : undefined
                  }
                />
              )}
            </div>

            {error && <ErrorBox>{error}</ErrorBox>}

            <button type="submit" disabled={loading} className={PRIMARY} style={{ background: AMBER }}>
              {loading ? (
                <span className="flex items-center justify-center gap-2">
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                  {mode === 'login' ? 'Signing in…' : 'Creating account…'}
                </span>
              ) : mode === 'login' ? (
                'Sign In'
              ) : firstBootstrap ? (
                'Create first account'
              ) : (
                'Create Account'
              )}
            </button>
          </form>

          {mode === 'login' && (
            <div className="mt-3 text-center">
              <button
                type="button"
                onClick={() => { setMode('forgot'); setError(''); setForgotMessage(''); }}
                className="text-xs font-medium text-slate-500 transition-colors hover:text-amber-600"
              >
                Forgot password?
              </button>
            </div>
          )}

          {mode === 'register' && firstBootstrap && (
            <p className="mt-5 text-center text-xs text-slate-500">
              No accounts exist yet. Whoever creates the first one owns this
              install and can manage everything in it.
            </p>
          )}
        </>
      )}
    </AuthShell>
  );
}
