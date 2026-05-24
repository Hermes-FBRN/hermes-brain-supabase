"""Supabase Mem0 memory provider for Hermes.

Uses Mem0 OSS (``Memory.from_config``) with the Supabase/pgvector vector store.
Installed as a user memory plugin under ``$HERMES_HOME/plugins/supabase_mem0``
so the plugin source survives Railway redeploys when HERMES_HOME is on /data.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_ALLOWED_SCOPES = {"shared", "user", "agent", "project"}
_ALLOWED_VISIBILITIES = {"shared", "private", "restricted"}
_QUERY_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "what", "when", "where", "how",
    "memory", "memories", "brain", "hermes", "agent", "agents", "current", "retrieval", "search",
}
_GOVERNANCE_TERMS = {
    "scope", "visibility", "owner_agent_id", "created_by_agent", "main_agent_id",
    "subagent_profile_id", "subject_agent_id", "project_id",
    "shared", "private", "peer", "peers", "governance", "registry", "policy", "durable",
}


def _clean_choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _clean_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None

_BRAIN_SEARCH_SCHEMA = {
    "name": "brain_search",
    "description": "Search the Supabase-backed brain by semantic meaning. Use for user preferences, project facts, decisions, and durable knowledge.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Maximum results, default 5, max 20."},
            "category": {"type": "string", "description": "Optional category metadata filter."},
            "scope": {"type": "string", "description": "Optional scope filter: shared, user, agent, or project."},
            "visibility": {"type": "string", "description": "Optional visibility filter: shared, private, or restricted."},
            "project_id": {"type": "string", "description": "Optional project identifier filter."},
            "include_private": {"type": "boolean", "description": "If true, include private memories owned by other agents. Defaults to false."},
        },
        "required": ["query"],
    },
}

_BRAIN_REMEMBER_SCHEMA = {
    "name": "brain_remember",
    "description": "Store an explicit durable fact in the Supabase-backed brain. Use only for stable facts/preferences/corrections/decisions, not temporary task progress.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory": {"type": "string", "description": "The durable fact to store."},
            "category": {"type": "string", "description": "Optional category, e.g. preference, project, infra, workflow."},
            "importance": {"type": "integer", "description": "Optional importance 1-10."},
            "scope": {"type": "string", "description": "Memory scope: shared, user, agent, or project. Defaults to agent."},
            "visibility": {"type": "string", "description": "Memory visibility: shared, private, or restricted. Defaults to private unless scope=shared."},
            "project_id": {"type": "string", "description": "Optional project identifier for project-scoped memories."},
            "owner_agent_id": {"type": "string", "description": "Optional owner main-agent id. Defaults to this agent."},
            "main_agent_id": {"type": "string", "description": "Optional independent top-level agent id. Defaults to this agent."},
            "subagent_profile_id": {"type": "string", "description": "Optional subagent/profile id under the main agent, e.g. claude-code-agent or codex-agent."},
            "subject_agent_id": {"type": "string", "description": "Optional id of the agent/profile the memory is about."},
        },
        "required": ["memory"],
    },
}

_BRAIN_PROFILE_SCHEMA = {
    "name": "brain_profile",
    "description": "Return a broader set of memories for the active user from the Supabase-backed brain.",
    "parameters": {
        "type": "object",
        "properties": {
            "top_k": {"type": "integer", "description": "Maximum memories, default 20, max 50."},
        },
        "required": [],
    },
}


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _load_env_file() -> None:
    """Best-effort .env loader for subprocess tests and plugin setup.

    Hermes normally loads .env for the gateway/CLI. This keeps direct provider
    tests working too. Values already in os.environ win.
    """
    env_path = _hermes_home() / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _load_json_config() -> Dict[str, Any]:
    path = _hermes_home() / "supabase_mem0.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _add_vendor_to_path() -> None:
    """Make persistent vendored deps available after Railway redeploys.

    We append, not prepend, so image/global Hermes packages remain preferred.
    If the redeployed image lacks mem0/vecs, Python can still import the copy
    installed in /data.
    """
    vendor = _hermes_home() / "vendor" / "supabase_mem0"
    if vendor.is_dir():
        s = str(vendor)
        if s not in sys.path:
            sys.path.append(s)


class SupabaseMem0MemoryProvider(MemoryProvider):
    """Mem0 OSS + Supabase pgvector provider."""

    def __init__(self) -> None:
        self._memory = None
        self._client_lock = threading.Lock()
        self._prefetch_thread = None
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._sync_thread = None
        self._db_url = ""
        self._collection = "hermes_brain"
        self._user_id = "hermes-user"
        self._agent_id = "hermes"
        self._auto_sync = False
        self._default_scope = "agent"
        self._default_visibility = "private"
        self._initialized = False

    @property
    def name(self) -> str:
        return "supabase_mem0"

    def is_available(self) -> bool:
        _load_env_file()
        _add_vendor_to_path()
        db_url = os.environ.get("SUPABASE_BRAIN_DB_URL", "")
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        if not (db_url and openai_key):
            return False
        try:
            import mem0  # noqa: F401
            import vecs  # noqa: F401
            return True
        except Exception:
            return False

    def get_config_schema(self):
        return [
            {"key": "db_url", "description": "Supabase Postgres connection string", "secret": True, "required": True, "env_var": "SUPABASE_BRAIN_DB_URL"},
            {"key": "openai_api_key", "description": "OpenAI key used by Mem0 for extraction/embeddings", "secret": True, "required": True, "env_var": "OPENAI_API_KEY"},
            {"key": "collection", "description": "Supabase/vecs collection name", "default": "hermes_brain"},
            {"key": "user_id", "description": "Tenant/user scope written to metadata", "default": "hermes-user", "env_var": "SUPABASE_BRAIN_USER_ID"},
            {"key": "agent_id", "description": "Stable ID of this Hermes agent", "default": "hermes", "env_var": "SUPABASE_BRAIN_AGENT_ID"},
            {"key": "default_scope", "description": "Default scope for brain_remember", "default": "agent", "choices": ["agent", "shared", "user", "project"]},
            {"key": "default_visibility", "description": "Default visibility for brain_remember", "default": "private", "choices": ["private", "shared", "restricted"]},
            {"key": "auto_sync", "description": "Automatically sync every completed turn", "default": "false", "choices": ["true", "false"]},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        path = Path(hermes_home) / "supabase_mem0.json"
        existing: Dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing.update(values)
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    def initialize(self, session_id: str, **kwargs) -> None:
        _load_env_file()
        _add_vendor_to_path()
        cfg = _load_json_config()
        self._db_url = os.environ.get("SUPABASE_BRAIN_DB_URL", "")
        self._collection = os.environ.get("SUPABASE_BRAIN_COLLECTION") or cfg.get("collection") or "hermes_brain"
        self._user_id = kwargs.get("user_id") or os.environ.get("SUPABASE_BRAIN_USER_ID") or cfg.get("user_id") or "hermes-user"
        self._agent_id = os.environ.get("SUPABASE_BRAIN_AGENT_ID") or cfg.get("agent_id") or "hermes"
        self._default_scope = _clean_choice(os.environ.get("SUPABASE_BRAIN_DEFAULT_SCOPE") or cfg.get("default_scope"), _ALLOWED_SCOPES, "agent")
        self._default_visibility = _clean_choice(os.environ.get("SUPABASE_BRAIN_DEFAULT_VISIBILITY") or cfg.get("default_visibility"), _ALLOWED_VISIBILITIES, "private")
        raw_auto = os.environ.get("SUPABASE_BRAIN_AUTO_SYNC", str(cfg.get("auto_sync", "false")))
        self._auto_sync = str(raw_auto).strip().lower() in {"1", "true", "yes", "on"}
        self._initialized = True
        # Lazy client creation; avoids DB/LLM setup during prompt construction.

    def _get_memory(self):
        with self._client_lock:
            if self._memory is not None:
                return self._memory
            if not self._db_url:
                raise RuntimeError("SUPABASE_BRAIN_DB_URL is not configured")
            try:
                from mem0 import Memory
            except ImportError as e:
                raise RuntimeError(
                    "mem0ai/vecs not installed. Expected global install or persistent vendor at "
                    f"{_hermes_home() / 'vendor' / 'supabase_mem0'}"
                ) from e
            config = {
                "vector_store": {
                    "provider": "supabase",
                    "config": {
                        "connection_string": self._db_url,
                        "collection_name": self._collection,
                        "embedding_model_dims": 1536,
                        "index_method": "hnsw",
                        "index_measure": "cosine_distance",
                    },
                }
            }
            self._memory = Memory.from_config(config)
            return self._memory

    def _filters(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        filters: Dict[str, Any] = {"user_id": self._user_id}
        if extra:
            filters.update({k: v for k, v in extra.items() if v is not None and v != ""})
        return filters

    def _metadata(
        self,
        *,
        source: str,
        category: str = "memory",
        importance: Any = None,
        scope: Any = None,
        visibility: Any = None,
        project_id: Any = None,
        owner_agent_id: Any = None,
        main_agent_id: Any = None,
        subagent_profile_id: Any = None,
        subject_agent_id: Any = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        resolved_scope = _clean_choice(scope, _ALLOWED_SCOPES, self._default_scope)
        default_visibility = "shared" if resolved_scope in {"shared", "user"} else self._default_visibility
        resolved_visibility = _clean_choice(visibility, _ALLOWED_VISIBILITIES, default_visibility)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        md: Dict[str, Any] = {
            "source": source,
            "category": category or "memory",
            "importance": importance,
            "user_id": self._user_id,
            "agent_id": self._agent_id,
            "created_by_agent": self._agent_id,
            "owner_agent_id": _clean_text(owner_agent_id) or _clean_text(main_agent_id) or self._agent_id,
            "main_agent_id": _clean_text(main_agent_id) or self._agent_id,
            "scope": resolved_scope,
            "visibility": resolved_visibility,
            "created_at": now_iso,
            "updated_at": now_iso,
            "stored_at": int(time.time()),
        }
        pid = _clean_text(project_id)
        if pid:
            md["project_id"] = pid
        subprofile = _clean_text(subagent_profile_id)
        if subprofile:
            md["subagent_profile_id"] = subprofile
        subject = _clean_text(subject_agent_id)
        if subject:
            md["subject_agent_id"] = subject
        md.update({k: v for k, v in extra.items() if v is not None and v != ""})
        return md

    def _visible_to_this_agent(self, metadata: Dict[str, Any]) -> bool:
        if metadata.get("visibility") == "shared" or metadata.get("scope") in {"shared", "user"}:
            return True
        owner = metadata.get("owner_agent_id") or metadata.get("agent_id") or metadata.get("created_by_agent")
        return owner in {None, "", self._agent_id}

    @staticmethod
    def _query_terms(query: str) -> List[str]:
        seen: set[str] = set()
        terms: List[str] = []
        for term in re.findall(r"[A-Za-z0-9_\-]{3,}", query.lower()):
            if term in _QUERY_STOPWORDS or term in seen:
                continue
            seen.add(term)
            terms.append(term)
        return terms[:14]

    @staticmethod
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

    def _hybrid_rank(self, item: Dict[str, Any], query: str, terms: List[str]) -> float:
        md = item.get("metadata") or {}
        text = self._memory_text(item)
        overlap = sum(1 for t in terms if t in text)
        score = float(overlap * 12)
        q = query.lower().strip()
        if q and q in text:
            score += 40
        if any(t in text for t in _GOVERNANCE_TERMS & set(terms)):
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
            score += max(0.0, min(float(item.get("score") or 0.0), 1.0))
        except Exception:
            pass
        return score

    def _text_candidates(self, query: str, *, extra: Dict[str, Any], limit: int, include_private: bool) -> List[Dict[str, Any]]:
        terms = self._query_terms(query)
        if not terms or not self._db_url:
            return []
        try:
            import psycopg
            from psycopg.rows import dict_row
        except Exception as e:
            logger.debug("supabase_mem0 text candidate search unavailable: %s", e)
            return []
        schema = os.environ.get("SUPABASE_BRAIN_SCHEMA") or "vecs"
        table = f'{schema}."{self._collection}"'
        clauses = ["metadata->>'user_id' = %s"]
        params: List[Any] = [self._user_id]
        term_clauses = []
        for term in terms:
            term_clauses.append("(coalesce(metadata->>'data','') ilike %s or metadata::text ilike %s)")
            pattern = f"%{term}%"
            params.extend([pattern, pattern])
        clauses.append("(" + " or ".join(term_clauses) + ")")
        for key in ("category", "scope", "visibility", "project_id"):
            if extra.get(key):
                clauses.append(f"metadata->>'{key}' = %s")
                params.append(extra[key])
        if not include_private:
            clauses.append("(metadata->>'visibility' = 'shared' or metadata->>'scope' in ('shared','user') or coalesce(metadata->>'owner_agent_id', metadata->>'agent_id', metadata->>'created_by_agent', %s) = %s)")
            params.extend([self._agent_id, self._agent_id])
        clauses.append("not (coalesce((metadata->>'archived')::boolean, false) or metadata ? 'archived_at')")
        params.append(max(limit, 1))
        sql = f"""
            select id, metadata from {table}
            where {' and '.join(clauses)}
            order by coalesce((metadata->>'importance')::int, 0) desc,
                     coalesce(metadata->>'updated_at', metadata->>'created_at') desc nulls last
            limit %s
        """
        try:
            with psycopg.connect(self._db_url, row_factory=dict_row) as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        except Exception as e:
            logger.debug("supabase_mem0 text candidate search failed: %s", e)
            return []
        out = []
        for row in rows:
            md = row.get("metadata") or {}
            out.append({"id": row.get("id"), "memory": md.get("data") or "", "data": md.get("data"), "metadata": md, "source_modes": ["text"]})
        return out

    @staticmethod
    def _results(response: Any) -> List[Dict[str, Any]]:
        if isinstance(response, dict):
            r = response.get("results", [])
            return r if isinstance(r, list) else []
        if isinstance(response, list):
            return response
        return []

    def system_prompt_block(self) -> str:
        return (
            "# Supabase Brain Memory\n"
            f"Active provider: supabase_mem0. User scope: {self._user_id}. Agent: {self._agent_id}. Collection: {self._collection}.\n"
            "Use brain_search for recall and brain_remember only for explicit durable facts. "
            "Do not store temporary progress or secrets."
        )

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        def _run() -> None:
            try:
                mem = self._get_memory()
                response = mem.search(query=query, filters=self._filters(), top_k=5)
                results = self._results(response)
                lines = []
                for r in results:
                    text = r.get("memory") or r.get("text") or ""
                    if text:
                        lines.append(f"- {text}")
                with self._prefetch_lock:
                    self._prefetch_result = "\n".join(lines)
            except Exception as e:
                logger.debug("supabase_mem0 prefetch failed: %s", e)

        if self._prefetch_thread and self._prefetch_thread.is_alive():
            return
        self._prefetch_thread = threading.Thread(target=_run, daemon=True, name="supabase-mem0-prefetch")
        self._prefetch_thread.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=2.5)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        return f"## Supabase Brain Recall\n{result}" if result else ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if not self._auto_sync:
            return

        def _sync() -> None:
            try:
                mem = self._get_memory()
                mem.add(
                    [
                        {"role": "user", "content": user_content},
                        {"role": "assistant", "content": assistant_content},
                    ],
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    metadata=self._metadata(source="hermes_auto_sync", category="auto_sync", scope="agent", visibility="private", session_id=session_id),
                )
            except Exception as e:
                logger.warning("supabase_mem0 sync failed: %s", e)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=3)
        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="supabase-mem0-sync")
        self._sync_thread.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [_BRAIN_SEARCH_SCHEMA, _BRAIN_REMEMBER_SCHEMA, _BRAIN_PROFILE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            mem = self._get_memory()
        except Exception as e:
            return tool_error(str(e))

        if tool_name == "brain_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return tool_error("Missing required parameter: query")
            top_k = max(1, min(int(args.get("top_k") or 5), 20))
            extra = {k: args.get(k) for k in ("category", "scope", "visibility", "project_id") if args.get(k)}
            include_private = bool(args.get("include_private"))
            terms = self._query_terms(query)
            candidates: Dict[str, Dict[str, Any]] = {}
            semantic_error: Optional[str] = None
            try:
                response = mem.search(query=query, filters=self._filters(extra), top_k=min(top_k * 6, 50))
                for r in self._results(response):
                    metadata = r.get("metadata") or {}
                    if not include_private and not self._visible_to_this_agent(metadata):
                        continue
                    mid = str(r.get("id") or metadata.get("id") or metadata.get("data") or len(candidates))
                    candidates[mid] = {
                        "memory": r.get("memory") or r.get("text") or "",
                        "score": r.get("score"),
                        "metadata": metadata,
                        "source_modes": ["semantic"],
                    }
            except Exception as e:
                semantic_error = str(e)

            for row in self._text_candidates(query, extra=extra, limit=min(top_k * 8, 80), include_private=include_private):
                mid = str(row.get("id"))
                existing = candidates.get(mid)
                if existing:
                    existing.setdefault("source_modes", ["semantic"]).append("text")
                    existing.setdefault("data", row.get("data"))
                else:
                    candidates[mid] = row

            if not candidates and semantic_error:
                return tool_error(f"brain_search failed: {semantic_error}")
            items = list(candidates.values())
            for item in items:
                item["hybrid_score"] = self._hybrid_rank(item, query, terms)
            items.sort(key=lambda item: item.get("hybrid_score", 0), reverse=True)
            items = items[:top_k]
            return json.dumps({
                "results": items,
                "count": len(items),
                "agent_id": self._agent_id,
                "mode": "hybrid" if any("text" in item.get("source_modes", []) for item in items) else "semantic",
                **({"semantic_error": semantic_error} if semantic_error else {}),
            })

        if tool_name == "brain_remember":
            text = str(args.get("memory") or "").strip()
            if not text:
                return tool_error("Missing required parameter: memory")
            metadata = self._metadata(
                source="hermes_brain_remember",
                category=args.get("category") or "memory",
                importance=args.get("importance"),
                scope=args.get("scope"),
                visibility=args.get("visibility"),
                project_id=args.get("project_id"),
                owner_agent_id=args.get("owner_agent_id"),
                main_agent_id=args.get("main_agent_id"),
                subagent_profile_id=args.get("subagent_profile_id"),
                subject_agent_id=args.get("subject_agent_id"),
            )
            try:
                mem.add(
                    [{"role": "user", "content": text}],
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    metadata=metadata,
                    infer=False,
                )
                return json.dumps({"result": "stored", "category": metadata["category"], "metadata": metadata})
            except TypeError:
                # Older/local Mem0 builds may not accept infer=False.
                try:
                    mem.add(
                        [{"role": "user", "content": text}],
                        user_id=self._user_id,
                        agent_id=self._agent_id,
                        metadata=metadata,
                    )
                    return json.dumps({"result": "stored", "category": metadata["category"], "metadata": metadata})
                except Exception as e:
                    return tool_error(f"brain_remember failed: {e}")
            except Exception as e:
                return tool_error(f"brain_remember failed: {e}")

        if tool_name == "brain_profile":
            top_k = max(1, min(int(args.get("top_k") or 20), 50))
            try:
                # Broad neutral query works across Mem0 OSS without relying on get_all availability.
                response = mem.search(query="user preferences projects decisions stable facts profile", filters=self._filters(), top_k=top_k)
                items = []
                for r in self._results(response):
                    metadata = r.get("metadata") or {}
                    if self._visible_to_this_agent(metadata):
                        items.append({"memory": r.get("memory") or r.get("text") or "", "score": r.get("score"), "metadata": metadata})
                    if len(items) >= top_k:
                        break
                return json.dumps({"results": items, "count": len(items), "agent_id": self._agent_id})
            except Exception as e:
                return tool_error(f"brain_profile failed: {e}")

        return tool_error(f"Unknown tool: {tool_name}")

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=3)
        with self._client_lock:
            self._memory = None


def register(ctx) -> None:
    ctx.register_memory_provider(SupabaseMem0MemoryProvider())
