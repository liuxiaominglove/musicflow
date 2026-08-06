# AGENTS.md - MusicFlow

## Run Commands

```bash
# Dev mode (Flask only, no Electron shell)
python3 server.py 18080

# Electron app
npx electron .                           # run
npx electron-builder --mac               # package (untested; no builder config in repo)

# Disable schedule checker on startup
python3 server.py 18080 --no-schedule
```

## Architecture

- **Backend**: `server.py` — Flask app (port 18080), VLC playback, YouTube/Bilibili search+download via `yt-dlp`, Shazam recognition via `shazamio`, built-in schedule checker
- **Desktop shell**: `main.js` + `preload.js` — Electron wrapper that spawns `server.py` as a child process and points a BrowserWindow at it
- **Frontend**: `static/index.html` (desktop), `static/mobile.html` (mobile remote control), `static/login.html` — vanilla JS/HTML/CSS, no framework

## Key Conventions & Gotchas

### Schedule system
`data/schedule.json` managed by REST API in `server.py`. Enabled by default, disable with `--no-schedule`.

### Path resolution in Electron
- `server.py` uses `MUSICFLOW_HOME` env var (set by `main.js`) to locate the writable data directory. Falls back to `__file__` parent in dev mode.
- `main.js` distinguishes `RESOURCES_DIR` (read-only source) from `DATA_DIR` (writable data). The `data/` subdirectory and `music_library/` live under `DATA_DIR`.

### macOS PATH fix
`server.py:17-19` prepends `/usr/local/bin` and `/opt/homebrew/bin` to `PATH` because Electron child processes have a stripped PATH that can't find `yt-dlp`.

### VPN proxy
`server.py:_get_proxy()` auto-detects Upnet proxy on `127.0.0.1:29758`. If unreachable, falls back to direct connection.

### Auth model
- Requests from `127.0.0.1` with no Cloudflare/X-Forwarded-For headers bypass auth entirely
- Remote requests (mobile, Cloudflare Tunnel) require token via URL param or cookie
- Admin-only endpoints (download, delete, rename, tag create, schedule write, shutdown) also check `_check_admin()` — remote users are never admin even with valid token

### Secret key
Generated on first run into `data/.secret_key` (gitignored). Printed to console on startup.

### Music library structure
`music_library/{tag_name}/{file.mp3}` — tags are subdirectories. `songs.json` is a lightweight download manifest (not used by the app, just for reference).

### VLC and Mutagen are optional
`VLC_AVAILABLE` / `MUTAGEN_AVAILABLE` flags — playback and ID3 reading gracefully degrade if dependencies missing.

### Electron quirks
- `main.js` suppresses EIO/EPIPE errors (terminal closed while app is running)
- Closing the window hides to system tray; quitting requires tray menu or Cmd+Q
- Flask crash auto-restarts after 10 seconds
- Flask startup timeout is 60 seconds

### The `current.yaml.template` file
Not a real template — it's a UI structure dump/snapshot, not used by any code. Do not modify.

### No tests, no lint, no CI
The repo has no test suite, no linter config, no CI workflows.

## File Purposes (non-obvious)

| File | Purpose |
|------|---------|
| `songs.json` | Manual download manifest (human-maintained song list for re-download) |
| `config.yaml` | Deprecated — was used by old `scheduler.py` (removed). Ignored by `server.py`. |
| `current.yaml.template` | UI snapshot, not a config template |
| `current.yaml.template` | UI snapshot, not a config template |
| `data/ratings.json` | Song ratings (0-3 stars), gitignored |
| `data/song_meta.json` | User-corrected song name/artist overrides, gitignored |
| `data/schedule.json` | Web UI schedule config, gitignored |
| `data/.secret_key` | Auth token, gitignored |
