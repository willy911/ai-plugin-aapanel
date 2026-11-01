# -*- coding: utf-8 -*-
"""Test AI Chat Agent aaPanel — stdlib unittest saja (tanpa pytest).

Semua upstream memakai server HTTP palsu lokal; tidak ada key asli,
tidak ada jaringan keluar. Jalankan: python3 -m unittest discover -s tests -v
"""
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLUGIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ai_chat_agent"))
sys.path.insert(0, PLUGIN_DIR)


class FakeUpstream(BaseHTTPRequestHandler):
    queue = []  # list of raw response bodies (str) untuk /chat/completions
    models_body = json.dumps({"data": [{"id": "b-model"}, {"id": "a-model"}]})
    last_user_agent = ""
    last_path = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeUpstream.last_user_agent = self.headers.get("User-Agent") or ""
        FakeUpstream.last_path = self.path
        if self.path.endswith("/models"):
            body = self.models_body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        FakeUpstream.last_user_agent = self.headers.get("User-Agent") or ""
        FakeUpstream.last_path = self.path
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.endswith("/chat/completions"):
            body = (self.queue.pop(0) if self.queue else
                    json.dumps({"choices": [{"message": {"role": "assistant", "content": "selesai"}}]}))
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self.send_response(404)
            self.end_headers()


def msg(content):
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})


def tool_call_msg(name, args, call_id="call_1"):
    return json.dumps({"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}]}}]})


class G:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "wwwroot")
        os.makedirs(self.root)
        self.panel = os.path.join(self.tmp, "panel")
        os.makedirs(os.path.join(self.panel, "data"))
        os.environ["AI_CHAT_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["AI_CHAT_AGENT_ROOT"] = self.root
        os.environ["AI_CHAT_PANEL_DIR"] = self.panel
        import ai_core
        importlib.reload(ai_core)
        self.core = ai_core
        import ai_chat_agent_main
        importlib.reload(ai_chat_agent_main)
        self.main = ai_chat_agent_main.ai_chat_agent_main()
        self.cfg = {"endpoint": "http://127.0.0.1:%d" % self.port,
                    "model": "fake-model", "api_key": "fake-key"}
        self.core.save_config(dict(self.cfg))
        FakeUpstream.queue = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_run(self, run_id, statuses=("done", "error", "stopped"), timeout=15):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.core.read_run(run_id)
            if last and last.get("status") in statuses:
                return last
            time.sleep(0.2)
        self.fail("run %s tidak mencapai %s, terakhir: %s" % (run_id, statuses, last))

    def wait_status(self, run_id, status, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = self.core.read_run(run_id)
            if run and run.get("status") == status:
                return run
            time.sleep(0.2)
        self.fail("run %s tidak mencapai status %s" % (run_id, status))


class TestCore(Base):
    def test_normalize_base(self):
        self.assertEqual(self.core.normalize_base("https://ai.skyzo.biz.id"),
                         "https://ai.skyzo.biz.id/v1")
        self.assertEqual(self.core.normalize_base("https://x.example/v1/"),
                         "https://x.example/v1")
        with self.assertRaises(ValueError):
            self.core.normalize_base("ftp://x.example")

    def test_parse_upstream_variants(self):
        p = self.core.parse_upstream_message
        self.assertEqual(p(msg("halo"))["content"], "halo")
        wrapped = json.dumps({"data": json.loads(msg("bungkus"))})
        self.assertEqual(p(wrapped)["content"], "bungkus")
        # JSON + trailer SSE ala 9Router
        self.assertEqual(p(msg("trailer") + "\ndata: [DONE]")["content"], "trailer")
        # SSE delta dengan tool_call terpecah
        sse = "\n".join([
            'data: {"choices":[{"delta":{"content":""}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"list","arguments":"{\\"path\\":"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"x\\""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]}}]}',
            'data: [DONE]'])
        # nama tool sengaja "list" agar mudah dicek perakitan fragmennya
        m = p(sse)
        self.assertEqual(m["tool_calls"][0]["function"]["name"], "list")
        self.assertEqual(m["tool_calls"][0]["function"]["arguments"], '{"path":"x"}')

    def test_safe_path_blocks_traversal(self):
        with self.assertRaises(ValueError):
            self.core.safe_path(self.root, "../etc/passwd")
        with self.assertRaises(ValueError):
            self.core.safe_path(self.root, "/etc/passwd")
        inside = os.path.join(self.root, "site1")
        self.assertEqual(self.core.safe_path(self.root, "site1"), inside)

    def test_tools_file_roundtrip_and_confine(self):
        r = self.core.exec_tool("write_file", {"path": "a/b.txt", "content": "isi"}, self.root)
        self.assertIn("OK", r)
        self.assertEqual(self.core.exec_tool("read_file", {"path": "a/b.txt"}, self.root), "isi")
        names = json.loads(self.core.exec_tool("list_dir", {"path": "a"}, self.root))
        self.assertEqual([n["name"] for n in names], ["b.txt"])
        bad = self.core.exec_tool("write_file", {"path": "../evil.txt", "content": "x"}, self.root)
        self.assertIn("gagal", bad)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))
        out = self.core.exec_tool("exec", {"command": "echo HELLO-EXEC"}, self.root)
        self.assertIn("HELLO-EXEC", out)

    def test_list_sites_from_fake_panel_db(self):
        db = os.path.join(self.panel, "data", "default.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE sites (id INTEGER, name TEXT, path TEXT, status TEXT)")
        con.execute("CREATE TABLE domain (id INTEGER, pid INTEGER, name TEXT)")
        con.execute("INSERT INTO sites VALUES (1, 'flazpedia.com', '/www/wwwroot/flazpedia.com', '1')")
        con.execute("INSERT INTO domain VALUES (1, 1, 'flazpedia.com')")
        con.commit()
        con.close()
        sites = self.core.list_sites_data(self.panel)
        self.assertEqual(sites[0]["name"], "flazpedia.com")
        self.assertEqual(sites[0]["domains"], ["flazpedia.com"])

    def test_config_never_returns_key(self):
        pub = self.core.public_config()
        self.assertNotIn("api_key", pub)
        self.assertTrue(pub["has_key"])

    def test_fetch_models_sorted(self):
        models = self.core.fetch_models("http://127.0.0.1:%d" % self.port, "fake-key")
        self.assertEqual(models, ["a-model", "b-model"])

    def test_dispatcher_chat(self):
        FakeUpstream.queue = [msg("balasan chat")]
        r = self.main.chat(G(messages=json.dumps([{"role": "user", "content": "halo"}])))
        self.assertTrue(r["status"])
        self.assertEqual(r["data"]["reply"], "balasan chat")

    def test_http_sends_plugin_user_agent_not_python_urllib(self):
        # Regresi insiden 1010: UA default Python-urllib diblokir Cloudflare BIC.
        self.core.fetch_models("http://127.0.0.1:%d" % self.port, "fake-key")
        self.assertNotIn("Python-urllib", FakeUpstream.last_user_agent)
        self.assertEqual(FakeUpstream.last_user_agent, "AI-Chat-Agent-aaPanel/1.1")

    def test_cloudflare_1010_explained_not_key_error(self):
        with self.assertRaises(ValueError) as ctx:
            self.core._check_cloudflare_block(403, "error code: 1010", "Chat")
        self.assertIn("BUKAN salah API key", str(ctx.exception))
        # Status lain / tanpa 1010 tidak diubah pesannya.
        self.core._check_cloudflare_block(403, "forbidden", "Chat")

    def test_chat_sessions_crud_via_dispatcher(self):
        msgs = [{"role": "user", "content": "halo dunia, ini session pertama"},
                {"role": "assistant", "content": "hai"}]
        r = self.main.save_chat_session(G(session_id="", messages=json.dumps(msgs), title=""))
        self.assertTrue(r["status"], r)
        sid = r["data"]["session"]["id"]
        self.assertEqual(r["data"]["session"]["title"], "halo dunia, ini session pertama")
        lst = self.main.list_chat_sessions(G())
        self.assertEqual([s["id"] for s in lst["data"]["sessions"]], [sid])
        got = self.main.get_chat_session(G(session_id=sid))
        self.assertEqual(got["data"]["session"]["messages"], msgs)
        self.assertEqual(self.main.delete_chat_session(G(session_id=sid))["status"], True)
        self.assertEqual(self.main.list_chat_sessions(G())["data"]["sessions"], [])
        # File session chmod 0600, seperti config.
        import stat
        self.assertEqual(stat.S_IMODE(os.stat(self.core.CHAT_SESSIONS_FILE).st_mode), 0o600)

    def test_rename_chat_session_and_agent_run(self):
        msgs = [{"role": "user", "content": "pesan pertama yang panjang sekali untuk judul"}]
        r = self.main.save_chat_session(G(session_id="", messages=json.dumps(msgs), title=""))
        sid = r["data"]["session"]["id"]
        renamed = self.main.rename_chat_session(G(session_id=sid, title="  Judul Baru  "))
        self.assertTrue(renamed["status"], renamed)
        self.assertEqual(renamed["data"]["session"]["title"], "Judul Baru")
        self.assertEqual(self.main.rename_chat_session(
            G(session_id=sid, title="   "))["status"], False)
        self.assertEqual(self.main.rename_chat_session(
            G(session_id="tidak-ada", title="x"))["status"], False)
        # Rename run agent: judul tampil di list_runs, bukan preview.
        run = {"id": "run_test_rename", "status": "done", "steps": 2,
               "history": [{"role": "user", "content": "perintah asli"}],
               "events": [], "error": "", "created": 1, "updated": 1, "pid": None}
        self.core.write_run(run)
        rr = self.main.rename_agent_run(G(run_id="run_test_rename", title="Cek site flazpedia"))
        self.assertTrue(rr["status"], rr)
        listed = [x for x in self.core.list_runs() if x["id"] == "run_test_rename"]
        self.assertEqual(listed[0]["title"], "Cek site flazpedia")

    def test_delete_agent_run_refuses_running(self):
        run = {"id": "run_test_running", "status": "running", "steps": 0,
               "history": [], "events": [], "error": "",
               "created": 1, "updated": 1, "pid": None}
        self.core.write_run(run)
        r = self.main.delete_agent_run(G(run_id="run_test_running"))
        self.assertFalse(r["status"])
        self.assertIsNotNone(self.core.read_run("run_test_running"))
        run["status"] = "done"
        self.core.write_run(run)
        self.assertTrue(self.main.delete_agent_run(G(run_id="run_test_running"))["status"])
        self.assertIsNone(self.core.read_run("run_test_running"))
        self.assertFalse(self.main.delete_agent_run(G(run_id="../evil"))["status"])

    def test_dispatcher_save_config_keeps_key_when_empty(self):
        r = self.main.save_config(G(endpoint="https://ai.skyzo.biz.id", model="m-x",
                                     api_key="", agent_root=self.root))
        self.assertTrue(r["status"])
        self.assertEqual(self.core.load_config()["api_key"], "fake-key")
        self.assertNotIn("api_key", r["data"])


class TestAgent(Base):
    def test_agent_read_tool_then_done(self):
        FakeUpstream.queue = [
            tool_call_msg("list_dir", {"path": "."}),
            msg("selesai, direktori kosong"),
        ]
        r = self.main.agent_start(G(messages=json.dumps([{"role": "user", "content": "list dir"}])))
        self.assertTrue(r["status"], r)
        run = self.wait_run(r["data"]["run_id"])
        self.assertEqual(run["status"], "done")
        tools = [e for e in run["events"] if e["type"] == "tool"]
        self.assertEqual(tools[0]["tool"], "list_dir")
        self.assertTrue(any(e["type"] == "text" for e in run["events"]))

    def test_agent_exec_waits_approval_then_allow(self):
        FakeUpstream.queue = [
            tool_call_msg("exec", {"command": "echo HELLO-AGENT"}),
            msg("perintah selesai"),
        ]
        r = self.main.agent_start(G(messages=json.dumps([{"role": "user", "content": "jalankan echo"}])))
        run_id = r["data"]["run_id"]
        self.wait_status(run_id, "waiting_approval")
        run = self.core.read_run(run_id)
        self.assertEqual(run["pending"]["tool"], "exec")
        ok = self.main.agent_approve(G(run_id=run_id, decision="allow"))
        self.assertTrue(ok["status"], ok)
        run = self.wait_run(run_id)
        self.assertEqual(run["status"], "done")
        tools = [e for e in run["events"] if e["type"] == "tool"]
        self.assertIn("HELLO-AGENT", tools[0]["result"])

    def test_agent_exec_deny_does_not_run(self):
        FakeUpstream.queue = [
            tool_call_msg("exec", {"command": "echo MUST-NOT-RUN"}),
            msg("baik, saya tidak menjalankannya"),
        ]
        r = self.main.agent_start(G(messages=json.dumps([{"role": "user", "content": "jalankan echo"}])))
        run_id = r["data"]["run_id"]
        self.wait_status(run_id, "waiting_approval")
        ok = self.main.agent_approve(G(run_id=run_id, decision="deny"))
        self.assertTrue(ok["status"], ok)
        run = self.wait_run(run_id)
        self.assertEqual(run["status"], "done")
        # Perintahnya memang tampil di event approval (itu yang disetujui/
        # ditolak user) — yang tidak boleh ada adalah HASIL eksekusinya.
        results = [e.get("result", "") for e in run["events"] if e["type"] == "tool"]
        self.assertEqual(results, ["DITOLAK user"])


class TestInstallScript(unittest.TestCase):
    def test_install_self_check(self):
        tmp = tempfile.mkdtemp()
        try:
            dest = os.path.join(tmp, "ai_chat_agent")
            shutil.copytree(PLUGIN_DIR, dest, ignore=shutil.ignore_patterns("data", "__pycache__"))
            rc = subprocess.run(["bash", os.path.join(dest, "install.sh"), "install"],
                                capture_output=True, text=True)
            self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
            self.assertTrue(os.path.isdir(os.path.join(dest, "data", "runs")))
            # Paket parsial harus ditolak, bukan diam-diam terpasang.
            os.remove(os.path.join(dest, "agent_worker.py"))
            rc = subprocess.run(["bash", os.path.join(dest, "install.sh"), "install"],
                                capture_output=True, text=True)
            self.assertNotEqual(rc.returncode, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_install_rejects_partial_upgrade_old_core(self):
        """Regresi insiden VPS 2026-11-01: main v1.1.0 + ai_core v1.0.0
        (tanpa list_chat_sessions) harus GAGAL di install.sh, bukan lolos
        lalu crash AttributeError saat menu Chat dibuka."""
        tmp = tempfile.mkdtemp()
        try:
            dest = os.path.join(tmp, "ai_chat_agent")
            shutil.copytree(PLUGIN_DIR, dest, ignore=shutil.ignore_patterns("data", "__pycache__"))
            old_core = subprocess.run(
                ["git", "-C", os.path.join(os.path.dirname(__file__), ".."),
                 "show", "27d456d:ai_chat_agent/ai_core.py"],
                capture_output=True, text=True).stdout
            self.assertNotIn("def list_chat_sessions", old_core)
            with open(os.path.join(dest, "ai_core.py"), "w") as f:
                f.write(old_core)
            rc = subprocess.run(["bash", os.path.join(dest, "install.sh"), "install"],
                                capture_output=True, text=True)
            self.assertNotEqual(rc.returncode, 0)
            msg = open("/tmp/bt_install.pl").read() if os.path.exists("/tmp/bt_install.pl") else ""
            self.assertIn("partial upgrade", msg.lower())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dispatcher_partial_core_returns_clean_error(self):
        import ai_core
        import ai_chat_agent_main
        importlib.reload(ai_core)
        importlib.reload(ai_chat_agent_main)
        main = ai_chat_agent_main.ai_chat_agent_main()
        saved = ai_core.list_chat_sessions
        try:
            delattr(ai_core, "list_chat_sessions")
            r = main.list_chat_sessions(G())
            self.assertFalse(r["status"])
            self.assertIn("parsial", r["msg"])
        finally:
            ai_core.list_chat_sessions = saved


if __name__ == "__main__":
    unittest.main()
