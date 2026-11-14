import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ParametersDialog from '../components/ParametersDialog';
import RunWithParametersDialog from '../components/RunWithParametersDialog';
import type { WorkflowParameter } from '../stores/workflowStore';
import { SECRET_PLACEHOLDER, isSecretParameter, parameterNameSaysSecret } from '../utils/storedSecrets';
import { buildRequestBody } from '../utils/triggerCommand';

/**
 * A pipeline parameter's default can be a secret. The server keeps it encrypted
 * and sends it back only as dots, so these two dialogs must never treat the
 * dots as the value: not when the default is edited, and not when a run starts.
 */

const store = vi.hoisted(() => ({
  parameters: [] as unknown[],
  setParameters: vi.fn(),
}));

vi.mock('../stores/workflowStore', () => ({
  useWorkflowStore: (selector: (state: typeof store) => unknown) => selector(store),
}));

const param = (over: Partial<WorkflowParameter> & { name: string }): WorkflowParameter => ({
  type: 'string', default: null, description: '', required: false, ...over,
});

beforeEach(() => {
  store.parameters = [];
  store.setParameters.mockClear();
});
afterEach(cleanup);

function defaultBox(row: number): HTMLInputElement {
  const body = document.querySelector('tbody') as HTMLElement;
  const cells = (body.querySelectorAll('tr')[row] as HTMLElement).querySelectorAll('td');
  return cells[2].querySelector('input') as HTMLInputElement;
}

function secretBox(row: number): HTMLInputElement | null {
  const body = document.querySelector('tbody') as HTMLElement;
  const cells = (body.querySelectorAll('tr')[row] as HTMLElement).querySelectorAll('td');
  return cells[5].querySelector('input');
}

it.each(['db_password', 'apiKey', 'STRIPE_API_KEY', 'github_token', 'smtp_pass'])('%s counts as a secret by name', (name) => {
  expect(parameterNameSaysSecret(name)).toBe(true);
});

it.each(['batch_size', 'max_tokens', 'merge_key', 'token_url', 'secret_name', 'pass_rate'])('%s stays readable', (name) => {
  expect(parameterNameSaysSecret(name)).toBe(false);
});

it('the switch makes any parameter a secret', () => {
  expect(isSecretParameter({ name: 'dsn', secret: true })).toBe(true);
  expect(isSecretParameter({ name: 'dsn' })).toBe(false);
});

it('hides a secret default and locks the switch when the name says secret', () => {
  store.parameters = [param({ name: 'api_token', default: SECRET_PLACEHOLDER }), param({ name: 'dataset', default: 'orders' })];
  render(<ParametersDialog open onClose={() => {}} />);

  expect(defaultBox(0).type).toBe('password');
  expect(secretBox(0)?.checked).toBe(true);
  expect(secretBox(0)?.disabled).toBe(true);

  expect(defaultBox(1).type).toBe('text');
  expect(defaultBox(1).value).toBe('orders');
  expect(secretBox(1)?.checked).toBe(false);
  expect(secretBox(1)?.disabled).toBe(false);
});

it('the switch hides the default of a parameter the name rule cannot know', () => {
  store.parameters = [param({ name: 'dsn', default: 'postgres://example.test/db' })];
  render(<ParametersDialog open onClose={() => {}} />);
  fireEvent.click(secretBox(0) as HTMLInputElement);
  expect(defaultBox(0).type).toBe('password');

  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(store.setParameters).toHaveBeenCalledWith([expect.objectContaining({ name: 'dsn', secret: true, default: 'postgres://example.test/db' })]);
});

it('typing over a stored secret replaces it without keeping the dots', () => {
  store.parameters = [param({ name: 'api_token', default: SECRET_PLACEHOLDER })];
  render(<ParametersDialog open onClose={() => {}} />);
  fireEvent.change(defaultBox(0), { target: { value: `${SECRET_PLACEHOLDER}new-value` } });

  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(store.setParameters).toHaveBeenCalledWith([expect.objectContaining({ default: 'new-value' })]);
});

it('leaving a stored secret alone sends the dots back, which the server reads as keep', () => {
  store.parameters = [param({ name: 'api_token', default: SECRET_PLACEHOLDER })];
  render(<ParametersDialog open onClose={() => {}} />);
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(store.setParameters).toHaveBeenCalledWith([expect.objectContaining({ default: SECRET_PLACEHOLDER })]);
});

it('switching off a stored secret clears it, since the stored value cannot be shown', () => {
  store.parameters = [param({ name: 'dsn', default: SECRET_PLACEHOLDER, secret: true })];
  render(<ParametersDialog open onClose={() => {}} />);
  fireEvent.click(secretBox(0) as HTMLInputElement);
  expect(defaultBox(0).type).toBe('text');
  expect(defaultBox(0).value).toBe('');

  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(store.setParameters).toHaveBeenCalledWith([expect.objectContaining({ default: null, secret: false })]);
});

it('offers no switch for a number or a true/false', () => {
  store.parameters = [param({ name: 'batch_size', type: 'int', default: 500 }), param({ name: 'dry', type: 'bool', default: false })];
  render(<ParametersDialog open onClose={() => {}} />);
  expect(secretBox(0)).toBeNull();
  expect(secretBox(1)).toBeNull();
});

it('starts a run without sending the dots, so the server uses the stored default', () => {
  const onRun = vi.fn();
  render(
    <RunWithParametersDialog
      open
      onClose={() => {}}
      workflowName="Orders"
      onRun={onRun}
      parameters={[param({ name: 'api_token', default: SECRET_PLACEHOLDER, required: true }), param({ name: 'day', default: '2026-11-11' })]}
    />,
  );
  const box = screen.getByPlaceholderText('Stored value (leave empty to use it)') as HTMLInputElement;
  expect(box.type).toBe('password');
  expect(box.value).toBe('');

  fireEvent.click(screen.getByRole('button', { name: 'Run' }));
  expect(onRun).toHaveBeenCalledWith({ day: '2026-11-11' });
});

it('sends a value typed for this run', () => {
  const onRun = vi.fn();
  render(
    <RunWithParametersDialog
      open
      onClose={() => {}}
      workflowName="Orders"
      onRun={onRun}
      parameters={[param({ name: 'api_token', default: SECRET_PLACEHOLDER })]}
    />,
  );
  fireEvent.change(screen.getByPlaceholderText('Stored value (leave empty to use it)'), { target: { value: 'typed-now' } });
  fireEvent.click(screen.getByRole('button', { name: 'Run' }));
  expect(onRun).toHaveBeenCalledWith({ api_token: 'typed-now' });
});

it('still asks for a required secret with nothing stored', () => {
  const onRun = vi.fn();
  render(
    <RunWithParametersDialog
      open
      onClose={() => {}}
      workflowName="Orders"
      onRun={onRun}
      parameters={[param({ name: 'api_token', required: true })]}
    />,
  );
  expect((screen.getByRole('button', { name: 'Run' }) as HTMLButtonElement).disabled).toBe(true);
});

it('leaves a stored secret out of the example request, so the server default applies', () => {
  expect(buildRequestBody([
    { name: 'api_token', type: 'string', default: SECRET_PLACEHOLDER },
    { name: 'region', type: 'string', default: 'EU' },
  ])).toEqual({ region: 'EU' });
});
