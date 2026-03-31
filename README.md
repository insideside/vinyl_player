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
brew install cloudflared  # macOS, for WAN tunnel
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
pip install httpx mutagen musicbrainzngs

# Run (optionally with --public for immediate LAN access)
python3 vinyl_player.py --public

# Or use systemd service
# Create /etc/systemd/system/vinyl-player.service
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
