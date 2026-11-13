"""Queue messages a run holds until it knows whether it succeeded.

Amazon SQS, Google Pub/Sub, RabbitMQ and MQTT hand a message to one reader and
drop it once the reader confirms it. A queue source therefore takes its
messages without confirming them and hands the run a ``HeldMessages``. The
executor calls ``commit()`` once the whole run, destinations included, has
succeeded: the messages are confirmed and leave the queue. For a run that
fails, is stopped, or only samples (Sample, sandbox, Test Node), the executor
calls ``release()``: the messages go back to the queue at once, or when the
queue's own hold time runs out if Flow can no longer reach it, and are
delivered again.

So a message can arrive twice after a failure, but none is lost. A run that
outlasts the queue's hold time (SQS visibility timeout, Pub/Sub acknowledgement
deadline) lets the queue deliver those messages to another reader as well; each
source sets that time from its Timeout and says so in its form.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class HeldMessages:
    """One step's unconfirmed messages and how to confirm or return them."""

    def __init__(
        self,
        label: str,
        step_id: str,
        count: int,
        confirm: Callable[[], None],
        give_back: Callable[[], None] | None = None,
        close: Callable[[], None] | None = None,
    ) -> None:
        self.label = label
        self.step_id = step_id
        self.count = count
        self._confirm = confirm
        self._give_back = give_back
        self._close = close
        self._done = False

    def commit(self) -> bool:
        """Confirm the messages: the whole run succeeded."""
        if self._done:
            return True
        self._done = True
        try:
            self._confirm()
            return True
        except Exception as exc:  # noqa: BLE001 — the run's data is already written
            logger.warning(
                "%s step %s: the run succeeded, but %d message(s) could not be confirmed, so "
                "the queue will deliver them again: %s", self.label, self.step_id, self.count, exc,
            )
            return False
        finally:
            self._shut()

    def release(self) -> None:
        """Give the messages back: the run failed, was stopped, or only sampled."""
        if self._done:
            return
        self._done = True
        try:
            if self._give_back is not None:
                self._give_back()
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "%s step %s: %d message(s) go back to the queue when its hold time ends (%s)",
                self.label, self.step_id, self.count, exc,
            )
        finally:
            self._shut()

    def _shut(self) -> None:
        if self._close is not None:
            try:
                self._close()
            except Exception:  # noqa: BLE001
                pass


def hold(ctx: Any, held: HeldMessages) -> None:
    """Hand the run ``held``; with no run to wait for (a context built by hand), give it back."""
    queue = getattr(ctx, "pending_sync_cursors", None)
    if queue is not None:
        queue.append(held)
    else:
        held.release()
