/**
 * Shared rule sets in the Data Quality step (2026-11-11).
 *
 * A shared rule set is a named list of Data Quality rules many pipelines use.
 * This step runs the rules of the sets picked here with its own, as the sets
 * are when it runs: editing a set changes the checks of every pipeline that
 * uses it from its next run, published ones included, which the editor says
 * before it saves. Backend: quality_rules/store.py, api/quality_rule_sets.py.
 */

import { useEffect, useState } from 'react';
import { api, type QualityRuleSet } from '../../api/client';
import DataQualityRules from '../DataQualityRules';
import { toast } from '../Toast';

type Rule = Record<string, any>;

const BUTTON = 'text-[11px] font-semibold px-2 py-1 rounded border border-slate-300 text-slate-700 hover:bg-slate-50 disabled:opacity-50';
const INPUT = 'w-full px-2.5 py-1.5 text-xs text-slate-800 bg-white border border-slate-300 rounded-lg';

function ruleText(r: Rule): string {
  const value = r.value === undefined ? '' : ` ${Array.isArray(r.value) ? r.value.join(', ') : String(r.value)}`;
  return `${r.column} ${r.op || 'not_null'}${value}`;
}

export default function RuleSetPicker({
  selected,
  ownRules,
  columns = [],
  sourceTypes,
  onChange,
}: {
  selected: string[];
  ownRules: Rule[];
  columns?: string[];
  sourceTypes?: Record<string, string>;
  onChange: (patch: { rule_sets?: string[]; rules?: Rule[] }) => void;
}) {
  const [sets, setSets] = useState<QualityRuleSet[] | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Rule[]>([]);
  const [saving, setSaving] = useState(false);
  const [newName, setNewName] = useState('');
  const [naming, setNaming] = useState(false);

  const load = () =>
    api.listQualityRuleSets()
      .then((r) => setSets(r.rule_sets))
      .catch(() => setSets([]));
  useEffect(() => { load(); }, []);

  const byId = new Map((sets || []).map((s) => [s.id, s]));
  const available = (sets || []).filter((s) => !selected.includes(s.id));

  const saveEdit = async (set: QualityRuleSet) => {
    setSaving(true);
    try {
      const saved = await api.updateQualityRuleSet(set.id, { rules: draft });
      const users = saved.used_by || [];
      const published = users.filter((u) => u.published).length;
      toast.success(
        saved.version === set.version
          ? `${set.name}: nothing changed`
          : `${set.name} saved as version ${saved.version}. ${users.length} pipeline${users.length === 1 ? '' : 's'}`
            + `${published ? ` (${published} published)` : ''} check the new rules from their next run.`,
      );
      setEditing(null);
      await load();
    } catch (err) {
      toast.error((err as Error).message || String(err));
    } finally {
      setSaving(false);
    }
  };

  const shareOwnRules = async () => {
    setSaving(true);
    try {
      const created = await api.createQualityRuleSet({ name: newName.trim(), rules: ownRules });
      // The rules move into the set: the step now runs them from there.
      onChange({ rule_sets: [...selected, created.id], rules: [] });
      toast.success(`Saved as the shared rule set "${created.name}". Other Data Quality steps can now pick it.`);
      setNaming(false);
      setNewName('');
      await load();
    } catch (err) {
      toast.error((err as Error).message || String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-2" data-testid="rule-set-picker">
      {selected.map((id) => {
        const set = byId.get(id);
        if (sets && !set) {
          return (
            <div key={id} className="flex items-center gap-2 px-2.5 py-2 rounded-lg border border-red-200 bg-red-50 text-[11px] text-red-800">
              <span className="flex-1">A shared rule set this step uses no longer exists ({id}). The run will stop here.</span>
              <button type="button" className={BUTTON} onClick={() => onChange({ rule_sets: selected.filter((s) => s !== id) })}>Remove</button>
            </div>
          );
        }
        if (!set) return null;
        const missing = columns.length
          ? Array.from(new Set(set.rules.map((r) => String(r.column)))).filter((c) => !columns.includes(c))
          : [];
        return (
          <div key={id} className="rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-[11px] space-y-1.5">
            <div className="flex items-center gap-2">
              <span className="font-semibold text-slate-800">{set.name}</span>
              <span className="text-slate-500">version {set.version} · {set.rule_count} rule{set.rule_count === 1 ? '' : 's'}</span>
              <span className="ml-auto flex gap-1">
                <button type="button" className={BUTTON} disabled={saving}
                  onClick={() => { setEditing(editing === id ? null : id); setDraft(set.rules); }}>
                  {editing === id ? 'Close' : 'Edit set'}
                </button>
                <button type="button" className={BUTTON} onClick={() => onChange({ rule_sets: selected.filter((s) => s !== id) })}>Remove</button>
              </span>
            </div>
            {editing !== id && (
              <ul className="text-slate-600 font-mono space-y-0.5">
                {set.rules.slice(0, 6).map((r, i) => <li key={i}>{ruleText(r)}</li>)}
                {set.rules.length > 6 && <li className="font-sans text-slate-500">and {set.rules.length - 6} more</li>}
              </ul>
            )}
            {missing.length > 0 && (
              <div className="text-red-700">This step's input has no {missing.join(', ')}: the run will stop here.</div>
            )}
            {editing === id && (
              <div className="space-y-2 pt-1">
                <DataQualityRules rules={draft} columns={columns} sourceTypes={sourceTypes} onChange={setDraft} />
                <div className="text-amber-800 bg-amber-50 border border-amber-200 rounded px-2 py-1">
                  Saving changes the checks of every pipeline that uses "{set.name}", published ones included, from their next run.
                  The rules it replaces are kept as version {set.version}.
                </div>
                <div className="flex justify-end gap-1">
                  <button type="button" className={BUTTON} onClick={() => setEditing(null)} disabled={saving}>Cancel</button>
                  <button type="button" className="text-[11px] font-semibold px-2 py-1 rounded bg-amber-700 text-white hover:bg-amber-800 disabled:opacity-50"
                    onClick={() => saveEdit(set)} disabled={saving || draft.length === 0}>
                    {saving ? 'Saving…' : 'Save set'}
                  </button>
                </div>
              </div>
            )}
          </div>
        );
      })}

      {available.length > 0 && (
        <select
          aria-label="Add a shared rule set"
          value=""
          onChange={(e) => e.target.value && onChange({ rule_sets: [...selected, e.target.value] })}
          className={INPUT}
        >
          <option value="">+ Add a shared rule set…</option>
          {available.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name} ({s.rule_count} rule{s.rule_count === 1 ? '' : 's'}{s.used_by_count ? `, used by ${s.used_by_count}` : ''})
            </option>
          ))}
        </select>
      )}
      {sets && sets.length === 0 && selected.length === 0 && (
        <div className="text-[11px] text-slate-500">
          No shared rule sets yet. Rules saved as a set can be picked by any Data Quality step, and fixed in one place.
        </div>
      )}

      {ownRules.length > 0 && !naming && (
        <button type="button" className={BUTTON} onClick={() => setNaming(true)}>
          Save this step's rules as a shared set
        </button>
      )}
      {naming && (
        <div className="flex gap-1">
          <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Name, e.g. customer record"
            className={INPUT} aria-label="Shared rule set name" />
          <button type="button" className={BUTTON} onClick={shareOwnRules} disabled={saving || !newName.trim()}>Save</button>
          <button type="button" className={BUTTON} onClick={() => setNaming(false)} disabled={saving}>Cancel</button>
        </div>
      )}
    </div>
  );
}
