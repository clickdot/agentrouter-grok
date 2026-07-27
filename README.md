# agentrouter-grok

Run xAI's open-source **Grok CLI** ([`xai-org/grok-build`](https://github.com/xai-org/grok-build))
against [AgentRouter](https://agentrouter.org) instead of x.ai — with `gpt`, `claude`,
`kimi`, and `glm` models, switchable at runtime with `/model`.

No fork, no recompile. A tiny local proxy makes the stock `grok` binary speak
AgentRouter's dialect.

---

## Quick start

```bash
# 1. Install the stock grok CLI (skip if you already have it)
curl -fsSL https://x.ai/cli/install.sh | bash

# 2. Clone this repo and start the proxy
git clone https://github.com/clickdot/agentrouter-grok
cd agentrouter-grok
python3 proxy.py &                     # runs on 127.0.0.1:8788

# 3. Set up config: copy the template into an isolated grok home,
#    then paste your AgentRouter token (https://agentrouter.org/register)
mkdir -p ~/.agentrouter-grok
cp config.template.toml ~/.agentrouter-grok/config.toml
${EDITOR:-nano} ~/.agentrouter-grok/config.toml   # replace YOUR_AGENTROUTER_TOKEN

# 4. Run grok against it (isolated — your existing ~/.grok is untouched)
GROK_HOME=~/.agentrouter-grok grok
```

Tip: add an alias so you don't type the path every time:

```bash
echo 'alias grok2="GROK_HOME=~/.agentrouter-grok grok"' >> ~/.bashrc
source ~/.bashrc
grok2                 # switch models in-session with /model
grok2 -m opus         # launch on Claude Opus
grok2 -p "17 * 23?"   # headless one-shot
```

Keep `python3 proxy.py` running while you use grok (background it, or use the
optional installer / systemd unit below to manage it for you).

## Models

Switch anytime with `/model` inside grok, or `-m <alias>` at launch:

| alias      | AgentRouter model |
|------------|-------------------|
| `gpt`      | `gpt-5.5` (default) |
| `gpt-sol`  | `gpt-5.6-sol`     |
| `opus`     | `claude-opus-4-8` |
| `opus-46`  | `claude-opus-4-6` |
| `kimi`     | `kimi-k3`         |
| `glm`      | `glm-5.2`         |

---

## Why a proxy is needed

Grok's CLI speaks a strict OpenAI chat-completions dialect. AgentRouter (built on
[One API](https://github.com/songquanpeng/one-api)) diverges in two ways that break
grok out of the box — both fixed by `proxy.py`:

1. **Client gating.** Raw requests get `401 unauthorized client detected`.
   AgentRouter only accepts the `codex_cli_rs` client identity, so the proxy
   injects `User-Agent: codex_cli_rs/0.80.0` and `originator: codex_cli_rs`.
2. **Missing `created` field.** AgentRouter omits the `created` timestamp from
   both non-streaming responses **and every streaming chunk**. Grok's
   deserializer requires it (`missing field 'created'`), so the proxy injects it.
   It also drops `billing.summary` events and intermittent bare `data: null`
   chunks that otherwise crash grok with
   `invalid type: null, expected struct ChatCompletionChunk`.

---

## Optional: one-command installer

If you'd rather not manage the proxy and config by hand, `install.sh` does the
clone-time steps for you — installs grok if missing, drops the proxy and config
into `~/.agentrouter-grok`, prompts for your token, and installs a `grok2`
wrapper that auto-starts the proxy:

```bash
curl -fsSL https://raw.githubusercontent.com/clickdot/agentrouter-grok/main/install.sh | bash
```

## Want a standalone renamed binary?

See [`FORK.md`](FORK.md) — heavy (recompiles the Rust CLI), and it still needs
the proxy running, so the clone-and-run flow above is almost always the better choice.

---

## Security notes

- Your token lives only in your local `~/.agentrouter-grok/config.toml`. It is
  never committed here — the template ships a placeholder.
- The config uses static-key (BYOK) auth, so grok never sends x.ai session tokens.
- All prompts/code route through `agentrouter.org`. Use accordingly.

## Credits

- [`xai-org/grok-build`](https://github.com/xai-org/grok-build) — the Grok CLI (Apache-2.0).
- Header-spoofing approach adapted from [`clickdot/agentrouter-cli-configs`](https://github.com/clickdot/agentrouter-cli-configs).
