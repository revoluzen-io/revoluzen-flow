import { api, authHeaders } from '../../api/client';
import type {
  Catalog, ReportDefinition, ReportDelivery, ReportResult, SavedReport,
} from './reportModel';

const BASE = '/api/reports';

export const reportsApi = {
  catalog: () => api.get<Catalog>(`${BASE}/builder/catalog`),
  preview: (definition: ReportDefinition) => api.post<ReportResult>(`${BASE}/builder/preview`, definition),
  list: () => api.get<{ reports: SavedReport[] }>(`${BASE}/saved`),
  get: (id: string) => api.get<SavedReport>(`${BASE}/saved/${encodeURIComponent(id)}`),
  create: (definition: ReportDefinition) => api.post<SavedReport>(`${BASE}/saved`, definition),
  update: (id: string, definition: ReportDefinition) => api.put<SavedReport>(`${BASE}/saved/${encodeURIComponent(id)}`, definition),
  remove: (id: string) => api.delete(`${BASE}/saved/${encodeURIComponent(id)}`),
  duplicate: (id: string) => api.post<SavedReport>(`${BASE}/saved/${encodeURIComponent(id)}/duplicate`),
  setSchedule: (id: string, enabled: boolean) => api.post<SavedReport>(`${BASE}/saved/${encodeURIComponent(id)}/schedule`, { enabled }),
  run: (id: string) => api.post<ReportResult>(`${BASE}/saved/${encodeURIComponent(id)}/run`),
  send: (id: string, toMe: boolean) => api.post<ReportDelivery>(`${BASE}/saved/${encodeURIComponent(id)}/send`, { to_me: toMe }),
  deliveries: (id: string) => api.get<{ deliveries: ReportDelivery[] }>(`${BASE}/saved/${encodeURIComponent(id)}/deliveries`),
  deliverySnapshot: (id: string, deliveryId: string) => api.get<ReportResult>(
    `${BASE}/saved/${encodeURIComponent(id)}/deliveries/${encodeURIComponent(deliveryId)}`,
  ),
};

/** Fetches the file and starts the browser's download. */
export async function downloadReport(id: string, format: 'pdf' | 'docx'): Promise<{ filename: string; savedToStorage: boolean }> {
  const res = await fetch(`${BASE}/saved/${encodeURIComponent(id)}/download?format=${format}`, { headers: authHeaders() });
  if (!res.ok) {
    let message = `Download failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body.detail === 'string') message = body.detail;
    } catch {
      /* the status is enough */
    }
    throw new Error(message);
  }
  const blob = await res.blob();
  const match = /filename="([^"]+)"/.exec(res.headers.get('content-disposition') || '');
  const filename = match ? match[1] : `report.${format}`;
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  return { filename, savedToStorage: Boolean(res.headers.get('x-storage-object-id')) };
}
