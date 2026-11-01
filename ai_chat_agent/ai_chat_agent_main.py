#!/usr/bin/python
# coding: utf-8
"""Dispatcher plugin aaPanel — AI Chat Agent.

Panel memanggil: /plugin?action=a&name=ai_chat_agent&s=<method>
dengan parameter form di `get`. Setiap method mengembalikan dict
{status, msg, data} (format yang sama dipakai plugin Perisai & Cloudflare
Tunnel). Modul `public` panel TIDAK dipakai supaya kode ini bisa diuji
di luar panel dengan Python standar saja.

Keamanan (ringkas, selengkapnya di README):
* GET config tidak pernah mengembalikan api_key (hanya has_key).
* Kolom key kosong saat Save = pakai key tersimpan (pola Terwo).
* Agent: tool baca otomatis, exec/write SELALU approval per tool,
  tanpa "Always Allow". Worker jalan detached; UI polling status.
"""

import json
import os
import signal
import subprocess
import sys
import time

PLUGIN_PATH = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_PATH not in sys.path:
    sys.path.insert(0, PLUGIN_PATH)

import ai_core  # noqa: E402


def _ok(msg, data=None):
    out = {"status": True, "msg": msg}
    if data is not None:
        out["data"] = data
    return out


def _fail(msg):
    return {"status": False, "msg": msg}


def _param(get, name, default=""):
    v = getattr(get, name, None) if get is not None else None
    return default if v is None else v


def _as_bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _parse_messages(get):
    """Pesan dari form aaPanel datang sebagai string JSON; di test bisa list."""
    raw = _param(get, "messages", "")
    if isinstance(raw, list):
        msgs = raw
    else:
        try:
            msgs = json.loads(raw or "[]")
        except Exception:
            return None, "messages harus JSON array"
    if not isinstance(msgs, list):
        return None, "messages harus JSON array"
    out = [m for m in msgs
           if isinstance(m, dict) and isinstance(m.get("content"), str)
           and m.get("role") in ("user", "assistant")][-50:]
    if not out or out[-1].get("role") != "user":
        return None, "Pesan terakhir harus dari user"
    return out, None


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _refresh_stale(run):
    """Run yang pid worker-nya sudah mati tapi status masih jalan =
    worker berhenti tak terduga (mis. VPS restart). Tandai error supaya
    UI tidak polling selamanya — beda dari Terwo yang run-nya hilang."""
    if run and run.get("status") in ("running", "waiting_approval"):
        if run.get("pid") and not _pid_alive(run["pid"]):
            run["status"] = "error"
            run["error"] = "Worker berhenti tak terduga (mungkin VPS/panel restart). Mulai run baru."
            run["pending"] = None
            ai_core.write_run(run)
    return run


class ai_chat_agent_main:
    # ---------- pengaturan ----------
    def get_config(self, get):
        return _ok("ok", ai_core.public_config())

    def save_config(self, get):
        endpoint = _param(get, "endpoint", "").strip()
        model = _param(get, "model", "").strip()
        key = _param(get, "api_key", "").strip()
        agent_root = _param(get, "agent_root", "").strip()
        if not endpoint or not model:
            return _fail("Endpoint dan model wajib diisi")
        try:
            endpoint = ai_core.normalize_base(endpoint)
        except ValueError as e:
            return _fail(str(e))
        if agent_root and not os.path.isdir(agent_root):
            return _fail("Agent root tidak ditemukan: %s" % agent_root)
        cur = ai_core.load_config()
        if not key and not cur.get("api_key"):
            return _fail("API key wajib diisi (belum ada key tersimpan)")
        cfg = {"endpoint": endpoint, "model": model,
               "api_key": key or cur.get("api_key", "")}
        if agent_root:
            cfg["agent_root"] = agent_root
        elif cur.get("agent_root"):
            cfg["agent_root"] = cur["agent_root"]
        ai_core.save_config(cfg)
        return _ok("Pengaturan disimpan", ai_core.public_config())

    def fetch_models(self, get):
        endpoint = _param(get, "endpoint", "").strip()
        key = _param(get, "api_key", "").strip()
        cur = ai_core.load_config()
        endpoint = endpoint or cur.get("endpoint", "")
        key = key or cur.get("api_key", "")
        if not endpoint:
            return _fail("Endpoint belum diatur")
        try:
            models = ai_core.fetch_models(endpoint, key)
        except ValueError as e:
            return _fail(str(e))
        return _ok("ok", {"models": models})

    # ---------- session chat (daftar di sidebar) ----------
    def _session_api_ok(self):
        """Jangan crash AttributeError bila upgrade parsial (kasus v1.1.0:
        main baru + ai_core lama). Kembalikan pesan jelas yang bisa
        ditindaklanjuti, supaya UI menampilkan error, bukan halaman
        crash panel."""
        for name in ("list_chat_sessions", "save_chat_session",
                     "get_chat_session", "delete_chat_session"):
            if not hasattr(ai_core, name):
                return (
                    "Instalasi plugin parsial: ai_core.py masih versi lama "
                    "(tidak ada %s). Jalankan di VPS: unzip -o "
                    "ai_chat_agent-*.zip -d /www/server/panel/plugin/ai_chat_agent/ "
                    "&& rm -rf /www/server/panel/plugin/ai_chat_agent/__pycache__ "
                    "&& bt restart" % name)
        return None

    def list_chat_sessions(self, get):
        err = self._session_api_ok()
        if err:
            return _fail(err)
        return _ok("ok", {"sessions": ai_core.list_chat_sessions()})

    def get_chat_session(self, get):
        err = self._session_api_ok()
        if err:
            return _fail(err)
        session_id = _param(get, "session_id", "").strip()
        session = ai_core.get_chat_session(session_id) if session_id else None
        if not session:
            return _fail("Session chat tidak ditemukan")
        return _ok("ok", {"session": session})

    def save_chat_session(self, get):
        err = self._session_api_ok()
        if err:
            return _fail(err)
        raw = _param(get, "messages", "")
        try:
            msgs = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return _fail("messages harus JSON array")
        if not isinstance(msgs, list):
            return _fail("messages harus JSON array")
        msgs = [m for m in msgs
                if isinstance(m, dict) and isinstance(m.get("content"), str)
                and m.get("role") in ("user", "assistant")][-100:]
        session = ai_core.save_chat_session(
            _param(get, "session_id", "").strip() or None, msgs,
            _param(get, "title", "").strip() or None)
        return _ok("Session disimpan", {"session": session})

    def delete_chat_session(self, get):
        err = self._session_api_ok()
        if err:
            return _fail(err)
        session_id = _param(get, "session_id", "").strip()
        if not session_id:
            return _fail("session_id wajib diisi")
        ai_core.delete_chat_session(session_id)
        return _ok("Session dihapus")

    def rename_chat_session(self, get):
        err = self._session_api_ok()
        if err:
            return _fail(err)
        session_id = _param(get, "session_id", "").strip()
        title = _param(get, "title", "").strip()
        if not session_id:
            return _fail("session_id wajib diisi")
        try:
            session = ai_core.rename_chat_session(session_id, title)
        except ValueError as e:
            return _fail(str(e))
        if not session:
            return _fail("Session chat tidak ditemukan")
        return _ok("Session di-rename", {"session": session})

    def rename_agent_run(self, get):
        run_id = _param(get, "run_id", "").strip()
        title = _param(get, "title", "").strip()
        if not run_id:
            return _fail("run_id wajib diisi")
        try:
            run = ai_core.rename_agent_run(run_id, title)
        except ValueError as e:
            return _fail(str(e))
        if not run:
            return _fail("Run tidak ditemukan")
        return _ok("Run di-rename", {"run_id": run_id, "title": run.get("title")})

    def delete_agent_run(self, get):
        run_id = _param(get, "run_id", "").strip()
        if not run_id:
            return _fail("run_id wajib diisi")
        try:
            deleted = ai_core.delete_agent_run(run_id)
        except ValueError as e:
            return _fail(str(e))
        if not deleted:
            return _fail("Run tidak ditemukan")
        return _ok("Run dihapus")

    # ---------- chat biasa (tanpa tool) ----------
    def chat(self, get):
        msgs, err = _parse_messages(get)
        if err:
            return _fail(err)
        cfg = ai_core.load_config()
        try:
            reply = ai_core.call_upstream(cfg, msgs)
        except ValueError as e:
            return _fail(str(e))
        return _ok("ok", {"reply": reply.get("content") or ""})

    # ---------- agent ----------
    def agent_start(self, get):
        msgs, err = _parse_messages(get)
        if err:
            return _fail(err)
        cfg = ai_core.load_config()
        if not cfg.get("endpoint") or not cfg.get("api_key") or not cfg.get("model"):
            return _fail("AI belum diatur: isi endpoint, API key, dan model di Pengaturan dulu")
        ai_core.ensure_dirs()
        import secrets
        run_id = "run_%d_%s" % (int(time.time()), secrets.token_hex(4))
        run = {"id": run_id, "status": "running", "steps": 0, "pending": None,
               "history": msgs, "events": [], "error": "",
               "created": int(time.time()), "updated": int(time.time()), "pid": None}
        ai_core.write_run(run)
        worker = os.path.join(PLUGIN_PATH, "agent_worker.py")
        log_path = os.path.join(ai_core.RUNS_DIR, run_id + ".log")
        try:
            log_f = open(log_path, "ab")
            proc = subprocess.Popen(
                [sys.executable, worker, run_id],
                stdout=log_f, stderr=subprocess.STDOUT,
                start_new_session=True, cwd=PLUGIN_PATH)
            log_f.close()  # worker mewarisi salinan fd-nya; parent boleh tutup
        except Exception as e:
            run["status"] = "error"
            run["error"] = "Gagal menjalankan worker: %s" % e
            ai_core.write_run(run)
            return _fail(run["error"])
        run["pid"] = proc.pid
        ai_core.write_run(run)
        return _ok("Agent dimulai", {"run_id": run_id})

    def agent_status(self, get):
        run_id = _param(get, "run_id", "").strip()
        run = ai_core.read_run(run_id) if run_id else None
        if not run:
            return _fail("Run tidak ditemukan")
        return _ok("ok", ai_core.public_run(_refresh_stale(run)))

    def list_runs(self, get):
        return _ok("ok", {"runs": ai_core.list_runs()})

    def agent_approve(self, get):
        run_id = _param(get, "run_id", "").strip()
        decision = _param(get, "decision", "").strip()
        if decision not in ("allow", "deny", "stop"):
            return _fail("decision harus allow / deny / stop")
        run = ai_core.read_run(run_id) if run_id else None
        if not run:
            return _fail("Run tidak ditemukan")
        run = _refresh_stale(run)
        if run.get("status") != "waiting_approval":
            return _fail("Run tidak sedang menunggu approval (status: %s)" % run.get("status"))
        if decision == "stop":
            return self.agent_stop(get)
        try:
            with open(ai_core.decision_path(run_id), "w", encoding="utf-8") as f:
                json.dump({"decision": decision}, f)
        except Exception as e:
            return _fail("Gagal menulis approval: %s" % e)
        return _ok("Approval %s dikirim, agent melanjutkan" % decision)

    def agent_stop(self, get):
        run_id = _param(get, "run_id", "").strip()
        run = ai_core.read_run(run_id) if run_id else None
        if not run:
            return _fail("Run tidak ditemukan")
        if run.get("status") in ("done", "error", "stopped"):
            return _ok("Run sudah %s" % run["status"])
        try:
            with open(ai_core.decision_path(run_id), "w", encoding="utf-8") as f:
                json.dump({"decision": "stop"}, f)
        except Exception:
            pass
        if run.get("pid"):
            try:
                os.kill(int(run["pid"]), signal.SIGTERM)
            except Exception:
                pass
        run["status"] = "stopped"
        run["pending"] = None
        run.setdefault("events", []).append({"type": "stopped"})
        run["updated"] = int(time.time())
        ai_core.write_run(run)
        return _ok("Agent dihentikan")

    # ---------- utilitas ----------
    def list_sites(self, get):
        try:
            return _ok("ok", {"sites": ai_core.list_sites_data()})
        except ValueError as e:
            return _fail(str(e))
