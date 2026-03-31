# Vinyl Player

Web-based music player with vinyl record visualization, multi-user support, LAN/WAN access, and metadata management.

## Features

- Vinyl record player with spinning animation, tonearm tracking, and scratch sound
- Album cover extraction and adaptive background colors
- Multi-user system with admin panel, PBKDF2 password hashing, rate limiting
- LAN mode (local network access) and WAN mode (Cloudflare Tunnel or static IP)
- Music metadata search via Deezer, iTunes, Last.fm, MusicBrainz
- VK Music playlist downloader integration
- Drag-and-drop track reordering for numbered catalogs
- Album grouping with cover flow
- Shuffle mode and search
- Media Session API for lock screen controls (iOS/Android)
- PWA support (add to home screen on iOS)
- Responsive mobile layout
- Track prefetching for gapless playback

## Requirements

- Python 3.8+
- Required: `httpx`, `mutagen`
- Optional: `vkpymusic` (VK Music download), `musicbrainzngs` (MusicBrainz metadata), `cloudflared` (WAN tunnel)

## Install

```bash
pip install httpx mutagen
# Optional
pip install vkpymusic musicbrainzngs
```

### Cloudflare Tunnel (optional, for WAN mode)

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

**Linux (any, binary):**
```bash
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
```

**Windows:**
```powershell
winget install Cloudflare.cloudflared
# or download from https://github.com/cloudflare/cloudflared/releases
```

## Run

```bash
python3 vinyl_player.py
```

Opens in browser at `http://127.0.0.1:7656`. On first launch, create an admin account.

### LAN mode

Enable LAN toggle in the UI (admin only) to allow access from other devices on the local network.

### WAN mode

Enable WAN toggle and choose:
- **Cloudflare Tunnel** — automatic HTTPS tunnel, no static IP needed
- **Static IP / VPS** — direct access by IP address

### Deploy on VPS

```bash
# Install dependencies
pip3 install httpx mutagen musicbrainzngs

# Run with public access
python3 vinyl_player.py --public
```

**systemd service** (`/etc/systemd/system/vinyl-player.service`):
```ini
[Unit]
Description=Vinyl Player
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

# Open firewall
sudo ufw allow 7656/tcp
```

## Security

- Passwords hashed with PBKDF2-SHA256 (260k iterations)
- VK tokens stored only in memory, never on disk
- Rate limiting on login (per-IP, per-user, global)
- Path traversal protection
- HttpOnly session cookies
- File permissions 600 on sensitive files

## License

MIT
