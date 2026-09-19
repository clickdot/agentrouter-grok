#!/usr/bin/env python3
"""
AgentRouter <-> Grok CLI compatibility proxy.

Allows xAI's Grok CLI (xai-org/grok-build) to communicate with AgentRouter
(One API / New API gateway) seamlessly by handling dialect mismatches:
- Client gating: Spoofs codex_cli_rs identity headers required by AgentRouter.
- Missing timestamps: Injects `created` timestamp in streaming chunks and responses.
- Stream cleanup: Drops `billing.summary` events and bare `data: null` chunks.
- Null stripping: Prunes nulls that crash grok's strict Rust deserializer while
  preserving structural keys.
- Tool name sanitization: Replaces empty tool names to avoid schema errors.
- Smart Thinking Cache: Captures thinking traces/signatures in Anthropic messages
  mode and restores them into outgoing assistant messages so multi-turn tool loops
  do not trigger upstream HTTP 400 (`content[].thinking must be passed back`).
- Missing signatures: Injects thinking block signatures to avoid Rust panics.
- Orphan tool sanitization: Drops orphan tool call results that don't match the
  preceding assistant message.
"""
import json
import os
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

UPSTREAM = os.environ.get("AR_UPSTREAM", "https://agentrouter.org").rstrip("/")
HOST = os.environ.get("AR_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("AR_PROXY_PORT", "8788"))

# codex_cli_rs identity is what AgentRouter's One API gateway accepts.
CLIENT_UA = os.environ.get("AR_CLIENT_UA", "codex_cli_rs/0.80.0")
CLIENT_ORIGINATOR = os.environ.get("AR_CLIENT_ORIGINATOR", "codex_cli_rs")

# Headers we must not forward verbatim upstream.
SKIP_REQ_HEADERS = ("host", "content-length", "transfer-encoding",
                    "accept-encoding", "connection")
# Headers we must not echo back to the client (we recompute length; content is
# already decoded so any upstream content-encoding is stale).
SKIP_RESP_HEADERS = ("transfer-encoding", "content-length",
                     "content-encoding", "connection")


def ensure_created(obj):
    if isinstance(obj, dict) and "created" not in obj:
        obj["created"] = int(time.time())
    return obj


def strip_nulls(obj, parent_key=None):
    """Recursively remove keys/elements whose value is null, but avoid
    removing structural keys that must be present (e.g. 'name').

    AgentRouter emits null where grok's strict deserializer expects a number
    or struct — e.g. the final usage chunk carries `input_tokens_details: null`.
    We remove nulls in most places, but do not drop keys that are structural:
    'name', 'id', 'function', 'type', 'role' (and similar). Also drop null
    elements inside lists.
    """
    # Keys considered structural; don't drop them even if value is None.
    PROTECTED_KEYS = {"name", "id", "function", "type", "role", "content"}

    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            # If value is None and key is protected, preserve the key/value.
            if v is None:
                if k in PROTECTED_KEYS:
                    out[k] = v
                # else drop it (skip)
                continue
            out[k] = strip_nulls(v, parent_key=k)
        return out
    if isinstance(obj, list):
        # Drop None elements inside lists; recurse into members.
        return [strip_nulls(v, parent_key=parent_key) for v in obj if v is not None]
    return obj


def sanitize_tool_schemas(obj):
    """Strip `default: null` entries from outgoing JSON Schemas.

    Grok's tool definitions (e.g. the workflow `args` parameter) include
    `{"default": null, "description": ...}` schema fragments with no `type`.
    Stricter model backends reject these with
    `JSON Schema not supported: could not understand the instance ...`.
    A null default is meaningless, so removing it is always safe. Applied only
    to outgoing request bodies; leaves message content untouched.
    """
    if isinstance(obj, dict):
        return {k: sanitize_tool_schemas(v)
                for k, v in obj.items()
                if not (k == "default" and v is None)}
    if isinstance(obj, list):
        return [sanitize_tool_schemas(v) for v in obj]
    return obj


def sanitize_empty_names(obj, path="root"):
    """Recursively replace empty-string tool/function names with a safe
    placeholder so upstream validation does not fail.

    This mutates obj in-place and returns it.
    """
    if isinstance(obj, dict):
        # If this dict has a 'function' dict with an empty 'name', fix it.
        func = obj.get("function")
        if isinstance(func, dict):
            name = func.get("name")
            if name == "":
                func["name"] = "__unnamed_tool"
                print(f"[agentrouter-grok] warning: sanitized empty function name at {path}/function", flush=True)
        # Direct tool definitions with empty 'name'
        if "name" in obj and obj.get("name") == "":
            obj["name"] = "__unnamed_tool"
            print(f"[agentrouter-grok] warning: sanitized empty name at {path}", flush=True)
        for k, v in obj.items():
            sanitize_empty_names(v, path=f"{path}/{k}")
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            sanitize_empty_names(v, path=f"{path}[{idx}]")
    return obj


def drop_empty_response_names(obj):
    """Remove empty names from response deltas.

    In a streamed tool call the function name arrives only in the first chunk;
    continuation chunks carry `name: ""` alongside more argument text. An empty
    name means "no name update", so it must be removed rather than replaced —
    substituting a placeholder overwrites the real name from the first chunk
    and every tool call arrives as that placeholder.
    """
    if isinstance(obj, dict):
        if obj.get("name") == "":
            obj.pop("name")

        func = obj.get("function")
        if isinstance(func, dict) and func.get("name") == "":
            func.pop("name")

        for value in obj.values():
            drop_empty_response_names(value)

    elif isinstance(obj, list):
        for value in obj:
            drop_empty_response_names(value)

    return obj


AR_HOME = (
    os.environ.get("GROK_HOME")
    or os.environ.get("AGENTROUTER_GROK_HOME")
    or os.path.expanduser("~/.agentrouter-grok")
)
THINKING_CACHE_FILE = os.environ.get("AR_THINKING_CACHE") or os.path.join(AR_HOME, "thinking_cache.json")
THINKING_CACHE = {}

def load_thinking_cache():
    global THINKING_CACHE
    try:
        if os.path.exists(THINKING_CACHE_FILE):
            with open(THINKING_CACHE_FILE, "r", encoding="utf-8") as f:
                THINKING_CACHE = json.load(f)
    except Exception:
        THINKING_CACHE = {}

    try:
        sessions_base = os.path.join(AR_HOME, "sessions")
        if os.path.exists(sessions_base):
            for root_dir, _, files in os.walk(sessions_base):
                if "chat_history.jsonl" in files:
                    spath = os.path.join(root_dir, "chat_history.jsonl")
                    try:
                        with open(spath, "r", encoding="utf-8") as f:
                            last_thought = ""
                            for line in f:
                                try:
                                    d = json.loads(line)
                                    if d.get("type") == "reasoning":
                                        s = d.get("summary", [])
                                        if s and isinstance(s, list) and isinstance(s[0], dict):
                                            last_thought = s[0].get("text", "")
                                    elif d.get("type") == "assistant":
                                        for tc in d.get("tool_calls", []):
                                            tcid = tc.get("id")
                                            if tcid and tcid not in THINKING_CACHE:
                                                THINKING_CACHE[tcid] = {
                                                    "type": "thinking",
                                                    "thinking": last_thought or "analyzing tool calls...",
                                                    "signature": "sig_seed_" + tcid
                                                }
                                except Exception:
                                    pass
                    except Exception:
                        pass
    except Exception:
        pass


def save_thinking_cache(tool_ids, entry):
    global THINKING_CACHE
    if not tool_ids:
        return
    updated = False
    for tid in tool_ids:
        if tid:
            THINKING_CACHE[tid] = entry
            updated = True
    if updated:
        try:
            if len(THINKING_CACHE) > 2000:
                keys = list(THINKING_CACHE.keys())[:-2000]
                for k in keys:
                    THINKING_CACHE.pop(k, None)
            with open(THINKING_CACHE_FILE, "w") as f:
                json.dump(THINKING_CACHE, f)
        except Exception:
            pass


def ensure_thinking_passback(parsed):
    """Ensure all assistant messages containing tool_use have a thinking block in Anthropic format.
    Uses authentic cached thinking traces and cryptographic signatures whenever available.
    Prevents HTTP 400: The `content[].thinking` in the thinking mode must be passed back to the API.
    """
    if not isinstance(parsed, dict):
        return parsed
    messages = parsed.get("messages")
    if not isinstance(messages, list):
        return parsed

    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            content = m.get("content")
            if isinstance(content, list):
                has_thought = any(isinstance(c, dict) and c.get("type") == "thinking" for c in content)
                has_tool_use = any(isinstance(c, dict) and c.get("type") == "tool_use" for c in content)
                if not has_thought and has_tool_use:
                    cached_entry = None
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            tid = c.get("id")
                            if tid and tid in THINKING_CACHE:
                                cached_entry = THINKING_CACHE[tid]
                                break
                    if cached_entry:
                        thought_block = {
                            "type": "thinking",
                            "thinking": cached_entry.get("thinking", "analyzing tool calls and continuing..."),
                            "signature": cached_entry.get("signature", "sig_auto_restored")
                        }
                    else:
                        thought_block = {
                            "type": "thinking",
                            "thinking": "analyzing tool calls and continuing...",
                            "signature": "sig_auto_restored"
                        }
                    content.insert(0, thought_block)
    return parsed


def ensure_thinking_signature(obj):
    """Ensure thinking blocks have a non-empty signature field for strict Anthropic deserializers."""
    if isinstance(obj, dict):
        cb = obj.get("content_block")
        if isinstance(cb, dict) and cb.get("type") == "thinking":
            if not cb.get("signature"):
                cb["signature"] = "sig_dummy_agentrouter_thinking"
        delta = obj.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "thinking_delta":
            if "signature" not in delta:
                delta["signature"] = "sig_dummy_agentrouter_thinking"
    return obj


def sanitize_tool_messages(parsed):
    """Ensure tool_result blocks (Anthropic) and tool messages (OpenAI) strictly match
    tool calls declared in the immediately preceding assistant message.
    Prevents:
    - OpenAI 400: `Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`
    - Anthropic 400: `unexpected tool_use_id found in tool_result blocks... Each tool_result block must have a corresponding tool_use block in the previous message.`
    """
    if not isinstance(parsed, dict):
        return parsed
    messages = parsed.get("messages")
    if not isinstance(messages, list):
        return parsed

    new_messages = []
    prev_assistant_tool_ids = set()

    for m in messages:
        if not isinstance(m, dict):
            new_messages.append(m)
            continue

        role = m.get("role")

        if role == "assistant":
            prev_assistant_tool_ids = set()
            tool_calls = m.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict) and tc.get("id"):
                        prev_assistant_tool_ids.add(tc["id"])
            content = m.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("id"):
                        prev_assistant_tool_ids.add(c["id"])
            new_messages.append(m)

        elif role == "tool":
            tcid = m.get("tool_call_id")
            if tcid and tcid in prev_assistant_tool_ids:
                new_messages.append(m)
            else:
                print(f"[agentrouter-grok] warning: dropped orphan OpenAI tool message: tool_call_id={tcid}", flush=True)

        elif role == "user":
            content = m.get("content")
            if isinstance(content, list):
                filtered_content = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        tuid = c.get("tool_use_id")
                        if tuid and tuid in prev_assistant_tool_ids:
                            filtered_content.append(c)
                        else:
                            print(f"[agentrouter-grok] warning: dropped orphan Anthropic tool_result: tool_use_id={tuid}", flush=True)
                    else:
                        filtered_content.append(c)
                if filtered_content:
                    m["content"] = filtered_content
                    new_messages.append(m)
            else:
                new_messages.append(m)
        else:
            new_messages.append(m)

    parsed["messages"] = new_messages
    return parsed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # quiet

    def _build_upstream_request(self, method):
        url = UPSTREAM + self.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        # Sanitize outgoing tool JSON Schemas (strip `default: null`) that
        # stricter backends reject with "JSON Schema not supported".
        if body:
            try:
                parsed = json.loads(body)

                # Always sanitize empty-string names in the parsed body; this
                # ensures top-level arrays like `input` are also fixed.
                sanitize_empty_names(parsed)
                ensure_thinking_passback(parsed)
                sanitize_tool_messages(parsed)

                if isinstance(parsed, dict) and "tools" in parsed:
                    parsed["tools"] = sanitize_tool_schemas(parsed["tools"])

                    # Lightweight diagnostic: warn if any tool dict lacks a
                    # non-empty 'name' so problematic requests are easier to find.
                    tools = parsed.get("tools")
                    if isinstance(tools, (list, tuple)):
                        for idx, tool in enumerate(tools):
                            if isinstance(tool, dict):
                                name = tool.get("name")
                                if not name:
                                    print(
                                        f"[agentrouter-grok] warning: outgoing tool #{idx} has empty name: {tool}",
                                        flush=True,
                                    )

                body = json.dumps(parsed).encode()
            except Exception:
                pass  # non-JSON body, leave untouched
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in SKIP_REQ_HEADERS}
        headers["User-Agent"] = CLIENT_UA
        headers["originator"] = CLIENT_ORIGINATOR
        return urllib.request.Request(url, data=body, headers=headers, method=method)

    def _forward(self, method):
        req = self._build_upstream_request(method)
        try:
            resp = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            # Upstream error (e.g. transient 504 Gateway Time-out). Relay the
            # status/body so grok can surface/retry it, but never let a client
            # disconnect turn into an unhandled BrokenPipeError.
            try:
                raw = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        except Exception as e:
            try:
                msg = str(e).encode()
                self.send_response(502)
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        ctype = resp.headers.get("Content-Type", "")
        if "event-stream" in ctype:
            self._relay_stream(resp)
        else:
            self._relay_json(resp)

    def _relay_stream(self, resp):
        # Use a portable way to get the numeric status code from the response.
        status = getattr(resp, "status", None)
        if status is None:
            try:
                status = resp.getcode()
            except Exception:
                status = 200
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        stream_thinking_chunks = []
        stream_thinking_sig = None
        stream_tool_ids = []

        for line in resp:
            if line.startswith(b"data: "):
                payload = line[6:].strip()
                if b"billing" in payload:
                    continue  # drop billing.summary events
                if payload and payload != b"[DONE]":
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        pass  # non-JSON keepalive etc., pass through untouched
                    else:
                        # AgentRouter emits bare `data: null` and non-object
                        # chunks that crash grok's ChatCompletionChunk parser
                        # ("invalid type: null, expected struct"). Drop them.
                        if not isinstance(obj, dict):
                            continue
                        obj = strip_nulls(ensure_created(obj))
                        # Continuation deltas may omit the function name.
                        drop_empty_response_names(obj)
                        ensure_thinking_signature(obj)

                        # Capture thinking text, signatures, and tool IDs for the smart cache
                        cb = obj.get("content_block")
                        if isinstance(cb, dict):
                            if cb.get("type") == "thinking":
                                if cb.get("thinking"):
                                    stream_thinking_chunks.append(cb["thinking"])
                                if cb.get("signature"):
                                    stream_thinking_sig = cb["signature"]
                            elif cb.get("type") == "tool_use" and cb.get("id"):
                                stream_tool_ids.append(cb["id"])

                        delta = obj.get("delta")
                        if isinstance(delta, dict):
                            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                                stream_thinking_chunks.append(delta["thinking"])
                            elif delta.get("type") == "signature_delta" and delta.get("signature"):
                                stream_thinking_sig = delta["signature"]
                            elif delta.get("reasoning_content"):
                                stream_thinking_chunks.append(delta["reasoning_content"])
                            tcalls = delta.get("tool_calls")
                            if isinstance(tcalls, list):
                                for tc in tcalls:
                                    if isinstance(tc, dict) and tc.get("id"):
                                        stream_tool_ids.append(tc["id"])

                        # Use CRLF as per SSE spec when constructing lines.
                        line = b"data: " + json.dumps(obj).encode() + b"\r\n"
            elif b"billing" in line:
                continue
            try:
                self.wfile.write(line)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break

        # Save to thinking cache when stream ends
        full_thought = "".join(stream_thinking_chunks)
        if stream_tool_ids and (full_thought or stream_thinking_sig):
            entry = {
                "type": "thinking",
                "thinking": full_thought or "analyzing tool calls and continuing...",
                "signature": stream_thinking_sig or "sig_auto_cached"
            }
            save_thinking_cache(stream_tool_ids, entry)

    def _relay_json(self, resp):
        raw = resp.read()
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                ensure_created(data)
                data.pop("billing", None)
                data = strip_nulls(data)
                drop_empty_response_names(data)
                ensure_thinking_signature(data)

                # Capture thinking from JSON content
                content = data.get("content")
                if isinstance(content, list):
                    entry = None
                    tool_ids = []
                    for c in content:
                        if isinstance(c, dict):
                            if c.get("type") == "thinking":
                                entry = {
                                    "type": "thinking",
                                    "thinking": c.get("thinking", ""),
                                    "signature": c.get("signature", "sig_auto_cached")
                                }
                            elif c.get("type") == "tool_use" and c.get("id"):
                                tool_ids.append(c["id"])
                    if entry and tool_ids:
                        save_thinking_cache(tool_ids, entry)

                raw = json.dumps(data).encode()
        except Exception:
            pass  # non-JSON, pass through untouched
        # Use portable status retrieval.
        status = getattr(resp, "status", None)
        if status is None:
            try:
                status = resp.getcode()
            except Exception:
                status = 200
        self.send_response(status)
        for k, v in resp.headers.items():
            if k.lower() not in SKIP_RESP_HEADERS:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        self._forward("GET")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    load_thinking_cache()
    print(f"[agentrouter-grok] proxy: {HOST}:{PORT} -> {UPSTREAM} (cache: {len(THINKING_CACHE)} items loaded)", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
