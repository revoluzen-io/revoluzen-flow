import { describe, expect, it } from 'vitest';
import {
  FILE_FORMATS, FORMAT_LABELS, TEXT_ENCODINGS, ENCODING_LABELS, columnProblems, columnsFromWidths,
  encryptedName, isPgpCredential, nextStart,
} from '../components/files/legacyFileOptions';

describe('fixed-width columns from widths', () => {
  it('counts start positions from 1', () => {
    const { columns, error } = columnsFromWidths('8, 30, 12', 'customer_id, name');
    expect(error).toBeUndefined();
    expect(columns.map((c) => [c.name, c.start, c.length])).toEqual([
      ['customer_id', 1, 8], ['name', 9, 30], ['column_3', 39, 12],
    ]);
  });

  it('says which width is wrong', () => {
    expect(columnsFromWidths('8, x', '').error).toContain('Width 2');
    expect(columnsFromWidths('', '').error).toContain('Type the widths');
  });

  it('starts a new column after the last one and finds problems', () => {
    expect(nextStart([{ name: 'a', start: 1, length: 8 }, { name: 'b', start: 9, length: 4 }])).toBe(13);
    expect(columnProblems([{ name: 'a', start: 0, length: 2 }, { name: 'A', start: 3, length: 0 }])).toEqual([
      'a: start must be 1 or more.', 'A: length must be 1 or more.', 'A: the name is used twice.',
    ]);
  });
});

describe('labels', () => {
  it('names every format and encoding in plain words', () => {
    for (const format of FILE_FORMATS) expect(FORMAT_LABELS[format]).toBeTruthy();
    for (const encoding of TEXT_ENCODINGS) expect(ENCODING_LABELS[encoding]).toBeTruthy();
    expect(FORMAT_LABELS.copybook).toBe('COBOL copybook records (mainframe)');
  });
});

describe('PGP options', () => {
  it('offers credentials that can do the job', () => {
    expect(isPgpCredential({ type: 'pgp' }, 'decrypt')).toBe(true);
    expect(isPgpCredential({ type: 'other', config: { private_key: '••••••••' } }, 'decrypt')).toBe(true);
    expect(isPgpCredential({ type: 'other', config: { public_key: 'x' } }, 'decrypt')).toBe(false);
    expect(isPgpCredential({ type: 'other', config: { public_key: 'x' } }, 'encrypt')).toBe(true);
    expect(isPgpCredential({ type: 'database', config: { password: 'x' } }, 'encrypt')).toBe(false);
  });

  it('offers the sender’s public key for checking and your private key for signing', () => {
    expect(isPgpCredential({ type: 'other', config: { public_key: 'x' } }, 'verify')).toBe(true);
    expect(isPgpCredential({ type: 'other', config: { public_key: 'x' } }, 'sign')).toBe(false);
    expect(isPgpCredential({ type: 'other', config: { private_key: 'x' } }, 'sign')).toBe(true);
    expect(isPgpCredential({ type: 'database', config: { password: 'x' } }, 'verify')).toBe(false);
  });

  it('reads the fields of a PGP key credential too, so a public key is not offered for decrypting or signing', () => {
    const publicOnly = { type: 'pgp', config: { public_key: '••••' } };
    expect(isPgpCredential(publicOnly, 'sign')).toBe(false);
    expect(isPgpCredential(publicOnly, 'decrypt')).toBe(false);
    expect(isPgpCredential(publicOnly, 'verify')).toBe(true);
    expect(isPgpCredential({ type: 'pgp', config: { private_key: '••••', passphrase: '••••' } }, 'encrypt')).toBe(true);
    expect(isPgpCredential({ type: 'other', config: { recipient_key: 'x' } }, 'encrypt')).toBe(true);
  });

  it('shows the name the encrypted file gets', () => {
    expect(encryptedName('out/orders.csv', false)).toBe('out/orders.csv.pgp');
    expect(encryptedName('out/orders.csv', true)).toBe('out/orders.csv.asc');
    expect(encryptedName('out/orders.csv.gpg', false)).toBe('out/orders.csv.gpg');
  });
});
