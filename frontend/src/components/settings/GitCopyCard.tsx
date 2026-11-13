import { useEffect, useState } from 'react';
import { api, type GitCopySettings, type GitCopySettingsInput } from '../../api/client';

/**
 * Admin setup for the copy of every published pipeline kept in the customer's
 * own Git repository. Each publish becomes a commit there, with readable files
 * and who did it and why. Passwords and keys are never included.
 */

const BLANK: GitCopySettingsInput = {
  enabled: false, repo_url: '', branch: 'main', folder: 'flow-pipelines', username: '', token: '',
};

export default function GitCopyCard({ dark = false }: { dark?: boolean }) {
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [saved, setSaved] = useState<GitCopySettings | null>(null);
  const [form, setForm] = useState<GitCopySettingsInput>(BLANK);
  const [busy, setBusy] = useState<'' | 'save' | 'test' | 'all'>('');
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const adopt = (s: GitCopySettings) => {
    setSaved(s);
    setForm({
      enabled: s.enabled, repo_url: s.repo_url, branch: s.branch || 'main',
      folder: s.folder, username: s.username, token: '',
    });
  };

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const s = await api.getGitCopySettings();
        if (alive) adopt(s);
      } catch (e: any) {
        if (alive && /403|forbidden|admin|requires role/i.test(e?.message || '')) setForbidden(true);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  if (forbidden || loading) return null;

  const set = <K extends keyof GitCopySettingsInput>(key: K, value: GitCopySettingsInput[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    setMsg(null);
  };

  const run = async (what: 'save' | 'test' | 'all', action: () => Promise<{ kind: 'ok' | 'err'; text: string }>) => {
    setBusy(what);
    setMsg(null);
    try {
      setMsg(await action());
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || 'That did not work.' });
    }
    setBusy('');
  };

  const save = () => run('save', async () => {
    adopt(await api.saveGitCopySettings(form));
    return {
      kind: 'ok',
      text: form.enabled ? 'Saved. Each publish now makes a commit in this repository.' : 'Saved. Copying to Git is off.',
    };
  });
  const test = () => run('test', async () => {
    const result = await api.testGitCopy(form);
    return { kind: result.ok ? 'ok' : 'err', text: result.message };
  });
  const saveAll = () => run('all', async () => {
    const { queued } = await api.saveAllToGit();
    return {
      kind: 'ok',
      text: queued === 0
        ? 'Nothing is published yet.'
        : `Copying ${queued} published pipeline${queued === 1 ? '' : 's'} to Git. Each one shows how it went on the Pipelines page.`,
    };
  });

  const cardCls = dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200';
  const helpCls = `text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`;
  const labelCls = `block text-xs font-semibold mb-1 ${dark ? 'text-slate-300' : 'text-slate-600'}`;
  const inputCls = `w-full px-3 py-2 text-sm rounded-lg border outline-none focus:ring-1 focus:ring-indigo-300 focus:border-indigo-400 ${
    dark ? 'bg-white/[0.04] border-white/[0.1] text-white' : 'bg-white border-slate-200 text-slate-800'
  }`;
  const secondaryCls = `px-3 py-2 text-xs font-semibold rounded-lg border disabled:opacity-50 ${
    dark ? 'border-white/[0.15] text-slate-200 hover:bg-white/[0.06]' : 'border-slate-300 text-slate-700 hover:bg-slate-50'
  }`;
  const working = busy !== '';

  return (
    <div className={`rounded-xl border shadow-sm p-5 mt-6 ${cardCls}`} data-testid="git-copy-card">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className={`text-base font-bold ${dark ? 'text-white' : 'text-slate-800'}`}>Keep a copy of published pipelines in Git</h3>
          <p className={`${helpCls} mt-1 max-w-2xl`}>
            Each publish becomes a commit in your own repository: the pipeline, what it depends on, and who
            published it and why. It stays safe outside Flow, and can be imported to put a pipeline back.
            Passwords and keys are never included.
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={form.enabled}
          aria-label="Keep a copy of published pipelines in Git"
          onClick={() => set('enabled', !form.enabled)}
          className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
            form.enabled ? 'bg-amber-500' : (dark ? 'bg-slate-600' : 'bg-slate-300')
          }`}
        >
          <span className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${form.enabled ? 'translate-x-5' : 'translate-x-0.5'}`} />
        </button>
      </div>

      {saved && !saved.git_installed && (
        <div className={`mt-3 text-xs rounded-lg px-3 py-2 ${dark ? 'bg-amber-500/10 text-amber-200' : 'bg-amber-50 text-amber-700'}`}>
          The git program is not installed on this server. Install Git, then come back here.
        </div>
      )}

      <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="md:col-span-2">
          <label htmlFor="git-repo" className={labelCls}>Repository address</label>
          <input id="git-repo" value={form.repo_url} onChange={(e) => set('repo_url', e.target.value)} placeholder="https://github.com/your-company/flow-pipelines.git" autoComplete="off" spellCheck={false} className={inputCls} />
        </div>
        <div>
          <label htmlFor="git-branch" className={labelCls}>Branch</label>
          <input id="git-branch" value={form.branch} onChange={(e) => set('branch', e.target.value)} placeholder="main" autoComplete="off" spellCheck={false} className={inputCls} />
        </div>
        <div>
          <label htmlFor="git-folder" className={labelCls}>Folder in the repository</label>
          <input id="git-folder" value={form.folder} onChange={(e) => set('folder', e.target.value)} placeholder="flow-pipelines" autoComplete="off" spellCheck={false} className={inputCls} />
        </div>
        <div>
          <label htmlFor="git-token" className={labelCls}>Access token</label>
          <input
            id="git-token"
            type="password"
            value={form.token}
            onChange={(e) => set('token', e.target.value)}
            placeholder={saved?.token_set ? 'Saved. Leave blank to keep it.' : 'A token that can write to this repository'}
            autoComplete="new-password"
            className={inputCls}
          />
        </div>
        <div>
          <label htmlFor="git-username" className={labelCls}>
            Username <span className={`font-normal ${dark ? 'text-slate-500' : 'text-slate-500'}`}>(only if your Git server needs one)</span>
          </label>
          <input id="git-username" value={form.username} onChange={(e) => set('username', e.target.value)} autoComplete="off" spellCheck={false} className={inputCls} />
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void save()}
          disabled={working}
          className="px-4 py-2 text-xs text-white font-semibold rounded-lg shadow-sm disabled:opacity-50"
          style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
        >
          {busy === 'save' ? 'Saving…' : 'Save'}
        </button>
        <button type="button" onClick={() => void test()} disabled={working || !form.repo_url.trim()} className={secondaryCls}>
          {busy === 'test' ? 'Connecting…' : 'Test connection'}
        </button>
        <button
          type="button"
          onClick={() => void saveAll()}
          disabled={working || !saved?.enabled}
          title={saved?.enabled ? 'Copy every pipeline that is already published' : 'Save the settings with the switch on first'}
          className={secondaryCls}
        >
          {busy === 'all' ? 'Copying…' : 'Copy everything already published'}
        </button>
      </div>

      {msg && (
        <div role="status" className={`mt-3 text-xs ${msg.kind === 'ok' ? (dark ? 'text-emerald-300' : 'text-emerald-600') : (dark ? 'text-red-300' : 'text-red-600')}`}>
          {msg.text}
        </div>
      )}
    </div>
  );
}
