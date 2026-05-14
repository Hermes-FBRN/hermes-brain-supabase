# Supabase Mem0 Brain Provider

Persistent user-installed Hermes memory provider stored under `$HERMES_HOME/plugins/supabase_mem0`, backed by Mem0 OSS and Supabase pgvector.

## Required env vars

```env
SUPABASE_BRAIN_DB_URL="postgresql://...?...sslmode=require"
OPENAI_API_KEY="sk-..."
```

Optional:

```env
SUPABASE_BRAIN_COLLECTION="hermes_brain"
SUPABASE_BRAIN_AUTO_SYNC="false"
SUPABASE_BRAIN_USER_ID="hermes-user"
SUPABASE_BRAIN_AGENT_ID="hermes"
```

## Tools

- `brain_search` — semantic search over Supabase-backed memories
- `brain_remember` — store an explicit durable fact
- `brain_profile` — retrieve broader profile/overview memories

By default, automatic per-turn sync is disabled. This avoids dumping every chat turn into the brain before governance is designed.
