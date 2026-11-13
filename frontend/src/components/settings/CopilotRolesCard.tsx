import { useEffect, useState } from 'react';
import { api } from '../../api/client';

/**
 * Copilot roles (2026-11-11): what the Copilot may do in this workspace.
 *
 * Four roles, each switched on or off by an admin: Discover (read what is
 * here), Build (draft and change pipelines), Test (check a draft) and Operate
 * (runs, incidents, starting and stopping runs). Switching a role off takes
 * its tools out of the Copilot's hands here, and the Copilot says so when
 * asked. Roles never let the Copilot do more than the person asking may do.
 * Backed by GET/PUT /api/v1/ai/roles (ai/roles.py).
 */

interface RoleRow {
  key: 'discover' | 'build' | 'test' | 'operate';
  label: string;
  does: string;
  enabled: boolean;
  tools: string[];
}

export default function CopilotRolesCard({ dark = false }: { dark?: boolean }) {
  const [roles, setRoles] = useState<RoleRow[] | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api.get<{ roles: RoleRow[] }>('/api/v1/ai/roles')
      .then((r) => { if (alive) setRoles(r.roles); })
      .catch(() => { if (alive) setRoles(null); });
    return () => { alive = false; };
  }, []);

  if (!roles) return null;

  const toggle = async (role: RoleRow) => {
    setSaving(role.key);
    setMsg(null);
    try {
      const r = await api.put<{ roles: RoleRow[] }>('/api/v1/ai/roles', { [role.key]: !role.enabled });
      setRoles(r.roles);
      setMsg({ kind: 'ok', text: `${role.label} is now ${role.enabled ? 'off' : 'on'} for the Copilot in this workspace.` });
    } catch (e: any) {
      setMsg({
        kind: 'err',
        text: /403|forbidden|admin/i.test(e?.message || '') ? 'Only an admin can change the Copilot roles.' : (e?.message || 'Save failed'),
      });
    } finally {
      setSaving(null);
    }
  };

  const cardCls = dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200';
  const helpCls = `text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`;

  return (
    <div className={`rounded-xl border shadow-sm p-5 mt-6 ${cardCls}`} data-testid="copilot-roles">
      <h3 className={`text-base font-bold ${dark ? 'text-white' : 'text-slate-800'}`}>Copilot roles</h3>
      <p className={`${helpCls} mt-1 max-w-xl`}>
        What the Copilot may do in this workspace. A role that is off takes its tools away from the Copilot here, and
        it says so when asked. The Copilot never does more than the person asking is allowed to do.
      </p>
      <ul className="mt-3 space-y-2">
        {roles.map((role) => (
          <li key={role.key} className={`flex items-start gap-3 rounded-lg border px-3 py-2 ${dark ? 'border-white/[0.08]' : 'border-slate-200'}`}>
            <div className="min-w-0 flex-1">
              <div className={`text-sm font-semibold ${dark ? 'text-slate-200' : 'text-slate-800'}`}>{role.label}</div>
              <div className={helpCls}>{role.does}</div>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={role.enabled}
              aria-label={`${role.label} role`}
              disabled={saving !== null}
              onClick={() => toggle(role)}
              className={`relative mt-1 inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-60 ${
                role.enabled ? 'bg-amber-500' : (dark ? 'bg-slate-600' : 'bg-slate-300')
              }`}
            >
              <span className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${role.enabled ? 'translate-x-5' : 'translate-x-0.5'}`} />
            </button>
          </li>
        ))}
      </ul>
      {msg && (
        <p className={`text-xs mt-2 ${msg.kind === 'ok' ? (dark ? 'text-emerald-400' : 'text-emerald-700') : 'text-red-600'}`}>{msg.text}</p>
      )}
    </div>
  );
}
