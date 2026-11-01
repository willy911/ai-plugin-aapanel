# AI Chat Agent untuk aaPanel

Chat AI + Mode Agent ala Terwo, dipindah ke plugin aaPanel native (Python,
hanya standard library — jalan di pyenv aaPanel tanpa install paket tambahan).
Referensi struktur plugin: [Cloudflare Tunnel aaPanel](https://github.com/fdciabdul/Cloudflare-Tunnel-aaPanel)
(dispatcher tipis + manager terpisah + self-check di `install.sh`).

## Yang bisa dilakukan

- **Chat biasa** — kirim pesan ke endpoint OpenAI-compatible (mis. 9Router
  `https://ai.skyzo.biz.id/v1`), jawabannya kembali sebagai satu balasan.
- **Mode Agent (centang di UI)** — model bisa memakai tool, loop sampai
  selesai, persis seperti Mode Agent Terwo:
  - `list_dir`, `read_file`, `list_sites` → jalan otomatis.
  - `exec`, `write_file` → **selalu** minta approval Allow / Deny / Stop
    per tool. Tidak ada "Always Allow".
  - `list_sites` (tambahan khusus aaPanel, read-only) → daftar website dari
    database panel (`sites` + `domain`).
- **Pengaturan** — endpoint, model (Fetch Models dari `/v1/models` atau
  ketik manual), API key, dan agent root. Key disimpan di
  `data/ai_config.json` (chmod 600) dan **tidak pernah** dikembalikan ke
  browser — GET config hanya mengembalikan `has_key`. Kolom key kosong
  saat Save = pakai key tersimpan (pola Terwo).
- **Riwayat** — riwayat chat disimpan lokal di browser (seperti Terwo);
  riwayat run agent bisa dibuka ulang dari tombol "Riwayat run".

## Struktur

```
ai_chat_agent/                  # folder plugin (yang di-zip untuk Import aaPanel)
├── info.json                   # metadata panel (nama ai_chat_agent, v1.0.0)
├── install.sh                  # hook install/update/uninstall + self-check
├── icon.png                    # ikon tile plugin
├── ai_chat_agent_main.py       # dispatcher: /plugin?action=a&name=ai_chat_agent&s=<method>
├── ai_core.py                  # config, upstream OpenAI-compatible, tools, state run
├── agent_worker.py             # worker background per run (detached dari request panel)
├── index.html                  # UI native aaPanel (chat + approval + pengaturan)
└── data/                       # runtime, TIDAK ikut di-zip: ai_config.json, runs/
tests/                          # unittest stdlib (jalan di mana saja, tanpa pytest)
scripts/package.sh              # pembuat zip
docs/INSTALASI.md               # panduan pasang di VPS
```

## Kenapa agent di aaPanel tidak bisa pakai SSE seperti Terwo

Terwo (Node) bisa menahan satu request SSE selama agent berjalan, dengan
state run di memori dan approval lewat stream kedua. Plugin aaPanel
dipanggil panel lewat POST sinkron yang mengembalikan JSON — tidak ada
stream yang bisa ditahan begitu. Maka di sini:

1. `agent_start` membuat `data/runs/<id>.json` lalu men-spawn
   `agent_worker.py` secara detached (`start_new_session=True`).
2. Worker menulis progres (teks, tool, approval) ke run.json.
3. UI polling `agent_status` tiap 1,5 detik dan merender event baru.
4. Approval dikirim lewat `agent_approve` → file
   `data/runs/<id>.decision.json` yang dibaca worker tiap 0,5 detik.
5. `agent_stop` menulis decision `stop` + SIGTERM ke worker.

Efek samping yang bagus: run **tidak hilang** kalau jendela panel ditutup
atau panel di-restart (worker detached tetap jalan); kalau VPS restart,
worker mati dan status run ditandai `error` saat polling berikutnya,
bukan menggantung selamanya.

## Keamanan — baca sebelum pakai

Ini bagian paling penting, karena plugin jalan sebagai user panel
(biasanya **root**):

- **Approval adalah satu-satunya pengaman `exec`.** Tool file dikurung
  di agent root (default `/www/wwwroot`, bisa diubah di Pengaturan),
  tapi `exec` menjalankan `bash -c` — shell bisa `cd` ke mana saja,
  `rm -rf`, `curl | bash`, dan seterusnya. Setujui hanya perintah yang
  kamu pahami. Jangan pernah menempel perintah dari model tanpa membaca.
- **Tidak ada batas langkah** (mengikuti keputusan Terwo 2026-10-28:
  batas 12 langkah dihapus total karena terlalu kecil untuk tugas
  panjang). Kendalinya adalah tombol **Stop run**. Kalau model nyangkut
  mengulang perintah yang sama, tekan Stop — jangan biarkan jalan.
- **Batasan tool** (sama seperti Terwo): `exec` timeout 30 detik, output
  maks 20 KB; `read_file` maks 100 KB; `list_dir` maks 200 entri.
- **Key & file sensitif**: config chmod 600; output tool yang tampil di
  UI dipotong 2.000 karakter per event (file run lengkap tetap di server).
  Jangan suruh agent membaca/menulis file kredensial, dan jangan
  membagikan isi `data/ai_config.json`.
- **Upstream non-standar ditoleransi**: balasan JSON biasa, berbungkus
  `{"data": ...}`, SSE padahal `stream:false`, dan trailer
  `data: [DONE]` ala 9Router — semuanya diurai seperti di Terwo, bukan
  langsung gagal.

## Instalasi

Lihat [docs/INSTALASI.md](docs/INSTALASI.md). Ringkasnya: zip folder
`ai_chat_agent/` → aaPanel → App Store → Import → buka plugin → isi
Pengaturan → chat / centang Mode Agent.

## Build & test

```bash
./scripts/package.sh            # hasil: dist/ai_chat_agent-1.1.2.zip
python3 -m unittest discover -s tests -v   # tanpa pytest, stdlib saja
```

Test memakai upstream HTTP palsu lokal (bukan key asli): mencakup parsing
upstream 9Router, `safe_path` anti-traversal, tool file, `list_sites`
dari database panel palsu, run agent read-only sampai selesai, dan run
agent yang menunggu approval `exec` lalu Allow → perintah jalan.

## Versi

- **1.1.2** (2026-11-01) — sidebar session pakai ikon saja (✏️
  rename inline, 🗑️ hapus, ＋ chat baru, ↻ muat ulang) supaya tidak
  menutupi halaman; rename + hapus untuk session chat DAN run agent
  (run yang masih jalan tidak bisa dihapus — hentikan dulu).
- **1.1.1** (2026-11-01) — perbaikan instalasi parsial: `install.sh`
  kini memeriksa simbol wajib di `ai_core.py`/`ai_chat_agent_main.py`
  dan menggagalkan instalasi dengan pesan jelas bila ada file versi
  lama yang tertinggal (insiden VPS: main v1.1.0 + core v1.0.0);
  dispatcher juga mengembalikan error bersih, bukan crash
  `AttributeError`, bila core lama terdeteksi.
- **1.1.0** (2026-11-01) — 3 menu (Pengaturan / Chat / Agent) + sidebar
  daftar session (chat tersimpan di server, run agent dari backend);
  perbaikan 1010: User-Agent plugin sendiri, bukan Python-urllib yang
  diblokir Cloudflare Browser Integrity Check, plus pesan error 1010
  yang menjelaskan itu bukan salah key.
- **1.0.0** (2026-11-01) — rilis pertama: chat, agent 5 tool, approval
  per tool, polling run, riwayat run, Fetch Models, agent root.
