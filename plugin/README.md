# Supabase Mem0 Brain Provider

Persistent user-installed Hermes memory provider stored under `$HERMES_HOME/plugins/supabase_mem0`, backed by Mem0 OSS and Supabase pgvector.

## Required env vars

```env
SUPABASE_BRAIN_DB_URL="postgresql://...?...sslmode=require"
OPENAI_API_KEY="sk-..."
```

## Optional env vars

```env
SUPABASE_BRAIN_COLLECTION="hermes_brain"
SUPABASE_BRAIN_AUTO_SYNC="false"
SUPABASE_BRAIN_LOBE_ID="nucleus"
# Deprecated aliases still accepted for old configs: SUPABASE_BRAIN_WORKSPACE_ID="nucleus", SUPABASE_BRAIN_USER_ID="nucleus"
SUPABASE_BRAIN_AGENT_ID="hermes"
SUPABASE_BRAIN_ADMIN_TOKEN="..."  # trusted direct-DB/control-plane agents only
SUPABASE_BRAIN_AGENT_TOKEN="..."  # client agents only, for API/MCP proxy mode
SUPABASE_BRAIN_ALLOWED_LOBES="*"
SUPABASE_BRAIN_DEFAULT_SCOPE="agent"
SUPABASE_BRAIN_DEFAULT_VISIBILITY="private"
```

For multi-agent deployments, keep the same `SUPABASE_BRAIN_COLLECTION`, put agents that should share a memory lobe in the same `SUPABASE_BRAIN_LOBE_ID`, set a unique `SUPABASE_BRAIN_AGENT_ID` for each independent main agent, and give broad DB/MCP-admin env only to trusted agents. Do not use subagent/profile names as main-agent IDs. `SUPABASE_BRAIN_USER_ID` is a legacy alias for the lobe id, not the human creator.

Security split:

- Trusted agents: direct DB access via `SUPABASE_BRAIN_DB_URL`; optional control-plane MCP admin via `SUPABASE_BRAIN_ADMIN_TOKEN`; `SUPABASE_BRAIN_ALLOWED_LOBES=*` is acceptable.
- Client agents: no direct DB URL and no admin token. Use a Brain API/MCP proxy with `SUPABASE_BRAIN_AGENT_ID` + `SUPABASE_BRAIN_AGENT_TOKEN`; server-side permissions must come from `public.hermes_agent_auth`.

## Governance metadata

Every explicit `brain_remember` write includes:

```json
{
  "lobe_id": "... Brain sector/lobe",
  "user_id": "... legacy Mem0 tenant key; same value as lobe_id",
  "agent_id": "...",
  "created_by_agent": "...",
  "owner_agent_id": "...",
  "main_agent_id": "...",
  "subagent_profile_id": "optional internal profile, e.g. claude-code-agent or codex-agent",
  "subject_agent_id": "optional agent/profile the memory is about",
  "scope": "agent | shared | user | project",
  "visibility": "private | shared | restricted",
  "project_id": "optional-project-id",
  "created_by_user_id": "optional human/platform id",
  "created_by_username": "optional human-readable name",
  "created_by_platform": "optional platform",
  "category": "...",
  "importance": 1
}
```

Defaults:

- `scope=agent`
- `visibility=private`
- `owner_agent_id=$SUPABASE_BRAIN_AGENT_ID`
- `main_agent_id=$SUPABASE_BRAIN_AGENT_ID`

Use `scope=shared, visibility=shared` for memories that should be available to all trusted agents sharing the same lobe/collection.

Use `scope=project, project_id=<project>, visibility=shared` for project-level knowledge.

## Tools

- `brain_search` — semantic search over Supabase-backed memories. By default it returns shared/user memories plus private memories owned by the current agent.
- `brain_remember` — store an explicit durable fact with governance metadata.
- `brain_profile` — retrieve broader profile/overview memories for the current lobe/agent view.

By default, automatic per-turn sync is disabled. This avoids dumping every chat turn into the brain before governance/review workflows are ready.
