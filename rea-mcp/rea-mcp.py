#!/usr/bin/env python3
"""Start REA's MCP server on the VM only when a tool is used.

Agents start their MCP servers with every session. Running REA directly
(`ssh agent-vm rea mcp`) would boot the VM and keep it awake for as long
as the session stays open, even if REA is never used. This stdio proxy
answers the MCP handshake and the tool and prompt lists from a saved
listing, and opens the SSH connection on the first request that needs
REA itself (a tool call, a prompt, a completion). After REA has been idle
for a while (REA_MCP_IDLE_MINUTES, default 30; 0 keeps it open) the proxy
closes the connection, so the VM can shut down; the next call reconnects.
Closing it ends REA's open analysis sessions.

Listings: the cache that the last live connection wrote
(~/.cache/rea-mcp/listing.json) is preferred, then the snapshot shipped
in the image (rea-listing.json.gz next to this script, or
REA_MCP_SNAPSHOT). Every live connection compares REA's lists with the
ones served and, when they differ, updates the cache and tells the client
that the lists changed.

  rea-mcp [-- UPSTREAM COMMAND...]
  rea-mcp --snapshot FILE [-- UPSTREAM COMMAND...]   (writes a listing)
"""

import gzip
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

DEFAULT_UPSTREAM = [
    "ssh", "-T", "-F", "/home/node/.ssh/agent-vm.conf", "agent-vm",
    "/usr/local/bin/rea", "mcp",
]
HERE = os.path.dirname(os.path.realpath(__file__))
SNAPSHOT = os.environ.get("REA_MCP_SNAPSHOT") or os.path.join(HERE, "rea-listing.json.gz")
CACHE = os.environ.get("REA_MCP_CACHE") or os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "rea-mcp", "listing.json")
# Methods answered from the listing. REA has no resources, so those get
# the same "method not found" REA would send.
LISTED = {"tools/list": "tools", "prompts/list": "prompts"}
NO_RESOURCES = {"resources/list", "resources/templates/list", "resources/read",
                "resources/subscribe", "resources/unsubscribe"}
INTERNAL = "rea-mcp:"  # prefix of the proxy's own request ids
# MCP 2026-07-28 drops initialize: clients probe with server/discover and
# send the protocol version, client info and capabilities in every
# request's _meta. List changes reach clients through subscriptions/listen.
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER = "io.modelcontextprotocol/serverInfo"
META_SUBSCRIPTION = "io.modelcontextprotocol/subscriptionId"
LIST_NOTIFICATIONS = {"toolsListChanged": ("tools", "notifications/tools/list_changed"),
                      "promptsListChanged": ("prompts", "notifications/prompts/list_changed")}


def log(msg):
    print(f"rea-mcp: {msg}", file=sys.stderr, flush=True)


def load_listing():
    for path in (CACHE, SNAPSHOT):
        try:
            opener = gzip.open if path.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("initialize"), dict) and isinstance(data.get("tools"), list):
                data.setdefault("prompts", [])
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log(f"ignoring {path}: {e}")
    return None


def save_listing(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    text = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    if path.endswith(".gz"):
        text = gzip.compress(text, mtime=0)  # reproducible snapshot
    with open(tmp, "wb") as f:
        f.write(text)
    os.replace(tmp, path)


class Upstream:
    """One SSH connection to `rea mcp`; its messages go to the event queue."""

    def __init__(self, cmd, events):
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.events = events
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log(f"REA sent a line that isn't JSON: {line[:200]!r}")
                continue
            self.events.put(("upstream", self, msg))
        self.proc.wait()
        self.events.put(("upstream-exit", self, self.proc.returncode))

    def send(self, msg):
        try:
            self.proc.stdin.write(json.dumps(msg, separators=(",", ":")).encode() + b"\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()


class Proxy:
    def __init__(self, upstream_cmd, idle_seconds, out=sys.stdout.buffer):
        self.cmd = upstream_cmd
        self.idle = idle_seconds
        self.out = out
        self.events = queue.Queue()
        self.listing = load_listing()
        self.client_init = None      # the client's initialize params
        self.client_initialized = False
        self.up = None               # connected Upstream
        self.ready = False           # upstream handshake done
        self.waiting = []            # client messages held until ready
        self.pending = set()         # client request ids REA hasn't answered
        self.last_activity = time.monotonic()
        self.seq = 0
        self.fresh = {}              # listing being collected from REA
        self.meta = None             # _meta of the client's last 2026-07-28 request
        self.listens = {}            # open subscriptions/listen: id -> notifications

    # -- output to the client
    def write(self, msg):
        self.out.write(json.dumps(msg, separators=(",", ":")).encode() + b"\n")
        self.out.flush()

    def reply(self, rid, result):
        self.write({"jsonrpc": "2.0", "id": rid, "result": result})

    def error(self, rid, code, message):
        self.write({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    # -- upstream lifecycle
    def internal(self, name):
        self.seq += 1
        return f"{INTERNAL}{name}:{self.seq}"

    def internal_params(self):
        return {"_meta": self.meta} if self.meta else {}

    def server_info(self):
        listing = self.listing or {}
        discover = listing.get("discover") or {}
        return ((discover.get("_meta") or {}).get(META_SERVER)
                or (listing.get("initialize") or {}).get("serverInfo"))

    def capabilities(self):
        listing = self.fresh if self.up and self.fresh else self.listing or {}
        source = listing.get("discover") or listing.get("initialize") or {}
        return source.get("capabilities") or {}

    def connect(self):
        log("starting REA on the VM")
        self.up = Upstream(self.cmd, self.events)
        self.ready = False
        self.fresh = {}
        if self.meta and self.client_init is None:
            # 2026-07-28: no handshake; every request carries its _meta.
            self.ready = True
            self.up.send({"jsonrpc": "2.0", "id": self.internal("discover"),
                          "method": "server/discover", "params": self.internal_params()})
            waiting, self.waiting = self.waiting, []
            for m in waiting:
                self.from_client(m)
            return
        params = self.client_init or {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "rea-mcp", "version": "1"}}
        self.up.send({"jsonrpc": "2.0", "id": self.internal("initialize"),
                      "method": "initialize", "params": params})

    def disconnect(self, reason):
        if self.up:
            log(f"closing the REA connection ({reason})")
            up, self.up = self.up, None
            self.ready = False
            up.close()

    def fail_pending(self, message):
        for rid in list(self.pending):
            self.error(rid, -32603, message)
        self.pending.clear()
        for msg in self.waiting:
            if "id" in msg and "method" in msg:
                self.error(msg["id"], -32603, message)
        self.waiting = []

    def forward(self, msg):
        if "id" in msg and "method" in msg:
            self.pending.add(msg["id"])
        if not self.up.send(msg):
            self.disconnect("write failed")
            self.fail_pending("The connection to REA on the VM was lost.")

    # -- messages from the client
    def from_client(self, msg):
        self.last_activity = time.monotonic()
        method, rid = msg.get("method"), msg.get("id")
        if method is None:          # a response to a request from REA
            if self.up and self.ready:
                self.forward(msg)
            return
        if method == "initialize":
            self.client_init = msg.get("params") or {}
            if self.listing:
                result = dict(self.listing["initialize"])
                wanted = self.client_init.get("protocolVersion")
                if wanted:
                    result["protocolVersion"] = wanted
                self.reply(rid, result)
            else:
                # No listing yet: this first session has to ask REA itself.
                self.waiting.append(msg)
                self.connect()
            return
        if method == "notifications/initialized":
            self.client_initialized = True
            return
        meta = (msg.get("params") or {}).get("_meta") or {}
        modern = META_VERSION in meta
        if modern:
            self.meta = meta
        if method == "server/discover" and rid is not None:
            if self.listing and self.listing.get("discover"):
                self.reply(rid, self.listing["discover"])
                return
        elif method == "subscriptions/listen" and rid is not None:
            # Answered here: the proxy sends list changes itself.
            caps = self.capabilities()
            wanted = (msg.get("params") or {}).get("notifications") or {}
            granted = {k: True for k, v in wanted.items()
                       if v and k in LIST_NOTIFICATIONS
                       and (caps.get(LIST_NOTIFICATIONS[k][0]) or {}).get("listChanged")}
            self.listens[rid] = granted
            self.write({"jsonrpc": "2.0", "method": "notifications/subscriptions/acknowledged",
                        "params": {"notifications": granted, "_meta": {META_SUBSCRIPTION: rid}}})
            return
        elif method == "notifications/cancelled":
            if (msg.get("params") or {}).get("requestId") in self.listens:
                del self.listens[msg["params"]["requestId"]]
                return
        if method == "ping" and rid is not None:
            self.reply(rid, {})
            return
        if self.listing and method in LISTED and rid is not None:
            key = LISTED[method]
            result = {key: self.listing[key]}
            if modern:
                result.update(resultType="complete", ttlMs=0, cacheScope="private")
                if self.server_info():
                    result["_meta"] = {META_SERVER: self.server_info()}
            self.reply(rid, result)
            return
        if self.listing and method in NO_RESOURCES and rid is not None:
            self.error(rid, -32601, "Method not found")
            return
        if rid is None and not self.up:
            return                  # notifications (cancelled, roots) need no REA
        if self.up and self.ready:
            self.forward(msg)
            return
        self.waiting.append(msg)
        if not self.up:
            self.connect()

    # -- messages from REA
    def from_upstream(self, up, msg):
        if up is not self.up:
            return
        self.last_activity = time.monotonic()
        rid = msg.get("id")
        if isinstance(rid, str) and rid.startswith(INTERNAL):
            self.internal_reply(rid, msg)
            return
        if "method" not in msg and rid is not None:
            self.pending.discard(rid)
        self.write(msg)

    def internal_reply(self, rid, msg):
        kind = rid[len(INTERNAL):].split(":")[0]
        if "error" in msg:
            log(f"REA refused {kind}: {msg['error']}")
            if kind == "initialize":
                for m in self.waiting:
                    if m.get("method") == "initialize":
                        self.write({"jsonrpc": "2.0", "id": m["id"], "error": msg["error"]})
                self.waiting = [m for m in self.waiting if m.get("method") != "initialize"]
                self.disconnect("initialize failed")
                self.fail_pending("REA on the VM failed to start.")
            return
        result = msg.get("result") or {}
        if kind == "discover":
            self.fresh["discover"] = result
            init = dict((self.listing or {}).get("initialize") or {})
            init.update(capabilities=result.get("capabilities") or {},
                        serverInfo=(result.get("_meta") or {}).get(META_SERVER)
                        or init.get("serverInfo"))
            init.setdefault("protocolVersion", "2025-06-18")
            if "instructions" in result:
                init["instructions"] = result["instructions"]
            self.fresh["initialize"] = init
            self.up.send({"jsonrpc": "2.0", "id": self.internal("tools"),
                          "method": "tools/list", "params": self.internal_params()})
        elif kind == "initialize":
            self.up.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.ready = True
            self.fresh = {"initialize": result}
            if (self.listing or {}).get("discover"):
                self.fresh["discover"] = self.listing["discover"]
            self.up.send({"jsonrpc": "2.0", "id": self.internal("tools"), "method": "tools/list"})
            waiting, self.waiting = self.waiting, []
            for m in waiting:
                if m.get("method") == "initialize":
                    self.reply(m["id"], result)
                else:
                    self.from_client(m)
        elif kind == "tools":
            self.fresh["tools"] = result.get("tools", [])
            if result.get("nextCursor"):
                self.fresh["tools_cursor"] = result["nextCursor"]
            if "prompts" in self.capabilities():
                self.up.send({"jsonrpc": "2.0", "id": self.internal("prompts"),
                              "method": "prompts/list", "params": self.internal_params()})
            else:
                self.fresh["prompts"] = []
                self.update_listing()
        elif kind == "prompts":
            self.fresh["prompts"] = result.get("prompts", [])
            self.update_listing()

    def update_listing(self):
        fresh = {k: self.fresh[k] for k in ("initialize", "discover", "tools", "prompts")
                 if k in self.fresh}
        if self.fresh.get("tools_cursor"):
            log("REA paginates its tool list; not caching it")
            return
        old = self.listing
        self.listing = fresh
        if old == fresh:
            return
        try:
            save_listing(CACHE, fresh)
        except OSError as e:
            log(f"can't write {CACHE}: {e}")
        if not old:
            return
        for flag, (key, method) in LIST_NOTIFICATIONS.items():
            if old[key] == fresh[key]:
                continue
            if self.client_initialized:
                self.write({"jsonrpc": "2.0", "method": method})
            for lid, granted in self.listens.items():
                if granted.get(flag):
                    self.write({"jsonrpc": "2.0", "method": method,
                                "params": {"_meta": {META_SUBSCRIPTION: lid}}})

    def upstream_exit(self, up, code):
        if up is not self.up:
            return
        log(f"REA connection ended (exit status {code})")
        self.up, self.ready = None, False
        self.fail_pending("REA on the VM exited; the next request reconnects.")

    # -- main loop
    def run(self, stdin=sys.stdin.buffer):
        def read_client():
            for line in stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.events.put(("client", None, json.loads(line)))
                except ValueError:
                    self.events.put(("client-bad", None, line))
            self.events.put(("client-eof", None, None))
        threading.Thread(target=read_client, daemon=True).start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.events.put(("client-eof", None, None)))

        while True:
            try:
                kind, src, data = self.events.get(timeout=30)
            except queue.Empty:
                kind = None
            if kind == "client":
                if isinstance(data, list):
                    for m in data:
                        self.from_client(m)
                else:
                    self.from_client(data)
            elif kind == "client-bad":
                self.error(None, -32700, "Parse error")
            elif kind == "client-eof":
                self.disconnect("client closed")
                return
            elif kind == "upstream":
                self.from_upstream(src, data)
            elif kind == "upstream-exit":
                self.upstream_exit(src, data)
            if (self.up and self.idle and not self.pending and not self.waiting
                    and time.monotonic() - self.last_activity > self.idle):
                self.disconnect(f"idle for {self.idle // 60} minutes")


def snapshot(path, cmd):
    """Ask REA for its listing and write it (the image's snapshot)."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def call(rid, method, params=None):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                     "params": params or {}}).encode() + b"\n")
        proc.stdin.flush()
        for line in proc.stdout:
            msg = json.loads(line)
            if msg.get("id") == rid:
                if "error" in msg:
                    sys.exit(f"rea-mcp: {method} failed: {msg['error']}")
                return msg["result"]
        sys.exit(f"rea-mcp: REA exited during {method}")

    init = call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "rea-mcp", "version": "1"}})
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    tools = call(2, "tools/list")
    if tools.get("nextCursor"):
        sys.exit("rea-mcp: REA paginates its tool list, which this proxy doesn't support")
    prompts = call(3, "prompts/list")["prompts"] if "prompts" in init.get("capabilities", {}) else []
    proc.stdin.close()
    proc.wait()
    listing = {"initialize": init, "tools": tools["tools"], "prompts": prompts}
    # The 2026-07-28 handshake, if REA speaks it, from a fresh connection.
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    meta = {META_VERSION: "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "rea-mcp", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    try:
        listing["discover"] = call(4, "server/discover", {"_meta": meta})
    except SystemExit as e:
        print(f"rea-mcp: no server/discover ({e}); legacy handshake only", file=sys.stderr)
    proc.stdin.close()
    proc.wait()
    save_listing(path, listing)
    print(f"{path}: REA {init.get('serverInfo', {}).get('version')}, "
          f"{len(tools['tools'])} tools, {len(prompts)} prompts")


def main(argv):
    snapshot_path = None
    if argv[:1] == ["--snapshot"] and len(argv) >= 2:
        snapshot_path, argv = argv[1], argv[2:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    elif argv:
        sys.exit(__doc__)
    cmd = argv or DEFAULT_UPSTREAM
    if snapshot_path:
        snapshot(snapshot_path, cmd)
        return
    try:
        idle = int(float(os.environ.get("REA_MCP_IDLE_MINUTES", "30")) * 60)
    except ValueError:
        idle = 30 * 60
    Proxy(cmd, max(idle, 0)).run()


if __name__ == "__main__":
    main(sys.argv[1:])
