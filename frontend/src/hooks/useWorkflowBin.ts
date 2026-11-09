import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { WorkflowBinItem } from '../api/client';

/**
 * The Bin list on the Pipelines page.
 *
 * Everyone in the workspace shares the Bin, so a pipeline someone else deleted
 * (or restored) since the last read would not show until the page was reloaded.
 * The list is therefore read when the page opens, again every time the Bin tab is
 * opened (`active` turns true), and whenever the page calls `reload` after one of
 * its own changes.
 *
 * Only the latest read is kept: if an older one finishes after a newer one, it is
 * dropped. A read that fails leaves the list as it was.
 */
export function useWorkflowBin(active: boolean) {
  const [items, setItems] = useState<WorkflowBinItem[]>([]);
  const [retentionDays, setRetentionDays] = useState(30);
  const [loading, setLoading] = useState(true);
  const latest = useRef(0);

  const reload = useCallback(async () => {
    const mine = ++latest.current;
    try {
      const bin = await api.listWorkflowBin();
      if (mine !== latest.current) return;
      setItems(Array.isArray(bin?.items) ? bin.items : []);
      if (typeof bin?.retention_days === 'number') setRetentionDays(bin.retention_days);
    } catch {
      /* best-effort: the tab keeps what it last had */
    } finally {
      if (mine === latest.current) setLoading(false);
    }
  }, []);

  // When the page opens.
  useEffect(() => { void reload(); }, [reload]);
  // Each time the Bin tab is opened.
  useEffect(() => { if (active) void reload(); }, [active, reload]);

  return { items, retentionDays, loading, reload };
}
