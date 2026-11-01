# Instalasi AI Chat Agent di VPS aaPanel

## 1. Pasang dari zip

1. Ambil `dist/ai_chat_agent-1.1.2.zip` dari proyek ini.
2. aaPanel → **App Store** → tombol **Import** (kanan atas) → pilih zip.
3. Tunggu sampai muncul "installed". Kalau gagal dengan pesan
   "missing/empty files", ulangi Import — itu self-check `install.sh`
   yang menolak paket parsial (pola plugin Cloudflare Tunnel).
4. Buka plugin dari daftar plugin. Kalau UI lama yang tampil setelah
   update, restart panel: `bt restart` (atau dari menu panel).

## 2. Isi Pengaturan (sekali saja)

- **Endpoint** — URL OpenAI-compatible, mis. `https://ai.skyzo.biz.id/v1`
  (akhiran `/v1` ditambahkan otomatis kalau belum ada).
- **Model** — klik **Fetch Models** lalu pilih, atau ketik manual
  (mis. model function-calling; chat biasa jalan di model apa saja, tapi
  Mode Agent **wajib** model yang mendukung function calling/tool calls).
- **API key** — ditempel sekali, disimpan chmod 600, tidak pernah
  ditampilkan lagi. Mengosongkan kolom saat Save = pakai key lama.
- **Agent root** — default `/www/wwwroot`. Tool file (`read_file`,
  `write_file`, `list_dir`) hanya boleh di dalam sini. Kosongkan untuk
  kembali ke default.

## 3. Pakai

- **Chat biasa**: tanpa centang Mode Agent, kirim pesan, jawaban kembali
  sebagai satu balasan.
- **Mode Agent**: centang Mode Agent, lalu suruh, mis. "daftarkan site
  apa saja yang ada di panel ini" atau "lihat isi folder site X dan
  ringkas file konfigurasinya". Tool baca jalan otomatis; setiap
  `exec`/`write_file` muncul kotak approval — klik **Allow** (sekali,
  untuk tool itu saja), **Deny** (model diberi tahu ditolak dan boleh
  mencoba pendekatan lain), atau **Stop run**.
- **Riwayat run**: tombol "Riwayat run" → "Lihat" untuk membuka ulang
  run lama; run yang masih jalan bisa dilanjutkan polling-nya dari sini
  (berguna kalau jendela panel sempat ditutup — worker tetap jalan).

## 4. Verifikasi cepat

1. Badge di header harus **siap** (hijau) setelah Pengaturan disimpan.
2. Chat biasa: kirim "halo" → balasan masuk.
3. Mode Agent: kirim "list site di panel ini" → harus muncul tool
   `list_sites` tanpa approval (tool baca), lalu ringkasan.
4. Mode Agent: kirim "jalankan `pwd` di agent root" → harus muncul
   kotak approval; klik Deny → agent melaporkan ditolak dan tidak ada
   perintah yang jalan.

## 5. Update & uninstall

- **Update**: Import zip versi baru lewat App Store (menimpa file plugin,
  config di `data/` dipertahankan karena zip tidak berisi `data/`).
  Restart panel (`bt restart`) bila perlu.
- **Uninstall**: panel menghapus folder plugin — termasuk
  `data/ai_config.json` dan riwayat run. Backup dulu bila perlu:
  `cp /www/server/panel/plugin/ai_chat_agent/data/ai_config.json ~/ai_config.backup.json`
  (jaga file backup itu seperti password).

## Troubleshooting

- **AttributeError: module 'ai_core' has no attribute 'list_chat_sessions'**
  (atau error serupa) → instalasi parsial: Import panel menimpa
  `ai_chat_agent_main.py` baru tapi `ai_core.py` lama masih tertinggal
  (terverifikasi di VPS 2026-11-01: zip 1.1.0 benar, core VPS masih
  v1.0.0 484 baris). Perbaiki bertahap, tanpa menghapus config:
  1. `bt restart` dulu, coba lagi — bisa jadi hanya modul lama yang
     masih tersimpan di memori proses panel.
  2. Bila masih error, cek di VPS:
     `grep -c "def list_chat_sessions" /www/server/panel/plugin/ai_chat_agent/ai_core.py`
     — hasilnya harus `1`; kalau `0`, file di disk memang lama.
  3. Timpa paksa (config di `data/` tidak ikut tertimpa karena zip
     tidak berisi `data/`):
     `unzip -o ai_chat_agent-1.1.2.zip -d /www/server/panel/plugin/ai_chat_agent/ && rm -rf /www/server/panel/plugin/ai_chat_agent/__pycache__ && bt restart`
  Sejak v1.1.1, `install.sh` memeriksa simbol wajib dan akan menggagalkan
  instalasi parsial dengan pesan jelas, bukan membiarkannya crash di UI.


- **"AI belum diatur"** → isi endpoint + model + key, klik Simpan.
- **Agent tidak memanggil tool** → model tidak mendukung function
  calling; ganti model.
- **"Access outside working directory denied"** → path di luar agent
  root; ubah agent root di Pengaturan atau minta path di dalamnya.
- **Run berstatus error "Worker berhenti tak terduga"** → VPS/panel
  restart saat run berjalan; mulai run baru.
- **Tool `exec` terasa menggantung** → perintah dibatasi 30 detik lalu
  dipotong; pecah perintah jadi langkah kecil, atau tekan Stop run.
