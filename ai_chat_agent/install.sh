#!/bin/bash
# AI Chat Agent — installer plugin aaPanel.
# Dipanggil panel sebagai: bash install.sh install|update|uninstall
# Mengikuti pola plugin Cloudflare Tunnel aaPanel: hook ini melakukan
# self-check (file wajib harus ada & tidak kosong) supaya upgrade parsial
# langsung ketahuan, bukan baru gagal saat UI dipakai.
# Plugin ini TIDAK punya service/systemd sendiri: agent jalan sebagai
# worker Python detached per run, dan berhenti saat run selesai/dihentikan.
# Data (config + runs) tinggal di <plugin>/data — TIDAK dihapus saat
# install/update; tapi ikut terhapus bila plugin di-uninstall panel,
# jadi backup data/ai_config.json sebelum uninstall.
PATH=/www/server/panel/pyenv/bin:/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin:/usr/local/sbin:~/bin
export PATH
install_tmp='/tmp/bt_install.pl'
PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"

REQUIRED_FILES=(
    ai_chat_agent_main.py
    ai_core.py
    agent_worker.py
    index.html
    info.json
)

do_install()
{
    mkdir -p "${PLUGIN_DIR}/data/runs"

    # Hapus bytecode basi dari versi sebelumnya (pola Cloudflare Tunnel:
    # copy aaPanel bisa membuat mtime seri sehingga .pyc lama menang).
    # Di v1.1.0 ada kasus nyata: main baru terpasang tapi ai_core lama
    # (v1.0.0, 484 baris, tanpa list_chat_sessions) ikut tertinggal,
    # sehingga plugin crash AttributeError saat menu Chat dibuka.
    rm -rf "${PLUGIN_DIR}/__pycache__"
    find "${PLUGIN_DIR}" -name "*.pyc" -delete 2>/dev/null || true

    missing=()
    for f in "${REQUIRED_FILES[@]}"; do
        if [ ! -s "${PLUGIN_DIR}/${f}" ]; then
            missing+=("$f")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        echo "Installation incomplete — missing/empty files: ${missing[*]}" > $install_tmp
        echo "Fix: re-download the release zip and Import again." >> $install_tmp
        exit 1
    fi

    # Sanity per simbol, bukan sekadar file ada: Import aaPanel terbukti
    # bisa menimpa sebagian file saja (main baru + core lama). Kalau satu
    # simbol wajib hilang, GAGALKAN instalasi di sini dengan pesan jelas,
    # jangan biarkan plugin terpasang setengah lalu crash di UI.
    check_symbols()
    {
        local file="$1"; shift
        local sym
        for sym in "$@"; do
            if ! grep -q "$sym" "${PLUGIN_DIR}/${file}" 2>/dev/null; then
                echo "Installation broken — ${file} is from an older version (missing ${sym})." > $install_tmp
                echo "This is a partial upgrade: the panel did not overwrite all files." >> $install_tmp
                echo "Fix (keeps data/): unzip -o ai_chat_agent-*.zip -d ${PLUGIN_DIR}/ && rm -rf ${PLUGIN_DIR}/__pycache__ && bt restart" >> $install_tmp
                exit 1
            fi
        done
    }
    check_symbols "ai_core.py" \
        "def list_chat_sessions" "def save_chat_session" \
        "def get_chat_session" "def delete_chat_session" \
        "def rename_chat_session" "def rename_agent_run" "def delete_agent_run" \
        "def _check_cloudflare_block" "def call_upstream"
    check_symbols "ai_chat_agent_main.py" \
        "def list_chat_sessions" "def save_chat_session" \
        "def get_chat_session" "def delete_chat_session" \
        "def rename_chat_session" "def rename_agent_run" "def delete_agent_run" \
        "def agent_start" "def agent_approve" "def chat"
    check_symbols "index.html" \
        "ac-pane-settings" "ac-pane-chat" "ac-pane-agent" "ac-chat-list" "ac-run-list"

    chmod 755 "${PLUGIN_DIR}/install.sh" "${PLUGIN_DIR}/agent_worker.py" 2>/dev/null
    chmod 600 "${PLUGIN_DIR}/ai_core.py" "${PLUGIN_DIR}/ai_chat_agent_main.py" "${PLUGIN_DIR}/index.html" "${PLUGIN_DIR}/info.json" 2>/dev/null
    [ -f "${PLUGIN_DIR}/data/ai_config.json" ] && chmod 600 "${PLUGIN_DIR}/data/ai_config.json" 2>/dev/null
    echo 'AI Chat Agent installed. Open the plugin, fill Settings (endpoint, model, API key), then chat or enable Agent mode.' > $install_tmp
}

do_uninstall()
{
    # Hentikan worker agent yang masih jalan (kalau ada), tanpa menghapus
    # data — panel sendiri yang menghapus folder plugin saat uninstall.
    pkill -f 'agent_worker.py' 2>/dev/null
    echo 'AI Chat Agent uninstalled. Note: panel removes the plugin folder, so data/ai_config.json goes with it — back it up first if you need it.' > $install_tmp
}

action="${1:-install}"
case "$action" in
    install|update) do_install ;;
    uninstall) do_uninstall ;;
    *) echo "Unknown action: $action (install|update|uninstall)" >&2; exit 2 ;;
esac
