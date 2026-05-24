#!/usr/bin/env python3
"""Safe administrative MCP server for the Supabase-backed Hermes brain.

Design goals:
- no raw SQL tool exposed to the model
- soft archive instead of hard delete
- metadata/version edits audited in public.hermes_memory_history when available
- semantic search/write through Mem0 when possible, direct Postgres for admin inspection
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Persistent vendored deps for Railway-style deployments.
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data/.hermes")).expanduser()
VENDOR = HERMES_HOME / "vendor" / "supabase_mem0"
if VENDOR.exists() and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from mcp.server.fastmcp import FastMCP
import psycopg
from psycopg.rows import dict_row

try:
    from mem0 import Memory
except Exception:  # pragma: no cover - health tool reports it
    Memory = None  # type: ignore

mcp = FastMCP("brain-manager")


def _load_env_file() -> None:
    """Load /data/.hermes/.env without printing or leaking values."""
    env_path = HERMES_HOME / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_env_file()

DB_URL = os.environ.get("SUPABASE_BRAIN_DB_URL")
COLLECTION = os.environ.get("SUPABASE_BRAIN_COLLECTION") or "hermes_brain"
BRAIN_SCHEMA = os.environ.get("SUPABASE_BRAIN_SCHEMA") or "vecs"
BRAIN_TABLE = f'{BRAIN_SCHEMA}."{COLLECTION}"'
DEFAULT_LOBE_ID = os.environ.get("SUPABASE_BRAIN_LOBE_ID") or os.environ.get("SUPABASE_BRAIN_WORKSPACE_ID") or os.environ.get("SUPABASE_BRAIN_USER_ID") or "nucleus"
DEFAULT_WORKSPACE_ID = DEFAULT_LOBE_ID
DEFAULT_USER_ID = DEFAULT_LOBE_ID  # legacy name used by Mem0/API filters; semantically lobe/workspace id
DEFAULT_AGENT_ID = os.environ.get("SUPABASE_BRAIN_AGENT_ID") or "hermes"
DEFAULT_ALLOWED_LOBES_RAW = os.environ.get("SUPABASE_BRAIN_ALLOWED_LOBES") or DEFAULT_LOBE_ID
DEFAULT_ALLOWED_LOBES = {x.strip() for x in DEFAULT_ALLOWED_LOBES_RAW.split(",") if x.strip()}
DEFAULT_SCOPE = os.environ.get("SUPABASE_BRAIN_DEFAULT_SCOPE") or "agent"
DEFAULT_VISIBILITY = os.environ.get("SUPABASE_BRAIN_DEFAULT_VISIBILITY") or "private"
ALLOWED_SCOPES = {"shared", "user", "agent", "project"}
ALLOWED_VISIBILITIES = {"shared", "private", "restricted"}
QUERY_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "what", "when", "where", "how",
    "memory", "memories", "brain", "hermes", "agent", "agents", "current", "retrieval", "search",
}
GOVERNANCE_TERMS = {
    "scope", "visibility", "owner_agent_id", "created_by_agent", "main_agent_id",
    "subagent_profile_id", "subject_agent_id", "project_id",
    "shared", "private", "peer", "peers", "governance", "registry", "policy", "durable",
}


def _choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lobe_allowed(lobe_id: str) -> bool:
    return "*" in DEFAULT_ALLOWED_LOBES or lobe_id in DEFAULT_ALLOWED_LOBES


def _deny_lobe(lobe_id: str) -> Dict[str, Any]:
    return {"ok": False, "error": "lobe_not_allowed", "lobe_id": lobe_id, "agent_id": DEFAULT_AGENT_ID, "allowed_lobes": sorted(DEFAULT_ALLOWED_LOBES)}


def _require_db_url() -> str:
    if not DB_URL:
        raise RuntimeError("SUPABASE_BRAIN_DB_URL is not configured")
    return DB_URL


def _connect():
    return psycopg.connect(_require_db_url(), row_factory=dict_row)


def _memory_engine():
    if Memory is None:
        raise RuntimeError("mem0 is not importable")
    config = {
        "vector_store": {
            "provider": "supabase",
            "config": {
                "connection_string": _require_db_url(),
                "collection_name": COLLECTION,
                "embedding_model_dims": 1536,
                "index_method": "hnsw",
                "index_measure": "cosine_distance",
            },
        }
    }
    return Memory.from_config(config)


def _sanitize_row(row: Dict[str, Any], include_vector: bool = False) -> Dict[str, Any]:
    md = row.get("metadata") or {}
    out = {
        "id": row.get("id"),
        "data": md.get("data"),
        "category": md.get("category"),
        "importance": md.get("importance"),
        "user_id": md.get("user_id"),
        "workspace_id": md.get("workspace_id") or md.get("user_id"),
        "lobe_id": md.get("lobe_id") or md.get("workspace_id") or md.get("user_id"),
        "agent_id": md.get("agent_id"),
        "created_by_agent": md.get("created_by_agent"),
        "owner_agent_id": md.get("owner_agent_id"),
        "main_agent_id": md.get("main_agent_id"),
        "subagent_profile_id": md.get("subagent_profile_id"),
        "subject_agent_id": md.get("subject_agent_id"),
        "scope": md.get("scope"),
        "visibility": md.get("visibility"),
        "project_id": md.get("project_id"),
        "source": md.get("source"),
        "created_at": md.get("created_at"),
        "updated_at": md.get("updated_at"),
        "archived": bool(md.get("archived") or md.get("deleted") or md.get("archived_at")),
        "metadata": md,
    }
    if include_vector and "vec" in row:
        out["vec"] = str(row["vec"])
    return out


def _build_memory_metadata(
    *,
    category: str,
    importance: int,
    user_id: str,
    agent_id: str,
    source: str,
    scope: Optional[str] = None,
    visibility: Optional[str] = None,
    project_id: Optional[str] = None,
    owner_agent_id: Optional[str] = None,
    main_agent_id: Optional[str] = None,
    subagent_profile_id: Optional[str] = None,
    subject_agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_scope = _choice(scope, ALLOWED_SCOPES, DEFAULT_SCOPE)
    default_visibility = "shared" if resolved_scope in {"shared", "user"} else DEFAULT_VISIBILITY
    resolved_visibility = _choice(visibility, ALLOWED_VISIBILITIES, default_visibility)
    now = _now_iso()
    md: Dict[str, Any] = {
        "category": category or "general",
        "importance": max(0, min(int(importance or 0), 10)),
        "source": source,
        "user_id": user_id,  # legacy Mem0 tenant key; semantically this is the Brain workspace/lobe id
        "workspace_id": user_id,
        "lobe_id": user_id,
        "agent_id": agent_id,
        "created_by_agent": agent_id,
        "owner_agent_id": _text(owner_agent_id) or _text(main_agent_id) or agent_id,
        "main_agent_id": _text(main_agent_id) or agent_id,
        "scope": resolved_scope,
        "visibility": resolved_visibility,
        "created_at": now,
        "updated_at": now,
    }
    pid = _text(project_id)
    if pid:
        md["project_id"] = pid
    subprofile = _text(subagent_profile_id)
    if subprofile:
        md["subagent_profile_id"] = subprofile
    subject = _text(subject_agent_id)
    if subject:
        md["subject_agent_id"] = subject
    return md


def _visible_to_agent(md: Dict[str, Any], agent_id: str) -> bool:
    if md.get("visibility") == "shared" or md.get("scope") in {"shared", "user"}:
        return True
    owner = md.get("owner_agent_id") or md.get("agent_id") or md.get("created_by_agent")
    return owner in {None, "", agent_id}


def _query_terms(query: str) -> List[str]:
    """Return stable terms for hybrid exact/semantic memory retrieval."""
    seen: set[str] = set()
    terms: List[str] = []
    for term in re.findall(r"[A-Za-z0-9_\-]{3,}", query.lower()):
        if term in QUERY_STOPWORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms[:14]


def _memory_text(item: Dict[str, Any]) -> str:
    md = item.get("metadata") or {}
    return " ".join(
        str(x or "")
        for x in (
            item.get("memory"), item.get("data"), md.get("data"), md.get("category"), md.get("scope"),
            md.get("visibility"), md.get("project_id"), md.get("owner_agent_id"), md.get("main_agent_id"),
            md.get("created_by_agent"), md.get("subagent_profile_id"), md.get("subject_agent_id"),
        )
    ).lower()


def _hybrid_rank(item: Dict[str, Any], query: str, terms: List[str]) -> float:
    """Deterministic reranker so governance anchors are not buried by generic semantic matches."""
    md = item.get("metadata") or {}
    text = _memory_text(item)
    overlap = sum(1 for t in terms if t in text)
    score = float(overlap * 12)
    q = query.lower().strip()
    if q and q in text:
        score += 40
    if any(t in text for t in GOVERNANCE_TERMS & set(terms)):
        score += 25
    if md.get("category") in {"governance", "agent_registry", "architecture"}:
        score += 20
    if md.get("visibility") == "shared":
        score += 8
    if md.get("scope") in {"shared", "user", "project"}:
        score += 6
    try:
        score += min(int(md.get("importance") or 0), 10)
    except Exception:
        pass
    try:
        # Mem0 scores can vary by backend; keep them as a tie-breaker only.
        score += max(0.0, min(float(item.get("score") or 0.0), 1.0))
    except Exception:
        pass
    return score


def _text_candidates(
    *,
    query: str,
    limit: int,
    user_id: str,
    agent_id: str,
    scope: Optional[str] = None,
    visibility: Optional[str] = None,
    project_id: Optional[str] = None,
    include_private: bool = False,
    include_archived: bool = False,
) -> List[Dict[str, Any]]:
    """Broad OR text candidate search used to complement Mem0 semantic search."""
    terms = _query_terms(query)
    if not terms:
        return []
    clauses = ["metadata->>'user_id' = %s"]
    params: List[Any] = [user_id]
    term_clauses = []
    for term in terms:
        term_clauses.append("(coalesce(metadata->>'data','') ilike %s or metadata::text ilike %s)")
        pattern = f"%{term}%"
        params.extend([pattern, pattern])
    clauses.append("(" + " or ".join(term_clauses) + ")")
    if scope:
        clauses.append("metadata->>'scope' = %s")
        params.append(scope)
    if visibility:
        clauses.append("metadata->>'visibility' = %s")
        params.append(visibility)
    if project_id:
        clauses.append("metadata->>'project_id' = %s")
        params.append(project_id)
    if not include_private:
        clauses.append("(metadata->>'visibility' = 'shared' or metadata->>'scope' in ('shared','user') or coalesce(metadata->>'owner_agent_id', metadata->>'agent_id', metadata->>'created_by_agent', %s) = %s)")
        params.extend([agent_id, agent_id])
    if not include_archived:
        clauses.append("not (coalesce((metadata->>'archived')::boolean, false) or metadata ? 'archived_at')")
    params.append(max(limit, 1))
    sql = f"""
        select id, metadata from {BRAIN_TABLE}
        where {' and '.join(clauses)}
        order by coalesce((metadata->>'importance')::int, 0) desc,
                 coalesce(metadata->>'updated_at', metadata->>'created_at') desc nulls last
        limit %s
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [_sanitize_row(r) for r in cur.fetchall()]


def _exec_history(cur, memory_id: str, prev_metadata: Dict[str, Any], new_metadata: Dict[str, Any], actor: str) -> None:
    try:
        cur.execute(
            """
            insert into public.hermes_memory_history(memory_id, prev_metadata, new_metadata, actor)
            values (%s, %s::jsonb, %s::jsonb, %s)
            """,
            (memory_id, json.dumps(prev_metadata), json.dumps(new_metadata), actor),
        )
    except Exception:
        # History table is nice-to-have; do not fail the admin update if absent/mismatched.
        pass


@mcp.tool()
def brain_health_check() -> Dict[str, Any]:
    """Check DB connectivity, Mem0 import status, collection existence, and memory counts."""
    result: Dict[str, Any] = {
        "ok": False,
        "collection": COLLECTION,
        "table": BRAIN_TABLE,
        "db_url_configured": bool(DB_URL),
        "mem0_importable": Memory is not None,
        "checked_at": _now_iso(),
    }
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("select to_regclass(%s) as reg", (f"{BRAIN_SCHEMA}.{COLLECTION}",))
            result["table_exists"] = bool(cur.fetchone()["reg"])
            if result["table_exists"]:
                cur.execute(f"select count(*) as total from {BRAIN_TABLE}")
                result["total_memories"] = cur.fetchone()["total"]
                cur.execute(f"select count(*) as archived from {BRAIN_TABLE} where coalesce((metadata->>'archived')::boolean, false) or metadata ? 'archived_at'")
                result["archived_memories"] = cur.fetchone()["archived"]
            result["ok"] = bool(result.get("table_exists"))
    except Exception as e:
        result["error"] = str(e)
    return result


@mcp.tool()
def brain_search(query: str, top_k: int = 5, lobe_id: Optional[str] = None, workspace_id: Optional[str] = None, user_id: Optional[str] = None, agent_id: Optional[str] = None, scope: Optional[str] = None, visibility: Optional[str] = None, project_id: Optional[str] = None, include_private: bool = False, include_archived: bool = False) -> Dict[str, Any]:
    """Hybrid search the brain: Mem0 semantic candidates + exact text/metadata anchors, reranked for governance terms."""
    top_k = max(1, min(int(top_k or 5), 20))
    user_id = lobe_id or workspace_id or user_id or DEFAULT_USER_ID
    agent_id = agent_id or DEFAULT_AGENT_ID
    if not _lobe_allowed(user_id):
        return _deny_lobe(user_id)
    filters = {"user_id": user_id}
    for key, value in (("scope", scope), ("visibility", visibility), ("project_id", project_id)):
        if value:
            filters[key] = value
    terms = _query_terms(query)
    semantic_error: Optional[str] = None
    candidates: Dict[str, Dict[str, Any]] = {}
    try:
        engine = _memory_engine()
        raw = engine.search(query=query, filters=filters, top_k=min(top_k * 6, 50))
        memories = raw.get("results", raw) if isinstance(raw, dict) else raw
        if isinstance(memories, list):
            for m in memories:
                md = m.get("metadata") or {}
                if not include_archived and (md.get("archived") or md.get("archived_at")):
                    continue
                if not include_private and not _visible_to_agent(md, agent_id):
                    continue
                mid = str(m.get("id") or md.get("id") or md.get("data") or len(candidates))
                candidates[mid] = {**m, "metadata": md, "source_modes": ["semantic"]}
    except Exception as e:
        semantic_error = str(e)

    try:
        for row in _text_candidates(
            query=query,
            limit=min(top_k * 8, 80),
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            visibility=visibility,
            project_id=project_id,
            include_private=include_private,
            include_archived=include_archived,
        ):
            mid = str(row.get("id"))
            existing = candidates.get(mid)
            if existing:
                existing.setdefault("source_modes", ["semantic"]).append("text")
                existing.setdefault("data", row.get("data"))
                existing.setdefault("metadata", row.get("metadata") or {})
            else:
                candidates[mid] = {
                    "id": row.get("id"),
                    "memory": row.get("data") or "",
                    "data": row.get("data"),
                    "metadata": row.get("metadata") or {},
                    "source_modes": ["text"],
                }
    except Exception as e:
        if semantic_error:
            return {"ok": False, "error": semantic_error, "text_error": str(e), "traceback": traceback.format_exc(limit=2)}

    ranked = list(candidates.values())
    for item in ranked:
        item["hybrid_score"] = _hybrid_rank(item, query, terms)
    ranked.sort(key=lambda item: item.get("hybrid_score", 0), reverse=True)
    results = ranked[:top_k]
    mode = "hybrid" if any("text" in item.get("source_modes", []) for item in results) else "semantic"
    response: Dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "query": query,
        "agent_id": agent_id,
        "filters": filters,
        "count": len(results),
        "results": results,
    }
    if semantic_error:
        response["semantic_error"] = semantic_error
    return response


@mcp.tool()
def brain_text_search(query: str, limit: int = 10, lobe_id: Optional[str] = None, workspace_id: Optional[str] = None, user_id: Optional[str] = None, agent_id: Optional[str] = None, category: Optional[str] = None, scope: Optional[str] = None, visibility: Optional[str] = None, project_id: Optional[str] = None, include_private: bool = False, include_archived: bool = False) -> Dict[str, Any]:
    """Admin text search over memory text/metadata; no embedding needed."""
    limit = max(1, min(int(limit or 10), 50))
    user_id = lobe_id or workspace_id or user_id
    agent_id = agent_id or DEFAULT_AGENT_ID
    if user_id and not _lobe_allowed(user_id):
        return _deny_lobe(user_id)
    pattern = f"%{query}%"
    clauses = ["coalesce(metadata->>'data','') ilike %s"]
    params: List[Any] = [pattern]
    if user_id:
        clauses.append("metadata->>'user_id' = %s")
        params.append(user_id)
    if category:
        clauses.append("metadata->>'category' = %s")
        params.append(category)
    if scope:
        clauses.append("metadata->>'scope' = %s")
        params.append(scope)
    if visibility:
        clauses.append("metadata->>'visibility' = %s")
        params.append(visibility)
    if project_id:
        clauses.append("metadata->>'project_id' = %s")
        params.append(project_id)
    if not include_private:
        clauses.append("(metadata->>'visibility' = 'shared' or metadata->>'scope' in ('shared','user') or coalesce(metadata->>'owner_agent_id', metadata->>'agent_id', metadata->>'created_by_agent', %s) = %s)")
        params.extend([agent_id, agent_id])
    if not include_archived:
        clauses.append("not (coalesce((metadata->>'archived')::boolean, false) or metadata ? 'archived_at')")
    params.append(limit)
    sql = f"select id, metadata from {BRAIN_TABLE} where {' and '.join(clauses)} order by coalesce(metadata->>'updated_at', metadata->>'created_at') desc nulls last limit %s"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = [_sanitize_row(r) for r in cur.fetchall()]
    return {"ok": True, "mode": "text", "query": query, "count": len(rows), "results": rows}


@mcp.tool()
def brain_profile(lobe_id: Optional[str] = None, workspace_id: Optional[str] = None, user_id: Optional[str] = None, agent_id: Optional[str] = None, limit: int = 20, include_archived: bool = False) -> Dict[str, Any]:
    """Return a broad recent/high-importance profile slice for a user."""
    user_id = lobe_id or workspace_id or user_id or DEFAULT_USER_ID
    agent_id = agent_id or DEFAULT_AGENT_ID
    if not _lobe_allowed(user_id):
        return _deny_lobe(user_id)
    limit = max(1, min(int(limit or 20), 50))
    clauses = ["metadata->>'user_id' = %s", "(metadata->>'visibility' = 'shared' or metadata->>'scope' in ('shared','user') or coalesce(metadata->>'owner_agent_id', metadata->>'agent_id', metadata->>'created_by_agent', %s) = %s)"]
    params: List[Any] = [user_id, agent_id, agent_id]
    if not include_archived:
        clauses.append("not (coalesce((metadata->>'archived')::boolean, false) or metadata ? 'archived_at')")
    params.append(limit)
    sql = f"""
        select id, metadata from {BRAIN_TABLE}
        where {' and '.join(clauses)}
        order by coalesce((metadata->>'importance')::int, 0) desc,
                 coalesce(metadata->>'updated_at', metadata->>'created_at') desc nulls last
        limit %s
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = [_sanitize_row(r) for r in cur.fetchall()]
    return {"ok": True, "user_id": user_id, "agent_id": agent_id, "count": len(rows), "results": rows}


@mcp.tool()
def brain_get_memory(memory_id: str) -> Dict[str, Any]:
    """Fetch one memory by ID with metadata. Vector is intentionally excluded."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"select id, metadata from {BRAIN_TABLE} where id = %s", (memory_id,))
        row = cur.fetchone()
    if not row:
        return {"ok": False, "error": "memory_not_found", "memory_id": memory_id}
    return {"ok": True, "memory": _sanitize_row(row)}


@mcp.tool()
def brain_remember(memory: str, category: str = "general", importance: int = 5, lobe_id: Optional[str] = None, workspace_id: Optional[str] = None, user_id: Optional[str] = None, agent_id: Optional[str] = None, scope: Optional[str] = None, visibility: Optional[str] = None, project_id: Optional[str] = None, owner_agent_id: Optional[str] = None, main_agent_id: Optional[str] = None, subagent_profile_id: Optional[str] = None, subject_agent_id: Optional[str] = None, created_by_user_id: Optional[str] = None, created_by_username: Optional[str] = None, created_by_platform: Optional[str] = None) -> Dict[str, Any]:
    """Store an explicit durable fact through Mem0. Use only for curated facts, not raw transcripts."""
    if not memory or len(memory.strip()) < 3:
        return {"ok": False, "error": "memory text is required"}
    importance = max(0, min(int(importance or 0), 10))
    user_id = lobe_id or workspace_id or user_id or DEFAULT_USER_ID
    agent_id = agent_id or DEFAULT_AGENT_ID
    if not _lobe_allowed(user_id):
        return _deny_lobe(user_id)
    metadata = _build_memory_metadata(
        category=category,
        importance=importance,
        user_id=user_id,
        agent_id=agent_id,
        source="mcp_brain_manager",
        scope=scope,
        visibility=visibility,
        project_id=project_id,
        owner_agent_id=owner_agent_id,
        main_agent_id=main_agent_id,
        subagent_profile_id=subagent_profile_id,
        subject_agent_id=subject_agent_id,
    )
    if created_by_user_id:
        metadata["created_by_user_id"] = created_by_user_id
    if created_by_username:
        metadata["created_by_username"] = created_by_username
    if created_by_platform:
        metadata["created_by_platform"] = created_by_platform
    engine = _memory_engine()
    raw = engine.add([{"role": "user", "content": memory}], user_id=user_id, metadata=metadata)
    return {"ok": True, "user_id": user_id, "agent_id": agent_id, "metadata": metadata, "result": raw}


@mcp.tool()
def brain_update_metadata(memory_id: str, category: Optional[str] = None, importance: Optional[int] = None, extra_metadata_json: Optional[str] = None, actor: str = "mcp_brain_manager") -> Dict[str, Any]:
    """Safely update selected metadata fields. Does not edit vector/content."""
    allowed_extra = {"source", "agent_id", "created_by_agent", "owner_agent_id", "main_agent_id", "subagent_profile_id", "subject_agent_id", "scope", "visibility", "project_id", "user_id", "workspace_id", "lobe_id", "created_by_user_id", "created_by_username", "created_by_platform", "note", "tags", "reviewed", "quality", "provenance"}
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"select metadata from {BRAIN_TABLE} where id = %s for update", (memory_id,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "memory_not_found", "memory_id": memory_id}
        prev = dict(row["metadata"] or {})
        new = dict(prev)
        if category is not None:
            new["category"] = category
        if importance is not None:
            new["importance"] = max(0, min(int(importance), 10))
        if extra_metadata_json:
            extra = json.loads(extra_metadata_json)
            rejected = sorted(set(extra) - allowed_extra)
            if rejected:
                return {"ok": False, "error": "extra metadata contains disallowed keys", "rejected_keys": rejected, "allowed_keys": sorted(allowed_extra)}
            new.update(extra)
        new["updated_at"] = _now_iso()
        _exec_history(cur, memory_id, prev, new, actor)
        cur.execute(f"update {BRAIN_TABLE} set metadata = %s::jsonb where id = %s", (json.dumps(new), memory_id))
        conn.commit()
    return {"ok": True, "memory_id": memory_id, "previous": prev, "updated": new}


@mcp.tool()
def brain_archive_memory(memory_id: str, reason: str = "archived via MCP brain-manager", actor: str = "mcp_brain_manager") -> Dict[str, Any]:
    """Soft-archive a memory. This never hard-deletes the row."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"select metadata from {BRAIN_TABLE} where id = %s for update", (memory_id,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "memory_not_found", "memory_id": memory_id}
        prev = dict(row["metadata"] or {})
        new = dict(prev)
        new.update({"archived": True, "archived_at": _now_iso(), "archive_reason": reason, "updated_at": _now_iso()})
        _exec_history(cur, memory_id, prev, new, actor)
        cur.execute(f"update {BRAIN_TABLE} set metadata = %s::jsonb where id = %s", (json.dumps(new), memory_id))
        conn.commit()
    return {"ok": True, "memory_id": memory_id, "archived": True, "reason": reason}


@mcp.tool()
def brain_quality_report(user_id: Optional[str] = None, include_archived: bool = False) -> Dict[str, Any]:
    """Return memory quality/admin metrics: counts, categories, missing fields, stale rows."""
    clauses: List[str] = []
    params: List[Any] = []
    if user_id:
        clauses.append("metadata->>'user_id' = %s")
        params.append(user_id)
    if not include_archived:
        clauses.append("not (coalesce((metadata->>'archived')::boolean, false) or metadata ? 'archived_at')")
    where = f"where {' and '.join(clauses)}" if clauses else ""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"select count(*) as total from {BRAIN_TABLE} {where}", params)
        total = cur.fetchone()["total"]
        cur.execute(f"select coalesce(metadata->>'category','uncategorized') as category, count(*) as count from {BRAIN_TABLE} {where} group by 1 order by count desc", params)
        categories = cur.fetchall()
        cur.execute(f"select count(*) as missing_importance from {BRAIN_TABLE} {where + (' and' if where else 'where')} not (metadata ? 'importance')", params)
        missing_importance = cur.fetchone()["missing_importance"]
        cur.execute(f"select count(*) as missing_user_id from {BRAIN_TABLE} {where + (' and' if where else 'where')} not (metadata ? 'user_id')", params)
        missing_user_id = cur.fetchone()["missing_user_id"]
        cur.execute(f"select count(*) as missing_agent_identity from {BRAIN_TABLE} {where + (' and' if where else 'where')} not (metadata ? 'agent_id') and not (metadata ? 'created_by_agent')", params)
        missing_agent_identity = cur.fetchone()["missing_agent_identity"]
        cur.execute(f"select coalesce(metadata->>'scope','legacy') as scope, count(*) as count from {BRAIN_TABLE} {where} group by 1 order by count desc", params)
        scopes = cur.fetchall()
        cur.execute(f"select id, metadata from {BRAIN_TABLE} {where} order by length(coalesce(metadata->>'data','')) asc limit 10", params)
        shortest = [_sanitize_row(r) for r in cur.fetchall()]
    return {
        "ok": True,
        "user_id": user_id,
        "total": total,
        "categories": categories,
        "missing_importance": missing_importance,
        "missing_user_id": missing_user_id,
        "missing_agent_identity": missing_agent_identity,
        "scopes": scopes,
        "shortest_memories_sample": shortest,
    }


@mcp.tool()
def brain_find_duplicates(user_id: Optional[str] = None, threshold: float = 0.35, limit: int = 20) -> Dict[str, Any]:
    """Find likely duplicate memories using pgvector cosine distance. Lower distance = more similar."""
    limit = max(1, min(int(limit or 20), 100))
    threshold = max(0.0, min(float(threshold), 2.0))
    clauses = ["a.id < b.id", "(a.vec <=> b.vec) <= %s"]
    params: List[Any] = [threshold]
    if user_id:
        clauses.append("a.metadata->>'user_id' = %s")
        clauses.append("b.metadata->>'user_id' = %s")
        params.extend([user_id, user_id])
    clauses.extend([
        "not (coalesce((a.metadata->>'archived')::boolean, false) or a.metadata ? 'archived_at')",
        "not (coalesce((b.metadata->>'archived')::boolean, false) or b.metadata ? 'archived_at')",
    ])
    params.append(limit)
    sql = f"""
        select a.id as id_a, b.id as id_b, (a.vec <=> b.vec) as distance,
               a.metadata as metadata_a, b.metadata as metadata_b
        from {BRAIN_TABLE} a
        join {BRAIN_TABLE} b on {' and '.join(clauses)}
        order by distance asc
        limit %s
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        pairs = []
        for r in cur.fetchall():
            pairs.append({
                "distance": float(r["distance"]),
                "a": _sanitize_row({"id": r["id_a"], "metadata": r["metadata_a"]}),
                "b": _sanitize_row({"id": r["id_b"], "metadata": r["metadata_b"]}),
            })
    return {"ok": True, "threshold": threshold, "count": len(pairs), "pairs": pairs}


if __name__ == "__main__":
    mcp.run()
