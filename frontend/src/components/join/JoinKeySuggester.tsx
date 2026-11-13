/**
 * "Suggest keys" for a Join step (2026-11-11).
 *
 * Samples both inputs as Test Node would (preview: nothing is written) and
 * ranks the column pairs that actually line up: shared values, names, types
 * and uniqueness. A pair with the same name on both sides applies as
 * `same_key`; a pair whose names differ applies as `mapped_keys` with one
 * key pair, which is the setting people (and models) most often get wrong
 * by typing the other side's name into the key list.
 */

import { useState } from 'react';
import { api, type JoinKeySuggestions } from '../../api/client';
import { nodesToWorkflow, useWorkflowStore } from '../../stores/workflowStore';

const SHAPE_NOTE: Record<string, string> = {
  'many-to-one': 'each left row matches at most one',
  'one-to-one': 'one row on each side',
  'one-to-many': 'each right row matches at most one',
  'many-to-many': 'can multiply rows',
};

export default function JoinKeySuggester({ nodeId, onChange }: {
  nodeId: string;
  onChange: (nodeId: string, params: Record<string, any>) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<JoinKeySuggestions | null>(null);

  const suggest = async () => {
    setBusy(true);
    setError(null);
    try {
      const s = useWorkflowStore.getState();
      const wf = nodesToWorkflow(s.nodes, s.edges, s.workflowName, s.workflowId || 'ephemeral', s.parameters);
      setResult(await api.suggestJoinKeys(wf, nodeId));
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const apply = (left: string, right: string) => {
    if (left === right) {
      onChange(nodeId, { key_mode: 'same_key', join_key: [left] });
    } else {
      onChange(nodeId, { key_mode: 'mapped_keys', key_pairs: [{ left, right, operator: '=' }] });
    }
  };

  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 -mt-1">
      <div className="flex items-center gap-2">
        <div className="flex-1 text-[11px] text-slate-500 leading-snug">
          Not sure which columns match? Flow can sample both inputs and rank the pairs that share values.
        </div>
        <button
          onClick={suggest}
          disabled={busy}
          className="shrink-0 px-2.5 py-1 text-[11px] font-semibold text-pipe-600 border border-pipe-300 rounded-lg hover:bg-pipe-50 disabled:opacity-50 transition-colors"
          title="Run both inputs on a sample (nothing is written) and suggest the join key"
        >
          {busy ? 'Sampling…' : 'Suggest keys'}
        </button>
      </div>
      {error && <div className="mt-1.5 text-[11px] text-red-600">{error}</div>}
      {result && (
        <div className="mt-2 space-y-1.5">
          {result.suggestions.length === 0 && (
            <div className="text-[11px] text-amber-700">
              No pair of columns shares values or a name in samples of {result.sampled_rows.left.toLocaleString()} and{' '}
              {result.sampled_rows.right.toLocaleString()} rows. Check that both inputs describe the same things.
            </div>
          )}
          {result.suggestions.map((s, i) => (
            <div key={`${s.left}-${s.right}`} className={`rounded-md border px-2.5 py-1.5 ${i === 0 ? 'border-pipe-200 bg-pipe-50/50' : 'border-slate-200'}`}>
              <div className="flex items-center gap-2">
                <div className="flex-1 min-w-0 text-[11px] font-mono text-slate-700 truncate">
                  {result.left_label}.{s.left} <span className="text-slate-400">=</span> {result.right_label}.{s.right}
                </div>
                <span className="text-[10px] font-semibold text-slate-500" title="How strongly the evidence points to this pair, out of 100">
                  {s.score}
                </span>
                <button
                  onClick={() => apply(s.left, s.right)}
                  className="text-[11px] font-semibold text-pipe-600 hover:text-pipe-800"
                >
                  Use
                </button>
              </div>
              <div
                className="text-[10px] text-slate-500 mt-0.5"
                title={s.estimated ? 'One side was sampled in part, so the share found in the samples is scaled to all of its rows.' : undefined}
              >
                {s.estimated ? 'About ' : ''}{s.left_matched_pct.toFixed(0)}% of values match · {s.shape}, {SHAPE_NOTE[s.shape]}
              </div>
              {s.repeat_note ? (
                <div className="text-[10px] font-semibold text-amber-700 mt-0.5" data-testid="join-key-repeats">
                  {s.repeat_note}.
                </div>
              ) : s.shape === 'many-to-many' && (
                <div className="text-[10px] font-semibold text-amber-700 mt-0.5">Neither side is unique: the join can multiply rows.</div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
