# Hermes Brain — Supabase + Mem0

Plugin custom per Hermes Agent che aggiunge memoria semantica persistente
usando Mem0 OSS con Supabase pgvector come backend.

## Cosa contiene

| Path | Descrizione |
|------|-------------|
| `plugin/` | Memory provider `supabase_mem0` — si auto-registra in Hermes |
| `mcp-server/` | MCP brain-manager per amministrazione (archive, dedup, quality report) |
| `install.sh` | Script di installazione idempotente — copia tutto in `$HERMES_HOME` |
| `README.md` | Questo file |

## Quick start

```bash
git clone https://github.com/hermes-fbrn/hermes-brain-supabase.git
cd hermes-brain-supabase
bash install.sh
```

L'installazione è idempotente: se il plugin esiste già, salta tutto.

Il plugin viene installato ma **NON attivato**. Per attivarlo:

```bash
hermes memory setup          # wizard → scegli supabase_mem0
# oppure
hermes config set memory.provider supabase_mem0
```

## Env vars richieste

```env
SUPABASE_BRAIN_DB_URL=postgresql://postgres.xxxx:password@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require
OPENAI_API_KEY=sk-...
```

Opzionali:
```env
SUPABASE_BRAIN_COLLECTION=hermes_brain
SUPABASE_BRAIN_USER_ID=hermes-user
SUPABASE_BRAIN_AGENT_ID=hermes
```

## Integrazione in start.sh

Aggiungi questo blocco prima di avviare Hermes:

```bash
# Brain plugin — installa al primo deploy, idempotente
BRAIN_DIR="/app/hermes-brain-supabase"
if [[ -d "$BRAIN_DIR" ]] && [[ ! -d "${HERMES_HOME}/plugins/supabase_mem0" ]]; then
    echo "[start.sh] Installing brain plugin..."
    bash "$BRAIN_DIR/install.sh" || echo "[start.sh] Brain install failed (non-fatal)"
fi
```

## Struttura completa

```
hermes-brain-supabase/
├── README.md
├── install.sh
├── plugin/
│   ├── plugin.yaml
│   ├── README.md
│   └── __init__.py          # provider Python
└── mcp-server/
    └── server.py             # brain-manager MCP
```
