# Forking & rebuilding a renamed `grok2` binary

> **You almost certainly don't need this.** The stock `grok` binary already works
> perfectly against AgentRouter once the proxy + config are in place (that's what
> `install.sh` sets up). Rebuilding only changes the binary's *name* — it does
> **not** remove the need for the proxy, because the two AgentRouter quirks
> (client-identity headers + missing `created` field) are server-side.
>
> Do this only if you specifically want a standalone binary literally called
> `grok2` with its own baked-in defaults.

## Why it's heavy

- `xai-org/grok-build` is a large Rust workspace (`Cargo.lock` ~341 KB).
- Build needs the pinned Rust toolchain + `protoc` + `dotslash`.
- The repo is a **one-way periodic mirror** from xAI's monorepo and does not
  accept external contributions — so you are maintaining a hard fork and
  re-patching on every upstream sync.

## Steps

```bash
# 1. Toolchain
cargo install dotslash
# install protoc via your package manager (apt install -y protobuf-compiler, etc.)

# 2. Clone
git clone https://github.com/xai-org/grok-build
cd grok-build

# 3. (Optional) rename the shipped binary to grok2
#    The binary crate is crates/codegen/xai-grok-pager-bin.
#    Edit its Cargo.toml [[bin]] name, or just rename the output artifact after build.

# 4. (Optional) bake in AgentRouter defaults so the binary needs no config file.
#    The endpoint override env var is:
#        GROK_PRODUCTION_CLI_CHAT_PROXY_BASE_URL
#    and API-key auth is via:
#        XAI_API_KEY   (legacy: GROK_CODE_XAI_API_KEY)
#    You can hardcode defaults in crates/codegen/xai-grok-env/src/lib.rs
#    (search for the base-url resolve() default) — but you STILL need the proxy
#    for the `created`-field fix, so point the default at the proxy, e.g.
#        http://127.0.0.1:8788/v1

# 5. Build
cargo build -p xai-grok-pager-bin --release
# artifact: target/release/xai-grok-pager

# 6. Install as grok2
cp target/release/xai-grok-pager ~/.local/bin/grok2
```

## Recommended baked-in defaults (still requires proxy running)

If you patch `xai-grok-env`'s base-url default to the proxy and ship `XAI_API_KEY`
via the environment, `grok2` can launch with no config file — but the proxy
(`proxy.py`) must still be running to fix the `created` field. For that reason the
wrapper approach in `install.sh` is simpler and just as effective.
