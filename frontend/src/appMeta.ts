/**
 * Front-end product constants.
 *
 * `/api/app/info` is the authoritative version — it serves
 * `revoluzen_flow.app_meta.VERSION`, which `pyproject.toml` and the release
 * tag are checked against. Components that can await a request should prefer
 * it (see `components/help/HelpFeedback.tsx`).
 *
 * This constant exists for the surfaces that render before any request can
 * resolve — the login footer, the sidebar chip — and as the fallback when the
 * call fails. It was previously copy-pasted into five components, which drifted
 * to 1.0.0 while the backend served 1.0.1, so the login screen told users the
 * wrong version. Keep it in one place and bump it with the backend.
 */
export const APP_VERSION = '1.0.1';
