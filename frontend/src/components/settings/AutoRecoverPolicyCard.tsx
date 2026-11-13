import { useEffect, useState } from 'react';
import { api, type AutoRecoverPolicy } from '../../api/client';

/**
 * Admin control for fixes by policy (Settings, beside the publish policy).
 *
 * When an unattended run fails for a reason a retry can plausibly fix — a
 * timeout, a source that was down — Flow can resume it from its checkpoint
 * after a delay, a limited number of times, and record each attempt on the
 * pipeline's incident. It never edits the pipeline, and it never resumes a
 * data, configuration or crash failure, which would only fail again. Off by
 * default. Backed by GET/PUT /api/incidents/policy.
 */

const CLASS_TEXT: Record<string, string> = {
  transient: 'Passing problems (timeouts, rate limits)',
  dependency: 'Outside systems that were unreachable or refused the login',
  unknown: 'Failures Flow could not classify',
};

export default function AutoRecoverPolicyCard({ dark = false }: { dark?: boolean }) {
  const [policy, setPolicy] = useState<AutoRecoverPolicy | null>(null);
  const [allowed, setAllowed] = useState<string[]>([]);
  const [forbidden, setForbidden] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api.getAutoRecoverPolicy()
      .then((r) => { if (alive) { setPolicy(r.policy); setAllowed(r.allowed_classes); } })
      .catch((e: any) => { if (alive && /403|forbidden|admin/i.test(e?.message || '')) setForbidden(true); });
    return () => { alive = false; };
  }, []);

  if (forbidden || !policy) return null;

  const save = async (next: AutoRecoverPolicy) => {
    setSaving(true);
    setMsg(null);
    try {
      const r = await api.putAutoRecoverPolicy(next);
      setPolicy(r.policy);
      setMsg({ kind: 'ok', text: r.policy.enabled ? 'Saved. Matching failures will be resumed.' : 'Saved. Nothing is resumed automatically.' });
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || 'Save failed' });
    } finally {
      setSaving(false);
    }
  };

  const cardCls = dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200';
  const helpCls = `text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`;
  const labelCls = `text-xs ${dark ? 'text-slate-300' : 'text-slate-700'}`;
  const inputCls = `w-20 px-2 py-1 text-xs rounded-md border ${dark ? 'bg-slate-800 border-slate-600 text-white' : 'border-slate-300'}`;

  return (
    <div className={`rounded-xl border shadow-sm p-5 mt-6 ${cardCls}`}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className={`text-base font-bold ${dark ? 'text-white' : 'text-slate-800'}`}>Resume failed runs automatically</h3>
          <p className={`${helpCls} mt-1 max-w-xl`}>
            Off by default. When on, a scheduled, triggered or API run that fails for a reason a retry can fix is
            resumed from where it stopped, after a delay, a limited number of times. Each attempt is recorded on the
            pipeline's incident. The pipeline itself is never changed, and data, configuration and crash failures are
            never resumed.
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={policy.enabled}
          disabled={saving}
          onClick={() => save({ ...policy, enabled: !policy.enabled })}
          className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-60 ${
            policy.enabled ? 'bg-amber-500' : (dark ? 'bg-slate-600' : 'bg-slate-300')
          }`}
        >
          <span className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${policy.enabled ? 'translate-x-5' : 'translate-x-0.5'}`} />
        </button>
      </div>

      <div className="mt-4 grid sm:grid-cols-2 gap-3">
        <div className="space-y-1.5">
          {allowed.map((c) => (
            <label key={c} className={`flex items-center gap-2 ${labelCls}`}>
              <input
                type="checkbox"
                checked={policy.failure_classes.includes(c)}
                disabled={saving}
                onChange={(e) => save({
                  ...policy,
                  failure_classes: e.target.checked
                    ? [...policy.failure_classes, c]
                    : policy.failure_classes.filter((x) => x !== c),
                })}
              />
              {CLASS_TEXT[c] || c}
            </label>
          ))}
        </div>
        <div className="space-y-2">
          <label className={`flex items-center gap-2 ${labelCls}`}>
            <span className="w-40">Attempts per incident</span>
            <input type="number" min={1} max={5} className={inputCls} value={policy.max_attempts} disabled={saving}
              onChange={(e) => setPolicy({ ...policy, max_attempts: Number(e.target.value) || 1 })}
              onBlur={() => save(policy)} />
          </label>
          <label className={`flex items-center gap-2 ${labelCls}`}>
            <span className="w-40">Wait before each (minutes)</span>
            <input type="number" min={1} max={1440} className={inputCls} value={Math.round(policy.delay_seconds / 60)} disabled={saving}
              onChange={(e) => setPolicy({ ...policy, delay_seconds: Math.max(10, (Number(e.target.value) || 1) * 60) })}
              onBlur={() => save(policy)} />
          </label>
        </div>
      </div>

      {msg && (
        <div className={`mt-3 text-xs ${msg.kind === 'ok' ? (dark ? 'text-emerald-300' : 'text-emerald-600') : (dark ? 'text-red-300' : 'text-red-600')}`}>
          {msg.text}
        </div>
      )}
    </div>
  );
}
