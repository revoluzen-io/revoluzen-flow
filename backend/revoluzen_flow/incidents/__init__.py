"""Incidents: one record per pipeline that has started failing.

A failed run already produces a run record, a bell notification and maybe
an alert. What none of those say is the thing an operator asks first:
*what changed?* An incident ties a run of failures together with the
cause class of the error, the step that failed and what feeds it, and the
versions saved since the pipeline last worked, and it stays open until a
run succeeds again.

* ``store.py``   — SQLite tables for incidents and their timeline.
* ``service.py`` — opened, repeated and resolved from every run outcome
  (``notifications/run_events.emit_run_notification`` calls it), plus the
  context shown when one is read.
* ``policy.py``  — fixes by policy: which failure classes may be resumed
  from their checkpoint without a person, how often, and after how long.
  Off until an admin turns it on.
"""
