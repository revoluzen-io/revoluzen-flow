/**
 * Business glossary (Insights > Glossary, 2026-11-11).
 *
 * The workspace's own terms ("active customer") and metrics ("net revenue"),
 * written once by a person. When a request to the Copilot, or a pipeline
 * draft, uses one of these words or another name for it, the definition, the
 * SQL and the columns written here go with the request, so the draft uses
 * this business's meaning instead of a guess. Backend: glossary/store.py.
 */

import { useEffect, useState } from 'react';
import { api, type GlossaryTerm } from '../../api/client';
import { toast } from '../Toast';

type Draft = Omit<GlossaryTerm, 'id' | 'updated_at' | 'updated_by'> & { id?: string };

const EMPTY: Draft = { name: '', kind: 'term', definition: '', synonyms: [], sql: '', where: '', owner: '' };

export default function GlossaryPanel({ dark = false }: { dark?: boolean }) {
  const [terms, setTerms] = useState<GlossaryTerm[] | null>(null);
  const [q, setQ] = useState('');
  const [draft, setDraft] = useState<Draft | null>(null);
  const [synText, setSynText] = useState('');
  const [saving, setSaving] = useState(false);

  const load = (query = q) =>
    api.listGlossary(query).then((r) => setTerms(r.terms)).catch(() => setTerms([]));
  useEffect(() => { load(''); }, []);

  const edit = (t: Draft) => { setDraft(t); setSynText(t.synonyms.join(', ')); };

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    const body = { ...draft, synonyms: synText.split(',').map((s) => s.trim()).filter(Boolean) };
    delete (body as { id?: string }).id;
    try {
      if (draft.id) await api.updateGlossaryTerm(draft.id, body);
      else await api.addGlossaryTerm(body);
      toast.success(`Saved "${body.name}". The Copilot reads it when a request uses it.`);
      setDraft(null);
      await load();
    } catch (e) {
      toast.error((e as Error).message || String(e));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (t: GlossaryTerm) => {
    if (!window.confirm(`Delete "${t.name}" from the glossary?`)) return;
    try {
      await api.deleteGlossaryTerm(t.id);
      await load();
    } catch (e) {
      toast.error((e as Error).message || String(e));
    }
  };

  const card = dark ? 'bg-[#111827] border-white/[0.08] text-slate-200' : 'bg-white border-slate-200 text-slate-800';
  const muted = dark ? 'text-slate-400' : 'text-slate-500';
  const input = `w-full px-2.5 py-1.5 text-sm rounded-lg border ${dark ? 'bg-slate-800 border-slate-600 text-white' : 'border-slate-300 bg-white'}`;
  const button = `shrink-0 whitespace-nowrap px-3 py-1.5 text-xs font-semibold rounded-lg border ${dark ? 'border-slate-600 text-slate-200 hover:bg-white/5' : 'border-slate-300 text-slate-700 hover:bg-slate-50'}`;

  return (
    <div className="space-y-4" data-testid="glossary">
      <div className={`rounded-xl border p-4 ${card}`}>
        <p className={`text-sm ${muted}`}>
          Write down what your business means by its words: a term such as <em>active customer</em>, a metric such as
          <em> net revenue</em>, the other names people use, and the SQL and columns behind them. When a request to the
          Copilot or a pipeline draft uses one of these words, its definition goes with the request.
        </p>
        <div className="flex gap-2 mt-3">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') load(); }}
            placeholder="Search, or paste a request to see which terms it uses"
            className={input}
            aria-label="Search the glossary"
          />
          <button type="button" className={button} onClick={() => load()}>Search</button>
          <button type="button" className={button} onClick={() => edit({ ...EMPTY })}>+ Add a term</button>
        </div>
      </div>

      {draft && (
        <div className={`rounded-xl border p-4 space-y-2 ${card}`}>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
            <label className="text-xs font-semibold md:col-span-2">Name
              <input className={input} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="Net revenue" />
            </label>
            <label className="text-xs font-semibold">Kind
              <select className={input} value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value as Draft['kind'] })}>
                <option value="term">Term (a thing)</option>
                <option value="metric">Metric (a number)</option>
              </select>
            </label>
          </div>
          <label className="text-xs font-semibold block">Definition, in plain words
            <textarea className={input} rows={2} value={draft.definition} onChange={(e) => setDraft({ ...draft, definition: e.target.value })}
              placeholder="Gross sales minus refunds and discounts, in the order's currency." />
          </label>
          <label className="text-xs font-semibold block">Other names, separated by commas
            <input className={input} value={synText} onChange={(e) => setSynText(e.target.value)} placeholder="NR, net sales" />
          </label>
          <label className="text-xs font-semibold block">SQL (optional): the metric's expression or the term's condition
            <input className={`${input} font-mono`} value={draft.sql} onChange={(e) => setDraft({ ...draft, sql: e.target.value })}
              placeholder="SUM(amount) - SUM(refund_amount) - SUM(discount)" />
          </label>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            <label className="text-xs font-semibold">Where it lives (optional)
              <input className={input} value={draft.where} onChange={(e) => setDraft({ ...draft, where: e.target.value })} placeholder="sales.orders" />
            </label>
            <label className="text-xs font-semibold">Owner (optional)
              <input className={input} value={draft.owner} onChange={(e) => setDraft({ ...draft, owner: e.target.value })} placeholder="Finance" />
            </label>
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" className={button} onClick={() => setDraft(null)} disabled={saving}>Cancel</button>
            <button type="button" onClick={save} disabled={saving || !draft.name.trim() || !draft.definition.trim()}
              className="px-3 py-1.5 text-xs font-semibold rounded-lg bg-amber-700 text-white hover:bg-amber-800 disabled:opacity-50">
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      )}

      {terms && terms.length === 0 && !draft && (
        <div className={`text-sm ${muted}`}>{q ? 'No term matches.' : 'No terms yet.'}</div>
      )}
      <ul className="space-y-2">
        {(terms || []).map((t) => (
          <li key={t.id} className={`rounded-xl border p-3 ${card}`}>
            <div className="flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <div className="text-sm font-semibold">
                  {t.name} <span className={`text-[11px] font-normal ${muted}`}>{t.kind}{t.owner ? ` · ${t.owner}` : ''}</span>
                </div>
                <div className="text-sm mt-0.5">{t.definition}</div>
                {t.sql && <div className={`text-xs font-mono mt-1 ${muted}`}>{t.sql}</div>}
                {(t.where || t.synonyms.length > 0) && (
                  <div className={`text-xs mt-1 ${muted}`}>
                    {t.where && <>In {t.where}. </>}
                    {t.synonyms.length > 0 && <>Also called {t.synonyms.join(', ')}.</>}
                  </div>
                )}
              </div>
              <button type="button" className={button} onClick={() => edit({ ...t })}>Edit</button>
              <button type="button" className={button} onClick={() => remove(t)}>Delete</button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
