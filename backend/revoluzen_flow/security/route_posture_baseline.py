"""AUTO-GENERATED route-posture baseline (Phase 3 ratchet).

Mutating routes with no *detected* auth/workspace dependency. The test in
tests/test_route_posture.py fails if a NEW ungated mutating route appears
that is not in this set, forcing every new route to use a recognized auth
gate (or a conscious addition here with a reason).

Regenerated 2026-10-23 after an unauthenticated probe in
REVOLUZEN_FLOW_SECURITY_MODE=server found nine of these were not merely
undetected but genuinely reachable -- two of them (arbitrary file write via
POST /api/upload, arbitrary file read via the intelligence endpoints) were
exploitable. The routes fixed in that pass have been removed from the set.

Being in this list does NOT mean a route is unprotected. Most entries are
gated in the handler body, which the ratchet deliberately does not detect
-- a dependency is auditable, an in-body call is not, so new routes should
prefer the dependency. What the list means is "nobody has proved this one
either way since the freeze".
"""

KNOWN_UNGATED = frozenset({
    'DELETE /api/notifications/',
    'DELETE /api/notifications/{notification_id}',
    'POST /api/auth/check-password',
    'POST /api/auth/forgot-password',
    'POST /api/auth/login',
    'POST /api/auth/logout',
    'POST /api/auth/register',
    'POST /api/auth/request-access',
    'POST /api/auth/reset-password',
    'POST /api/notifications/read-all',
    'POST /api/notifications/{notification_id}/read',
    'POST /api/published/{path:path}',
    'POST /api/system/shutdown',
    'POST /api/v1/mcp',
    'POST /api/v1/mcp/',
    'PUT /api/published/{path:path}',
})
