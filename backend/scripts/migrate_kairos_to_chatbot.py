"""
One-off data migration: rename Kairos tables to `chatbot_*` prefix.

Background
==========
Before consolidation, the Kairos chatbot tables were named generically
(`conversations`, `chats`, `new_prompts`, `email_rate_limits`) and lived
in a separate Supabase project. Post-consolidation, they live in the
single Temveris database with `chatbot_` prefixed names matching the
new SQLAlchemy models in `app/kairos/models.py`.

What this script does
=====================
For each old → new pair:
  1. Skip if the new table already has rows (idempotent — already migrated).
  2. Copy rows from the old table to the new one preserving columns.
  3. Drop the old table once the copy is verified.

Safe to run:
  - Multiple times (idempotent — skips already-migrated tables).
  - Against a DB where the old tables don't exist (no-op for that table).
  - Against a DB where the new tables don't exist yet (creates them via
    SQLAlchemy ensure_tables before copying).

How to run
==========
1. Ensure `backend/.env` `DATABASE_URL` points at the unified Temveris
   Supabase project via a Supavisor pooler URL.
2. Run:
     cd backend
     python scripts/migrate_kairos_to_chatbot.py
3. Verify the row counts it prints, then delete this file (or keep it as
   a historical record — it's idempotent so leaving it in place is fine).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running this from the backend/ directory directly.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import inspect, text  # noqa: E402

from database.database import engine  # noqa: E402
from app.kairos import ensure_tables  # noqa: E402


RENAMES = [
    # (legacy_name, new_name, columns_in_common)
    ("conversations", "chatbot_conversations", [
        "id", "session_id", "user_email", "user_name", "company",
        "employee_id", "title", "preview", "message_count", "status",
        "started_at", "last_message_at", "expires_at", "created_at",
        "updated_at",
    ]),
    ("chats", "chatbot_messages", [
        "id", "session_id", "user_email", "role", "content", "metadata",
        "expires_at", "created_at", "updated_at",
    ]),
    ("new_prompts", "chatbot_unknown_prompts", [
        "prompt", "count",
    ]),
    ("email_rate_limits", "chatbot_email_rate_limits", [
        "key", "points", "expire", "created_at", "updated_at",
    ]),
]


def _count(conn, table: str) -> int:
    try:
        return conn.execute(text(f'select count(*) from "{table}"')).scalar() or 0
    except Exception:
        return -1  # signals "table missing"


def main() -> int:
    print("=== Kairos -> chatbot_* migration ===")
    insp = inspect(engine)
    existing = set(insp.get_table_names())

    # 1. Make sure the new chatbot_* tables exist (idempotent).
    print("Ensuring chatbot_* tables exist...")
    ensure_tables()
    insp = inspect(engine)  # refresh after create
    existing = set(insp.get_table_names())

    migrated_total = 0

    for old, new, cols in RENAMES:
        print(f"\n--- {old} -> {new} ---")
        if old not in existing:
            print(f"  [skip] legacy table `{old}` not present")
            continue
        if new not in existing:
            print(f"  [skip] new table `{new}` missing (ensure_tables failed?)")
            continue

        with engine.begin() as conn:
            new_count = _count(conn, new)
            old_count = _count(conn, old)
            print(f"  legacy rows={old_count}, new rows={new_count}")

            if new_count > 0:
                print(f"  [skip] `{new}` already populated; not overwriting")
                continue
            if old_count == 0:
                print(f"  [skip] `{old}` is empty; nothing to copy")
                continue

            col_list = ", ".join(f'"{c}"' for c in cols)
            # ON CONFLICT DO NOTHING in case the new table has a PK that
            # already exists from a half-completed prior run.
            conn.execute(text(
                f'INSERT INTO "{new}" ({col_list}) '
                f'SELECT {col_list} FROM "{old}" '
                f'ON CONFLICT DO NOTHING'
            ))
            copied = _count(conn, new)
            migrated_total += copied
            print(f"  copied {copied} row(s)")

    # 2. Drop the legacy tables ONLY after successful copy.
    print("\n--- Dropping legacy tables ---")
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    for old, new, _ in RENAMES:
        if old not in existing:
            continue
        with engine.begin() as conn:
            new_count = _count(conn, new)
            old_count = _count(conn, old)
            if new_count >= old_count > 0 or old_count == 0:
                conn.execute(text(f'DROP TABLE IF EXISTS "{old}" CASCADE'))
                print(f"  dropped `{old}`")
            else:
                print(
                    f"  [WARN] `{old}` not dropped — new table has fewer rows "
                    f"({new_count} < {old_count}); investigate before re-running"
                )

    print(f"\nDone. Total rows migrated: {migrated_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
