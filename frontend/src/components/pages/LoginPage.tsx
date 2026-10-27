import { useState, useEffect, useCallback } from 'react';
import { api } from '../../api/client';
import { WORKSPACES_ENABLED, DEFAULT_WORKSPACE_ID } from '../../config/edition';
import { checkPassword, MIN_LENGTH, MIN_CLASSES, MAX_BYTES } from '../../auth/password';
import { APP_VERSION } from '../../appMeta';

interface LoginPageProps {
  onLogin: (user: any) => void;
}

export default function LoginPage({ onLogin }: LoginPageProps) {
  // Mode machine:
  //   login / register — the existing two tabs
  //   forgot           — "enter your email" forgot-password screen
  //   reset            — "pick a new password" screen, reached either
  //                      from the forgot screen (token came back in the
  //                      response body — no SMTP handoff) or from a URL
  //                      query string `?reset_token=...` when an admin
  //                      shared the reset link out-of-band.
  //   reset_done       — terminal success card pushing the user back to
  //                      the Sign In tab.
  const [mode, setMode] = useState<'login' | 'register' | 'forgot' | 'reset' | 'reset_done'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  // Forgot / reset-specific state. `resetToken` is the single-use token
  // minted by /forgot-password (or pasted in via URL). `resetEmail` is
  // the email bound to that token, shown read-only on the reset form
  // so the user knows which account they're resetting.
  const [forgotMessage, setForgotMessage] = useState('');
  const [resetToken, setResetToken] = useState('');
  const [resetEmail, setResetEmail] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newPasswordConfirm, setNewPasswordConfirm] = useState('');
  const [sso, setSso] = useState<{ enabled: boolean; provider_label: string }>({ enabled: false, provider_label: '' });
  // Signup policy — defaults to "register hidden", matching the server
  // (`allow_self_registration` defaults to False: Revoluzen Flow is a
  // single-operator install and the operator's account is seeded on first
  // boot). Defaulting to hidden means we never flash a Register tab that
  // the policy call is about to take away. `firstBootstrap` still wins
  // over the flag: if the user table is empty the tab always shows, so a
  // wiped data dir can still create its first account.
  const [signupAllowed, setSignupAllowed] = useState<boolean>(false);
  const [firstBootstrap, setFirstBootstrap] = useState<boolean>(false);
  // `bootstrap_password_pending` / `bootstrap_admin_email` are still
  // returned by /auth/signup-policy, but nothing on this screen renders
  // them any more — the card shows the sign-in / sign-up control and
  // nothing else. Read them again here if that ever changes.

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
    // Load password policy
    api.getPasswordPolicy().then(setPwPolicy).catch(() => {});

    // Check signup policy so we can hide the Register tab when the
    // admin has the instance in invite-only mode. This is a PUBLIC
    // endpoint — the login page can't attach a token to a request for
    // something the user isn't signed in for yet.
    api.getSignupPolicy()
      .then((p: any) => {
        setSignupAllowed(!!p?.allow_self_registration);
        setFirstBootstrap(!!p?.first_user_bootstrap);
        // Edge case: if the admin disabled self-signup AND the caller
        // happens to land on the Register tab from a stale URL state,
        // snap them back to Sign In so the form they see matches what
        // the server will actually accept.
        if (!p?.allow_self_registration && !p?.first_user_bootstrap) {
          setMode('login');
        }
      })
      .catch(() => {
        // Endpoint missing or unreachable — stay closed, matching the
        // server default. Showing a Register tab we can't back up would
        // send the operator into a form that 403s on submit.
        setSignupAllowed(false);
      });

    // Deep-link entry into the reset flow via query string, e.g.
    //   https://revoluzen.example/?reset_token=ABCDEF
    // This is the path an admin uses when they copy a reset link out
    // of the Auth Queue page and share it with the locked-out user.
    // We verify the token server-side before flipping into reset mode
    // so an expired / invalid link lands on the forgot screen with a
    // clear error instead of a blank form.
    const qs = new URLSearchParams(window.location.search);
    const urlToken = qs.get('reset_token');
    if (urlToken) {
      api.verifyResetToken(urlToken)
        .then((r: any) => {
          setResetToken(urlToken);
          setResetEmail(r?.email || '');
          setMode('reset');
          setError('');
          // Strip the token from the URL so refreshing the page
          // doesn't re-verify and doesn't leak the token in the
          // browser history / share sheet.
          window.history.replaceState(null, '', window.location.pathname);
        })
        .catch((err: any) => {
          setMode('forgot');
          setError(err?.message || 'Reset link is invalid or expired.');
          window.history.replaceState(null, '', window.location.pathname);
        });
    }

    // Handle OIDC callback — token in URL hash
    if (window.location.hash.includes('token=')) {
      const params = new URLSearchParams(window.location.hash.slice(1));
      const token = params.get('token');
      const emailParam = params.get('email');
      if (token) {
        localStorage.setItem('revoluzen_flow_token', token);
        window.history.replaceState(null, '', window.location.pathname);
        // Fetch user details with the new token
        api.getMe().then((user: any) => {
          localStorage.setItem('revoluzen_flow_user', JSON.stringify(user));
          onLogin(user);
        }).catch(() => {
          // Fallback — create minimal user shim from email
          const shim = { email: emailParam || '', name: emailParam || '' };
          localStorage.setItem('revoluzen_flow_user', JSON.stringify(shim));
          onLogin(shim);
        });
      }
    }
  }, []);

  // 2026-05-19 (P0 #7 of PAGE_BY_PAGE_AUDIT.md): when the no-SMTP backend
  // The decision about whether it is safe to show a reset token inline
  // belongs to the SERVER, and the client honours it.
  //
  // This used to be gated here on a build-time flag
  // (`VITE_REVOLUZEN_FLOW_FORGOT_TOKEN_INLINE=1`, off by default), which
  // broke the product's main deployment. The backend's `_inline_ok`
  // already returns a token only when the instance is single-user and
  // NOT network-exposed, or the operator opted in from loopback — and it
  // writes a message saying "You can use it below to set a new password".
  // The default build then discarded that token, so the user read "use it
  // below" above a card with nothing below it, under a "Check your email"
  // heading when no email had been sent. A dead end on the one install
  // that is supposed to work without any infrastructure.
  //
  // A build-time flag cannot know how many accounts exist, whether the
  // bind is open, or whether SMTP is configured, and one prebuilt binary
  // ships the same flag to everyone. The server knows all three. So: if a
  // token comes back, the server decided it was safe to show, and we show
  // it. If it withheld the token, there is nothing to show and the
  // message explains what to do instead.

  const handleForgot = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setForgotMessage('');
    setLoading(true);
    try {
      const r: any = await api.forgotPassword(email);
      setForgotMessage(r?.message || 'Request received.');
      // `smtp_sent` is not a field the API returns; the absence of a token
      // on a successful call means either SMTP delivered a link or the
      // server deliberately withheld it. Either way there is nothing for
      // this screen to offer, and `forgotMessage` says which it was.
      if (r?.reset_token) {
        // The server judged it safe to inline. Stash the token so the
        // follow-up CTA opens the reset form with it prefilled. We don't
        // auto-advance — the message is worth showing once.
        setResetToken(r.reset_token);
        setResetEmail(email);
      } else {
        // No token: emailed, withheld, or no such account. Clear any
        // stale state so the CTA can't offer to reset the wrong account.
        setResetToken('');
        setResetEmail('');
      }
    } catch (err: any) {
      setError(err?.message || 'Unable to submit request.');
    }
    setLoading(false);
  };

  // Live strength for the reset form. `resetEmail` is the address the
  // token belongs to, so the policy can reject a password containing it.
  const resetCheck = newPassword
    ? checkPassword(newPassword, resetEmail, '')
    : null;

  // Submit the reset-password form. The server re-validates and remains
  // the source of truth; checking here too means the user is told before
  // they submit, instead of after. The confirm-password mismatch is
  // client-side to avoid a pointless round-trip for a fat-finger.
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
      // Prefill the login tab with the email we just reset so the
      // user lands one-click away from signing in.
      setEmail(resetEmail);
      setPassword('');
      setNewPassword('');
      setNewPasswordConfirm('');
      setResetToken('');
    } catch (err: any) {
      // Weak-password errors come back as a structured dict in the
      // message. We print the raw text — the full policy checklist
      // can be wired in later as a dedicated component.
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

    // Email validation
    const emErr = validateEmail(email);
    if (emErr) { setEmailError(emErr); return; }

    // Register-mode: block submit if password policy fails
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
        result = await api.register(email, password, name);
      }
      localStorage.setItem('revoluzen_flow_token', result.token);
      localStorage.setItem('revoluzen_flow_user', JSON.stringify(result.user));
      // Schema v2: persist the user's workspace memberships and pick a
      // current workspace. Landing rules (order matters):
      //   1. The shared "default" workspace WHEN the user is a member of
      //      it. For a single-operator OSS install the operator belongs to
      //      "default", that's where the seeded + existing pipelines live,
      //      and it's where the backend's no-header fallback stamps new
      //      ones (auth/deps current_workspace_id → "default"). Landing
      //      anywhere else made real pipelines look like they'd vanished.
      //   2. Otherwise the user's Personal workspace — self-signed-up Plus
      //      users who aren't "default" members still get their own sandbox
      //      (unchanged behaviour for that path).
      //   3. Otherwise the first membership.
      // If the landing is ever wrong, the Sidebar workspace switcher (shown
      // whenever the user has >1 workspace, DEV or PROD) recovers it — a
      // user must never have data they can't reach. We never leave
      // revoluzen_flow_workspace_id unset because the api client reads it on every
      // request.
      // OSS is single-operator: always land in the one shared `default`
      // workspace (the block below only runs when workspaces are enabled in a
      // Plus build). This is what keeps pipelines from "vanishing" into an
      // empty Personal scope.
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

  return (
    <div className="h-screen w-screen flex items-center justify-center bg-gradient-to-br from-slate-50 to-amber-50">
      <div className="w-full max-w-md">
        {/* Logo */}
        <div className="text-center mb-10">
          <div className="w-16 h-16 rounded-2xl shadow-lg mx-auto mb-5 bg-white overflow-hidden">
            <img src="/revoluzen-flow-logo-mark.png" alt="Revoluzen Flow" className="w-full h-full object-cover" />
          </div>
          <h1 className="text-3xl font-bold text-slate-800">Revoluzen Flow</h1>
          {/* 2026-06-03 — tagline rewritten to match readme.md lead +
              the About page card. The login screen is the FIRST thing
              every new user sees; leading with "AI-native" set up the
              wrong expectation (this is a pipeline engine, not a chat
              product) and contradicted the sober v1.0 positioning. */}
          <p className="text-base text-slate-500 mt-2">Single-binary, local-first data pipeline engine</p>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl shadow-xl border border-slate-200 p-8">
          {/* ── FORGOT PASSWORD VIEW ────────────────────────────────────
              Standalone screen — no tabs, no SSO block. The user lands
              here from the "Forgot password?" link under the login
              form and leaves via either (a) the reset success card
              (`reset_done`) after completing the flow, or (b) the
              "Back to sign in" link, which re-snaps mode=login. */}
          {mode === 'forgot' && (
            <div>
              <div className="mb-6 text-center">
                <h2 className="text-lg font-bold text-slate-700">Forgot your password?</h2>
                <p className="text-xs text-slate-500 mt-1">
                  Enter the email on your account and we'll generate a reset link.
                </p>
              </div>

              {!forgotMessage ? (
                <form onSubmit={handleForgot} className="space-y-5">
                  <div>
                    <label className="text-sm font-semibold text-slate-600 mb-1.5 block">Email</label>
                    <input
                      type="email" value={email} onChange={e => setEmail(e.target.value)}
                      placeholder="you@example.com" required autoFocus
                      className="w-full px-4 py-3 text-sm border border-slate-200 rounded-xl focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none"
                    />
                  </div>
                  {error && (
                    <div className="bg-red-50 border border-red-200 text-red-600 text-sm font-medium px-4 py-2.5 rounded-xl">
                      {error}
                    </div>
                  )}
                  <button
                    type="submit" disabled={loading}
                    className="w-full py-3 text-white text-sm font-bold rounded-xl shadow-sm hover:shadow-md transition-all disabled:opacity-50"
                    style={{ background: 'linear-gradient(135deg, #F5A623, #D4880A)' }}
                  >
                    {loading ? 'Submitting...' : 'Send reset link'}
                  </button>
                </form>
              ) : (
                <div className="space-y-5">
                  {/* Two treatments share this slot:
                      - Token in body: the server judged inlining safe, so
                        show the "Set a new password now" CTA, which opens
                        the reset form prefilled with that token.
                      - No token: either SMTP delivered a link or the
                        server withheld it. This used to be headed "Check
                        your email" with an envelope glyph, which was a
                        guess — on the withheld path no email was ever
                        sent, so the card asserted something untrue. The
                        API message already distinguishes the cases
                        ("emailed to you" vs "not shown here, because
                        this instance is reachable over the network"), so
                        the message alone is shown and is always right. */}
                  {resetToken ? (
                    <>
                      <div className="bg-emerald-50 border border-emerald-200 text-emerald-700 text-sm px-4 py-3 rounded-xl">
                        {forgotMessage}
                      </div>
                      <button
                        onClick={() => { setMode('reset'); setError(''); }}
                        className="w-full py-3 text-white text-sm font-bold rounded-xl shadow-sm hover:shadow-md transition-all"
                        style={{ background: 'linear-gradient(135deg, #F5A623, #D4880A)' }}
                      >
                        Set a new password now
                      </button>
                    </>
                  ) : (
                    <div className="bg-emerald-50 border border-emerald-200 text-emerald-700 text-sm px-4 py-3 rounded-xl">
                      {forgotMessage}
                    </div>
                  )}
                </div>
              )}

              <button
                type="button"
                onClick={() => { setMode('login'); setError(''); setForgotMessage(''); }}
                className="mt-5 w-full text-xs text-slate-500 hover:text-slate-600 transition-colors"
              >
                ← Back to sign in
              </button>
            </div>
          )}

          {/* ── RESET PASSWORD VIEW ─────────────────────────────────────
              The form is reached either by clicking "Set a new password
              now" from the forgot success card, or by visiting the
              page with `?reset_token=…` in the URL. The email is
              shown read-only (not editable) because it's derived from
              the verified token and changing it would be a no-op. */}
          {mode === 'reset' && (
            <div>
              <div className="mb-6 text-center">
                <h2 className="text-lg font-bold text-slate-700">Choose a new password</h2>
                {resetEmail && (
                  <p className="text-xs text-slate-500 mt-1">
                    Resetting password for <span className="font-semibold text-slate-600">{resetEmail}</span>
                  </p>
                )}
              </div>

              <form onSubmit={handleReset} className="space-y-5">
                <div>
                  <label className="text-sm font-semibold text-slate-600 mb-1.5 block">New password</label>
                  <input
                    type="password" value={newPassword} onChange={e => setNewPassword(e.target.value)}
                    placeholder="••••••••" required autoFocus
                    minLength={MIN_LENGTH} maxLength={MAX_BYTES}
                    className="w-full px-4 py-3 text-sm border border-slate-200 rounded-xl focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none"
                  />
                  {/* Same treatment as the sign-up and change-password
                      forms — bar plus blockers only, never the optional
                      nudges. This was the one password field in the app
                      with no feedback at all, so someone resetting found
                      out their password was too weak only after the
                      server rejected it. Uses the local mirror rather
                      than the /check-password round-trip because this
                      screen is reached while signed out. */}
                  {!newPassword && (
                    <p className="text-xs text-slate-500 mt-1.5">
                      At least {MIN_LENGTH} characters, using {MIN_CLASSES} of:
                      lowercase, uppercase, a number, a symbol.
                    </p>
                  )}
                  {newPassword && resetCheck && (
                    <div className="mt-2 space-y-1.5">
                      <div className="flex items-center gap-2">
                        <div className="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                          <div
                            className={`h-full rounded-full transition-all ${
                              resetCheck.score >= 3 ? 'bg-emerald-500' : resetCheck.score >= 2 ? 'bg-amber-400' : 'bg-red-400'
                            }`}
                            style={{ width: `${Math.min(100, Math.max(8, (resetCheck.score / 4) * 100))}%` }}
                          />
                        </div>
                        <span className={`text-xs font-bold ${
                          resetCheck.score >= 3 ? 'text-emerald-600' : resetCheck.score >= 2 ? 'text-amber-600' : 'text-red-500'
                        }`}>{resetCheck.label}</span>
                      </div>
                      {newPassword.length >= 4 && resetCheck.failures.length > 0 && (
                        <ul className="space-y-0.5">
                          {resetCheck.failures.map((f, i) => (
                            <li key={i} className="flex items-start gap-1.5 text-xs text-red-500">
                              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" className="mt-[3px] shrink-0"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                              <span>{f}</span>
                            </li>
                          ))}
                        </ul>
                      )}
                      {newPassword.length >= 4 && resetCheck.failures.length === 0 && (
                        <p className="flex items-center gap-1.5 text-xs text-emerald-600">
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" className="shrink-0"><polyline points="20 6 9 17 4 12" /></svg>
                          <span>Looks good.</span>
                        </p>
                      )}
                    </div>
                  )}
                </div>
                <div>
                  <label className="text-sm font-semibold text-slate-600 mb-1.5 block">Confirm new password</label>
                  <input
                    type="password" value={newPasswordConfirm} onChange={e => setNewPasswordConfirm(e.target.value)}
                    placeholder="••••••••" required
                    minLength={MIN_LENGTH} maxLength={MAX_BYTES}
                    className="w-full px-4 py-3 text-sm border border-slate-200 rounded-xl focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none"
                  />
                </div>
                {error && (
                  <div className="bg-red-50 border border-red-200 text-red-600 text-sm font-medium px-4 py-2.5 rounded-xl">
                    {error}
                  </div>
                )}
                <button
                  type="submit" disabled={loading}
                  className="w-full py-3 text-white text-sm font-bold rounded-xl shadow-sm hover:shadow-md transition-all disabled:opacity-50"
                  style={{ background: 'linear-gradient(135deg, #F5A623, #D4880A)' }}
                >
                  {loading ? 'Resetting...' : 'Reset password'}
                </button>
              </form>

              <button
                type="button"
                onClick={() => { setMode('login'); setError(''); }}
                className="mt-5 w-full text-xs text-slate-500 hover:text-slate-600 transition-colors"
              >
                ← Cancel and go back to sign in
              </button>
            </div>
          )}

          {/* ── RESET DONE CARD ─────────────────────────────────────────
              Terminal screen after a successful self-serve reset. We
              keep the user on LoginPage rather than auto-logging them
              in because the new-session flow exercises the fresh
              password end-to-end and avoids any stale session state
              lingering from before the reset. */}
          {mode === 'reset_done' && (
            <div className="text-center space-y-5">
              <div className="mx-auto w-14 h-14 rounded-full bg-emerald-50 flex items-center justify-center">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#059669" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </div>
              <div>
                <h2 className="text-lg font-bold text-slate-700">Password reset</h2>
                <p className="text-xs text-slate-500 mt-1">
                  Your password has been updated. Please sign in with the new one.
                </p>
              </div>
              <button
                onClick={() => { setMode('login'); setError(''); setForgotMessage(''); }}
                className="w-full py-3 text-white text-sm font-bold rounded-xl shadow-sm hover:shadow-md transition-all"
                style={{ background: 'linear-gradient(135deg, #F5A623, #D4880A)' }}
              >
                Continue to sign in
              </button>
            </div>
          )}

          {/* ── LOGIN / REGISTER (existing) ─────────────────────────── */}
          {(mode === 'login' || mode === 'register') && (<>
          {/* Tabs — the Register tab is only shown when self-signup is
              permitted OR this is a brand-new install (first-user
              bootstrap). In strict invite-only mode we hide the tab
              strip entirely and show a plain "Sign In" heading, plus a
              small footer line pointing invitees at their admin. */}
          {(signupAllowed || firstBootstrap) ? (
            <div className="flex gap-1 bg-slate-100 rounded-xl p-1 mb-7">
              <button
                onClick={() => { setMode('login'); setError(''); }}
                className={`flex-1 py-2.5 text-sm font-semibold rounded-lg transition-all ${mode === 'login' ? 'bg-white text-slate-700 shadow-sm' : 'text-slate-500'}`}
              >
                Sign In
              </button>
              <button
                onClick={() => { setMode('register'); setError(''); }}
                className={`flex-1 py-2.5 text-sm font-semibold rounded-lg transition-all ${mode === 'register' ? 'bg-white text-slate-700 shadow-sm' : 'text-slate-500'}`}
              >
                {firstBootstrap ? 'Create first account' : 'Register'}
              </button>
            </div>
          ) : (
            <div className="mb-7 text-center">
              <h2 className="text-lg font-bold text-slate-700">Sign in</h2>
              {/* Heading only — no explanatory copy under it, by decision.
                  This slot previously held an edition statement ("runs as a
                  single operator") and then a first-run hint naming
                  INITIAL_ADMIN_PASSWORD.txt. Both are gone: the card shows
                  the sign-in / sign-up affordance and nothing else. The
                  first-run password is still written to that file in the
                  data folder and printed by the CLI on first boot, so the
                  information is not lost — it just isn't on this screen. */}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-5">
            {mode === 'register' && (
              <div>
                <label className="text-sm font-semibold text-slate-600 mb-1.5 block">Name</label>
                <input
                  type="text" value={name} onChange={e => setName(e.target.value)}
                  placeholder="Your name"
                  className="w-full px-4 py-3 text-sm border border-slate-200 rounded-xl focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none"
                />
              </div>
            )}
            <div>
              <label className="text-sm font-semibold text-slate-600 mb-1.5 block">Email <span className="text-red-400">*</span></label>
              <input
                type="email" value={email}
                onChange={e => { setEmail(e.target.value); setEmailError(''); }}
                onBlur={() => { if (mode === 'register') setEmailError(validateEmail(email)); }}
                placeholder="you@example.com" required autoFocus
                className={`w-full px-4 py-3 text-sm border rounded-xl focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none ${emailError ? 'border-red-300 bg-red-50/50' : 'border-slate-200'}`}
              />
              {emailError && (
                <p className="text-xs text-red-500 mt-1 font-medium">{emailError}</p>
              )}
            </div>
            <div>
              <label className="text-sm font-semibold text-slate-600 mb-1.5 block">Password <span className="text-red-400">*</span></label>
              <input
                type="password" value={password} onChange={e => setPassword(e.target.value)}
                placeholder="••••••••" required
                // Cap only when CREATING a password. The pre-2026-06-03
                // scheme was salted SHA-256, which had no length limit,
                // so an account from back then may legitimately have a
                // password longer than MAX_BYTES — capping the sign-in
                // field would lock them out of their own install.
                maxLength={mode === 'register' ? MAX_BYTES : undefined}
                className="w-full px-4 py-3 text-sm border border-slate-200 rounded-xl focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none"
              />
              {/* Password feedback — register mode only.
                  Deliberately NOT a checklist. We used to render every rule
                  the server advertises as a ✓/✗ row, with pass/fail decided
                  by a substring match against the failure text. That meant a
                  perfectly good passphrase showed a column of red crosses for
                  rules that are only suggestions, which is the single fastest
                  way to make someone abandon a sign-up form. The policy is
                  NIST SP 800-63B — length and a blocklist, no composition —
                  so the form states the rule once up front and then only
                  reports what genuinely blocks submit. */}
              {mode === 'register' && password.length === 0 && pwPolicy && (
                <p className="text-xs text-slate-500 mt-1.5">
                  At least {pwPolicy.min_length} characters, using {MIN_CLASSES} of:
                  lowercase, uppercase, a number, a symbol.
                </p>
              )}
              {mode === 'register' && password.length > 0 && (
                <div className="mt-2 space-y-1.5">
                  {/* Strength bar. score is 0..4 (see password_policy.py), so
                      the bar divides by 4 — dividing by 5 capped a perfect
                      password at 80% and never filled. */}
                  {pwCheck && (
                    <div className="flex items-center gap-2">
                      <div className="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full transition-all ${
                            pwCheck.score >= 3 ? 'bg-emerald-500' : pwCheck.score >= 2 ? 'bg-amber-400' : 'bg-red-400'
                          }`}
                          style={{ width: `${Math.min(100, Math.max(8, (pwCheck.score / 4) * 100))}%` }}
                        />
                      </div>
                      <span className={`text-xs font-bold ${
                        pwCheck.score >= 3 ? 'text-emerald-600' : pwCheck.score >= 2 ? 'text-amber-600' : 'text-red-500'
                      }`}>{pwCheck.label}</span>
                    </div>
                  )}
                  {/* Only real blockers, never the optional "add a symbol"
                      nudges — those live in pwCheck.suggestions and stay off
                      the form. Held back until the 4th character so the field
                      doesn't go red on the first keystroke. */}
                  {pwCheck && password.length >= 4 && pwCheck.failures.length > 0 && (
                    <ul className="space-y-0.5">
                      {pwCheck.failures.map((f, i) => (
                        <li key={i} className="flex items-start gap-1.5 text-xs text-red-500">
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" className="mt-[3px] shrink-0"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                          <span>{f}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  {pwCheck && password.length >= 4 && pwCheck.failures.length === 0 && (
                    <p className="flex items-center gap-1.5 text-xs text-emerald-600">
                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" className="shrink-0"><polyline points="20 6 9 17 4 12" /></svg>
                      <span>Looks good.</span>
                    </p>
                  )}
                </div>
              )}
            </div>

            {error && (
              <div className="bg-red-50 border border-red-200 text-red-600 text-sm font-medium px-4 py-2.5 rounded-xl">
                {error}
              </div>
            )}

            <button
              type="submit" disabled={loading}
              className="w-full py-3 text-white text-sm font-bold rounded-xl shadow-sm hover:shadow-md transition-all disabled:opacity-50"
              style={{ background: 'linear-gradient(135deg, #F5A623, #D4880A)' }}
            >
              {loading ? (
                <span className="flex items-center justify-center gap-2">
                  <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  {mode === 'login' ? 'Signing in...' : 'Creating account...'}
                </span>
              ) : (
                mode === 'login' ? 'Sign In' : 'Create Account'
              )}
            </button>
          </form>

          {mode === 'login' && (
            <div className="mt-3 text-center">
              <button
                type="button"
                onClick={() => {
                  setMode('forgot');
                  setError('');
                  setForgotMessage('');
                }}
                className="text-xs text-slate-500 hover:text-amber-600 transition-colors font-medium"
              >
                Forgot password?
              </button>
            </div>
          )}

          {sso.enabled && mode === 'login' && (
            <>
              <div className="flex items-center gap-2 my-4">
                <div className="flex-1 h-px bg-slate-200" />
                <span className="text-xs text-slate-500 font-semibold">OR</span>
                <div className="flex-1 h-px bg-slate-200" />
              </div>
              <a
                href="/api/auth/oidc/login"
                className="w-full py-2.5 text-sm font-bold rounded-xl border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 transition-all flex items-center justify-center gap-2"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></svg>
                Sign in with {sso.provider_label}
              </a>
            </>
          )}
          </>)}

        </div>

        <p className="text-center text-xs text-slate-500 mt-5">
          Revoluzen Flow v{APP_VERSION} · Local-first pipeline engine by Revoluzen
        </p>
      </div>
    </div>
  );
}
