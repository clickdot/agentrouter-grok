#!/usr/bin/env python3
"""
AgentRouter <-> Grok CLI compatibility proxy.

Grok's CLI talks a strict OpenAI chat-completions dialect. AgentRouter (built on
One API) diverges in two ways that break Grok out of the box:

  1. Client gating: requests without the codex_cli_rs client identity are
     rejected with `401 unauthorized client detected`. This proxy injects the
     required `User-Agent` and `originator` headers on every upstream request.

  2. Missing `created` field: AgentRouter omits the `created` timestamp from
     both non-streaming responses AND every streaming `chat.completion.chunk`.
     Grok's deserializer treats `created` as mandatory and errors with
     `missing field 'created'`. This proxy injects it where absent.

It also drops the trailing `billing.summary` SSE events that strict parsers choke on.

Usage:
    python3 proxy.py                      # listens on 127.0.0.1:8788
    AR_PROXY_PORT=9000 python3 proxy.py   # custom port
    AR_UPSTREAM=https://agentrouter.org python3 proxy.py

Point Grok's config `base_url` at http://127.0.0.1:<port>/v1
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


def strip_nulls(obj):
    """Recursively remove keys/elements whose value is null.

    AgentRouter emits null where grok's strict deserializer expects a number or
    struct — e.g. the final usage chunk carries `input_tokens_details: null`,
    crashing grok with `invalid type: null, expected u32`. Also covers
    `system_fingerprint: null`, `logprobs: null`, etc. grok tolerates absent
    optional fields, so dropping the null ones is safe.
    """
    if isinstance(obj, dict):
        return {k: strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [strip_nulls(v) for v in obj]
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
                if isinstance(parsed, dict) and "tools" in parsed:
                    parsed["tools"] = sanitize_tool_schemas(parsed["tools"])
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
        self.send_response(resp.status)
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
                        line = b"data: " + json.dumps(obj).encode() + b"\n"
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
                raw = json.dumps(data).encode()
        except Exception:
            pass  # non-JSON, pass through untouched
        self.send_response(resp.status)
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
