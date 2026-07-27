# agentrouter-grok

Run xAI's open-source **Grok CLI** ([`xai-org/grok-build`](https://github.com/xai-org/grok-build))
against [AgentRouter](https://agentrouter.org) instead of x.ai — with `gpt`, `claude`,
`kimi`, and `glm` models, switchable at runtime with `/model`.

No fork, no recompile required. A tiny local proxy makes the stock `grok` binary
speak AgentRouter's dialect.

---

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/clickdot/agentrouter-grok/main/install.sh | bash
```

The installer:
1. Installs the stock `grok` CLI if you don't have it.
2. Sets up an **isolated** `GROK_HOME` at `~/.agentrouter-grok` — your existing
   `~/.grok` sessions and x.ai auth are left untouched.
3. Installs the compatibility proxy and a `config.toml` with all models.
4. Prompts for your AgentRouter token (get one at https://agentrouter.org/register).
5. Installs a `grok2` wrapper that auto-starts the proxy and launches grok.

Then just:

```bash
grok2                 # start; switch models in-session with /model
grok2 -m opus         # launch on Claude Opus
grok2 -p "17 * 23?"   # headless one-shot
```

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
   It also drops the trailing `billing.summary` events.

---

## Manual setup (no installer)

```bash
# 1. stock grok
curl -fsSL https://x.ai/cli/install.sh | bash

# 2. proxy (keep running; systemd unit or nohup)
python3 proxy.py                       # 127.0.0.1:8788

# 3. config — copy config.template.toml to an isolated GROK_HOME,
#    replace YOUR_AGENTROUTER_TOKEN, then:
GROK_HOME=~/.agentrouter-grok grok
```

## Three ways to use this repo

| You want… | Use |
|-----------|-----|
| Just the pieces + docs | `proxy.py` + `config.template.toml` (this repo's base) |
| A one-command install + `grok2` wrapper | `install.sh` |
| A standalone renamed binary | see [`FORK.md`](FORK.md) (heavy; still needs the proxy) |

---

## Security notes

- Your token is written only to your local `~/.agentrouter-grok/config.toml`
  (chmod 600). It is never committed here — the template ships a placeholder.
- The config uses static-key (BYOK) auth, so grok never sends x.ai session tokens.
- All prompts/code route through `agentrouter.org`. Use accordingly.

## Credits

- [`xai-org/grok-build`](https://github.com/xai-org/grok-build) — the Grok CLI (Apache-2.0).
- Header-spoofing approach adapted from [`clickdot/agentrouter-cli-configs`](https://github.com/clickdot/agentrouter-cli-configs).
