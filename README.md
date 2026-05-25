# Hermes Brain — Supabase + Mem0

Custom Hermes Agent memory plugin that adds persistent semantic memory using Mem0 OSS with Supabase pgvector as the backend, plus a safe Brain Manager MCP server for administration.

## What it contains

| Path | Description |
|------|-------------|
| `plugin/` | Memory provider `supabase_mem0` — auto-registers in Hermes |
| `mcp-server/` | Brain Manager MCP server for health, search, remember, archive, dedup, quality reports |
| `install.sh` | Idempotent installer — copies plugin, MCP server, and vendored deps into `$HERMES_HOME` |
| `README.md` | This file |

## Quick start

```bash
git clone https://github.com/Hermes-FBRN/hermes-brain-supabase.git
cd hermes-brain-supabase
bash install.sh
```

The install is idempotent: if files already exist, it skips them.

The plugin is installed but **NOT activated**. To activate it:

```bash
hermes memory setup          # wizard → choose supabase_mem0
# or
hermes config set memory.provider supabase_mem0
```

## Required env vars

```env
SUPABASE_BRAIN_DB_URL=postgresql://postgres.xxxx:***@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require
OPENAI_API_KEY=sk-...
```

## Multi-agent identity and governance

The repo is generic: agent names are **not hardcoded**. Set identity per Hermes instance through env vars.

For multiple independent **main agents** sharing one Supabase brain, use the same DB and collection, but different main-agent IDs. Subagent/profile names must stay separate from main-agent IDs:

```env
SUPABASE_BRAIN_COLLECTION=hermes_brain
SUPABASE_BRAIN_LOBE_ID=nucleus          # Brain sector/lobe namespace, not a human creator id
# Deprecated aliases still accepted for old configs: SUPABASE_BRAIN_WORKSPACE_ID, then legacy SUPABASE_BRAIN_USER_ID
SUPABASE_BRAIN_AGENT_ID=hermes-main        # main agent id; change per independent main agent
SUPABASE_BRAIN_ADMIN_TOKEN=...             # only for trusted direct-DB control-plane agents
SUPABASE_BRAIN_ALLOWED_LOBES=*             # trusted direct-DB deployments only; client agents use server-side permissions
SUPABASE_BRAIN_DEFAULT_SCOPE=agent         # default: private to this agent
SUPABASE_BRAIN_DEFAULT_VISIBILITY=private  # default: not shown to other agents
SUPABASE_BRAIN_AUTO_SYNC=false             # recommended
```

Think of `SUPABASE_BRAIN_LOBE_ID` as the Brain lobe/sector. It isolates an area of memory such as `nucleus`, `client-x`, or `fabeeo-personal`; it is not the human who created the memory. `nucleus` is the first lobe: the vital operational core for survival, cloning, and environment restoration of trusted main agents. For attribution, store actor metadata such as `created_by_user_id`, `created_by_username`, and `created_by_platform` when available.

Each stored memory gets governance metadata:

```json
{
  "lobe_id": "nucleus",
  "user_id": "nucleus",
  "agent_id": "hermes-main",
  "created_by_agent": "hermes-main",
  "owner_agent_id": "hermes-main",
  "main_agent_id": "hermes-main",
  "subagent_profile_id": "optional-profile-name",
  "subject_agent_id": "optional-agent-or-profile-name",
  "scope": "agent",
  "visibility": "private",
  "project_id": "optional-project-name",
  "created_by_user_id": "optional-human/platform-id",
  "created_by_username": "optional-human-readable-name",
  "created_by_platform": "optional-platform",
  "category": "infra",
  "importance": 8,
  "source": "hermes_brain_remember"
}
```

Supported scopes:

| Scope | Meaning |
|-------|---------|
| `agent` | Private/specific to one agent by default |
| `shared` | Useful to all agents |
| `user` | User profile/preference memory, readable to all trusted agents for that user |
| `project` | Project-specific memory, optionally tagged with `project_id` |

Supported visibility values:

| Visibility | Meaning |
|------------|---------|
| `private` | Returned only to the owning/current agent by default |
| `shared` | Returned to all trusted agents sharing the same lobe/collection |
| `restricted` | Reserved for stricter future policy / explicit admin workflows |

Search defaults to shared/user memories in the current lobe plus private memories owned by the current main agent. Admin tools can pass `include_private=true` when needed. Use `subagent_profile_id` only when a main agent writes/reads on behalf of one of its internal profiles. For Hermes, profile names should be written explicitly as `claude-code-agent` and `codex-agent` to avoid confusing them with independent peer main agents.

## Lobe access policy

Use two access tiers:

- **Trusted main agents** (`hermes-main`, `seraph-main`, `smith-main`): may receive `SUPABASE_BRAIN_DB_URL`, `OPENAI_API_KEY`, `SUPABASE_BRAIN_ADMIN_TOKEN`, and broad `SUPABASE_BRAIN_ALLOWED_LOBES=*`.
- **Client / tenant agents**: must not receive `SUPABASE_BRAIN_DB_URL` or `SUPABASE_BRAIN_ADMIN_TOKEN`; they should access the Brain only through a Brain API/MCP proxy using `SUPABASE_BRAIN_AGENT_ID` + `SUPABASE_BRAIN_AGENT_TOKEN`.

`SUPABASE_BRAIN_ALLOWED_LOBES` is a trusted direct-DB deployment guard, not an authoritative tenant security boundary. If an untrusted agent can edit its own env, server-side permissions in `public.hermes_agent_auth` must decide which lobes/projects/sectors it can access.

Admin-only MCP tools are available only when `SUPABASE_BRAIN_ADMIN_TOKEN` is configured: `brain_register_agent`, `brain_list_agent_permissions`, `brain_revoke_agent`, plus metadata/archive/quality/dedup administration.

To provision a client agent token from a trusted main agent, call `brain_register_agent` with the target `agent_id` and allowed lobes/projects/sectors. It returns `agent_token` once; store that in the client deployment as `SUPABASE_BRAIN_AGENT_TOKEN`. The Brain stores only a hash.

See `LOBE_MIGRATION_PLAN.md` before migrating existing memories.

## Brain Manager MCP setup

After `bash install.sh`, add this to `$HERMES_HOME/config.yaml`:

```yaml
mcp_servers:
  brain-manager:
    command: python3
    args:
      - /data/.hermes/mcp/brain-manager/server.py
    timeout: 120
    sampling:
      enabled: false
```

Then reload MCP in chat:

```text
/reload-mcp
```

The MCP server supports the same governance fields on `brain_remember`:

```json
{
  "memory": "Stable fact to store",
  "category": "project",
  "importance": 8,
  "scope": "project",
  "visibility": "shared",
  "project_id": "brain-manager",
  "main_agent_id": "hermes-main",
  "subagent_profile_id": "codex-agent",
  "subject_agent_id": "codex-agent"
}
```

## Railway `start.sh` integration

Add this block before starting Hermes:

```bash
if [[ "${HERMES_BRAIN_BOOTSTRAP:-1}" != "0" ]]; then
    BRAIN_DIR="${HOME}/Developer/hermes-brain-supabase"

    if [[ ! -d "${HERMES_HOME}/plugins/supabase_mem0" ]]; then
        echo "[start.sh] Installing brain plugin..."

        if [[ ! -d "$BRAIN_DIR" ]]; then
            git clone https://github.com/Hermes-FBRN/hermes-brain-supabase.git "$BRAIN_DIR" 2>/dev/null || true
        fi

        if [[ -f "$BRAIN_DIR/install.sh" ]]; then
            bash "$BRAIN_DIR/install.sh" || echo "[start.sh] Brain install failed (non-fatal)"
        fi
    fi
fi
```

Disable bootstrap on a clone with:

```env
HERMES_BRAIN_BOOTSTRAP=0
```

## Repository structure

```text
hermes-brain-supabase/
├── README.md
├── install.sh
├── plugin/
│   ├── plugin.yaml
│   ├── README.md
│   └── __init__.py          # Hermes memory provider
└── mcp-server/
    └── server.py             # Brain Manager MCP
```
