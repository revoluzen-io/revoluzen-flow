import { useState } from 'react';
import { useWorkflowStore } from '../../stores/workflowStore';
import { uiConfirm } from '../../ui/dialog';
import { toast } from '../Toast';
import { pipelinesAddress } from '../pipelines/PipelineViewTabs';

/**
 * Says where the person stands when a published pipeline is open in the Editor.
 *
 * The published version cannot be changed from here. Saving makes a draft beside
 * it, and schedules, endpoints and every other run nobody is watching stay on
 * the published version until someone publishes again.
 *
 * Auto-save stays off on a published pipeline, so an edit made only to try
 * something never becomes a draft by itself.
 */
export default function PublishedBanner() {
  const isDirty = useWorkflowStore((s) => s.isDirty);
  const workflowId = useWorkflowStore((s) => s.workflowId);
  const live = useWorkflowStore((s) => s.publishedVersion);
  const hasDraft = useWorkflowStore((s) => s.hasUnpublishedChanges);
  const discardDraft = useWorkflowStore((s) => s.discardDraft);
  const [busy, setBusy] = useState(false);

  const hasNumber = typeof live === 'number';
  const liveName = hasNumber ? `Version ${live}` : 'The published version';

  const discard = async () => {
    const ok = await uiConfirm({
      title: 'Discard this draft?',
      message: `The pipeline goes back to ${live ? `version ${live}` : 'the published version'}. The draft stays in Versions if you want it later.`,
      confirmLabel: 'Discard draft',
      destructive: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await discardDraft();
      toast.success('Draft discarded', `Back to ${live ? `version ${live}` : 'the published version'}.`);
    } catch (err: any) {
      toast.error('Could not discard the draft', err?.message || 'Try again.');
    }
    setBusy(false);
  };

  return (
    <div className="px-4 py-2.5 bg-violet-50 border-b border-violet-200 flex flex-wrap items-center gap-x-3 gap-y-1 shrink-0" data-testid="published-banner">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-violet-600 shrink-0" aria-hidden="true">
        <rect x="3" y="11" width="18" height="11" rx="2" />
        <path d="M7 11V7a5 5 0 0 1 10 0v4" />
      </svg>
      <span className="text-xs font-medium text-violet-800 min-w-0 flex-1 basis-80">
        {hasDraft ? (
          <>
            <strong>Draft of a published pipeline.</strong> {liveName} is locked and keeps running.
            {isDirty
              ? ' You also have unsaved changes. Test and Run use them as they are.'
              : ' Publish this draft when it is ready, or discard it to go back.'}
          </>
        ) : (
          <>
            <strong>Published and locked.</strong> {liveName} keeps running whatever you do here.
            {isDirty
              ? ' You have unsaved changes. Test and Run use them as they are, and Save keeps them as a draft.'
              : ' Saving makes a draft, and nothing changes for schedules or endpoints until you publish it.'}
          </>
        )}
      </span>
      {workflowId && hasNumber && (hasDraft || isDirty) && (
        <a href={pipelinesAddress(`${workflowId}~v${live}`)} className="text-xs font-semibold text-violet-700 underline hover:text-violet-900">
          See what is live
        </a>
      )}
      {hasDraft && (
        <button
          type="button"
          onClick={() => void discard()}
          disabled={busy || isDirty}
          title={isDirty ? 'Save or undo your unsaved changes first' : 'Go back to what is live'}
          className="px-2.5 py-1 text-xs font-semibold text-violet-800 bg-white border border-violet-300 rounded-lg hover:bg-violet-100 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Discard draft
        </button>
      )}
    </div>
  );
}
