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
SUPABASE_BRAIN_USER_ID="hermes-user"
SUPABASE_BRAIN_AGENT_ID="hermes"
SUPABASE_BRAIN_DEFAULT_SCOPE="agent"
SUPABASE_BRAIN_DEFAULT_VISIBILITY="private"
```

For multi-agent deployments, keep the same `SUPABASE_BRAIN_COLLECTION` and set a unique `SUPABASE_BRAIN_AGENT_ID` for each independent main agent. Do not use subagent/profile names as main-agent IDs.

## Governance metadata

Every explicit `brain_remember` write includes:

```json
{
  "user_id": "...",
  "agent_id": "...",
  "created_by_agent": "...",
  "owner_agent_id": "...",
  "main_agent_id": "...",
  "subagent_profile_id": "optional internal profile, e.g. claude-code-agent or codex-agent",
  "subject_agent_id": "optional agent/profile the memory is about",
  "scope": "agent | shared | user | project",
  "visibility": "private | shared | restricted",
  "project_id": "optional-project-id",
  "category": "...",
  "importance": 1
}
```

Defaults:

- `scope=agent`
- `visibility=private`
- `owner_agent_id=$SUPABASE_BRAIN_AGENT_ID`
- `main_agent_id=$SUPABASE_BRAIN_AGENT_ID`

Use `scope=shared, visibility=shared` for memories that should be available to all trusted agents sharing the same user/collection.

Use `scope=project, project_id=<project>, visibility=shared` for project-level knowledge.

## Tools

- `brain_search` — semantic search over Supabase-backed memories. By default it returns shared/user memories plus private memories owned by the current agent.
- `brain_remember` — store an explicit durable fact with governance metadata.
- `brain_profile` — retrieve broader profile/overview memories for the current user/agent view.

By default, automatic per-turn sync is disabled. This avoids dumping every chat turn into the brain before governance/review workflows are ready.
