# insideside music

Web-based music player with vinyl record visualization, multi-user support, LAN/WAN access, and metadata management.

## Features

- Vinyl record player with spinning animation, tonearm tracking, and scratch sound
- Album cover extraction and adaptive animated background
- Multi-user system (admin/user/demo roles) with PBKDF2 password hashing, rate limiting
- LAN mode (local network) and WAN mode (Cloudflare Tunnel or static IP)
- Music metadata search via Deezer, iTunes, Genius, Last.fm, MusicBrainz
- Multi-platform playlist import: VK, Yandex Music, Spotify, Apple Music, SoundCloud
- VK Music playlist downloader and track search
- Drag-and-drop track reordering for numbered catalogs
- Per-track editing (rename, reorder, metadata)
- Album grouping with cover flow
- Shuffle mode and search
- Media Session API for lock screen controls (iOS/Android)
- PWA support (add to home screen on iOS)
- Responsive mobile layout with portrait lock
- MUSIC_ROOT sandbox for non-admin users
- Catalog download as ZIP (admin)

## Requirements

- Python 3.8+
- Required: `httpx`, `mutagen`
- Optional: `vkpymusic` (VK Music), `musicbrainzngs` (MusicBrainz metadata)

## Quick Start

### macOS / Linux

```bash
pip3 install httpx mutagen vkpymusic musicbrainzngs
python3 vinyl_player.py
```

### Windows

```bash
pip install httpx mutagen vkpymusic musicbrainzngs
python vinyl_player.py
```

### With LAN access (all OS)

```bash
python3 vinyl_player.py --public
```

Opens in browser at `http://127.0.0.1:7656`. On first launch, create an admin account.

## Windows Server — One-Click Install

Download and run `install_server_win.bat` as Administrator. It will:

1. Install Python 3.11 (if not present)
2. Install all pip dependencies
3. Create `C:\insideside-music\` directory
4. Download the app and cloudflared
5. Open firewall port 7656
6. Set up auto-start on Windows logon
7. Show IP addresses for remote access

After install: `C:\insideside-music\start_server.bat`

## Cloudflare Tunnel (optional, for WAN mode)

**macOS:**
```bash
brew install cloudflared
```

**Linux (Debian/Ubuntu):**
```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared
```

**Linux (binary):**
```bash
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
```

**Windows:**
```powershell
winget install Cloudflare.cloudflared
```

## Build Installers

### macOS (.dmg)
```bash
bash build_mac.sh
# Output: dist/VinylPlayer-macOS.dmg
```

### Windows (.exe)
```bash
build_win.bat
# Output: dist\VinylPlayer.exe
```

### Linux (.deb)
```bash
bash build_linux.sh
# Output: dist/vinyl-player_1.0.0_amd64.deb
# Install: sudo dpkg -i dist/vinyl-player_*.deb
# Run: vinyl-player
```

All installers include cloudflared and all Python dependencies bundled.

## Deploy on VPS (Linux)

```bash
pip3 install httpx mutagen vkpymusic musicbrainzngs
python3 vinyl_player.py --public
```

**systemd service** (`/etc/systemd/system/vinyl-player.service`):
```ini
[Unit]
Description=insideside music
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/vinyl-player
ExecStart=/usr/bin/python3 vinyl_player.py --public
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable vinyl-player
sudo systemctl start vinyl-player
sudo ufw allow 7656/tcp
```

## LAN / WAN Access

| Mode | How | Who |
|---|---|---|
| Local | `python3 vinyl_player.py` | Only localhost |
| LAN | Toggle in UI (admin) | All devices on local network |
| WAN Tunnel | Toggle in UI → Cloudflare Tunnel | Anyone with the URL |
| WAN Static | Toggle in UI → Static IP | Anyone with IP:port |

WAN static IP config persists across restarts.

## User Roles

| | Admin | User | Demo |
|---|---|---|---|
| Listen to music | yes | yes | yes |
| Own folders | any path | within MUSIC_ROOT | fixed `_demo/` |
| Add folders | yes | yes | no |
| Metadata search | yes | yes | no |
| VK / Import | yes | yes | no |
| Reorder tracks | yes | yes | no |
| LAN / WAN | yes | no | no |
| Manage users | yes | no | no |
| Download catalog | yes | no | no |

## Security

- Passwords: PBKDF2-SHA256 (260k iterations) with per-user salt
- VK tokens: stored only in memory, never on disk
- Sessions: persistent across restarts, HttpOnly + SameSite=Strict cookies
- Rate limiting: per-IP (5/5min), per-user (5/5min), global (20/5min)
- Path traversal protection on all file endpoints
- MUSIC_ROOT sandbox for non-admin users
- Security headers: X-Frame-Options, X-Content-Type-Options, X-XSS-Protection
- File permissions 600 on sensitive files
- All admin endpoints verified server-side

## License

MIT
