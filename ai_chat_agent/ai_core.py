# -*- coding: utf-8 -*-
"""Inti AI Chat Agent untuk aaPanel — hanya Python standard library.

Port dari AI Chat + Mode Agent Terwo (Node.js) ke pola plugin aaPanel
Python, mengikuti referensi plugin Cloudflare Tunnel aaPanel:
dispatcher tipis, manager terpisah, config chmod 0600, dan key tidak
pernah dikembalikan ke browser.

Perbedaan penting dari Terwo (disengaja, karena aaPanel berbeda):
* Terwo memakai SSE stream yang hidup selama satu request HTTP.
  Plugin aaPanel dipanggil panel lewat POST sinkron yang mengembalikan
  JSON, jadi run agent TIDAK bisa numpang di satu request. Sebagai ganti,
  agent jalan sebagai worker background (agent_worker.py) yang menulis
  progres ke data/runs/<id>.json, dan UI melakukan polling. Approval
  exec/write disampaikan lewat file data/runs/<id>.decision.json.
* Batas langkah TIDAK ada (keputusan user Terwo 2026-10-28: batas 12
  langkah dihapus total). Kendalinya adalah tombol Stop di UI.
* Tool baca jalan otomatis; tool tulis/eksekusi SELALU menunggu
  approval Allow/Deny/Stop per tool. Tidak ada "Always Allow" — di VPS
  yang panelnya jalan sebagai root, itu terlalu berbahaya untuk
  dijadikan sekali klik.

Jalur bisa di-override lewat environment (untuk pengujian di luar panel):
  AI_CHAT_DATA_DIR   (default: <plugin>/data)
  AI_CHAT_PANEL_DIR  (default: /www/server/panel)
  AI_CHAT_AGENT_ROOT (default: /www/wwwroot bila ada, bila tidak /www)
"""

import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("AI_CHAT_DATA_DIR") or os.path.join(PLUGIN_DIR, "data")
PANEL_DIR = os.environ.get("AI_CHAT_PANEL_DIR") or "/www/server/panel"
CONFIG_FILE = os.path.join(DATA_DIR, "ai_config.json")
RUNS_DIR = os.path.join(DATA_DIR, "runs")

AGENT_SYSTEM = (
    "Kamu adalah AI agent yang berjalan di dalam plugin aaPanel di VPS. "
    "Jawab dalam Bahasa Indonesia. Kamu punya tool: exec (jalankan perintah "
    "shell), read_file, write_file, list_dir, list_sites. Semua file hanya "
    "di dalam agent root (biasanya /www/wwwroot). Gunakan tool untuk "
    "menyelesaikan tugas, jangan hanya menjelaskan. Setelah tool selesai, "
    "rangkum hasilnya dengan singkat. Bila tool ditolak user, jangan "
    "mengulang tool yang sama tanpa mengubah pendekatan."
)

EXEC_TIMEOUT = 30          # detik, sama seperti Terwo
EXEC_OUTPUT_MAX = 20000    # byte, sama seperti Terwo
READ_FILE_MAX = 100000     # byte, sama seperti Terwo
LIST_DIR_MAX = 200         # entri, sama seperti Terwo


def default_agent_root():
    if os.environ.get("AI_CHAT_AGENT_ROOT"):
        return os.environ["AI_CHAT_AGENT_ROOT"]
    for cand in ("/www/wwwroot", "/www"):
        if os.path.isdir(cand):
            return cand
    return "/www/wwwroot"


def ensure_dirs():
    os.makedirs(RUNS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Konfigurasi (key tidak pernah keluar dari file ini)
# ---------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    ensure_dirs()
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)
    return cfg


def public_config():
    """Versi config yang aman dikirim ke browser: tanpa api_key."""
    cfg = load_config()
    root = cfg.get("agent_root") or default_agent_root()
    return {
        "endpoint": cfg.get("endpoint", ""),
        "model": cfg.get("model", ""),
        "has_key": bool(cfg.get("api_key")),
        "agent_root": root,
        "agent_root_exists": os.path.isdir(root),
    }


def normalize_base(endpoint):
    base = (endpoint or "").strip().rstrip("/")
    if not re.match(r"^https?://", base, re.IGNORECASE):
        raise ValueError("Endpoint harus URL http(s), mis. https://ai.skyzo.biz.id/v1")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


# ---------------------------------------------------------------------------
# Upstream OpenAI-compatible (toleran terhadap router non-standar ala 9Router)
# ---------------------------------------------------------------------------

def first_json_value(text):
    """Ambil objek JSON pertama yang lengkap, abaikan trailer sesudahnya."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def _msg_from_choice_data(d):
    if not d:
        return None
    if isinstance(d, dict) and d.get("error"):
        raise ValueError("Upstream error: " + json.dumps(d["error"])[:300])
    ch = (d.get("choices") or [None])[0] if isinstance(d, dict) else None
    if not ch:
        return None
    if ch.get("message"):
        return ch["message"]
    if ch.get("delta"):
        # JSON utuh tapi berisi delta (bukan message): rakit jadi satu pesan.
        msg = {"role": "assistant", "content": ch["delta"].get("content")}
        if ch["delta"].get("tool_calls"):
            msg["tool_calls"] = ch["delta"]["tool_calls"]
        return msg
    return None


def parse_upstream_message(text):
    """Urai balasan /chat/completions non-streaming.

    Menerima: JSON biasa, JSON berbungkus {"data": ...}, SSE padahal
    stream:false, dan trailer "data: [DONE]" ala 9Router. Mengembalikan
    dict message OpenAI (role/content/tool_calls).
    """
    clean = (text or "").replace("\ufeff", "").strip()
    if not clean:
        raise ValueError("Respons upstream kosong")
    try:
        parsed = json.loads(clean)
        msg = _msg_from_choice_data(parsed.get("data") if isinstance(parsed, dict) else None)
        if msg:
            return msg
        msg = _msg_from_choice_data(parsed)
        if msg:
            return msg
    except ValueError as e:
        # Error upstream yang disengaja harus lolos; JSONDecodeError
        # (mis. trailer "data: [DONE]" sesudah JSON) lanjut ke fallback.
        if str(e).startswith("Upstream error:"):
            raise
    except Exception:
        pass
    # SSE HARUS dicek sebelum first_json_value: kalau tidak, objek JSON
    # pertama (satu chunk delta) langsung dikembalikan dan tool_call yang
    # terpecah di chunk-chunk berikutnya hilang.
    if clean.startswith("data:"):
        content = ""
        tool_map = {}
        last_msg = None
        for line in clean.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                pj = json.loads(payload)
            except Exception:
                pj = first_json_value(payload)
            if not pj:
                continue
            d = pj.get("data") if isinstance(pj, dict) else None
            d = d or pj
            if isinstance(d, dict) and d.get("error"):
                raise ValueError("Upstream error: " + json.dumps(d["error"])[:300])
            ch = (d.get("choices") or [None])[0] if isinstance(d, dict) else None
            if not ch:
                continue
            if ch.get("message"):
                last_msg = ch["message"]
            if ch.get("delta"):
                if ch["delta"].get("content"):
                    content += ch["delta"]["content"]
                for t in ch["delta"].get("tool_calls") or []:
                    idx = t.get("index") if t.get("index") is not None else 0
                    cur = tool_map.get(idx) or {
                        "id": t.get("id") or ("call_sse_%d" % idx),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                    if t.get("id"):
                        cur["id"] = t["id"]
                    fn = t.get("function") or {}
                    if fn.get("name"):
                        cur["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        cur["function"]["arguments"] += fn["arguments"]
                    tool_map[idx] = cur
        if last_msg:
            return last_msg
        if content or tool_map:
            msg = {"role": "assistant", "content": content or None}
            if tool_map:
                msg["tool_calls"] = [tool_map[k] for k in sorted(tool_map)]
            return msg
    # Bukan SSE: JSON + teks tambahan (mis. trailer sesudah JSON utuh).
    j = first_json_value(clean)
    if j:
        msg = _msg_from_choice_data(j.get("data") if isinstance(j, dict) else None) or _msg_from_choice_data(j)
        if msg:
            return msg
    raise ValueError("Respons agent bukan JSON. Upstream mengembalikan: " + clean[:300])


def _check_cloudflare_block(status, txt, what):
    """1010 bukan salah key: itu Cloudflare Browser Integrity Check di
    edge yang menolak request sebelum sampai ke API. Bila masih muncul
    setelah perbaikan header, satu-satunya perbaikan di sisi server
    adalah pemilik zona mematikan BIC untuk path /v1/ (Configuration
    Rule). Pesan ini dibuat eksplisit supaya user tidak buang waktu
    mengganti-ganti key — key tidak pernah diperiksa pada 1010."""
    if status == 403 and "1010" in (txt or ""):
        raise ValueError(
            "%s gagal: Cloudflare memblokir di edge (error code: 1010, "
            "Browser Integrity Check) — request tidak sampai ke API, jadi "
            "ini BUKAN salah API key. Plugin kini mengirim User-Agent "
            "sendiri, bukan Python-urllib yang diblokir; bila masih 1010, "
            "pemilik zona upstream harus mematikan Browser Integrity "
            "Check untuk path /v1/." % what)


def _http(method, url, api_key, body=None, timeout=120):
    # PENTING (insiden 2026-11-01, run agent VPS -> 403 error code: 1010):
    # urllib Python tanpa User-Agent eksplisit mengirim
    # "Python-urllib/3.x", dan string itu ada di daftar blokir Cloudflare
    # Browser Integrity Check. Akibatnya request ditolak di edge sebelum
    # API key sempat diperiksa — persis gejala yang dilaporkan (steps: 0,
    # tidak ada tool yang jalan). Terwo (Node fetch) dan Perisai Go tidak
    # kena karena UA mereka bukan Python-urllib. Perbaikannya di sisi
    # klien: kirim UA milik plugin sendiri, bukan menyamar jadi browser.
    headers = {
        "Authorization": "Bearer " + api_key,
        "User-Agent": "AI-Chat-Agent-aaPanel/1.1",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        raise ValueError("Gagal menghubungi upstream: %s" % e)


def call_upstream(cfg, messages, tools=None, timeout=120):
    if not cfg.get("endpoint") or not cfg.get("api_key") or not cfg.get("model"):
        raise ValueError("AI belum diatur: isi endpoint, API key, dan model di Pengaturan dulu")
    base = normalize_base(cfg["endpoint"])
    body = {"model": cfg["model"], "messages": messages, "stream": False}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    status, txt = _http("POST", base + "/chat/completions", cfg["api_key"], body, timeout=timeout)
    _check_cloudflare_block(status, txt, "Chat")
    if status < 200 or status >= 300:
        raise ValueError("Chat gagal (%s): %s" % (status, txt[:300]))
    return parse_upstream_message(txt)


def fetch_models(endpoint, api_key, timeout=30):
    base = normalize_base(endpoint)
    if not api_key:
        raise ValueError("API key belum diatur")
    status, txt = _http("GET", base + "/models", api_key, timeout=timeout)
    _check_cloudflare_block(status, txt, "Fetch models")
    if status < 200 or status >= 300:
        raise ValueError("Fetch models gagal (%s): %s" % (status, txt[:300]))
    try:
        j = json.loads(txt)
    except Exception:
        raise ValueError("Respons models bukan JSON")
    data = j.get("data") if isinstance(j, dict) else None
    data = data or (j.get("models") if isinstance(j, dict) else None) or []
    models = sorted(m for m in ((m if isinstance(m, str) else m.get("id")) for m in data) if m)
    return models


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def tool_defs():
    return [
        {"type": "function", "function": {
            "name": "exec",
            "description": "Jalankan perintah shell (bash -c) di VPS. Output dibatasi 20 KB, timeout 30 detik. PERLU approval user sebelum dijalankan.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "Direktori kerja, relatif terhadap agent root (default agent root)"},
            }, "required": ["command"]}}},
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Baca isi file teks (maks 100 KB, selebihnya dipotong). Path relatif terhadap agent root atau absolut di dalam agent root.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "write_file",
            "description": "Tulis/overwrite file teks. Path relatif terhadap agent root atau absolut di dalam agent root. PERLU approval user sebelum dijalankan.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"},
            }, "required": ["path", "content"]}}},
        {"type": "function", "function": {
            "name": "list_dir",
            "description": "Daftar isi direktori (maks 200 entri). Path relatif terhadap agent root atau absolut di dalam agent root.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}}},
        {"type": "function", "function": {
            "name": "list_sites",
            "description": "Daftar website yang terdaftar di aaPanel (nama, path, status, domain) — dibaca dari database panel, read-only.",
            "parameters": {"type": "object", "properties": {}}}},
    ]


def safe_path(root, rel_or_abs):
    """Kurung path di dalam root; traversal keluar ditolak (pola Terwo)."""
    root_resolved = os.path.realpath(root)
    if os.path.isabs(rel_or_abs or ""):
        p = os.path.realpath(rel_or_abs)
    else:
        p = os.path.realpath(os.path.join(root_resolved, rel_or_abs or "."))
    if p != root_resolved and not p.startswith(root_resolved + os.sep):
        raise ValueError("Access outside working directory denied")
    return p


def list_sites_data(panel_dir=None):
    """Baca daftar site aaPanel (read-only). Dipakai tool list_sites dan
    endpoint list_sites. Mengembalikan list dict, bukan string."""
    db = os.path.join(panel_dir or PANEL_DIR, "data", "default.db")
    if not os.path.exists(db):
        raise ValueError("Database panel tidak ditemukan: %s" % db)
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        sites = []
        for sid, name, path, status in con.execute(
                "SELECT id, name, path, status FROM sites ORDER BY id"):
            domains = [r[0] for r in con.execute(
                "SELECT name FROM domain WHERE pid=? ORDER BY id", (sid,))]
            sites.append({"id": sid, "name": name, "path": path,
                          "status": status, "domains": domains})
        return sites
    finally:
        con.close()


def exec_tool(name, args, root, panel_dir=None):
    """Jalankan satu tool. Gagal dikembalikan sebagai string hasil (pola
    Terwo), bukan exception, supaya model bisa membaca dan memperbaiki."""
    args = args or {}
    try:
        if name == "list_dir":
            full = safe_path(root, args.get("path") or ".")
            entries = sorted(os.scandir(full), key=lambda e: e.name)[:LIST_DIR_MAX]
            return json.dumps([{"name": e.name,
                                "type": "dir" if e.is_dir() else "file"}
                               for e in entries])
        if name == "read_file":
            full = safe_path(root, args.get("path") or "")
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(READ_FILE_MAX + 1)
            if len(text) > READ_FILE_MAX:
                text = text[:READ_FILE_MAX] + "\n... [dipotong, file lebih dari 100 KB]"
            return text
        if name == "write_file":
            full = safe_path(root, args.get("path") or "")
            content = args.get("content") or ""
            os.makedirs(os.path.dirname(full) or root, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            return "OK, %d byte ditulis ke %s" % (len(content.encode("utf-8")), args.get("path"))
        if name == "exec":
            cwd = safe_path(root, args.get("cwd") or ".")
            proc = subprocess.run(["bash", "-c", args.get("command") or ""],
                                  cwd=cwd, capture_output=True, text=True,
                                  timeout=EXEC_TIMEOUT)
            combined = proc.stdout or ""
            if proc.stderr:
                combined += ("\n[stderr]\n" if combined else "") + proc.stderr
            if len(combined) > EXEC_OUTPUT_MAX:
                combined = combined[:EXEC_OUTPUT_MAX] + "\n... [output dipotong 20 KB]"
            return "exit code %d%s" % (proc.returncode,
                                        ("\n" + combined) if combined else " (tanpa output)")
        if name == "list_sites":
            return json.dumps(list_sites_data(panel_dir))
        return "Tool tidak dikenal: %s" % name
    except subprocess.TimeoutExpired:
        return "exec timeout: perintah dihentikan setelah %d detik" % EXEC_TIMEOUT
    except Exception as e:
        return "Tool %s gagal: %s" % (name, e)


# ---------------------------------------------------------------------------
# Run state (file-based, dibaca worker + dispatcher + UI polling)
# ---------------------------------------------------------------------------

def run_path(run_id):
    return os.path.join(RUNS_DIR, "%s.json" % run_id)


def decision_path(run_id):
    return os.path.join(RUNS_DIR, "%s.decision.json" % run_id)


def read_run(run_id):
    try:
        with open(run_path(run_id), "r", encoding="utf-8") as f:
            run = json.load(f)
        return run if isinstance(run, dict) else None
    except Exception:
        return None


def write_run(run):
    ensure_dirs()
    path = run_path(run["id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(run, f, indent=2)
    os.replace(tmp, path)
    return run


def list_runs():
    ensure_dirs()
    runs = []
    for fname in os.listdir(RUNS_DIR):
        if not fname.endswith(".json") or fname.endswith(".decision.json"):
            continue
        try:
            with open(os.path.join(RUNS_DIR, fname), "r", encoding="utf-8") as f:
                run = json.load(f)
        except Exception:
            continue
        first_user = ""
        for m in run.get("history") or []:
            if m.get("role") == "user":
                first_user = (m.get("content") or "")[:80]
                break
        runs.append({"id": run.get("id"), "status": run.get("status"),
                     "steps": run.get("steps", 0), "updated": run.get("updated", 0),
                     "title": run.get("title") or first_user,
                     "preview": first_user, "error": run.get("error", "")})
    runs.sort(key=lambda r: r.get("updated", 0), reverse=True)
    return runs


CHAT_SESSIONS_FILE = os.path.join(DATA_DIR, "chat_sessions.json")


def read_chat_sessions():
    """Semua session chat, diurutkan dari yang terbaru diubah.

    Disimpan di server (bukan localStorage browser) supaya daftar di
    sidebar tetap ada walau plugin dibuka dari perangkat lain. File ini
    chmod 0600 karena isi chat bisa mengandung potongan konfigurasi.
    """
    ensure_dirs()
    try:
        with open(CHAT_SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_chat_sessions(sessions):
    ensure_dirs()
    tmp = CHAT_SESSIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CHAT_SESSIONS_FILE)
    return sessions


def _chat_title(messages, title):
    if title:
        return title[:80]
    for m in messages or []:
        if m.get("role") == "user" and (m.get("content") or "").strip():
            text = " ".join(str(m["content"]).split())
            return text[:40] if text else "Chat baru"
    return "Chat baru"


def save_chat_session(session_id, messages, title=None):
    """Buat (id kosong) atau perbarui satu session chat."""
    messages = [m for m in (messages or [])
                if isinstance(m, dict) and isinstance(m.get("content"), str)
                and m.get("role") in ("user", "assistant")][-100:]
    sessions = read_chat_sessions()
    now = int(time.time())
    if session_id:
        for s in sessions:
            if s.get("id") == session_id:
                s["messages"] = messages
                s["title"] = _chat_title(messages, title or s.get("title"))
                s["updated"] = now
                write_chat_sessions(sessions)
                return s
    new_id = "chat_%d_%s" % (now, secrets.token_hex(4))
    s = {"id": new_id, "title": _chat_title(messages, title),
         "messages": messages, "created": now, "updated": now}
    sessions.append(s)
    write_chat_sessions(sessions)
    return s


def get_chat_session(session_id):
    for s in read_chat_sessions():
        if s.get("id") == session_id:
            return s
    return None


def delete_chat_session(session_id):
    sessions = [s for s in read_chat_sessions() if s.get("id") != session_id]
    write_chat_sessions(sessions)
    return True


def rename_chat_session(session_id, title):
    """Ubah judul satu session chat. Judul kosong ditolak; judul
    panjang dipotong 80 karakter. Urutan (updated) tidak diubah supaya
    rename tidak tiba-tiba memindahkan session ke atas daftar."""
    title = " ".join((title or "").split())
    if not title:
        raise ValueError("Judul tidak boleh kosong")
    sessions = read_chat_sessions()
    for s in sessions:
        if s.get("id") == session_id:
            s["title"] = title[:80]
            write_chat_sessions(sessions)
            return s
    return None


def rename_agent_run(run_id, title):
    """Ubah judul tampilan satu run agent (disimpan di run.json, tidak
    mengubah preview pesan pertama bila belum pernah di-rename)."""
    title = " ".join((title or "").split())
    if not title:
        raise ValueError("Judul tidak boleh kosong")
    run = read_run(run_id)
    if not run:
        return None
    run["title"] = title[:80]
    write_run(run)
    return run


def delete_agent_run(run_id):
    """Hapus file satu run agent (json + decision + log). Run yang
    masih jalan/menunggu approval DITOLAK — hentikan dulu lewat Stop,
    supaya worker tidak menulis ke file yang sudah dihapus. ID
    divalidasi ketat (tanpa slash) supaya tidak bisa menghapus file
    lain lewat path traversal."""
    if not re.match(r"^[A-Za-z0-9_-]+$", run_id or ""):
        raise ValueError("run_id tidak valid")
    run = read_run(run_id)
    if not run:
        return False
    if run.get("status") in ("running", "waiting_approval"):
        raise ValueError("Run masih berjalan — hentikan dulu (Stop run), baru hapus")
    for path in (run_path(run_id), decision_path(run_id),
                 os.path.join(RUNS_DIR, run_id + ".log")):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    return True


def list_chat_sessions():
    sessions = read_chat_sessions()
    sessions.sort(key=lambda s: s.get("updated", 0), reverse=True)
    return [{"id": s.get("id"), "title": s.get("title", "Chat baru"),
             "updated": s.get("updated", 0),
             "count": len(s.get("messages") or [])} for s in sessions]


def public_run(run):
    """Salinan run untuk UI: hasil tool dipotong supaya polling ringan."""
    if not run:
        return None
    out = dict(run)
    events = []
    for ev in run.get("events") or []:
        ev2 = dict(ev)
        if isinstance(ev2.get("result"), str) and len(ev2["result"]) > 2000:
            ev2["result"] = ev2["result"][:2000] + "\n... [dipotong untuk tampilan]"
        events.append(ev2)
    out["events"] = events
    out.pop("messages", None)  # internal loop state, bukan untuk UI
    return out
