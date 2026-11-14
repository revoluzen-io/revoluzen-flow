/**
 * A pipeline's Versions view, in its tab on the Pipelines page, and the
 * read-only view of one saved version in a tab of its own.
 *
 * Versions lists every kept version, newest first: who saved it, when, the
 * kind of save, its name, and the runs that used it. Picking one compares it
 * with the current version, on the canvas (each step marked added, changed or
 * removed) or as a list of every setting before and after. From there an
 * older version can be opened read-only, restored, copied into a new
 * pipeline, named or downloaded.
 *
 * Restore puts the older version in the Editor as unsaved changes on the
 * pipeline's own tab; nothing changes until Save, which records it as
 * "Restored version N" (workflowStore.restoreVersionInEditor).
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  BaseEdge,
  Handle,
  MarkerType,
  Position,
  type Edge,
  type EdgeProps,
  type Node as FlowNode,
  type NodeProps,
} from '@xyflow/react';

import { api, type VersionHistory, type VersionHistoryItem } from '../../api/client';
import { navigateToSubRoute } from '../../router';
import { useWorkflowStore } from '../../stores/workflowStore';
import { uiConfirm, uiPrompt } from '../../ui/dialog';
import { ModuleIcon } from '../ModulesPanel';
import { nodeTypeLabel } from '../modulesPanelData';
import { toast } from '../Toast';
import { diffVersions, type SettingRow, type StepChange, type StepDiff, type VersionDiff } from './versionDiff';

type View = 'canvas' | 'list';
type NodeKind = StepChange | 'plain';

// ── Small helpers ───────────────────────────────────────────────────────────

/** "Today, 11:40", "Yesterday, 09:10", "3 Nov, 15:02". */
export function versionWhen(iso: string, now: Date = new Date()): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return `Today, ${time}`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return `Yesterday, ${time}`;
  const date = d.toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' as const } : {}),
  });
  return `${date}, ${time}`;
}

/** What the list calls a version: its name, else what made it. */
export function versionTitle(v: VersionHistoryItem): string {
  if (v.name) return v.name;
  if (v.kind === 'auto') return 'Auto-save';
  const note = (v.change_summary || '').trim();
  if (note && note !== 'Auto-save') return note;
  return v.kind === 'restored' ? 'Restored' : 'Saved';
}

/** Versions saved before Flow recorded who saved them say "user". */
function savedBy(v: VersionHistoryItem): string {
  return v.created_by && v.created_by !== 'user' ? v.created_by : 'Not recorded';
}

function runState(status: string): 'ok' | 'fail' | 'other' {
  const s = (status || '').toLowerCase();
  if (s === 'success' || s === 'completed' || s === 'ok') return 'ok';
  if (s === 'error' || s === 'failed' || s === 'failure') return 'fail';
  return 'other';
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function keepNote(h: VersionHistory): string {
  const saves = h.keep.saves > 0 ? `the last ${h.keep.saves} saves` : 'every save';
  const autos = h.keep.auto_saves > 0 ? `the last ${h.keep.auto_saves} auto-saves` : 'every auto-save';
  const removed = h.removed > 0
    ? ` ${plural(h.removed, 'older version has', 'older versions have')} been removed.`
    : ' None has been removed yet.';
  return `Flow keeps every named or published version, ${saves} and ${autos}.${removed}`;
}

/** Restore notes say "Restored version N"; the view uses it to explain two
 *  versions that are the same. */
function restoredFrom(v: VersionHistoryItem | undefined): number | null {
  const m = /^Restored version (\d+)/.exec(v?.change_summary || '');
  return m ? Number(m[1]) : null;
}

// ── Icons ───────────────────────────────────────────────────────────────────

function Glyph({ d, size = 14, className = '' }: { d: string; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={`shrink-0 ${className}`} aria-hidden="true">
      <path d={d} />
    </svg>
  );
}
const ICON = {
  eye: 'M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
  restore: 'M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5',
  copy: 'M9 9h12v12H9zM5 15V5a2 2 0 0 1 2-2h10',
  dots: 'M5 12h.01M12 12h.01M19 12h.01',
  tag: 'M6 3h12v18l-6-4-6 4z',
  down: 'M12 3v12m-5-5 5 5 5-5M4 20h16',
  clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2',
  check: 'm5 12 5 5L20 7',
  x: 'M18 6 6 18M6 6l12 12',
  chevR: 'm9 6 6 6-6 6',
  chevD: 'm6 9 6 6 6-6',
  shield: 'M12 3 4 6v6c0 5 3.4 8 8 9 4.6-1 8-4 8-9V6zM9 12l2 2 4-4',
  info: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 11v5M12 8h.01',
};

// ── Canvas pieces ───────────────────────────────────────────────────────────

const BOX: Record<NodeKind, string> = {
  added: 'border-2 border-emerald-600 shadow-[0_0_0_4px_rgba(22,163,74,0.15)]',
  changed: 'border-2 border-amber-600 shadow-[0_0_0_4px_rgba(217,119,6,0.16)]',
  removed: 'border-2 border-dashed border-red-600 bg-red-50',
  same: 'border border-slate-300',
  plain: 'border border-slate-300',
};
const TAG: Partial<Record<NodeKind, { text: string; cls: string }>> = {
  added: { text: 'Added', cls: 'bg-emerald-100 text-emerald-700' },
  changed: { text: 'Changed', cls: 'bg-amber-100 text-amber-700' },
  removed: { text: 'Removed', cls: 'bg-red-100 text-red-700' },
};

interface StepNodeData {
  name: string;
  type: string;
  kind: NodeKind;
  selected: boolean;
  [key: string]: unknown;
}

function VersionStepNode({ data }: NodeProps) {
  const d = data as StepNodeData;
  const tag = TAG[d.kind];
  return (
    <div className={`relative flex flex-col items-center w-[140px] ${d.kind === 'same' && !d.selected ? 'opacity-50 hover:opacity-100' : ''}`}>
      <Handle type="target" position={Position.Left} style={{ top: 40, left: 30, opacity: 0 }} isConnectable={false} />
      {tag && (
        <span className={`absolute -top-6 px-2 py-0.5 rounded-full text-[10.5px] font-bold whitespace-nowrap ${tag.cls}`}>{tag.text}</span>
      )}
      <div
        className={`w-20 h-20 rounded-2xl bg-white grid place-items-center ${BOX[d.kind]} ${
          d.selected ? 'outline outline-2 outline-offset-4 outline-blue-600' : ''
        }`}
      >
        <div className={d.kind === 'removed' ? 'opacity-40' : ''}>
          <ModuleIcon type={d.type} size={52} />
        </div>
      </div>
      <div className={`mt-2 max-w-[140px] truncate text-xs font-bold ${d.kind === 'removed' ? 'text-red-700 line-through' : 'text-slate-800'}`} title={d.name}>
        {d.name || nodeTypeLabel(d.type)}
      </div>
      <div className="max-w-[140px] truncate text-[11px] text-slate-500">{nodeTypeLabel(d.type)}</div>
      <Handle type="source" position={Position.Right} style={{ top: 40, right: 30, opacity: 0 }} isConnectable={false} />
    </div>
  );
}

/** A link only the older version has, drawn as a dashed arc over the steps
 *  between, so it does not run through the steps that replaced it. */
function RemovedLinkEdge({ sourceX, sourceY, targetX, targetY, markerEnd, style }: EdgeProps) {
  const mx = (sourceX + targetX) / 2;
  const lift = Math.max(70, Math.abs(targetX - sourceX) * 0.3);
  const my = Math.min(sourceY, targetY) - lift;
  return <BaseEdge path={`M ${sourceX},${sourceY} Q ${mx},${my} ${targetX},${targetY}`} markerEnd={markerEnd} style={style} />;
}

const NODE_TYPES = { versionStep: VersionStepNode };
const EDGE_TYPES = { removedLink: RemovedLinkEdge };

const EDGE_COLOR: Record<string, string> = { same: '#94a3b8', added: '#16a34a', removed: '#dc2626', plain: '#64748b' };

/** Where each step sits on the comparison canvas: the Editor's layout,
 *  scaled down so the steps read larger once the view is fitted (the Editor
 *  spaces steps for its bigger squares). The shape stays the same. */
const LAYOUT_SCALE = 0.62;
function laidOut(steps: { id: string; position: { x: number; y: number } }[]): Record<string, { x: number; y: number }> {
  const out: Record<string, { x: number; y: number }> = {};
  const allZero = steps.every((s) => !s.position.x && !s.position.y);
  steps.forEach((s, i) => {
    out[s.id] = allZero
      ? { x: i * 190, y: 0 }
      : { x: s.position.x * LAYOUT_SCALE, y: s.position.y * LAYOUT_SCALE };
  });
  return out;
}

function flowForDiff(diff: VersionDiff, selected: string | null): { nodes: FlowNode[]; edges: Edge[] } {
  const at = laidOut(diff.steps);
  const nodes: FlowNode[] = diff.steps.map((s) => ({
    id: s.id,
    type: 'versionStep',
    position: at[s.id],
    data: { name: s.name, type: s.type, kind: s.kind, selected: s.id === selected } satisfies StepNodeData,
    draggable: false,
    connectable: false,
  }));
  const edges: Edge[] = diff.links.map((l, i) => ({
    id: `l${i}-${l.from}-${l.to}`,
    source: l.from,
    target: l.to,
    type: l.kind === 'removed' ? 'removedLink' : 'default',
    style: {
      stroke: EDGE_COLOR[l.kind],
      strokeWidth: l.kind === 'added' ? 2.5 : 2,
      ...(l.kind === 'removed' ? { strokeDasharray: '6 5' } : {}),
    },
    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: EDGE_COLOR[l.kind] },
  }));
  return { nodes, edges };
}

function flowForVersion(wf: any, selected: string | null): { nodes: FlowNode[]; edges: Edge[] } {
  const steps = (wf?.steps || []).map((s: any) => ({ id: s.id, position: { x: Number(s.position?.x) || 0, y: Number(s.position?.y) || 0 } }));
  const at = laidOut(steps);
  const nodes: FlowNode[] = (wf?.steps || []).map((s: any) => ({
    id: s.id,
    type: 'versionStep',
    position: at[s.id],
    data: { name: s.label || '', type: s.type || '', kind: 'plain', selected: s.id === selected } satisfies StepNodeData,
    draggable: false,
    connectable: false,
  }));
  const edges: Edge[] = (wf?.connections || []).map((c: any, i: number) => ({
    id: `p${i}-${c.from_step}-${c.to_step}`,
    source: c.from_step,
    target: c.to_step,
    style: { stroke: EDGE_COLOR.plain, strokeWidth: 2 },
    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: EDGE_COLOR.plain },
  }));
  return { nodes, edges };
}

function StepCanvas({
  flow,
  flowKey,
  onPick,
  legend,
}: {
  flow: { nodes: FlowNode[]; edges: Edge[] };
  flowKey: string;
  onPick: (id: string) => void;
  legend: boolean;
}) {
  // 2026-11-12: on a laptop-height window the canvas got what the 224 px step
  // panel left over, about 70 px, with the legend floating over the steps. It now
  // keeps a readable height (the comparison scrolls instead) and the legend sits
  // above it.
  return (
    <div className="relative flex-1 min-h-[300px] flex flex-col bg-[#FFFBEB]" data-testid="version-canvas">
      {legend && (
        <div className="shrink-0 flex flex-wrap gap-3 px-4 py-1.5 border-b border-amber-200 bg-white/90 text-[11.5px] text-slate-600" data-testid="version-legend">
          <span className="flex items-center gap-1.5"><i className="w-2.5 h-2.5 rounded-[3px] border-2 border-emerald-600" />Added</span>
          <span className="flex items-center gap-1.5"><i className="w-2.5 h-2.5 rounded-[3px] border-2 border-amber-600" />Changed</span>
          <span className="flex items-center gap-1.5"><i className="w-2.5 h-2.5 rounded-[3px] border-2 border-dashed border-red-600" />Removed</span>
          <span className="flex items-center gap-1.5"><i className="w-2.5 h-2.5 rounded-[3px] border border-slate-300 opacity-60" />The same</span>
        </div>
      )}
      <div className="relative flex-1 min-h-0">
      <ReactFlow
        key={flowKey}
        nodes={flow.nodes}
        edges={flow.edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.35, maxZoom: 1 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        onNodeClick={(_, n) => onPick(n.id)}
        panOnDrag
        zoomOnScroll
        preventScrolling
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#FDE68A" />
      </ReactFlow>
      </div>
    </div>
  );
}

const PILL: Record<NodeKind, string> = {
  added: 'bg-emerald-100 text-emerald-700',
  changed: 'bg-amber-100 text-amber-700',
  removed: 'bg-red-100 text-red-700',
  same: 'bg-slate-100 text-slate-600',
  plain: 'bg-slate-100 text-slate-600',
};
const PILL_TEXT: Record<NodeKind, string> = { added: 'Added', changed: 'Changed', removed: 'Removed', same: 'The same', plain: 'Read-only' };

function StepHead({ type, name, kind }: { type: string; name: string; kind: NodeKind }) {
  return (
    <div className="flex items-center gap-2 min-w-0">
      <ModuleIcon type={type} size={26} />
      <b className="text-[13px] text-slate-800 truncate">{name || nodeTypeLabel(type)}</b>
      <span className="text-[11.5px] text-slate-500 truncate">{nodeTypeLabel(type)}</span>
      <span className={`ml-auto shrink-0 px-2 py-0.5 rounded-full text-[10.5px] font-bold ${PILL[kind]}`}>{PILL_TEXT[kind]}</span>
    </div>
  );
}

/** The settings of the step picked on the canvas, in both versions. */
function StepDock({ step, from, to }: { step: StepDiff | null; from: number; to: number }) {
  if (!step) {
    return (
      <div className="h-56 shrink-0 border-t border-slate-300 bg-white px-5 py-4 text-[12.5px] text-slate-500">
        Click a step to see its settings in both versions.
      </div>
    );
  }
  return (
    <div className="h-56 shrink-0 border-t border-slate-300 bg-white px-5 py-3 overflow-auto" data-testid="version-step-dock">
      <StepHead type={step.type} name={step.name} kind={step.kind} />
      {step.rows.length === 0 ? (
        <p className="mt-3 text-[12.5px] text-slate-500">This step has no settings.</p>
      ) : (
        <table className="mt-2 w-full table-fixed border-collapse text-[12.5px]">
          <thead>
            <tr className="text-left text-[10.5px] uppercase tracking-wider text-slate-500">
              <th className="w-48 px-2 py-1.5 font-bold bg-slate-50">Setting</th>
              <th className="px-2 py-1.5 font-bold bg-slate-50">Version {from}</th>
              <th className="px-2 py-1.5 font-bold bg-slate-50">Version {to}</th>
            </tr>
          </thead>
          <tbody>
            {step.rows.map((r) => (
              <tr key={r.key} className="border-t border-slate-100 align-top">
                <th className="px-2 py-1.5 text-left font-semibold text-slate-600 break-words">{r.label}</th>
                <SettingCell row={r} side="before" />
                <SettingCell row={r} side="after" />
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function SettingCell({ row, side }: { row: SettingRow; side: 'before' | 'after' }) {
  const value = side === 'before' ? row.before : row.after;
  if (value === null) return <td className="px-2 py-1.5 italic text-slate-400">Not in this version</td>;
  const cls = !row.changed
    ? 'text-slate-600'
    : side === 'before'
      ? 'bg-red-50 text-red-800 line-through decoration-red-800/40'
      : 'bg-emerald-50 text-emerald-800 font-semibold';
  return <td className={`px-2 py-1.5 whitespace-pre-wrap break-words ${cls}`}>{value}</td>;
}

/** Option A: the changes as a list. */
function ChangeList({ diff, from, to, toIsCurrent }: { diff: VersionDiff; from: number; to: number; toIsCurrent: boolean }) {
  const name = (id: string) => {
    const s = diff.steps.find((x) => x.id === id);
    return s ? s.name || nodeTypeLabel(s.type) : id;
  };
  const group = (kind: StepChange, title: string) => {
    const steps = diff.steps.filter((s) => s.kind === kind);
    if (steps.length === 0) return null;
    return (
      <section>
        <h4 className="mt-4 mb-2 flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-slate-500">
          {title} <span className="px-1.5 rounded-full bg-slate-200 text-slate-600">{steps.length}</span>
        </h4>
        {steps.map((s) => {
          const rows = kind === 'changed' ? s.rows.filter((r) => r.changed) : s.rows;
          return (
            <div key={s.id} className="mb-2 rounded-xl border border-slate-200 bg-white px-3 py-2.5">
              <StepHead type={s.type} name={s.name} kind={s.kind} />
              {rows.length > 0 && (
                <table className="mt-1.5 w-full table-fixed border-separate border-spacing-y-1 text-[12.5px]">
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.key} className="align-top">
                        <th className="w-48 pr-2 py-1 text-left font-semibold text-slate-600 break-words">{r.label}</th>
                        {kind !== 'added' && (
                          <td className="px-2 py-1 rounded bg-red-50 text-red-800 line-through decoration-red-800/40 whitespace-pre-wrap break-words">{r.before}</td>
                        )}
                        {kind === 'changed' && <td className="w-6 text-center text-slate-400">→</td>}
                        {kind !== 'removed' && (
                          <td className="px-2 py-1 rounded bg-emerald-50 text-emerald-800 whitespace-pre-wrap break-words">{r.after}</td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          );
        })}
      </section>
    );
  };
  const changedLinks = diff.links.filter((l) => l.kind !== 'same');
  const same = diff.steps.filter((s) => s.kind === 'same');
  const parts: string[] = [];
  if (diff.added) parts.push(plural(diff.added, 'step') + ' added');
  if (diff.removed) parts.push(`${diff.removed} removed`);
  if (diff.changed) parts.push(`${diff.changed} changed`);
  return (
    <div className="flex-1 min-h-0 overflow-auto px-6 py-4" data-testid="version-change-list">
      <p className="text-sm font-semibold text-slate-800">
        From version {from} to {toIsCurrent ? 'the current version' : `version ${to}`}: {parts.length ? parts.join(', ') : 'no step changes'}.
      </p>
      {group('changed', 'Steps changed')}
      {group('added', 'Steps added')}
      {group('removed', 'Steps removed')}
      {changedLinks.length > 0 && (
        <section>
          <h4 className="mt-4 mb-2 flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-slate-500">
            Links between steps <span className="px-1.5 rounded-full bg-slate-200 text-slate-600">{changedLinks.length}</span>
          </h4>
          <ul className="grid gap-1">
            {changedLinks.map((l, i) => (
              <li key={i} className={`px-2.5 py-1.5 rounded-lg text-[12.5px] ${l.kind === 'added' ? 'bg-emerald-50 text-emerald-800' : 'bg-red-50 text-red-800'}`}>
                {l.kind === 'added' ? '+ ' : '− '}{name(l.from)} → {name(l.to)}
              </li>
            ))}
          </ul>
        </section>
      )}
      {diff.pipeline.length > 0 && (
        <section>
          <h4 className="mt-4 mb-2 flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-slate-500">
            Pipeline settings <span className="px-1.5 rounded-full bg-slate-200 text-slate-600">{diff.pipeline.length}</span>
          </h4>
          <div className="rounded-xl border border-slate-200 bg-white px-3 py-2">
            <table className="w-full table-fixed border-separate border-spacing-y-1 text-[12.5px]">
              <tbody>
                {diff.pipeline.map((r) => (
                  <tr key={r.key} className="align-top">
                    <th className="w-48 pr-2 py-1 text-left font-semibold text-slate-600 break-words">{r.label}</th>
                    <td className="px-2 py-1 rounded bg-red-50 text-red-800 line-through decoration-red-800/40 whitespace-pre-wrap break-words">{r.before}</td>
                    <td className="w-6 text-center text-slate-400">→</td>
                    <td className="px-2 py-1 rounded bg-emerald-50 text-emerald-800 whitespace-pre-wrap break-words">{r.after}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {same.length > 0 && (
        <p className="mt-4 text-[12.5px] text-slate-500">The same in both: {same.map((s) => s.name || nodeTypeLabel(s.type)).join(', ')}.</p>
      )}
      <p className="mt-1 text-xs text-slate-400">Schedules are kept apart from versions, so they are not compared here.</p>
    </div>
  );
}

// ── Actions on a version ────────────────────────────────────────────────────

interface ActionContext {
  pipelineId: string;
  pipelineName: string;
  projectId: string | null;
  folderId: string | null;
  history: VersionHistory | null;
  readContent: (v: number) => Promise<any>;
  reload: () => void;
  onChanged?: () => void;
}

function useVersionActions(ctx: ActionContext) {
  const { pipelineId, pipelineName, projectId, folderId, history, readContent, reload, onChanged } = ctx;

  const restore = useCallback(async (v: number) => {
    if (!history) return;
    const published = history.status === 'published';
    const ok = await uiConfirm({
      title: `Restore version ${v}?`,
      message:
        `Version ${v} opens in the Editor as unsaved changes, on this pipeline's tab. ` +
        (published
          ? `Test it there, then Save it as a draft. Version ${history.published_version ?? history.current_version} keeps running until you publish again.`
          : 'Test it there, then Save to make it the newest version. Nothing changes until you save.'),
      confirmLabel: 'Open in Editor',
    });
    if (!ok) return;
    try {
      const workflow = await readContent(v);
      const result = useWorkflowStore.getState().restoreVersionInEditor(
        { version: v, workflow },
        v,
        {
          version: history.current_version,
          status: history.status,
          publishedVersion: history.published_version,
          hasUnpublishedChanges: history.has_unpublished_changes,
        },
      );
      if (result === 'dirty') {
        toast.warning(
          'The Editor has unsaved changes to this pipeline',
          'Save or discard them there first, then restore this version.',
        );
        return;
      }
      if (result === 'full') return; // the store has said why
      navigateToSubRoute('editor', pipelineId);
      toast.success(`Version ${v} is in the Editor`, 'It is not saved yet. Save there to make it the newest version.');
    } catch (err: any) {
      toast.error('Restore failed', err?.message || `Version ${v} could not be read.`);
    }
  }, [history, pipelineId, readContent]);

  const copy = useCallback(async (v: number) => {
    const name = await uiPrompt({
      title: `New pipeline from version ${v}`,
      message: `A new draft with version ${v}'s steps, in the same project and folder. ${pipelineName} stays as it is.`,
      defaultValue: `${pipelineName} (from v${v})`,
      confirmLabel: 'Create draft',
    });
    if (!name || !name.trim()) return;
    try {
      const wf = await readContent(v);
      const created = await api.createWorkflowWithSteps({
        name: name.trim(),
        project_id: projectId,
        folder_id: folderId,
        description: wf?.description || '',
        business_purpose: wf?.business_purpose || '',
        readme: wf?.readme || '',
        tags: wf?.tags || [],
        parameters: wf?.parameters || [],
        steps: wf?.steps || [],
        connections: wf?.connections || [],
      });
      toast.success('New pipeline created', `“${created?.workflow?.name || name.trim()}” is a draft in the same folder.`);
      onChanged?.();
    } catch (err: any) {
      toast.error('Could not create the pipeline', err?.message || 'Try again.');
    }
  }, [folderId, onChanged, pipelineName, projectId, readContent]);

  const rename = useCallback(async (v: VersionHistoryItem) => {
    const value = await uiPrompt({
      title: v.name ? `Rename version ${v.version}` : `Name version ${v.version}`,
      message: 'Named versions are never removed. Leave the name empty to remove it.',
      defaultValue: v.name,
      placeholder: 'For example: Before the GST change',
      confirmLabel: 'Save name',
    });
    if (value === null) return;
    const name = value.trim();
    if (name.length > 80) {
      toast.error('That name is too long', 'A version name can be at most 80 characters.');
      return;
    }
    try {
      await api.nameWorkflowVersion(pipelineId, v.version, name);
      toast.success(name ? `Version ${v.version} is named “${name}”` : `Version ${v.version} has no name now`);
      reload();
    } catch (err: any) {
      toast.error('Could not name the version', err?.message || 'Try again.');
    }
  }, [pipelineId, reload]);

  const download = useCallback(async (v: number) => {
    try {
      const data = await api.exportPipeline(pipelineId, v);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${pipelineName.replace(/[^a-zA-Z0-9_-]/g, '_')}-v${v}.revoluzen-flow.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success(`Version ${v} downloaded`, a.download);
    } catch (err: any) {
      toast.error('Download failed', err?.message || 'Try again.');
    }
  }, [pipelineId, pipelineName]);

  return { restore, copy, rename, download };
}

function useHistory(pipelineId: string, refreshKey: number) {
  const [history, setHistory] = useState<VersionHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    api.getWorkflowHistory(pipelineId)
      .then((h) => { if (!cancelled) setHistory(h); })
      .catch((err: any) => { if (!cancelled) setError(err?.message || 'The version history could not be read.'); });
    return () => { cancelled = true; };
  }, [pipelineId, refreshKey, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { history, error, reload };
}

function useVersionContent(pipelineId: string) {
  const cache = useRef<Map<number, Promise<any>>>(new Map());
  useEffect(() => { cache.current = new Map(); }, [pipelineId]);
  return useCallback((v: number): Promise<any> => {
    let p = cache.current.get(v);
    if (!p) {
      p = api.getWorkflowVersion(pipelineId, v).then((data: any) => data?.workflow || data);
      p.catch(() => cache.current.delete(v));
      cache.current.set(v, p);
    }
    return p;
  }, [pipelineId]);
}

function useConnectionNames(): Record<string, string> {
  const [names, setNames] = useState<Record<string, string>>({});
  useEffect(() => {
    let cancelled = false;
    // Connections, and the shared rule sets a Data Quality step picks by id.
    // Names are a nicety: when either list cannot be read, its ids still show.
    Promise.all([
      Promise.resolve(api.listConnections()).catch(() => [] as any[]),
      Promise.resolve(api.listQualityRuleSets()).then((r: any) => r?.rule_sets || []).catch(() => [] as any[]),
    ])
      .then(([connections, ruleSets]: any[]) => {
        if (cancelled) return;
        const out: Record<string, string> = {};
        if (Array.isArray(connections)) for (const c of connections) if (c?.id && c?.name) out[c.id] = c.name;
        if (Array.isArray(ruleSets)) for (const s of ruleSets) if (s?.id && s?.name) out[s.id] = s.name;
        setNames(out);
      })
      .catch(() => { /* names are a nicety: ids still read */ });
    return () => { cancelled = true; };
  }, []);
  return names;
}

function MoreMenu({
  anchor,
  item,
  isCurrent,
  canEdit,
  onClose,
  actions,
  onOpen,
}: {
  anchor: DOMRect;
  item: VersionHistoryItem;
  isCurrent: boolean;
  canEdit: boolean;
  onClose: () => void;
  actions: ReturnType<typeof useVersionActions>;
  onOpen: (v: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDown = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) onClose(); };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('mousedown', onDown);
    window.addEventListener('keydown', onKey);
    ref.current?.querySelector<HTMLButtonElement>('button')?.focus();
    return () => {
      document.removeEventListener('mousedown', onDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [onClose]);
  const left = Math.max(8, Math.min(anchor.right - 240, window.innerWidth - 248));
  const top = anchor.bottom + 236 > window.innerHeight ? anchor.top - 6 - 200 : anchor.bottom + 4;
  const item_ = (label: string, icon: string, run: () => void) => (
    <button
      type="button"
      role="menuitem"
      onClick={() => { onClose(); run(); }}
      className="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-[12.5px] text-slate-700 text-left hover:bg-slate-100 focus:bg-slate-100 focus:outline-none"
    >
      <Glyph d={icon} className="text-slate-500" />{label}
    </button>
  );
  return (
    <div ref={ref} role="menu" aria-label={`Version ${item.version}`} className="fixed z-[60] w-60 rounded-lg border border-slate-200 bg-white p-1 shadow-xl" style={{ left, top }}>
      {!isCurrent && (
        <>
          {canEdit && item_('Restore…', ICON.restore, () => void actions.restore(item.version))}
          {canEdit && item_('New pipeline from this version…', ICON.copy, () => void actions.copy(item.version))}
          {item_('Open read-only', ICON.eye, () => onOpen(item.version))}
          <hr className="my-1 border-slate-200" />
        </>
      )}
      {canEdit && item_(item.name ? 'Rename this version…' : 'Name this version…', ICON.tag, () => void actions.rename(item))}
      {item_('Download as a file', ICON.down, () => void actions.download(item.version))}
    </div>
  );
}

// ── The Versions view ───────────────────────────────────────────────────────

export interface PipelineVersionsProps {
  pipelineId: string;
  pipelineName: string;
  /** Where the pipeline lives now; New pipeline from a version goes there too. */
  projectId: string | null;
  folderId: string | null;
  /** Restore, New pipeline and names need edit rights; archived pipelines are read-only. */
  canEdit: boolean;
  /** Open a version read-only in a tab of its own on this page. */
  onOpenVersion: (version: number) => void;
  /** Something the Pipelines list shows changed (a new pipeline). */
  onChanged?: () => void;
  /** Bump to read the history again, after a save made elsewhere. */
  refreshKey?: number;
  /** Compare this version with the current one first, instead of the one before it. */
  initialFrom?: number;
}

type ListItem =
  | { kind: 'row'; v: VersionHistoryItem }
  | { kind: 'autos'; key: number; versions: VersionHistoryItem[] };

export default function PipelineVersions({
  pipelineId,
  pipelineName,
  projectId,
  folderId,
  canEdit,
  onOpenVersion,
  onChanged,
  refreshKey = 0,
  initialFrom,
}: PipelineVersionsProps) {
  const { history, error, reload } = useHistory(pipelineId, refreshKey);
  const readContent = useVersionContent(pipelineId);
  const names = useConnectionNames();
  const actions = useVersionActions({ pipelineId, pipelineName, projectId, folderId, history, readContent, reload, onChanged });

  const current = history?.current_version ?? null;
  const [from, setFrom] = useState<number | null>(null);
  const [to, setTo] = useState<number | null>(null);
  const [view, setView] = useState<View>('canvas');
  const [selected, setSelected] = useState<string | null>(null);
  const [openGroups, setOpenGroups] = useState<Set<number>>(new Set());
  const [menu, setMenu] = useState<{ item: VersionHistoryItem; anchor: DOMRect } | null>(null);
  const [pair, setPair] = useState<{ a: any; b: any } | null>(null);
  const [pairError, setPairError] = useState<string | null>(null);

  // First look: the version asked for, else the one before the current one,
  // against the current one.
  const askedFor = useRef<number | undefined>(initialFrom);
  useEffect(() => {
    if (!history) return;
    const has = (n: number | null | undefined) => n != null && history.versions.some((v) => v.version === n);
    if (!has(to)) setTo(history.current_version);
    if (askedFor.current != null) {
      const asked = askedFor.current;
      askedFor.current = undefined;
      if (has(asked) && asked !== history.current_version) {
        setFrom(asked);
        setTo(history.current_version);
        return;
      }
    }
    if (!has(from) || from === history.current_version) {
      // A review step changes no step, so the first look skips them.
      const previous = history.versions.find((v) => v.version !== history.current_version && v.kind !== 'review')
        || history.versions.find((v) => v.version !== history.current_version);
      setFrom(previous ? previous.version : null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history]);

  useEffect(() => {
    setPair(null);
    setPairError(null);
    if (from === null || to === null) return;
    let cancelled = false;
    Promise.all([readContent(from), readContent(to)])
      .then(([a, b]) => { if (!cancelled) setPair({ a, b }); })
      .catch((err: any) => { if (!cancelled) setPairError(err?.message || 'These versions could not be read.'); });
    return () => { cancelled = true; };
  }, [from, to, readContent]);

  const diff = useMemo(() => (pair ? diffVersions(pair.a, pair.b, { names }) : null), [pair, names]);

  // The step shown below the canvas: the one picked, else the first change.
  useEffect(() => {
    if (!diff) return;
    if (selected && diff.steps.some((s) => s.id === selected)) return;
    setSelected(diff.steps.find((s) => s.kind !== 'same')?.id ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [diff]);

  const items = useMemo<ListItem[]>(() => {
    const out: ListItem[] = [];
    const vs = history?.versions || [];
    for (let i = 0; i < vs.length; ) {
      if (vs[i].kind === 'auto' && vs[i].version !== current) {
        let j = i;
        while (j < vs.length && vs[j].kind === 'auto' && vs[j].version !== current) j++;
        const group = vs.slice(i, j);
        if (group.length > 1) out.push({ kind: 'autos', key: group[0].version, versions: group });
        else out.push({ kind: 'row', v: group[0] });
        i = j;
      } else {
        out.push({ kind: 'row', v: vs[i] });
        i++;
      }
    }
    return out;
  }, [history, current]);

  const byNumber = useMemo(() => {
    const m = new Map<number, VersionHistoryItem>();
    for (const v of history?.versions || []) m.set(v.version, v);
    return m;
  }, [history]);

  const pick = (v: VersionHistoryItem) => {
    if (v.version === current) {
      toast.info('This is the current version', 'Pick an older one to compare it with this one.');
      return;
    }
    setFrom(v.version);
    setTo(current);
  };

  if (error) {
    return (
      <div className="flex-1 grid place-items-center p-8 text-center">
        <div>
          <p className="text-sm font-semibold text-slate-700">The version history could not be read</p>
          <p className="mt-1 text-xs text-slate-500">{error}</p>
          <button type="button" onClick={reload} className="mt-3 px-3 py-1.5 text-xs font-semibold rounded-lg border border-slate-300 bg-white hover:bg-slate-50">Try again</button>
        </div>
      </div>
    );
  }
  if (!history) {
    return (
      <div className="flex-1 grid place-items-center" data-testid="pipeline-versions">
        <div className="w-6 h-6 border-2 border-pipe-300 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  const fromItem = from !== null ? byNumber.get(from) : undefined;
  const toItem = to !== null ? byNumber.get(to) : undefined;
  const fromIsCurrent = from === current;
  const options = history.versions.map((v) => (
    <option key={v.version} value={v.version}>
      {`v${v.version} · ${versionTitle(v)}${v.version === current ? ' (current)' : ''}`}
    </option>
  ));

  const sameNote = (() => {
    if (!diff || from === null || to === null) return null;
    if (from === to) return 'Pick two different versions.';
    if (diff.identical || diff.layoutOnly) {
      const hi = Math.max(from, to);
      const lo = Math.min(from, to);
      const restored = restoredFrom(byNumber.get(hi)) === lo ? ` Version ${hi} restored version ${lo}.` : '';
      return diff.layoutOnly
        ? `Only the steps' places on the canvas differ.${restored}`
        : `These two versions are the same.${restored}`;
    }
    return null;
  })();

  return (
    <div className="flex-1 min-h-0 flex" data-testid="pipeline-versions">
      {/* Every kept version, newest first. */}
      <aside className="w-80 shrink-0 flex flex-col min-h-0 border-r border-slate-200 bg-white" aria-label="Versions">
        <div className="flex items-center justify-between px-4 pt-3 pb-2">
          <b className="text-[11px] font-bold uppercase tracking-wider text-slate-500">Versions</b>
          <span className="text-[11.5px] text-slate-500">{history.versions.length} kept</span>
        </div>
        <div className="flex-1 min-h-0 overflow-auto px-2 pb-2">
          {history.versions.length === 1 && (
            <p className="px-2 pb-2 text-xs text-slate-500">This pipeline has one version so far. Every save that changes it adds one.</p>
          )}
          {items.map((it) => {
            if (it.kind === 'autos') {
              const openGroup = openGroups.has(it.key);
              const newest = it.versions[0];
              const oldest = it.versions[it.versions.length - 1];
              return (
                <div key={`g${it.key}`}>
                  <button
                    type="button"
                    aria-expanded={openGroup}
                    onClick={() => setOpenGroups((s) => {
                      const n = new Set(s);
                      if (n.has(it.key)) n.delete(it.key); else n.add(it.key);
                      return n;
                    })}
                    className="my-0.5 w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg border border-dashed border-slate-300 bg-slate-50 text-xs font-semibold text-slate-600 hover:bg-slate-100"
                  >
                    <Glyph d={openGroup ? ICON.chevD : ICON.chevR} size={12} />
                    {it.versions.length} auto-saves · {versionWhen(oldest.created_at)} to {versionWhen(newest.created_at).split(', ').pop()}
                  </button>
                  {openGroup && it.versions.map((v) => (
                    <VersionRow key={v.version} v={v} current={current} picked={from} onPick={pick} onMenu={(anchor) => setMenu({ item: v, anchor })} history={history} />
                  ))}
                </div>
              );
            }
            return (
              <VersionRow key={it.v.version} v={it.v} current={current} picked={from} onPick={pick} onMenu={(anchor) => setMenu({ item: it.v, anchor })} history={history} />
            );
          })}
        </div>
        <p className="border-t border-slate-200 px-4 py-2.5 text-[11.5px] leading-snug text-slate-500">{keepNote(history)}</p>
      </aside>

      {/* The comparison. */}
      <section className="flex-1 min-w-0 flex flex-col bg-slate-50" aria-label="Compare versions">
        <div className="shrink-0 grid gap-2 border-b border-slate-200 bg-white px-5 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="version-from" className="text-[12.5px] font-semibold text-slate-600">Compare</label>
            <select
              id="version-from"
              value={from ?? ''}
              onChange={(e) => setFrom(Number(e.target.value))}
              className="max-w-[230px] rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-[12.5px] font-semibold text-slate-800"
            >
              {options}
            </select>
            <label htmlFor="version-to" className="text-[12.5px] font-semibold text-slate-600">with</label>
            <select
              id="version-to"
              value={to ?? ''}
              onChange={(e) => setTo(Number(e.target.value))}
              className="max-w-[230px] rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-[12.5px] font-semibold text-slate-800"
            >
              {options}
            </select>
            <div className="ml-auto flex items-center gap-1.5">
              {fromItem && !fromIsCurrent && (
                <>
                  <span className="mr-0.5 rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-bold text-blue-700">Version {fromItem.version}</span>
                  <ActionButton icon={ICON.eye} label="Open" title={`Open version ${fromItem.version} read-only in a tab`} onClick={() => onOpenVersion(fromItem.version)} />
                  {canEdit && <ActionButton icon={ICON.restore} label="Restore…" title={`Restore version ${fromItem.version}`} onClick={() => void actions.restore(fromItem.version)} />}
                  {canEdit && <ActionButton icon={ICON.copy} label="New pipeline" title={`New pipeline from version ${fromItem.version}`} onClick={() => void actions.copy(fromItem.version)} />}
                </>
              )}
              {fromItem && (
                <button
                  type="button"
                  aria-label={`More for version ${fromItem.version}`}
                  onClick={(e) => setMenu({ item: fromItem, anchor: (e.currentTarget as HTMLElement).getBoundingClientRect() })}
                  className="grid h-[30px] w-[30px] place-items-center rounded-lg border border-slate-300 bg-white text-slate-600 hover:bg-slate-50"
                >
                  <Glyph d={ICON.dots} size={16} />
                </button>
              )}
            </div>
          </div>
          <div className="flex min-h-[24px] flex-wrap items-center gap-1.5" data-testid="version-summary">
            {!diff && !pairError && from !== null && <span className="text-xs text-slate-500">Reading the two versions…</span>}
            {pairError && <span className="text-xs text-red-600">{pairError}</span>}
            {from === null && <span className="text-xs text-slate-500">There is no older version to compare yet.</span>}
            {diff && sameNote && (
              <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-bold text-emerald-700">
                <Glyph d={ICON.check} size={12} />{sameNote}
              </span>
            )}
            {diff && !sameNote && (
              <>
                {diff.added > 0 && <Chip cls="bg-emerald-100 text-emerald-700">+{diff.added} added</Chip>}
                {diff.removed > 0 && <Chip cls="bg-red-100 text-red-700">−{diff.removed} removed</Chip>}
                {diff.changed > 0 && <Chip cls="bg-amber-100 text-amber-700">{diff.changed} changed</Chip>}
                {diff.linksChanged && <Chip cls="bg-indigo-100 text-indigo-700">Links changed</Chip>}
                {diff.pipeline.length > 0 && <Chip cls="bg-indigo-100 text-indigo-700">Pipeline settings changed</Chip>}
                {diff.same > 0 && <Chip cls="bg-slate-100 text-slate-600">{diff.same} the same</Chip>}
              </>
            )}
            <div className="ml-auto inline-flex rounded-lg bg-slate-100 p-0.5" role="group" aria-label="Show the changes">
              {(['canvas', 'list'] as View[]).map((v) => (
                <button
                  key={v}
                  type="button"
                  aria-pressed={view === v}
                  onClick={() => setView(v)}
                  className={`rounded-md px-2.5 py-1 text-xs font-semibold ${view === v ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600 hover:text-slate-800'}`}
                >
                  {v === 'canvas' ? 'On the canvas' : 'List'}
                </button>
              ))}
            </div>
          </div>
        </div>

        {diff && from !== null && to !== null ? (
          view === 'canvas' ? (
            <div className="flex-1 min-h-0 flex flex-col overflow-y-auto" data-testid="version-compare-canvas-view">
              <StepCanvas flow={flowForDiff(diff, selected)} flowKey={`${from}-${to}`} onPick={setSelected} legend />
              <StepDock step={diff.steps.find((s) => s.id === selected) || null} from={from} to={to} />
            </div>
          ) : (
            <ChangeList diff={diff} from={from} to={to} toIsCurrent={to === current} />
          )
        ) : (
          <div className="flex-1" />
        )}
        {toItem && fromItem && (
          <p className="sr-only" aria-live="polite">
            Comparing version {fromItem.version} with version {toItem.version}.
          </p>
        )}
      </section>

      {menu && (
        <MoreMenu
          anchor={menu.anchor}
          item={menu.item}
          isCurrent={menu.item.version === current}
          canEdit={canEdit}
          onClose={() => setMenu(null)}
          actions={actions}
          onOpen={onOpenVersion}
        />
      )}
    </div>
  );
}

function Chip({ cls, children }: { cls: string; children: ReactNode }) {
  return <span className={`rounded-full px-2.5 py-0.5 text-xs font-bold ${cls}`}>{children}</span>;
}

function ActionButton({ icon, label, title, onClick }: { icon: string; label: string; title: string; onClick: () => void }) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:border-slate-400 hover:bg-slate-50"
    >
      <Glyph d={icon} size={13} />{label}
    </button>
  );
}

function VersionRow({
  v,
  current,
  picked,
  onPick,
  onMenu,
  history,
}: {
  v: VersionHistoryItem;
  current: number | null;
  picked: number | null;
  onPick: (v: VersionHistoryItem) => void;
  onMenu: (anchor: DOMRect) => void;
  history: VersionHistory;
}) {
  const isCurrent = v.version === current;
  const isPicked = !isCurrent && v.version === picked;
  const live = v.live ?? (isCurrent && history.status === 'published');
  if (v.kind === 'review') {
    // A review step changes no step: it marks the version it was made on.
    return (
      <div className="relative group">
        <button
          type="button"
          onClick={() => onPick(v)}
          aria-pressed={isPicked}
          className={`w-full flex items-center gap-2 rounded-lg py-1.5 pl-11 pr-9 text-left text-[11.5px] ${isPicked ? 'bg-blue-50 ring-1 ring-blue-200' : 'hover:bg-slate-50'}`}
        >
          <Glyph d={ICON.shield} size={13} className="text-slate-500" />
          <span className="min-w-0 truncate text-slate-600"><b className="font-semibold text-slate-700">{versionTitle(v)}</b> · {versionWhen(v.created_at)}</span>
        </button>
        <span className="absolute left-3 top-1.5 text-[10px] font-bold text-slate-400">v{v.version}</span>
      </div>
    );
  }
  const runs = v.runs > 0
    ? (
      <>
        {plural(v.runs, 'run')} · last
        {runState(v.last_run_status) === 'ok' && <Glyph d={ICON.check} size={11} className="text-emerald-600" />}
        {runState(v.last_run_status) === 'fail' && <Glyph d={ICON.x} size={11} className="text-red-600" />}
        {runState(v.last_run_status) === 'other' && <span>{v.last_run_status}</span>}
        {versionWhen(v.last_run_at).split(', ')[0]}
      </>
    )
    : 'No runs';
  return (
    <div className="relative group">
      <button
        type="button"
        onClick={() => onPick(v)}
        aria-pressed={isPicked}
        data-version={v.version}
        className={`w-full grid grid-cols-[30px_minmax(0,1fr)] gap-2.5 rounded-xl py-2 pl-2 pr-9 text-left ${
          isPicked ? 'bg-blue-50 ring-1 ring-blue-200' : 'hover:bg-slate-50'
        }`}
      >
        <span className={`grid h-[30px] w-[30px] place-items-center rounded-full border text-[11px] font-bold ${
          isCurrent ? 'border-slate-800 bg-slate-800 text-white'
            : isPicked ? 'border-blue-600 bg-blue-600 text-white'
              : 'border-slate-200 bg-slate-50 text-slate-600'
        }`}>v{v.version}</span>
        <span className="min-w-0">
          <span className="flex flex-wrap items-center gap-1">
            <b className={`mr-0.5 text-[13px] ${v.kind === 'auto' ? 'font-medium text-slate-600' : 'font-semibold text-slate-900'}`}>{versionTitle(v)}</b>
            {isCurrent && <Pill cls="bg-slate-800 text-white">Current</Pill>}
            {live && <Pill cls="bg-emerald-100 text-emerald-700">Live</Pill>}
            {v.deployed && <Pill cls="bg-emerald-100 text-emerald-700">Live in PROD</Pill>}
            {v.published && <Pill cls="bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200">Published</Pill>}
            {v.name && <Pill cls="bg-amber-100 text-amber-800">Named</Pill>}
            {v.kind === 'restored' && <Pill cls="bg-violet-100 text-violet-700">Restored</Pill>}
            {v.kind === 'auto' && <Pill cls="bg-slate-100 text-slate-600">Auto-save</Pill>}
          </span>
          {v.name && v.change_summary && <span className="mt-0.5 block truncate text-xs text-slate-600">{v.change_summary}</span>}
          <span className="mt-0.5 block text-[11.5px] text-slate-500">{savedBy(v)} · {versionWhen(v.created_at)}</span>
          <span className="mt-0.5 flex items-center gap-1 text-[11.5px] text-slate-600">
            <Glyph d={ICON.clock} size={11} className="text-slate-400" />{runs}
          </span>
        </span>
      </button>
      <button
        type="button"
        aria-label={`More for version ${v.version}`}
        onClick={(e) => onMenu((e.currentTarget as HTMLElement).getBoundingClientRect())}
        className={`absolute right-1.5 top-2 grid h-6 w-6 place-items-center rounded-md text-slate-500 hover:bg-slate-200 hover:text-slate-800 focus-visible:opacity-100 ${
          isPicked || isCurrent ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
        }`}
      >
        <Glyph d={ICON.dots} size={16} />
      </button>
    </div>
  );
}

function Pill({ cls, children }: { cls: string; children: ReactNode }) {
  return <span className={`whitespace-nowrap rounded-full px-1.5 py-px text-[10.5px] font-bold ${cls}`}>{children}</span>;
}

// ── One version, read-only, in a tab of its own ─────────────────────────────

export interface VersionReadOnlyProps {
  pipelineId: string;
  pipelineName: string;
  version: number;
  projectId: string | null;
  folderId: string | null;
  canEdit: boolean;
  /** Go to the pipeline's Versions view comparing this version with the current one. */
  onCompare: (version: number) => void;
  onChanged?: () => void;
}

export function VersionReadOnly({
  pipelineId,
  pipelineName,
  version,
  projectId,
  folderId,
  canEdit,
  onCompare,
  onChanged,
}: VersionReadOnlyProps) {
  const { history, reload } = useHistory(pipelineId, 0);
  const readContent = useVersionContent(pipelineId);
  const actions = useVersionActions({ pipelineId, pipelineName, projectId, folderId, history, readContent, reload, onChanged });
  const [wf, setWf] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const names = useConnectionNames();

  useEffect(() => {
    let cancelled = false;
    setWf(null);
    setError(null);
    readContent(version)
      .then((w) => { if (!cancelled) setWf(w); })
      .catch((err: any) => { if (!cancelled) setError(err?.message || `Version ${version} could not be read. It may have been removed.`); });
    return () => { cancelled = true; };
  }, [version, readContent]);

  const item = history?.versions.find((v) => v.version === version);
  const self = useMemo(() => (wf ? diffVersions(wf, wf, { names }) : null), [wf, names]);
  const step = self?.steps.find((s) => s.id === selected) || null;

  return (
    <div className="flex-1 min-h-0 flex flex-col" data-testid="version-read-only">
      <div className="shrink-0 flex flex-wrap items-center gap-3 border-b border-violet-200 bg-violet-50 px-5 py-2.5">
        <Glyph d={ICON.eye} size={16} className="text-violet-700" />
        <div className="min-w-0">
          <b className="block truncate text-[13.5px] text-violet-950">
            {pipelineName} · version {version}{item ? ` · ${versionTitle(item)}` : ''}
          </b>
          <span className="text-xs text-violet-700">
            {item ? `Saved by ${savedBy(item)}, ${versionWhen(item.created_at)}. ` : ''}Read-only: nothing here changes the pipeline.
          </span>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          {history && version !== history.current_version && (
            <ActionButton icon={ICON.eye} label="Compare with current" title="Compare this version with the current one" onClick={() => onCompare(version)} />
          )}
          {canEdit && history && version !== history.current_version && (
            <ActionButton icon={ICON.restore} label="Restore…" title={`Restore version ${version}`} onClick={() => void actions.restore(version)} />
          )}
          {canEdit && (
            <ActionButton icon={ICON.copy} label="New pipeline" title={`New pipeline from version ${version}`} onClick={() => void actions.copy(version)} />
          )}
          <ActionButton icon={ICON.down} label="Download" title={`Download version ${version} as a file`} onClick={() => void actions.download(version)} />
        </div>
      </div>
      {error ? (
        <div className="flex-1 grid place-items-center p-8 text-center text-sm text-slate-600">{error}</div>
      ) : !wf || !self ? (
        <div className="flex-1 grid place-items-center">
          <div className="w-6 h-6 border-2 border-pipe-300 border-t-transparent rounded-full animate-spin" />
        </div>
      ) : (
        <>
          <StepCanvas flow={flowForVersion(wf, selected)} flowKey={`ro-${version}`} onPick={setSelected} legend={false} />
          {step ? (
            <div className="h-56 shrink-0 border-t border-slate-300 bg-white px-5 py-3 overflow-auto">
              <StepHead type={step.type} name={step.name} kind="plain" />
              <table className="mt-2 w-full table-fixed border-collapse text-[12.5px]">
                <tbody>
                  {step.rows.map((r) => (
                    <tr key={r.key} className="border-t border-slate-100 align-top">
                      <th className="w-48 px-2 py-1.5 text-left font-semibold text-slate-600 break-words">{r.label}</th>
                      <td className="px-2 py-1.5 whitespace-pre-wrap break-words text-slate-700">{r.after}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="h-56 shrink-0 border-t border-slate-300 bg-white px-5 py-4 text-[12.5px] text-slate-500">
              Click a step to see its settings in version {version}.
            </div>
          )}
        </>
      )}
    </div>
  );
}
