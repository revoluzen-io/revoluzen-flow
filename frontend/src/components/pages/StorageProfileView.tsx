/**
 * Profile tab of the Storage drawer (2026-11-11): what is in each column,
 * before a pipeline is built on it. The same statistics the Data Profile
 * step produces: empty values, distinct values, smallest and largest, the
 * most common value, and averages for numbers. Large data is profiled on a
 * random sample, and the header says how many rows were read.
 */

import { useEffect, useState } from 'react';
import { api } from '../../api/client';

interface ColumnProfile {
  column: string;
  data_type: string;
  row_count: number;
  null_count: number;
  null_pct: number;
  distinct_count: number;
  distinct_pct: number;
  min_value: string | null;
  max_value: string | null;
  top_value: string | null;
  top_value_count: number;
  mean_value: number | null;
  median_value: number | null;
  stddev_value: number | null;
  avg_length: number | null;
  max_length: number | null;
}

interface ProfileResponse {
  columns: ColumnProfile[];
  rows_profiled: number;
  row_count: number;
  sampled: boolean;
}

const fmt = (n: number | null | undefined, digits = 2) =>
  n == null || Number.isNaN(n) ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });

export default function StorageProfileView({ id, kind }: { id: string; kind: 'object' | 'table' }) {
  const [data, setData] = useState<ProfileResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    setError(null);
    const url = kind === 'table' ? `/api/storage/tables/${id}/profile` : `/api/storage/profile/${id}`;
    api.get<ProfileResponse>(url)
      .then((d) => { if (alive) setData(d); })
      .catch((e) => { if (alive) setError(e instanceof Error ? e.message : String(e)); });
    return () => { alive = false; };
  }, [id, kind]);

  if (error) return <div className="p-6 text-sm text-red-600">Could not profile this data: {error}</div>;
  if (!data) return <div className="p-8 text-center text-sm text-slate-500">Profiling every column…</div>;
  if (data.columns.length === 0) return <div className="p-6 text-sm text-slate-500">No rows to profile.</div>;

  return (
    <div className="p-4">
      <div className="text-xs text-slate-500 mb-3">
        {data.sampled
          ? <>Profiled a random sample of <span className="font-semibold text-slate-700">{data.rows_profiled.toLocaleString()}</span> of {data.row_count.toLocaleString()} rows.</>
          : <>Profiled all <span className="font-semibold text-slate-700">{data.rows_profiled.toLocaleString()}</span> rows.</>}
        {' '}Same statistics as the Data Profile step.
      </div>
      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="min-w-full text-xs">
          <thead className="bg-slate-50 text-slate-600">
            <tr>
              {['Column', 'Type', 'Empty', 'Distinct', 'Smallest', 'Largest', 'Most common', 'Average', 'Longest'].map((h) => (
                <th key={h} className="px-3 py-2 text-left font-semibold whitespace-nowrap">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.columns.map((c) => (
              <tr key={c.column} className="align-top">
                <td className="px-3 py-2 font-mono font-semibold text-slate-800 whitespace-nowrap">{c.column}</td>
                <td className="px-3 py-2 font-mono text-slate-500 whitespace-nowrap">{c.data_type}</td>
                <td className="px-3 py-2 whitespace-nowrap">
                  <div className="flex items-center gap-2">
                    <div className="w-16 h-1.5 rounded-full bg-slate-100 overflow-hidden" aria-hidden="true">
                      <div
                        className={`h-full ${c.null_pct > 50 ? 'bg-red-400' : c.null_pct > 10 ? 'bg-amber-400' : 'bg-emerald-400'}`}
                        style={{ width: `${Math.min(100, Math.max(0, c.null_pct))}%` }}
                      />
                    </div>
                    <span className="text-slate-700">{fmt(c.null_pct, 1)}%</span>
                  </div>
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-slate-700">
                  {c.distinct_count.toLocaleString()}
                  {c.distinct_count > 0 && c.distinct_count === c.row_count - c.null_count && (
                    <span className="ml-1.5 text-[10px] font-bold uppercase text-violet-700 bg-violet-50 px-1 py-0.5 rounded" title="Every non-empty value is different: a candidate key">unique</span>
                  )}
                </td>
                <td className="px-3 py-2 max-w-[10rem] truncate text-slate-700" title={c.min_value ?? ''}>{c.min_value ?? '—'}</td>
                <td className="px-3 py-2 max-w-[10rem] truncate text-slate-700" title={c.max_value ?? ''}>{c.max_value ?? '—'}</td>
                <td className="px-3 py-2 max-w-[12rem] truncate text-slate-700" title={c.top_value ?? ''}>
                  {c.top_value ?? '—'}
                  {c.top_value != null && <span className="ml-1 text-slate-400">×{c.top_value_count.toLocaleString()}</span>}
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-slate-700">{fmt(c.mean_value)}</td>
                <td className="px-3 py-2 whitespace-nowrap text-slate-700">{c.max_length != null ? `${c.max_length} chars` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
