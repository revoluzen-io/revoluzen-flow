/**
 * The editor's address names the pipeline on screen (`#editor/<id>`), so a
 * link, a bookmark, a reload or Back opens that pipeline in its tab. With no
 * id the editor shows the tab used last, or an empty canvas in a tab of its
 * own.
 *
 * A pipeline that cannot be opened (deleted, or not in the workspace the
 * person is in) is said so plainly, and the editor goes on with what it shows
 * when no pipeline is named (`ensureEditorTab` in stores/workflowStore.ts).
 */
import { useEffect } from 'react';

import { readCurrentPage, readSubRoute } from '../../router';
import { useWorkflowStore } from '../../stores/workflowStore';

/**
 * Keeps the editor and its address together while `active`: App passes true
 * on the editor page once someone is signed in, so a link opened while signed
 * out waits for the sign-in and then opens.
 */
export function useEditorAddress(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    // The address shows the tab on screen: its pipeline, or none for a
    // pipeline not saved yet or a past run.
    const syncAddress = () => {
      if (readCurrentPage() !== 'editor') return;
      const s = useWorkflowStore.getState();
      if (!s.activeTabKey) return;
      const want = s.workflowId && !s.pastRunId ? `#editor/${s.workflowId}` : '#editor';
      if (window.location.hash !== want) history.replaceState(null, '', want);
    };
    // Only the address followed last writes the address back, so an open
    // that is still loading never puts an older address over a newer one,
    // not even after the editor was left and entered again meanwhile.
    let latest = 0;
    const follow = async () => {
      if (readCurrentPage() !== 'editor') return;
      const mine = ++latest;
      await useWorkflowStore.getState().ensureEditorTab(readSubRoute());
      if (mine === latest) syncAddress();
    };
    void follow();
    window.addEventListener('hashchange', follow);
    // A tab picked, a first save or a tab closed: the address follows.
    const unsubscribe = useWorkflowStore.subscribe((s, prev) => {
      if (s.activeTabKey !== prev.activeTabKey || s.workflowId !== prev.workflowId || s.pastRunId !== prev.pastRunId) {
        syncAddress();
      }
    });
    return () => {
      latest += 1; // an open still loading from here writes nothing back
      window.removeEventListener('hashchange', follow);
      unsubscribe();
    };
  }, [active]);
}
