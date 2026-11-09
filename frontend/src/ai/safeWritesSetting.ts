/**
 * One "let the assistant draft changes" switch, shared by both chat surfaces.
 *
 * There were two assistants with two different halves of what a repair
 * needs. The editor chat sends the rich canvas context — nodes, parameters,
 * validation state, the active node's error — but hard-coded
 * `allow_safe_writes: false`, so it could explain a fault and never draft
 * the fix. The global panel had the switch but a much thinner context: the
 * page key, the visible ids and the active node, with no canvas.
 *
 * `docs/repair-evaluation.md` measures the combination neither of them
 * shipped — rich context WITH safe writes on — so the scores described a
 * configuration no user could actually get.
 *
 * This is the switch itself, stored per browser so both surfaces agree and
 * the choice survives a reload. Nothing here grants anything: the server
 * still checks RBAC and the tier, every safe write is draft-only, and
 * applying a draft remains a separate high-impact call with its own
 * confirmation.
 */

const KEY = 'revoluzen_flow_allow_safe_writes';

/** Off unless the person turned it on. Reading must never throw. */
export function allowSafeWrites(): boolean {
  try {
    return localStorage.getItem(KEY) === '1';
  } catch {
    // Private window, blocked site data, or a preview with no storage.
    return false;
  }
}

export function setAllowSafeWrites(on: boolean): void {
  try {
    localStorage.setItem(KEY, on ? '1' : '0');
  } catch {
    // Nothing to do: the in-memory state still drives this session.
  }
}
