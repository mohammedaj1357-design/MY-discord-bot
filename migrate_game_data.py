"""Explicit, one-time import of the legacy game JSON into an EMPTY database.

Stop all bot instances first. Never overwrites an existing database state.
Usage: python3 python-starter/migrate_game_data.py --source /path/to/game_data.json
   or: python3 python-starter/migrate_game_data.py --fresh
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

import psycopg

from game_store import validate_state


def import_game_data(path, database_url, *, fresh=False):
    if fresh:
        if path is not None:
            raise ValueError("Cannot initialize a fresh game and import a file together")
        if (Path(__file__).parent / "data" / "game_data.json").exists():
            raise RuntimeError("Legacy game data exists; back it up and import it instead")
        state = validate_state({}, "new game")
        digest = None
    else:
        source = Path(path)
        raw = source.read_bytes()
        state = validate_state(json.loads(raw), source)
        digest = hashlib.sha256(raw).hexdigest()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required; no data was imported")
    # The unique row makes a second import or a racing bot mutation fail closed.
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO discord_game_state (id, state) VALUES (1, %s::jsonb) "
                "ON CONFLICT (id) DO NOTHING RETURNING id",
                (json.dumps(state, ensure_ascii=False),),
            )
            if cur.fetchone() is None:
                raise RuntimeError(
                    "Database already has game state; import refused. "
                    "Do not replace active player balances."
                )
    return digest, len(state["users"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", help="Legacy game_data.json or a full-state backup")
    group.add_argument("--fresh", action="store_true", help="Explicitly create a new empty game")
    args = parser.parse_args()
    digest, count = import_game_data(
        args.source, os.environ.get("DATABASE_URL"), fresh=args.fresh
    )
    if args.fresh:
        print("Initialized an empty game.")
    else:
        print(f"Imported {count} player records (source SHA-256: {digest}).")