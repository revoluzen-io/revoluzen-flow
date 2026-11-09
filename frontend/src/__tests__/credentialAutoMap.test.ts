import { describe, expect, it } from 'vitest';
import { credentialAutoMap } from '../components/pages/ConnectionsPage';

const field = (key: string, type = 'text') => ({ key, label: key, type, placeholder: '' }) as any;

describe('credentialAutoMap', () => {
  it('fills the fields a credential holds under the same name', () => {
    const fields = [field('host'), field('username'), field('password', 'password')];
    expect(credentialAutoMap(fields, ['username', 'password'])).toEqual({ username: 'username', password: 'password' });
  });

  it('leaves everything else for the person to fill in', () => {
    expect(credentialAutoMap([field('host'), field('database')], ['username', 'password'])).toEqual({});
  });

  it('recognises another name for the same secret, preferring the exact one', () => {
    expect(credentialAutoMap([field('token', 'password')], ['api_key'])).toEqual({ token: 'api_key' });
    expect(credentialAutoMap([field('token', 'password')], ['api_key', 'token'])).toEqual({ token: 'token' });
  });

  it('never fills a choice, a switch or a long text field', () => {
    const fields = [field('auth_type', 'select'), field('ssl', 'checkbox'), field('private_key', 'textarea')];
    expect(credentialAutoMap(fields, ['auth_type', 'ssl', 'private_key'])).toEqual({});
  });
});
