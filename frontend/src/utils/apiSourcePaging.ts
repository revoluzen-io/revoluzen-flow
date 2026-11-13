// What the API Source step does with its Operation and Pagination settings,
// so the form shows what will run (2026-11-11). It mirrors
// `ApiSourceNode._pagination_style` in backend/revoluzen_flow/nodes/activities.py.
//
// The step pages when Operation is "paginate". A step saved without an
// Operation (by a template, the Copilot or the API) pages when it names a
// paging style; the form used to show "fetch" for it although it paged.
export function apiSourceOperation(params: Record<string, any> | null | undefined): 'fetch' | 'paginate' {
  const operation = String(params?.operation ?? '').trim().toLowerCase();
  if (operation) return operation === 'paginate' ? 'paginate' : 'fetch';
  const style = String(params?.pagination ?? '').trim().toLowerCase();
  return style && style !== 'none' ? 'paginate' : 'fetch';
}
