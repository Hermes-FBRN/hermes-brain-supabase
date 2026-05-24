# Collegare un nuovo main agent al Brain Supabase/Mem0

Questa guida installa il plugin `supabase_mem0` e il MCP amministrativo `brain-manager` su un nuovo agente Hermes, collegandolo allo stesso Brain condiviso.

## 0. Modello mentale corretto

- `SUPABASE_BRAIN_AGENT_ID` identifica un **main agent indipendente**.
- I subagent/profile interni non vanno usati come main-agent ID.
- Se un main agent scrive una memoria per un proprio profilo interno, usa metadata separati:
  - `main_agent_id`: main agent indipendente.
  - `subagent_profile_id`: profilo/subagent interno.
  - `subject_agent_id`: agente/profilo di cui parla la memoria.
  - `owner_agent_id`: main agent che possiede operativamente la memoria.

Per Hermes, i profili interni vanno nominati esplicitamente `claude-code-agent` e `codex-agent`, non con abbreviazioni ambigue.

## 1. Prerequisiti

Sul nuovo host/agente:

```bash
hermes --version
python3 --version
python3 -m pip --version
git --version
```

Serve anche l'accesso agli stessi secret del Brain:

```env
SUPABASE_BRAIN_DB_URL=postgresql://...
OPENAI_API_KEY=...
```

Non committare mai questi valori nel repo.

## 2. Clona il plugin

```bash
mkdir -p "$HOME/Developer"
git clone https://github.com/Hermes-FBRN/hermes-brain-supabase.git "$HOME/Developer/hermes-brain-supabase"
cd "$HOME/Developer/hermes-brain-supabase"
```

Se il repo esiste già:

```bash
cd "$HOME/Developer/hermes-brain-supabase"
git pull --ff-only
```

## 3. Configura `.env` del nuovo main agent

Trova il path env corretto:

```bash
hermes config env-path
```

Aggiungi i valori, adattando `SUPABASE_BRAIN_LOBE_ID` al lobo/area del Brain e `SUPABASE_BRAIN_AGENT_ID` al nuovo main agent:

```env
SUPABASE_BRAIN_DB_URL=postgresql://...
OPENAI_API_KEY=...
SUPABASE_BRAIN_COLLECTION=hermes_brain
SUPABASE_BRAIN_LOBE_ID=nucleus
# Aliases still accepted, but prefer LOBE_ID:
# SUPABASE_BRAIN_WORKSPACE_ID=nucleus
# SUPABASE_BRAIN_USER_ID=nucleus
SUPABASE_BRAIN_AGENT_ID=<new-main-agent-id>
SUPABASE_BRAIN_DEFAULT_SCOPE=agent
SUPABASE_BRAIN_DEFAULT_VISIBILITY=private
SUPABASE_BRAIN_AUTO_SYNC=false
```

Esempio:

```env
SUPABASE_BRAIN_AGENT_ID=research-main
```

Nota: `SUPABASE_BRAIN_LOBE_ID` è il namespace/lobo del Brain, non l’utente umano creatore. Per tracciare l’attore umano usa metadata come `created_by_user_id`, `created_by_username` e `created_by_platform` quando disponibili.

## 4. Installa plugin e MCP

```bash
cd "$HOME/Developer/hermes-brain-supabase"
bash install.sh
```

Installa:

- `$HERMES_HOME/plugins/supabase_mem0/`
- `$HERMES_HOME/mcp/brain-manager/server.py`
- `$HERMES_HOME/vendor/supabase_mem0/`

## 5. Attiva il memory provider

```bash
hermes config set memory.provider supabase_mem0
```

Controlla:

```bash
hermes memory status
```

Se il comando non è disponibile nella build corrente, controlla direttamente:

```bash
hermes config | grep -A 8 '^memory:'
```

## 6. Configura il MCP `brain-manager`

Aggiungi a `$HERMES_HOME/config.yaml`:

```yaml
mcp_servers:
  brain-manager:
    command: python3
    args:
      - ${HERMES_HOME}/mcp/brain-manager/server.py
    timeout: 120
    connect_timeout: 60
    sampling:
      enabled: false
```

Se `${HERMES_HOME}` non viene espanso dalla tua versione/config, usa il path assoluto, per esempio:

```yaml
args:
  - /data/.hermes/mcp/brain-manager/server.py
```

## 7. Ricarica Hermes/MCP

In una sessione gateway/chat:

```text
/reload-mcp
```

Se i tool non compaiono, avvia una nuova sessione o riavvia il gateway dall'esterno della sessione agente. Non far riavviare il gateway all'agente stesso se è in esecuzione dentro il gateway.

Da terminale puoi testare senza riavviare tutto:

```bash
hermes mcp test brain-manager
```

## 8. Verifica health e identità

Chiedi al nuovo agente di eseguire un health check del Brain, oppure da terminale:

```bash
hermes mcp test brain-manager
```

Poi verifica che il provider runtime veda l'identità corretta:

```bash
python3 - <<'PY'
import os
print('SUPABASE_BRAIN_AGENT_ID=', os.environ.get('SUPABASE_BRAIN_AGENT_ID', '<not loaded in this shell>'))
print('SUPABASE_BRAIN_COLLECTION=', os.environ.get('SUPABASE_BRAIN_COLLECTION', '<default hermes_brain>'))
PY
```

Nota: la shell potrebbe non caricare `$HERMES_HOME/.env`; Hermes lo carica internamente.

## 9. Scrittura test consigliata

Dal nuovo main agent, salva una memoria non sensibile:

```text
Store this durable Brain test: "<new-main-agent-id> can access the shared Hermes/FBRN Brain lobe." category=governance importance=5 scope=agent visibility=private project_id=hermes-fbrn lobe_id=nucleus
```

Per una memoria riferita a un profilo interno:

```json
{
  "memory": "Internal profile worker-a belongs to <new-main-agent-id> and uses the shared Brain via that main agent.",
  "category": "governance",
  "importance": 6,
  "scope": "agent",
  "visibility": "private",
  "project_id": "hermes-fbrn",
  "lobe_id": "nucleus",
  "workspace_id": "nucleus",
  "created_by_user_id": "optional-human/platform-id",
  "created_by_username": "optional-name",
  "created_by_platform": "optional-platform",
  "owner_agent_id": "<new-main-agent-id>",
  "main_agent_id": "<new-main-agent-id>",
  "subagent_profile_id": "worker-a",
  "subject_agent_id": "worker-a"
}
```

## 10. Retrieval test

Cerca:

```text
owner_agent_id main_agent_id subagent_profile_id shared durable memory layer
```

Atteso:

- risultati `shared`, `user` e `project` visibili dentro lo stesso `lobe_id`/workspace;
- memorie private visibili solo se `owner_agent_id` corrisponde al main agent corrente;
- record governance in alto grazie al ranking ibrido semantico + testuale.

## 11. Regole operative

- Default sicuro: `scope=agent`, `visibility=private`.
- Usa `scope=project`, `visibility=shared`, `project_id=hermes-fbrn` per conoscenza di progetto utile ai peer.
- Usa `scope=shared`, `visibility=shared` solo per policy/governance davvero globali.
- Non salvare raw transcript, log lunghi, token, password o secret.
- Non usare subagent/profile names come identità di main agent.
- Mantieni `SUPABASE_BRAIN_AUTO_SYNC=false` finché non esiste una governance UX/approval dedicata.

## 12. Update futuro

Su ogni agente già collegato:

```bash
cd "$HOME/Developer/hermes-brain-supabase"
git pull --ff-only
bash install.sh
hermes mcp test brain-manager
```

Poi in chat/gateway:

```text
/reload-mcp
```
