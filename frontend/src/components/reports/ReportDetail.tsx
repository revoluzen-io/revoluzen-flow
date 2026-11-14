import { useCallback, useEffect, useState } from 'react';
import { toast } from '../Toast';
import Icon from '../shared/Icon';
import StatusPill from '../shared/StatusPill';
import ReportDocument from './ReportDocument';
import {
  deliveryLabel, deliveryPillStatus, formatWhen, triggerLabel,
  type ReportDelivery, type ReportResult, type SavedReport,
} from './reportModel';
import { downloadReport, reportsApi } from './reportsApi';

const BUTTON = 'inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:border-slate-300 disabled:cursor-not-allowed disabled:opacity-50';

function DeliveryLog({ report, version, onView }: {
  report: SavedReport;
  version: number;
  onView: (delivery: ReportDelivery) => void;
}) {
  const [rows, setRows] = useState<ReportDelivery[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    reportsApi.deliveries(report.id)
      .then((r) => { setRows(r.deliveries); setError(''); })
      .catch((err: Error) => setError(err.message));
  }, [report.id, version]);

  if (error) return <p className="text-sm text-rose-600" role="alert">{error}</p>;
  if (rows === null) return <p className="text-sm text-slate-500">Loading…</p>;
  if (rows.length === 0) {
    return <p className="py-8 text-center text-sm text-slate-500">This report has not been sent yet.</p>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="w-full text-[12.5px]" data-testid="delivery-log">
        <thead className="bg-thead-dev-bg text-thead-dev-text border-b-2 border-thead-dev-border">
          <tr>
            {['When', 'How', 'Period', 'Sent to', 'Result', ''].map((h) => (
              <th key={h} className="whitespace-nowrap px-3 py-2 text-left text-[10px] font-bold uppercase tracking-wider">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((d) => (
            <tr key={d.id}>
              <td className="whitespace-nowrap px-3 py-2 align-top text-slate-700">{formatWhen(d.started_at)}</td>
              <td className="whitespace-nowrap px-3 py-2 align-top text-slate-700">{triggerLabel(d.trigger)}</td>
              <td className="px-3 py-2 align-top text-slate-700">{d.period || '—'}</td>
              <td className="px-3 py-2 align-top text-slate-700">
                {d.targets.length === 0 && '—'}
                {d.targets.map((t) => (
                  <div key={`${t.type}:${t.target}`} className={t.status === 'failed' ? 'text-rose-600' : ''} title={t.error || undefined}>
                    {t.target}
                  </div>
                ))}
              </td>
              <td className="px-3 py-2 align-top">
                <StatusPill size="sm" status={deliveryPillStatus(d.status)} label={deliveryLabel(d.status)} />
                {d.status !== 'delivered' && d.detail && <div className="mt-1 max-w-sm text-[11.5px] text-slate-500">{d.detail}</div>}
              </td>
              <td className="whitespace-nowrap px-3 py-2 align-top text-right">
                {d.has_snapshot && (
                  <button type="button" className="text-xs font-semibold text-blue-600 hover:underline" onClick={() => onView(d)}>What was sent</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ReportDetail({
  report: initial, canAuthor, onEdit, onBack, onChanged,
}: {
  report: SavedReport;
  canAuthor: boolean;
  onEdit: () => void;
  onBack: () => void;
  onChanged: (report: SavedReport) => void;
}) {
  const [report, setReport] = useState(initial);
  const [tab, setTab] = useState<'report' | 'deliveries'>('report');
  const [result, setResult] = useState<ReportResult | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [logVersion, setLogVersion] = useState(0);
  const [sentCopy, setSentCopy] = useState<{ delivery: ReportDelivery; result: ReportResult } | null>(null);

  useEffect(() => setReport(initial), [initial]);

  const load = useCallback(() => {
    setLoading(true);
    reportsApi.run(initial.id)
      .then((r) => { setResult(r); setError(''); })
      .catch((err: Error) => setError(err.message || 'The report could not be made.'))
      .finally(() => setLoading(false));
  }, [initial.id]);

  useEffect(load, [load]);

  const refreshReport = async () => {
    try {
      const fresh = await reportsApi.get(report.id);
      setReport(fresh);
      onChanged(fresh);
    } catch {
      /* the page keeps what it has */
    }
  };

  const send = async (toMe: boolean) => {
    if (!toMe) {
      const count = report.recipients.length;
      if (count === 0) {
        toast.error('No recipients yet', 'Edit the report to add who it goes to.');
        return;
      }
      if (!window.confirm(`Send this report now to ${count} recipient${count === 1 ? '' : 's'}?`)) return;
    }
    setBusy(toMe ? 'test' : 'send');
    try {
      const delivery = await reportsApi.send(report.id, toMe);
      if (delivery.status === 'delivered') toast.success(toMe ? 'Test sent' : 'Report sent', delivery.detail);
      else toast.error(deliveryLabel(delivery.status), delivery.detail);
      setLogVersion((v) => v + 1);
      await refreshReport();
    } catch (err: any) {
      toast.error('The report could not be sent', err.message);
    } finally {
      setBusy('');
    }
  };

  const download = async (format: 'pdf' | 'docx') => {
    setBusy(format);
    try {
      const { filename, savedToStorage } = await downloadReport(report.id, format);
      toast.success(`Downloaded ${filename}`, savedToStorage ? 'A copy is in Storage > Files.' : undefined);
    } catch (err: any) {
      toast.error('The download failed', err.message);
    } finally {
      setBusy('');
    }
  };

  const viewSent = async (delivery: ReportDelivery) => {
    try {
      setSentCopy({ delivery, result: await reportsApi.deliverySnapshot(report.id, delivery.id) });
    } catch (err: any) {
      toast.error('That copy is not available', err.message);
    }
  };

  const tabClass = (active: boolean) => `border-b-2 px-3 py-2 text-xs font-semibold ${active ? 'border-slate-800 text-slate-900' : 'border-transparent text-slate-500 hover:text-slate-800'}`;

  return (
    <div className="space-y-4" data-testid="report-detail">
      <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <button type="button" onClick={onBack} className="text-xs font-semibold text-slate-500 hover:text-slate-800">← Reports</button>
            <h2 className="mt-0.5 truncate text-base font-bold text-slate-900">{report.name}</h2>
            <p className="text-xs text-slate-500">
              {report.schedule.enabled
                ? <>{report.schedule_text}. Next: <span className="text-slate-700">{formatWhen(report.next_send_at)}</span></>
                : 'Not on a schedule'}
              {report.delivery.last_status && (
                <> · Last: <span className="text-slate-700">{deliveryLabel(report.delivery.last_status)}, {formatWhen(report.delivery.last_attempt_at)}</span></>
              )}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className={BUTTON} disabled={loading} onClick={load}><Icon name="rotate-cw" size={12} />Refresh</button>
            <button type="button" className={BUTTON} disabled={Boolean(busy)} onClick={() => void download('pdf')}><Icon name="download" size={12} />{busy === 'pdf' ? 'Making…' : 'PDF'}</button>
            <button type="button" className={BUTTON} disabled={Boolean(busy)} onClick={() => void download('docx')}><Icon name="download" size={12} />{busy === 'docx' ? 'Making…' : 'Word'}</button>
            {canAuthor && (
              <>
                <button type="button" className={BUTTON} disabled={Boolean(busy)} onClick={() => void send(true)}><Icon name="mail" size={12} />{busy === 'test' ? 'Sending…' : 'Test to me'}</button>
                <button type="button" className={BUTTON} disabled={Boolean(busy)} onClick={() => void send(false)}><Icon name="mail" size={12} />{busy === 'send' ? 'Sending…' : 'Send now'}</button>
                <button type="button" className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-bold text-white hover:bg-slate-700" onClick={onEdit}>Edit</button>
              </>
            )}
          </div>
        </div>
        <div className="mt-2 flex gap-1 border-b border-slate-100" role="tablist">
          <button type="button" role="tab" aria-selected={tab === 'report'} className={tabClass(tab === 'report')} onClick={() => setTab('report')}>Report</button>
          <button type="button" role="tab" aria-selected={tab === 'deliveries'} className={tabClass(tab === 'deliveries')} onClick={() => setTab('deliveries')}>Sent history</button>
        </div>
      </div>

      {tab === 'report' && (
        <div className="mx-auto max-w-5xl rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          {error && <p className="py-10 text-center text-sm text-rose-600" role="alert">{error}</p>}
          {!error && !result && <p className="py-16 text-center text-sm text-slate-500">Working out the numbers…</p>}
          {!error && result && <ReportDocument result={result} />}
        </div>
      )}
      {tab === 'deliveries' && <DeliveryLog report={report} version={logVersion} onView={viewSent} />}

      {sentCopy && (
        <>
          <div className="fixed inset-0 z-[60] bg-black/40" onClick={() => setSentCopy(null)} />
          <div className="pointer-events-none fixed inset-0 z-[65] flex items-center justify-center p-4">
            <div role="dialog" aria-label="What was sent" className="pointer-events-auto flex h-[90vh] w-[960px] max-w-[96vw] flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
              <div className="flex shrink-0 items-center justify-between border-b border-slate-200 px-5 py-3">
                <div className="text-sm font-bold text-slate-800">As sent on {formatWhen(sentCopy.delivery.started_at)}</div>
                <button type="button" aria-label="Close" onClick={() => setSentCopy(null)} className="rounded p-1 text-slate-500 hover:bg-slate-100">
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                </button>
              </div>
              <div className="flex-1 overflow-y-auto p-6"><ReportDocument result={sentCopy.result} /></div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
