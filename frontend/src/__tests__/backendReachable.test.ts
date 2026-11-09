import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiRequest } from '../api/client';

const seen: boolean[] = [];
window.addEventListener('revoluzen-flow:backend-reachable', (e) => {
  seen.push((e as CustomEvent).detail.reachable);
});

const answers = () => Promise.resolve(new Response('{}', { status: 200 }));

describe('the backend-unreachable banner', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    seen.length = 0;
  });

  it('stays quiet when one request drops but the server still answers', async () => {
    const fetchMock = vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch')).mockImplementation(answers);
    vi.stubGlobal('fetch', fetchMock);
    await expect(apiRequest('/pool/status')).rejects.toThrow('Failed to fetch');
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/health', { method: 'GET' }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(seen).toEqual([]);
  });

  it('says nothing about a request that was cancelled', async () => {
    const fetchMock = vi.fn().mockRejectedValueOnce(new DOMException('Aborted', 'AbortError'));
    vi.stubGlobal('fetch', fetchMock);
    await expect(apiRequest('/pool/status')).rejects.toThrow();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(seen).toEqual([]);
  });

  it('speaks up when the server does not answer either, and clears when it comes back', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(apiRequest('/pool/status')).rejects.toThrow('Failed to fetch');
    await vi.waitFor(() => expect(seen).toEqual([false]));
    vi.stubGlobal('fetch', vi.fn().mockImplementation(answers));
    await apiRequest('/pool/status');
    expect(seen).toEqual([false, true]);
  });
});
