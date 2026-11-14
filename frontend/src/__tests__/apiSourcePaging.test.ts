import { describe, expect, it } from 'vitest';
import { apiSourceOperation } from '../utils/apiSourcePaging';

// The form must show what the step will do: it mirrors
// ApiSourceNode._pagination_style on the server (2026-11-11).
describe('apiSourceOperation', () => {
  it('follows Operation when it is set', () => {
    expect(apiSourceOperation({ operation: 'paginate' })).toBe('paginate');
    expect(apiSourceOperation({ operation: 'fetch', pagination: 'offset_limit' })).toBe('fetch');
  });

  it('treats the retired stream choice as one request', () => {
    expect(apiSourceOperation({ operation: 'stream', pagination: 'page_number' })).toBe('fetch');
  });

  it('shows paginate for a step saved with a paging style and no Operation', () => {
    // e.g. the "Add pagination (offset-based)" quick fix sets only pagination.
    expect(apiSourceOperation({ pagination: 'offset', page_size: 100 })).toBe('paginate');
    expect(apiSourceOperation({ pagination: 'none' })).toBe('fetch');
    expect(apiSourceOperation({})).toBe('fetch');
    expect(apiSourceOperation(undefined)).toBe('fetch');
  });
});
