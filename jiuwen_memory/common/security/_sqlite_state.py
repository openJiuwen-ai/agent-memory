"""Private security SQLite state with constructor-local failure cleanup."""

import sqlite3
from pathlib import Path


def open_private_state(path: str, schema: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    try:
        connection.executescript(schema)
    except BaseException:
        connection.close()
        raise
    return connection
