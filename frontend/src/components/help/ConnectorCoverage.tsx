import { useEffect, useMemo, useState } from 'react';

/**
 * Live connector-coverage strip for the Node Reference page.
 *
 * It answers one question: how mature are the connectors a user can set up?
 * "Can set up" means the types the New Connection page offers: everything
 * declared in ConnectionsPage.tsx except the entries marked 'roadmap'. That is
 * the same 66 the Dashboard, About and the Connections gallery quote.
 *
 * The live catalog (`/api/v1/catalog/connectors`) registers more types than
 * that (102 today): the 14 still marked Coming soon, plus 22 that the page
 * never lists. Its `count` is the size of the registry, not the number of
 * connectors, so it is no longer shown as the headline. The certification mix
 * is tallied over the offered types only, so the chips add up to the number in
 * the headline.
 *
 * Fails closed: on any fetch / shape error it renders nothing, so the node
 * list below always stands on its own. If the offered list cannot be loaded it
 * falls back to the registry totals and says that is what they are.
 */

type Tier = 'production' | 'certified' | 'configurable' | 'form_only' | 'declared_only';
type Maturity = Partial<Record<Tier, number>>;

interface ConnectorEntry {
  name: string;
  maturity?: string;
}

interface ConnectorCatalog {
  count: number;
  maturity_summary: Maturity;
  connectors?: ConnectorEntry[];
}

const TIERS: Array<{ key: Tier; label: string; cls: string }> = [
  { key: 'production',    label: 'Production',   cls: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
  { key: 'certified',     label: 'Certified',    cls: 'bg-blue-50 text-blue-700 border-blue-200' },
  { key: 'configurable',  label: 'Configurable', cls: 'bg-violet-50 text-violet-700 border-violet-200' },
  { key: 'form_only',     label: 'Form-only',    cls: 'bg-amber-50 text-amber-700 border-amber-200' },
  { key: 'declared_only', label: 'Declared',     cls: 'bg-slate-50 text-slate-600 border-slate-200' },
];

const TIER_KEYS = new Set<string>(TIERS.map((t) => t.key));

/**
 * The connection types the New Connection page offers. Loaded on demand so the
 * Connections page code is fetched only when this strip is on screen.
 */
async function loadOfferedTypes(): Promise<Set<string>> {
  const { CONNECTION_TYPES, connectorStatus } = await import('../pages/ConnectionsPage');
  return new Set(
    CONNECTION_TYPES.filter((t) => connectorStatus(t.type) !== 'roadmap').map((t) => t.type),
  );
}

export default function ConnectorCoverage() {
  const [data, setData] = useState<ConnectorCatalog | null>(null);
  // null = still loading; 'failed' = fall back to the registry totals.
  const [offered, setOffered] = useState<Set<string> | 'failed' | null>(null);

  useEffect(() => {
    let alive = true;
    fetch('/api/v1/catalog/connectors')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d: ConnectorCatalog) => { if (alive) setData(d); })
      .catch(() => { /* fail closed — the node list remains */ });
    loadOfferedTypes()
      .then((types) => { if (alive) setOffered(types); })
      .catch(() => { if (alive) setOffered('failed'); });
    return () => { alive = false; };
  }, []);

  const view = useMemo(() => {
    if (!data || typeof data.count !== 'number' || offered === null) return null;
    const registered = data.count;
    const entries = Array.isArray(data.connectors) ? data.connectors : [];
    if (offered !== 'failed' && entries.length > 0) {
      const mine = entries.filter((c) => offered.has(c.name));
      if (mine.length > 0) {
        const mix: Maturity = {};
        for (const c of mine) {
          if (c.maturity && TIER_KEYS.has(c.maturity)) {
            const key = c.maturity as Tier;
            mix[key] = (mix[key] ?? 0) + 1;
          }
        }
        return { scoped: true, count: mine.length, registered, mix };
      }
    }
    return { scoped: false, count: registered, registered, mix: data.maturity_summary || {} };
  }, [data, offered]);

  if (!view) return null;

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <div className="text-sm font-bold text-slate-800">
            {view.scoped
              ? `${view.count} connectors you can set up`
              : `${view.count} connector types registered`}
          </div>
          <div className="text-xs text-slate-500 mt-0.5 max-w-[75ch]">
            {view.scoped
              ? `These are the connectors on the New Connection page, which the Source and Destination nodes use. The catalog registers ${view.registered} types in all; the other ${view.registered - view.count} are not offered there. Certification level:`
              : 'Every type the catalog registers, including ones the New Connection page does not offer. Certification level:'}
          </div>
        </div>
        <a
          href="#cert-matrix"
          className="text-xs font-semibold text-pipe-600 hover:text-pipe-700 whitespace-nowrap"
        >
          Full catalog + cert matrix &rarr;
        </a>
      </div>
      <div className="flex items-center gap-2 mt-2 flex-wrap">
        {TIERS.map((t) => (
          <span key={t.key} className={`text-xs font-bold px-2 py-1 rounded border ${t.cls}`}>
            {view.mix[t.key] ?? 0} {t.label}
          </span>
        ))}
      </div>
    </div>
  );
}
