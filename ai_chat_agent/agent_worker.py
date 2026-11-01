#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Worker run agent (background, detached dari request panel).

Dipanggil dispatcher sebagai:
    python3 agent_worker.py <run_id>

Loop (port dari aiAgentLoop Terwo):
  model -> tool_call -> eksekusi -> hasil kembali ke model.
  * list_dir / read_file / list_sites: jalan otomatis.
  * exec / write_file: tulis status waiting_approval + pending ke run.json,
    lalu polling file <run_id>.decision.json tiap 0,5 detik sampai user
    menjawab allow / deny / stop. File decision dihapus setelah dibaca.
  * Tidak ada batas langkah. Stop dari UI adalah kendalinya.
  * Status akhir: done / error / stopped. Bila worker mati tak terduga
    (mis. VPS restart), dispatcher menandai run yang pid-nya sudah tidak
    hidup sebagai error saat UI polling berikutnya.

Worker TIDAK pernah menulis API key ke run.json: key hanya dibaca dari
config di awal dan disimpan di memori proses ini.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ai_core  # noqa: E402


def _event(run, ev):
    run.setdefault("events", []).append(ev)
    run["updated"] = int(time.time())
    ai_core.write_run(run)


def _wait_decision(run):
    """Tunggu jawaban approval. Kembalikan 'allow' / 'deny' / 'stop'."""
    path = ai_core.decision_path(run["id"])
    while True:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    decision = (json.load(f) or {}).get("decision")
            except Exception:
                decision = None
            try:
                os.remove(path)
            except Exception:
                pass
            if decision in ("allow", "deny", "stop"):
                return decision
        time.sleep(0.5)


def main(run_id):
    run = ai_core.read_run(run_id)
    if not run:
        return 2
    cfg = ai_core.load_config()
    root = cfg.get("agent_root") or ai_core.default_agent_root()
    panel_dir = ai_core.PANEL_DIR
    messages = [{"role": "system", "content": ai_core.AGENT_SYSTEM}]
    messages.extend(run.get("history") or [])
    read_tools = {"list_dir", "read_file", "list_sites"}
    write_tools = {"exec", "write_file"}
    try:
        while True:
            msg = ai_core.call_upstream(cfg, messages, tools=ai_core.tool_defs())
            messages.append(msg)
            if msg.get("content"):
                _event(run, {"type": "text", "content": msg["content"]})
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                run["status"] = "done"
                run["pending"] = None
                _event(run, {"type": "done"})
                return 0
            run["steps"] = run.get("steps", 0) + 1
            ai_core.write_run(run)
            for tc in tool_calls:
                fn = tc.get("function") or {}
                tname = fn.get("name") or ""
                try:
                    targs = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    targs = {}
                if tname in read_tools:
                    result = ai_core.exec_tool(tname, targs, root, panel_dir)
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})
                    _event(run, {"type": "tool", "tool": tname, "args": targs, "result": result})
                elif tname in write_tools:
                    run["status"] = "waiting_approval"
                    run["pending"] = {"tool": tname, "args": targs, "call_id": tc.get("id")}
                    ai_core.write_run(run)
                    _event(run, {"type": "approval", "tool": tname, "args": targs})
                    decision = _wait_decision(run)
                    run["pending"] = None
                    if decision == "stop":
                        run["status"] = "stopped"
                        _event(run, {"type": "stopped"})
                        return 0
                    if decision == "allow":
                        result = ai_core.exec_tool(tname, targs, root, panel_dir)
                        messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})
                        _event(run, {"type": "tool", "tool": tname, "args": targs, "result": result})
                        run["status"] = "running"
                        ai_core.write_run(run)
                    else:  # deny
                        denied = "User menolak menjalankan tool ini."
                        messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": denied})
                        _event(run, {"type": "tool", "tool": tname, "args": targs, "result": "DITOLAK user"})
                        run["status"] = "running"
                        ai_core.write_run(run)
                else:
                    unknown = "Tool tidak dikenal: %s" % tname
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": unknown})
                    _event(run, {"type": "tool", "tool": tname, "args": targs, "result": unknown})
    except Exception as e:
        run["status"] = "error"
        run["error"] = str(e)
        run["pending"] = None
        _event(run, {"type": "error", "error": str(e)})
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: agent_worker.py <run_id>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
