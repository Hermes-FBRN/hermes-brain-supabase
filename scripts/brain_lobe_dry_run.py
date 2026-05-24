#!/usr/bin/env python3
"""Dry-run classifier for migrating existing Supabase/Mem0 brain rows to nucleus.

No database writes. Outputs JSON and CSV proposals under ./migration_output/.
"""
from __future__ import annotations

import csv
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
OUT_DIR = ROOT / "migration_output"
OUT_DIR.mkdir(exist_ok=True)

TEST_PATTERNS = [
    r"\btest\b", r"testing", r"dummy", r"hello world", r"brain test", r"shared durable brain test",
    r"can access the shared .* brain lobe", r"probe", r"smoke test",
]
LOW_VALUE_PATTERNS = [
    r"\bphase \d+\b", r"\bdone\b", r"completed", r"fixed bug", r"submitted pr", r"commit [0-9a-f]{7,40}",
    r"temporary", r"one[- ]off", r"raw transcript", r"session progress", r"todo", r"draft only",
]
RAILWAY_PATTERNS = [r"railway", r"railway.app", r"railway-", r"nixpacks"]
SECRET_HINTS = [r"api[_-]?key", r"token", r"password", r"secret", r"credential", r"postgresql://", r"sk-[A-Za-z0-9]"]


def is_archived(md: dict[str, Any]) -> bool:
    return bool(md.get("archived") or md.get("deleted") or md.get("archived_at"))


def text_for(md: dict[str, Any]) -> str:
    parts = [
        md.get("data"), md.get("memory"), md.get("category"), md.get("project_id"), md.get("scope"),
        md.get("visibility"), md.get("source"), md.get("lobe_id"), md.get("user_id"), md.get("workspace_id"),
    ]
    return " ".join(str(p or "") for p in parts).lower()


def has_any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, flags=re.I) for p in patterns)


def snippet(md: dict[str, Any]) -> str:
    s = str(md.get("data") or md.get("memory") or "")
    s = re.sub(r"\s+", " ", s).strip()
    # Avoid placing likely secret-ish long values into the review CSV.
    s = re.sub(r"(sk-|api[_-]?key\s*[:=]|token\s*[:=]|password\s*[:=]|postgresql://)[^\s,;]+", r"\1[REDACTED]", s, flags=re.I)
    return s[:180]


def classify(row: dict[str, Any]) -> dict[str, Any]:
    md = row.get("metadata") or {}
    t = text_for(md)
    importance = int(md.get("importance") or 0)
    archived = is_archived(md)
    existing_lobe = md.get("lobe_id") or md.get("workspace_id") or md.get("user_id")

    if archived:
        decision = "keep_archived"
        reason = "already archived; no migration proposed"
        target_project = ""
    elif has_any(TEST_PATTERNS, t):
        decision = "archive_test"
        reason = "looks like test/probe memory"
        target_project = ""
    elif has_any(LOW_VALUE_PATTERNS, t) or importance <= 1:
        decision = "archive_low_value"
        reason = "low-value/temporary/task-progress candidate"
        target_project = ""
    elif has_any(RAILWAY_PATTERNS, t):
        decision = "migrate_to_nucleus_railway"
        reason = "Railway-specific operational memory"
        target_project = "railway"
    else:
        decision = "migrate_to_nucleus"
        reason = "non-test active memory; preserve in nucleus per user policy"
        target_project = md.get("project_id") or ""

    risk_flags = []
    if has_any(SECRET_HINTS, t):
        risk_flags.append("review_secret_pointer")
    if not md.get("lobe_id"):
        risk_flags.append("missing_lobe_id")
    if md.get("workspace_id"):
        risk_flags.append("legacy_workspace_id_present")
    if existing_lobe not in {None, "", "nucleus", "462939789210157056"}:
        risk_flags.append(f"nonstandard_existing_lobe:{existing_lobe}")

    proposal = {
        "memory_id": str(row.get("id")),
        "decision": decision,
        "reason": reason,
        "risk": ";".join(risk_flags),
        "old_user_id": md.get("user_id") or "",
        "old_lobe_id": md.get("lobe_id") or "",
        "old_workspace_id": md.get("workspace_id") or "",
        "target_lobe_id": "nucleus" if decision.startswith("migrate") else "",
        "target_user_id": "nucleus" if decision.startswith("migrate") else "",
        "target_workspace_id": "",  # intentionally blank: workspace_id is deprecated
        "target_project_id": target_project,
        "target_category": "railway" if decision == "migrate_to_nucleus_railway" else (md.get("category") or ""),
        "target_visibility": "shared" if decision.startswith("migrate") else "",
        "target_created_by_agent": "hermes-main" if decision.startswith("migrate") else "",
        "importance": importance,
        "category": md.get("category") or "",
        "project_id": md.get("project_id") or "",
        "snippet": snippet(md),
    }
    return proposal


def main() -> None:
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(f"select id, metadata from {TABLE} order by coalesce(metadata->>'updated_at', metadata->>'created_at') desc nulls last")
        rows = cur.fetchall()

    proposals = [classify(r) for r in rows]
    counts = Counter(p["decision"] for p in proposals)
    risks = Counter(flag for p in proposals for flag in p["risk"].split(";") if flag)
    old_user_counts = Counter(p["old_user_id"] or "<missing>" for p in proposals)
    old_lobe_counts = Counter(p["old_lobe_id"] or "<missing>" for p in proposals)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = OUT_DIR / f"nucleus_migration_dry_run_{stamp}.json"
    csv_path = OUT_DIR / f"nucleus_migration_dry_run_{stamp}.csv"
    summary = {
        "generated_at": stamp,
        "table": TABLE,
        "total_rows": len(rows),
        "decision_counts": dict(counts),
        "risk_counts": dict(risks),
        "old_user_id_counts": dict(old_user_counts),
        "old_lobe_id_counts": dict(old_lobe_counts),
        "notes": [
            "No database writes were performed.",
            "workspace_id is intentionally not proposed for target metadata; lobe_id is canonical, user_id is kept only for Mem0 compatibility.",
            "Rows with review_secret_pointer require manual check before migration so raw secrets are not promoted into nucleus.",
        ],
        "proposals": proposals,
    }
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(proposals[0].keys()) if proposals else ["memory_id", "decision"])
        writer.writeheader()
        writer.writerows(proposals)

    print(json.dumps({
        "json": str(json_path),
        "csv": str(csv_path),
        "total_rows": len(rows),
        "decision_counts": dict(counts),
        "risk_counts": dict(risks),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
