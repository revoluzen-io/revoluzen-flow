import { useCallback, useEffect, useState } from 'react';
import { usePageContext } from '../../hooks/usePageContext';
import { navigateTo, navigateToSubRoute, readHashQuery, readSubRoute } from '../../router';
import { toast } from '../Toast';
import EmptyState from '../shared/EmptyState';
import Icon from '../shared/Icon';
import PageHeader from '../shared/PageHeader';
import StatusPill from '../shared/StatusPill';
import TierChip from '../shared/TierChip';
import InventoryDocument from '../reports/InventoryDocument';
import ReportDesigner from '../reports/ReportDesigner';
import ReportDetail from '../reports/ReportDetail';
import {
  canAuthorReports, deliveryLabel, deliveryPillStatus, formatWhen,
  type Catalog, type SavedReport, type TemplateSpec,
} from '../reports/reportModel';
import { reportsApi } from '../reports/reportsApi';

// Insights > Reports: the reports this workspace has designed, the starting
// points for a new one, and each report's own page.
//
//   #reports                 the list
//   #reports/new             a blank designer (?template=<id> starts from one)
//   #reports/<id>            one report, with its sent history
//   #reports/<id>/edit       the designer on a saved report
//
// Design notes and the API: backend/revoluzen_flow/reports/builder/.

interface User {
  id: string;
  email: string;
  role: string;
  name?: string;
}

type Route =
  | { view: 'list' }
  | { view: 'new'; template: string | null }
  | { view: 'report'; id: string }
  | { view: 'edit'; id: string };

function readRoute(): Route {
  const sub = readSubRoute();
  if (!sub) return { view: 'list' };
  if (sub === 'new') return { view: 'new', template: readHashQuery().template || null };
  const [id, action] = sub.split('/');
  return action === 'edit' ? { view: 'edit', id } : { view: 'report', id };
}

function NewReportButton() {
  return (
    <button
      type="button"
      onClick={() => navigateToSubRoute('reports', 'new')}
      className="inline-flex items-center gap-1 rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-bold text-white hover:bg-slate-700"
    >
      + New report
    </button>
  );
}

function ScheduleSwitch({ report, canAuthor, onToggle }: {
  report: SavedReport;
  canAuthor: boolean;
  onToggle: (report: SavedReport, enabled: boolean) => void;
}) {
  const on = report.schedule.enabled;
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={`Send ${report.name} automatically`}
      disabled={!canAuthor}
      onClick={() => onToggle(report, !on)}
      className={`relative h-[18px] w-8 shrink-0 rounded-full transition disabled:cursor-not-allowed disabled:opacity-60 ${on ? 'bg-emerald-500' : 'bg-slate-300'}`}
    >
      <span className={`absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white shadow transition-all ${on ? 'left-[15px]' : 'left-0.5'}`} />
    </button>
  );
}

function ReportsTable({ reports, canAuthor, onToggle, onDuplicate, onDelete }: {
  reports: SavedReport[];
  canAuthor: boolean;
  onToggle: (report: SavedReport, enabled: boolean) => void;
  onDuplicate: (report: SavedReport) => void;
  onDelete: (report: SavedReport) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border-2 border-thead-dev-border bg-white shadow-sm">
      <table className="w-full text-[13px]" data-testid="reports-table">
        <thead className="bg-thead-dev-bg text-thead-dev-text border-b-2 border-thead-dev-border">
          <tr>
            {['Report', 'Covers', 'Schedule', 'Last sent', ''].map((h) => (
              <th key={h} className="whitespace-nowrap px-4 py-2.5 text-left text-xs font-bold uppercase tracking-wider">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {reports.map((r) => (
            <tr key={r.id} className="hover:bg-slate-50/60">
              <td className="px-4 py-2.5 align-top">
                <button type="button" className="text-left font-semibold text-slate-900 hover:underline" onClick={() => navigateToSubRoute('reports', r.id)}>{r.name}</button>
                <div className="text-xs text-slate-500">{r.sections.length} section{r.sections.length === 1 ? '' : 's'}{r.description ? ` · ${r.description}` : ''}</div>
              </td>
              <td className="whitespace-nowrap px-4 py-2.5 align-top text-slate-700">{r.time_range_label}</td>
              <td className="px-4 py-2.5 align-top">
                <div className="flex items-center gap-2">
                  <ScheduleSwitch report={r} canAuthor={canAuthor} onToggle={onToggle} />
                  <div className="min-w-0">
                    <div className="text-slate-700">{r.schedule.enabled ? r.schedule_text : 'Off'}</div>
                    {r.schedule.enabled && <div className="text-xs text-slate-500">Next: {formatWhen(r.next_send_at)} · {r.recipients.length} recipient{r.recipients.length === 1 ? '' : 's'}</div>}
                  </div>
                </div>
              </td>
              <td className="px-4 py-2.5 align-top">
                {r.delivery.last_status ? (
                  <div>
                    <StatusPill size="sm" status={deliveryPillStatus(r.delivery.last_status)} label={deliveryLabel(r.delivery.last_status)} title={r.delivery.last_error || undefined} />
                    <div className="mt-0.5 text-xs text-slate-500">
                      {formatWhen(r.delivery.last_attempt_at)}
                      {r.delivery.last_status === 'failed' && (
                        <span className="block">{r.delivery.last_sent_at ? `Last reached someone: ${formatWhen(r.delivery.last_sent_at)}` : 'Has not reached anyone yet'}</span>
                      )}
                    </div>
                  </div>
                ) : <span className="text-xs text-slate-500">Not sent yet</span>}
              </td>
              <td className="whitespace-nowrap px-4 py-2.5 text-right align-top">
                <button type="button" className="mr-3 text-xs font-semibold text-blue-600 hover:underline" onClick={() => navigateToSubRoute('reports', r.id)}>Open</button>
                {canAuthor && (
                  <>
                    <button type="button" className="mr-3 text-xs font-semibold text-slate-600 hover:underline" onClick={() => navigateToSubRoute('reports', `${r.id}/edit`)}>Edit</button>
                    <button type="button" className="mr-3 text-xs font-semibold text-slate-600 hover:underline" onClick={() => onDuplicate(r)}>Copy</button>
                    <button type="button" className="text-xs font-semibold text-rose-600 hover:underline" onClick={() => onDelete(r)}>Delete</button>
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TemplateCard({ title, body, onUse, action }: { title: string; body: string; onUse: () => void; action: string }) {
  return (
    <button type="button" onClick={onUse} className="flex flex-col rounded-xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:border-slate-300 hover:shadow">
      <span className="text-[13px] font-semibold text-slate-900">{title}</span>
      <span className="mt-1 flex-1 text-xs leading-snug text-slate-500">{body}</span>
      <span className="mt-2 text-xs font-semibold text-blue-600">{action}</span>
    </button>
  );
}

function Templates({ templates, canAuthor, onInventory }: { templates: TemplateSpec[]; canAuthor: boolean; onInventory: () => void }) {
  return (
    <section>
      <h3 className="mb-2 text-sm font-bold text-slate-800">Start from</h3>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {canAuthor && templates.map((t) => (
          <TemplateCard
            key={t.id}
            title={t.name}
            body={t.description}
            action="Use as a start"
            onUse={() => { window.location.hash = `reports/new?template=${encodeURIComponent(t.id)}`; }}
          />
        ))}
        <TemplateCard
          title="Installation inventory"
          body="One document listing everything in this install, as PDF or Word. Not limited to a period."
          action="Open"
          onUse={onInventory}
        />
      </div>
    </section>
  );
}

function Body({ user }: { user: User | null }) {
  const [route, setRoute] = useState<Route>(readRoute);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [reports, setReports] = useState<SavedReport[] | null>(null);
  const [error, setError] = useState('');
  const [showInventory, setShowInventory] = useState(false);

  const canAuthor = canAuthorReports(user?.role);
  const isAdmin = user?.role === 'super_admin' || user?.role === 'admin';

  useEffect(() => {
    const follow = () => setRoute(readRoute());
    window.addEventListener('hashchange', follow);
    return () => window.removeEventListener('hashchange', follow);
  }, []);

  const loadReports = useCallback(() => {
    reportsApi.list()
      .then((r) => { setReports(r.reports); setError(''); })
      .catch((err: Error) => setError(err.message || 'The reports could not be loaded.'));
  }, []);

  useEffect(() => {
    reportsApi.catalog().then(setCatalog).catch((err: Error) => setError(err.message || 'The report options could not be loaded.'));
    loadReports();
  }, [loadReports]);

  useEffect(() => {
    if (route.view === 'list') loadReports();
  }, [route.view, loadReports]);

  usePageContext({
    page: 'reports',
    filters: {
      view: route.view,
      report_id: route.view === 'report' || route.view === 'edit' ? route.id : null,
      saved_reports: reports?.length ?? null,
    },
  });

  const replaceReport = (updated: SavedReport) => {
    setReports((rows) => (rows ? rows.map((r) => (r.id === updated.id ? updated : r)) : rows));
  };

  const toggle = async (report: SavedReport, enabled: boolean) => {
    try {
      replaceReport(await reportsApi.setSchedule(report.id, enabled));
    } catch (err: any) {
      toast.error('Could not change the schedule', err.message);
    }
  };

  const duplicate = async (report: SavedReport) => {
    try {
      const copy = await reportsApi.duplicate(report.id);
      toast.success('Report copied', copy.name);
      loadReports();
    } catch (err: any) {
      toast.error('Could not copy the report', err.message);
    }
  };

  const remove = async (report: SavedReport) => {
    if (!window.confirm(`Delete "${report.name}" and its sent history? This cannot be undone.`)) return;
    try {
      await reportsApi.remove(report.id);
      toast.success('Report deleted', report.name);
      loadReports();
    } catch (err: any) {
      toast.error('Could not delete the report', err.message);
    }
  };

  if (error && !catalog) return <p className="py-10 text-center text-sm text-rose-600" role="alert">{error}</p>;
  if (!catalog) return <p className="py-16 text-center text-sm text-slate-500">Loading…</p>;

  if (route.view === 'new' && canAuthor) {
    return (
      <ReportDesigner
        key={`new-${route.template || ''}`}
        catalog={catalog}
        templateId={route.template}
        onSaved={(saved) => navigateToSubRoute('reports', saved.id)}
        onCancel={() => navigateTo('reports')}
      />
    );
  }

  if (route.view === 'edit' || route.view === 'report') {
    return (
      <SavedReportView
        key={`${route.view}-${route.id}`}
        id={route.id}
        catalog={catalog}
        editing={route.view === 'edit'}
        canAuthor={canAuthor}
      />
    );
  }

  return (
    <div className="space-y-5" data-testid="reports-home">
      <section>
        <div className="mb-2 flex items-center justify-between gap-2">
          <h3 className="text-sm font-bold text-slate-800">Your reports</h3>
          {canAuthor && <NewReportButton />}
        </div>
        {error && <p className="mb-2 text-sm text-rose-600" role="alert">{error}</p>}
        {reports === null && !error && <p className="py-8 text-center text-sm text-slate-500">Loading…</p>}
        {reports !== null && reports.length === 0 && (
          <div className="rounded-xl border border-slate-200 bg-white shadow-sm">
            <EmptyState
              icon={<Icon name="file-text" size={24} />}
              title="No reports yet"
              body="Choose what goes in, narrow it to the projects and period you care about, and send it on a schedule."
              primaryCta={canAuthor ? { label: '+ New report', onClick: () => navigateToSubRoute('reports', 'new') } : undefined}
            />
          </div>
        )}
        {reports !== null && reports.length > 0 && (
          <ReportsTable reports={reports} canAuthor={canAuthor} onToggle={toggle} onDuplicate={duplicate} onDelete={remove} />
        )}
      </section>
      <Templates templates={catalog.templates} canAuthor={canAuthor} onInventory={() => setShowInventory(true)} />
      {showInventory && <InventoryDocument isAdmin={Boolean(isAdmin)} onClose={() => setShowInventory(false)} />}
    </div>
  );
}

function SavedReportView({ id, catalog, editing, canAuthor }: {
  id: string;
  catalog: Catalog;
  editing: boolean;
  canAuthor: boolean;
}) {
  const [report, setReport] = useState<SavedReport | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    reportsApi.get(id).then(setReport).catch((err: Error) => setError(err.message || 'That report could not be opened.'));
  }, [id]);

  if (error) {
    return (
      <div className="py-12 text-center">
        <p className="text-sm text-rose-600" role="alert">{error}</p>
        <button type="button" className="mt-2 text-xs font-semibold text-blue-600 hover:underline" onClick={() => navigateTo('reports')}>Back to reports</button>
      </div>
    );
  }
  if (!report) return <p className="py-16 text-center text-sm text-slate-500">Loading…</p>;

  if (editing && canAuthor) {
    return (
      <ReportDesigner
        catalog={catalog}
        existing={report}
        onSaved={(saved) => navigateToSubRoute('reports', saved.id)}
        onCancel={() => navigateToSubRoute('reports', report.id)}
      />
    );
  }
  return (
    <ReportDetail
      report={report}
      canAuthor={canAuthor}
      onBack={() => navigateTo('reports')}
      onEdit={() => navigateToSubRoute('reports', `${report.id}/edit`)}
      onChanged={setReport}
    />
  );
}

export default function ReportsPage({
  user,
  embedded = false,
  tier = 'free',
  environment = 'dev',
}: {
  user: User | null;
  embedded?: boolean;
  tier?: 'plus' | 'free';
  environment?: 'dev' | 'prod';
}) {
  if (embedded) return <Body user={user} />;
  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <PageHeader
        environment={environment}
        icon={<Icon name="file-text" size={20} className="text-indigo-500" />}
        title="Reports"
        subtitle="Design the reports you need, and send them on a schedule."
        titleAccessory={<TierChip tier={tier} environment={environment} />}
      />
      <div className="flex-1 overflow-auto bg-canvas-bg">
        <div className="mx-auto w-full max-w-[1560px] px-6 py-5"><Body user={user} /></div>
      </div>
    </div>
  );
}
