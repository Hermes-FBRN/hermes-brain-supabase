#!/usr/bin/env python3
"""Apply approved Supabase/Mem0 brain lobe migration proposals.

Input: migration_output/nucleus_migration_dry_run_*.json from brain_lobe_dry_run.py.
Safety:
- soft-archives only, never deletes rows;
- removes deprecated workspace_id from migrated rows;
- skips rows with likely raw secrets, but migrates safe secret-pointer wording;
- writes public.hermes_memory_history when available;
- does not print memory contents.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data/.hermes"))
VENDOR = HERMES_HOME / "vendor" / "supabase_mem0"
if VENDOR.exists() and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

import psycopg
from psycopg.rows import dict_row

ENV = HERMES_HOME / ".env"
if ENV.exists():
    for raw in ENV.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DB_URL = os.environ["SUPABASE_BRAIN_DB_URL"]
SCHEMA = os.environ.get("SUPABASE_BRAIN_SCHEMA", "vecs")
COLLECTION = os.environ.get("SUPABASE_BRAIN_COLLECTION", "hermes_brain")
TABLE = f'{SCHEMA}."{COLLECTION}"'

RAW_SECRET_PATTERNS = [
    r"postgres(?:ql)?://[^\s]+",
    r"sk-[A-Za-z0-9_\-]{16,}",
    r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]{8,}",
    r"(?i)(bearer)\s+[A-Za-z0-9_\-.]{16,}",
]
SAFE_POINTER_PATTERNS = [
    r"(?i)env\s*var", r"(?i)environment", r"(?i)\.env", r"(?i)secret manager",
    r"(?i)credentials? (live|lives|stored|configured)", r"(?i)not injected", r"(?i)do not copy",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_archived(md: dict[str, Any]) -> bool:
    return bool(md.get("archived") or md.get("deleted") or md.get("archived_at"))


def memory_text(md: dict[str, Any]) -> str:
    return " ".join(str(md.get(k) or "") for k in ("data", "memory", "note", "provenance", "category", "project_id"))


def has_raw_secret(md: dict[str, Any]) -> bool:
    text = memory_text(md)
    if any(re.search(p, text) for p in RAW_SECRET_PATTERNS):
        # If it also contains only safe pointer language but no concrete assignment/URL/key, do not block.
        return True
    return False


def exec_history(cur: Any, memory_id: str, prev: dict[str, Any], new: dict[str, Any], actor: str) -> None:
    try:
        cur.execute(
            """
            insert into public.hermes_memory_history(memory_id, prev_metadata, new_metadata, actor)
            values (%s, %s::jsonb, %s::jsonb, %s)
            """,
            (memory_id, json.dumps(prev), json.dumps(new), actor),
        )
    except Exception:
        pass


def migrate_metadata(prev: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    new = dict(prev)
    new.pop("workspace_id", None)  # deprecated: lobe_id is canonical
    new.update(
        {
            "lobe_id": "nucleus",
            "user_id": "nucleus",  # legacy Mem0 tenant key only
            "visibility": "shared",
            "created_by_agent": "hermes-main",
            "agent_id": new.get("agent_id") or "hermes-main",
            "main_agent_id": "hermes-main",
            "owner_agent_id": "hermes-main",
            "updated_at": now_iso(),
            "migration": {
                "name": "nucleus_lobe_backfill",
                "applied_at": now_iso(),
                "source_user_id": prev.get("user_id"),
                "source_lobe_id": prev.get("lobe_id"),
                "workspace_id_removed": "workspace_id" in prev,
            },
        }
    )
    if proposal["decision"] == "migrate_to_nucleus_railway":
        new["project_id"] = "railway"
        new["category"] = "railway"
    return new


def archive_metadata(prev: dict[str, Any], reason: str) -> dict[str, Any]:
    new = dict(prev)
    new.update({"archived": True, "archived_at": now_iso(), "archive_reason": reason, "updated_at": now_iso()})
    return new


def latest_proposal(path_arg: str | None) -> Path:
    if path_arg:
        return Path(path_arg)
    paths = sorted((ROOT / "migration_output").glob("nucleus_migration_dry_run_*.json"))
    if not paths:
        raise SystemExit("No dry-run JSON found in migration_output/")
    return paths[-1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("proposal", nargs="?", help="Dry-run proposal JSON path; defaults to latest")
    ap.add_argument("--yes", action="store_true", help="Apply writes; without this, report what would happen")
    args = ap.parse_args()

    proposal_path = latest_proposal(args.proposal)
    proposal_doc = json.loads(proposal_path.read_text(encoding="utf-8"))
    proposals = {p["memory_id"]: p for p in proposal_doc.get("proposals", [])}
    ids = list(proposals)
    counts: Counter[str] = Counter()
    skipped: Counter[str] = Counter()

    with psycopg.connect(DB_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        for memory_id in ids:
            p = proposals[memory_id]
            cur.execute(f"select metadata from {TABLE} where id = %s for update", (memory_id,))
            row = cur.fetchone()
            if not row:
                skipped["missing_row"] += 1
                continue
            prev = dict(row["metadata"] or {})
            if is_archived(prev) and p["decision"] != "keep_archived":
                skipped["already_archived_changed_since_dryrun"] += 1
                continue

            decision = p["decision"]
            if decision.startswith("migrate"):
                if has_raw_secret(prev):
                    skipped["review_raw_secret_not_migrated"] += 1
                    continue
                new = migrate_metadata(prev, p)
                action = decision
            elif decision == "archive_test":
                new = archive_metadata(prev, "nucleus migration cleanup: test/probe memory")
                action = decision
            elif decision == "archive_low_value":
                new = archive_metadata(prev, "nucleus migration cleanup: low-value/temporary memory")
                action = decision
            elif decision == "keep_archived":
                skipped["keep_archived"] += 1
                continue
            else:
                skipped[f"unknown_decision:{decision}"] += 1
                continue

            counts[action] += 1
            if args.yes:
                exec_history(cur, memory_id, prev, new, "nucleus_lobe_migration")
                cur.execute(f"update {TABLE} set metadata = %s::jsonb where id = %s", (json.dumps(new), memory_id))
        if args.yes:
            conn.commit()
        else:
            conn.rollback()

    print(json.dumps({
        "proposal": str(proposal_path),
        "applied": bool(args.yes),
        "actions": dict(counts),
        "skipped": dict(skipped),
        "note": "No memory contents printed. Rows skipped as review_raw_secret_not_migrated require manual safe-pointer rewrite or approval.",
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
