#!/usr/bin/env bash
#
# agentrouter-grok installer
#
#   curl -fsSL https://raw.githubusercontent.com/clickdot/agentrouter-grok/main/install.sh | bash
#
# What it does:
#   1. Installs the stock `grok` CLI (xai-org/grok-build) if not present.
#   2. Sets up an isolated GROK_HOME at ~/.agentrouter-grok so your existing
#      ~/.grok sessions/auth are never touched.
#   3. Installs the compatibility proxy (proxy.py).
#   4. Writes config.toml with all AgentRouter models, prompting for your token.
#   5. Installs a `grok2` wrapper that auto-starts the proxy then launches grok
#      against the isolated config.
#
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/clickdot/agentrouter-grok/main"
AR_HOME="${AGENTROUTER_GROK_HOME:-$HOME/.agentrouter-grok}"
BIN_DIR="${AGENTROUTER_GROK_BIN:-$HOME/.local/bin}"
PROXY_PORT="${AR_PROXY_PORT:-8788}"

say() { printf '\033[1;36m[agentrouter-grok]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[agentrouter-grok]\033[0m %s\n' "$*" >&2; }

# --- 1. stock grok CLI -------------------------------------------------------
if ! command -v grok >/dev/null 2>&1; then
  say "Installing stock grok CLI from x.ai ..."
  curl -fsSL https://x.ai/cli/install.sh | bash
else
  say "Found existing grok: $(command -v grok)"
fi

mkdir -p "$AR_HOME" "$BIN_DIR"

# --- 2. proxy ----------------------------------------------------------------
say "Installing proxy.py ..."
if [ -f "$(dirname "$0")/proxy.py" ]; then
  cp "$(dirname "$0")/proxy.py" "$AR_HOME/proxy.py"
else
  curl -fsSL "$REPO_RAW/proxy.py" -o "$AR_HOME/proxy.py"
fi

# --- 3. token ----------------------------------------------------------------
TOKEN="${AGENTROUTER_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  printf 'Paste your AgentRouter token (from https://agentrouter.org/register): '
  read -r TOKEN </dev/tty
fi
if [ -z "$TOKEN" ]; then
  err "No token provided; writing placeholder. Edit $AR_HOME/config.toml later."
  TOKEN="YOUR_AGENTROUTER_TOKEN"
fi

# --- 4. config ---------------------------------------------------------------
say "Writing config.toml (all models) ..."
if [ -f "$(dirname "$0")/config.template.toml" ]; then
  TEMPLATE="$(cat "$(dirname "$0")/config.template.toml")"
else
  TEMPLATE="$(curl -fsSL "$REPO_RAW/config.template.toml")"
fi
printf '%s\n' "$TEMPLATE" \
  | sed "s|YOUR_AGENTROUTER_TOKEN|$TOKEN|g" \
  | sed "s|127.0.0.1:8788|127.0.0.1:$PROXY_PORT|g" \
  > "$AR_HOME/config.toml"
chmod 600 "$AR_HOME/config.toml"

# --- 5. grok2 wrapper --------------------------------------------------------
say "Installing grok2 wrapper -> $BIN_DIR/grok2"
cat > "$BIN_DIR/grok2" <<WRAP
#!/usr/bin/env bash
# grok2: stock grok CLI wired to AgentRouter via the local compat proxy.
set -euo pipefail
AR_HOME="${AR_HOME}"
PROXY_PORT="${PROXY_PORT}"

# Start the proxy if nothing is listening on the port.
if ! python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(s.connect_ex(('127.0.0.1',${PROXY_PORT})))" 2>/dev/null; then
  AR_PROXY_PORT="\${PROXY_PORT}" nohup python3 "\${AR_HOME}/proxy.py" \
    >"\${AR_HOME}/proxy.log" 2>&1 &
  sleep 1
fi

exec env GROK_HOME="\${AR_HOME}" grok "\$@"
WRAP
chmod +x "$BIN_DIR/grok2"

say "Done."
say "GROK_HOME : $AR_HOME"
say "Wrapper   : $BIN_DIR/grok2"
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) say "NOTE: $BIN_DIR is not on your PATH. Add it:"
     say "      export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
say "Run:  grok2        (switch models in-session with /model, or grok2 -m opus)"
