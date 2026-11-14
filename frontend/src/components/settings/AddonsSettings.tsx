import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../../api/client';

/**
 * Settings > Add-ons (2026-11-11). Drivers some connectors need but a standard
 * install leaves out, because of the vendor's licence or native code. An admin
 * installs one here; it goes into this install's data folder and works without
 * a restart. The backend (connections/addon_installer.py) installs only the
 * drivers it lists, and nothing installed here can replace a package Revoluzen
 * Flow ships.
 */

interface AddonJob {
  state: 'running' | 'done' | 'failed';
  source: string;
  message: string;
  log: string;
  version?: string | null;
}

interface AddonPackage {
  package: string;
  label: string;
  types: string[];
  licence: string;
  licence_url: string;
  version: string | null;
  uploaded_wheels: string[];
  job: AddonJob | null;
}

interface AddonStatus {
  installs_allowed: boolean;
  pip_available: boolean;
  folder: string;
  folder_active: boolean;
  python: string;
  packages: AddonPackage[];
  running: string | null;
}

const TYPE_NAMES: Record<string, string> = {
  db2: 'IBM Db2', informix: 'Informix', sap_hana: 'SAP HANA', teradata: 'Teradata',
  kafka: 'Kafka', trino: 'Trino', presto: 'Presto',
};

export default function AddonsSettings({ dark = false }: { dark?: boolean }) {
  const [status, setStatus] = useState<AddonStatus | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState('');
  const [accepted, setAccepted] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState('');
  const [notes, setNotes] = useState<Record<string, { kind: 'ok' | 'err'; text: string }>>({});
  const timer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await api.get<AddonStatus>('/api/addons');
      setStatus(next);
      setError('');
    } catch (e: any) {
      const text = e?.message || '';
      if (/403|forbidden|admin|requires role/i.test(text)) setForbidden(true);
      else setError(text || 'Could not load the add-ons.');
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  // While an install runs, check on it every two seconds.
  useEffect(() => {
    if (timer.current) window.clearTimeout(timer.current);
    if (status?.running) timer.current = window.setTimeout(() => { void refresh(); }, 2000);
    return () => { if (timer.current) window.clearTimeout(timer.current); };
  }, [status, refresh]);

  const note = (pkg: string, kind: 'ok' | 'err', text: string) => setNotes((n) => ({ ...n, [pkg]: { kind, text } }));

  const install = async (pkg: AddonPackage, fromUploads: boolean) => {
    setBusy(pkg.package);
    setNotes((n) => { const { [pkg.package]: _drop, ...rest } = n; return rest; });
    try {
      await api.post(`/api/addons/${encodeURIComponent(pkg.package)}/install`, {
        accept_licence: !!accepted[pkg.package], from_uploads: fromUploads,
      });
      await refresh();
    } catch (e: any) {
      note(pkg.package, 'err', e?.message || 'The install could not start.');
    }
    setBusy('');
  };

  const upload = async (pkg: AddonPackage, files: FileList | null) => {
    if (!files || files.length === 0) return;
    setBusy(pkg.package);
    try {
      const form = new FormData();
      Array.from(files).forEach((f) => form.append('files', f));
      const r = await api.postRaw<{ saved: string[] }>(`/api/addons/${encodeURIComponent(pkg.package)}/wheels`, form);
      note(pkg.package, 'ok', `Uploaded ${r.saved.length} file${r.saved.length === 1 ? '' : 's'}.`);
      await refresh();
    } catch (e: any) {
      note(pkg.package, 'err', e?.message || 'The upload did not work.');
    }
    setBusy('');
  };

  const clearUploads = async (pkg: AddonPackage) => {
    setBusy(pkg.package);
    try {
      await api.delete(`/api/addons/${encodeURIComponent(pkg.package)}/wheels`);
      await refresh();
    } catch (e: any) {
      note(pkg.package, 'err', e?.message || 'The files could not be removed.');
    }
    setBusy('');
  };

  const cardCls = dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200';
  const helpCls = `text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`;
  const titleCls = `text-sm font-bold ${dark ? 'text-white' : 'text-slate-800'}`;
  const secondaryCls = `px-3 py-2 text-xs font-semibold rounded-lg border disabled:opacity-50 ${
    dark ? 'border-white/[0.15] text-slate-200 hover:bg-white/[0.06]' : 'border-slate-300 text-slate-700 hover:bg-slate-50'
  }`;
  const warnCls = `text-xs rounded-lg px-3 py-2 ${dark ? 'bg-amber-500/10 text-amber-200' : 'bg-amber-50 text-amber-700'}`;

  if (forbidden) {
    return (
      <div className={`rounded-xl border shadow-sm p-5 ${cardCls}`} data-testid="addons-settings">
        <h3 className={titleCls}>Add-ons</h3>
        <p className={`${helpCls} mt-1`}>Only an admin can add drivers.</p>
      </div>
    );
  }

  const canInstall = !!status && status.installs_allowed && status.pip_available;

  return (
    <div className="space-y-4" data-testid="addons-settings">
      <div className={`rounded-xl border shadow-sm p-5 ${cardCls}`}>
        <h3 className={`text-base font-bold ${dark ? 'text-white' : 'text-slate-800'}`}>Drivers for more connectors</h3>
        <p className={`${helpCls} mt-1 max-w-3xl`}>
          Some connectors need a driver from the database vendor that a standard install leaves out, because the
          vendor sets its own licence terms. Add one here and the connector works straight away, with no restart.
          Drivers are installed into this install's data folder, so they stay there across upgrades.
        </p>
        {error && <div className={`${warnCls} mt-3`}>{error}</div>}
        {status && !status.installs_allowed && (
          <div className={`${warnCls} mt-3`}>
            Installing add-ons is turned off on this server. Ask whoever runs Revoluzen Flow to add the driver you need.
          </div>
        )}
        {status && status.installs_allowed && !status.pip_available && (
          <div className={`${warnCls} mt-3`}>
            This install has no package installer, so drivers can't be added from here. Ask whoever set up Revoluzen Flow.
          </div>
        )}
        {status && (
          <p className={`${helpCls} mt-3`}>
            Installed into <code className="font-mono">{status.folder}</code> (Python {status.python}).
            To remove a driver, delete its folder there and restart Revoluzen Flow.
          </p>
        )}
      </div>

      {status?.packages.map((pkg) => {
        const job = pkg.job;
        const running = job?.state === 'running';
        const someoneRunning = !!status.running;
        const working = busy === pkg.package || running;
        const uses = pkg.types.map((t) => TYPE_NAMES[t] || t).join(', ');
        const msg = notes[pkg.package];
        return (
          <div key={pkg.package} className={`rounded-xl border shadow-sm p-5 ${cardCls}`} data-testid={`addon-${pkg.package}`}>
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <h4 className={titleCls}>{pkg.label}</h4>
                <p className={`${helpCls} mt-0.5`}>For the {uses} connector{pkg.types.length === 1 ? '' : 's'}.</p>
              </div>
              <span className={`text-xs font-semibold whitespace-nowrap ${
                pkg.version ? (dark ? 'text-emerald-300' : 'text-emerald-600') : (dark ? 'text-slate-400' : 'text-slate-500')
              }`}>
                {pkg.version ? `Installed${pkg.version !== 'installed' ? ` (${pkg.version})` : ''}` : 'Not installed'}
              </span>
            </div>

            <p className={`${helpCls} mt-3`}>
              Licence: {pkg.licence}{' '}
              <a href={pkg.licence_url} target="_blank" rel="noopener noreferrer" className="font-semibold underline">Read the terms</a>
            </p>

            {canInstall && (
              <>
                <label className={`mt-3 flex items-center gap-2 text-xs ${dark ? 'text-slate-300' : 'text-slate-700'}`}>
                  <input
                    type="checkbox"
                    checked={!!accepted[pkg.package]}
                    onChange={(e) => setAccepted((a) => ({ ...a, [pkg.package]: e.target.checked }))}
                  />
                  I accept the vendor's licence terms for this driver
                </label>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void install(pkg, false)}
                    disabled={working || someoneRunning || !accepted[pkg.package]}
                    className="px-4 py-2 text-xs text-white font-semibold rounded-lg shadow-sm disabled:opacity-50"
                    style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
                  >
                    {running ? 'Installing…' : pkg.version ? 'Install again' : 'Install'}
                  </button>
                  <label className={`${secondaryCls} cursor-pointer ${working ? 'pointer-events-none opacity-50' : ''}`}>
                    Upload wheel files…
                    <input
                      type="file"
                      accept=".whl"
                      multiple
                      className="hidden"
                      onChange={(e) => { void upload(pkg, e.target.files); e.target.value = ''; }}
                    />
                  </label>
                  {pkg.uploaded_wheels.length > 0 && (
                    <>
                      <button
                        type="button"
                        onClick={() => void install(pkg, true)}
                        disabled={working || someoneRunning || !accepted[pkg.package]}
                        className={secondaryCls}
                      >
                        Install from uploaded files
                      </button>
                      <button type="button" onClick={() => void clearUploads(pkg)} disabled={working} className={secondaryCls}>
                        Remove uploaded files
                      </button>
                    </>
                  )}
                </div>
                {pkg.uploaded_wheels.length > 0 && (
                  <p className={`${helpCls} mt-2`}>Uploaded: {pkg.uploaded_wheels.join(', ')}</p>
                )}
                <p className={`${helpCls} mt-2`}>
                  No internet on this machine? Upload the driver's .whl files, and any it needs, then install from them.
                </p>
              </>
            )}

            {running && (
              <div role="status" className={`mt-3 text-xs ${dark ? 'text-slate-300' : 'text-slate-600'}`}>
                Installing… this can take a few minutes.
              </div>
            )}
            {job && job.state !== 'running' && (
              <div role="status" className={`mt-3 text-xs ${job.state === 'done' ? (dark ? 'text-emerald-300' : 'text-emerald-600') : (dark ? 'text-red-300' : 'text-red-600')}`}>
                {job.message}
                {job.state === 'failed' && job.log && (
                  <details className="mt-2">
                    <summary className="cursor-pointer font-semibold">Details</summary>
                    <pre className={`mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded p-2 text-[11px] ${dark ? 'bg-black/30 text-slate-300' : 'bg-slate-50 text-slate-700'}`}>{job.log}</pre>
                  </details>
                )}
              </div>
            )}
            {msg && (
              <div role="status" className={`mt-2 text-xs ${msg.kind === 'ok' ? (dark ? 'text-emerald-300' : 'text-emerald-600') : (dark ? 'text-red-300' : 'text-red-600')}`}>
                {msg.text}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
