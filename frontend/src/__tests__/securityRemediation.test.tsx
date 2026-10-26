import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TrustPage from '../components/pages/TrustPage';
import { api } from '../api/client';

vi.mock('../hooks/usePageContext', () => ({ usePageContext: vi.fn() }));

const FIX = 'icacls "C:\\data\\secret.key" /inheritance:r /grant:r "*S-1-5-18:(F)"';

function posture(control: Record<string, unknown>) {
  return {
    as_of: '2026-10-26T12:00:00Z',
    sovereignty: {
      telemetry_currently_enabled: null,
      active_provider_summary: {
        provider: 'openai', model: 'test-model', is_local: false, status: 'configured',
      },
    },
    security_baseline: [{
      key: 'master_key_perms',
      label: 'Master key permissions',
      detail: 'Read the NTFS ACL. Covers the file DACL only.',
      checked_at: null,
      evidence: 'exposed: Everyone',
      ...control,
    }],
  };
}

const FAILING = posture({ status: 'failed', remediation: FIX, can_auto_fix: true });
const FIXED = posture({ status: 'verified', remediation: null, can_auto_fix: false });

beforeEach(() => {
  vi.spyOn(api, 'getTrustEvalSummary').mockResolvedValue({ ran: false } as any);
  vi.spyOn(api, 'getCertMatrix').mockResolvedValue({ rows: [], total: 0 } as any);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); window.location.hash = ''; });

it('offers Fix this only for controls the server says it can fix', async () => {
  vi.spyOn(api, 'getTrustPosture').mockResolvedValue(
    posture({ status: 'failed', remediation: FIX, can_auto_fix: false }) as any,
  );
  render(<TrustPage embedded />);
  // The command is still shown — the operator can always run it by hand.
  expect(await screen.findByText(FIX)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Fix this' })).toBeNull();
});

it('applies the fix and re-reads the posture', async () => {
  const get = vi.spyOn(api, 'getTrustPosture')
    .mockResolvedValueOnce(FAILING as any)
    .mockResolvedValue(FIXED as any);
  const fix = vi.spyOn(api, 'remediateTrustControl').mockResolvedValue({
    control_key: 'master_key_perms',
    attempted: true,
    message: 'Reset the DACL on C:\\data\\secret.key.',
    control: FIXED.security_baseline[0],
  } as any);

  render(<TrustPage embedded />);
  fireEvent.click(await screen.findByRole('button', { name: 'Fix this' }));

  await waitFor(() => expect(fix).toHaveBeenCalledWith('master_key_perms'));
  // A re-fetch, not a local patch: the page must show what a fresh check saw.
  await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  expect(await screen.findByText('Verified')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Fix this' })).toBeNull();
});

it('says so when the fix ran but the control still fails', async () => {
  // The case that matters. Reporting the attempt instead of the re-check
  // would show success over a key that is still readable by everyone.
  vi.spyOn(api, 'getTrustPosture').mockResolvedValue(FAILING as any);
  vi.spyOn(api, 'remediateTrustControl').mockResolvedValue({
    control_key: 'master_key_perms',
    attempted: true,
    message: 'Reset the DACL.',
    control: FAILING.security_baseline[0],
  } as any);

  render(<TrustPage embedded />);
  fireEvent.click(await screen.findByRole('button', { name: 'Fix this' }));

  expect(await screen.findByText(/still reports failed/)).toBeInTheDocument();
  expect(screen.getByText('Failed')).toBeInTheDocument();
});

it('explains a permission refusal instead of showing a raw error', async () => {
  vi.spyOn(api, 'getTrustPosture').mockResolvedValue(FAILING as any);
  vi.spyOn(api, 'remediateTrustControl').mockRejectedValue(
    Object.assign(new Error('Forbidden'), { status: 403 }),
  );

  render(<TrustPage embedded />);
  fireEvent.click(await screen.findByRole('button', { name: 'Fix this' }));

  expect(await screen.findByText(/needs an admin account/)).toBeInTheDocument();
});
