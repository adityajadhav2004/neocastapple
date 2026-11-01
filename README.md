# NeoCast
Stream movies, music, YouTube, live audio and your screen to an Apple TV 2 (software 4.4.4, AirPlay 1) from a **Windows PC or a Mac**. Everything is capped at 720p / 30fps; anything else is converted and cached.

## Install
- Windows: `winget install ffmpeg`, then in PowerShell: `py -3.12 -m venv .venv; .venv\Scripts\activate; pip install -r requirements.txt`
- Mac: `brew install ffmpeg`, then `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Windows firewall, admin PowerShell, once: `New-NetFirewallRule -DisplayName "NeoCast Media" -Direction Inbound -Protocol TCP -LocalPort 8787 -Action Allow -Profile Private`

## Run
    python neocast.py --host 192.168.1.108 portal     # UI at http://localhost:8080 (host is saved)
    python neocast.py video "D:/Movies/film.mkv"       # converts to 720p30 if needed, then plays
    python neocast.py audio song.mp3
    python neocast.py scan | info | devices | selftest
    python neocast.py live --device "BlackHole 2ch"    # Win default: "CABLE Output (VB-Audio Virtual Cable)"
    python neocast.py screen [--device 1]              # HLS, ~10s delay, no sound

The portal (`ui.html`, served by `neocast.py`) has a recent-media sidebar with thumbnails and progress, a drop zone, subtitle/audio/stream cards, and a player bar (±15s, pause, seek, CC, stop). Browsers can't reveal a dropped file's path, so a drop is matched by name and size against your library folders; otherwise use Select Video, which lists the library or opens a native file dialog (Windows needs Python's tkinter, included in python.org installs).

Library folders and Apple TV IP are editable in the portal's Settings (saved to `~/NeoCast/settings.json`). Defaults: `~/Videos` (Windows) or `~/Movies` (Mac), plus `~/Music`.

## Experimental / not built
- Live audio needs BlackHole (Mac) or VB-Cable (Windows). Screen share on Mac needs Screen Recording permission for your terminal; run `devices` to find the screen index.
- Video starts with pyatv `play_url` (verified on an Apple TV 2; a raw `POST /play` is accepted by the box but it never fetches the file). Position, pause (`/rate`), seek (`/scrub`) and resume use raw AirPlay 1 HTTP on port 7000, as in djaysan/airplay-drop; the calls return 200 and the position tracks on a real Apple TV 2, but check pause visually.
- Subtitle burn-in (tick the box in the portal, or `video --sub N`) needs an ffmpeg with libass; audio-track picker via `--audio-track N`.
- Not built: volume, playlists, tray icon, thumbnails, job cancel (Stop kills playback and screen/live capture, not an in-progress conversion).
- Untested against real hardware: AirPlay playback, live audio, screen share.

## Troubleshooting
| Symptom | Fix |
|---|---|
| "Apple TV never fetched the media" | Firewall on port 8787 (see above); PC and Apple TV on same network |
| "Apple TV not found" | Check IP; run `python neocast.py scan` |
| "ffmpeg not found" | Install ffmpeg and reopen the terminal |
| yt-dlp failed | `pip install -U yt-dlp` |
