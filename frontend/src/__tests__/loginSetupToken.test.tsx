/**
 * First-run setup token on server-mode installs (2026-11-13 pen test).
 *
 * The sign-up form asks for the one-time token only when /auth/signup-policy
 * says so, and sends it with the first registration.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import LoginPage from '../components/pages/LoginPage';
import { api } from '../api/client';

vi.mock('../api/client', () => ({
  api: {
    getPasswordPolicy: vi.fn(),
    getSignupPolicy: vi.fn(),
    checkPassword: vi.fn(),
    verifyResetToken: vi.fn(),
    getMe: vi.fn(),
    register: vi.fn(),
    login: vi.fn(),
  },
}));

const policy = (overrides: Record<string, unknown>) => ({
  allow_self_registration: false,
  first_user_bootstrap: true,
  bootstrap_password_pending: false,
  bootstrap_admin_email: null,
  setup_token_required: false,
  ...overrides,
});

beforeEach(() => {
  vi.mocked(api.getPasswordPolicy).mockResolvedValue({ min_length: 8, rules: [] } as any);
  vi.mocked(api.checkPassword).mockResolvedValue({ ok: true, score: 4, label: 'strong', failures: [], suggestions: [] } as any);
  vi.mocked(api.getMe).mockRejectedValue(new Error('no session') as any);
  vi.mocked(api.verifyResetToken).mockResolvedValue({ valid: false } as any);
  vi.mocked(api.register).mockResolvedValue({ token: 't', user: { id: 'u1', email: 'op@corp.test', role: 'super_admin' }, workspaces: [] } as any);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('first-run setup token', () => {
  it('asks for the token on an empty server and sends it', async () => {
    vi.mocked(api.getSignupPolicy).mockResolvedValue(policy({ setup_token_required: true }) as any);
    render(<LoginPage onLogin={() => {}} />);

    const field = await screen.findByLabelText(/setup token/i);
    fireEvent.change(field, { target: { value: '  tok-from-the-log  ' } });
    fireEvent.change(screen.getByLabelText(/^email/i), { target: { value: 'op@corp.test' } });
    fireEvent.change(screen.getByLabelText(/^password/i), { target: { value: 'Str0ng-Passw0rd!' } });
    fireEvent.click(screen.getByRole('button', { name: /create first account/i }));

    await waitFor(() => expect(api.register).toHaveBeenCalled());
    expect(vi.mocked(api.register).mock.calls[0][3]).toBe('tok-from-the-log');
  });

  it('does not show the field when no token is required', async () => {
    vi.mocked(api.getSignupPolicy).mockResolvedValue(policy({ setup_token_required: false }) as any);
    render(<LoginPage onLogin={() => {}} />);
    await screen.findByRole('button', { name: /create first account/i });
    expect(screen.queryByLabelText(/setup token/i)).toBeNull();
  });
});
