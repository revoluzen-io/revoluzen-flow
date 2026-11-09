/**
 * Wording for the pipeline Bin, in one place so every screen says the same
 * thing about how long a deleted pipeline is kept.
 *
 * The number comes from the server (`retention_days` on GET /workflows/bin,
 * REVOLUZEN_FLOW_BIN_RETENTION_DAYS, default 30). 0 means nothing is removed
 * automatically: a deleted pipeline then stays until someone deletes it from
 * the Bin or empties the Bin.
 */

const DAY_MS = 24 * 60 * 60 * 1000;

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;

/** "30 days", "1 day". */
export const daysPhrase = (days: number): string => plural(days, 'day');

/** The Delete choice in the Remove Pipeline dialog. */
export function deleteOptionDescription(retentionDays: number, bulk: boolean): string {
  const what = bulk ? 'the pipelines' : 'the pipeline';
  const them = bulk ? 'them' : 'it';
  return retentionDays > 0
    ? `Move ${what} to the Bin. Removed for good after ${daysPhrase(retentionDays)}; restore ${them} from the Bin until then.`
    : `Move ${what} to the Bin. ${bulk ? 'They stay' : 'It stays'} there until you delete ${them} from the Bin or empty the Bin.`;
}

/** The confirm shown before the bulk Delete. */
export function bulkDeleteConfirm(count: number, retentionDays: number): {
  title: string;
  message: string;
  confirmLabel: string;
} {
  const one = count === 1;
  const subject = one ? 'The selected pipeline' : `All ${count} selected pipelines`;
  const them = one ? 'it' : 'them';
  return {
    title: `Move ${plural(count, 'pipeline')} to the Bin?`,
    message: retentionDays > 0
      ? `${subject} will be moved to the Bin and removed for good after ${daysPhrase(retentionDays)}. Restore ${them} from the Bin until then.`
      : `${subject} will be moved to the Bin. ${one ? 'It stays' : 'They stay'} there until you delete ${them} from the Bin or empty the Bin.`,
    confirmLabel: one ? 'Move to Bin' : `Move ${count} to Bin`,
  };
}

/** The one-line note above the Bin list. */
export function binBanner(retentionDays: number): string {
  return retentionDays > 0
    ? `Pipelines stay here for ${daysPhrase(retentionDays)}, then are removed for good.`
    : 'Pipelines stay here until you delete them from the Bin or empty it.';
}

/** The empty Bin. */
export function binEmptyBody(retentionDays: number): string {
  return retentionDays > 0
    ? `Deleted pipelines stay here for ${daysPhrase(retentionDays)}, so you can restore them.`
    : 'Deleted pipelines stay here until you delete them from the Bin or empty it.';
}

/** The "Removed" column: when a binned pipeline is removed for good. */
export function removalLabel(purgeAt: string | null | undefined, now: number = Date.now()): string {
  if (!purgeAt) return 'Not automatic';
  const when = new Date(purgeAt).getTime();
  if (Number.isNaN(when)) return 'Not automatic';
  const left = Math.ceil((when - now) / DAY_MS);
  if (left <= 0) return 'Due now';
  return `In ${daysPhrase(left)}`;
}

/** A pipeline's name, quoted for a message. */
export const quoted = (name: string): string => `"${name}"`;
