#!/usr/bin/env python3
"""
AgentRouter <-> Grok CLI compatibility proxy (fixed).

This file is a patched copy of the original proxy.py with the following fixes:

- Safer null stripping: drops null elements from lists and avoids removing
  structural keys like 'name' which must be present (even if null) so we don't
  accidentally turn a null into a missing required field.
- Uses resp.getcode() (with fallback) instead of assuming resp.status exists.
- Adds a lightweight diagnostic log when outgoing tool definitions lack a
  non-empty name, so problematic requests are easier to find.
- Uses CRLF when rewriting SSE data lines.
- Sanitizes empty-string tool/function names by replacing them with
  '__unnamed_tool' to avoid upstream validation errors.
- Runs the empty-name sanitizer on any parsed JSON body (not only when
  "tools" is present) so top-level arrays like `input` are fixed too.

Save as proxyfixed.py for review.
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
                        # Sanitize empty-string names in streamed chunks too.
                        sanitize_empty_names(obj)
                        # Use CRLF as per SSE spec when constructing lines.
                        line = b"data: " + json.dumps(obj).encode() + b"\r\n"
            elif b"billing" in line:
                continue
            try:
                self.wfile.write(line)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break

    def _relay_json(self, resp):
        raw = resp.read()
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                ensure_created(data)
                data.pop("billing", None)
                data = strip_nulls(data)
                # Sanitize empty-string names in non-streamed JSON responses.
                sanitize_empty_names(data)
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
    print(f"[agentrouter-grok] proxy: {HOST}:{PORT} -> {UPSTREAM}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
