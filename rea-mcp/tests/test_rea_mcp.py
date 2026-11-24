"""Tests for rea-mcp.py against a mock REA (no VM, no SSH)."""
import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "..", "rea-mcp.py")
SNAPSHOT = os.path.join(HERE, "..", "rea-listing.json.gz")
MOCK = [sys.executable, "-I", os.path.join(HERE, "mock_rea.py")]


class Session:
    def __init__(self, tmp, snapshot=SNAPSHOT, idle_minutes="30", tools="open_binary"):
        self.log = os.path.join(tmp, "starts")
        env = dict(os.environ, MOCK_REA_LOG=self.log, MOCK_REA_TOOLS=tools,
                   REA_MCP_SNAPSHOT=snapshot, REA_MCP_CACHE=os.path.join(tmp, "cache.json"),
                   REA_MCP_IDLE_MINUTES=idle_minutes)
        self.proc = subprocess.Popen([sys.executable, "-I", PROXY, "--", *MOCK], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        self.next_id = 0

    def starts(self):
        try:
            with open(self.log) as f:
                return len(f.readlines())
        except FileNotFoundError:
            return 0

    def send(self, msg):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        self.proc.stdin.flush()

    def read(self):
        return json.loads(self.proc.stdout.readline())

    def request(self, method, params=None):
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params or {}})
        msg = self.read()
        while msg.get("id") != self.next_id:
            self.notes.append(msg)
            msg = self.read()
        return msg

    def start(self):
        self.notes = []
        init = self.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                           "clientInfo": {"name": "t", "version": "0"}})
        self.send({"method": "notifications/initialized"})
        return init

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=20)


class ReaMcpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_listing_without_connecting(self):
        s = Session(self.tmp.name)
        init = s.start()["result"]
        self.assertEqual(init["protocolVersion"], "2025-03-26")
        self.assertEqual(init["serverInfo"]["name"], "rea")
        self.assertEqual(len(s.request("tools/list")["result"]["tools"]), 170)
        self.assertEqual(len(s.request("prompts/list")["result"]["prompts"]), 6)
        self.assertEqual(s.request("resources/list")["error"]["code"], -32601)
        self.assertEqual(s.request("ping")["result"], {})
        s.send({"method": "notifications/cancelled", "params": {"requestId": 9}})
        s.close()
        self.assertEqual(s.starts(), 0)

    def test_call_connects_and_refreshes_listing(self):
        s = Session(self.tmp.name)
        s.start()
        reply = s.request("tools/call", {"name": "open_binary", "arguments": {}})
        self.assertEqual(reply["result"]["content"][0]["text"], "called open_binary")
        self.assertEqual(s.starts(), 1)
        # The mock's list differs from the snapshot: the client hears about it.
        reply = s.request("tools/call", {"name": "x", "arguments": {}})
        methods = [n.get("method") for n in s.notes]
        self.assertIn("notifications/tools/list_changed", methods)
        self.assertEqual([t["name"] for t in s.request("tools/list")["result"]["tools"]],
                         ["open_binary"])
        s.close()
        self.assertEqual(s.starts(), 1)
        # The next session serves the cached list, still without connecting.
        s2 = Session(self.tmp.name)
        s2.start()
        self.assertEqual(len(s2.request("tools/list")["result"]["tools"]), 1)
        s2.close()
        self.assertEqual(s2.starts(), 1)

    def test_idle_disconnect_and_reconnect(self):
        s = Session(self.tmp.name, idle_minutes="0.02")  # 1.2 s
        s.start()
        s.request("tools/call", {"name": "open_binary", "arguments": {}})
        time.sleep(32)  # the proxy checks idleness at least every 30 s
        reply = s.request("tools/call", {"name": "open_binary", "arguments": {}})
        self.assertIn("result", reply)
        self.assertEqual(s.starts(), 2)
        s.close()

    def test_modern_protocol(self):
        # MCP 2026-07-28: no initialize; _meta on every request.
        s = Session(self.tmp.name)
        s.notes = []
        meta = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                          "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"},
                          "io.modelcontextprotocol/clientCapabilities": {}}}
        found = s.request("server/discover", meta)["result"]
        self.assertEqual(found["supportedVersions"], ["2026-07-28"])
        s.send({"id": "listen:0", "method": "subscriptions/listen",
                "params": {**meta, "notifications": {"toolsListChanged": True}}})
        ack = s.read()
        self.assertEqual(ack["method"], "notifications/subscriptions/acknowledged")
        self.assertEqual(ack["params"]["notifications"], {"toolsListChanged": True})
        tools = s.request("tools/list", meta)["result"]
        self.assertEqual(len(tools["tools"]), 170)
        self.assertEqual(tools["resultType"], "complete")
        self.assertEqual(s.starts(), 0)
        reply = s.request("tools/call", {**meta, "name": "open_binary", "arguments": {}})
        self.assertEqual(reply["result"]["content"][0]["text"], "called open_binary")
        s.request("tools/call", {**meta, "name": "x", "arguments": {}})
        changed = [n for n in s.notes if n.get("method") == "notifications/tools/list_changed"]
        self.assertEqual(changed[0]["params"]["_meta"],
                         {"io.modelcontextprotocol/subscriptionId": "listen:0"})
        self.assertEqual(s.request("server/discover", meta)["result"]["_meta"]
                         ["io.modelcontextprotocol/serverInfo"]["version"], "mock")
        s.send({"method": "notifications/cancelled", "params": {"requestId": "listen:0"}})
        s.close()
        self.assertEqual(s.starts(), 1)

    def test_without_listing_asks_rea(self):
        s = Session(self.tmp.name, snapshot=os.path.join(self.tmp.name, "none.json.gz"))
        init = s.start()["result"]
        self.assertEqual(init["serverInfo"]["version"], "mock")
        self.assertEqual(s.request("tools/list")["result"]["tools"][0]["name"], "open_binary")
        s.close()
        self.assertEqual(s.starts(), 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "cache.json")))

    def test_snapshot_mode(self):
        out = os.path.join(self.tmp.name, "snap.json.gz")
        env = dict(os.environ, MOCK_REA_LOG=os.path.join(self.tmp.name, "starts"))
        subprocess.run([sys.executable, "-I", PROXY, "--snapshot", out, "--", *MOCK],
                       env=env, check=True, capture_output=True)
        with gzip.open(out, "rt") as f:
            data = json.load(f)
        self.assertEqual(data["tools"][0]["name"], "open_binary")
        self.assertEqual(data["prompts"], [{"name": "trace_crash"}])


if __name__ == "__main__":
    unittest.main()
