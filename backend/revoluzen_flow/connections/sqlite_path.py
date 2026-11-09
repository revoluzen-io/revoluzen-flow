"""Where a SQLite connection's database file is.

The connection form saves the file under ``file_path``; the API and older
configs use ``database``, ``file`` or ``path``. Every reader goes through here
so they can't drift apart again.
"""

from __future__ import annotations

import os
import sys
from typing import Any

_PATH_KEYS = ("database", "file_path", "file", "path")


def _data_dir() -> str | None:
    # Only ask the running app: importing main from a node would load all of it.
    main = sys.modules.get("revoluzen_flow.main")
    found = getattr(main, "app_state", {}).get("data_dir") if main else None
    return str(found) if found else os.environ.get("REVOLUZEN_FLOW_DATA_DIR")


def sqlite_db_path(config: dict[str, Any]) -> str | None:
    """The database file a SQLite connection points at, or None if it names none.

    A relative path means relative to the data folder: the server's working
    directory is not something a user can see or choose. One that only exists
    under the working directory still opens, as it always did.
    """
    path = next((str(config[key]).strip() for key in _PATH_KEYS if config.get(key)), None)
    if not path or path == ":memory:" or os.path.isabs(path):
        return path
    data_dir = _data_dir()
    if not data_dir:
        return path
    in_data_dir = os.path.join(data_dir, path)
    if os.path.exists(in_data_dir) or not os.path.exists(path):
        return in_data_dir
    return path
