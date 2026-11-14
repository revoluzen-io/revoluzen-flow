/**
 * Choices and helpers for the File step's mainframe formats and PGP options.
 * Kept free of React so the rules can be tested on their own.
 *
 * The values match backend/revoluzen_flow/nodes/file_node.py and
 * backend/revoluzen_flow/legacy_files/.
 */

export const FILE_FORMATS = [
  'auto', 'csv', 'tsv', 'json', 'ndjson', 'parquet', 'excel', 'xml', 'orc', 'avro',
  'fixed_width', 'copybook',
];

export const FORMAT_LABELS: Record<string, string> = {
  auto: 'Auto (from the file name)',
  csv: 'CSV',
  tsv: 'TSV (tab separated)',
  json: 'JSON',
  ndjson: 'NDJSON (one JSON per line)',
  parquet: 'Parquet',
  excel: 'Excel',
  xml: 'XML',
  orc: 'ORC',
  avro: 'Avro',
  fixed_width: 'Fixed-width text',
  copybook: 'COBOL copybook records (mainframe)',
};

export const EBCDIC_CODE_PAGES = ['cp037', 'cp1140', 'cp1047', 'cp500', 'cp273', 'cp875', 'cp1026', 'cp424'];

export const ENCODING_LABELS: Record<string, string> = {
  'utf-8': 'UTF-8',
  'utf-8-sig': 'UTF-8 with BOM',
  'utf-16': 'UTF-16',
  'latin-1': 'Latin-1 (ASCII)',
  'iso-8859-1': 'ISO-8859-1',
  cp1252: 'Windows-1252',
  ascii: 'ASCII',
  cp037: 'EBCDIC 037 (US, Canada)',
  cp1140: 'EBCDIC 1140 (US, Canada, with euro)',
  cp1047: 'EBCDIC 1047 (z/OS UNIX)',
  cp500: 'EBCDIC 500 (International)',
  cp273: 'EBCDIC 273 (Germany, Austria)',
  cp875: 'EBCDIC 875 (Greek)',
  cp1026: 'EBCDIC 1026 (Turkish)',
  cp424: 'EBCDIC 424 (Hebrew)',
};

export const TEXT_ENCODINGS = ['utf-8', 'utf-8-sig', 'utf-16', 'latin-1', 'iso-8859-1', 'cp1252', 'ascii', ...EBCDIC_CODE_PAGES];
export const COPYBOOK_CODE_PAGES = [...EBCDIC_CODE_PAGES, 'latin-1', 'cp1252', 'utf-8'];

export const RECORD_FORMAT_LABELS: Record<string, string> = {
  lines: 'One record per line',
  fixed: 'Fixed length, no line breaks',
  rdw: 'Variable length, each record starts with its length (RDW)',
  bdw_rdw: 'Variable length in blocks (BDW and RDW)',
};

export const COLUMN_TYPE_LABELS: Record<string, string> = {
  text: 'Text',
  integer: 'Whole number',
  decimal: 'Number with a decimal point',
  implied: 'Number with implied decimals',
  date: 'Date',
};

export const TRIM_LABELS: Record<string, string> = {
  both: 'Remove spaces on both sides',
  right: 'Remove trailing spaces only',
  left: 'Remove leading spaces only',
  none: 'Keep spaces',
};

export const BAD_VALUE_LABELS: Record<string, string> = {
  stop: 'Stop the run and say where',
  empty: 'Leave that value empty and go on',
};

export interface FixedColumn {
  name: string;
  start: number;
  length: number;
  type?: string;
  decimals?: number;
  format?: string;
}

export interface RedefinesRule {
  branch: string;
  field: string;
  values: string;
}

/** Columns from a list of widths ("8, 30, 12") and optional names. */
export function columnsFromWidths(widths: string, names: string): { columns: FixedColumn[]; error?: string } {
  const parts = widths.split(/[,;\s]+/).map((w) => w.trim()).filter(Boolean);
  if (parts.length === 0) return { columns: [], error: 'Type the widths, for example 8, 30, 12.' };
  const nameList = names.split(/[,;\n]+/).map((n) => n.trim());
  const columns: FixedColumn[] = [];
  let start = 1;
  for (let i = 0; i < parts.length; i++) {
    const width = Number(parts[i]);
    if (!Number.isInteger(width) || width < 1) {
      return { columns: [], error: `Width ${i + 1} ("${parts[i]}") must be a whole number above 0.` };
    }
    columns.push({ name: nameList[i] || `column_${i + 1}`, start, length: width, type: 'text' });
    start += width;
  }
  return { columns };
}

/** Where the next column starts: right after the last one. */
export function nextStart(columns: FixedColumn[]): number {
  return columns.reduce((end, c) => Math.max(end, (Number(c.start) || 1) + (Number(c.length) || 0)), 1);
}

/** Problems a column list has before it is run. */
export function columnProblems(columns: FixedColumn[]): string[] {
  const problems: string[] = [];
  const seen = new Set<string>();
  columns.forEach((c, i) => {
    const label = c.name?.trim() || `column ${i + 1}`;
    if (!Number.isInteger(Number(c.start)) || Number(c.start) < 1) problems.push(`${label}: start must be 1 or more.`);
    if (!Number.isInteger(Number(c.length)) || Number(c.length) < 1) problems.push(`${label}: length must be 1 or more.`);
    const key = (c.name || '').trim().toLowerCase();
    if (key) {
      if (seen.has(key)) problems.push(`${label}: the name is used twice.`);
      seen.add(key);
    }
  });
  return problems;
}

/**
 * What a PGP key is used for in the File step: decrypt (your private key),
 * encrypt (the partner's public key), verify (the sender's public key, to
 * check their signature) or sign (your private key).
 */
export type PgpPurpose = 'decrypt' | 'encrypt' | 'verify' | 'sign';

// The field names backend/revoluzen_flow/openpgp/credentials.py reads.
const PRIVATE_KEY_FIELDS = ['private_key', 'pgp_private_key', 'secret_key', 'private_key_block'];
const PUBLIC_KEY_FIELDS = ['public_key', 'pgp_public_key', 'public_keys', 'recipient_key', 'recipient_public_key',
  'partner_public_key'];

/**
 * A credential the PGP options can use, by the fields it has: a private key
 * to decrypt or sign, a public (or private) key to encrypt or check a
 * signature. A PGP key credential whose fields are not known is offered.
 */
export function isPgpCredential(credential: { type?: string; config?: Record<string, unknown> }, purpose: PgpPurpose): boolean {
  const fields = Object.keys(credential.config || {}).map((k) => k.toLowerCase());
  if (fields.length === 0) return (credential.type || '').toLowerCase() === 'pgp';
  const hasPrivate = fields.some((f) => PRIVATE_KEY_FIELDS.includes(f));
  if (purpose === 'decrypt' || purpose === 'sign') return hasPrivate;
  return hasPrivate || fields.some((f) => PUBLIC_KEY_FIELDS.includes(f));
}

/** The name an encrypted output gets: orders.csv -> orders.csv.pgp (or .asc). */
export function encryptedName(path: string, armored: boolean): string {
  if (!path) return path;
  return /\.(pgp|gpg|asc)$/i.test(path) ? path : `${path}${armored ? '.asc' : '.pgp'}`;
}
