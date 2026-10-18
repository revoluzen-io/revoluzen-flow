"""Revoluzen Flow package entry point.

Enables ``python -m revoluzen_flow <command>`` so operators can invoke the CLI
without relying on the console_scripts shim being discoverable on PATH.
On Windows this is the canonical way to avoid "revoluzen_flow is not recognized"
when Python's Scripts directory was not added to PATH.

In particular this is the canonical way to run the dev-seed:

    python -m revoluzen_flow seed-admin

Stage 1 added this file so the seeded super_admin password reset can
happen WITHOUT booting the server — a precondition for moving the
write out of main.py module-import time.
"""

from __future__ import annotations

from revoluzen_flow.cli import main


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
