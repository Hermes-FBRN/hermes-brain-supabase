#!/usr/bin/env bash
# =============================================================================
# Hermes Brain Plugin — Install script
# =============================================================================
# Idempotent: safe to run multiple times. Skips steps already done.
#
# Installs to $HERMES_HOME (default: /data/.hermes):
#   plugins/supabase_mem0/       — memory provider (auto-discovered by Hermes)
#   mcp/brain-manager/server.py  — admin MCP server
#   vendor/supabase_mem0/        — vendored pip deps
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${HERMES_HOME:-/data/.hermes}"
PLUGIN_SRC="$SCRIPT_DIR/plugin"
MCP_SRC="$SCRIPT_DIR/mcp-server"

info()    { echo "[brain-install] $*"; }
done_()   { echo "[brain-install] ✓ $*"; }
skip()    { echo "[brain-install] ⊘ $* (already exists)"; }
warn()    { echo "[brain-install] ⚠ $*"; }

# ── Plugin ────────────────────────────────────────────────────────────────
PLUGIN_DST="$HERMES_HOME/plugins/supabase_mem0"
if [[ -d "$PLUGIN_DST" ]]; then
    skip "Plugin → $PLUGIN_DST"
else
    info "Installing plugin → $PLUGIN_DST"
    mkdir -p "$PLUGIN_DST"
    cp "$PLUGIN_SRC/plugin.yaml" "$PLUGIN_DST/"
    cp "$PLUGIN_SRC/README.md"   "$PLUGIN_DST/"
    cp "$PLUGIN_SRC/__init__.py" "$PLUGIN_DST/"
    done_ "Plugin installed"
fi

# ── MCP server ─────────────────────────────────────────────────────────────
MCP_DST="$HERMES_HOME/mcp/brain-manager"
if [[ -f "$MCP_DST/server.py" ]]; then
    skip "MCP server → $MCP_DST/server.py"
else
    info "Installing MCP server → $MCP_DST"
    mkdir -p "$MCP_DST"
    cp "$MCP_SRC/server.py" "$MCP_DST/server.py"
    chmod +x "$MCP_DST/server.py"
    done_ "MCP server installed"
fi

# ── Vendor deps ────────────────────────────────────────────────────────────
VENDOR_DIR="$HERMES_HOME/vendor/supabase_mem0"
if [[ -d "$VENDOR_DIR" ]] && python3 -c "import sys; sys.path.insert(0,'$VENDOR_DIR'); import mem0" 2>/dev/null; then
    skip "Vendor deps → $VENDOR_DIR"
else
    info "Installing Python deps → $VENDOR_DIR"
    mkdir -p "$VENDOR_DIR"
    PYTHON=$(command -v python3 || command -v python)
    "$PYTHON" -m pip install \
        --quiet --target "$VENDOR_DIR" --no-deps \
        "mem0ai==2.0.2" "vecs==0.4.5" "psycopg[binary]" 2>&1 | tail -3
    done_ "Vendor deps installed"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Hermes Brain Plugin — Installato"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Attiva con:"
echo "    hermes memory setup"
echo "    hermes config set memory.provider supabase_mem0"
echo ""
echo "  Env vars necessarie in .env:"
echo "    SUPABASE_BRAIN_DB_URL=postgresql://..."
echo "    OPENAI_API_KEY=sk-..."
echo ""
