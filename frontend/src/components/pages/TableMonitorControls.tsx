/**
 * Table monitors (2026-11-11): when a managed table's tests run on their own.
 *
 * "After every load" checks the table in the background each time a pipeline
 * writes it. "Check every" is the only thing that catches a table nobody loads
 * any more: with no load, nothing runs after a load. A failed test lands in
 * the Steward and the admins' bell. Backend: datastore/table_monitor.py.
 */

import { useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';

export interface TableMonitor {
  after_load: boolean;
  every_minutes: number | null;
}

export interface TableTestRun {
  ran_at: string;
  trigger?: string;
  passed: number;
  failed: number;
  results: Array<{ test_type: string; columns: string[]; ok: boolean; violations: number; message?: string }>;
}

const EVERY_OPTIONS: Array<{ value: number; label: string }> = [
  { value: 0, label: 'Off' },
  { value: 15, label: '15 minutes' },
  { value: 60, label: 'hour' },
  { value: 360, label: '6 hours' },
  { value: 1440, label: 'day' },
];

const TRIGGER_LABEL: Record<string, string> = {
  manual: 'Run tests',
  after_load: 'after a load',
  schedule: 'the periodic check',
};

export default function TableMonitorControls({
  tableId,
  monitor,
  lastRun,
  onChange,
}: {
  tableId: string;
  monitor: TableMonitor;
  lastRun: TableTestRun | null;
  onChange: (m: TableMonitor) => void;
}) {
  const [saving, setSaving] = useState(false);
  const every = monitor.every_minutes ?? 0;
  const options = EVERY_OPTIONS.some((o) => o.value === every)
    ? EVERY_OPTIONS
    : [...EVERY_OPTIONS, { value: every, label: `${every} minutes` }];

  const save = async (next: TableMonitor) => {
    setSaving(true);
    try {
      const res = await api.put<TableMonitor>(`/api/storage/tables/${tableId}/monitor`, {
        after_load: next.after_load,
        every_minutes: next.every_minutes || null,
      });
      onChange({ after_load: res.after_load, every_minutes: res.every_minutes });
    } catch (err) {
      toast.error(`Could not save the monitor: ${(err as Error).message || err}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-md border border-slate-200 bg-white px-3 py-2 text-xs space-y-1.5" data-testid="table-monitor">
      <div className="flex items-center gap-4 flex-wrap">
        <span className="font-semibold text-slate-800">Run these tests on their own</span>
        <label className="inline-flex items-center gap-1.5 text-slate-700">
          <input
            type="checkbox"
            checked={monitor.after_load}
            disabled={saving}
            onChange={(e) => save({ ...monitor, after_load: e.target.checked })}
          />
          After every load
        </label>
        <label className="inline-flex items-center gap-1.5 text-slate-700">
          Check every
          <select
            value={every}
            disabled={saving}
            onChange={(e) => save({ ...monitor, every_minutes: Number(e.target.value) || null })}
            className="px-1.5 py-0.5 rounded border border-slate-300 bg-white"
            aria-label="Check every"
          >
            {options.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </label>
      </div>
      <div className="text-[11px] text-slate-500">
        {every > 0
          ? 'The periodic check also catches a table that stopped being loaded. '
          : ''}
        A failed test shows in the Steward and in the admins' notifications.
        {lastRun && (
          <>
            {' '}Last run {new Date(lastRun.ran_at).toLocaleString()}
            {lastRun.trigger ? `, by ${TRIGGER_LABEL[lastRun.trigger] || lastRun.trigger}` : ''}:{' '}
            <span className={lastRun.failed ? 'text-red-700 font-semibold' : 'text-emerald-700 font-semibold'}>
              {lastRun.passed} passed, {lastRun.failed} failed
            </span>
            .
          </>
        )}
      </div>
    </div>
  );
}
