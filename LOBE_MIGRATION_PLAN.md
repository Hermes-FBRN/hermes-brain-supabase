# Brain Lobe Migration Plan

This plan formalizes the Brain as isolated but optionally connected sectors/lobes. It is intentionally cautious: do not rewrite existing memory namespaces until the target access model is verified.

## Mental model

- **Brain**: the whole Supabase/Mem0 memory system.
- **Lobe / sector**: a top-level memory area, identified by `lobe_id` and mirrored to `workspace_id` / legacy Mem0 `user_id` for compatibility.
- **Project / category**: optional subdivision inside a lobe, usually represented by `project_id` and/or `category`.
- **Human actor**: who asked for or authored the memory, represented by `created_by_user_id`, `created_by_username`, and `created_by_platform`.
- **Agent actor**: the main agent that wrote or owns the memory, represented by `created_by_agent`, `main_agent_id`, and `owner_agent_id`.

## First lobe: `nucleus`

`nucleus` is the vital operational core. It stores the minimum high-value information needed for survival, restoration, cloning, and environment migration of the agent ecosystem.

Examples of appropriate `nucleus` memories:

- where each main agent lives, e.g. Railway vs VPS;
- how to clone/recreate an agent environment;
- critical service boundaries and bootstrap dependencies;
- non-secret pointers to credentials/config locations;
- emergency governance/policy facts required before taking action.

Examples that should usually **not** go in `nucleus`:

- raw transcripts;
- routine project notes;
- client-specific operational detail;
- secrets/tokens/passwords;
- short-lived task progress.

## Access model

Initial allowed main agents for `nucleus`:

- `hermes-main`
- `seraph-main`
- `smith-main`

Explicitly not allowed unless later granted:

- `mario-main`

This access list is enforced at the MCP layer with `SUPABASE_BRAIN_ALLOWED_LOBES`; it must also be protected operationally by each deployment’s secrets/config so an unauthorized agent cannot simply set itself to `nucleus`.

## Canonical metadata for nucleus memories

```json
{
  "lobe_id": "nucleus",
  "workspace_id": "nucleus",
  "user_id": "nucleus",
  "project_id": "optional-project-or-system-area",
  "category": "governance | infra | bootstrap | recovery | credentials-pointer",
  "scope": "project",
  "visibility": "shared",
  "created_by_user_id": "optional-human/platform-id",
  "created_by_username": "optional-name",
  "created_by_platform": "optional-platform",
  "created_by_agent": "hermes-main",
  "main_agent_id": "hermes-main",
  "owner_agent_id": "hermes-main"
}
```

## Safe migration phases

### Phase 0 — No DB rewrite

- Add code aliases: prefer `SUPABASE_BRAIN_LOBE_ID`, accept `SUPABASE_BRAIN_WORKSPACE_ID`, keep `SUPABASE_BRAIN_USER_ID` as legacy.
- New writes include `lobe_id`, `workspace_id`, and legacy `user_id` with the same lobe value.
- Document `nucleus` and access policy.

### Phase 1 — Audit existing memories

- Count current memories by `user_id`, `workspace_id`, `project_id`, `scope`, `visibility`, and `owner_agent_id`.
- Identify high-importance governance/infra/bootstrap records that belong in `nucleus`.
- Do not move client/project/task records into `nucleus`.

### Phase 2 — Dry-run mapping

Create a CSV/JSON proposal with rows:

```text
memory_id, old_user_id, proposed_lobe_id, proposed_project_id, reason, risk
```

Review manually before applying.

### Phase 3 — Backfill only approved rows

For each approved nucleus row, set:

```json
{
  "lobe_id": "nucleus",
  "workspace_id": "nucleus",
  "user_id": "nucleus"
}
```

If the human actor is known, add:

```json
{
  "created_by_user_id": "...",
  "created_by_username": "...",
  "created_by_platform": "..."
}
```

### Phase 4 — Verify retrieval

Run searches as each allowed and disallowed main agent:

- `hermes-main`
- `seraph-main`
- `smith-main`
- `mario-main`

Verify:

- allowed agents can retrieve `nucleus` governance/bootstrap records;
- disallowed agents cannot retrieve private/restricted `nucleus` records;
- non-nucleus project memories remain outside `nucleus`.

## Environment variables

Preferred:

```env
SUPABASE_BRAIN_LOBE_ID=nucleus
SUPABASE_BRAIN_ALLOWED_LOBES=nucleus
```

Accepted alias:

```env
SUPABASE_BRAIN_WORKSPACE_ID=nucleus
```

Legacy compatibility only:

```env
SUPABASE_BRAIN_USER_ID=nucleus
```

`SUPABASE_BRAIN_USER_ID` should not be interpreted as the human creator.
