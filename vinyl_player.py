#!/usr/bin/env python3
"""
Vinyl Record Music Player
Веб-плеер с визуализацией виниловой пластинки.
Запускается как localhost в браузере.
"""

import base64
import json
import math
import mimetypes
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote, quote

# Supported Python range — outside this, some deps have no prebuilt wheels
# (notably cryptography on Windows, vkpymusic transitive deps on 3.14+).
# We don't refuse to run, just warn — the app degrades gracefully via HAS_* flags.
_PY_MIN = (3, 9)
_PY_MAX_TESTED = (3, 13)
if sys.version_info < _PY_MIN or sys.version_info[:2] > _PY_MAX_TESTED:
    print(
        "[!] Python {}.{} обнаружен. Поддерживается {}.{}–{}.{}. "
        "Часть зависимостей может не установиться (cryptography, vkpymusic).".format(
            sys.version_info.major, sys.version_info.minor,
            _PY_MIN[0], _PY_MIN[1], _PY_MAX_TESTED[0], _PY_MAX_TESTED[1]
        ),
        file=sys.stderr,
    )

try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TCON, TRCK, TDRC, APIC, ID3NoHeaderError
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggvorbis import OggVorbis
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

try:
    import musicbrainzngs
    musicbrainzngs.set_useragent("VinylPlayer", "1.0", "https://github.com/vinyl-player")
    HAS_MB = True
except ImportError:
    HAS_MB = False

try:
    from httpx import Client as HttpClient
except ImportError:
    sys.stderr.write(
        "[!] Не установлен httpx — без него приложение работать не может.\n"
        "    Установите: python -m pip install httpx\n"
    )
    sys.exit(1)

try:
    from vkpymusic import Service as VkService
    HAS_VK = True
except ImportError:
    HAS_VK = False

import hashlib
import hmac
import secrets
import http.cookies

SERVER_PORT = 7656     # LAN/WAN: HTTPS on 0.0.0.0 when public (phones connect here — keep stable)
LOCAL_PORT = 7666      # localhost: always plain HTTP on 127.0.0.1 (the Mac app/widget use this)
_user_music_dirs = {}  # username -> current MUSIC_DIR
USERS_FILE = Path.home() / ".vinyl_users.json"
SETTINGS_FILE = Path.home() / ".vinyl_settings.json"
VK_APP_ID = 2685278
VK_USER_AGENT = "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)"
IS_PUBLIC = False

SUPPORTED_FORMATS = {'.mp3', '.flac', '.m4a', '.ogg', '.wav', '.aac', '.opus', '.aiff', '.aif', '.alac'}

# Explicit Content-Type per extension. Mobile browsers (iOS Safari especially)
# are strict about the audio MIME type and refuse to play with a wrong/guessed
# one, so we don't rely on mimetypes.guess_type for these.
AUDIO_MIME = {
    '.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.m4a': 'audio/mp4',
    '.aac': 'audio/aac', '.ogg': 'audio/ogg', '.opus': 'audio/ogg',
    '.wav': 'audio/wav', '.aiff': 'audio/aiff', '.aif': 'audio/aiff',
    '.alac': 'audio/mp4',
}

# ──────────────────── Desktop widget bridge ────────────────────
# In-memory bridge between the browser player and an external desktop widget
# (Übersicht). The browser POSTs its now-playing state; the widget polls it.
# The widget POSTs control commands; the browser polls and executes them.
# Localhost-only — never exposed over LAN/WAN.
_widget_state = {
    "playing": False, "title": "", "artist": "", "album": "",
    "file": "", "position": 0, "duration": 0, "ts": 0,
}
_widget_command = None  # pending command for the browser to execute
_widget_seen = 0.0      # когда виджет последний раз читал состояние
# Виджет — это Übersicht, а он существует только под macOS. На Windows и Linux
# опрашивать нечего, и браузеру об этом лучше сказать сразу, чем гонять запросы
# в пустоту.
WIDGET_POSSIBLE = (platform.system() == "Darwin")
WIDGET_ALIVE_SEC = 30
_widget_lock = threading.Lock()

# Native file-picker results, keyed by username. The picker blocks until the
# user chooses files, so it runs in a background thread and the browser polls
# for the result — the single-threaded HTTP server must not block (audio
# streaming and status polling would stall otherwise).
_local_pick = {}
_local_pick_lock = threading.Lock()

# ──────────────────── User system ────────────────────

SESSIONS_FILE = Path.home() / ".vinyl_sessions.json"
_sessions = {}  # token -> username
_login_attempts_ip = {}    # ip -> (count, last_time)
_login_attempts_user = {}  # username -> (count, last_time)
_LOGIN_MAX_IP = 5
_LOGIN_MAX_USER = 5
_LOGIN_WINDOW = 300  # 5 minutes
_GLOBAL_FAIL_COUNT = 0
_GLOBAL_FAIL_TIME = 0
_GLOBAL_MAX = 20  # max 20 failures total across all IPs/users in window


def _hash_pw(password, salt=None):
    """PBKDF2-SHA256, 260k iterations (OWASP 2024 recommendation)."""
    if not salt:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 260000).hex()
    return salt + ":" + h


def _check_pw(password, stored):
    if ":" not in stored:
        return False
    salt, expected_hash = stored.split(":", 1)
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 260000).hex()
    return hmac.compare_digest(actual, expected_hash)


def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2))


def get_music_root():
    s = load_settings()
    root = s.get("music_root", "")
    if not root:
        root = str(Path.home() / "VinylMusic")
    return root


def set_music_root(path):
    s = load_settings()
    s["music_root"] = path
    save_settings(s)
    Path(path).mkdir(parents=True, exist_ok=True)


def load_users():
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text())
        except Exception:
            pass
    return {}


_users_lock = threading.Lock()

def save_users(users):
    with _users_lock:
        USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2))
        try:
            USERS_FILE.chmod(0o600)
        except Exception:
            pass


def create_user(username, password, is_admin=False, role="user"):
    """role: 'admin', 'user', 'demo'"""
    users = load_users()
    if username in users:
        return False
    music_root = get_music_root()
    if role == "demo":
        # Demo users share a common demo folder
        user_folder = str(Path(music_root) / "_demo")
    else:
        user_folder = str(Path(music_root) / username)
    Path(user_folder).mkdir(parents=True, exist_ok=True)
    users[username] = {
        "password": _hash_pw(password),
        "is_admin": is_admin,
        "role": role,  # 'admin', 'user', 'demo'
        "folders": [user_folder],
    }
    save_users(users)
    return True


def authenticate_user(username, password):
    users = load_users()
    u = users.get(username)
    if not u:
        return False
    return _check_pw(password, u["password"])


def _load_sessions():
    global _sessions
    if SESSIONS_FILE.exists():
        try:
            data = json.loads(SESSIONS_FILE.read_text())
            # Only load sessions for users that still exist
            users = load_users()
            _sessions = {k: v for k, v in data.items() if v in users}
        except Exception:
            pass


def _save_sessions():
    try:
        SESSIONS_FILE.write_text(json.dumps(_sessions, ensure_ascii=False))
        SESSIONS_FILE.chmod(0o600)
    except Exception:
        pass


def create_session(username):
    token = secrets.token_hex(32)
    _sessions[token] = username
    _save_sessions()
    return token


def get_session_user(token):
    user = _sessions.get(token)
    if user is None and token:
        # Cookie miss: another process may have written a new session to disk
        # since we loaded. Re-read once and retry — protects against multi-process
        # drift (e.g. tunnel restart, stale dev process) without forcing relogin.
        _load_sessions()
        user = _sessions.get(token)
    return user


def get_user_data(username):
    users = load_users()
    return users.get(username)


def get_user_folders(username):
    users = load_users()
    u = users.get(username)
    if not u:
        return []
    if u.get("is_admin"):
        # Admin sees all folders from all users
        all_folders = []
        seen = set()
        for uname, udata in users.items():
            for f in udata.get("folders", []):
                if f not in seen:
                    all_folders.append(f)
                    seen.add(f)
        return all_folders
    return u.get("folders", [])


def is_path_within(path, root):
    """Check if path is inside root directory."""
    try:
        return str(Path(path).resolve()).startswith(str(Path(root).resolve()))
    except Exception:
        return False


def add_user_folder(username, folder):
    users = load_users()
    u = users.get(username)
    if not u:
        return False
    # Non-admins can only add folders inside MUSIC_ROOT
    if not u.get("is_admin"):
        music_root = get_music_root()
        if not is_path_within(folder, music_root):
            return False
    if folder not in u["folders"]:
        u["folders"].append(folder)
        save_users(users)
    return True


def remove_user_folder(username, folder):
    users = load_users()
    u = users.get(username)
    if u and folder in u["folders"]:
        u["folders"].remove(folder)
        save_users(users)


# VK tokens stored only in memory, per-user, never persisted to disk
_vk_tokens = {}  # username -> token


def get_user_vk_token(username):
    return _vk_tokens.get(username)


def set_user_vk_token(username, token):
    if token:
        _vk_tokens[username] = token
    else:
        _vk_tokens.pop(username, None)


def _safe_path(base_dir, filename):
    """Prevents path traversal — returns resolved path only if within base_dir."""
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    if not str(target).startswith(str(base) + os.sep) and target != base:
        return None
    return target


def get_user_last_folder(username):
    users = load_users()
    u = users.get(username)
    return u.get("last_folder", "") if u else ""


def set_user_last_folder(username, folder):
    users = load_users()
    u = users.get(username)
    if u:
        u["last_folder"] = folder
        save_users(users)

# ──────────────────── VK Download ────────────────────

_vk_states = {}  # username -> state dict

def get_vk_state(username=""):
    if username not in _vk_states:
        _vk_states[username] = {
            "service": None, "running": False, "cancel": False,
            "progress": 0, "total": 0, "log": [], "done": False,
        }
    return _vk_states[username]


def vk_load_token():
    return None  # Now per-user, loaded via get_user_vk_token


def vk_save_token(token):
    pass  # Now per-user, saved via set_user_vk_token


def vk_validate_token(token):
    try:
        svc = VkService(VK_USER_AGENT, token)
        svc.get_popular(count=1)
        return True
    except Exception as e:
        err = str(e).lower()
        # Captcha = token works but VK wants verification, accept it
        if "captcha" in err:
            print("VK: captcha requested, token accepted anyway")
            return True
        # Token expired or invalid
        if "access_token" in err or "authorization" in err:
            print("VK token invalid:", str(e)[:100])
            return False
        # Other errors (network, etc) — accept token optimistically
        print("VK validation warning:", str(e)[:100])
        return True


def vk_parse_playlist_url(url):
    m = re.search(r"music/playlist/(-?\d+)_(\d+)_([a-f0-9]+)", url)
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3)


def vk_get_all_songs(service, owner_id, playlist_id, access_key):
    all_songs = []
    offset = 0
    while True:
        songs = service.get_songs_by_playlist_id(
            user_id=owner_id, playlist_id=playlist_id,
            access_key=access_key, count=100, offset=offset)
        if not songs:
            break
        all_songs.extend(songs)
        if len(songs) < 100:
            break
        offset += 100
        time.sleep(0.3)
    return all_songs


def vk_safe_filename(s):
    s = re.sub(r'[<>:"/\\|?*]', '', s)
    s = s.strip('. ')
    return s if s else 'unknown'


def vk_download_song(song, filepath):
    url = song.url
    if not url or "index.m3u8" in url:
        return False
    try:
        with HttpClient(timeout=60) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return False
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_bytes(resp.content)
        return True
    except Exception:
        return False


def vk_search_fallback(service, artist, title, filepath):
    try:
        results = service.search_songs_by_text(artist + " " + title, count=5)
    except Exception:
        return False
    for r in results:
        if r.url and "index.m3u8" not in r.url:
            if vk_download_song(r, filepath):
                return True
    return False


def vk_get_existing_tracks(folder):
    # All supported audio formats, not just mp3 — local imports can be flac/m4a/
    # etc., and missing them here breaks renumbering (each batch restarted at 1).
    tracks = []
    for f in Path(folder).iterdir():
        if not f.is_file() or f.suffix.lower() not in SUPPORTED_FORMATS:
            continue
        m = re.match(r'^(\d+)\.\s+(.+)$', f.stem)
        if m:
            tracks.append((int(m.group(1)), m.group(2), f))
    tracks.sort(key=lambda x: x[0])
    return tracks


def vk_renumber_tracks(folder, start_from):
    tracks = vk_get_existing_tracks(folder)
    if not tracks:
        return
    total_n = start_from + len(tracks) - 1
    pad = len(str(total_n))
    for i in reversed(range(len(tracks))):
        _, name, old_path = tracks[i]
        new_num = str(start_from + i).zfill(pad)
        new_path = old_path.parent / (new_num + ". " + name + old_path.suffix)
        if old_path != new_path:
            old_path.rename(new_path)


def vk_repad_tracks(folder):
    tracks = vk_get_existing_tracks(folder)
    if not tracks:
        return
    mx = max(t[0] for t in tracks)
    pad = len(str(mx))
    for num, name, old_path in tracks:
        new_path = old_path.parent / (str(num).zfill(pad) + ". " + name + old_path.suffix)
        if old_path != new_path:
            old_path.rename(new_path)


# ──────────────────── Playlist Parsers ────────────────────

def parse_yandex_playlist(url):
    """Парсит публичный плейлист Яндекс.Музыки через API."""
    try:
        import json as _json
        # Extract UUID or owner/kind from URL
        m = re.search(r'/playlists/([a-f0-9-]{36})', url)
        if m:
            # UUID format — use direct API
            uuid = m.group(1)
            api_url = "https://api.music.yandex.net/playlist/{}".format(uuid)
        else:
            # users/LOGIN/playlists/KIND format
            m = re.search(r'/users/([^/]+)/playlists/(\d+)', url)
            if not m:
                return None
            api_url = "https://api.music.yandex.net/users/{}/playlists/{}".format(m.group(1), m.group(2))

        with HttpClient(timeout=15) as client:
            resp = client.get(api_url, headers={"User-Agent": "Yandex-Music-API"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        result = data.get("result", {})
        tracks_raw = result.get("tracks", [])
        tracks = []
        for t in tracks_raw:
            track = t.get("track", t)
            title = track.get("title", "")
            artists = track.get("artists", [])
            artist = artists[0].get("name", "") if artists else ""
            if title:
                tracks.append({"artist": artist, "title": title})
        return tracks if tracks else None
    except Exception:
        return None


def parse_spotify_playlist(url):
    """Парсит публичный плейлист Spotify. Embed = max 50, direct page = more."""
    try:
        import json as _json
        m = re.search(r'playlist/([a-zA-Z0-9]+)', url)
        if not m:
            return None
        playlist_id = m.group(1)
        # Method 1: Embed (reliable, max 50)
        embed_url = "https://open.spotify.com/embed/playlist/{}".format(playlist_id)
        with HttpClient(timeout=15, follow_redirects=True) as client:
            resp = client.get(embed_url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        m2 = re.search(r'<script[^>]*type="application/json"[^>]*>(.+?)</script>', resp.text)
        if not m2:
            return None
        data = _json.loads(m2.group(1))
        entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
        track_list = entity.get("trackList", [])
        tracks = [{"artist": t.get("subtitle", ""), "title": t.get("title", "")} for t in track_list if t.get("title")]
        # Method 2: Direct page (more tracks via regex)
        if len(tracks) >= 48:  # likely truncated at 50
            try:
                with HttpClient(timeout=15, follow_redirects=True) as client:
                    resp2 = client.get("https://open.spotify.com/playlist/{}".format(playlist_id),
                        headers={"User-Agent": "Mozilla/5.0"})
                if resp2.status_code == 200:
                    # Extract all title+subtitle pairs
                    pairs = re.findall(r'"title":"((?:[^"\\]|\\.)+)","subtitle":"((?:[^"\\]|\\.)+)"', resp2.text)
                    if len(pairs) > len(tracks):
                        tracks = [{"artist": a, "title": t} for t, a in pairs]
            except Exception:
                pass
        return tracks if tracks else None
    except Exception:
        return None


def parse_apple_playlist(url):
    """Парсит публичный плейлист Apple Music."""
    try:
        with HttpClient(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        html = resp.text
        artist_names = re.findall(r'"artistName":"((?:[^"\\]|\\.)+)"', html)
        all_names = re.findall(r'"name":"((?:[^"\\]|\\.)+)"', html)
        if not artist_names:
            return None
        # Deduplicate names (each appears twice), skip UI labels
        skip = set()
        # Detect playlist title/name to skip
        m = re.search(r'<title>([^<]+)</title>', html)
        if m:
            for part in m.group(1).replace('—', '-').split(' - '):
                skip.add(part.strip())
        # Extract playlist name from og:title or title (inside «» or quotes)
        for attr in ['og:title', 'og:description']:
            m2 = re.search(r'property="' + attr + '"[^>]*content="([^"]+)"', html)
            if m2:
                # Extract name from «Name» pattern
                m3 = re.search(r'[«"](.*?)[»"]', m2.group(1))
                if m3:
                    skip.add(m3.group(1).strip())
        # Common UI labels to skip
        for label in ['Подборка', 'Прослушать отрывки', 'Apple Music', 'Плейлист']:
            skip.add(label)
        song_names = []
        prev = ''
        for n in all_names:
            if n == prev or n in skip:
                prev = n
                continue
            song_names.append(n)
            prev = n
        # Pair artists with song names
        tracks = []
        for i in range(len(artist_names)):
            title = song_names[i] if i < len(song_names) else ""
            if title:
                tracks.append({"artist": artist_names[i], "title": title})
        return tracks if tracks else None
    except Exception:
        return None


def parse_soundcloud_playlist(url):
    """Парсит публичный плейлист SoundCloud."""
    try:
        with HttpClient(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        html = resp.text
        import json as _json
        # SoundCloud stores data in window.__sc_hydration
        m = re.search(r'window\.__sc_hydration\s*=\s*(\[.+?\]);\s*<', html, re.DOTALL)
        if m:
            data = _json.loads(m.group(1))
            tracks = []
            for item in data:
                d = item.get("data", {})
                if d.get("kind") == "playlist":
                    for t in d.get("tracks", []):
                        artist = t.get("user", {}).get("username", "")
                        tracks.append({"artist": artist, "title": t.get("title", "")})
                elif d.get("kind") == "track":
                    tracks.append({"artist": d.get("user", {}).get("username", ""), "title": d.get("title", "")})
            if tracks:
                return tracks
        # Fallback: meta tags
        titles = re.findall(r'"title":"([^"]+)"', html)
        if titles:
            return [{"artist": "", "title": t} for t in titles[:50]]
        return None
    except Exception:
        return None


def parse_external_playlist(url):
    """Определяет платформу по URL и парсит плейлист."""
    url_lower = url.lower()
    if "music.yandex" in url_lower:
        return parse_yandex_playlist(url), "yandex"
    elif "spotify.com" in url_lower:
        return parse_spotify_playlist(url), "spotify"
    elif "music.apple.com" in url_lower:
        return parse_apple_playlist(url), "apple"
    elif "soundcloud.com" in url_lower:
        return parse_soundcloud_playlist(url), "soundcloud"
    return None, "unknown"


def vk_download_worker(urls, folder, order, mode, run_meta_after, username=""):
    vk_state = get_vk_state(username)
    vk_state["running"] = True
    vk_state["done"] = False
    vk_state["cancel"] = False
    vk_state["log"] = []
    vk_state["progress"] = 0
    vk_state["total"] = 0

    try:
        service = vk_state["service"]
        save_dir = Path(folder)
        save_dir.mkdir(parents=True, exist_ok=True)

        all_tracks = []
        total_pl = len(urls)

        for i, url in enumerate(reversed(urls)):
            if vk_state["cancel"]:
                break
            pl_num = total_pl - i
            parsed = vk_parse_playlist_url(url)
            if not parsed:
                vk_state["log"].append("Ошибка URL: " + url)
                continue
            owner_id, playlist_id, access_key = parsed
            vk_state["log"].append("[{}/{}] Загружаю список треков...".format(pl_num, total_pl))
            try:
                songs = vk_get_all_songs(service, owner_id, playlist_id, access_key)
            except Exception as e:
                if "captcha" in str(e).lower():
                    vk_state["log"].append("  VK включил captcha. Подождите 15 минут и повторите.")
                    break
                vk_state["log"].append("  Ошибка: " + str(e)[:80])
                continue
            vk_state["log"].append("  Найдено: {} треков".format(len(songs)))
            if order == "reverse":
                songs = list(reversed(songs))
            all_tracks.extend(songs)

        new_count = len(all_tracks)
        if new_count == 0:
            vk_state["log"].append("Треков не найдено.")
            return

        existing = vk_get_existing_tracks(save_dir)
        if mode == "prepend" and existing:
            vk_state["log"].append("Сдвигаю {} существующих треков...".format(len(existing)))
            vk_renumber_tracks(save_dir, start_from=new_count + 1)
            start_num = 1
        elif mode == "append" and existing:
            start_num = max(t[0] for t in existing) + 1
        else:
            start_num = 1

        total = new_count
        vk_state["total"] = total
        max_num = start_num + total - 1
        if mode in ("prepend", "append") and existing:
            refreshed = vk_get_existing_tracks(save_dir)
            if refreshed:
                max_num = max(max_num, max(t[0] for t in refreshed))
        pad = len(str(max_num))

        vk_state["log"].append("\nСкачиваю {} треков...".format(total))
        downloaded = 0
        failed = []

        for idx, song in enumerate(all_tracks):
            if vk_state["cancel"]:
                vk_state["log"].append("\nОтменено.")
                break
            track_num = start_num + idx
            num_str = str(track_num).zfill(pad)
            artist = vk_safe_filename(song.artist)
            title = vk_safe_filename(song.title)
            filename = "{}. {} - {}.mp3".format(num_str, artist, title)
            filepath = save_dir / filename
            display = "{} - {}".format(artist, title)
            vk_state["progress"] = idx + 1

            if filepath.exists():
                downloaded += 1
                continue

            try:
                ok = vk_download_song(song, filepath)
                if not ok:
                    ok = vk_search_fallback(service, song.artist, song.title, filepath)
            except Exception as e:
                if "captcha" in str(e).lower():
                    vk_state["log"].append("\n⚠ VK включил captcha. Скачано: {}/{}. Подождите 15 минут.".format(downloaded, total))
                    break
                ok = False

            if ok:
                downloaded += 1
                vk_state["log"].append("  OK: " + display)
            else:
                failed.append(display)
                vk_state["log"].append("  НЕ НАЙДЕН: " + display)
            time.sleep(0.3)

        vk_repad_tracks(save_dir)
        repair_playlist_refs(str(save_dir))

        vk_state["log"].append("\n========================================")
        vk_state["log"].append("Скачано: {}/{}".format(downloaded, total))
        if failed:
            vk_state["log"].append("Не найдено ({}):" .format(len(failed)))
            for f in failed:
                vk_state["log"].append("  - " + f)

        if run_meta_after and not vk_state["cancel"]:
            vk_state["log"].append("\nЗапускаю поиск мета-данных...")
            metadata_worker(folder, username)

    except Exception as e:
        vk_state["log"].append("ОШИБКА: внутренняя ошибка сервера")
    finally:
        vk_state["running"] = False
        vk_state["done"] = True


# ──────────────────── Local file import ────────────────────

def _native_pick_files():
    """Open a native OS file picker on the server machine. Returns a list of
    selected file paths, or [] if cancelled. Runs in a subprocess so it never
    blocks/crashes the HTTP server thread (GUI must not run inline on macOS)."""
    system = platform.system()
    try:
        if system == "Darwin":
            script = (
                'set theFiles to choose file with prompt "Выберите аудиофайлы" '
                'with multiple selections allowed\n'
                'set out to ""\n'
                'repeat with f in theFiles\n'
                '  set out to out & (POSIX path of f) & "\\n"\n'
                'end repeat\n'
                'return out'
            )
            res = subprocess.run(["osascript", "-e", script],
                                 capture_output=True, text=True, timeout=600)
            if res.returncode != 0:
                return []  # user cancelled or error
            return [l for l in res.stdout.splitlines() if l.strip()]
        elif system == "Windows":
            ps = (
                'Add-Type -AssemblyName System.Windows.Forms | Out-Null; '
                '$d = New-Object System.Windows.Forms.OpenFileDialog; '
                '$d.Multiselect = $true; '
                '$d.Title = "Выберите аудиофайлы"; '
                '$d.Filter = "Audio|*.mp3;*.flac;*.m4a;*.ogg;*.wav;*.aac;*.opus|All files|*.*"; '
                'if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){ '
                '[Console]::Out.Write(($d.FileNames -join "`n")) }'
            )
            res = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                                 capture_output=True, text=True, timeout=600)
            if res.returncode != 0:
                return []
            return [l for l in res.stdout.splitlines() if l.strip()]
        else:  # Linux / other — try zenity, then kdialog
            for cmd in (
                ["zenity", "--file-selection", "--multiple", "--separator", "\n",
                 "--title", "Выберите аудиофайлы"],
                ["kdialog", "--getopenfilename", str(Path.home()),
                 "--multiple", "--separate-output", "--title", "Выберите аудиофайлы"],
            ):
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                except FileNotFoundError:
                    continue
                if res.returncode != 0:
                    return []
                return [l for l in res.stdout.splitlines() if l.strip()]
            return []
    except Exception:
        return []
    return []


def _parse_local_name(stem):
    """Best-effort split of a filename stem into (artist, title)."""
    s = re.sub(r'^\s*\d+[\.\)\-]\s+', '', stem).strip()  # strip leading "NN. " / "NN - "
    if ' - ' in s:
        artist, title = s.split(' - ', 1)
        return artist.strip(), title.strip()
    return '', s


def local_import_worker(files, folder, mode, position, run_meta_after, username=""):
    """Copy local audio files into the catalog at the requested position,
    renumbering the existing numbered tracks. Reuses vk_state for progress."""
    vk_state = get_vk_state(username)
    vk_state["running"] = True
    vk_state["done"] = False
    vk_state["cancel"] = False
    vk_state["log"] = []
    vk_state["progress"] = 0
    vk_state["total"] = 0

    temps = []  # temp paths to clean up on failure
    try:
        save_dir = Path(folder)
        save_dir.mkdir(parents=True, exist_ok=True)

        srcs = []
        for fp in files:
            p = Path(fp)
            if p.is_file() and p.suffix.lower() in SUPPORTED_FORMATS:
                srcs.append(p)
            else:
                vk_state["log"].append("Пропущен (не аудио): " + str(fp))

        new_count = len(srcs)
        vk_state["total"] = new_count
        if new_count == 0:
            vk_state["log"].append("Нет подходящих аудиофайлов.")
            return

        # Step 1 (slow, failure-prone): copy new files to temp BEFORE touching
        # the existing catalog, so an error here leaves it intact.
        new_entries = []  # (name_part, tmp_path, suffix)
        new_meta = {}     # tmp_path -> (artist, title) for imported files
        for i, src in enumerate(srcs):
            if vk_state["cancel"]:
                vk_state["log"].append("\nОтменено.")
                return
            vk_state["progress"] = i + 1
            artist, title = _parse_local_name(src.stem)
            name_part = (vk_safe_filename(artist) + " - " + vk_safe_filename(title)) if artist else vk_safe_filename(title)
            tmp = save_dir / ("__tmp_li_new_{}_{}".format(i, src.name))
            shutil.copy2(src, tmp)
            temps.append(tmp)
            new_entries.append((name_part, tmp, src.suffix))
            new_meta[tmp] = (artist, title)  # for optional metadata lookup below
            vk_state["log"].append("Скопирован: " + name_part)

        # Determine 0-based insert index among existing numbered tracks
        existing = vk_get_existing_tracks(save_dir)
        n_exist = len(existing)
        if mode == "prepend":
            insert_idx = 0
        elif mode == "append":
            insert_idx = n_exist
        else:  # "position"
            try:
                insert_idx = max(0, min(int(position) - 1, n_exist))
            except (TypeError, ValueError):
                insert_idx = n_exist

        # Step 2 (fast): temp-rename existing numbered tracks to avoid collisions
        temp_others = []  # (name, tmp_path, suffix)
        for num, tname, tpath in existing:
            tmp = tpath.parent / ("__tmp_li_old_{}_{}".format(num, tpath.name))
            tpath.rename(tmp)
            temps.append(tmp)
            temp_others.append((tname, tmp, tpath.suffix))

        # Step 3: assemble final order and renumber sequentially
        combined = temp_others[:insert_idx] + new_entries + temp_others[insert_idx:]
        pad = len(str(len(combined)))
        imported_finals = []  # (final_name, artist, title) for the just-imported files
        for i, (tname, tmp, suffix) in enumerate(combined):
            final = "{}. {}{}".format(str(i + 1).zfill(pad), tname, suffix)
            tmp.rename(save_dir / final)
            if tmp in new_meta:
                a, t = new_meta[tmp]
                imported_finals.append((final, a, t))
        temps = []  # all renamed successfully

        vk_state["log"].append("\n========================================")
        vk_state["log"].append("Добавлено: {} (на позицию {})".format(
            new_count, insert_idx + 1))

        # Renumbering renamed existing files — heal playlist references so they
        # don't go blank.
        repair_playlist_refs(folder)

        if run_meta_after and not vk_state["cancel"]:
            # Fetch & WRITE metadata for the imported files (fill missing fields:
            # album/year/cover — never clobber existing tags). Unlike the bulk
            # scanner, this writes directly and is scoped to the new files.
            vk_state["log"].append("\nИщу мета-данные для импортированных треков...")
            for final, a, t in imported_finals:
                if vk_state["cancel"]:
                    break
                fp = save_dir / final
                existing = get_metadata(str(fp))
                if existing.get("artist") and existing.get("album") and existing.get("cover"):
                    continue  # already complete — nothing to fill
                found = search_metadata(a or existing.get("artist", ""),
                                        t or existing.get("title", ""))
                if found:
                    cover = fetch_cover_art(found)
                    write_metadata_to_file(str(fp), found, cover, overwrite=False)
                    vk_state["log"].append("  Мета: " + final)
                else:
                    vk_state["log"].append("  Не найдено: " + final)
                time.sleep(0.2)
            vk_state["log"].append("Мета-данные готовы.")
    except Exception:
        vk_state["log"].append("ОШИБКА: не удалось импортировать файлы")
    finally:
        # Clean up any leftover temp copies on failure
        for tmp in temps:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
        vk_state["running"] = False
        vk_state["done"] = True


def load_config():
    # Backward compat — now per-user
    return {"folders": [], "last_folder": ""}


def save_config(config):
    pass


def add_folder_to_config(folder):
    pass


def _tag_year(value):
    """Год из тега. Даты приезжают как «2016», «2016-04-11», «2016/04» — берём
    первые четыре цифры и отсеиваем мусор вроде «0000»."""
    m = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(m.group(0)) if m else 0


def _tag_track_no(value):
    """Номер трека: в тегах это «7», «7/12» или кортеж (7, 12)."""
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    m = re.match(r"\s*(\d+)", str(value or ""))
    return int(m.group(1)) if m else 0


def get_metadata(filepath):
    """Извлекает метаданные трека: title, artist, album, cover (base64).

    Год, жанр, номер и длительность нужны сортировкам и умным плейлистам.
    Длительность с битрейтом берутся из `audio.info`, который mutagen и так
    разбирает при открытии файла, — лишнего чтения с диска это не добавляет.
    """
    p = Path(filepath)
    meta = {
        "title": p.stem,
        "artist": "",
        "album": "",
        "albumartist": "",
        "genre": "",
        "year": 0,
        "track_no": 0,
        "duration": 0,
        "bitrate": 0,
        "cover": None,
        "cover_mime": None,
        "fmt": "",  # lossless format label (FLAC/ALAC/WAV/AIFF); "" for lossy
    }
    # Unambiguous lossless formats are known from the extension alone. m4a is a
    # container (AAC=lossy OR ALAC=lossless), so it's resolved by codec below.
    meta["fmt"] = {".flac": "FLAC", ".wav": "WAV", ".aiff": "AIFF",
                   ".aif": "AIFF", ".alac": "ALAC"}.get(p.suffix.lower(), "")
    if not HAS_MUTAGEN:
        return meta

    audio = None
    try:
        ext = p.suffix.lower()
        if ext == '.mp3':
            audio = MP3(filepath)
            tags = audio.tags
            if tags:
                meta["title"] = str(tags.get("TIT2", p.stem))
                meta["artist"] = str(tags.get("TPE1", ""))
                meta["album"] = str(tags.get("TALB", ""))
                meta["albumartist"] = str(tags.get("TPE2", ""))
                meta["genre"] = str(tags.get("TCON", ""))
                # TDRC — год в ID3v2.4, TYER — в 2.3; встречаются оба
                meta["year"] = _tag_year(tags.get("TDRC") or tags.get("TYER") or tags.get("TDRL"))
                meta["track_no"] = _tag_track_no(tags.get("TRCK"))
                for key in tags:
                    if key.startswith("APIC"):
                        apic = tags[key]
                        meta["cover"] = base64.b64encode(apic.data).decode()
                        meta["cover_mime"] = apic.mime
                        break
        elif ext == '.flac':
            audio = FLAC(filepath)
            meta["title"] = audio.get("title", [p.stem])[0]
            meta["artist"] = audio.get("artist", [""])[0]
            meta["album"] = audio.get("album", [""])[0]
            meta["albumartist"] = audio.get("albumartist", [""])[0]
            meta["genre"] = audio.get("genre", [""])[0]
            meta["year"] = _tag_year(audio.get("date", [""])[0] or audio.get("year", [""])[0])
            meta["track_no"] = _tag_track_no(audio.get("tracknumber", [""])[0])
            if audio.pictures:
                pic = audio.pictures[0]
                meta["cover"] = base64.b64encode(pic.data).decode()
                meta["cover_mime"] = pic.mime
        elif ext == '.m4a':
            audio = MP4(filepath)
            tags = audio.tags or {}
            meta["title"] = (tags.get("\xa9nam") or [p.stem])[0]
            meta["artist"] = (tags.get("\xa9ART") or [""])[0]
            meta["album"] = (tags.get("\xa9alb") or [""])[0]
            meta["albumartist"] = (tags.get("aART") or [""])[0]
            meta["genre"] = (tags.get("\xa9gen") or [""])[0]
            meta["year"] = _tag_year((tags.get("\xa9day") or [""])[0])
            meta["track_no"] = _tag_track_no(tags.get("trkn"))
            covr = tags.get("covr")
            if covr:
                meta["cover"] = base64.b64encode(bytes(covr[0])).decode()
                meta["cover_mime"] = "image/jpeg"
            # ALAC = lossless, AAC = lossy — both live in .m4a, tell them apart
            info = getattr(audio, "info", None)
            codec = ((getattr(info, "codec", "") or getattr(info, "codec_description", "")) or "").lower()
            meta["fmt"] = "ALAC" if "alac" in codec else ""
        elif ext == '.ogg':
            audio = OggVorbis(filepath)
            meta["title"] = audio.get("title", [p.stem])[0]
            meta["artist"] = audio.get("artist", [""])[0]
            meta["album"] = audio.get("album", [""])[0]
            meta["albumartist"] = audio.get("albumartist", [""])[0]
            meta["genre"] = audio.get("genre", [""])[0]
            meta["year"] = _tag_year(audio.get("date", [""])[0])
            meta["track_no"] = _tag_track_no(audio.get("tracknumber", [""])[0])
        info = getattr(audio, "info", None)
        if info is not None:
            meta["duration"] = int(getattr(info, "length", 0) or 0)
            # У FLAC/WAV битрейта в info нет — считаем из размера и длительности
            br = int(getattr(info, "bitrate", 0) or 0)
            if not br and meta["duration"]:
                try:
                    br = int(p.stat().st_size * 8 / meta["duration"])
                except Exception:
                    br = 0
            meta["bitrate"] = br // 1000
    except Exception:
        pass
    return meta


MAX_TRACKS = 50000

def scan_library(music_dir):
    """Сканирует директорию и возвращает список треков с метаданными (без обложек)."""
    tracks = []
    music_path = Path(music_dir)
    if not music_path.exists():
        return tracks

    files = sorted(music_path.iterdir(), key=lambda f: f.name)
    for f in files:
        if len(tracks) >= MAX_TRACKS:
            break
        if f.suffix.lower() in SUPPORTED_FORMATS and f.is_file():
            meta = get_metadata(str(f))
            rec = {
                "id": len(tracks),
                "file": f.name,
                "title": meta["title"],
                "artist": meta["artist"],
                "album": meta["album"],
                "has_cover": meta["cover"] is not None,
                "fmt": meta.get("fmt", ""),
            }
            # Короткие ключи и пропуск пустых значений — не косметика: каталог
            # целиком лежит в localStorage (~1 МБ на 2500 треков при квоте 5 МБ),
            # и шесть полных ключей с пустыми строками на трек её бы дожали.
            for key, src in (("yr", "year"), ("gen", "genre"), ("trk", "track_no"),
                             ("dur", "duration"), ("br", "bitrate"), ("aart", "albumartist")):
                val = meta.get(src)
                if val:
                    rec[key] = val
            tracks.append(rec)
    return tracks


def group_by_album(tracks):
    """Группирует треки по альбомам для cover flow."""
    albums = OrderedDict()
    for t in tracks:
        key = t["album"] or "Unknown"
        if key not in albums:
            albums[key] = {
                "name": key,
                "artist": t["artist"],
                "cover_file": t["file"] if t.get("has_cover") else None,
                "tracks": [],
            }
        albums[key]["tracks"].append(t["id"])
        if not albums[key]["cover_file"] and t.get("has_cover"):
            albums[key]["cover_file"] = t["file"]
    return list(albums.values())


# ──────────────────── Новинки артистов ────────────────────
# Что вышло у артистов, которые уже есть в библиотеке. Список артистов нигде не
# зашит: он каждый раз пересчитывается из текущего каталога, поэтому при
# пополнении, чистке или смене библиотеки раздел перестраивается сам.
#
# Источник — iTunes Search API (без ключа и регистрации), запасной — Deezer.
# На выборке из этой библиотеки iTunes нашёл всех артистов, включая русский
# андерграунд, и по свежести релизов заметно обгонял Deezer.

RELEASES_FILE = Path.home() / ".vinyl_releases.json"
RELEASES_TTL = 24 * 3600        # как часто перепроверять одного артиста
RELEASES_SCHEMA = 2             # растёт, когда в записи релиза добавляются поля
RELEASES_WINDOW_DAYS = 400      # что вообще считаем «новинкой»
RELEASES_LIMIT = 300            # сколько дропов показываем: отбираются лучшие по релевантности
FORYOU_LIMIT = 300              # столько же для 4YOU — остального каталога тех же артистов
_releases_lock = threading.Lock()
_releases_state = {"running": False, "done": 0, "total": 0, "started": 0}


ART_DIR = Path.home() / ".vinyl_release_art"
ART_MAX_FILES = 600          # ~30 МБ при обложках 600×600
ART_HOSTS_SUFFIX = (".mzstatic.com", ".dzcdn.net")


def _art_allowed(url):
    """Только картинки Apple и Deezer и только по https.

    Проверяем именно окончание с точкой: у «evil-mzstatic.com» его нет, так что
    подставить чужой хост не выйдет. Без этого ручка была бы открытым прокси.
    """
    try:
        u = urlparse(url)
    except Exception:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host.endswith(sfx) for sfx in ART_HOSTS_SUFFIX)


def _art_path(url):
    return ART_DIR / (hashlib.md5(url.encode("utf-8")).hexdigest() + ".img")


def _art_sniff(buf):
    if buf[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if buf[:4] == b"\x89PNG":
        return "image/png"
    if buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        return "image/webp"
    return None


def _art_evict():
    """Держим каталог в разумных пределах, выбрасывая самые давние файлы."""
    try:
        files = sorted(ART_DIR.glob("*.img"), key=lambda f: f.stat().st_mtime)
        for f in files[:-ART_MAX_FILES]:
            try: f.unlink()
            except Exception: pass
    except Exception:
        pass


def get_release_art(url):
    """Байты обложки: с диска, иначе скачиваем и кладём на диск.

    В кэше новинок лежит только ссылка, поэтому без этого браузер тянул бы
    картинки с серверов Apple при каждом открытии вкладки и после каждой чистки
    своего кэша. Здесь они переживают и перезапуск сервера.
    """
    path = _art_path(url)
    try:
        if path.is_file():
            data = path.read_bytes()
            mime = _art_sniff(data)
            if mime:
                os.utime(str(path), None)      # отметка использования для вытеснения
                return data, mime
    except Exception:
        pass
    client = _http()
    try:
        r = client.get(url)
        if r.status_code != 200:
            return None, None
        data = r.content
    except Exception:
        return None, None
    finally:
        try: client.close()
        except Exception: pass
    mime = _art_sniff(data)
    if not mime:
        return None, None
    try:
        ART_DIR.mkdir(exist_ok=True)
        os.chmod(str(ART_DIR), 0o700)
        path.write_bytes(data)
        _art_evict()
    except Exception:
        pass
    return data, mime


STARRED_FILE = Path.home() / ".vinyl_starred.json"


def _starred_load():
    """Отмеченные релизы: {пользователь: {ключ: снимок релиза}}.

    Отдельный файл, а не поле в кэше новинок: обновление переписывает кэш
    целиком в фоновом потоке, и отметка, поставленная в этот момент, потерялась
    бы. Храним не только ключ, но и сам релиз — отмеченное обязано оставаться
    видимым, даже когда оно выпало из окна ленты или артист вышел из проверки.
    """
    try:
        return json.loads(STARRED_FILE.read_text())
    except Exception:
        return {}


def _starred_save(data):
    try:
        STARRED_FILE.write_text(json.dumps(data, ensure_ascii=False))
        os.chmod(str(STARRED_FILE), 0o600)
    except Exception:
        pass


def set_release_starred(user, key, on, item=None):
    with _releases_lock:
        data = _starred_load()
        mine = data.setdefault(user, {})
        if on:
            mine[key] = item or mine.get(key) or {}
        else:
            mine.pop(key, None)
        _starred_save(data)
        return len(mine)


def _releases_load():
    try:
        return json.loads(RELEASES_FILE.read_text())
    except Exception:
        return {"artists": {}, "updated_at": 0}


def _releases_save(data):
    try:
        RELEASES_FILE.write_text(json.dumps(data, ensure_ascii=False))
        os.chmod(str(RELEASES_FILE), 0o600)
    except Exception:
        pass


def _norm_title(s):
    """Свести название к сравнимому виду: без регистра, пунктуации и хвостов
    вроде « - Single», которые iTunes дописывает к синглам и EP."""
    s = (s or "").lower()
    s = re.sub(r"\s*[-–—]\s*(single|ep|deluxe|remastered)\b.*$", "", s)
    s = re.sub(r"\((feat|prod|slowed|sped|remix)[^)]*\)", " ", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def split_artists(value):
    r"""Теги хранят соисполнителей через «/», имена файлов — через «,».

    Точку после feat/ft забираем разделителем: с `\bfeat\.?\b` граница слова
    после точки не совпадала, точка оставалась в имени, и «Linkin Park feat. Jay-Z»
    давало артиста «. Jay-Z» — отдельную запись в рейтинге и лишний запрос к API.
    Ведущую пунктуацию срезаем, хвостовую точку — нет: есть имена вроде
    «nicebeatzprod.».
    """
    out = []
    for part in re.split(r"[/,;]|\bfeat\b\.?|\bft\b\.?", value or "", flags=re.IGNORECASE):
        part = part.strip().lstrip(".,;:-–— ").rstrip(" -–—")
        if part:
            out.append(part)
    return out


def rank_library_artists(tracks):
    """Ранжирует артистов библиотеки: сколько у них треков и насколько свежо их
    добавляли.

    Каталог отсортирован по имени файла, а файлы пронумерованы от новых к
    старым, поэтому позиция трека в списке — уже готовая мера свежести, ничего
    дополнительно читать с диска не нужно. Счёт растёт логарифмически от числа
    треков (иначе один огромный артист забивает всё) и умножается на свежесть,
    так что артист с двумя треками, добавленными вчера, обгоняет артиста с
    сотней треков, которых не касались полгода.
    """
    n = len(tracks) or 1
    stats = {}
    for idx, t in enumerate(tracks):
        # _fresh проставляет _collect_user_tracks (по каталогу, а не по общему
        # списку); позиционный расчёт — запасной путь для прямого вызова.
        fresh = t.get("_fresh")
        if fresh is None:
            fresh = 1.0 - idx / n
        for a in split_artists(t.get("artist") or ""):
            key = a.lower()
            e = stats.setdefault(key, {"name": a, "count": 0, "fresh": 0.0})
            e["count"] += 1
            if fresh > e["fresh"]:
                e["fresh"] = fresh
    for e in stats.values():
        e["score"] = math.log(1 + e["count"], 2) * (0.5 + e["fresh"])
    return stats


def _artist_budget(total_artists):
    """Сколько артистов проверять за цикл. Растёт вместе с библиотекой, но
    медленнее неё — иначе на большой коллекции упрёмся в лимиты API."""
    return max(25, min(120, int(total_artists ** 0.6)))


def _http():
    return HttpClient(timeout=12, follow_redirects=True,
                      headers={"User-Agent": "insideside-music/1.0 (release tracker)"})


def _get_json(client, url, params=None, tries=3):
    """GET с повторами: iTunes при частых обращениях троттлит и рвёт соединение
    (Connection reset by peer), и одна такая осечка не должна выглядеть как
    «данных нет» — иначе мы зря уходим на запасной источник или отдаём пустоту."""
    for i in range(tries):
        try:
            r = client.get(url, params=params or {})
            if r.status_code == 200:
                return r.json()
            if r.status_code in (400, 401, 403, 404):
                return {}
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return {}


def _itunes_artist_releases(client, name):
    """(itunes_id, [релизы]) или (None, []). Два запроса на нового артиста и
    один на уже известного."""
    r = _get_json(client, "https://itunes.apple.com/search",
                  {"term": name, "entity": "musicArtist", "limit": 5})
    found = None
    for cand in r.get("results", []):
        if _norm_title(cand.get("artistName")) == _norm_title(name):
            found = cand
            break
    if not found:
        return None, []
    return found["artistId"], _itunes_albums(client, found["artistId"])


def _itunes_albums(client, artist_id):
    r = _get_json(client, "https://itunes.apple.com/lookup",
                  {"id": artist_id, "entity": "album", "limit": 25, "sort": "recent"})
    out = []
    for a in r.get("results", []):
        if a.get("wrapperType") != "collection":
            continue
        date = (a.get("releaseDate") or "")[:10]
        if not date:
            continue
        art = a.get("artworkUrl100") or ""
        out.append({
            "rid": a.get("collectionId"),
            "title": re.sub(r"\s*[-–—]\s*(Single|EP)$", "", a.get("collectionName") or ""),
            "date": date,
            "kind": ("single" if (a.get("trackCount") or 0) == 1
                     else "ep" if (a.get("trackCount") or 0) <= 6 else "album"),
            "tracks": a.get("trackCount") or 0,
            "art": art.replace("100x100bb", "600x600bb"),
            "url": a.get("collectionViewUrl") or "",
            "source": "itunes",
        })
    return out


def _deezer_artist_releases(client, name):
    """Запасной источник, когда iTunes не знает артиста."""
    r = _get_json(client, "https://api.deezer.com/search/artist", {"q": name, "limit": 5})
    found = None
    for cand in r.get("data", []):
        if _norm_title(cand.get("name")) == _norm_title(name):
            found = cand
            break
    if not found:
        return None, []
    al = _get_json(client, "https://api.deezer.com/artist/%d/albums" % found["id"], {"limit": 25})
    out = []
    for a in al.get("data", []):
        date = a.get("release_date") or ""
        if not date:
            continue
        rt = (a.get("record_type") or "album").lower()
        out.append({
            "rid": a.get("id"),
            "title": a.get("title") or "",
            "date": date[:10],
            "kind": "single" if rt == "single" else "ep" if rt == "ep" else "album",
            "tracks": a.get("nb_tracks") or 0,
            "art": a.get("cover_big") or a.get("cover_medium") or "",
            "url": a.get("link") or "",
            "source": "deezer",
        })
    return found["id"], out


def _fetch_artist(client, name, cached):
    """Один артист: сначала iTunes (по известному id — одним запросом),
    при неудаче Deezer. Возвращает запись кэша или None."""
    try:
        if cached and cached.get("itunes_id"):
            rel = _itunes_albums(client, cached["itunes_id"])
            if rel:
                return {"name": name, "itunes_id": cached["itunes_id"], "v": RELEASES_SCHEMA,
                        "releases": rel, "checked_at": time.time()}
        aid, rel = _itunes_artist_releases(client, name)
        if rel:
            return {"name": name, "itunes_id": aid, "v": RELEASES_SCHEMA, "releases": rel, "checked_at": time.time()}
    except Exception:
        pass
    try:
        did, rel = _deezer_artist_releases(client, name)
        if rel:
            return {"name": name, "deezer_id": did, "v": RELEASES_SCHEMA, "releases": rel, "checked_at": time.time()}
    except Exception:
        pass
    # Артист не найден — всё равно помечаем время, чтобы не долбить API каждый цикл
    return {"name": name, "releases": [], "v": RELEASES_SCHEMA, "checked_at": time.time(), "not_found": True}


_preview_cache = {}      # (source, rid) -> [треки]; в памяти, живёт до перезапуска

# Отдавать ссылку на превью прямо в браузер нельзя: Apple присылает файл с
# Content-Type "audio/x-m4p", а canPlayType на него отвечает пустой строкой —
# то есть браузер не обязан его проигрывать (iOS Safari к типам аудио особенно
# придирчив). Проксируем и выставляем корректный тип.
# Список хостов закрытый: без него это был бы открытый прокси, через который
# можно ходить куда угодно от имени сервера.
PREVIEW_HOSTS = {
    "audio-ssl.itunes.apple.com": "audio/mp4",
    "audio-preview.itunes.apple.com": "audio/mp4",
    "cdnt-preview.dzcdn.net": "audio/mpeg",
    "cdns-preview.dzcdn.net": "audio/mpeg",
}


def _preview_mime(url):
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return None
    if urlparse(url).scheme != "https":
        return None
    if host in PREVIEW_HOSTS:
        return PREVIEW_HOSTS[host]
    # У Deezer превью раскиданы по cdns-preview-a..z.dzcdn.net
    if re.match(r"^cdns-preview-[a-z0-9]\.dzcdn\.net$", host):
        return "audio/mpeg"
    return None


def _find_release_id(client, source, artist, album):
    """Найти тот же релиз в другом источнике — по артисту и названию."""
    if not artist or not album:
        return None
    if source == "deezer":
        q = _get_json(client, "https://api.deezer.com/search/album",
                      {"q": 'artist:"%s" album:"%s"' % (artist, album), "limit": 5})
        for a in q.get("data", []):
            if _norm_title(a.get("title")).startswith(_norm_title(album)[:14]):
                return a.get("id")
        return None
    q = _get_json(client, "https://itunes.apple.com/search",
                  {"term": "%s %s" % (artist, album), "entity": "album", "limit": 5})
    for a in q.get("results", []):
        if _norm_title(a.get("collectionName")).startswith(_norm_title(album)[:14]):
            return a.get("collectionId")
    return None


def release_tracks(source, rid, artist=None, album=None):
    """Треки релиза с 30-секундными превью.

    Отдельный запрос на релиз, поэтому дёргается только когда пользователь
    действительно нажал «прослушать» — тянуть это для всей ленты значило бы
    сотни лишних обращений к API ради того, что почти никто не откроет.

    Если основной источник ничего не дал (нет превью, троттлинг, релиза там
    просто нет), тот же релиз ищется во втором по артисту и названию. На выборке
    из этой библиотеки Deezer нашёл 10 релизов из 10, так что подстраховка
    рабочая, а не формальная.
    """
    out = _fetch_release_tracks(source, rid)
    if out:
        return out
    alt = "deezer" if source != "deezer" else "itunes"
    client = _http()
    try:
        alt_rid = _find_release_id(client, alt, artist, album)
    except Exception:
        alt_rid = None
    finally:
        try: client.close()
        except Exception: pass
    if alt_rid:
        return _fetch_release_tracks(alt, alt_rid)
    return []


def _fetch_release_tracks(source, rid):
    key = (source, str(rid))
    if key in _preview_cache:
        return _preview_cache[key]
    out = []
    client = _http()
    try:
        if source == "deezer":
            d = _get_json(client, "https://api.deezer.com/album/%s/tracks" % rid)
            for i, t in enumerate(d.get("data", []), 1):
                if t.get("preview"):
                    out.append({"n": t.get("track_position") or i, "title": t.get("title") or "",
                                "duration": t.get("duration") or 0, "preview": t["preview"]})
        else:
            d = _get_json(client, "https://itunes.apple.com/lookup",
                          {"id": rid, "entity": "song", "limit": 50})
            results = d.get("results", [])
            # В ответе первым идёт сам альбом — берём его название, чтобы срезать
            # хвост вида «Трек - Название Альбома», который iTunes дописывает у
            # концертников и саундтреков.
            album = next((x.get("collectionName") for x in results
                          if x.get("wrapperType") == "collection"), "") or ""
            for t in results:
                if t.get("wrapperType") == "track" and t.get("previewUrl"):
                    name = t.get("trackName") or ""
                    if album and name.endswith(album) and len(name) > len(album) + 2:
                        name = re.sub(r"\s*[-–—(]\s*$", "", name[:-len(album)]).strip()
                    out.append({"n": t.get("trackNumber") or len(out) + 1,
                                "title": name or t.get("trackName") or "",
                                "duration": round((t.get("trackTimeMillis") or 0) / 1000),
                                "preview": t["previewUrl"]})
    except Exception:
        return []
    finally:
        try: client.close()
        except Exception: pass
    out.sort(key=lambda x: x["n"])
    if out:
        _preview_cache[key] = out
    return out


_lib_snapshot = {}   # user -> (timestamp, tracks); скан 3000+ файлов дорогой
_LIB_SNAPSHOT_TTL = 120


def _collect_user_tracks_cached(user):
    """То же, что _collect_user_tracks, но с коротким кэшем.

    Лента запрашивается при каждом открытии вкладки, а сканирование читает теги
    у нескольких тысяч файлов. Без кэша сервер занимался бы этим впустую при
    каждом переключении вкладок.
    """
    now = time.time()
    got = _lib_snapshot.get(user)
    if got and now - got[0] < _LIB_SNAPSHOT_TTL:
        return got[1]
    tracks = _collect_user_tracks(user)
    _lib_snapshot[user] = (now, tracks)
    return tracks


def _collect_user_tracks(user):
    """Треки всех каталогов пользователя, у каждого проставлена свежесть 0..1.

    Считается внутри своего каталога, а не по сквозному списку: иначе второй и
    последующие каталоги целиком получали бы низкую свежесть просто потому, что
    идут ниже. Нумерованный каталог даёт свежесть позицией (файлы пронумерованы
    от новых к старым), ненумерованный — временем изменения файла.
    """
    tracks = []
    for folder in get_user_folders(user):
        try:
            if not Path(folder).is_dir():
                continue
            part = scan_library(folder)
        except Exception:
            continue
        n = len(part) or 1
        numbered = sum(1 for t in part if re.match(r"^\d+\.\s", t.get("file", ""))) > n / 2
        if numbered:
            for idx, t in enumerate(part):
                t["_fresh"] = 1.0 - idx / n
        else:
            mtimes = []
            for t in part:
                try:
                    mtimes.append(os.path.getmtime(str(Path(folder) / t["file"])))
                except Exception:
                    mtimes.append(0.0)
            lo, hi = min(mtimes), max(mtimes)
            span = (hi - lo) or 1.0
            for t, mt in zip(part, mtimes):
                t["_fresh"] = (mt - lo) / span
        tracks += part
    return tracks


def refresh_releases(user):
    """Фоновое обновление. Идёт по ранжированному списку артистов текущей
    библиотеки; артистов вне топа тоже понемногу добирает — по давности
    последней проверки, чтобы со временем покрыть всех."""
    global _releases_state
    with _releases_lock:
        if _releases_state["running"]:
            return
        _releases_state = {"running": True, "done": 0, "total": 0, "started": time.time()}
    try:
        _lib_snapshot.pop(user, None)          # обновление читает библиотеку заново
        tracks = _collect_user_tracks_cached(user)
        stats = rank_library_artists(tracks)
        data = _releases_load()
        cache = data.setdefault("artists", {})

        ranked = sorted(stats.items(), key=lambda kv: -kv[1]["score"])
        budget = _artist_budget(len(ranked))
        top = ranked[:budget]
        # «Разведка»: немного артистов из хвоста, дольше всех не проверявшихся
        tail = sorted(ranked[budget:],
                      key=lambda kv: cache.get(kv[0], {}).get("checked_at", 0))[:10]
        # После обновления схемы один раз добираем всех, кто уже лежит в кэше:
        # иначе часть записей осталась бы без новых полей до своей очереди.
        if data.get("schema", 0) < RELEASES_SCHEMA:
            seen = set(k for k, _ in top + tail)
            top = top + [kv for kv in ranked if kv[0] in cache and kv[0] not in seen]

        now = time.time()
        todo = [(k, v) for k, v in top + tail
                if now - cache.get(k, {}).get("checked_at", 0) > RELEASES_TTL
                or cache.get(k, {}).get("v", 0) < RELEASES_SCHEMA]
        with _releases_lock:
            _releases_state["total"] = len(todo)

        client = _http()
        try:
            for key, meta in todo:
                entry = _fetch_artist(client, meta["name"], cache.get(key))
                if entry:
                    cache[key] = entry
                with _releases_lock:
                    _releases_state["done"] += 1
                time.sleep(1.2)   # iTunes не любит частых запросов
        finally:
            try: client.close()
            except Exception: pass

        # Выкидываем артистов, которых в библиотеке уже нет
        for gone in [k for k in cache if k not in stats]:
            cache.pop(gone, None)
        data["updated_at"] = time.time()
        data["schema"] = RELEASES_SCHEMA
        _releases_save(data)
    except Exception as ex:
        print("Новинки: ошибка обновления: {}".format(ex))
    finally:
        with _releases_lock:
            _releases_state["running"] = False


def _feed_library(user):
    """Общая часть обеих лент: рейтинг артистов и карта «что уже есть».

    Считается на каждый запрос, а не кэшируется вместе с релизами: библиотека
    меняется чаще, чем данные о релизах.
    """
    tracks = _collect_user_tracks_cached(user)
    stats = rank_library_artists(tracks)
    owned = {}   # artist key -> set нормализованных названий (альбомы и треки)
    for t in tracks:
        names = {_norm_title(t.get("album")), _norm_title(t.get("title"))}
        names.discard("")
        for a in split_artists(t.get("artist") or ""):
            owned.setdefault(a.lower(), set()).update(names)
    return stats, owned


def _release_key(rel):
    """Устойчивый ключ релиза.

    У записей из старого кэша `rid` нет (поле появилось вместе с превью), и
    «itunes:None» совпадал у всех таких сразу — дедупликация схлопывала их в
    одну карточку, а в 4YOU ключ ещё и пересекался с чужим релизом. Для
    безридовых строим ключ из названия и даты; превью у них всё равно нет.
    """
    src = rel.get("source") or "itunes"
    rid = rel.get("rid")
    if rid in (None, ""):
        return "{}:t:{}|{}".format(src, _norm_title(rel.get("title")), rel.get("date") or "")
    return "{}:{}".format(src, rid)


def _feed_cutoff():
    """Граница между лентами: свежее неё — NEW, старее — 4YOU."""
    return (datetime.now() - timedelta(days=RELEASES_WINDOW_DAYS)).strftime("%Y-%m-%d")


def _feed_dedup(items):
    """Схлопывает дубли, оставляя вариант ближайшего артиста.

    Обязана идти ДО среза, иначе он теряет места на дубли: совместка приходит
    от каждого участника, а iTunes порой отдаёт один и тот же релиз с разными
    id под разными артистами — ловим и по ключу, и по паре «название + дата».
    """
    items.sort(key=lambda x: -x.get("artist_score", 0))
    seen_keys, seen_titles, uniq = set(), set(), []
    for it in items:
        # Ключ может быть ещё не проставлен: 4YOU схлопывает дубли до отметок,
        # а не после.
        k = it.get("key") or _release_key(it)
        tk = (_norm_title(it.get("title")), it.get("date"))
        if k in seen_keys or tk in seen_titles:
            continue
        seen_keys.add(k)
        seen_titles.add(tk)
        uniq.append(it)
    return uniq


def _feed_add_starred(user, items, want):
    """Проставляет отметки и возвращает в ленту закреплённое, что из неё выпало.

    Хранилище отметок общее для обеих лент, поэтому чужие записи надо отсеять:
    `want(snapshot)` решает, этой ли ленте принадлежит снимок. Само возвращение
    нужно потому, что закреплённое не должно исчезать, когда релиз вышел из
    окна дат или артист выпал из проверки.
    """
    starred = _starred_load().get(user, {})
    seen = set()
    for it in items:
        k = _release_key(it)
        it["key"] = k
        it["starred"] = k in starred
        seen.add(k)
    for k, snap in starred.items():
        if k in seen or not snap or not want(snap):
            continue
        rev = dict(snap)
        rev["key"] = k
        rev["starred"] = True
        rev.setdefault("artist_score", 0)
        rev.setdefault("in_library", False)
        items.append(rev)
    return items, len(starred)


def _feed_envelope(items, found, stats, cache, data, extra=None):
    """Общая обвязка ответа: прогресс проверки и состояние кэша."""
    out = {
        "items": items,
        "found": found,          # сколько всего нашлось до среза
        "updated_at": data.get("updated_at", 0),
        "artists_total": len(stats),
        "artists_checked": sum(1 for k in stats if k in cache),
        "budget": _artist_budget(len(stats)),
        # Отметка схемы хранится один раз на весь файл и снимается по завершении
        # обновления. Если проверять её по каждому артисту, то записи, не попавшие
        # в бюджет цикла, держали бы флаг поднятым вечно и обновление запускалось бы
        # снова и снова.
        "schema_stale": data.get("schema", 0) < RELEASES_SCHEMA,
        "refreshing": _releases_state["running"],
        "progress": {"done": _releases_state["done"], "total": _releases_state["total"]},
    }
    out.update(extra or {})
    return out


def build_foryou_feed(user, limit=None):
    """4YOU: остальной каталог артистов библиотеки — не новинки.

    Отдельных запросов к API не делает и своего кэша не заводит: iTunes отдаёт
    по 25 последних релизов на артиста, а новинками из них считаются только
    последние RELEASES_WINDOW_DAYS дней — всё остальное уже лежит в
    ~/.vinyl_releases.json и просто нигде не показывалось.

    Отбор: то, чего в библиотеке нет (в этом весь смысл раздела), ранжирование —
    по близости артиста. Внутри артиста релизы идут от свежего к старому, а сами
    артисты выдаются по кругу: при обычной сортировке по весу первые два десятка
    карточек занимал бы один артист, у которого в кэше 25 записей.
    """
    stats, owned = _feed_library(user)
    data = _releases_load()
    cache = data.get("artists", {})
    cutoff_s = _feed_cutoff()

    by_artist = {}
    for key, meta in stats.items():
        entry = cache.get(key)
        if not entry:
            continue
        have = owned.get(key, set())
        bucket = []
        for rel in entry.get("releases", []):
            if rel.get("date", "") >= cutoff_s:
                continue                      # это новинка, ей место в NEW
            if _norm_title(rel.get("title")) in have:
                continue                      # уже есть — показывать нечего
            bucket.append({
                "artist": meta["name"],
                "rid": rel.get("rid"),
                "artist_score": round(meta["score"], 3),
                "artist_tracks": meta["count"],
                "title": rel.get("title", ""),
                "date": rel.get("date", ""),
                "kind": rel.get("kind", "album"),
                "tracks": rel.get("tracks", 0),
                "art": rel.get("art", ""),
                "url": rel.get("url", ""),
                "source": rel.get("source", ""),
                "in_library": False,
            })
        if bucket:
            bucket.sort(key=lambda x: x["date"], reverse=True)
            by_artist[key] = bucket

    items = _feed_dedup([it for b in by_artist.values() for it in b])
    # Дедупликация перетасовала списки — раскладываем обратно по артистам,
    # сохраняя порядок «свежее сверху» внутри каждого.
    buckets, order = {}, []
    for it in items:
        k = (it.get("artist") or "").lower()
        if k not in buckets:
            buckets[k] = []
            order.append((it.get("artist_score", 0), k))
        buckets[k].append(it)
    order.sort(key=lambda x: -x[0])
    ranked, row = [], 0
    while True:
        added = False
        for _, k in order:
            b = buckets[k]
            if row < len(b):
                b[row]["rank"] = round(b[row].get("artist_score", 0), 4)
                ranked.append(b[row])
                added = True
        if not added:
            break
        row += 1

    found = len(ranked)
    ranked, starred_count = _feed_add_starred(user, ranked, lambda s: s.get("date", "") < cutoff_s)
    # Закреплённое под срез не попадает: запись из снимка приходит без веса и
    # уехала бы в самый хвост, то есть отмеченная карточка исчезала бы из ленты.
    keep = [x for x in ranked if x.get("starred")]
    rest = [x for x in ranked if not x.get("starred")]
    room = max(0, (limit or FORYOU_LIMIT) - len(keep))
    items = keep + rest[:room]
    return _feed_envelope(items, found, stats, cache, data,
                          {"starred_count": starred_count, "mode": "foryou"})


def build_releases_feed(user, limit=None):
    """Лента новинок. «Есть ли уже в библиотеке» считается здесь, а не в
    кэше: библиотека меняется чаще, чем данные о релизах."""
    stats, owned = _feed_library(user)
    data = _releases_load()
    cache = data.get("artists", {})

    cutoff_s = _feed_cutoff()
    items = []
    for key, meta in stats.items():
        entry = cache.get(key)
        if not entry:
            continue
        have = owned.get(key, set())
        for rel in entry.get("releases", []):
            if rel.get("date", "") < cutoff_s:
                continue
            items.append({
                "artist": meta["name"],
                "rid": rel.get("rid"),
                "artist_score": round(meta["score"], 3),
                "artist_tracks": meta["count"],
                "title": rel.get("title", ""),
                "date": rel.get("date", ""),
                "kind": rel.get("kind", "album"),
                "tracks": rel.get("tracks", 0),
                "art": rel.get("art", ""),
                "url": rel.get("url", ""),
                "source": rel.get("source", ""),
                "in_library": _norm_title(rel.get("title")) in have,
            })
    # Отмеченное, которого в ленте уже нет (вышло из окна дат, артист выпал из
    # проверки), возвращаем из снимка: «жду выхода» не должно исчезать само.
    # Снимки старее окна принадлежат 4YOU — хранилище отметок общее.
    items, starred_count = _feed_add_starred(user, items, lambda sn: sn.get("date", "") >= cutoff_s)

    # Список режется, поэтому важно, ЧТО попадёт в срез. Просто «самые свежие»
    # набивают его случайными синглами артистов, которых вы слушали пару раз.
    # Поэтому вес = близость артиста × свежесть релиза: анонсы и вышедшее на
    # днях идут с полным весом, дальше вес плавно падает (вдвое примерно за три
    # месяца), и артист с сотней треков обгоняет артиста с двумя.
    items = _feed_dedup(items)

    today = datetime.now().strftime("%Y-%m-%d")
    def relevance(x):
        try:
            age = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(x["date"], "%Y-%m-%d")).days
        except Exception:
            age = 400
        fresh = 1.0 if age <= 0 else math.exp(-age / 120.0)
        return x.get("artist_score", 0) * (0.4 + fresh)
    for it in items:
        it["rank"] = round(relevance(it), 4)
    found = len(items)
    # Избранное под срез не попадает никогда. Раньше запись из снимка приходила
    # без artist_score (артиста уже нет в проверке), получала вес 0, уезжала в
    # самый хвост ранжирования и обрезалась — отмеченная карточка просто
    # исчезала из ленты.
    keep = [x for x in items if x.get("starred")]
    rest = [x for x in items if not x.get("starred")]
    rest.sort(key=lambda x: (x["rank"], x["date"]), reverse=True)
    room = max(0, (limit or RELEASES_LIMIT) - len(keep))
    items = keep + rest[:room]
    # Внутри среза показываем по свежести — так привычнее читать ленту
    items.sort(key=lambda x: (x["date"], x["rank"]), reverse=True)
    # Отмеченные — всегда наверху, порядок между ними прежний
    items.sort(key=lambda x: not x.get("starred"))
    return _feed_envelope(items, found, stats, cache, data,
                          {"starred_count": starred_count, "mode": "new"})


# ──────────────────── Playlists ────────────────────

def _playlists_file(music_dir):
    return Path(music_dir) / ".vinyl_playlists.json"


def load_playlists(music_dir):
    p = _playlists_file(music_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return []


def save_playlists(music_dir, playlists):
    _playlists_file(music_dir).write_text(json.dumps(playlists, ensure_ascii=False, indent=2))


def _denumber_name(filename):
    """Strip a leading 'NN. ' track-number prefix from a filename, so two
    filenames that differ only by their catalog number compare equal."""
    return re.sub(r'^\d+\.\s+', '', filename)


def repair_playlist_refs(folder, playlists=None):
    """Heal playlist track references whose filenames have drifted because the
    catalog was renumbered (import to start/position, reorder, track edit).
    A dangling reference is re-matched to the current file with the same
    de-numbered name. Persists changes and returns the playlists.

    Playlists store tracks by filename ("NN. Artist - Title.ext"); renumbering
    changes the "NN" prefix, so without this the whole playlist goes blank."""
    if playlists is None:
        playlists = load_playlists(folder)
    if not playlists:
        return playlists
    try:
        current = [f.name for f in Path(folder).iterdir()
                   if f.is_file() and f.suffix.lower() in SUPPORTED_FORMATS]
    except (OSError, FileNotFoundError):
        return playlists
    current_set = set(current)
    by_denum = {}
    for name in current:
        by_denum.setdefault(_denumber_name(name), name)
    changed = False
    for pl in playlists:
        new_refs = []
        for ref in pl.get("tracks", []):
            if ref in current_set:
                new_refs.append(ref)
            else:
                alt = by_denum.get(_denumber_name(ref))
                if alt and alt not in new_refs:
                    new_refs.append(alt)
                    changed = True
                else:
                    new_refs.append(ref)  # keep dangling — harmless, skipped on render
        pl["tracks"] = new_refs
    if changed:
        try:
            save_playlists(folder, playlists)
        except Exception:
            pass
    return playlists


# ──────────────────── Периоды прослушивания («эры») ────────────────────
# Каталог отсортирован по номеру, свежее — выше. Пользователь размечает пачки
# номеров годами («1200–1400 = 2016»), и из этой разметки собираются плейлисты
# «Слушал в 2016».
#
# Границы хранятся НЕ номерами, а якорями — обезномеренными именами файлов на
# краях диапазона. Импорт перенумеровывает каталог целиком, и номер как граница
# уже через одну загрузку указывал бы на другую музыку. С якорем всё сходится
# само: номера сместились — граница уехала вместе с файлом; трек вклинился
# внутрь диапазона — он между якорями, то есть промежуток «расширился» сам.
#
# Разрешение якорей в номера делает КЛИЕНТ по своему списку треков: тогда
# разметка одинаково работает и с сервером, и офлайн из кэша каталога. Сервер —
# хранилище и точка синхронизации между устройствами.


def _eras_file(music_dir):
    return Path(music_dir) / ".vinyl_eras.json"


def load_eras(music_dir):
    try:
        d = json.loads(_eras_file(music_dir).read_text())
        if isinstance(d, dict):
            return {"enabled": bool(d.get("enabled")), "eras": d.get("eras") or []}
    except Exception:
        pass
    return {"enabled": False, "eras": []}


def save_eras(music_dir, data):
    payload = {"enabled": bool(data.get("enabled")), "eras": data.get("eras") or []}
    _eras_file(music_dir).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


# ──────────────────── История прослушиваний ────────────────────
# Что вы слушаете, а не что добавили. До этого «свежесть» артиста выводилась из
# позиции файла в нумерованном каталоге — то есть из даты добавления; здесь же
# факты.
#
# Ключ записи — обезномеренное имя файла. Каталог перенумеровывается целиком
# при каждом импорте, и полное имя как ключ означало бы, что вся история
# обнуляется после добавления одного трека (той же болезнью болели плейлисты,
# см. repair_playlist_refs).

PLAYS_FILE = Path.home() / ".vinyl_plays.json"
_plays_lock = threading.Lock()


def _plays_load():
    try:
        return json.loads(PLAYS_FILE.read_text())
    except Exception:
        return {}


def _plays_save(data):
    try:
        PLAYS_FILE.write_text(json.dumps(data, ensure_ascii=False))
        os.chmod(str(PLAYS_FILE), 0o600)
    except Exception:
        pass


def record_plays(user, folder, plays):
    """Отмечает прослушивания пачкой.

    Пачкой, потому что без сервера они копятся на клиенте и приезжают списком —
    как отметки избранного в DROPS. Время берём присланное: у отложенной записи
    оно относится к моменту прослушивания, а не отправки.
    """
    if not plays:
        return 0
    now = time.time()
    added = 0
    with _plays_lock:
        data = _plays_load()
        fold = data.setdefault(user, {}).setdefault(folder, {})
        for item in plays:
            if isinstance(item, dict):
                name, at = item.get("file") or "", item.get("at") or now
            else:
                name, at = item, now
            name = _denumber_name(name)
            if not name:
                continue
            try:
                at = float(at)
            except (TypeError, ValueError):
                at = now
            e = fold.setdefault(name, {"n": 0, "first": at, "last": 0})
            e["n"] = e.get("n", 0) + 1
            e["last"] = max(e.get("last", 0), at)
            e["first"] = min(e.get("first", at), at)
            added += 1
        _plays_save(data)
    return added


def get_plays(user, folder):
    """Счётчики каталога: обезномеренное имя -> {n, first, last}."""
    return _plays_load().get(user, {}).get(folder, {})


# ──────────────────── Metadata lookup ────────────────────

_meta_states = {}  # username -> state dict

def get_meta_state(username=""):
    if username not in _meta_states:
        _meta_states[username] = {
            "running": False, "cancel": False,
            "progress": 0, "total": 0, "log": [], "done": False,
        }
    return _meta_states[username]


def parse_track_name(filename):
    """Парсит '0001. Artist - Title.mp3' -> (artist, title). Использует clean_vk_filename."""
    return clean_vk_filename(filename)


def search_deezer(artist, title):
    """Ищет трек в Deezer API (отличная база русской музыки, без ключа)."""
    try:
        query = "{} {}".format(artist, title) if artist else title
        url = "https://api.deezer.com/search?q={}&limit=5".format(quote(query))
        with HttpClient(timeout=10) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = data.get('data', [])
        if not items:
            return None
        item = items[0]
        album = item.get('album', {})
        return {
            'title': item.get('title', title),
            'artist': item.get('artist', {}).get('name', artist),
            'album': album.get('title', ''),
            'year': '',
            # id альбома нужен для добора: жанр и дата у Deezer лежат на альбоме,
            # а не в ответе поиска (см. _deezer_album_info)
            'album_id': album.get('id'),
            'cover_url': album.get('cover_big') or album.get('cover_medium') or album.get('cover'),
            'release_mbid': None,
        }
    except Exception:
        return None


def search_musicbrainz(artist, title):
    """Ищет трек в MusicBrainz."""
    if not HAS_MB:
        return None
    try:
        if artist:
            query = 'recording:"{}" AND artist:"{}"'.format(
                title.replace('"', ''), artist.replace('"', ''))
            result = musicbrainzngs.search_recordings(query=query, limit=5)
        else:
            result = musicbrainzngs.search_recordings(recording=title, limit=5)

        recordings = result.get('recording-list', [])
        if not recordings:
            return None

        rec = recordings[0]
        meta = {
            'title': rec.get('title', title),
            'artist': '',
            'album': '',
            'year': '',
            'cover_url': None,
            'release_mbid': None,
        }

        credits = rec.get('artist-credit', [])
        if credits:
            names = []
            for c in credits:
                if isinstance(c, dict) and 'artist' in c:
                    names.append(c['artist'].get('name', ''))
            meta['artist'] = ', '.join(names)

        releases = rec.get('release-list', [])
        if releases:
            rel = releases[0]
            meta['album'] = rel.get('title', '')
            meta['year'] = rel.get('date', '')[:4] if rel.get('date') else ''
            meta['release_mbid'] = rel.get('id')

        return meta
    except Exception:
        return None


def search_itunes(artist, title):
    """Ищет трек в iTunes Search API (хорошая база, без ключа)."""
    try:
        query = "{} {}".format(artist, title) if artist else title
        url = "https://itunes.apple.com/search?term={}&media=music&limit=5".format(quote(query))
        with HttpClient(timeout=10) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = data.get('results', [])
        if not items:
            return None
        item = items[0]
        cover = item.get('artworkUrl100', '')
        if cover:
            cover = cover.replace('100x100', '600x600')
        year = ''
        release_date = item.get('releaseDate', '')
        if release_date:
            year = release_date[:4]
        return {
            'title': item.get('trackName', title),
            'artist': item.get('artistName', artist),
            'album': item.get('collectionName', ''),
            'year': year,
            # Жанр, номер трека и album artist лежат в том же ответе — отдельных
            # запросов не нужно. collectionArtistName приходит только у сборников,
            # поэтому пустой оставляем пустым, а не подставляем исполнителя трека.
            'genre': item.get('primaryGenreName', '') or '',
            'track': item.get('trackNumber') or 0,
            'album_artist': item.get('collectionArtistName', '') or '',
            'cover_url': cover,
            'release_mbid': None,
        }
    except Exception:
        return None


def search_lastfm(artist, title):
    """Ищет трек в Last.fm API (бесплатный ключ, огромная база)."""
    try:
        # Last.fm public API key (shared/demo)
        api_key = "b25b959554ed76058ac220b7b2e0a026"
        url = "https://ws.audioscrobbler.com/2.0/?method=track.getInfo&api_key={}&artist={}&track={}&format=json".format(
            api_key, quote(artist), quote(title))
        with HttpClient(timeout=10) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        track_info = data.get('track')
        if not track_info:
            return None
        album_info = track_info.get('album', {})
        album_name = album_info.get('title', '') if isinstance(album_info, dict) else ''
        # Метки Last.fm ставят руками, и для андерграунда это часто
        # единственный источник жанра. Берём самую популярную.
        lf_genre = ''
        tags = (track_info.get('toptags') or {}).get('tag')
        if isinstance(tags, dict):
            tags = [tags]
        if isinstance(tags, list):
            for tg in tags:
                if isinstance(tg, dict) and tg.get('name'):
                    lf_genre = tg['name']
                    break
        cover_url = ''
        if isinstance(album_info, dict):
            images = album_info.get('image', [])
            for img in reversed(images):
                if isinstance(img, dict) and img.get('#text'):
                    cover_url = img['#text']
                    break
        return {
            'title': track_info.get('name', title),
            'artist': track_info.get('artist', {}).get('name', artist) if isinstance(track_info.get('artist'), dict) else artist,
            'album': album_name,
            'genre': lf_genre,
            'year': '',
            'cover_url': cover_url if cover_url and 'noimage' not in cover_url else None,
            'release_mbid': None,
        }
    except Exception:
        return None


def search_genius(artist, title):
    """Ищет трек в Genius (отличная база русской музыки, публичный API)."""
    try:
        query = "{} {}".format(artist, title) if artist else title
        url = "https://genius.com/api/search?q={}".format(quote(query))
        with HttpClient(timeout=10) as client:
            resp = client.get(url, headers={"User-Agent": "VinylPlayer/1.0"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        hits = data.get('response', {}).get('hits', [])
        if not hits:
            return None
        hit = hits[0].get('result', {})
        album_name = ''
        # Genius doesn't always return album in search — try to get it
        if hit.get('album'):
            album_name = hit['album'].get('name', '')
        cover_url = hit.get('song_art_image_url') or hit.get('header_image_thumbnail_url') or ''
        return {
            'title': hit.get('title', title),
            'artist': hit.get('primary_artist', {}).get('name', artist),
            'album': album_name,
            'year': '',
            'cover_url': cover_url if cover_url else None,
            'release_mbid': None,
        }
    except Exception:
        return None


def search_spotify_public(artist, title):
    """Ищет трек через публичный Spotify endpoint (без ключа)."""
    try:
        query = "{} {}".format(artist, title) if artist else title
        url = "https://api.spotify.com/v1/search?q={}&type=track&limit=3".format(quote(query))
        # Try without auth — returns 401, but we use the embed endpoint instead
        embed_url = "https://open.spotify.com/oembed?url=https://open.spotify.com/search/{}".format(quote(query))
        # Alternative: use Spotify's public web API proxy
        url2 = "https://spotify-scraper.p.rapidapi.com/v1/track/search?q={}".format(quote(query))
        # Simplest: use the same approach as other services
        # Actually let's skip Spotify (needs OAuth) and use Genius + better parsing
        return None
    except Exception:
        return None


def clean_vk_filename(filename):
    """Очищает типичные VK-названия от мусора и извлекает артиста/название."""
    stem = Path(filename).stem
    # Remove numbering: "0001. " or "001 "
    cleaned = re.sub(r'^\d+[\.\s]+\s*', '', stem)
    # Common VK patterns
    # "Artist - Title (feat. X)" — standard
    # "Artist – Title" (em-dash)
    # "ARTIST, ARTIST2 - TITLE"
    # "Title" (no separator)
    # Clean up brackets/tags: [Official], (Official Audio), (Prod. by X), etc.
    cleaned = re.sub(r'\s*[\[\(](official|audio|video|prod\.?|lyrics?|clip|music|hq|hd|remix|remastered)[\s\w.]*[\]\)]', '', cleaned, flags=re.IGNORECASE)
    # Remove trailing whitespace and dots
    cleaned = cleaned.strip(' .-_')
    # Try separators in order
    for sep in [' - ', ' – ', ' — ', ' ~ ']:
        if sep in cleaned:
            parts = cleaned.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    # No separator found — might be just title
    # Try comma as artist separator: "GSPD, МУККА - БИПОЛЯРКА"
    # Already handled by ' - ' above
    return '', cleaned.strip()


def search_metadata(artist, title):
    """Ищет метаданные по нескольким источникам."""
    # Clean up input from VK naming quirks
    if not artist and title:
        artist, title = clean_vk_filename(title + '.mp3')
        if not title:
            title = artist
            artist = ''

    # 1. Deezer — лучший для русской музыки
    result = search_deezer(artist, title)
    if result and result.get('album'):
        return result
    # 2. iTunes — большая международная база
    result2 = search_itunes(artist, title)
    if result2 and result2.get('album'):
        return result2
    # 3. Genius — отличная база русской и мировой музыки
    result_g = search_genius(artist, title)
    if result_g and result_g.get('album'):
        return result_g
    # 4. Last.fm — огромная база, хорошо для редких треков
    if artist:
        result3 = search_lastfm(artist, title)
        if result3 and result3.get('album'):
            return result3
    # 5. MusicBrainz — академический источник
    result4 = search_musicbrainz(artist, title)
    if result4 and result4.get('album'):
        return result4
    # 6. Retry with cleaned title if first pass failed
    if artist:
        # Try just the title without artist (VK sometimes puts garbage in artist)
        result5 = search_deezer('', title)
        if result5 and result5.get('album'):
            return result5
    # Return best partial match
    return result or result_g or result2 or result4


def _deezer_album_info(album_id):
    """Жанр и дата выпуска у Deezer лежат на альбоме.

    В ответе /search их нет — только id, title и обложка (проверено). Отдельный
    запрос на альбом стоит того: без него треки, которых не знает iTunes,
    оставались вообще без жанра и года, а это добрая половина русского
    андерграунда в библиотеке.
    """
    if not album_id:
        return {}
    try:
        with HttpClient(timeout=10) as client:
            r = client.get("https://api.deezer.com/album/{}".format(album_id))
        if r.status_code != 200:
            return {}
        j = r.json()
        gens = (j.get("genres") or {}).get("data") or []
        return {"genre": (gens[0].get("name") or "") if gens else "",
                "year": (j.get("release_date") or "")[:4]}
    except Exception:
        return {}


def enrich_new_tags(meta, artist, title):
    """Дозаполняет жанр, год и номер трека из iTunes.

    Нужно потому, что в search_metadata первым идёт Deezer, и для большинства
    треков побеждает он — а у него в ответе поиска нет ни жанра, ни номера, ни
    даты (проверено: только id/title/artist/album/cover). Отдельный запрос
    делаем лишь когда полей действительно не хватает.

    Трогает ТОЛЬКО новые поля: название, исполнитель, альбом и обложка остаются
    теми, что нашёл основной источник.
    """
    if not meta:
        return meta
    if meta.get("genre") and meta.get("year") and meta.get("track"):
        return meta
    extra = search_itunes(artist or meta.get("artist", ""), title or meta.get("title", ""))
    if not extra:
        return _fill_missing_tags(meta, artist, title)

    # Сверяемся по альбому: iTunes мог найти другую запись, и её номер трека с
    # годом относились бы к чужому релизу. Издания при этом считаем одним
    # альбомом — источники расходятся сплошь и рядом («From Zero» против
    # «From Zero (Deluxe Edition)»), а музыка та же.
    a, b = _norm_title(meta.get("album") or ""), _norm_title(extra.get("album") or "")
    same_album = not a or not b or a == b or a.startswith(b) or b.startswith(a)

    # Жанр — свойство трека и артиста, а не конкретного издания, поэтому его
    # берём и при разных альбомах, лишь бы совпал исполнитель. Номер трека, год
    # и album artist без совпадения альбома брать нельзя.
    same_artist = _norm_title(meta.get("artist") or "") == _norm_title(extra.get("artist") or "")
    keys = ("genre", "year", "track", "album_artist") if same_album else \
           (("genre",) if same_artist else ())
    for key in keys:
        if not meta.get(key) and extra.get(key):
            meta[key] = extra[key]
    _fill_missing_tags(meta, artist, title)
    return meta


def _fill_missing_tags(meta, artist, title):
    """Добирает жанр и год теми же источниками, что уже есть в приложении.

    Порядок по цене и попаданию: альбом Deezer (один запрос, знает почти всё,
    что знает его же поиск), метки Last.fm (жанр), MusicBrainz (год). Каждый
    следующий шаг делается, только если чего-то до сих пор не хватает, — на
    хорошо оттегированной библиотеке до них дело почти не доходит.
    """
    if meta.get("genre") and meta.get("year"):
        return meta
    a = artist or meta.get("artist", "")
    t = title or meta.get("title", "")

    info = _deezer_album_info(meta.get("album_id"))
    for key in ("genre", "year"):
        if not meta.get(key) and info.get(key):
            meta[key] = info[key]

    if not meta.get("genre"):
        lf = search_lastfm(a, t)
        if lf and lf.get("genre"):
            meta["genre"] = lf["genre"]

    if not meta.get("year"):
        mb = search_musicbrainz(a, t)
        if mb and mb.get("year"):
            meta["year"] = mb["year"]
    return meta


def fetch_cover_art(meta):
    """Скачивает обложку: из Deezer URL или Cover Art Archive."""
    # Deezer cover
    cover_url = meta.get('cover_url')
    if cover_url:
        try:
            with HttpClient(timeout=15, follow_redirects=True) as client:
                resp = client.get(cover_url)
            if resp.status_code == 200:
                return resp.content
        except Exception:
            pass

    # Cover Art Archive fallback
    release_mbid = meta.get('release_mbid')
    if release_mbid:
        try:
            url = "https://coverartarchive.org/release/{}/front-500".format(release_mbid)
            with HttpClient(timeout=15, follow_redirects=True) as client:
                resp = client.get(url)
            if resp.status_code == 200:
                return resp.content
        except Exception:
            pass
    return None


def _update_tags(filepath, title, artist):
    """Update only title and artist tags in a file (preserves everything else)."""
    ext = Path(filepath).suffix.lower()
    if ext == '.mp3':
        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            tags = ID3()
        tags["TIT2"] = TIT2(encoding=3, text=title)
        if artist:
            tags["TPE1"] = TPE1(encoding=3, text=artist)
        tags.save(filepath)
    elif ext == '.flac':
        audio = FLAC(filepath)
        audio["title"] = title
        if artist:
            audio["artist"] = artist
        audio.save()
    elif ext == '.m4a':
        audio = MP4(filepath)
        audio.tags["\xa9nam"] = [title]
        if artist:
            audio.tags["\xa9ART"] = [artist]
        audio.save()
    elif ext == '.ogg':
        audio = OggVorbis(filepath)
        audio["title"] = [title]
        if artist:
            audio["artist"] = [artist]
        audio.save()


def write_metadata_to_file(filepath, meta, cover_data, overwrite=False):
    """Записывает метаданные в файл. НЕ перезаписывает существующие поля (если overwrite=False).

    Пишутся title, artist, album, year, genre, номер трека, album artist и
    обложка. При overwrite=False каждое поле ставится, только если своего в
    файле нет, — прогон дозаполняет, но ничего не теряет.
    """
    if not HAS_MUTAGEN:
        return False
    p = Path(filepath)
    ext = p.suffix.lower()

    try:
        if ext == '.mp3':
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                from mutagen.id3 import ID3 as ID3Class
                tags = ID3Class()

            # Only fill empty fields unless overwrite=True
            if meta.get('title') and (overwrite or not tags.get('TIT2')):
                tags.setall('TIT2', [TIT2(encoding=3, text=meta['title'])])
            if meta.get('artist') and (overwrite or not tags.get('TPE1')):
                tags.setall('TPE1', [TPE1(encoding=3, text=meta['artist'])])
            if meta.get('album') and (overwrite or not tags.get('TALB')):
                tags.setall('TALB', [TALB(encoding=3, text=meta['album'])])
            if meta.get('year') and (overwrite or not tags.get('TDRC')):
                tags.setall('TDRC', [TDRC(encoding=3, text=meta['year'])])
            if meta.get('genre') and (overwrite or not tags.get('TCON')):
                tags.setall('TCON', [TCON(encoding=3, text=meta['genre'])])
            if meta.get('track') and (overwrite or not tags.get('TRCK')):
                tags.setall('TRCK', [TRCK(encoding=3, text=str(meta['track']))])
            if meta.get('album_artist') and (overwrite or not tags.get('TPE2')):
                tags.setall('TPE2', [TPE2(encoding=3, text=meta['album_artist'])])
            # Cover: only add if no existing cover
            has_cover = any(k.startswith('APIC') for k in tags)
            if cover_data and (overwrite or not has_cover):
                tags.setall('APIC', [APIC(
                    encoding=3, mime='image/jpeg', type=3,
                    desc='Cover', data=cover_data
                )])
            tags.save(filepath, v2_version=3)
            return True

        elif ext == '.flac':
            audio = FLAC(filepath)
            if meta.get('title') and (overwrite or not audio.get('title')):
                audio['title'] = meta['title']
            if meta.get('artist') and (overwrite or not audio.get('artist')):
                audio['artist'] = meta['artist']
            if meta.get('album') and (overwrite or not audio.get('album')):
                audio['album'] = meta['album']
            if meta.get('year') and (overwrite or not audio.get('date')):
                audio['date'] = meta['year']
            if meta.get('genre') and (overwrite or not audio.get('genre')):
                audio['genre'] = meta['genre']
            if meta.get('track') and (overwrite or not audio.get('tracknumber')):
                audio['tracknumber'] = str(meta['track'])
            if meta.get('album_artist') and (overwrite or not audio.get('albumartist')):
                audio['albumartist'] = meta['album_artist']
            if cover_data and (overwrite or not audio.pictures):
                pic = Picture()
                pic.type = 3
                pic.mime = 'image/jpeg'
                pic.data = cover_data
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
            return True

        elif ext == '.m4a':
            audio = MP4(filepath)
            if audio.tags is None:
                audio.add_tags()
            if meta.get('title') and (overwrite or not audio.tags.get('\xa9nam')):
                audio.tags['\xa9nam'] = [meta['title']]
            if meta.get('artist') and (overwrite or not audio.tags.get('\xa9ART')):
                audio.tags['\xa9ART'] = [meta['artist']]
            # Здесь не было проверки на затирание — единственное место, где
            # «заполнение пустого» молча переписывало существующий альбом.
            if meta.get('album') and (overwrite or not audio.tags.get('\xa9alb')):
                audio.tags['\xa9alb'] = [meta['album']]
            if meta.get('year') and (overwrite or not audio.tags.get('\xa9day')):
                audio.tags['\xa9day'] = [meta['year']]
            if meta.get('genre') and (overwrite or not audio.tags.get('\xa9gen')):
                audio.tags['\xa9gen'] = [meta['genre']]
            if meta.get('track') and (overwrite or not audio.tags.get('trkn')):
                audio.tags['trkn'] = [(int(meta['track']), 0)]
            if meta.get('album_artist') and (overwrite or not audio.tags.get('aART')):
                audio.tags['aART'] = [meta['album_artist']]
            if cover_data and (overwrite or not audio.tags.get('covr')):
                audio.tags['covr'] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            return True

    except Exception:
        pass
    return False


def meta_is_complete(existing):
    """Файлу больше нечего дозаполнять.

    Раньше проверялись только исполнитель, альбом и обложка — и файл с ними
    пропускался навсегда, так что жанр, год и номер трека в него уже никогда не
    попадали. Теперь в условие входят и они.
    """
    return bool(existing.get("artist") and existing.get("album") and existing.get("cover")
                and existing.get("genre") and existing.get("year") and existing.get("track_no"))


def _meta_done_path(music_dir):
    return Path(music_dir) / ".vinyl_meta_done.json"


def _load_meta_done(music_dir):
    p = _meta_done_path(music_dir)
    if p.exists():
        try:
            return set(json.loads(p.read_text()))
        except Exception:
            pass
    return set()


def _save_meta_done(music_dir, done_set):
    p = _meta_done_path(music_dir)
    p.write_text(json.dumps(sorted(done_set), ensure_ascii=False))


def metadata_worker(music_dir, username=""):
    """Сканирует и собирает предложения по метаданным (без записи)."""
    meta_state = get_meta_state(username)
    meta_state["running"] = True
    meta_state["done"] = False
    meta_state["cancel"] = False
    meta_state["log"] = []
    meta_state["progress"] = 0
    meta_state["proposals"] = []

    files = sorted(Path(music_dir).iterdir(), key=lambda f: f.name)
    track_files = [f for f in files if f.suffix.lower() in SUPPORTED_FORMATS and f.is_file()]
    meta_state["total"] = len(track_files)

    done_set = _load_meta_done(music_dir)
    found_count = 0

    for i, f in enumerate(track_files):
        if meta_state["cancel"]:
            meta_state["log"].append("\n  Отменено.")
            break

        meta_state["progress"] = i + 1

        if f.name in done_set:
            continue

        existing = get_metadata(str(f))
        if meta_is_complete(existing):
            done_set.add(f.name)
            continue

        artist, title = parse_track_name(f.name)
        meta_state["log"].append("  Ищу: {} - {}...".format(artist or '?', title))

        found_meta = search_metadata(artist, title)
        if not found_meta:
            meta_state["log"].append("    Не найдено")
            time.sleep(0.3)
            continue

        has_cover = bool(fetch_cover_art(found_meta)) if found_meta else False

        proposal = {
            "file": f.name,
            "old_artist": existing.get("artist", ""),
            "old_title": existing.get("title", f.stem),
            "old_album": existing.get("album", ""),
            "old_has_cover": existing.get("cover") is not None,
            "new_artist": found_meta.get("artist", ""),
            "new_title": found_meta.get("title", ""),
            "new_album": found_meta.get("album", ""),
            "new_year": found_meta.get("year", ""),
            "new_has_cover": has_cover,
            "checked": True,
        }
        # Store full meta for apply phase
        proposal["_meta"] = found_meta
        meta_state["proposals"].append(proposal)
        found_count += 1
        meta_state["log"].append("    Найдено: {} - {} ({})".format(
            found_meta.get('artist', '?'), found_meta.get('title', '?'),
            found_meta.get('album', '')))
        time.sleep(0.3)

    meta_state["log"].append("\nСканирование завершено. Найдено предложений: {}".format(found_count))
    meta_state["running"] = False
    meta_state["done"] = True


def metadata_bulk_worker(music_dir, files, username=""):
    """Fetch & WRITE metadata for a specific set of files (multi-select bulk
    action). Fill-only: never clobbers existing tags, only fills what's missing
    (album/year/cover). Runs in the background; progress via meta_state."""
    meta_state = get_meta_state(username)
    meta_state["running"] = True
    meta_state["done"] = False
    meta_state["cancel"] = False
    meta_state["log"] = []
    meta_state["progress"] = 0
    meta_state["total"] = len(files)
    meta_state["proposals"] = []

    done_set = _load_meta_done(music_dir)
    written = 0
    try:
        for i, fname in enumerate(files):
            if meta_state["cancel"]:
                break
            meta_state["progress"] = i + 1
            fp = Path(music_dir) / fname
            if not fp.is_file():
                continue
            existing = get_metadata(str(fp))
            if meta_is_complete(existing):
                done_set.add(fname)
                continue
            # Запрос строим по тегам, если они есть, и только иначе по имени
            # файла. Дозаполнение теперь доходит и до хорошо оттегированных
            # файлов, а у них имя часто хуже тега («088. 01 - From Zero.flac»
            # против «Linkin Park — From Zero»).
            artist = existing.get("artist") or ""
            title = existing.get("title") or ""
            if not artist or not title:
                fa, ft = parse_track_name(fname)
                artist = artist or fa
                title = title or ft
            found = search_metadata(artist, title)
            if found:
                found = enrich_new_tags(found, artist, title)
                # Обложку тянем, только если своей нет: это самый тяжёлый запрос
                # в проходе, а дозаполнение тегов её не требует.
                cover = fetch_cover_art(found) if not existing.get("cover") else None
                if write_metadata_to_file(str(fp), found, cover, overwrite=False):
                    written += 1
                    done_set.add(fname)
                    meta_state["log"].append("  OK: " + fname)
                else:
                    meta_state["log"].append("  Без изменений: " + fname)
            else:
                meta_state["log"].append("  Не найдено: " + fname)
            time.sleep(0.2)
        _save_meta_done(music_dir, done_set)
        meta_state["log"].append("\nГотово. Обновлено: {}".format(written))
    except Exception:
        meta_state["log"].append("ОШИБКА при поиске мета-данных")
    finally:
        meta_state["running"] = False
        meta_state["done"] = True


def metadata_apply(music_dir, proposals, username=""):
    """Применяет подтверждённые предложения метаданных."""
    meta_state = get_meta_state(username)
    meta_state["running"] = True
    meta_state["done"] = False
    meta_state["log"] = ["Применяю метаданные..."]
    meta_state["progress"] = 0
    meta_state["total"] = len(proposals)

    done_set = _load_meta_done(music_dir)
    applied = 0

    for i, p in enumerate(proposals):
        meta_state["progress"] = i + 1
        filepath = Path(music_dir) / p["file"]
        if not filepath.exists():
            continue
        found_meta = p.get("_meta", {})
        if not found_meta:
            continue
        found_meta = enrich_new_tags(found_meta, p.get("old_artist", ""), p.get("old_title", ""))
        cover_data = fetch_cover_art(found_meta)
        if write_metadata_to_file(str(filepath), found_meta, cover_data):
            applied += 1
            done_set.add(p["file"])
            meta_state["log"].append("  OK: " + p["file"])
        else:
            meta_state["log"].append("  Ошибка: " + p["file"])

    _save_meta_done(music_dir, done_set)
    meta_state["log"].append("\nПрименено: {}/{}".format(applied, len(proposals)))
    meta_state["running"] = False
    meta_state["done"] = True
    meta_state["proposals"] = []


# ──────────────────── HTML ────────────────────

SW_JS = r"""
var CACHE_APP = 'app-BUILD_HASH';

self.addEventListener('install', function(e) {
  e.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', function(e) {
  e.waitUntil(
    caches.keys().then(function(names) {
      var stale = names.filter(function(n) {
        return n.startsWith('app-') && n !== CACHE_APP;
      });
      return Promise.all(stale.map(function(n) { return caches.delete(n); }))
        .then(function() { return stale.length > 0; });
    }).then(function(wasUpdate) {
      return self.clients.claim().then(function() {
        // Страница отдаётся из кэша сразу (см. обработчик fetch), поэтому после
        // обновления сервера первое обновление показывало бы старую сборку, а
        // новую — только второе. Активация нового воркера означает новый билд:
        // просим открытые вкладки перезагрузиться. Клиент это сообщение уже
        // слушал, но никто его не слал.
        // Только при обновлении: на первой установке воркера перезагружать
        // нечего, страница и так свежая.
        if (!wasUpdate) return;
        return self.clients.matchAll({type: 'window'}).then(function(list) {
          list.forEach(function(c) { c.postMessage({action: 'reload'}); });
        });
      });
    })
  );
});

// Helper: open IndexedDB from Service Worker
function openIDB() {
  return new Promise(function(resolve, reject) {
    var req = indexedDB.open('vinylCache', 1);
    req.onupgradeneeded = function(e) {
      var db = e.target.result;
      if (!db.objectStoreNames.contains('audio')) db.createObjectStore('audio');
    };
    req.onsuccess = function(e) { resolve(e.target.result); };
    req.onerror = function() { reject(); };
  });
}

function getFromIDB(key) {
  return openIDB().then(function(db) {
    return new Promise(function(resolve, reject) {
      var tx = db.transaction('audio', 'readonly');
      var req = tx.objectStore('audio').get(key);
      req.onsuccess = function() { resolve(req.result || null); };
      req.onerror = function() { resolve(null); };
    });
  }).catch(function() { return null; });
}

// Auto-retries every 2s; when /api/version succeeds, reloads. Avoids stranding
// users on a dead-end page when the server briefly went away (restart, LAN/HTTPS
// toggle, network blip).
var OFFLINE_FALLBACK = '<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head><body style="background:#111;color:#eee;font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0"><div style="padding:24px;max-width:340px;width:100%;text-align:center"><h2 style="color:#e94560;margin:0 0 12px">Сервер недоступен</h2><p id="s" style="color:rgba(255,255,255,0.5);font-size:14px;margin:0 0 16px">Жду подключения...</p><button id="r" style="padding:10px 20px;border:none;border-radius:8px;background:#e94560;color:#fff;font-size:14px;cursor:pointer">Попробовать сейчас</button><div style="margin-top:24px;padding-top:16px;border-top:1px solid rgba(255,255,255,0.08);text-align:left"><div style="color:rgba(255,255,255,0.4);font-size:12px;margin-bottom:8px">Сменился IP компьютера? Укажите адрес вручную:</div><div id="k"></div><div style="display:flex;gap:6px;margin-top:6px"><input id="h" placeholder="192.168.1.50" autocapitalize="off" autocorrect="off" spellcheck="false" style="flex:1;min-width:0;padding:9px 10px;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:rgba(255,255,255,0.05);color:#eee;font-size:14px"><button id="b" style="padding:9px 14px;border:none;border-radius:8px;background:#e94560;color:#fff;font-size:13px;cursor:pointer">Перейти</button></div><div style="color:rgba(255,255,255,0.25);font-size:11px;margin-top:10px;line-height:1.5">Кэш треков привязан к адресу и на новый IP не переедет.</div></div></div><script>(function(){var t=0;function ping(){t++;document.getElementById("s").textContent="Жду подключения... ("+t+")";fetch("/api/version",{cache:"no-store"}).then(function(r){return r.json()}).then(function(d){if(d&&d.version)location.reload()}).catch(function(){});}setInterval(ping,2000);ping();document.getElementById("r").onclick=function(){location.reload()};function loopback(h){h=(h||"").toLowerCase();return h==="localhost"||h==="127.0.0.1";}function norm(v){v=(v||"").trim();while(v.length&&v.charAt(v.length-1)==="/")v=v.slice(0,-1);if(!v)return "";if(v.indexOf("://")<0){v=(loopback(v.split("/")[0].split(":")[0])?"http://":"https://")+v;}try{var u=new URL(v);if(!u.port)u.port=loopback(u.hostname)?"7666":"SW_PORT";return u.protocol+"//"+u.host;}catch(e){return "";}}function go(v){var u=norm(v);if(!u)return;try{var l=JSON.parse(localStorage.getItem("_vc_hosts")||"[]").filter(function(x){return x!==u});l.unshift(u);localStorage.setItem("_vc_hosts",JSON.stringify(l.slice(0,8)));}catch(e){}location.href=u+"/";}document.getElementById("b").onclick=function(){go(document.getElementById("h").value)};document.getElementById("h").onkeydown=function(e){if(e.key==="Enter")go(this.value)};var seen=[],cfg={};try{cfg=JSON.parse(localStorage.getItem("_vc_config")||"{}")}catch(e){}function add(u){u=norm(u);if(u&&u!==location.origin&&seen.indexOf(u)<0)seen.push(u)}add(cfg.lan_host_url);(cfg.all_urls||[]).forEach(add);try{JSON.parse(localStorage.getItem("_vc_hosts")||"[]").forEach(add)}catch(e){}var k=document.getElementById("k");seen.forEach(function(u){var b=document.createElement("button");b.textContent=u;b.style.cssText="display:block;width:100%;margin-bottom:6px;padding:9px 10px;border:1px solid rgba(255,255,255,0.12);border-radius:8px;background:rgba(255,255,255,0.05);color:#eee;font-size:13px;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:left";b.onclick=function(){go(u)};k.appendChild(b)});})();</script></body></html>';

self.addEventListener('fetch', function(e) {
  var url = new URL(e.request.url);

  // Cross-origin requests are none of our business: the path rules below are
  // written for this server, and swallowing another host's /api/* call would
  // make an unreachable address look alive when probing for a moved server.
  if (url.origin !== self.location.origin) return;

  // Audio streams and covers — do NOT intercept, let browser handle directly.
  // Offline playback is handled client-side via IndexedDB blob URLs.
  // Intercepting audio breaks iOS PWA standalone mode (Range request issues).
  if (url.pathname.startsWith('/api/stream/') || url.pathname.startsWith('/api/cover/') || url.pathname === '/reset' || url.pathname === '/icon.png') {
    return;
  }

  // App shell — stale-while-revalidate, but never cache login page
  if (url.pathname === '/' || url.pathname === '/index.html') {
    e.respondWith(
      caches.open(CACHE_APP).then(function(cache) {
        return cache.match('/').then(function(cached) {
          var fetchPromise = fetch(e.request).then(function(resp) {
            // Only cache the main app page (large), not login page (small)
            if (resp.ok && resp.headers.get('content-length') > 10000) {
              cache.put('/', resp.clone());
            }
            return resp;
          });
          // Only serve from cache if it's the real app page, not login
          if (cached && cached.headers.get('content-length') > 10000) {
            return cached;
          }
          return fetchPromise.catch(function() {
            return cached || new Response(OFFLINE_FALLBACK, {headers:{'Content-Type':'text/html'}});
          });
        });
      })
    );
    return;
  }

  // API calls — network only, return offline JSON on failure
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(fetch(e.request).catch(function() {
      return new Response(JSON.stringify({error:'offline'}), {headers:{'Content-Type':'application/json'}});
    }));
    return;
  }
});
"""

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#1a1a1a">
<link rel="apple-touch-icon" sizes="180x180" href="/icon.png">
<link rel="icon" type="image/png" sizes="180x180" href="/icon.png">
<title></title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; user-select: none; -webkit-user-select: none; }
input, textarea { user-select: text; -webkit-user-select: text; }

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #111;
  color: #eee;
  overflow: hidden;
  height: 100vh; height: 100dvh;
}

/* ── Animated background layer ── */
.bg-canvas {
  position: fixed; inset: 0; z-index: -1;
}
.bg-canvas::after {
  content: ''; position: absolute; inset: 0; opacity: 0.35;
  background: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence baseFrequency='0.9' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='200' height='200' filter='url(%23n)' opacity='0.08'/%3E%3C/svg%3E");
  pointer-events: none; z-index: 1;
}
.bg-canvas canvas { width: 100%; height: 100%; display: block; }

/* ── Layout ── */
.app { position: relative; height: 100vh; height: 100dvh; }
.vinyl-side { position: absolute; inset: 0; right: 360px; display: flex; flex-direction: column; align-items: center; justify-content: center; transition: right 0.25s ease; overflow: hidden; touch-action: none; }
.playlist-side {
  position: absolute; top: 0; right: 0; bottom: 0; width: 360px;
  background: rgba(0,0,0,0.4); backdrop-filter: blur(20px);
  display: flex; flex-direction: column; border-left: 1px solid rgba(255,255,255,0.06);
  z-index: 15; overflow: hidden;
  transition: transform 0.25s ease;
}
.sidebar-collapsed .playlist-side { transform: translateX(100%); }
.sidebar-collapsed .vinyl-side { right: 0; }

/* ── Player mode toggle ── */
.player-mode-toggle {
  position: absolute; top: 12px; right: 12px; z-index: 20;
  display: flex; gap: 4px;
  background: rgba(255,255,255,0.06); border-radius: 8px; padding: 3px;
}
.player-mode-btn {
  width: 40px; height: 40px; border: none; border-radius: 8px; background: none;
  color: rgba(255,255,255,0.3); cursor: pointer; display: flex; align-items: center; justify-content: center;
  transition: all 0.25s ease;
}
.player-mode-btn.active { background: rgba(255,255,255,0.1); color: #e94560; }
.player-mode-btn:hover { color: rgba(255,255,255,0.6); }

/* ── Vinyl Scene ── */
.vinyl-scene {
  position: relative;
  width: min(55vw, 55vh);
  height: min(55vw, 55vh);
  container-type: inline-size;
}

/* ── iPod Classic ── */
.ipod-scene { display: none; }
.player-mode-ipod .ipod-scene { display: flex; flex-direction: column; align-items: center; justify-content: center; }
.player-mode-ipod .vinyl-scene { display: none; }
.player-mode-ipod .track-info { display: none; }
.player-mode-ipod .controls { display: none; }
.player-mode-ipod .progress-wrap { display: none; }
.player-mode-ipod .volume-wrap { display: none; }

/* iPod Dark (default) — graphite aluminum with 3D volume */
.ipod-body {
  width: min(28vw, 34vh); min-width: 200px;
  aspect-ratio: 0.6;
  background:
    linear-gradient(90deg,
      #555 0%, #4a4a4e 3%, #434347 8%, #3e3e42 20%,
      #3c3c40 40%, #3c3c40 60%,
      #3e3e42 80%, #434347 92%, #4a4a4e 97%, #555 100%);
  border-radius: 18px;
  position: relative;
  box-shadow:
    0 24px 60px rgba(0,0,0,0.65),
    0 6px 16px rgba(0,0,0,0.4),
    inset 0 2px 1px rgba(255,255,255,0.15),
    inset 0 -2px 1px rgba(0,0,0,0.25),
    inset 4px 0 6px -2px rgba(255,255,255,0.08),
    inset -4px 0 6px -2px rgba(255,255,255,0.08);
}
/* Brushed aluminum texture + top highlight */
.ipod-body::before {
  content: ''; position: absolute; inset: 0; border-radius: 18px;
  background:
    repeating-linear-gradient(90deg, transparent, transparent 1px, rgba(255,255,255,0.01) 1px, rgba(255,255,255,0.01) 2px),
    linear-gradient(180deg, rgba(255,255,255,0.06) 0%, transparent 15%, transparent 85%, rgba(0,0,0,0.08) 100%),
    radial-gradient(ellipse at 35% 10%, rgba(255,255,255,0.1) 0%, transparent 40%);
  pointer-events: none;
}
/* Inner bevel for depth */
.ipod-body::after {
  content: ''; position: absolute; inset: 2px; border-radius: 16px;
  border-top: 1px solid rgba(255,255,255,0.08);
  border-bottom: 1px solid rgba(0,0,0,0.2);
  border-left: 1px solid rgba(255,255,255,0.04);
  border-right: 1px solid rgba(255,255,255,0.04);
  pointer-events: none;
}


/* Screen */
.ipod-screen {
  position: absolute; top: 5%; left: 9%; right: 9%; height: 40%;
  background: #1a1a1a;
  border-radius: 3px;
  box-shadow: inset 0 2px 8px rgba(0,0,0,0.8), 0 1px 0 rgba(255,255,255,0.08);
  overflow: hidden;
  display: flex; flex-direction: column;
}
/* Screen glass reflection */
.ipod-screen::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 40%;
  background: linear-gradient(180deg, rgba(255,255,255,0.06) 0%, transparent 100%);
  pointer-events: none; z-index: 5;
}

/* Screen content — now playing */
.ipod-np {
  flex: 1; display: flex; flex-direction: column;
  padding: 6%; color: #eee; font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
}
.ipod-np-header {
  font-size: clamp(7px, 1.8vmin, 10px); text-align: center;
  border-bottom: 1px solid rgba(255,255,255,0.15);
  padding-bottom: 3px; margin-bottom: 4px;
  font-weight: 600; letter-spacing: 0.5px; color: rgba(255,255,255,0.6);
}
.ipod-np-body {
  flex: 1; display: flex; gap: 6%; align-items: center;
  min-height: 0; overflow: hidden;
}
.ipod-np-cover {
  width: 42%; aspect-ratio: 1; border-radius: 2px; flex-shrink: 0;
  background: #333; display: flex; align-items: center; justify-content: center;
  overflow: hidden;
}
.ipod-np-cover img { width: 100%; height: 100%; object-fit: cover; }
.ipod-np-cover-ph { color: rgba(255,255,255,0.15); font-size: 20px; }
.ipod-np-info {
  flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;
}
.ipod-np-title {
  font-size: clamp(8px, 2vmin, 12px); font-weight: 700; color: #fff;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ipod-np-artist {
  font-size: clamp(7px, 1.6vmin, 10px); color: rgba(255,255,255,0.5);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  cursor: pointer; border-radius: 2px; padding: 0 3px; margin-left: -3px;
  transition: background 0.15s;
}
.ipod-np-artist:hover { background: rgba(255,255,255,0.1); }
.ipod-np-album {
  font-size: clamp(6px, 1.4vmin, 9px); color: rgba(255,255,255,0.35);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ipod-np-progress { margin-top: auto; padding-top: 4px; }
.ipod-np-bar {
  height: 3px; background: rgba(255,255,255,0.12); border-radius: 2px; overflow: hidden;
}
.ipod-np-bar-fill {
  height: 100%; background: #4a9eff; width: 0%; transition: width 0.3s linear;
}
.ipod-np-time {
  display: flex; justify-content: space-between;
  font-size: clamp(6px, 1.2vmin, 8px); color: rgba(255,255,255,0.3); margin-top: 2px;
}

/* Screen — track list mode */
.ipod-list {
  flex: 1; display: none; flex-direction: column;
  color: #eee; font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
  overflow: hidden;
}
.ipod-list.active { display: flex; }
.ipod-np-wrap { display: flex; flex-direction: column; flex: 1; }
.ipod-np-wrap.hidden { display: none; }
.ipod-list-header {
  font-size: clamp(7px, 1.8vmin, 10px); text-align: center;
  border-bottom: 1px solid rgba(255,255,255,0.15);
  padding: 6% 6% 3px; font-weight: 600; letter-spacing: 0.5px;
  color: rgba(255,255,255,0.6); flex-shrink: 0;
}
.ipod-list-items {
  flex: 1; overflow: hidden;
}
.ipod-list-item {
  padding: 3px 6%; border-bottom: 1px solid rgba(255,255,255,0.06);
  font-size: clamp(7px, 1.6vmin, 10px);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  cursor: pointer; transition: background 0.1s, color 0.1s;
}
.ipod-list-item.selected {
  background: #4a9eff; color: #fff;
}

/* Click Wheel — dark */
.ipod-wheel {
  position: absolute; bottom: 8%; left: 50%; transform: translateX(-50%);
  width: 64%; aspect-ratio: 1;
  border-radius: 50%;
  background: radial-gradient(circle at 48% 45%, #3a3a3e, #2e2e32 40%, #262628 80%, #1e1e20 100%);
  box-shadow:
    0 2px 8px rgba(0,0,0,0.3),
    inset 0 1px 1px rgba(255,255,255,0.06);
  cursor: pointer; user-select: none; touch-action: none;
  -webkit-user-select: none;
}
/* Center button */
.ipod-wheel-center {
  position: absolute; top: 50%; left: 50%;
  width: 36%; height: 36%;
  transform: translate(-50%, -50%);
  border-radius: 50%;
  background: radial-gradient(circle at 48% 45%, #4a4a4e, #3a3a3e 60%, #2e2e32 100%);
  box-shadow:
    0 1px 4px rgba(0,0,0,0.3),
    inset 0 1px 1px rgba(255,255,255,0.08);
  cursor: pointer; z-index: 2;
}
.ipod-wheel-center:active { background: radial-gradient(circle, #3a3a3e, #2e2e32); }

/* Wheel labels */
.ipod-wheel-label {
  position: absolute; font-size: clamp(7px, 1.4vmin, 10px); font-weight: 600;
  color: rgba(200,200,210,0.5); pointer-events: none;
  font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
}
.ipod-wl-menu { top: 8%; left: 50%; transform: translateX(-50%); }
.ipod-wl-fwd { right: 8%; top: 50%; transform: translateY(-50%); font-size: clamp(10px,2vmin,14px); }
.ipod-wl-back { left: 8%; top: 50%; transform: translateY(-50%); font-size: clamp(10px,2vmin,14px); }
.ipod-wl-play { bottom: 8%; left: 50%; transform: translateX(-50%); font-size: clamp(8px,1.6vmin,12px); }

/* ── Cassette ── */
.cassette-scene { display: none; }
.player-mode-cassette .cassette-scene { display: flex; align-items: center; justify-content: center; }
.player-mode-cassette .vinyl-scene { display: none; }
.player-mode-cassette .track-info { display: none; }

.cassette-body {
  --cw: min(54vw, 46vh);
  width: var(--cw); aspect-ratio: 1.6;
  min-width: 300px;
  background:
    linear-gradient(90deg,
      #3a3530 0%, #33302b 4%, #2e2b26 10%, #2a2723 30%,
      #282520 50%,
      #2a2723 70%, #2e2b26 90%, #33302b 96%, #3a3530 100%);
  border-radius: 10px 10px 5px 5px;
  position: relative;
  box-shadow:
    0 14px 50px rgba(0,0,0,0.6),
    0 2px 4px rgba(0,0,0,0.4),
    inset 0 1px 0 rgba(255,255,255,0.08),
    inset 0 -1px 0 rgba(0,0,0,0.3),
    inset 3px 0 4px -2px rgba(255,255,255,0.05),
    inset -3px 0 4px -2px rgba(255,255,255,0.05);
}
/* Horizontal stripes across body */
.cassette-stripes {
  position: absolute; left: 0; right: 0; top: 0; bottom: 0; border-radius: 10px 10px 5px 5px;
  pointer-events: none; overflow: hidden; z-index: 1;
}
.cassette-stripes::before {
  content: ''; position: absolute; left: 0; right: 0; top: 32%; height: 10%;
  background:
    repeating-linear-gradient(180deg,
      transparent, transparent 3px,
      rgba(255,255,255,0.03) 3px, rgba(255,255,255,0.03) 4px
    );
}

/* Label — horizontal strip at top only */
.cassette-label {
  position: absolute; top: 4%; left: 6%; right: 6%; height: 28%;
  background: linear-gradient(180deg, #b5ae9e 0%, #b0a999 30%, #aaa393 70%, #a59e8e 100%);
  border-radius: 3px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.25), inset 0 0 0 1px rgba(0,0,0,0.04);
  display: flex; flex-direction: column; align-items: stretch; justify-content: center;
  padding: 4px 14px; overflow: hidden;
}
/* Faint ruled lines */
.cassette-label::before {
  content: ''; position: absolute; inset: 0;
  background: repeating-linear-gradient(0deg, transparent, transparent 8px, rgba(0,0,0,0.04) 8px, rgba(0,0,0,0.04) 9px);
  pointer-events: none;
}
/* Subtle top edge line */
.cassette-label::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: rgba(0,0,0,0.08);
  border-radius: 3px 3px 0 0;
}
.cassette-label-content {
  display: flex; align-items: center; gap: 10px;
  position: relative; z-index: 1; width: 100%;
  overflow: hidden;
}
.cassette-cover-wrap {
  width: clamp(26px, 5vmin, 38px); height: clamp(26px, 5vmin, 38px);
  flex-shrink: 0; position: relative; border-radius: 3px; overflow: hidden;
  background: linear-gradient(135deg, #9e9888, #8f8978);
  box-shadow: 0 1px 3px rgba(0,0,0,0.2);
}
.cassette-cover {
  width: 100%; height: 100%; object-fit: cover;
  position: absolute; inset: 0;
}
.cassette-cover-placeholder {
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
  color: rgba(0,0,0,0.15); font-size: 20px;
  position: absolute; inset: 0;
}
.cassette-label-text {
  flex: 1; min-width: 0;
}
.cassette-label-title {
  font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
  font-size: clamp(11px, 3vmin, 15px); font-weight: 600; color: #2a2520;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  line-height: 1.3;
}
.cassette-label-artist {
  font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
  font-size: clamp(8px, 1.8vmin, 11px); font-weight: 500; color: #5a5548;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  line-height: 1.3; letter-spacing: 0.3px; text-transform: uppercase; margin-top: 1px;
  cursor: pointer; border-radius: 3px; padding: 1px 4px; margin-left: -4px;
  transition: background 0.15s ease;
}
.cassette-label-artist:hover { background: rgba(0,0,0,0.08); }
.cassette-label-artist:active { background: rgba(0,0,0,0.12);
}
.cassette-label-brand {
  position: absolute; bottom: 3px; right: 8px;
  font-size: clamp(6px, 1.5vmin, 8px); font-weight: 700; color: rgba(0,0,0,0.1);
  letter-spacing: 2px; text-transform: uppercase;
}
.cassette-label-side {
  position: absolute; bottom: 3px; left: 8px;
  font-size: clamp(7px, 1.5vmin, 9px); font-weight: 800; color: rgba(0,0,0,0.15);
}

/* Tape window */
.cassette-window {
  position: absolute; top: 37%; left: 16%; right: 16%; bottom: 18%;
  background: radial-gradient(ellipse at center, #1e1c18, #141210);
  border-radius: 5px;
  box-shadow:
    inset 0 2px 6px rgba(0,0,0,0.7),
    inset 0 -1px 2px rgba(0,0,0,0.3),
    0 1px 0 rgba(255,255,255,0.04);
  overflow: visible;
  z-index: 2;
}

/* Reels — sized via JS in px to guarantee perfect circles */
.cassette-reel {
  position: absolute; top: 50%;
  border-radius: 50%;
  background: radial-gradient(circle at 45% 40%, #8a8578 0%, #7d786c 15%, #706b60 30%, #635e54 50%, #565148 75%, #4a4640 100%);
  box-shadow:
    0 0 0 1px rgba(0,0,0,0.4),
    inset 0 1px 2px rgba(255,255,255,0.06),
    inset 0 -1px 2px rgba(0,0,0,0.3);
  transform: translate(-50%, -50%);
  z-index: 2;
}
.cassette-reel-l { left: 30%; }
.cassette-reel-r { left: 70%; }
/* Outer ring ridges */
.cassette-reel::before {
  content: ''; position: absolute; inset: 3%; border-radius: 50%;
  border: 1px solid rgba(0,0,0,0.12);
  box-shadow: inset 0 0 0 2px rgba(0,0,0,0.05);
}
/* Hub — rendered as SVG in HTML for realistic shape */
.cassette-reel-spokes {
  position: absolute; inset: 0; border-radius: 50%; cursor: grab;
}
.cassette-reel-spokes.grabbing { cursor: grabbing; }
.cassette-hub-svg {
  position: absolute; top: 50%; left: 50%;
  width: 50%; height: 50%;
  transform: translate(-50%, -50%);
}

/* Tape wound around reels — sized via JS */
.cassette-tape-spool {
  position: absolute; top: 50%; border-radius: 50%;
  transform: translate(-50%, -50%);
  pointer-events: none; z-index: 1;
  background: conic-gradient(
    from 0deg,
    #2a1a0e, #3a2818, #2a1a0e, #352214, #2a1a0e, #3a2818,
    #2a1a0e, #352214, #2a1a0e, #3a2818, #2a1a0e, #352214
  );
  box-shadow: inset 0 0 3px rgba(0,0,0,0.5);
}
.cassette-tape-spool-l { left: 30%; }
.cassette-tape-spool-r { left: 70%; }

/* Tape path between reels */
.cassette-tape-path {
  position: absolute; bottom: 22%; left: 10%; right: 10%; height: 2px;
  z-index: 3;
}
/* Two lines — tape going from left reel down and across, then up to right reel */
.cassette-tape-path::before {
  content: ''; position: absolute; inset: 0;
  background: #2a1a0e;
}

/* Corner screws */
.cassette-screw {
  position: absolute; width: 12px; height: 12px; border-radius: 50%;
  background: radial-gradient(circle at 35% 35%, #6b665c, #4a4640, #3a3530);
  box-shadow: inset 0 1px 1px rgba(255,255,255,0.12), 0 1px 2px rgba(0,0,0,0.4);
  z-index: 3;
}
.cassette-screw::before {
  content: ''; position: absolute; top: 50%; left: 20%; right: 20%; height: 1px;
  background: rgba(0,0,0,0.6); margin-top: -0.5px;
}
.cassette-screw::after {
  content: ''; position: absolute; left: 50%; top: 20%; bottom: 20%; width: 1px;
  background: rgba(0,0,0,0.6); margin-left: -0.5px;
}
.cs-tl { top: 6px; left: 6px; }
.cs-tr { top: 6px; right: 6px; }
.cs-bl { bottom: 6px; left: 6px; }
.cs-br { bottom: 6px; right: 6px; }

/* Dark panel below label (around tape window) */
.cassette-dark-panel {
  position: absolute; top: 34%; left: 5%; right: 5%; bottom: 15%;
  background: linear-gradient(180deg, #1a1816, #151310, #1a1816);
  border-radius: 2px;
  box-shadow: inset 0 1px 4px rgba(0,0,0,0.5);
  z-index: 1;
}

/* Bottom chin — separate strip like real cassette */
.cassette-bottom {
  position: absolute; bottom: -1px; left: 8%; right: 8%; height: 15%;
  background:
    linear-gradient(90deg, #6a655c 0%, #5e594f 5%, #555045 20%, #4e493e 50%, #555045 80%, #5e594f 95%, #6a655c 100%);
  border-radius: 2px 2px 4px 4px;
  box-shadow:
    0 2px 4px rgba(0,0,0,0.3),
    inset 0 1px 0 rgba(255,255,255,0.08),
    inset 2px 0 3px -1px rgba(255,255,255,0.05),
    inset -2px 0 3px -1px rgba(255,255,255,0.05);
}
.cassette-bottom-holes {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  display: flex; align-items: center; gap: 6px;
}
.cassette-bh-lg {
  width: 9px; height: 9px; border-radius: 50%;
  background: radial-gradient(circle, #0a0a0a, #1a1a1a);
  box-shadow: inset 0 1px 3px rgba(0,0,0,0.9), 0 0 0 1px rgba(0,0,0,0.3);
}
.cassette-bh-md {
  width: 7px; height: 7px; border-radius: 50%;
  background: #111; box-shadow: inset 0 1px 2px rgba(0,0,0,0.9);
}
.cassette-bh-sm {
  width: 5px; height: 5px; border-radius: 50%;
  background: #111; box-shadow: inset 0 1px 2px rgba(0,0,0,0.8);
}

/* ── Vinyl Record ── */
.vinyl-record {
  /* Пластинку JS поворачивает каждый кадр, а заливка у неё дорогая: радиальный
     градиент на четырнадцать переходов, круглое скругление и три тени, одна из
     них внутренняя на 80px. Без этой подсказки браузер при каждом повороте
     перерисовывает её целиком; с ней она растеризуется один раз и дальше только
     поворачивается уже готовой — работа уходит с процессора на композитор.
     Вид не меняется совсем.
     Здесь это безопасно, в отличие от рамки радио: там слой лежит под CSS-маской,
     и на iOS такой слой отрисовывался неполно. У пластинки маски нет. */
  will-change: transform;
  width: 100%; height: 100%; border-radius: 50%;
  background: radial-gradient(circle,
    #1a1a1a 0%, #111 18%, #1a1a1a 19%, #0d0d0d 20%,
    #1a1a1a 38%, #111 39%, #1a1a1a 40%, #0d0d0d 58%,
    #1a1a1a 59%, #111 60%, #1a1a1a 78%, #111 79%, #0d0d0d 100%
  );
  position: relative;
  box-shadow: 0 0 0 6px #222, 0 0 60px rgba(0,0,0,0.6), inset 0 0 80px rgba(0,0,0,0.3);
  transition: box-shadow 0.3s;
}

.vinyl-grooves {
  position: absolute; inset: 10px; border-radius: 50%;
  background: repeating-radial-gradient(circle at center,
    transparent 0px, transparent 2px, rgba(255,255,255,0.025) 2.5px, transparent 3px);
  pointer-events: none;
}

.vinyl-label {
  position: absolute; top: 50%; left: 50%;
  width: 38%; height: 38%; margin: -19% 0 0 -19%;
  border-radius: 50%; overflow: hidden; background: #222;
  box-shadow: 0 0 0 4px #333, 0 0 20px rgba(0,0,0,0.4);
}

.vinyl-cover-placeholder {
  width: 100%; height: 100%; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, #333, #1a1a1a);
  color: rgba(255,255,255,0.3); font-size: 48px;
}

.vinyl-hole {
  position: absolute; top: 50%; left: 50%; width: 14px; height: 14px;
  margin: -7px 0 0 -7px; border-radius: 50%;
  background: #0a0a0a; box-shadow: inset 0 0 4px rgba(0,0,0,0.8), 0 0 0 2px #1a1a1a;
  z-index: 5;
}

/* Vinyl rotation is now controlled by JS */

/* ── Tonearm ── */
.tonearm-pivot {
  position: absolute; top: -2%; right: 4%; z-index: 10;
}
.tonearm-base {
  width: 28px; height: 28px; border-radius: 50%;
  background: radial-gradient(circle, #666, #333);
  box-shadow: 0 2px 12px rgba(0,0,0,0.6);
  position: relative; z-index: 2;
}
.tonearm {
  position: absolute; top: 50%; left: 50%;
  transform-origin: 0 0; transform: rotate(53deg);
}

.vinyl-record { cursor: grab; }
.vinyl-record.grabbing { cursor: grabbing; }
.tonearm-arm {
  width: 52cqi; height: 0.8cqi;
  background: linear-gradient(to right, #999, #777);
  border-radius: 2px; box-shadow: 0 2px 6px rgba(0,0,0,0.4);
}
.tonearm-head {
  position: absolute; right: -2.2cqi; top: -0.8cqi;
  width: 2.2cqi; height: 2.2cqi; min-width: 8px; min-height: 8px;
  background: linear-gradient(to bottom, #aaa, #888);
  border-radius: 1px 1px 2px 2px; box-shadow: 0 2px 4px rgba(0,0,0,0.3);
}
.tonearm-head::after {
  content: ''; position: absolute; bottom: -0.5cqi; left: 50%; margin-left: -0.15cqi;
  width: 0.3cqi; height: 0.6cqi; background: #ccc;
}
.tonearm-counterweight {
  position: absolute; left: -3.5cqi; top: -1.5cqi;
  width: 3.8cqi; height: 3.8cqi; min-width: 14px; min-height: 14px;
  border-radius: 50%;
  background: radial-gradient(circle, #888, #555);
  box-shadow: 0 2px 6px rgba(0,0,0,0.4);
}

/* ── Track info ── */
.track-info {
  text-align: center; margin-top: 28px; height: 52px;
}
.track-title {
  font-size: 22px; font-weight: 600; color: #fff;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 50vw;
  transition: opacity 0.3s ease; line-height: 28px;
}
.track-artist {
  font-size: 15px; color: rgba(255,255,255,0.5); margin-top: 2px;
  transition: opacity 0.3s ease; line-height: 20px; min-height: 20px;
}
.artist-link {
  cursor: pointer; border-radius: 4px; padding: 1px 6px; margin: -1px -6px;
  transition: background 0.15s ease;
}
.artist-link:hover { background: rgba(255,255,255,0.1); }
.artist-link:active { background: rgba(255,255,255,0.15); }
.vinyl-cover-img { width: 100%; height: 100%; object-fit: cover; transition: opacity 0.4s ease; }

/* ── Controls ── */
.controls {
  display: flex; align-items: center; gap: 20px; margin-top: 24px;
}
.ctrl-btn {
  width: 48px; height: 48px; border-radius: 50%; border: none;
  background: rgba(255,255,255,0.1); color: #fff; font-size: 20px;
  cursor: pointer; display: flex; align-items: center; justify-content: center;
  transition: background 0.2s;
}
.ctrl-btn:hover { background: rgba(255,255,255,0.2); }
.ctrl-btn.play-btn {
  width: 60px; height: 60px; font-size: 24px;
  background: rgba(255,255,255,0.15);
}

/* ── Progress bar ── */
.progress-wrap {
  width: min(50vw, 400px); margin-top: 16px; cursor: pointer;
  touch-action: none; -webkit-user-select: none; user-select: none;
  /* enlarge the finger target above/below the thin bar */
  padding: 10px 0; margin-top: 6px;
}
.progress-bg {
  width: 100%; height: 4px; background: rgba(255,255,255,0.15);
  border-radius: 2px; position: relative;
}
.progress-wrap.dragging .progress-fill { transition: none; }
.progress-wrap.dragging .progress-bg { height: 6px; }
.progress-fill {
  height: 100%; background: #e94560; border-radius: 2px; width: 0%;
  transition: width 0.3s linear;
}
.time-display {
  display: flex; justify-content: space-between; font-size: 11px;
  color: rgba(255,255,255,0.4); margin-top: 4px;
}

/* ── Volume ── */
.volume-wrap {
  display: flex; align-items: center; gap: 8px; margin-top: 8px;
}
.volume-wrap input[type=range] {
  width: 100px; accent-color: #e94560;
}

/* ── Playlist ── */
.playlist-header {
  padding: 12px 12px; border-bottom: 1px solid rgba(255,255,255,0.06);
  font-size: 14px; font-weight: 600; color: rgba(255,255,255,0.6);
}
.playlist-tabs {
  display: flex; border-bottom: 1px solid rgba(255,255,255,0.06);
}
.playlist-tab {
  flex: 1; padding: 10px; text-align: center; font-size: 13px;
  color: rgba(255,255,255,0.4); cursor: pointer; border: none; background: none;
  transition: color 0.2s;
}
.playlist-tab.active { color: #e94560; border-bottom: 2px solid #e94560; }
#tabNew { letter-spacing: 0.06em; font-weight: 600; position: relative; }

/* У отрывка перемотки нет, поэтому полосу прячем — но местом она остаётся:
   display:none сдвинул бы вверх регулятор громкости, и интерфейс дёргался бы
   на каждом переключении между отрывком и обычным треком. */
#progressWrap { transition: opacity 0.3s ease; }
#progressWrap.no-seek { opacity: 0; pointer-events: none; }
/* Отрывок играет в отдельном элементе, и ползунок им не управляет. Прячем так
   же, как полосу перемотки: место остаётся, поэтому кнопка перемешивания рядом
   не прыгает. */
.volume-wrap > span, .volume-wrap > input[type=range] { transition: opacity 0.3s ease; }
.volume-wrap.no-volume > span,
.volume-wrap.no-volume > input[type=range] { opacity: 0; pointer-events: none; }

.fmt-badge.fmt-drops {
  background: rgba(233,69,96,0.18); color: #ff7a90; border-color: rgba(233,69,96,0.45);
  letter-spacing: 0.06em;
}

.perf-row {
  display: flex; gap: 9px; align-items: flex-start; margin-bottom: 9px; cursor: pointer;
}
.perf-row input { margin-top: 2px; flex-shrink: 0; }
.perf-row b { display: block; font-size: 12.5px; font-weight: 600; color: rgba(255,255,255,0.8); }
.perf-row i {
  display: block; font-style: normal; font-size: 11px; line-height: 1.45;
  color: rgba(255,255,255,0.35); margin-top: 2px;
}
/* Окно трека: сначала сведения, правка — по кнопке. Режимы переключаются
   классом, а не пересборкой разметки: поля тогда не теряют фокус и значения. */
.ti-modal { width: min(440px, 94vw); max-height: 88vh; overflow-y: auto; -webkit-overflow-scrolling: touch; }
.ti-modal:not(.editing) .ti-edit { display: none !important; }
.ti-modal.editing .ti-view { display: none !important; }

.ti-head { display: flex; gap: 12px; align-items: flex-start; margin-bottom: 14px; }
.ti-cover {
  position: relative; width: 92px; height: 92px; flex-shrink: 0;
  border-radius: 10px; overflow: hidden; background: #262628;
}
.ti-cover img { width: 100%; height: 100%; object-fit: cover; display: none; }
.ti-cover-ph {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: rgba(255,255,255,0.2); font-size: 26px;
}
.ti-cover-pick {
  position: absolute; left: 0; right: 0; bottom: 0; padding: 5px 0;
  border: none; background: rgba(0,0,0,0.65); color: #fff; font-size: 10.5px; cursor: pointer;
}
.ti-head-txt { min-width: 0; flex: 1; }
.ti-h-title { font-size: 15px; font-weight: 600; line-height: 1.25; word-break: break-word; }
.ti-h-artist { font-size: 12.5px; color: rgba(255,255,255,0.5); margin-top: 3px; word-break: break-word; }
.ti-h-file { font-size: 10.5px; color: rgba(255,255,255,0.25); margin-top: 6px; word-break: break-all; }

.ti-rows { display: flex; flex-direction: column; }
.ti-row {
  display: flex; align-items: center; gap: 10px; min-height: 30px;
  padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.04);
}
.ti-k { flex: 0 0 42%; font-size: 11.5px; color: rgba(255,255,255,0.35); }
.ti-v { flex: 1; min-width: 0; font-size: 12.5px; color: rgba(255,255,255,0.8); word-break: break-word; }
.ti-i {
  flex: 1; min-width: 0; padding: 5px 8px; border-radius: 7px;
  border: 1px solid rgba(255,255,255,0.12); background: rgba(255,255,255,0.05);
  color: #eee; font-size: 12.5px;
}
.ti-actions { display: flex; gap: 6px; margin-top: 14px; }

@media (max-width: 560px) {
  /* Подпись над значением: в две колонки на узком экране всё сжимается в кашу. */
  .ti-row { flex-direction: column; align-items: stretch; gap: 2px; padding: 7px 0; }
  .ti-k { flex: none; }
  .ti-i { font-size: 16px; }          /* мельче — iOS зумит страницу */
  .ti-cover { width: 76px; height: 76px; }
}

.perf-toggle {
  display: flex; align-items: center; gap: 8px; width: 100%; margin-bottom: 4px;
  padding: 0; border: none; background: none; cursor: pointer;
  font-size: 12px; color: rgba(255,255,255,0.4);
  transition: color 0.15s;
}
.perf-toggle:hover { color: rgba(255,255,255,0.65); }
.perf-toggle > span:first-child { flex: 1; text-align: left; }
.perf-chev { font-size: 10px; transition: transform 0.2s ease; }
.perf-toggle.open .perf-chev { transform: rotate(180deg); }
.perf-toggle.open { margin-bottom: 10px; }

/* Матовое стекло панели пересчитывается каждый раз, когда меняется картинка за
   ним, — а за ним анимированный фон. Выключение снимает эту работу целиком,
   поэтому фон панели делаем плотнее: иначе она станет просто прозрачной. */
html.perf-noblur .playlist-side {
  backdrop-filter: none; -webkit-backdrop-filter: none;
  background: rgba(14,14,16,0.94);
}
.tonearm { will-change: transform; }

html.perf-radiostatic .radio-glow.on > i,
html.perf-radiostatic .radio-halo.on { animation: none; }

.pl-cover-ph {
  width: 100%; height: 100%; background: #333; display: flex;
  align-items: center; justify-content: center;
  color: rgba(255,255,255,0.2); font-size: 20px;
}

.radio-card { cursor: pointer; }
.radio-card.on { border-color: rgba(233,69,96,0.45); background: rgba(233,69,96,0.06); }

@media (max-width: 560px) {
  /* На телефоне окно занимает почти весь экран и прокручивается: критериев
     много, и в 88vh они перестают помещаться. */
  #radioOverlay .meta-modal, #erasOverlay .meta-modal {
    width: 96vw; max-height: 92vh; padding: 16px 14px;
    overflow-y: auto; -webkit-overflow-scrolling: touch;
  }
  /* Список выбора прокручивается сам, поэтому окну прокрутка не нужна — иначе
     скроллов было бы два вложенных. */
  #eraPickOverlay .meta-modal { width: 96vw; max-height: 90vh; padding: 14px 12px; }
  #eraPickSearch { font-size: 16px; }        /* мельче — iOS зумит страницу */
  .era-pick-row { padding: 10px 8px; font-size: 13px; }
  .era-pick-no { width: 52px; }
  .era-pick-link { font-size: 11.5px; padding: 7px 9px; }
  .radio-chip { padding: 6px 11px; font-size: 12px; }   /* палец, не курсор */
  .era-row input { font-size: 16px; }                   /* iOS зумит поля мельче 16px */
}

.radio-group { margin-bottom: 13px; }
.radio-group-title {
  font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase;
  color: rgba(255,255,255,0.32); margin-bottom: 7px;
}
.radio-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.radio-chip {
  padding: 5px 10px; border: 1px solid rgba(255,255,255,0.1); border-radius: 999px;
  background: none; color: rgba(255,255,255,0.5); font-size: 11px; cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}
.radio-chip .n { opacity: 0.45; margin-left: 4px; }
.radio-chip.on { color: #e94560; border-color: rgba(233,69,96,0.5); background: rgba(233,69,96,0.1); }
.radio-count {
  margin-top: 12px; padding-top: 11px; border-top: 1px solid rgba(255,255,255,0.07);
  font-size: 12px; color: rgba(255,255,255,0.5);
}
.radio-count b { color: #eee; font-weight: 600; }
.radio-count.empty b { color: #e94560; }
.radio-years { display: flex; align-items: center; gap: 6px; }
.radio-years select {
  padding: 6px 8px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.12);
  background: rgba(255,255,255,0.05); color: #eee; font-size: 12px;
}
.radio-years { flex-wrap: wrap; }

.pl-section { display: flex; align-items: center; gap: 8px; padding: 15px 12px 7px; }
.pl-section-title {
  flex: 1; min-width: 0; font-size: 11px; font-weight: 600; letter-spacing: 0.08em;
  text-transform: uppercase; color: rgba(255,255,255,0.28);
}
.pl-section button { flex-shrink: 0; font-size: 11px; padding: 5px 11px; }
.pl-section:first-child { padding-top: 8px; }

.era-row { display: flex; gap: 6px; align-items: center; margin-bottom: 6px; }
.era-row input {
  padding: 7px 8px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.12);
  background: rgba(255,255,255,0.05); color: #eee; font-size: 13px; min-width: 0;
}
.era-row .era-sep { color: rgba(255,255,255,0.25); font-size: 12px; flex-shrink: 0; }
/* Границы периода — не просто подпись, а кнопки: искать номер трека руками в
   библиотеке невыносимо, поэтому по клику открывается список. */
.era-bound { display: flex; gap: 6px; margin: -2px 0 9px 0; }
.era-pick-link {
  flex: 1; min-width: 0; padding: 5px 8px; border-radius: 7px; cursor: pointer;
  border: 1px dashed rgba(255,255,255,0.14); background: rgba(255,255,255,0.03);
  color: rgba(255,255,255,0.42); font-size: 10.5px; text-align: left;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  transition: color 0.15s, border-color 0.15s;
}
.era-pick-link:hover { color: rgba(255,255,255,0.75); border-color: rgba(255,255,255,0.3); }
/* Открывается ИЗ окна периодов, значит обязано лежать выше него. У всех
   .meta-overlay z-index одинаковый, и порядок решала разметка: окно выбора
   стоит в ней раньше, поэтому периоды закрывали его собой и список был виден,
   но не нажимался. */
#eraPickOverlay { z-index: 120; }
.era-pick-list { flex: 1; min-height: 0; overflow-y: auto; -webkit-overflow-scrolling: touch; }
.era-pick-row {
  display: flex; gap: 9px; align-items: center; padding: 7px 8px; border-radius: 7px;
  cursor: pointer; font-size: 12px;
}
.era-pick-row:hover { background: rgba(255,255,255,0.06); }
.era-pick-no { flex-shrink: 0; width: 46px; color: rgba(255,255,255,0.3); font-size: 11px; }
.era-pick-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* Подвкладки внутри DROPS — в строке с количеством, а не отдельным рядом:
   ряд вкладок над ней уже есть, второй такой же читался бы как вложенность. */
.rel-subtab {
  flex-shrink: 0; padding: 4px 9px; border: 1px solid rgba(255,255,255,0.1);
  border-radius: 999px; background: none; color: rgba(255,255,255,0.4);
  font-size: 10px; font-weight: 600; letter-spacing: 0.06em; cursor: pointer;
  transition: color 0.2s, border-color 0.2s, background 0.2s;
}
.rel-subtab.active {
  color: #e94560; border-color: rgba(233,69,96,0.5); background: rgba(233,69,96,0.1);
}

.rel-group-title {
  font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
  color: rgba(255,255,255,0.28); padding: 16px 14px 7px;
}
.rel-group-title:first-child { padding-top: 6px; }
.rel-card {
  position: relative;
  display: flex; gap: 11px; align-items: center; padding: 9px 12px;
  margin: 0 8px 6px; border-radius: 12px;
  background: rgba(255,255,255,0.035); border: 1px solid rgba(255,255,255,0.055);
  transition: background 0.15s, border-color 0.15s;
}
.rel-card:hover { background: rgba(255,255,255,0.06); }
/* Релиз, которого нет в библиотеке — то, ради чего раздел и нужен */
.rel-card.rel-missing { border-color: rgba(233,69,96,0.35); background: rgba(233,69,96,0.05); }
.rel-card.rel-missing:hover { background: rgba(233,69,96,0.09); }
.rel-card.rel-owned { opacity: 0.5; }

.rel-art {
  width: 58px; height: 58px; flex-shrink: 0; border-radius: 8px; object-fit: cover;
  background: rgba(255,255,255,0.05); box-shadow: 0 2px 8px rgba(0,0,0,0.35);
}
.rel-art-ph {
  width: 58px; height: 58px; flex-shrink: 0; border-radius: 8px;
  background: linear-gradient(135deg, rgba(255,255,255,0.09), rgba(255,255,255,0.03));
  display: flex; align-items: center; justify-content: center;
  color: rgba(255,255,255,0.18); font-size: 22px;
}
.rel-body { flex: 1; min-width: 0; }
.rel-artist {
  font-size: 11px; color: rgba(255,255,255,0.42); margin-bottom: 2px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rel-title {
  font-size: 14px; font-weight: 600; color: #eee; line-height: 1.25;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rel-meta { display: flex; align-items: center; gap: 6px; margin-top: 5px; flex-wrap: wrap; }
.rel-badge {
  font-size: 9px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
  padding: 2px 6px; border-radius: 5px;
  background: rgba(255,255,255,0.09); color: rgba(255,255,255,0.55);
}
.rel-badge.kind-album { background: rgba(82,183,136,0.16); color: #7fd4a8; }
.rel-badge.kind-ep    { background: rgba(233,165,69,0.16); color: #e9a545; }
.rel-badge.kind-soon  { background: rgba(233,69,96,0.9);  color: #fff; }
.rel-date { font-size: 11px; color: rgba(255,255,255,0.3); }
.rel-have { font-size: 11px; color: rgba(82,183,136,0.75); }
.rel-actions { display: flex; gap: 6px; flex-shrink: 0; margin-top: 6px; }
.rel-btn {
  width: 32px; height: 32px; border-radius: 9px; border: 1px solid rgba(255,255,255,0.12);
  background: rgba(255,255,255,0.06); color: rgba(255,255,255,0.6);
  display: flex; align-items: center; justify-content: center; cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.rel-btn:hover { background: rgba(255,255,255,0.14); color: #fff; }
.rel-btn.rel-btn-get { background: #e94560; border-color: transparent; color: #fff; }
/* Звёздочка вынесена в угол карточки и не выглядит кнопкой: она вне потока,
   поэтому ряд действий из-за неё не съезжает. */
.rel-btn.rel-btn-star {
  position: absolute; top: 3px; right: 5px; z-index: 1;
  width: 22px; height: 22px; border: none; background: none;
  color: rgba(255,255,255,0.22);
}
.rel-btn.rel-btn-star:hover { background: none; color: rgba(255,255,255,0.65); }
.rel-btn.rel-btn-star.on, .rel-btn.rel-btn-star.on:hover { color: #e9a545; background: none; }
/* Закреплённые — тёплая рамка, чтобы отличались от «нет в библиотеке» */
.rel-card.rel-starred { border-color: rgba(233,165,69,0.4); }
.rel-btn.rel-btn-get:hover { background: #d13a54; }
.rel-note {
  padding: 10px 14px; font-size: 11px; color: rgba(255,255,255,0.3); line-height: 1.5;
}
.rel-top {
  display: flex; align-items: center; gap: 8px; padding: 8px 12px 2px;
}
.rel-top-note { flex: 1; min-width: 0; font-size: 11px; color: rgba(255,255,255,0.3); }
.rel-top-btn { flex-shrink: 0; font-size: 11px; padding: 6px 12px; }
/* Превью: список треков разворачивается под карточкой */
/* Раскрытая карточка и список треков должны читаться как одна рамка:
   у карточки снизу убираем отступ, скругление и границу, у списка — сверху. */
.rel-card.expanded { margin-bottom: 0; border-radius: 12px 12px 0 0; border-bottom-color: transparent; }
.rel-tracks {
  margin: 0 8px 6px; padding: 2px 0 4px; border-radius: 0 0 12px 12px;
  background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.055);
  border-top: none; overflow: hidden;
}
.rel-tracks.missing { border-color: rgba(233,69,96,0.35); background: rgba(233,69,96,0.045); }
.rel-tracks.owned { border-color: rgba(255,255,255,0.055); opacity: 0.75; }
/* Карточка избранного обведена своим цветом — раскрытый список должен читаться
   с ней как одна рамка, иначе низ у неё вдруг становился обычным. */
.rel-tracks.starred { border-color: rgba(233,165,69,0.4); background: rgba(233,165,69,0.05); }
.rel-track {
  display: flex; align-items: center; gap: 9px; padding: 7px 12px;
  cursor: pointer; position: relative; transition: background 0.12s;
}
.rel-track:hover { background: rgba(255,255,255,0.05); }
.rel-track.playing { background: rgba(233,69,96,0.1); }
.rel-track-n {
  width: 16px; flex-shrink: 0; text-align: right;
  font-size: 10px; color: rgba(255,255,255,0.25);
}
.rel-track.playing .rel-track-n { color: #e94560; }
.rel-track-name {
  flex: 1; min-width: 0; font-size: 12px; color: rgba(255,255,255,0.72);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rel-track.playing .rel-track-name { color: #fff; }
.rel-track-dur { font-size: 10px; color: rgba(255,255,255,0.22); flex-shrink: 0; }
/* Полоса под строкой — прогресс 30-секундного отрывка */
.rel-track-bar {
  position: absolute; left: 0; bottom: 0; height: 2px; width: 0;
  background: #e94560; transition: width 0.25s linear;
}
.rel-btn.rel-playing { background: #e94560; border-color: transparent; color: #fff; }
.tab-slider {
  flex: 1; overflow: hidden; position: relative;
}
.tab-slider-inner {
  height: 100%; position: relative;
}
.playlist-list, .coverflow-wrap {
  position: absolute; inset: 0; overflow-y: auto; padding: 4px 0; scroll-behavior: smooth;
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.tab-panel-hidden {
  opacity: 0; transform: translateX(20px); pointer-events: none; z-index: 0;
}
.tab-panel-visible {
  opacity: 1; transform: translateX(0); z-index: 1;
}
.playlist-item {
  display: flex; align-items: center; gap: 10px; padding: 8px 12px;
  cursor: pointer; transition: background 0.15s;
}
.playlist-item:hover { background: rgba(255,255,255,0.05); }
.playlist-item.active { background: rgba(233,69,96,0.15); transition: background 0.3s ease; }
.playlist-item .cover-thumb {
  width: 40px; height: 40px; border-radius: 4px; background: rgba(255,255,255,0.06);
  display: flex; align-items: center; justify-content: center; overflow: hidden; flex-shrink: 0;
  color: rgba(255,255,255,0.12); font-size: 16px;
}
.playlist-item .cover-thumb::after { content: '\266B'; }
.playlist-item .cover-thumb:has(img) { color: transparent; }
.playlist-item .cover-thumb:has(img)::after { display: none; }
.playlist-item .cover-thumb img { width: 100%; height: 100%; object-fit: cover; }
.playlist-item .info { flex: 1; overflow: hidden; }
.playlist-item .info .name-row { display: flex; align-items: center; gap: 6px; }
.playlist-item .info .name { font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1 1 auto; min-width: 0; }
.playlist-item .info .artist { font-size: 12px; color: rgba(255,255,255,0.4); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* Format badge — always visible; the name truncates further to make room.
   Each lossless format gets a unique colour. */
.fmt-badge {
  flex-shrink: 0;
  font-size: 9px; font-weight: 700; letter-spacing: 0.4px; line-height: 1;
  padding: 2px 4px; border-radius: 4px;
  color: #e94560; background: rgba(233,69,96,0.16);
  border: 1px solid rgba(233,69,96,0.4);
  text-transform: uppercase; white-space: nowrap;
}
.fmt-badge.fmt-flac { color: #e94560; background: rgba(233,69,96,0.16); border-color: rgba(233,69,96,0.40); }
.fmt-badge.fmt-alac { color: #4aa3ff; background: rgba(74,163,255,0.16); border-color: rgba(74,163,255,0.40); }
.fmt-badge.fmt-wav  { color: #f0a431; background: rgba(240,164,49,0.16); border-color: rgba(240,164,49,0.40); }
.fmt-badge.fmt-aiff { color: #b07cff; background: rgba(176,124,255,0.16); border-color: rgba(176,124,255,0.40); }
.track-title-row { display: flex; align-items: center; justify-content: center; gap: 8px; }
.track-title-row .track-title { min-width: 0; }
.fmt-badge-player { font-size: 11px; padding: 2px 6px; flex-shrink: 0; }
/* Multi-select: a checkable circle to the left of the cover (cover/title shift right). */
.sel-circle {
  width: 22px; height: 22px; border-radius: 50%; flex-shrink: 0; margin-right: 10px;
  border: 2px solid rgba(255,255,255,0.35); position: relative; transition: background 0.15s, border-color 0.15s;
}
.sel-circle.on { background: #e94560; border-color: #e94560; }
.sel-circle.on::after {
  content: '\2713'; position: absolute; inset: 0; display: flex; align-items: center;
  justify-content: center; color: #fff; font-size: 13px; font-weight: 700;
}
.playlist-item.selected { background: rgba(233,69,96,0.12); }
.imp-tab.active { background: #e94560 !important; color: #fff !important; border-color: #e94560 !important; }
.imp-match { display:flex; align-items:flex-start; gap:8px; padding:8px; border-bottom:1px solid rgba(255,255,255,0.04); font-size:11px; }
.imp-match .orig { color:rgba(255,255,255,0.5); flex:1; min-width:0; }
.imp-match .vk { color:#eee; flex:1; min-width:0; }
.imp-match .nomatch { color:#e94560; }

.track-edit-btn {
  width: 28px; height: 28px; flex-shrink: 0; border: none; border-radius: 50%;
  background: none; color: rgba(255,255,255,0.15); cursor: pointer;
  display: flex; align-items: center; justify-content: center; transition: color 0.15s;
}
.track-edit-btn:hover { color: rgba(255,255,255,0.5); }

/* ── Cover Flow ── */
.coverflow-wrap {
  flex: 1; overflow-y: auto; padding: 12px; scroll-behavior: smooth;
}
.album-card {
  display: flex; align-items: center; gap: 12px; padding: 10px; border-radius: 10px;
  cursor: pointer; transition: background 0.15s; margin-bottom: 0;
}
.album-card:hover { background: rgba(255,255,255,0.05); }
.album-card.active { background: rgba(233,69,96,0.12); }
.album-card.pl-drag-over { background: rgba(233,69,96,0.18); box-shadow: inset 0 0 0 1px rgba(233,69,96,0.4); }
.album-tracks {
  overflow: hidden; max-height: 0;
  transition: max-height 0.35s ease-out, opacity 0.25s ease;
  opacity: 0;
}
.album-tracks.open {
  max-height: 50000px;
  opacity: 1;
  transition: max-height 0.45s ease-in, opacity 0.3s ease 0.05s;
}
.album-cover {
  width: 56px; height: 56px; border-radius: 6px; background: rgba(255,255,255,0.06);
  overflow: hidden; flex-shrink: 0; display: flex; align-items: center; justify-content: center;
  color: rgba(255,255,255,0.12); font-size: 22px;
}
.album-cover::after { content: '\266B'; }
.album-cover:has(img) { color: transparent; }
.album-cover:has(img)::after { display: none; }
.album-cover img { width: 100%; height: 100%; object-fit: cover; }
.album-info { flex: 1; display: flex; flex-direction: column; justify-content: center; overflow: hidden; }
.album-name { font-size: 14px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.album-artist { font-size: 12px; color: rgba(255,255,255,0.4); }
.album-count { font-size: 11px; color: rgba(255,255,255,0.25); }

/* ── Folder panel ── */
.folder-panel {
  padding: 10px 12px; border-bottom: 1px solid rgba(255,255,255,0.06);
  display: flex; flex-direction: column; gap: 6px;
}
.fp-row { display: flex; gap: 6px; align-items: center; }
.folder-panel > .fp-meta-row { grid-column: 1 / -1; display: flex; gap: 6px; align-items: center; }
.folder-row { display: flex; gap: 6px; align-items: center; }
.folder-select {
  flex: 1; padding: 8px 10px; border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.12); background: rgba(255,255,255,0.06);
  color: #eee; font-size: 13px; outline: none; appearance: none;
  -webkit-appearance: none; -moz-appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='rgba(255,255,255,0.4)'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 10px center;
  padding-right: 28px; cursor: pointer;
}
.folder-select:focus { border-color: #e94560; }
.folder-select option { background: #1c1c1c; color: #eee; }
.folder-btn {
  padding: 8px 14px; border-radius: 8px; border: none; font-size: 12px;
  cursor: pointer; white-space: nowrap; transition: background 0.15s;
}
.folder-btn-primary { background: #e94560; color: #fff; }
.folder-btn-primary:hover { background: #d13a54; }
.folder-btn-secondary {
  background: rgba(255,255,255,0.07); color: rgba(255,255,255,0.7);
  border: 1px solid rgba(255,255,255,0.12);
}
.folder-btn-secondary:hover { background: rgba(255,255,255,0.14); color: #fff; }
.folder-btn-icon {
  width: 34px; height: 34px; padding: 0; display: flex; align-items: center; justify-content: center;
  font-size: 16px; border-radius: 8px; background: rgba(255,255,255,0.07);
  color: rgba(255,255,255,0.6); border: 1px solid rgba(255,255,255,0.12); cursor: pointer;
}
.folder-btn-icon:hover { background: rgba(255,255,255,0.14); color: #fff; }
.lan-info-btn {
  width: 18px; height: 18px; flex-shrink: 0; padding: 0; margin-right: 2px;
  border-radius: 50%; border: 1px solid rgba(255,255,255,0.2); background: transparent;
  color: rgba(255,255,255,0.45); font: italic 700 11px/1 Georgia, serif; cursor: pointer;
  align-items: center; justify-content: center; transition: background 0.15s, color 0.15s;
}
.lan-info-btn:hover { background: rgba(255,255,255,0.12); color: #fff; }
.lan-info-btn.open { background: rgba(233,69,96,0.9); border-color: transparent; color: #fff; }
.mobile-only { display: none; }
.force-hidden { display: none !important; }
.folder-path-input {
  flex: 1; padding: 8px 10px; border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.12); background: rgba(255,255,255,0.06);
  color: #eee; font-size: 13px; outline: none;
}
.folder-path-input:focus { border-color: #e94560; }
.folder-path-input::placeholder { color: rgba(255,255,255,0.25); }
.folder-add-row {
  display: none; flex-direction: column; gap: 6px;
  padding: 8px; background: rgba(255,255,255,0.03); border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.06);
}
.folder-add-row.show { display: flex; }

/* ── Meta modal ── */
.meta-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0);
  z-index: 100; display: flex; align-items: center; justify-content: center;
  pointer-events: none; transition: background 0.25s ease;
}
.meta-overlay .meta-modal {
  transform: scale(0.95); opacity: 0; transition: transform 0.25s ease, opacity 0.25s ease;
}
/* Профиль перерос окно: журнал и переключатели не помещаются, а .meta-modal
   ограничен 80vh без прокрутки — нижние кнопки просто срезало. */
#profileOverlay .meta-modal { overflow-y: auto; -webkit-overflow-scrolling: touch; }
.meta-overlay.show { background: rgba(0,0,0,0.7); pointer-events: auto; }
.meta-overlay.show .meta-modal { transform: scale(1); opacity: 1; }
.meta-modal {
  background: #1c1c1c; border-radius: 16px; padding: 24px; width: 500px; max-height: 80vh;
  display: flex; flex-direction: column; box-shadow: 0 20px 60px rgba(0,0,0,0.6);
  border: 1px solid rgba(255,255,255,0.06);
}
.meta-modal h3 { margin-bottom: 12px; color: #e94560; }
.meta-modal .meta-progress { font-size: 13px; color: rgba(255,255,255,0.5); margin-bottom: 8px; }
.meta-modal .meta-log {
  flex: 1; background: #111; border-radius: 8px; padding: 12px;
  font-family: 'SF Mono', Menlo, monospace; font-size: 11px; color: #aaa;
  overflow-y: auto; max-height: 50vh; white-space: pre-wrap; min-height: 100px;
  border: 1px solid rgba(255,255,255,0.06);
}
.meta-modal .meta-bar { width: 100%; height: 6px; background: #333; border-radius: 3px; margin-bottom: 8px; }
.meta-modal .meta-bar-fill { height: 100%; background: #e94560; border-radius: 3px; transition: width 0.3s; }
.meta-modal > button {
  margin-top: 12px; align-self: flex-end; padding: 8px 20px; border-radius: 8px;
  border: none; background: rgba(255,255,255,0.1); color: #eee; cursor: pointer;
}

/* ── Sidebar toggle (desktop) ── */
.sidebar-toggle {
  display: none; position: fixed; right: 360px; top: 88px;
  z-index: 20; width: 30px; height: 44px; border: none; border-radius: 8px 0 0 8px;
  background: rgba(0,0,0,0.4); backdrop-filter: blur(20px); color: rgba(255,255,255,0.3); cursor: pointer;
  align-items: center; justify-content: center;
  transition: right 0.25s ease, background 0.15s;
}
.sidebar-toggle:hover { color: rgba(255,255,255,0.6); background: rgba(0,0,0,0.5); }
.sidebar-collapsed .sidebar-toggle { right: 0; }
@media (min-width: 769px) {
  .sidebar-toggle { display: flex; }
}

/* ── Password field with eye ── */
.pw-field {
  display: flex; align-items: center; border: 1px solid rgba(255,255,255,0.12);
  border-radius: 8px; background: rgba(255,255,255,0.06); overflow: hidden;
}
.pw-field input {
  flex: 1; border: none; background: none; color: #eee; padding: 8px 10px;
  font-size: 13px; outline: none;
}
.pw-field input::placeholder { color: rgba(255,255,255,0.25); }
.pw-field:focus-within { border-color: #e94560; }
.pw-eye {
  width: 36px; flex-shrink: 0; align-self: stretch;
  background: none; border: none; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  opacity: 0.35; transition: opacity 0.15s;
}
.pw-eye:hover { opacity: 0.6; }
.pw-eye.visible { opacity: 1; }
.pw-eye img { width: 16px; height: 16px; }
.pw-eye.visible img { filter: brightness(0) saturate(100%) invert(38%) sepia(82%) saturate(2000%) hue-rotate(330deg); }

/* Admin key icon */
.admin-pw-btn { width: 26px; height: 26px; }
.admin-pw-btn img { width: 14px; height: 14px; }

/* ── Tooltips ── */
.tip-popup {
  position: fixed; padding: 5px 10px; border-radius: 6px; background: #222; color: #ccc;
  font-size: 11px; white-space: nowrap; pointer-events: none; z-index: 999;
  border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 4px 12px rgba(0,0,0,0.5);
  opacity: 0; transition: opacity 0.15s;
}
.tip-popup.show { opacity: 1; }

/* ── LAN/WAN links ── */
.net-link {
  color: #eee; text-decoration: none; font-weight: 600; font-size: 12px;
  background: rgba(255,255,255,0.06); padding: 2px 8px; border-radius: 4px;
  transition: background 0.15s; user-select: all; display: inline-block; margin: 1px 0;
}
.net-link:hover { background: rgba(255,255,255,0.14); }

/* ── Shuffle button ── */
.shuffle-btn {
  width: 36px; height: 36px; border-radius: 50%; border: none;
  background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.4);
  cursor: pointer; display: flex; align-items: center; justify-content: center;
  transition: background 0.15s, color 0.15s; font-size: 16px;
}
.shuffle-btn:hover { background: rgba(255,255,255,0.15); }
.shuffle-btn.active { color: #e94560; background: rgba(233,69,96,0.15); }
.shuffle-bar {
  display: flex; align-items: center; gap: 8px; padding: 8px 20px;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.shuffle-bar button { font-size: 12px; }

/* ── Browse ── */
.browse-item {
  display: flex; align-items: center; gap: 8px; padding: 8px 12px;
  cursor: pointer; transition: background 0.12s; font-size: 13px;
  border-bottom: 1px solid rgba(255,255,255,0.03);
}
.browse-item:hover { background: rgba(255,255,255,0.06); }
.browse-item .bi-icon { font-size: 16px; width: 20px; text-align: center; flex-shrink: 0; }
.browse-item .bi-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.browse-item.is-dir .bi-name { color: #eee; }
.browse-item.is-file .bi-name { color: rgba(255,255,255,0.35); font-size: 12px; }
.browse-info { padding: 6px 12px; font-size: 11px; color: rgba(255,255,255,0.3); border-top: 1px solid rgba(255,255,255,0.06); }

/* ── Edit mode ── */
.playlist-item.dragging { opacity: 0.4; }
.playlist-item.drag-over { border-top: 2px solid #e94560; }
.drag-handle {
  cursor: grab; color: rgba(255,255,255,0.2); font-size: 16px; padding: 0 4px;
  user-select: none; -webkit-user-select: none; flex-shrink: 0;
}
.drag-handle:active { cursor: grabbing; }

/* ── Context menu ── */
.ctx-menu {
  position: fixed; z-index: 300; min-width: 180px;
  background: rgba(30,30,30,0.96); border-radius: 12px; padding: 6px 0;
  backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
  border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  display: none;
}
.ctx-menu.show { display: block; }
.ctx-item {
  padding: 10px 16px; font-size: 13px; color: rgba(255,255,255,0.8);
  cursor: pointer; display: flex; align-items: center; gap: 10px;
}
.ctx-item:hover { background: rgba(255,255,255,0.08); }
.ctx-item:active { background: rgba(233,69,96,0.2); }
.ctx-item.danger { color: #e94560; }
.ctx-sep { height: 1px; background: rgba(255,255,255,0.08); margin: 4px 0; }
.ctx-sub { padding: 6px 0; max-height: 200px; overflow-y: auto; }
.ctx-sub .ctx-item { padding: 8px 16px; font-size: 12px; }
.ctx-sub-header { padding: 6px 16px; font-size: 11px; color: rgba(255,255,255,0.3); }

/* ── Toast ── */
.toast {
  position: fixed; top: max(20px, calc(env(safe-area-inset-top) + 10px)); left: 50%; transform: translateX(-50%) translateY(-120px);
  background: rgba(30,30,50,0.95); color: #eee; padding: 12px 24px; border-radius: 12px;
  font-size: 14px; z-index: 200; transition: transform 0.3s ease; pointer-events: none;
  backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,0.1);
  max-width: calc(100vw - 32px); text-align: center; word-wrap: break-word;
}
.toast.show { transform: translateX(-50%) translateY(0); }

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 16px; }
::-webkit-scrollbar-track { background: rgba(255,255,255,0.04); border-radius: 8px; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.22); border-radius: 8px; border: 3px solid transparent; background-clip: padding-box; min-height: 40px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.35); border: 3px solid transparent; background-clip: padding-box; }
::-webkit-scrollbar-thumb:active { background: rgba(255,255,255,0.45); }

/* ── Search field with clear button ── */
.search-wrap {
  position: relative;
}
.search-wrap input { padding-right: 30px; }
.search-clear {
  position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
  width: 22px; height: 22px; border: none; border-radius: 50%;
  background: rgba(255,255,255,0.12); color: rgba(255,255,255,0.5);
  font-size: 14px; cursor: pointer; display: none;
  align-items: center; justify-content: center; line-height: 1; padding: 0;
}
.search-clear:hover { background: rgba(255,255,255,0.25); color: #fff; }
.search-clear.show { display: flex; }

/* ── iOS safe areas ── */
html { overflow: hidden; touch-action: none; position: fixed; width: 100%; height: 100%; }
body { overflow: hidden; touch-action: none; position: fixed; width: 100%; height: 100%; }
.playlist-list, .coverflow-wrap, .meta-log, .meta-modal, #vkQueue, #vkSearchResults, #browseList, #adminUserList { touch-action: pan-y; -webkit-overflow-scrolling: touch; }
.playlist-side { padding-top: env(safe-area-inset-top); }
.vinyl-side { padding-top: env(safe-area-inset-top); padding-bottom: env(safe-area-inset-bottom); }

/* ── Mobile toggle ── */
.mobile-bar {
  display: none; position: fixed; bottom: 0; left: 0; right: 0;
  z-index: 50; pointer-events: none;
  padding: 10px 16px; padding-bottom: max(10px, env(safe-area-inset-bottom));
}
.mobile-bar-inner {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  pointer-events: auto; width: fit-content; margin: 0 auto;
}
.mobile-toggle {
  display: flex; border-radius: 22px; padding: 3px;
  background: rgba(255,255,255,0.08); backdrop-filter: blur(16px);
  flex-shrink: 0; position: relative;
}
.mobile-toggle-bg {
  position: absolute; top: 3px; left: 3px; width: 40px; height: 40px;
  border-radius: 20px; background: #e94560;
  transition: transform 0.25s cubic-bezier(0.4,0,0.2,1);
}
.mobile-toggle-bg.right { transform: translateX(40px); }
.mobile-toggle button {
  padding: 0; width: 40px; height: 40px; border: none; border-radius: 20px; font-size: 11px;
  background: none; color: rgba(255,255,255,0.4); cursor: pointer; transition: color 0.2s;
  display: flex; align-items: center; justify-content: center;
  position: relative; z-index: 1; pointer-events: none;
}
.mobile-toggle button.active { color: #fff; }
.mobile-mini-btn {
  padding: 0; width: 40px; height: 40px; border-radius: 50%; border: none;
  background: rgba(255,255,255,0.08); backdrop-filter: blur(16px);
  color: rgba(255,255,255,0.5); cursor: pointer; display: none; align-items: center; justify-content: center;
}
.mobile-mini-btn:active { background: rgba(255,255,255,0.2); }
.mobile-mini-btn.show { display: flex; }

@media (max-width: 768px) {
  .app { position: relative; }
  .vinyl-side, .playlist-side { position: absolute; inset: 0; width: 100%; height: 100vh; height: 100dvh; }
  .playlist-side { z-index: 2; }
  .vinyl-side { z-index: 1; }
  .mobile-view-vinyl .vinyl-side { display: flex; }
  .mobile-view-vinyl .playlist-side { display: none; }
  .mobile-view-playlist .vinyl-side { display: none; }
  .mobile-view-playlist .playlist-side { display: flex; flex-direction: column; }
  .mobile-bar { display: block; }
  .playlist-side { padding-bottom: 0; }
  .vinyl-side { padding-bottom: 70px; }
  .vinyl-scene { width: min(80vw, 50vh); height: min(80vw, 50vh); }
  .track-title { max-width: 80vw; }
  .ipod-body { width: min(72vw, 50vh); min-width: 200px; }
  .cassette-body { --cw: min(90vw, 50vh); min-width: 260px; }
  .player-mode-toggle { position: fixed; top: auto; bottom: 0; left: 16px; right: auto; z-index: 51; margin-bottom: 10px; margin-bottom: max(10px, env(safe-area-inset-bottom)); transition: all 0.25s ease; }
  .player-mode-toggle.collapsed .player-mode-btn:not(.active) { width: 0; padding: 0; opacity: 0; overflow: hidden; pointer-events: none; }
  .player-mode-toggle.collapsed .player-mode-btn.active { color: #e94560; }
  .player-mode-toggle.collapsed { gap: 0; padding: 3px; }
  /* Phone layout: catalog management, user admin and Meta live on the desktop
     only, and «Загрузить» becomes an icon in the first row — that frees the
     whole second row, leaving catalog + Профиль + Загрузить above the search. */
  .mobile-only { display: flex; }
  #addFolderBtn, #removeFolderBtn, #adminBtn, #metaBtn, #vkBtn { display: none !important; }
  /* The row only survives while it still carries the LAN/WAN toggles (which a
     phone never shows — they need the server machine — but a narrow desktop
     window does). */
  #metaVkRow:not(.has-toggles) { display: none; }
}
/* Force portrait on narrow screens */
@media (max-width: 768px) and (orientation: landscape) {
  body::before {
    content: 'Поверните устройство в портретный режим';
    position: fixed; inset: 0; z-index: 9999;
    background: #111; color: rgba(255,255,255,0.5);
    display: flex; align-items: center; justify-content: center;
    font-size: 18px; text-align: center; padding: 40px;
  }
}
.playlist-header span { cursor: pointer; }
/* Трек, выбранный «играть следующим» */
.playlist-item.queued-next { box-shadow: inset 2px 0 0 #e9a545; }
.next-badge {
  flex-shrink: 0; margin-left: 6px; padding: 1px 6px; border-radius: 5px;
  background: rgba(233,165,69,0.16); color: #e9a545;
  font-size: 9px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase;
}
/* Рамка радиостанции: кольцо из вращающегося конического градиента.
   Маской вырезаем середину — остаётся только полоса по краю. Появление и
   исчезновение — через opacity на родителе, чтобы плавность не конфликтовала с
   бесконечной анимацией дыхания на внутреннем слое. */
.radio-glow, .radio-halo {
  /* 6px, а не больше: переключатель вида плеера стоит на top/right 12px, и
     рамка с отступом 16px проходила прямо по нему.
     Безопасные зоны учитываем ЗДЕСЬ, а не полагаемся на padding родителя:
     абсолютное позиционирование считается от padding-box, то есть padding
     .vinyl-side рамку не сдвигает — сверху она уходила под строку состояния и
     выглядела обрезанной. Снизу отступ минимальный: линия должна пройти под
     кнопками, а не поверх них. */
  position: absolute; left: 6px; right: 6px;
  top: calc(6px + env(safe-area-inset-top, 0px));
  /* Половина безопасной зоны: линия должна пройти ПОД кнопками, но не залезть
     на индикатор home. Полная зона поднимала её слишком высоко. */
  bottom: calc(2px + env(safe-area-inset-bottom, 0px) / 2);
  border-radius: 18px; z-index: 8;
  pointer-events: none; opacity: 0;
  transition: opacity 1.2s cubic-bezier(0.4, 0, 0.2, 1);
}
.radio-glow { padding: 2px; overflow: hidden;
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite: xor;
  mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  mask-composite: exclude;
}
.radio-glow > i {
  /* Квадрат со стороной заведомо больше диагонали экрана: при вращении слой
     обязан накрывать рамку в любом положении.
     Проценты здесь не годятся — они считаются от сторон родителя по
     отдельности. 150% на узком телефоне давало слой 585×1200 при экране
     390×800, и при повороте на 90° ширина слоя (585) переставала доставать до
     высоты рамки (800): кольцо разрывалось сверху и снизу, а на части оборота
     пропадало целиком. vmax берёт большую сторону экрана, 150vmax перекрывает
     диагональ (√2 ≈ 1.42) при любом соотношении сторон.
     will-change / translateZ здесь НЕ ставим: композитный слой под маской на
     iOS отрисовывался частично. */
  position: absolute; left: 50%; top: 50%;
  width: 150vmax; height: 150vmax;
  margin: -75vmax 0 0 -75vmax; display: block;
  background: conic-gradient(from 0deg,
    var(--rg1, #e94560), var(--rg2, #7a5cff), var(--rg3, #35d0c0),
    var(--rg2, #7a5cff), var(--rg1, #e94560));
}
/* Мягкое свечение внутрь — отдельным слоем: box-shadow на кольце срезался бы маской. */
.radio-halo { box-shadow: inset 0 0 70px -26px var(--rg2, #7a5cff); }

/* Слегка размытая копия кольца под чёткой: линия в 2px сама по себе читалась
   слишком технично. Размытие намеренно небольшое — у панели со списком стоит
   backdrop-filter: blur(20px), она подмешивает то, что лежит вплотную к её
   краю, и сильное свечение проступало сквозь неё на список треков. */
.radio-bloom { padding: 3px; filter: blur(4px); }
.radio-bloom > i { animation-duration: 11s, 5s; }

.radio-glow.on, .radio-halo.on { opacity: 1; }
.radio-bloom.on { opacity: 0.7; }
/* Вращение и дыхание запускаем только когда рамка видна: незачем крутить
   анимацию под скрытым элементом. */
.radio-glow.on > i { animation: radioSpin 11s linear infinite, radioBreath 5s ease-in-out infinite; }
.radio-halo.on { animation: radioBreath 5s ease-in-out infinite; }
@keyframes radioSpin { to { transform: rotate(360deg); } }
@keyframes radioBreath { 0%, 100% { opacity: 0.45; } 50% { opacity: 1; } }

@media (prefers-reduced-motion: reduce) {
  .radio-glow.on > i, .radio-halo.on { animation: none; }
}

/* Окно свёрнуто или ушло в фон — вращение и дыхание рамки останавливаются.
   Именно останавливаются, а не прячутся: animation-play-state снимает работу с
   композитора, но кадр остаётся на экране, поэтому при возврате картинка не
   прыгает. */
html.ui-idle .radio-glow.on > i,
html.ui-idle .radio-halo.on { animation-play-state: paused; }

/* На телефоне экран скруглён сильнее — увеличиваем радиус, чтобы линия шла
   вдоль угла, а не срезалась им. Отступы при этом те же: их задаёт безопасная
   зона, и добавлять сверху ещё константу значит поднять рамку над кнопками. */
@media (max-width: 900px) {
  .radio-glow, .radio-halo { border-radius: 26px; }
}

.loading-spinner { width:28px;height:28px;border:3px solid rgba(255,255,255,0.1);border-top-color:#e94560;border-radius:50%;animation:lspin .7s linear infinite; }
@keyframes lspin { to { transform:rotate(360deg); } }
</style>
</head>
<body>
<div class="bg-canvas" id="bgCanvas"><canvas id="bgC"></canvas></div>
<div class="app">
  <!-- Left: Vinyl -->
  <div class="vinyl-side">
    <!-- Рамка радиостанции. Лежит внутри .vinyl-side, а та кончается там, где
         начинается столбец со списком (right: 360px), поэтому рамка не может на
         него заехать — и сама едет при сворачивании панели. -->
    <div class="radio-glow radio-bloom" id="radioBloom"><i></i></div>
    <div class="radio-glow" id="radioGlow"><i></i></div>
    <div class="radio-halo" id="radioHalo"></div>
    <button onclick="showAppInfo()" style="position:absolute;top:16px;left:16px;z-index:5;width:36px;height:36px;border:none;border-radius:50%;background:rgba(255,255,255,0.06);color:rgba(255,255,255,0.2);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:18px;font-style:italic;font-weight:700;transition:color 0.15s" onmouseover="this.style.color='rgba(255,255,255,0.5)'" onmouseout="this.style.color='rgba(255,255,255,0.2)'">i</button>
    <div class="player-mode-toggle">
      <button class="player-mode-btn active" id="modeVinyl" onclick="setPlayerMode('vinyl')" data-tip="Пластинка"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="12" cy="12" r="3" fill="currentColor"/></svg></button>
      <button class="player-mode-btn" id="modeCassette" onclick="setPlayerMode('cassette')" data-tip="Кассета"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="2" y="5" width="20" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="8.5" cy="13" r="2.5" fill="none" stroke="currentColor" stroke-width="1"/><circle cx="15.5" cy="13" r="2.5" fill="none" stroke="currentColor" stroke-width="1"/><line x1="11" y1="13" x2="13" y2="13" stroke="currentColor" stroke-width="1"/><rect x="6" y="6.5" width="12" height="4" rx="1" fill="none" stroke="currentColor" stroke-width="0.8"/></svg></button>
      <button class="player-mode-btn" id="modeIpod" onclick="setPlayerMode('ipod')" data-tip="iPod"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="5" y="1" width="14" height="22" rx="3"/><rect x="7" y="3" width="10" height="8" rx="1"/><circle cx="12" cy="17" r="3.5"/><circle cx="12" cy="17" r="1.5"/></svg></button>
    </div>
    <div class="cassette-scene">
      <div class="cassette-body">
        <div class="cassette-screw cs-tl"></div>
        <div class="cassette-screw cs-tr"></div>
        <div class="cassette-screw cs-bl"></div>
        <div class="cassette-screw cs-br"></div>
        <div class="cassette-dark-panel"></div>
        <div class="cassette-stripes"></div>
        <div class="cassette-label">
          <div class="cassette-label-side">A</div>
          <div class="cassette-label-brand">insideside</div>
          <div class="cassette-label-content">
            <div class="cassette-cover-wrap">
              <img id="cassetteCover" class="cassette-cover" style="display:none">
              <div class="cassette-cover-placeholder" id="cassetteCoverPh">&#9835;</div>
            </div>
            <div class="cassette-label-text">
              <div class="cassette-label-title" id="cassetteTitle"></div>
              <div class="cassette-label-artist" id="cassetteArtist" onclick="searchArtist(this.textContent)"></div>
            </div>
          </div>
        </div>
        <div class="cassette-window">
          <div class="cassette-tape-spool cassette-tape-spool-l" id="cassetteSpoolL"></div>
          <div class="cassette-tape-spool cassette-tape-spool-r" id="cassetteSpoolR"></div>
          <div class="cassette-reel cassette-reel-l" id="cassetteReelL"><div class="cassette-reel-spokes" id="cassetteHubL"><svg class="cassette-hub-svg" viewBox="0 0 40 40"><circle cx="20" cy="20" r="18" fill="#111" stroke="#222" stroke-width="0.5"/><path d="M20,5 L23,15 L33,11 L27,20 L33,29 L23,25 L20,35 L17,25 L7,29 L13,20 L7,11 L17,15 Z" fill="#1a1a1a" stroke="#333" stroke-width="0.3"/><circle cx="20" cy="20" r="3" fill="#0a0a0a" stroke="#333" stroke-width="0.3"/></svg></div></div>
          <div class="cassette-reel cassette-reel-r" id="cassetteReelR"><div class="cassette-reel-spokes" id="cassetteHubR"><svg class="cassette-hub-svg" viewBox="0 0 40 40"><circle cx="20" cy="20" r="18" fill="#111" stroke="#222" stroke-width="0.5"/><path d="M20,5 L23,15 L33,11 L27,20 L33,29 L23,25 L20,35 L17,25 L7,29 L13,20 L7,11 L17,15 Z" fill="#1a1a1a" stroke="#333" stroke-width="0.3"/><circle cx="20" cy="20" r="3" fill="#0a0a0a" stroke="#333" stroke-width="0.3"/></svg></div></div>
          <div class="cassette-tape-path"></div>
        </div>
        <div class="cassette-bottom">
          <div class="cassette-bottom-holes"><div class="cassette-bh-sm"></div><div class="cassette-bh-md"></div><div class="cassette-bh-lg"></div><div class="cassette-bh-md"></div><div class="cassette-bh-sm"></div></div>
        </div>
      </div>
    </div>
    <div class="ipod-scene">
      <div class="ipod-body" id="ipodBody">
        <div class="ipod-screen">
          <div class="ipod-np-wrap" id="ipodNpWrap">
            <div class="ipod-np">
              <div class="ipod-np-header">Now Playing</div>
              <div class="ipod-np-body">
                <div class="ipod-np-cover" id="ipodCoverWrap"><span class="ipod-np-cover-ph" id="ipodCoverPh">&#9835;</span><img id="ipodCover" style="display:none"></div>
                <div class="ipod-np-info">
                  <div class="ipod-np-title" id="ipodTitle"></div>
                  <div class="ipod-np-artist" id="ipodArtist" onclick="searchArtist(this.textContent)"></div>
                  <div class="ipod-np-album" id="ipodAlbum"></div>
                </div>
              </div>
              <div class="ipod-np-progress">
                <div class="ipod-np-bar"><div class="ipod-np-bar-fill" id="ipodProgress"></div></div>
                <div class="ipod-np-time"><span id="ipodTimeCur">0:00</span><span id="ipodTimeDur">0:00</span></div>
              </div>
            </div>
          </div>
          <div class="ipod-list" id="ipodList">
            <div class="ipod-list-header">Tracks</div>
            <div class="ipod-list-items" id="ipodListItems"></div>
          </div>
        </div>
        <div class="ipod-wheel" id="ipodWheel">
          <span class="ipod-wheel-label ipod-wl-menu">MENU</span>
          <span class="ipod-wheel-label ipod-wl-fwd">&#9654;&#9654;&#124;</span>
          <span class="ipod-wheel-label ipod-wl-back">&#124;&#9664;&#9664;</span>
          <span class="ipod-wheel-label ipod-wl-play">&#9654;&#10073;&#10073;</span>
          <div class="ipod-wheel-center" id="ipodCenter"></div>
        </div>
      </div>
    </div>
    <div class="vinyl-scene">
      <div class="tonearm-pivot">
        <div class="tonearm-base"></div>
        <div class="tonearm" id="tonearm">
          <div class="tonearm-counterweight"></div>
          <div class="tonearm-arm"></div>
          <div class="tonearm-head"></div>
        </div>
      </div>
      <div class="vinyl-record" id="vinylRecord">
        <div class="vinyl-grooves"></div>
        <div class="vinyl-label">
          <img id="vinylCover" class="vinyl-cover-img" style="display:none">
          <div id="vinylPlaceholder" class="vinyl-cover-placeholder">&#9835;</div>
        </div>
        <div class="vinyl-hole"></div>
      </div>
    </div>

    <div class="track-info">
      <div class="track-title-row">
        <div class="track-title" id="trackTitle" style="opacity:0.3" data-idle="1"></div>
        <span class="fmt-badge fmt-badge-player" id="trackTitleBadge" style="display:none"></span>
      </div>
      <div class="track-artist"><span class="artist-link" id="trackArtist" onclick="searchArtist(this.textContent)" style="opacity:0.3">Выберите трек</span></div>
    </div>

    <div class="controls">
      <button class="ctrl-btn" onclick="prevTrack()"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zm12 0v12l-8.5-6z"/></svg></button>
      <button class="ctrl-btn play-btn" id="playBtn" onclick="togglePlay()"><svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor" id="playIcon"><path d="M8 5v14l11-7z"/></svg></button>
      <button class="ctrl-btn" onclick="nextTrack()"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg></button>
      <button class="ctrl-btn" id="radioBtn" onclick="openRadioModal()" data-tip="Критерии радиостанции" style="display:none;color:#e94560"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a1 1 0 0 1 .45 1.9L8.2 6h11.3A2.5 2.5 0 0 1 22 8.5v10A2.5 2.5 0 0 1 19.5 21h-15A2.5 2.5 0 0 1 2 18.5v-10a2.5 2.5 0 0 1 1.6-2.33l8-3.98A1 1 0 0 1 12 2zm5 7a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zm0 2a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3zM5 10h6v2H5v-2zm0 4h6v2H5v-2z"/></svg></button>
    </div>

    <div class="progress-wrap" id="progressWrap">
      <div class="progress-bg">
        <div class="progress-fill" id="progressFill"></div>
      </div>
      <div class="time-display">
        <span id="timeCurrent">0:00</span>
        <span id="timeDuration">0:00</span>
      </div>
    </div>

    <div class="volume-wrap">
      <span style="font-size:14px;opacity:0.5">&#128264;</span>
      <input type="range" min="0" max="1" step="0.01" value="0.8" oninput="setVolume(this.value)">
      <button class="shuffle-btn" id="shufflePlayerBtn" onclick="toggleShuffle()" style="width:32px;height:32px" data-tip="Перемешать"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/></svg></button>
    </div>
  </div>

  <button class="sidebar-toggle" id="sidebarToggle" onclick="toggleSidebar()">
    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" id="sidebarIcon"><path d="M8.59 16.59L10 18l6-6-6-6-1.41 1.41L13.17 12z"/></svg>
  </button>

  <!-- Right: Playlist -->
  <div class="playlist-side">
    <!-- Folder panel -->
    <div class="folder-panel">
      <div class="fp-row">
        <select id="folderSelect" class="folder-select" style="flex:1" onchange="onFolderSelect(this.value)">
          <option value="">Выберите каталог</option>
        </select>
        <button class="folder-btn-icon" id="addFolderBtn" onclick="toggleAddFolder()" data-tip="Добавить каталог">+</button>
        <button class="folder-btn-icon" id="removeFolderBtn" onclick="removeCurrentFolder()" data-tip="Удалить каталог">&times;</button>
        <button class="folder-btn-icon" onclick="openProfile()" data-tip="Профиль" id="profileBtn"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12zm0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8z"/></svg></button>
        <button class="folder-btn-icon mobile-only" onclick="openVkModal()" data-tip="Загрузить" id="vkBtnIcon"><svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M19.35 10.04A7.49 7.49 0 0 0 12 4C9.11 4 6.6 5.64 5.35 8.04A5.994 5.994 0 0 0 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM17 13l-5 5-5-5h3V9h4v4h3z"/></svg></button>
        <button class="folder-btn-icon" onclick="openAdmin()" data-tip="Пользователи" id="adminBtn" style="display:none"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg></button>
      </div>

      <div class="folder-add-row" id="addFolderRow">
        <div class="fp-row">
          <input type="text" id="newFolderPath" class="folder-path-input" style="flex:1" placeholder="/путь/к/музыке..." onkeydown="if(event.key==='Enter')addFolderFromInput()">
          <button class="folder-btn folder-btn-secondary" onclick="openBrowse()" data-tip="Обзор">&#128193;</button>
          <button class="folder-btn folder-btn-primary" onclick="addFolderFromInput()">Добавить</button>
        </div>
      </div>

      <div class="fp-row" id="metaVkRow">
        <button class="folder-btn folder-btn-secondary" id="metaBtn" style="flex:1" onclick="startMetaSearch()" data-tip="Поиск обложек, артистов и альбомов">Meta</button>
        <button class="folder-btn folder-btn-secondary" id="vkBtn" style="flex:1" onclick="openVkModal()" data-tip="Импорт из VK, Яндекс, Spotify, Apple Music, SoundCloud">Загрузить</button>
        <div id="networkToggles" style="display:none;align-items:center;gap:4px;flex-shrink:0">
          <button id="lanInfoBtn" class="lan-info-btn" onclick="toggleLanInfo()" data-tip="Адреса подключения" style="display:none">i</button>
          <span style="font-size:10px;color:rgba(255,255,255,0.35)">LAN</span>
          <label style="position:relative;width:30px;height:16px;cursor:pointer;flex-shrink:0">
            <input type="checkbox" id="publicToggle" onchange="togglePublic(this.checked)" style="opacity:0;width:0;height:0">
            <span style="position:absolute;inset:0;background:rgba(255,255,255,0.15);border-radius:8px;transition:.3s"></span>
            <span id="publicDot" style="position:absolute;top:2px;left:2px;width:12px;height:12px;background:#888;border-radius:50%;transition:.3s"></span>
          </label>
          <span style="font-size:10px;color:rgba(255,255,255,0.35)">WAN</span>
          <label style="position:relative;width:30px;height:16px;cursor:pointer;flex-shrink:0">
            <input type="checkbox" id="wanToggle" onchange="toggleWan(this.checked)" style="opacity:0;width:0;height:0">
            <span style="position:absolute;inset:0;background:rgba(255,255,255,0.15);border-radius:8px;transition:.3s"></span>
            <span id="wanDot" style="position:absolute;top:2px;left:2px;width:12px;height:12px;background:#888;border-radius:50%;transition:.3s"></span>
          </label>
        </div>
      </div>

      <div id="lanInfo" style="font-size:11px;color:rgba(255,255,255,0.4);display:none;line-height:1.6"></div>

      <div class="search-wrap" style="position:relative">
        <input type="text" id="searchInput" class="folder-path-input" style="width:100%" placeholder="Поиск по трекам..." oninput="onSearchInput(this.value)">
        <button class="search-clear" id="searchClear" onclick="clearSearch()">&times;</button>
      </div>
    </div>

    <div class="playlist-tabs">
      <button class="playlist-tab active" id="tabTracks" onclick="showTab('tracks')">Треки</button>
      <button class="playlist-tab" id="tabAlbums" onclick="showTab('albums')">Альбомы</button>
      <button class="playlist-tab" id="tabPlaylists" onclick="showTab('playlists')">Плейлисты</button>
      <button class="playlist-tab" id="tabNew" onclick="showTab('new')">DROPS</button>
    </div>

    <div class="playlist-header" style="display:flex;align-items:center;gap:8px">
      <button class="shuffle-btn" id="downloadCatalogBtn" onclick="downloadCatalog()" data-tip="Скачать ZIP-архив" style="display:none"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M20 6h-8l-2-2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm-2 10h-3v3h-2v-3H9l5-5 5 5z"/></svg></button>
      <button class="shuffle-btn" id="cacheBtn" onclick="startCacheAll()" data-tip="Кэшировать для офлайн"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg></button>
      <button class="shuffle-btn" id="cachedOnlyBtn" onclick="toggleCachedOnly()" data-tip="Только кэш"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg></button>
      <span id="playlistHeader" style="flex:1;cursor:pointer" onclick="scrollTracklistTop()">0 треков</span>
      <div id="relSubTabs" style="display:none;gap:4px">
        <button class="rel-subtab active" id="relTabNew" onclick="showRelTab('new')">NEW</button>
        <button class="rel-subtab" id="relTabFy" onclick="showRelTab('foryou')">4YOU</button>
      </div>
      <button class="shuffle-btn" id="shuffleListBtn" onclick="toggleShuffleFromList()" data-tip="Перемешать"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/></svg></button>
      <button class="shuffle-btn" id="editBtn" onclick="startEdit()" data-tip="Редактировать порядок" style="display:none"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 000-1.41l-2.34-2.34a1 1 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg></button>
      <div id="editControls" style="display:none;gap:4px">
        <button class="shuffle-btn" onclick="saveEdit()" data-tip="Сохранить" style="color:#52b788"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg></button>
        <button class="shuffle-btn" onclick="cancelEdit()" data-tip="Отмена" style="color:#e94560"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg></button>
      </div>
      <div id="selectControls" style="display:none;gap:4px;align-items:center">
        <button class="shuffle-btn" onclick="openBulkMenu(event)" data-tip="Действия с выбранными"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z"/></svg></button>
        <button class="shuffle-btn" onclick="exitSelection()" data-tip="Отменить выбор" style="color:#e94560"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg></button>
      </div>
    </div>

    <div class="tab-slider">
      <div class="tab-slider-inner" id="tabSlider">
        <div class="playlist-list tab-panel-visible" id="trackList"></div>
        <div class="coverflow-wrap tab-panel-hidden" id="albumList"></div>
        <div class="coverflow-wrap tab-panel-hidden" id="playlistsList"></div>
        <div class="coverflow-wrap tab-panel-hidden" id="newList"></div>
      </div>
    </div>
  </div>
</div>

<!-- Meta confirm -->
<div class="meta-overlay" id="metaConfirmOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)metaConfirmClose()">
  <div class="meta-modal" style="width:400px">
    <h3>Meta-данные</h3>
    <p style="font-size:13px;color:rgba(255,255,255,0.6);margin:12px 0">Начать поиск Meta-данных для всех треков в каталоге?</p>
    <label style="display:flex;align-items:center;gap:8px;font-size:13px;color:rgba(255,255,255,0.5);cursor:pointer;margin:12px 0;padding:10px;background:rgba(255,255,255,0.04);border-radius:8px">
      <input type="checkbox" id="autoMetaCheck" style="accent-color:#e94560;width:16px;height:16px">
      <span>Автоматически искать Meta-данные при воспроизведении трека, если их нет</span>
    </label>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button class="folder-btn folder-btn-primary" onclick="metaConfirmGo()">Сканировать</button>
      <button class="folder-btn folder-btn-secondary" onclick="metaConfirmClose()">Закрыть</button>
    </div>
  </div>
</div>

<!-- Meta search modal -->
<div class="meta-overlay" id="metaOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)closeMetaModal()">
  <div class="meta-modal" style="width:min(550px,94vw);max-height:90vh;overflow-y:auto">
    <h3>Поиск метаданных</h3>
    <div style="font-size:11px;color:rgba(255,255,255,0.3);margin-bottom:8px">Deezer + iTunes + Genius + Last.fm + MusicBrainz</div>
    <div class="meta-progress" id="metaProgress"></div>
    <div class="meta-bar"><div class="meta-bar-fill" id="metaBarFill" style="width:0%"></div></div>
    <div class="meta-log" id="metaLog" style="max-height:150px;overflow-y:auto"></div>
    <!-- Proposals review -->
    <div id="metaProposals" style="display:none;margin-top:10px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span style="font-size:13px;font-weight:600;flex:1">Предложения</span>
        <button class="folder-btn folder-btn-secondary" style="padding:4px 10px;font-size:11px" onclick="metaToggleAll(true)">Все</button>
        <button class="folder-btn folder-btn-secondary" style="padding:4px 10px;font-size:11px" onclick="metaToggleAll(false)">Ни одного</button>
      </div>
      <div id="metaProposalList" style="max-height:40vh;overflow-y:auto;border:1px solid rgba(255,255,255,0.06);border-radius:8px"></div>
      <button class="folder-btn folder-btn-primary" style="width:100%;margin-top:8px" onclick="applyMetaProposals()">Применить выбранные</button>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
      <button class="folder-btn folder-btn-primary" onclick="cancelMeta()" id="metaCancelBtn">Отменить</button>
      <button class="folder-btn folder-btn-secondary" onclick="closeMetaModal()">Закрыть</button>
    </div>
  </div>
</div>

<!-- Import help modal -->
<div class="meta-overlay" id="importHelpOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')" style="z-index:110">
  <div class="meta-modal" style="width:min(500px,92vw);max-height:85vh;overflow-y:auto">
    <h3>Как работает загрузка</h3>
    <div style="font-size:12px;color:rgba(255,255,255,0.6);line-height:1.6">
      <p style="margin-bottom:10px"><b style="color:#e94560">Авторизация VK</b> — необходима для любого способа загрузки. Треки скачиваются из VK Music. Нажмите «Войти», авторизуйтесь в браузере, скопируйте URL и вставьте в поле.</p>

      <p style="margin-bottom:6px"><b style="color:#eee">Источники:</b></p>
      <ul style="margin:0 0 10px 16px;color:rgba(255,255,255,0.5)">
        <li><b>VK</b> — ссылки на плейлисты VK Music, прямая загрузка</li>
        <li><b>Яндекс / Spotify / Apple / SoundCloud</b> — вставьте ссылку на публичный плейлист. Система получит список треков и найдёт их в VK</li>
        <li><b>Поиск</b> — ручной поиск трека по названию в VK</li>
      </ul>

      <p style="margin-bottom:6px"><b style="color:#eee">Сопоставление треков:</b></p>
      <p style="margin-bottom:10px;color:rgba(255,255,255,0.5)">Для внешних площадок система автоматически ищет каждый трек в VK. Вы увидите таблицу: оригинал → найденное в VK. Можно снять галку с неверных совпадений или нажать «найти другую версию» для повторного поиска.</p>

      <p style="margin-bottom:6px"><b style="color:#eee">Настройки размещения:</b></p>
      <ul style="margin:0 0 10px 16px;color:rgba(255,255,255,0.5)">
        <li><b>В начало</b> — новые треки получат номера 1, 2, 3..., существующие сдвинутся</li>
        <li><b>В конец</b> — новые треки добавятся после последнего трека в каталоге</li>
        <li><b>Как в плейлисте / Обратный</b> — порядок загрузки из VK плейлиста</li>
      </ul>

      <p style="margin-bottom:6px"><b style="color:#eee">Очередь и порядок:</b></p>
      <p style="margin-bottom:10px;color:rgba(255,255,255,0.5)">В поиске и при импорте можно собрать очередь из нескольких треков, перетаскивая их для изменения порядка. Треки загрузятся именно в этом порядке.</p>

      <p style="margin-bottom:6px"><b style="color:#eee">Meta-данные:</b></p>
      <p style="margin-bottom:10px;color:rgba(255,255,255,0.5)">Флажок «Meta» запустит поиск обложек и информации об альбоме после загрузки (Deezer, iTunes, Genius, Last.fm, MusicBrainz).</p>

      <p style="margin-bottom:6px"><b style="color:#e9a545">Ограничения VK:</b></p>
      <p style="color:rgba(255,255,255,0.5)">При частых запросах VK может включить captcha. В этом случае загрузка остановится, уже найденные треки можно скачать сразу. Кнопка «Повторить поиск» станет доступна через 15 минут для ненайденных треков.</p>
    </div>
    <button class="folder-btn folder-btn-secondary" style="width:100%;margin-top:12px" onclick="document.getElementById('importHelpOverlay').classList.remove('show')">Понятно</button>
  </div>
</div>

<!-- Import modal -->
<div class="meta-overlay" id="vkOverlay">
  <div class="meta-modal" style="width:min(560px,94vw);max-height:90vh;display:flex;flex-direction:column;overflow:hidden">
    <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
      <h3 style="flex:1">Загрузка треков</h3>
      <button onclick="showImportHelp()" style="width:24px;height:24px;border:none;border-radius:50%;background:rgba(255,255,255,0.08);color:rgba(255,255,255,0.3);cursor:pointer;font-size:13px;font-weight:700;flex-shrink:0;display:flex;align-items:center;justify-content:center">?</button>
    </div>
    <div id="vkAuthSection" style="flex-shrink:0">
      <div id="vkAuthStatus" style="font-size:12px;color:rgba(255,255,255,0.4);margin-bottom:6px"></div>
      <div id="vkAuthForm" style="display:none;margin-bottom:8px">
        <div style="font-size:11px;color:rgba(255,255,255,0.4);margin-bottom:4px">Вставьте URL после авторизации VK:</div>
        <div style="display:flex;gap:6px">
          <input type="text" id="vkTokenInput" class="folder-path-input" style="flex:1;font-size:11px" placeholder="https://oauth.vk.com/blank.html#access_token=...">
          <button class="folder-btn folder-btn-primary" style="padding:6px 12px;font-size:11px" onclick="submitVkToken()">OK</button>
        </div>
      </div>
    </div>
    <!-- Source tabs -->
    <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px;flex-shrink:0">
      <button class="folder-btn folder-btn-secondary imp-tab active" onclick="showImpTab('vk')" id="impTabVk" style="flex:1;padding:6px 4px;font-size:11px;min-width:60px">VK</button>
      <button class="folder-btn folder-btn-secondary imp-tab" onclick="showImpTab('yandex')" id="impTabYandex" style="flex:1;padding:6px 4px;font-size:11px;min-width:60px">Яндекс</button>
      <button class="folder-btn folder-btn-secondary imp-tab" onclick="showImpTab('spotify')" id="impTabSpotify" style="flex:1;padding:6px 4px;font-size:11px;min-width:60px">Spotify</button>
      <button class="folder-btn folder-btn-secondary imp-tab" onclick="showImpTab('apple')" id="impTabApple" style="flex:1;padding:6px 4px;font-size:11px;min-width:60px">Apple</button>
      <button class="folder-btn folder-btn-secondary imp-tab" onclick="showImpTab('soundcloud')" id="impTabSoundcloud" style="flex:1;padding:6px 4px;font-size:11px;min-width:60px">SoundCloud</button>
      <button class="folder-btn folder-btn-secondary imp-tab" onclick="showImpTab('search')" id="impTabSearch" style="flex:1;padding:6px 4px;font-size:11px;min-width:60px">Поиск</button>
    </div>
    <div style="flex:1;overflow-y:auto;min-height:0">
    <!-- VK Playlists -->
    <div id="impVk">
      <div id="vkFolderHint" style="font-size:11px;color:rgba(255,255,255,0.3);margin-bottom:6px"></div>
      <textarea id="vkUrls" style="width:100%;height:60px;padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:rgba(255,255,255,0.06);color:#eee;font-size:11px;resize:vertical;outline:none;font-family:inherit" placeholder="Ссылки на VK плейлисты (по одной на строку)"></textarea>
      <div style="display:flex;gap:6px;margin:6px 0;font-size:11px">
        <select id="vkMode" class="folder-select" style="flex:1;padding:6px 24px 6px 8px;font-size:11px"><option value="prepend">В начало</option><option value="append">В конец</option></select>
        <select id="vkOrder" class="folder-select" style="flex:1;padding:6px 24px 6px 8px;font-size:11px"><option value="normal">Как в плейлисте</option><option value="reverse">Обратный</option></select>
      </div>
      <label style="display:flex;align-items:center;gap:5px;color:rgba(255,255,255,0.4);cursor:pointer;font-size:11px;margin-bottom:6px"><input type="checkbox" id="vkRunMeta" style="accent-color:#e94560"> Meta-данные после загрузки</label>
      <button class="folder-btn folder-btn-primary" style="width:100%;font-size:12px" onclick="startVkDownload()">Загрузить VK плейлисты</button>
      <!-- Local file import (localhost only) -->
      <div id="localImportBlock" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.08)">
        <div style="font-size:11px;color:rgba(255,255,255,0.4);margin-bottom:6px">Добавить треки с этого компьютера:</div>
        <div style="display:flex;gap:6px;margin-bottom:6px;font-size:11px">
          <select id="localMode" class="folder-select" style="flex:1;padding:6px 24px 6px 8px;font-size:11px" onchange="updateLocalPosVis()"><option value="prepend">В начало</option><option value="append">В конец</option><option value="position">На позицию №</option></select>
          <input type="number" id="localPos" min="1" value="1" style="display:none;width:70px;padding:6px 8px;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:rgba(255,255,255,0.06);color:#eee;font-size:11px;outline:none" placeholder="№">
        </div>
        <label style="display:flex;align-items:center;gap:5px;color:rgba(255,255,255,0.4);cursor:pointer;font-size:11px;margin-bottom:6px"><input type="checkbox" id="localRunMeta" style="accent-color:#e94560"> Meta-данные после импорта</label>
        <button class="folder-btn folder-btn-secondary" style="width:100%;font-size:12px" onclick="pickLocalFiles()">Загрузить из локального хранилища</button>
      </div>
    </div>
    <!-- External: Yandex/Spotify/Apple/SoundCloud -->
    <div id="impExternal" style="display:none">
      <div style="display:flex;gap:6px;margin-bottom:8px">
        <input type="text" id="impExtUrl" class="folder-path-input" style="flex:1;font-size:11px" placeholder="Ссылка на публичный плейлист...">
        <button class="folder-btn folder-btn-primary" style="padding:6px 12px;font-size:11px" onclick="importExternal()">Искать</button>
      </div>
      <div id="impExtStatus" style="font-size:11px;color:rgba(255,255,255,0.3);margin-bottom:6px"></div>
      <div id="impMatchList" style="max-height:35vh;overflow-y:auto;border-radius:8px"></div>
      <div id="impMatchActions" style="display:none;margin-top:6px">
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
          <select id="impExtMode" class="folder-select" style="flex:1;padding:6px 24px 6px 8px;font-size:11px"><option value="prepend">В начало</option><option value="append">В конец</option></select>
          <label style="display:flex;align-items:center;gap:4px;color:rgba(255,255,255,0.4);font-size:11px;white-space:nowrap;cursor:pointer"><input type="checkbox" id="impExtMeta" style="accent-color:#e94560"> Meta</label>
          <button class="folder-btn folder-btn-secondary" style="padding:4px 8px;font-size:10px" onclick="impToggleAll(true)">Все</button>
          <button class="folder-btn folder-btn-secondary" style="padding:4px 8px;font-size:10px" onclick="impToggleAll(false)">Нет</button>
        </div>
        <div style="display:flex;gap:6px">
          <button class="folder-btn folder-btn-primary" style="flex:1;font-size:12px" onclick="downloadImportMatches()">Скачать выбранные</button>
          <button class="folder-btn folder-btn-secondary" style="flex:1;font-size:11px" id="impRetryBtn" onclick="retryUnmatched()" disabled data-tip="Повторить поиск ненайденных треков">Повторить поиск</button>
        </div>
      </div>
    </div>
    <!-- Search -->
    <div id="impSearch" style="display:none">
      <div style="display:flex;gap:6px;margin-bottom:8px">
        <input type="text" id="vkSearchQuery" class="folder-path-input" style="flex:1;font-size:11px" placeholder="Название трека или артист..." onkeydown="if(event.key==='Enter')vkSearchTracks()">
        <button class="folder-btn folder-btn-primary" style="padding:6px 12px;font-size:11px" onclick="vkSearchTracks()">Найти</button>
      </div>
      <div id="vkSearchResults" style="max-height:180px;overflow-y:auto;border-radius:8px"></div>
      <div id="vkQueueSection" style="display:none;margin-top:6px">
        <div style="font-size:11px;color:rgba(255,255,255,0.3);margin-bottom:4px">Очередь (перетащите для порядка):</div>
        <div id="vkQueue" style="max-height:25vh;overflow-y:auto;border:1px solid rgba(255,255,255,0.06);border-radius:8px;background:rgba(255,255,255,0.02)"></div>
        <div style="display:flex;gap:6px;align-items:center;margin-top:6px">
          <select id="vkSearchMode" class="folder-select" style="flex:1;padding:6px 24px 6px 8px;font-size:11px"><option value="prepend">В начало</option><option value="append">В конец</option></select>
          <label style="display:flex;align-items:center;gap:4px;color:rgba(255,255,255,0.4);font-size:11px;white-space:nowrap;cursor:pointer"><input type="checkbox" id="vkSearchMeta" style="accent-color:#e94560"> Meta</label>
        </div>
        <button class="folder-btn folder-btn-primary" style="width:100%;margin-top:6px;font-size:12px" onclick="vkDownloadSelected()">Скачать очередь</button>
      </div>
    </div>
    </div>
    <!-- Progress (shared) -->
    <div id="vkProgressSection" style="display:none;margin-top:8px;flex-shrink:0">
      <div class="meta-progress" id="vkProgress"></div>
      <div class="meta-bar"><div class="meta-bar-fill" id="vkBarFill" style="width:0%"></div></div>
      <div class="meta-log" id="vkLog" style="max-height:150px;overflow-y:auto"></div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px;flex-shrink:0">
      <button onclick="cancelVkDownload()" class="folder-btn folder-btn-primary">Отменить</button>
      <button onclick="closeVkModal()" class="folder-btn folder-btn-secondary">Закрыть</button>
    </div>
  </div>
</div>

<div class="mobile-bar">
  <div class="mobile-bar-inner">
    <button class="mobile-mini-btn" id="mobilePlayBtn" onclick="togglePlay()"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></button>
    <div class="mobile-toggle" id="mobileToggle" onclick="mobileToggleView()">
      <div class="mobile-toggle-bg right" id="toggleBg"></div>
      <button id="btnVinyl"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="12" cy="12" r="4" fill="currentColor"/></svg></button>
      <button class="active" id="btnPlaylist"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z"/></svg></button>
    </div>
    <button class="mobile-mini-btn" id="mobileNextBtn" onclick="nextTrack()"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg></button>
  </div>
</div>

<div class="toast" id="toast"></div>
<div class="tip-popup" id="tipPopup"></div>

<!-- WAN mode modal -->
<div class="meta-overlay" id="wanModeOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this){this.classList.remove('show');setToggle('wanToggle','wanDot',false)}">
  <div class="meta-modal" style="width:min(400px,90vw)">
    <h3>Внешний доступ (WAN)</h3>
    <div style="display:flex;flex-direction:column;gap:6px;margin:12px 0">
      <button class="folder-btn folder-btn-secondary" style="padding:12px;text-align:left;white-space:normal" onclick="startWanMode('tunnel')">
        <div style="font-weight:600;font-size:13px">Cloudflare Tunnel</div>
        <div style="font-size:11px;color:rgba(255,255,255,0.35);margin-top:2px">Автоматический HTTPS-туннель, не нужен статический IP</div>
      </button>
      <button class="folder-btn folder-btn-secondary" style="padding:12px;text-align:left;white-space:normal" onclick="document.getElementById('wanStaticForm').style.display=''">
        <div style="font-weight:600;font-size:13px">Статический IP / VPS</div>
        <div style="font-size:11px;color:rgba(255,255,255,0.35);margin-top:2px">Прямой доступ по IP для VPS и выделенных серверов</div>
      </button>
    </div>
    <div id="wanStaticForm" style="display:none;margin-top:8px">
      <div style="font-size:12px;color:rgba(255,255,255,0.4);margin-bottom:6px">Настройки прямого доступа</div>
      <div style="display:flex;gap:6px;margin-bottom:6px">
        <input type="text" id="wanStaticIp" class="folder-path-input" style="flex:2" placeholder="IP (напр. 85.192.12.34)">
        <input type="text" id="wanStaticPort" class="folder-path-input" style="flex:1" placeholder="7656">
      </div>
      <button class="folder-btn folder-btn-primary" style="width:100%" onclick="startWanMode('static')">Подключить</button>
    </div>
    <button class="folder-btn folder-btn-secondary" style="width:100%;margin-top:10px" onclick="document.getElementById('wanModeOverlay').classList.remove('show');setToggle('wanToggle','wanDot',false)">Отмена</button>
  </div>
</div>

<!-- Server address modal — rescue when the LAN IP changed under an installed PWA -->
<div class="meta-overlay" id="serverOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')">
  <div class="meta-modal" style="width:min(420px,90vw)">
    <h3>Подключение к серверу</h3>
    <div style="font-size:11px;color:rgba(255,255,255,0.35);margin:6px 0 12px">Текущий адрес: <span id="srvCurrent"></span></div>
    <div style="font-size:12px;color:rgba(255,255,255,0.4);margin-bottom:6px">Известные адреса</div>
    <div id="srvList" style="display:flex;flex-direction:column;gap:6px"></div>
    <div style="font-size:12px;color:rgba(255,255,255,0.4);margin:14px 0 6px">Другой адрес</div>
    <div style="display:flex;gap:6px">
      <input type="text" id="srvManual" class="folder-path-input" style="flex:1" placeholder="192.168.1.50 или имя.local" autocapitalize="off" autocorrect="off" spellcheck="false" onkeydown="if(event.key==='Enter')goToManualServer()">
      <button class="folder-btn folder-btn-primary" onclick="goToManualServer()">Перейти</button>
    </div>
    <div id="srvHint" style="font-size:11px;color:rgba(255,255,255,0.3);margin-top:12px;line-height:1.55"></div>
    <div style="display:flex;gap:8px;margin-top:14px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.06)">
      <button class="folder-btn folder-btn-secondary" style="flex:1" onclick="location.href='/reset'">Обновить приложение</button>
      <button class="folder-btn folder-btn-secondary" style="flex:1" onclick="document.getElementById('serverOverlay').classList.remove('show')">Закрыть</button>
    </div>
  </div>
</div>

<!-- Playlist edit modal -->
<div class="meta-overlay" id="plEditOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')">
  <div class="meta-modal" style="width:min(480px,92vw);max-height:85vh;display:flex;flex-direction:column;overflow:hidden">
    <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
      <h3 id="plEditTitle" style="flex:1">Плейлист</h3>
    </div>
    <input type="text" id="plEditName" class="folder-path-input" style="margin:8px 0;flex-shrink:0" placeholder="Название плейлиста">
    <div style="display:flex;gap:6px;margin-bottom:8px;flex-shrink:0">
      <button class="folder-btn folder-btn-secondary" style="flex:1;font-size:11px" onclick="plAddTracks()">+ Добавить треки</button>
    </div>
    <div id="plEditTracks" style="flex:1;overflow-y:auto;min-height:0;border:1px solid rgba(255,255,255,0.06);border-radius:8px"></div>
    <div style="display:flex;gap:6px;margin-top:8px;flex-shrink:0">
      <button class="folder-btn folder-btn-primary" style="flex:1" onclick="savePlEdit()">Сохранить</button>
      <button class="folder-btn folder-btn-secondary" style="flex:1" onclick="document.getElementById('plEditOverlay').classList.remove('show')">Отмена</button>
      <button class="folder-btn folder-btn-secondary" id="plDeleteBtn" style="color:#e94560;flex-shrink:0;padding:8px 12px" onclick="deletePlEdit()" data-tip="Удалить плейлист">&#10005;</button>
    </div>
  </div>
</div>

<!-- Playlist add tracks modal -->
<div class="meta-overlay" id="plAddOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')" style="z-index:110">
  <div class="meta-modal" style="width:min(440px,90vw);max-height:80vh;display:flex;flex-direction:column;overflow:hidden">
    <h3>Выбрать треки</h3>
    <input type="text" id="plAddSearch" class="folder-path-input" style="margin:6px 0;flex-shrink:0" placeholder="Поиск..." oninput="filterPlAddTracks(this.value)">
    <div id="plAddList" style="flex:1;overflow-y:auto;min-height:0"></div>
    <div style="display:flex;gap:6px;margin-top:8px;flex-shrink:0">
      <button class="folder-btn folder-btn-primary" style="flex:1" onclick="confirmPlAdd('start')">В начало</button>
      <button class="folder-btn folder-btn-primary" style="flex:1" onclick="confirmPlAdd('order')">По порядку</button>
      <button class="folder-btn folder-btn-primary" style="flex:1" onclick="confirmPlAdd('end')">В конец</button>
    </div>
  </div>
</div>

<!-- Track edit modal -->
<div class="meta-overlay" id="trackEditOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')">
  <div class="meta-modal ti-modal" id="tiModal">
    <div class="ti-head">
      <div class="ti-cover">
        <img id="tiCover" alt="">
        <div id="tiCoverPh" class="ti-cover-ph">&#9835;</div>
        <button class="ti-cover-pick ti-edit" onclick="tiPickCover()">Заменить</button>
      </div>
      <div class="ti-head-txt">
        <div class="ti-h-title" id="tiHeadTitle"></div>
        <div class="ti-h-artist" id="tiHeadArtist"></div>
        <div class="ti-h-file" id="trackEditFile"></div>
      </div>
    </div>
    <input type="file" id="tiCoverInput" accept="image/*" style="display:none" onchange="tiCoverChosen(this)">

    <div class="ti-rows">
      <div class="ti-row"><span class="ti-k">Название</span>
        <span class="ti-v ti-view" id="tiValTitle"></span>
        <input class="ti-i ti-edit" type="text" id="trackEditTitle"></div>
      <div class="ti-row"><span class="ti-k">Исполнитель</span>
        <span class="ti-v ti-view" id="tiValArtist"></span>
        <input class="ti-i ti-edit" type="text" id="trackEditArtist"></div>
      <div class="ti-row"><span class="ti-k">Альбом</span>
        <span class="ti-v ti-view" id="tiValAlbum"></span>
        <input class="ti-i ti-edit" type="text" id="tiAlbum"></div>
      <div class="ti-row"><span class="ti-k">Исполнитель альбома</span>
        <span class="ti-v ti-view" id="tiValAart"></span>
        <input class="ti-i ti-edit" type="text" id="tiAart"></div>
      <div class="ti-row"><span class="ti-k">Год</span>
        <span class="ti-v ti-view" id="tiValYear"></span>
        <input class="ti-i ti-edit" type="number" id="tiYear" placeholder="—"></div>
      <div class="ti-row"><span class="ti-k">Жанр</span>
        <span class="ti-v ti-view" id="tiValGenre"></span>
        <input class="ti-i ti-edit" type="text" id="tiGenre"></div>
      <div class="ti-row"><span class="ti-k">Номер в альбоме</span>
        <span class="ti-v ti-view" id="tiValTrk"></span>
        <input class="ti-i ti-edit" type="number" id="tiTrk" min="1" placeholder="—"></div>
      <div class="ti-row" id="trackEditOrderRow"><span class="ti-k">Позиция в каталоге <span id="trackEditOrderHint"></span></span>
        <span class="ti-v ti-view" id="tiValOrder"></span>
        <input class="ti-i ti-edit" type="number" id="trackEditOrder" min="1" placeholder="—"></div>
      <div class="ti-row"><span class="ti-k">Длительность</span><span class="ti-v" id="tiValDur"></span></div>
      <div class="ti-row"><span class="ti-k">Качество</span><span class="ti-v" id="tiValQuality"></span></div>
    </div>

    <label class="ti-edit" style="display:flex;align-items:center;gap:6px;color:rgba(255,255,255,0.5);cursor:pointer;font-size:12px;margin:10px 0 4px">
      <input type="checkbox" id="trackEditMeta" style="accent-color:#e94560"> Найти метаданные после сохранения
    </label>

    <div id="trackEditCacheRow" style="display:none;align-items:center;gap:8px;margin-top:10px;padding:8px 10px;background:rgba(255,255,255,0.04);border-radius:8px">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="#52b788"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
      <span style="font-size:12px;color:rgba(255,255,255,0.5);flex:1">Закэширован</span>
      <button class="folder-btn folder-btn-secondary" style="padding:4px 10px;font-size:11px" onclick="uncacheEditTrack()">Убрать из кэша</button>
    </div>

    <div class="ti-actions">
      <button class="folder-btn folder-btn-primary ti-view" style="flex:1" onclick="tiSetEdit(true)">Изменить</button>
      <button class="folder-btn folder-btn-secondary ti-view" style="flex:1" onclick="document.getElementById('trackEditOverlay').classList.remove('show')">Закрыть</button>
      <button class="folder-btn folder-btn-primary ti-edit" style="flex:1" onclick="saveTrackEdit()">Сохранить</button>
      <button class="folder-btn folder-btn-secondary ti-edit" style="flex:1" onclick="tiSetEdit(false)">Отмена</button>
      <button class="folder-btn folder-btn-secondary ti-edit" style="color:#e94560;flex-shrink:0;padding:8px 12px" onclick="deleteEditTrack()" data-tip="Удалить трек">&#10005;</button>
    </div>
  </div>
</div>

<!-- Browse modal -->
<div class="meta-overlay" id="browseOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')">
  <div class="meta-modal" style="width:480px;max-height:80vh;display:flex;flex-direction:column">
    <h3>Выбор каталога</h3>
    <div style="display:flex;gap:6px;margin-bottom:8px;align-items:center">
      <input type="text" id="browsePath" class="folder-path-input" style="flex:1;font-size:12px" onkeydown="if(event.key==='Enter')browseTo(this.value)">
      <button class="folder-btn folder-btn-secondary" onclick="browseTo(document.getElementById('browsePath').value)">Перейти</button>
    </div>
    <div id="browseList" style="flex:1;overflow-y:auto;border:1px solid rgba(255,255,255,0.06);border-radius:8px;background:#111;min-height:200px"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
      <button class="folder-btn folder-btn-primary" onclick="browseSelect()">Выбрать эту папку</button>
      <button class="folder-btn folder-btn-secondary" onclick="document.getElementById('browseOverlay').classList.remove('show')">Отмена</button>
    </div>
  </div>
</div>

<!-- Profile modal -->
<div class="meta-overlay" id="profileOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')">
  <div class="meta-modal" style="width:360px">
    <h3>Профиль</h3>
    <div style="font-size:14px;color:rgba(255,255,255,0.6);margin:8px 0 16px" id="profileUser"></div>
    <button class="folder-btn folder-btn-secondary" style="width:100%" onclick="document.getElementById('profPwSection').style.display=document.getElementById('profPwSection').style.display==='none'?'':'none'">Сменить пароль</button>
    <div id="profPwSection" style="display:none;margin-top:10px">
      <div class="pw-field"><input type="password" id="profOldPw" placeholder="Текущий пароль"><button class="pw-eye" onclick="togglePwVis('profOldPw',this)"><img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIGZpbGw9IiM4ODgiIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyIDQuNUM3IDQuNSAyLjczIDcuNjEgMSAxMmMxLjczIDQuMzkgNiA3LjUgMTEgNy41czkuMjctMy4xMSAxMS03LjVjLTEuNzMtNC4zOS02LTcuNS0xMS03LjV6TTEyIDE3Yy0yLjc2IDAtNS0yLjI0LTUtNXMyLjI0LTUgNS01IDUgMi4yNCA1IDUtMi4yNCA1LTUgNXptMC04Yy0xLjY2IDAtMyAxLjM0LTMgM3MxLjM0IDMgMyAzIDMtMS4zNCAzLTMtMS4zNC0zLTMtM3oiLz48L3N2Zz4="></button></div>
      <div class="pw-field" style="margin-top:6px"><input type="password" id="profNewPw" placeholder="Новый пароль"><button class="pw-eye" onclick="togglePwVis('profNewPw',this)"><img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIGZpbGw9IiM4ODgiIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyIDQuNUM3IDQuNSAyLjczIDcuNjEgMSAxMmMxLjczIDQuMzkgNiA3LjUgMTEgNy41czkuMjctMy4xMSAxMS03LjVjLTEuNzMtNC4zOS02LTcuNS0xMS03LjV6TTEyIDE3Yy0yLjc2IDAtNS0yLjI0LTUtNXMyLjI0LTUgNS01IDUgMi4yNCA1IDUtMi4yNCA1LTUgNXptMC04Yy0xLjY2IDAtMyAxLjM0LTMgM3MxLjM0IDMgMyAzIDMtMS4zNCAzLTMtMS4zNC0zLTMtM3oiLz48L3N2Zz4="></button></div>
      <button class="folder-btn folder-btn-primary" style="width:100%;margin-top:8px" onclick="changeMyPassword()">Сохранить</button>
    </div>
    <div style="margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.06)">
      <button class="perf-toggle" id="perfToggle" onclick="perfToggleOpen()">
        <span>Графика</span><span class="perf-chev">&#9662;</span>
      </button>
      <div id="perfBody" style="display:none">
      <label class="perf-row"><input type="checkbox" id="perfPauseBlur" onchange="perfSet('pauseBlur',this.checked)">
        <span><b>Замирать без фокуса</b>
        <i>Анимации останавливаются, когда окно неактивно. Звук продолжает играть.</i></span></label>
      <label class="perf-row"><input type="checkbox" id="perfNoBlur" onchange="perfSet('noBlur',this.checked)">
        <span><b>Панель без размытия</b>
        <i>Список треков станет плотным вместо матового стекла. Размытие пересчитывается на каждом кадре фона — для видеоядра это самое дорогое здесь.</i></span></label>
      <label class="perf-row"><input type="checkbox" id="perfBgStatic" onchange="perfSet('bgStatic',this.checked)">
        <span><b>Неподвижный фон</b>
        <i>Цветные пятна перестанут плыть. Останется градиент под цвет обложки, он всё так же меняется со сменой трека.</i></span></label>
      <label class="perf-row"><input type="checkbox" id="perfRadioStatic" onchange="perfSet('radioStatic',this.checked)">
        <span><b>Неподвижная рамка радио</b>
        <i>Свечение вокруг плеера перестанет вращаться и пульсировать. Цвет останется.</i></span></label>
      </div>
    </div>

    <div style="margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.06)">
      <div style="font-size:12px;color:rgba(255,255,255,0.4);margin-bottom:8px">Офлайн-кэш</div>
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:12px;color:rgba(255,255,255,0.5);flex:1" id="profileCacheInfo"></span>
        <button class="folder-btn folder-btn-secondary" style="padding:6px 12px;font-size:12px;color:#e94560" onclick="clearAllCache()">Очистить кэш</button>
      </div>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button class="folder-btn folder-btn-secondary" style="flex:1;font-size:12px" onclick="openServerDialog()">Адрес сервера</button>
        <button class="folder-btn folder-btn-secondary" style="flex:1;font-size:12px" onclick="location.href='/reset'">Обновить приложение</button>
      </div>
      <div id="buildInfo" style="font-size:11px;color:rgba(255,255,255,0.3);margin-top:8px;line-height:1.5"></div>
      <button class="folder-btn folder-btn-secondary" style="width:100%;font-size:12px;margin-top:8px" onclick="toggleMediaLog()">Журнал медиа-событий</button>
      <div id="mediaLogBox" style="display:none;margin-top:8px">
        <pre id="mediaLogText" style="font-size:10px;line-height:1.45;color:rgba(255,255,255,0.45);background:rgba(0,0,0,0.25);border-radius:8px;padding:8px;max-height:240px;overflow:auto;white-space:pre-wrap;margin:0"></pre>
        <div style="display:flex;gap:8px;margin-top:6px">
          <button class="folder-btn folder-btn-secondary" style="flex:1;font-size:12px" onclick="renderMediaLog()">Обновить</button>
          <button class="folder-btn folder-btn-secondary" style="flex:1;font-size:12px" onclick="copyMediaLog()">Копировать</button>
          <button class="folder-btn folder-btn-secondary" style="flex:1;font-size:12px;color:#e94560" onclick="clearMediaLog()">Очистить</button>
        </div>
        <button id="scratchCtxBtn" class="folder-btn folder-btn-secondary" style="width:100%;font-size:12px;margin-top:6px" onclick="toggleScratchCtx()">Звук скретча: вкл</button>
        <button id="recoverBtn" class="folder-btn folder-btn-secondary" style="width:100%;font-size:12px;margin-top:6px" onclick="toggleRecover()">Пересборка при застревании: выкл</button>
      </div>
    </div>
    <div style="display:flex;gap:8px;margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.06)">
      <button class="folder-btn folder-btn-secondary" style="flex:1" onclick="doLogout()">Выйти</button>
      <button class="folder-btn folder-btn-secondary" style="flex:1" onclick="document.getElementById('profileOverlay').classList.remove('show')">Закрыть</button>
    </div>
  </div>
</div>

<!-- Admin modal -->
<div class="meta-overlay" id="adminOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')">
  <div class="meta-modal" style="width:440px;max-height:80vh;display:flex;flex-direction:column;overflow:hidden">
    <h3>Управление пользователями</h3>
    <div id="adminUserList" style="flex:1;overflow-y:auto;margin:10px 0;min-height:0"></div>
    <div style="border-top:1px solid rgba(255,255,255,0.08);padding-top:12px;margin-top:8px">
      <div style="font-size:12px;color:rgba(255,255,255,0.5);margin-bottom:4px">Корневая папка музыки</div>
      <div class="fp-row" style="margin-bottom:10px">
        <input type="text" id="adminMusicRoot" class="folder-path-input" style="flex:1" placeholder="/path/to/music">
        <button class="folder-btn folder-btn-primary" style="padding:7px 12px" onclick="saveMusicRoot()">Сохранить</button>
      </div>
    </div>
    <div style="border-top:1px solid rgba(255,255,255,0.08);padding-top:12px;margin-top:8px">
      <div style="font-size:12px;color:rgba(255,255,255,0.5);margin-bottom:6px">Создать пользователя</div>
      <div style="display:flex;gap:6px;margin-bottom:6px">
        <input type="text" id="newUserName" class="folder-path-input" placeholder="Логин" style="flex:1">
        <input type="password" id="newUserPw" class="folder-path-input" placeholder="Пароль" style="flex:1">
      </div>
      <div style="display:flex;gap:6px;margin-bottom:6px;align-items:center">
        <span style="font-size:12px;color:rgba(255,255,255,0.4)">Роль:</span>
        <select id="newUserRole" class="folder-select" style="flex:1;padding:7px 28px 7px 10px;font-size:12px">
          <option value="user">Пользователь</option>
          <option value="admin">Администратор</option>
          <option value="demo">Демо</option>
        </select>
        <button onclick="showRolesHelp()" style="width:24px;height:24px;border:none;border-radius:50%;background:rgba(255,255,255,0.08);color:rgba(255,255,255,0.3);cursor:pointer;font-size:13px;font-weight:700;flex-shrink:0;display:flex;align-items:center;justify-content:center">?</button>
      </div>
      <button class="folder-btn folder-btn-primary" style="width:100%" onclick="adminCreateUser()">Создать</button>
    </div>
    <div style="display:flex;justify-content:flex-end;margin-top:12px">
      <button class="folder-btn folder-btn-secondary" onclick="document.getElementById('adminOverlay').classList.remove('show')">Закрыть</button>
    </div>
  </div>
</div>

<!-- Roles help modal -->
<div class="meta-overlay" id="rolesHelpOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')">
  <div class="meta-modal" style="width:min(480px,92vw);max-height:85vh;overflow-y:auto">
    <h3>Роли пользователей</h3>
    <table style="width:100%;border-collapse:collapse;font-size:12px;margin:12px 0">
      <tr style="border-bottom:1px solid rgba(255,255,255,0.1)">
        <th style="text-align:left;padding:6px 8px;color:rgba(255,255,255,0.5)">Возможность</th>
        <th style="padding:6px 8px;color:#e94560">Админ</th>
        <th style="padding:6px 8px;color:#52b788">Пользователь</th>
        <th style="padding:6px 8px;color:#e9a545">Демо</th>
      </tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,0.04)"><td style="padding:6px 8px">Слушать музыку</td><td style="text-align:center">✓</td><td style="text-align:center">✓</td><td style="text-align:center">✓</td></tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,0.04)"><td style="padding:6px 8px">Поиск по трекам</td><td style="text-align:center">✓</td><td style="text-align:center">✓</td><td style="text-align:center">✓</td></tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,0.04)"><td style="padding:6px 8px">Свои каталоги</td><td style="text-align:center;color:rgba(255,255,255,0.4)">любые</td><td style="text-align:center;color:rgba(255,255,255,0.4)">в MUSIC_ROOT</td><td style="text-align:center;color:rgba(255,255,255,0.4)">фиксированный</td></tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,0.04)"><td style="padding:6px 8px">Добавлять каталоги</td><td style="text-align:center">✓</td><td style="text-align:center">✓</td><td style="text-align:center;color:rgba(255,255,255,0.15)">—</td></tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,0.04)"><td style="padding:6px 8px">Поиск мета-данных</td><td style="text-align:center">✓</td><td style="text-align:center">✓</td><td style="text-align:center;color:rgba(255,255,255,0.15)">—</td></tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,0.04)"><td style="padding:6px 8px">Импорт из VK / площадок</td><td style="text-align:center">✓</td><td style="text-align:center">✓</td><td style="text-align:center;color:rgba(255,255,255,0.15)">—</td></tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,0.04)"><td style="padding:6px 8px">Редактирование треков</td><td style="text-align:center">✓</td><td style="text-align:center">✓</td><td style="text-align:center;color:rgba(255,255,255,0.15)">—</td></tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,0.04)"><td style="padding:6px 8px">Скачивание каталога</td><td style="text-align:center">✓</td><td style="text-align:center;color:rgba(255,255,255,0.15)">—</td><td style="text-align:center;color:rgba(255,255,255,0.15)">—</td></tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,0.04)"><td style="padding:6px 8px">LAN / WAN доступ</td><td style="text-align:center">✓</td><td style="text-align:center;color:rgba(255,255,255,0.15)">—</td><td style="text-align:center;color:rgba(255,255,255,0.15)">—</td></tr>
      <tr><td style="padding:6px 8px">Управление пользователями</td><td style="text-align:center">✓</td><td style="text-align:center;color:rgba(255,255,255,0.15)">—</td><td style="text-align:center;color:rgba(255,255,255,0.15)">—</td></tr>
    </table>
    <div style="font-size:11px;color:rgba(255,255,255,0.3);margin-bottom:12px">
      <b style="color:#e94560">Админ</b> — полный доступ, управление сервером и пользователями.<br>
      <b style="color:#52b788">Пользователь</b> — работа с музыкой в своих каталогах.<br>
      <b style="color:#e9a545">Демо</b> — только прослушивание, для демонстрации.
    </div>
    <button class="folder-btn folder-btn-secondary" style="width:100%" onclick="document.getElementById('rolesHelpOverlay').classList.remove('show')">Закрыть</button>
  </div>
</div>

<!-- Admin password change modal -->
<div class="meta-overlay" id="pwChangeOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')">
  <div class="meta-modal" style="width:380px">
    <h3>Сменить пароль</h3>
    <div style="font-size:14px;color:rgba(255,255,255,0.5);margin-bottom:12px" id="pwChangeUser"></div>
    <div class="pw-field" style="margin-bottom:6px"><input type="password" id="pwChangeNew" placeholder="Новый пароль"><button class="pw-eye" onclick="togglePwVis('pwChangeNew',this)"><img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIGZpbGw9IiM4ODgiIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyIDQuNUM3IDQuNSAyLjczIDcuNjEgMSAxMmMxLjczIDQuMzkgNiA3LjUgMTEgNy41czkuMjctMy4xMSAxMS03LjVjLTEuNzMtNC4zOS02LTcuNS0xMS03LjV6TTEyIDE3Yy0yLjc2IDAtNS0yLjI0LTUtNXMyLjI0LTUgNS01IDUgMi4yNCA1IDUtMi4yNCA1LTUgNXptMC04Yy0xLjY2IDAtMyAxLjM0LTMgM3MxLjM0IDMgMyAzIDMtMS4zNCAzLTMtMS4zNC0zLTMtM3oiLz48L3N2Zz4="></button></div>
    <div class="pw-field"><input type="password" id="pwChangeConfirm" placeholder="Подтвердите пароль"><button class="pw-eye" onclick="togglePwVis('pwChangeConfirm',this)"><img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIGZpbGw9IiM4ODgiIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyIDQuNUM3IDQuNSAyLjczIDcuNjEgMSAxMmMxLjczIDQuMzkgNiA3LjUgMTEgNy41czkuMjctMy4xMSAxMS03LjVjLTEuNzMtNC4zOS02LTcuNS0xMS03LjV6TTEyIDE3Yy0yLjc2IDAtNS0yLjI0LTUtNXMyLjI0LTUgNS01IDUgMi4yNCA1IDUtMi4yNCA1LTUgNXptMC04Yy0xLjY2IDAtMyAxLjM0LTMgM3MxLjM0IDMgMyAzIDMtMS4zNCAzLTMtMS4zNC0zLTMtM3oiLz48L3N2Zz4="></button></div>
    <div style="display:flex;gap:8px;margin-top:12px">
      <button class="folder-btn folder-btn-primary" style="flex:1" onclick="submitPwChange()">Сменить</button>
      <button class="folder-btn folder-btn-secondary" style="flex:1" onclick="document.getElementById('pwChangeOverlay').classList.remove('show')">Отмена</button>
    </div>
  </div>
</div>

<!-- App info -->
<div class="meta-overlay" id="appInfoOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')">
  <div class="meta-modal" style="width:min(360px,88vw);text-align:center">
    <div id="appInfoContent" style="padding:12px 0"></div>
    <button class="folder-btn folder-btn-secondary" style="margin-top:12px" onclick="document.getElementById('appInfoOverlay').classList.remove('show')">Закрыть</button>
  </div>
</div>

<!-- Confirm dialog -->
<div class="meta-overlay" id="radioOverlay">
  <div class="meta-modal" style="width:460px;max-width:94vw;text-align:left;max-height:88vh;overflow:auto">
    <div style="font-size:15px;font-weight:600;margin-bottom:6px">Радиостанция</div>
    <div style="font-size:12px;color:rgba(255,255,255,0.45);line-height:1.5;margin-bottom:14px">
      Бесконечный поток из вашей библиотеки. Критерии можно менять на ходу – играющий трек не прервётся.
    </div>
    <div id="radioBody"></div>
    <div id="radioCount" class="radio-count"></div>
    <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
      <button class="folder-btn folder-btn-primary" style="flex:1 1 130px" id="radioGo" onclick="radioApply()">Включить</button>
      <button class="folder-btn folder-btn-secondary" style="flex:1 1 110px" onclick="radioRandomize()" data-tip="Подобрать критерии случайно">Случайно</button>
      <button class="folder-btn folder-btn-secondary" style="flex:1 1 90px" onclick="closeRadioModal()">Отмена</button>
    </div>
  </div>
</div>

<div class="meta-overlay" id="eraPickOverlay">
  <div class="meta-modal" style="width:520px;max-width:96vw;text-align:left;display:flex;flex-direction:column;max-height:88vh">
    <div style="font-size:14px;font-weight:600;margin-bottom:8px" id="eraPickTitle">Граница периода</div>
    <input type="text" id="eraPickSearch" class="folder-path-input" placeholder="Поиск по исполнителю или названию"
           autocapitalize="off" autocorrect="off" spellcheck="false" oninput="renderEraPick(this.value)" style="width:100%;margin-bottom:8px">
    <div id="eraPickList" class="era-pick-list"></div>
    <div id="eraPickNote" style="font-size:11px;color:rgba(255,255,255,0.3);margin-top:8px"></div>
    <button class="folder-btn folder-btn-secondary" style="width:100%;margin-top:10px" onclick="closeEraPick()">Отмена</button>
  </div>
</div>

<div class="meta-overlay" id="erasOverlay">
  <div class="meta-modal" style="width:440px;max-width:94vw;text-align:left">
    <div style="font-size:15px;font-weight:600;margin-bottom:6px">Периоды прослушивания</div>
    <div style="font-size:12px;color:rgba(255,255,255,0.45);line-height:1.55;margin-bottom:14px">
      Каталог идёт от свежих треков к старым. Разметьте пачки номеров годами – из них соберутся плейлисты «Слушал в&nbsp;…».
      Границы привязываются к самим трекам, поэтому переживают перенумерацию: добавили музыку – периоды поехали вместе с ней,
      трек вклинился внутрь – попал в свой год. Период, начинающийся с №1, растёт вверх сам.
    </div>
    <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:12px;cursor:pointer">
      <input type="checkbox" id="erasEnabled"> Собирать плейлисты по периодам
    </label>
    <div id="erasRows"></div>
    <button class="folder-btn folder-btn-secondary" style="width:100%;font-size:12px;margin-top:8px" onclick="addEraRow()">+ Добавить период</button>
    <div id="erasHint" style="font-size:11px;color:rgba(255,255,255,0.3);margin-top:10px"></div>
    <div style="display:flex;gap:8px;margin-top:16px">
      <button class="folder-btn folder-btn-primary" style="flex:1" onclick="applyEras()">Сохранить</button>
      <button class="folder-btn folder-btn-secondary" style="flex:1" onclick="closeErasModal()">Отмена</button>
    </div>
  </div>
</div>

<div class="meta-overlay" id="confirmOverlay">
  <div class="meta-modal" style="width:360px;text-align:center">
    <div id="confirmText" style="font-size:15px;margin:12px 0 20px"></div>
    <div style="display:flex;gap:8px;justify-content:center">
      <button class="folder-btn" style="background:#e94560;color:#fff;min-width:80px" id="confirmYes">Удалить</button>
      <button class="folder-btn folder-btn-secondary" style="min-width:80px" onclick="closeConfirm()">Отмена</button>
    </div>
  </div>
</div>
<audio id="audioEl"></audio>
<audio id="previewEl" preload="none"></audio>

<!-- Track context menu -->
<div class="ctx-menu" id="ctxMenu"></div>

<!-- Playlist context menu -->
<div class="ctx-menu" id="plCtxMenu">
  <div class="ctx-item danger" onclick="plCtxDelete()"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg> Удалить плейлист</div>
</div>

<script>
// ── iOS PWA audio session fix ──
// iOS standalone PWA cannot activate audio session (WebKit bug).
// Detect: play() resolves but currentTime stays 0. Show overlay
// with link that opens Safari (target=_blank), which activates the
// shared system audio session. User returns to PWA — audio works.
var _pwaAudioChecked = false;
var _pwaRecoverAttempts = 0;

function _pwaRecoverAudio() {
  if (typeof mediaLog === 'function') mediaLog('pwa:recover', mediaLogState());
  // iOS PWA audio session recovery:
  // 1. Try re-creating audio element (clears stale WebKit audio state)
  // 2. Try silent AudioContext unlock (activates system audio session)
  // 3. If all fails, prompt user to open in Safari to fix audio session
  _pwaRecoverAttempts++;
  if (_pwaRecoverAttempts <= 2) {
    // Attempt 1-2: recreate audio element + AudioContext unlock
    var parent = audio.parentNode;
    var newAudio = document.createElement('audio');
    newAudio.id = 'audioEl';
    parent.replaceChild(newAudio, audio);
    audio = newAudio;
    audio.volume = 0.8;
    bindAudioEvents();
    // Silent AudioContext unlock to activate system audio session
    try {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      gain.gain.value = 0;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.01);
      ctx.close();
    } catch(e) {}
    _pwaAudioChecked = false; // allow re-check on next play
    showToast('Восстановление аудио…');
    setTimeout(function() { togglePlay(); }, 300);
  } else {
    // All retries failed — notify user
    showToast('Аудио не запускается. Попробуйте закрыть и открыть приложение');
  }
}

function _d(s){return decodeURIComponent(escape(atob(s.split('').reverse().join(''))));}
var _n=_d("==wYpNXdtBSZkl2clRWaz5Wa");
// iOS doesn't support audio.volume — hide slider
var _isIOS=/iPad|iPhone|iPod/.test(navigator.userAgent)||(/Mac/.test(navigator.userAgent)&&navigator.maxTouchPoints>1);
var _p=_d("lRWazVGZpNnbpBSeiBCZlJXZ39Gc");
var _l=_d("usY09CdtQnY01CNgR/L0wC9tQDCsQDY0+CtgRLL0wCNIPGNuQHY0wC9uQPL0+CdgRDytQXL0xCNI1CNuQ3L0wCtsQ7L03CNjRvL0+C9vQHY04CNI1CtvQrL0BGdtQfY0AGdtQzL08CtvQrL0ggL0g8Y04CthRDL06CNuQTY04CNtQ7L08CNIsUL04CdvQXL09CNsQDY0CGdgR7L0AG9vQHY0wCNoQDiLPGNuQ3L0wCtsQ7L03CNjRvL0+C9vQHY04CNI+C9sQ7L06CdgRXL0HGNgRXL08CNvQ7L06CdtQ3L0g4L0zCtvQ3L0HGNuQvL0g8Y07CNtQDivQ3L0MG9uQXL0CGNuQfY0OG9uQrL0BGNuQDivQ3L01C9hRDL09C9tQDL09CNtQXL0AG9vQDCuQDCvQ7L0CGtuQXL0+CNgR/L0gU2YyV3bzBiblB3bg8Y0BGtgRXL0PG9uQLL0PGNI1CNuQ3L01CttQ7L07CNuQDY0/CNI+CtgR3K0");
// Set titles
document.title=_n;
(function(){
  var m=document.querySelector('meta[name=apple-mobile-web-app-title]');if(m)m.content=_n;
  var ti=document.getElementById('trackTitle');if(ti&&ti.dataset.idle)ti.textContent=_n;
})();

// ── Player mode (vinyl / cassette) ──
var _playerMode = localStorage.getItem('_vc_player_mode') || 'vinyl';
var _ipodListMode = false;
var _ipodListOffset = 0;
var _ipodSelectedIdx = 0;

function _isMobile() { return window.innerWidth <= 768; }

function _modeToggleCollapse() {
  var tog = document.querySelector('.player-mode-toggle');
  if (tog && _isMobile()) tog.classList.add('collapsed');
}

function _modeToggleExpand(e) {
  var tog = document.querySelector('.player-mode-toggle');
  if (!tog || !_isMobile()) return;
  if (tog.classList.contains('collapsed')) {
    e.stopPropagation();
    e.preventDefault();
    tog.classList.remove('collapsed');
  }
}

function setPlayerMode(mode) {
  _playerMode = mode;
  localStorage.setItem('_vc_player_mode', mode);
  ['modeVinyl','modeCassette','modeIpod'].forEach(function(id) {
    document.getElementById(id).classList.remove('active');
  });
  document.getElementById(mode === 'vinyl' ? 'modeVinyl' : mode === 'cassette' ? 'modeCassette' : 'modeIpod').classList.add('active');
  var vs = document.querySelector('.vinyl-side');
  vs.classList.remove('player-mode-cassette', 'player-mode-ipod');
  if (mode !== 'vinyl') vs.classList.add('player-mode-' + mode);
  // Update mobile toggle icon to match current mode
  var modeIcons = {
    vinyl: '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="12" cy="12" r="4" fill="currentColor"/></svg>',
    cassette: '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="2" y="5" width="20" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="9" cy="13" r="2.5" fill="none" stroke="currentColor" stroke-width="1"/><circle cx="15" cy="13" r="2.5" fill="none" stroke="currentColor" stroke-width="1"/></svg>',
    ipod: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="5" y="1" width="14" height="22" rx="3"/><rect x="7" y="3" width="10" height="8" rx="1"/><circle cx="12" cy="17" r="3.5"/><circle cx="12" cy="17" r="1.5"/></svg>'
  };
  var mb = document.getElementById('btnVinyl');
  if (mb) mb.innerHTML = modeIcons[mode] || modeIcons.vinyl;
  // Collapse on mobile after selection
  _modeToggleCollapse();
  // Sync alt player with current track
  if (currentIdx >= 0 && currentIdx < tracks.length) {
    var t = tracks[currentIdx];
    if (mode === 'cassette') {
      document.getElementById('cassetteTitle').textContent = t.title;
      document.getElementById('cassetteArtist').textContent = t.artist;
    }
    if (mode === 'ipod') _ipodSyncTrack(t);
  }
  if (mode === 'ipod') {
    _ipodListMode = false;
    _ipodShowNp();
  }
}

function _ipodSyncTrack(t) {
  document.getElementById('ipodTitle').textContent = t.title;
  document.getElementById('ipodArtist').textContent = t.artist;
  document.getElementById('ipodAlbum').textContent = t.album || '';
  var ic = document.getElementById('ipodCover');
  var icp = document.getElementById('ipodCoverPh');
  setCoverSrc(ic, t.file, t.has_cover, icp);
}

function _ipodPlayOrToggle() {
  if (_ipodListMode) {
    if (_ipodSelectedIdx >= 0 && _ipodSelectedIdx < tracks.length) {
      playFromList(_ipodSelectedIdx);
      _ipodShowNp();
    }
  } else {
    togglePlay();
  }
}

function _ipodShowNp() {
  document.getElementById('ipodNpWrap').classList.remove('hidden');
  document.getElementById('ipodList').classList.remove('active');
  _ipodListMode = false;
}

function _ipodShowList() {
  _ipodListMode = true;
  _ipodSelectedIdx = currentIdx >= 0 ? currentIdx : 0;
  _ipodListOffset = Math.max(0, _ipodSelectedIdx - 3);
  document.getElementById('ipodNpWrap').classList.add('hidden');
  document.getElementById('ipodList').classList.add('active');
  _ipodRenderList();
}

function _ipodRenderList() {
  var container = document.getElementById('ipodListItems');
  var maxVisible = 9;
  var html = '';
  for (var i = _ipodListOffset; i < Math.min(tracks.length, _ipodListOffset + maxVisible); i++) {
    var t = tracks[i];
    var sel = i === _ipodSelectedIdx ? ' selected' : '';
    html += '<div class="ipod-list-item' + sel + '" data-idx="' + i + '">'
      + esc(t.artist ? t.artist + ' — ' : '') + esc(t.title) + '</div>';
  }
  container.innerHTML = html;
}

// Init mode on load
(function() {
  if (_playerMode !== 'vinyl') {
    document.getElementById('modeVinyl').classList.remove('active');
    if (_playerMode === 'cassette') document.getElementById('modeCassette').classList.add('active');
    if (_playerMode === 'ipod') document.getElementById('modeIpod').classList.add('active');
    document.querySelector('.vinyl-side').classList.add('player-mode-' + _playerMode);
    // Update mobile toggle icon
    setPlayerMode(_playerMode);
  }
  // Mobile: collapse to single icon, expand on tap
  var tog = document.querySelector('.player-mode-toggle');
  if (tog) {
    _modeToggleCollapse();
    tog.addEventListener('click', _modeToggleExpand);
    document.addEventListener('click', function(e) {
      if (_isMobile() && tog && !tog.contains(e.target) && !tog.classList.contains('collapsed')) {
        tog.classList.add('collapsed');
      }
    });
  }
})();

var tracks = [];
var filteredTracks = null; // null = show all
var albums = [];
var currentIdx = -1;
var playQueue = []; // ordered list of track indices for prev/next
var playQueuePos = -1; // position within playQueue
var isPlaying = false;
var audio = document.getElementById('audioEl');
var activeTab = 'tracks';
var expandedAlbum = null;
var savedFolders = [];

// ── Prefetch next track (cache warm, no swap) ──
var prefetchLink = null;

function prefetchNext() {
  if (playQueue.length < 2) return;
  var nextPos = (playQueuePos + 1) % playQueue.length;
  var nextIdx = playQueue[nextPos];
  if (nextIdx < 0 || nextIdx >= tracks.length) return;
  var nextFile = tracks[nextIdx].file;
  // Skip prefetch if already in IndexedDB cache
  if (isTrackCached(nextFile)) return;
  var url = '/api/stream/' + encodeURIComponent(nextFile);
  // Use <link rel=prefetch> to warm browser cache without creating audio conflicts
  if (prefetchLink) prefetchLink.remove();
  prefetchLink = document.createElement('link');
  prefetchLink.rel = 'prefetch';
  prefetchLink.href = url;
  prefetchLink.as = 'fetch';
  document.head.appendChild(prefetchLink);
}

// ── Scratch sound via Web Audio API ──
var audioCtx = null;
var _scratchOff = false;
try { _scratchOff = localStorage.getItem('_vc_noctx') === '1'; } catch (e) {}
var scratchGain = null;
var scratchNoise = null;
var scratchFilter = null;
var isScratchPlaying = false;

function acRevive(where) {
  if (!audioCtx || audioCtx.state === 'running') return;
  var p = null;
  try { p = audioCtx.resume(); }
  catch (e) { mediaLog('ac:resume>throw', where + ' ' + ((e && e.name) || '?')); return; }
  if (p && p.then) {
    p.then(function() { mediaLog('ac:resume>ok', where + ' ' + audioCtx.state); },
           function(e) { mediaLog('ac:resume>rej', where + ' ' + ((e && e.name) || '?')); });
  }
}

function scratchCtxRelease() {
  if (!audioCtx) return;
  try { audioCtx.close(); } catch (e) {}
  audioCtx = null; scratchGain = null; scratchFilter = null; scratchNoise = null;
  isScratchPlaying = false;
  if (typeof mediaLog === 'function') mediaLog('ac:closed');
}

function initScratchSound() {
  if (_scratchOff) return;
  if (audioCtx) {
    // iOS requires resume after user gesture
    if (audioCtx.state === 'suspended') audioCtx.resume();
    return;
  }
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  // iOS: resume on first interaction
  if (audioCtx.state === 'suspended') audioCtx.resume();
  var bufSize = audioCtx.sampleRate * 2;
  var buf = audioCtx.createBuffer(1, bufSize, audioCtx.sampleRate);
  var data = buf.getChannelData(0);
  for (var i = 0; i < bufSize; i++) data[i] = Math.random() * 2 - 1;
  scratchNoise = audioCtx.createBufferSource();
  scratchNoise.buffer = buf;
  scratchNoise.loop = true;
  scratchFilter = audioCtx.createBiquadFilter();
  scratchFilter.type = 'bandpass';
  scratchFilter.frequency.value = 800;
  scratchFilter.Q.value = 0.5;
  scratchGain = audioCtx.createGain();
  scratchGain.gain.value = 0;
  scratchNoise.connect(scratchFilter);
  scratchFilter.connect(scratchGain);
  scratchGain.connect(audioCtx.destination);
  scratchNoise.start();
  var ctx = audioCtx;
  try {
    ctx.addEventListener('statechange', function() { mediaLog('ac:' + ctx.state); });
  } catch (e) {}
  mediaLog('ac:created', audioCtx.state);
}

function startScratch(speed) {
  if (!audioCtx) initScratchSound();
  // iOS PWA: context may be suspended/interrupted after backgrounding
  if (audioCtx && audioCtx.state !== 'running') {
    try { audioCtx.resume(); } catch(e) {}
  }
  if (!scratchGain || !scratchFilter) return;
  var vol = Math.min(Math.abs(speed) * 0.15, 0.35);
  scratchFilter.frequency.value = 600 + Math.abs(speed) * 200;
  scratchGain.gain.setTargetAtTime(vol, audioCtx.currentTime, 0.02);
  isScratchPlaying = true;
}

// Pre-init AudioContext on first user gesture so scratch sound works offline
// (iOS PWA requires a gesture to unlock audio; touchmove alone is not enough)
(function() {
  function unlock() {
    if (_scratchOff) return;
    if (!audioCtx) initScratchSound();
    acRevive('touch');
  }
  document.addEventListener('touchstart', unlock, true);
  document.addEventListener('mousedown', unlock, true);
})();

function stopScratch() {
  if (!audioCtx || !isScratchPlaying) return;
  scratchGain.gain.setTargetAtTime(0, audioCtx.currentTime, 0.05);
  isScratchPlaying = false;
}

// Vinyl rotation state (JS-controlled)
var vinylAngle = 0;
var vinylSpeed = 0; // deg per frame, ~33rpm = 198deg/s = 3.3deg/frame@60fps
var TARGET_SPEED = 3.3;
var vinylRec = document.getElementById('vinylRecord');
var tonearmEl = document.getElementById('tonearm');

// Tonearm range: from outer edge (START_DEG) to inner label (END_DEG)
var ARM_REST = 53;
var ARM_START = 83;
var ARM_END = 105;
var currentArmAngle = ARM_REST;

// Vinyl drag-to-seek state
var isDragging = false;
var dragStartAngle = 0;
var dragStartTime = 0;
var dragVelocity = 0;
var lastDragDelta = 0;
var lastDragTime = 0;
var inertiaActive = false;

audio.volume = 0.8;

// ── Animation loop ──
var lastTime = 0;
var _lastBarPct = -1, _lastTimeText = '';

// Окно свёрнуто, вкладка ушла в фон или экран заблокирован — анимации
// замирают. Просто потеря фокуса не в счёт: окно видно, и застывшая на глазах
// картинка выглядит поломкой, а стоит она недорого.
//
// Ровно это и различает document.hidden: свёрнутое окно, чужая вкладка и
// локскрин дают true, а «видно, но не в фокусе» — false. Отдельно проверять
// hasFocus не нужно и вредно.
//
// Звук от этого не зависит вовсе: рисование к воспроизведению отношения не
// имеет, а на батарее сказывается сильнее всего.
var _uiActive = true;

function syncUiActive() {
  var was = _uiActive;
  // Потеря фокуса гасит анимации только если это включено в настройках:
  // окно ведь видно, и застывшая картинка выглядит поломкой.
  _uiActive = !document.hidden && (!perfCfg.pauseBlur || document.hasFocus());
  if (_uiActive && !was) {
    // Вернулись — стрелка и полоса могли уехать далеко, пока мы стояли.
    _armAtRest = false;
    _lastBarPct = -1; _lastTimeText = '';
  }
  document.documentElement.classList.toggle('ui-idle', !_uiActive);
}

document.addEventListener('visibilitychange', syncUiActive);

function animationLoop(ts) {
  var dt = lastTime ? (ts - lastTime) / 1000 : 0;
  lastTime = ts;

  // Цикл крутился 60 раз в секунду всегда — и на паузе тоже, переписывая в DOM
  // те же самые значения: пластинка стоит, стрелка на месте, время не идёт, а
  // кадры считаются. Это была самая большая постоянная нагрузка на процессор.
  // Вид не меняется — пропускаются только кадры, в которых нечего менять.
  if (!_uiActive) { requestAnimationFrame(animationLoop); return; }

  // Цель тонарма считаем ДО решения о простое: она зависит от позиции в треке,
  // и если решать раньше, стрелка замирала бы, не доехав до места.
  var targetArm = ARM_REST;
  if (isPlaying && (!audio.duration || isNaN(audio.duration))) {
    targetArm = ARM_START;
  } else if (audio.duration && !isNaN(audio.duration) && (isPlaying || audio.currentTime > 0)) {
    targetArm = ARM_START + (ARM_END - ARM_START) * (audio.currentTime / audio.duration);
  }
  _armAtRest = Math.abs(targetArm - currentArmAngle) < 0.05;

  var moving = isPlaying || isDragging || inertiaActive || Math.abs(vinylSpeed) > 0.01;
  if (!moving && _armAtRest) { requestAnimationFrame(animationLoop); return; }

  if (isPlaying && !isDragging) {
    // Smooth spin-up
    vinylSpeed += (TARGET_SPEED - vinylSpeed) * 0.05;
    vinylAngle += vinylSpeed;
  } else if (!isDragging) {
    // Slow down
    vinylSpeed *= 0.95;
    if (Math.abs(vinylSpeed) > 0.01) vinylAngle += vinylSpeed;
  }

  vinylRec.style.transform = 'rotate(' + (vinylAngle % 360) + 'deg)';

  if (!isDragging) {
    currentArmAngle += (targetArm - currentArmAngle) * 0.12;
    tonearmEl.style.transform = 'rotate(' + currentArmAngle + 'deg)';
  }

  // Cassette reel + tape spool animation
  if (_playerMode === 'cassette') {
    var reelL = document.getElementById('cassetteReelL');
    var reelR = document.getElementById('cassetteReelR');
    var spoolL = document.getElementById('cassetteSpoolL');
    var spoolR = document.getElementById('cassetteSpoolR');
    var cWin = reelL ? reelL.parentElement : null;
    if (reelL && reelR && cWin) {
      var tPct = (audio.duration && !isNaN(audio.duration)) ? audio.currentTime / audio.duration : 0;
      // Use window HEIGHT for circle sizing (prevents oval)
      var winH = cWin.offsetHeight;
      var reelPx = winH * 0.6;
      var minSpool = reelPx;
      var maxSpool = winH * 0.92;
      // Reel speed: supply slows, takeup speeds up
      var reelSpeedL = isPlaying ? TARGET_SPEED * (1.2 - tPct * 0.6) : 0;
      var reelSpeedR = isPlaying ? TARGET_SPEED * (0.6 + tPct * 0.6) : 0;
      if (!reelL._angle) reelL._angle = 0;
      if (!reelR._angle) reelR._angle = 0;
      reelL._angle += reelSpeedL;
      reelR._angle += reelSpeedR;
      // Set reel size in px — guaranteed circles
      reelL.style.width = reelPx + 'px'; reelL.style.height = reelPx + 'px';
      reelR.style.width = reelPx + 'px'; reelR.style.height = reelPx + 'px';
      reelL.style.transform = 'translate(-50%,-50%) rotate(' + (reelL._angle % 360) + 'deg)';
      reelR.style.transform = 'translate(-50%,-50%) rotate(' + (reelR._angle % 360) + 'deg)';
      // Tape spool: left full→empty, right empty→full
      if (spoolL && spoolR) {
        var sL = minSpool + (maxSpool - minSpool) * (1 - tPct);
        var sR = minSpool + (maxSpool - minSpool) * tPct;
        spoolL.style.width = sL + 'px'; spoolL.style.height = sL + 'px';
        spoolR.style.width = sR + 'px'; spoolR.style.height = sR + 'px';
      }
    }
  }

  // Progress bar & time
  // Полоса и время меняются раз в секунду, а не 60: сравниваем с уже
  // нарисованным и трогаем DOM, только когда значение действительно другое.
  // Запись textContent тянет за собой пересчёт разметки, и делать её впустую
  // шестьдесят раз в секунду дороже всего остального в этом цикле.
  if (audio.duration && !isDragging) {
    var pctBar = audio.currentTime / audio.duration * 100;
    var timeText = formatTime(audio.currentTime);
    if (Math.abs(pctBar - _lastBarPct) > 0.05) {
      _lastBarPct = pctBar;
      document.getElementById('progressFill').style.width = pctBar + '%';
      if (_playerMode === 'ipod') document.getElementById('ipodProgress').style.width = pctBar + '%';
    }
    if (timeText !== _lastTimeText) {
      _lastTimeText = timeText;
      document.getElementById('timeCurrent').textContent = timeText;
      if (_playerMode === 'ipod') {
        document.getElementById('ipodTimeCur').textContent = timeText;
        document.getElementById('ipodTimeDur').textContent = formatTime(audio.duration);
      }
    }
  }

  requestAnimationFrame(animationLoop);
}
requestAnimationFrame(animationLoop);

var _trackSrcGen = 0; // incremented on each src change to detect stale ended events

function bindAudioEvents() {
  audio.addEventListener('loadedmetadata', function() {
    document.getElementById('timeDuration').textContent = formatTime(audio.duration);
  });
  audio.addEventListener('ended', function() {
    if (isDragging || inertiaActive) return;
    var dur = audio.duration;
    if (!isFinite(dur) || dur <= 0) return;
    var gen = _trackSrcGen;
    setTimeout(function() {
      if (_trackSrcGen !== gen) return;
      nextTrack();
    }, 0);
  });
  audio.addEventListener('timeupdate', onTimeUpdate);
  // Fallback: iOS PWA may not fire 'ended' in background — detect near-end via timeupdate
  audio.addEventListener('timeupdate', function() {
    if (!isPlaying || isDragging || inertiaActive) return;
    var dur = audio.duration;
    var cur = audio.currentTime;
    if (!isFinite(dur) || dur <= 0) return;
    // If within last 0.3s AND audio is actually paused (iOS stopped it), advance
    if (cur >= dur - 0.3 && audio.paused && _trackSrcGen > 0) {
      var gen = _trackSrcGen;
      setTimeout(function() {
        if (_trackSrcGen !== gen) return;
        nextTrack();
      }, 350);
    }
  });
}
bindAudioEvents();

// ── Vinyl drag to seek ──
function getAngleFromCenter(el, clientX, clientY) {
  var rect = el.getBoundingClientRect();
  var cx = rect.left + rect.width / 2;
  var cy = rect.top + rect.height / 2;
  return Math.atan2(clientY - cy, clientX - cx) * 180 / Math.PI;
}

vinylRec.addEventListener('mousedown', function(e) {
  if (!audio.duration) return;
  e.preventDefault();
  isDragging = true;
  inertiaActive = false;
  dragVelocity = 0;
  lastDragTime = performance.now();
  vinylRec.classList.add('grabbing');
  dragStartAngle = getAngleFromCenter(vinylRec, e.clientX, e.clientY);
  dragStartTime = audio.currentTime;
  vinylSpeed = 0;
});

document.addEventListener('mousemove', function(e) {
  if (!isDragging) return;
  var angle = getAngleFromCenter(vinylRec, e.clientX, e.clientY);
  var delta = angle - dragStartAngle;
  if (delta > 180) delta -= 360;
  if (delta < -180) delta += 360;

  vinylAngle += delta;
  dragStartAngle = angle;

  // Track velocity for inertia
  var now = performance.now();
  var dt = now - lastDragTime;
  if (dt > 0) dragVelocity = delta / dt * 16; // deg per frame
  lastDragDelta = delta;
  lastDragTime = now;

  var secPerRevolution = 60 / 33;
  var timeDelta = (delta / 360) * secPerRevolution;
  var newTime = Math.max(0, Math.min(audio.currentTime + timeDelta, audio.duration - 0.5));
  audio.currentTime = newTime;

  var pct = newTime / audio.duration;
  currentArmAngle = ARM_START + (ARM_END - ARM_START) * pct;
  tonearmEl.style.transform = 'rotate(' + currentArmAngle + 'deg)';
  document.getElementById('progressFill').style.width = (pct * 100) + '%';
  document.getElementById('timeCurrent').textContent = formatTime(newTime);

  startScratch(delta);
});

document.addEventListener('mouseup', function() {
  if (!isDragging) return;
  isDragging = false;
  vinylRec.classList.remove('grabbing');
  // Apply inertia if velocity is significant
  if (Math.abs(dragVelocity) > 0.3 && audio.duration) {
    inertiaActive = true;
    applyInertia();
  } else {
    stopScratch();
  }
});

// Touch support for vinyl drag
vinylRec.addEventListener('touchstart', function(e) {
  if (!audio.duration || e.touches.length !== 1) return;
  e.preventDefault();
  isDragging = true;
  inertiaActive = false;
  dragVelocity = 0;
  lastDragTime = performance.now();
  var t = e.touches[0];
  dragStartAngle = getAngleFromCenter(vinylRec, t.clientX, t.clientY);
  dragStartTime = audio.currentTime;
  vinylSpeed = 0;
}, {passive: false});

document.addEventListener('touchmove', function(e) {
  if (!isDragging || e.touches.length !== 1) return;
  var t = e.touches[0];
  var angle = getAngleFromCenter(vinylRec, t.clientX, t.clientY);
  var delta = angle - dragStartAngle;
  if (delta > 180) delta -= 360;
  if (delta < -180) delta += 360;
  vinylAngle += delta;
  dragStartAngle = angle;
  var now = performance.now();
  var dt = now - lastDragTime;
  if (dt > 0) dragVelocity = delta / dt * 16;
  lastDragDelta = delta;
  lastDragTime = now;
  var secPerRevolution = 60 / 33;
  var timeDelta = (delta / 360) * secPerRevolution;
  var newTime = Math.max(0, Math.min(audio.currentTime + timeDelta, audio.duration - 0.5));
  audio.currentTime = newTime;
  var pct = newTime / audio.duration;
  currentArmAngle = ARM_START + (ARM_END - ARM_START) * pct;
  tonearmEl.style.transform = 'rotate(' + currentArmAngle + 'deg)';
  document.getElementById('progressFill').style.width = (pct * 100) + '%';
  document.getElementById('timeCurrent').textContent = formatTime(newTime);
  startScratch(delta);
}, {passive: false});

document.addEventListener('touchend', function() {
  if (!isDragging) return;
  isDragging = false;
  if (Math.abs(dragVelocity) > 0.3 && audio.duration) {
    inertiaActive = true;
    applyInertia();
  } else {
    stopScratch();
  }
});

function applyInertia() {
  if (!inertiaActive || isDragging) { inertiaActive = false; stopScratch(); return; }
  dragVelocity *= 0.92; // friction
  if (Math.abs(dragVelocity) < 0.1) { inertiaActive = false; stopScratch(); return; }

  vinylAngle += dragVelocity;

  var secPerRevolution = 60 / 33;
  var timeDelta = (dragVelocity / 360) * secPerRevolution;
  var newTime = audio.currentTime + timeDelta;
  newTime = Math.max(0, Math.min(newTime, audio.duration - 0.5));
  audio.currentTime = newTime;

  var pct = newTime / audio.duration;
  currentArmAngle = ARM_START + (ARM_END - ARM_START) * pct;
  tonearmEl.style.transform = 'rotate(' + currentArmAngle + 'deg)';
  document.getElementById('progressFill').style.width = (pct * 100) + '%';
  document.getElementById('timeCurrent').textContent = formatTime(newTime);

  startScratch(dragVelocity);
  requestAnimationFrame(applyInertia);
}

// ── Cassette hub drag-to-seek ──
(function() {
  var hubDragging = false, hubStartAngle = 0, hubStartTime = 0;
  function hubAngle(el, cx, cy) {
    var r = el.getBoundingClientRect();
    return Math.atan2(cy - (r.top + r.height/2), cx - (r.left + r.width/2)) * 180 / Math.PI;
  }
  function hubDown(e) {
    if (!audio.duration || _playerMode !== 'cassette') return;
    e.preventDefault();
    hubDragging = true;
    var hub = e.currentTarget;
    hub.classList.add('grabbing');
    var cx = e.clientX || (e.touches && e.touches[0].clientX);
    var cy = e.clientY || (e.touches && e.touches[0].clientY);
    hubStartAngle = hubAngle(hub, cx, cy);
    hubStartTime = audio.currentTime;
  }
  function hubMove(e) {
    if (!hubDragging) return;
    var cx = e.clientX || (e.touches && e.touches[0].clientX);
    var cy = e.clientY || (e.touches && e.touches[0].clientY);
    var hub = document.getElementById('cassetteHubR');
    var angle = hubAngle(hub, cx, cy);
    var delta = angle - hubStartAngle;
    if (delta > 180) delta -= 360;
    if (delta < -180) delta += 360;
    hubStartAngle = angle;
    var secPerRev = 60 / 33;
    audio.currentTime = Math.max(0, Math.min(audio.duration, audio.currentTime + (delta / 360) * secPerRev));
  }
  function hubUp() {
    if (!hubDragging) return;
    hubDragging = false;
    document.getElementById('cassetteHubL').classList.remove('grabbing');
    document.getElementById('cassetteHubR').classList.remove('grabbing');
  }
  document.addEventListener('DOMContentLoaded', function() {
    ['cassetteHubL','cassetteHubR'].forEach(function(id) {
      var el = document.getElementById(id);
      if (!el) return;
      el.addEventListener('mousedown', hubDown);
      el.addEventListener('touchstart', hubDown, {passive:false});
    });
    document.addEventListener('mousemove', hubMove);
    document.addEventListener('touchmove', hubMove, {passive:false});
    document.addEventListener('mouseup', hubUp);
    document.addEventListener('touchend', hubUp);
  });
})();

var _ipodWheelMoved = false; // track if wheel was rotated during drag

// iPod click sound via Web Audio API
var _ipodClickCtx = null;
function ipodClick() {
  if (navigator.vibrate) navigator.vibrate(5);
  try {
    if (!_ipodClickCtx) _ipodClickCtx = new (window.AudioContext || window.webkitAudioContext)();
    var ctx = _ipodClickCtx;
    var t = ctx.currentTime;
    // Short percussive "tick" — like plastic tap
    var osc = ctx.createOscillator();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(800, t);
    osc.frequency.exponentialRampToValueAtTime(200, t + 0.008);
    var gain = ctx.createGain();
    gain.gain.setValueAtTime(0.045, t);
    gain.gain.exponentialRampToValueAtTime(0.001, t + 0.015);
    // Low-pass to soften
    var lp = ctx.createBiquadFilter();
    lp.type = 'lowpass';
    lp.frequency.value = 600;
    osc.connect(lp);
    lp.connect(gain);
    gain.connect(ctx.destination);
    osc.start(t);
    osc.stop(t + 0.02);
  } catch(e) {}
}

// ── iPod Click Wheel ──
(function() {
  var wheelDragging = false, wheelLastAngle = 0, wheelAccum = 0;
  var WHEEL_STEP = 30; // degrees per scroll step

  function wheelAngle(el, cx, cy) {
    var r = el.getBoundingClientRect();
    return Math.atan2(cy - (r.top + r.height/2), cx - (r.left + r.width/2)) * 180 / Math.PI;
  }

  function wheelDown(e) {
    if (_playerMode !== 'ipod') return;
    // Ignore if clicking center button or labels
    if (e.target.id === 'ipodCenter' || e.target.closest('.ipod-wheel-center')) return;
    e.preventDefault();
    wheelDragging = true;
    _ipodWheelMoved = false;
    wheelAccum = 0;
    var cx = e.clientX || (e.touches && e.touches[0].clientX);
    var cy = e.clientY || (e.touches && e.touches[0].clientY);
    wheelLastAngle = wheelAngle(document.getElementById('ipodWheel'), cx, cy);
  }

  function wheelMove(e) {
    if (!wheelDragging) return;
    e.preventDefault();
    var cx = e.clientX || (e.touches && e.touches[0].clientX);
    var cy = e.clientY || (e.touches && e.touches[0].clientY);
    var wheel = document.getElementById('ipodWheel');
    var angle = wheelAngle(wheel, cx, cy);
    var delta = angle - wheelLastAngle;
    if (delta > 180) delta -= 360;
    if (delta < -180) delta += 360;
    wheelLastAngle = angle;
    wheelAccum += delta;
    if (Math.abs(delta) > 2) _ipodWheelMoved = true;

    if (_ipodListMode) {
      // Scroll track list
      while (wheelAccum > WHEEL_STEP) { wheelAccum -= WHEEL_STEP; _ipodScrollList(1); ipodClick(); }
      while (wheelAccum < -WHEEL_STEP) { wheelAccum += WHEEL_STEP; _ipodScrollList(-1); ipodClick(); }
    } else {
      // Seek in Now Playing
      if (audio.duration && !isNaN(audio.duration)) {
        var seekDelta = (delta / 360) * 15;
        audio.currentTime = Math.max(0, Math.min(audio.duration, audio.currentTime + seekDelta));
      }
      while (wheelAccum > WHEEL_STEP) { wheelAccum -= WHEEL_STEP; ipodClick(); }
      while (wheelAccum < -WHEEL_STEP) { wheelAccum += WHEEL_STEP; ipodClick(); }
    }
  }

  function wheelUp() { wheelDragging = false; }

  function _ipodScrollList(dir) {
    _ipodSelectedIdx = Math.max(0, Math.min(tracks.length - 1, _ipodSelectedIdx + dir));
    var maxVisible = 9;
    if (_ipodSelectedIdx < _ipodListOffset) _ipodListOffset = _ipodSelectedIdx;
    if (_ipodSelectedIdx >= _ipodListOffset + maxVisible) _ipodListOffset = _ipodSelectedIdx - maxVisible + 1;
    _ipodRenderList();
  }

  // Bind immediately (script is inline at bottom of body, DOM is ready)
  var wheel = document.getElementById('ipodWheel');
  var center = document.getElementById('ipodCenter');
  if (wheel) {
    wheel.addEventListener('mousedown', wheelDown);
    wheel.addEventListener('touchstart', wheelDown, {passive:false});
    document.addEventListener('mousemove', wheelMove);
    document.addEventListener('touchmove', wheelMove, {passive:false});
    document.addEventListener('mouseup', wheelUp);
    document.addEventListener('touchend', wheelUp);

    // Track touch position for quadrant tap detection
    var _wheelTouchStart = null;

    wheel.addEventListener('touchstart', function(e) {
      var t = e.touches[0];
      _wheelTouchStart = {x: t.clientX, y: t.clientY};
    }, {passive: true});

    wheel.addEventListener('touchend', function(e) {
      if (_playerMode !== 'ipod') return;
      if (_ipodWheelMoved) { _ipodWheelMoved = false; return; }
      if (!_wheelTouchStart) return;
      var r = wheel.getBoundingClientRect();
      var x = (_wheelTouchStart.x - r.left) / r.width - 0.5;
      var y = (_wheelTouchStart.y - r.top) / r.height - 0.5;
      _wheelTouchStart = null;
      var dist = Math.sqrt(x*x + y*y);
      if (dist < 0.18) {
        // Center tap
        ipodClick(); _ipodPlayOrToggle();
        return;
      }
      if (dist > 0.5) return;
      // Quadrant tap
      ipodClick();
      if (Math.abs(x) > Math.abs(y)) {
        if (x > 0) nextTrack(); else prevTrack();
      } else {
        if (y < 0) {
          if (_ipodListMode) _ipodShowNp(); else _ipodShowList();
        } else { ipodClick(); _ipodPlayOrToggle(); }
      }
    });

    // Mouse click — desktop only
    wheel.addEventListener('click', function(e) {
      if (_playerMode !== 'ipod') return;
      if (_ipodWheelMoved) { _ipodWheelMoved = false; return; }
      if (e.target.closest('.ipod-wheel-center')) return;
      var r = wheel.getBoundingClientRect();
      var x = (e.clientX - r.left) / r.width - 0.5;
      var y = (e.clientY - r.top) / r.height - 0.5;
      var dist = Math.sqrt(x*x + y*y);
      if (dist < 0.2) return;
      ipodClick();
      if (Math.abs(x) > Math.abs(y)) {
        if (x > 0) nextTrack(); else prevTrack();
      } else {
        if (y < 0) {
          if (_ipodListMode) _ipodShowNp(); else _ipodShowList();
        } else { _ipodPlayOrToggle(); }
      }
    });
  }

  if (center) {
    center.addEventListener('click', function() {
      if (_playerMode !== 'ipod') return;
      if (_ipodWheelMoved) { _ipodWheelMoved = false; return; }
      ipodClick(); _ipodPlayOrToggle();
    });
  }
})();

function formatTime(s) {
  if (!isFinite(s) || s < 0) return '0:00';
  var m = Math.floor(s / 60);
  var sec = Math.floor(s % 60);
  return m + ':' + (sec < 10 ? '0' : '') + sec;
}

// Lossless formats get a unique, per-format badge next to the track name.
// The server resolves the real codec (so .m4a shows ALAC only when it truly is
// lossless, not for AAC). Extension fallback is for legacy cached data without
// t.fmt — m4a is intentionally excluded there since the extension is ambiguous.
var _LOSSLESS_BADGE = {flac:'FLAC', alac:'ALAC', wav:'WAV', aiff:'AIFF', aif:'AIFF'};
function fmtBadge(t) {
  if (t && typeof t.fmt === 'string') return t.fmt;  // '' for lossy
  var file = (t && t.file) || '';
  var dot = file.lastIndexOf('.');
  if (dot < 0) return '';
  return _LOSSLESS_BADGE[file.slice(dot + 1).toLowerCase()] || '';
}
function fmtBadgeHtml(t) {
  var b = fmtBadge(t);
  return b ? '<span class="fmt-badge fmt-' + b.toLowerCase() + '">' + b + '</span>' : '';
}

function renderTracks() {
  var html = '';
  var indices = getVisibleIndices();
  for (var ii = 0; ii < indices.length; ii++) {
    var i = indices[ii];
    var t = tracks[i];
    var coverHtml = t.has_cover
      ? (isTrackCached(t.file)
          ? '<img data-cfile="' + encodeURIComponent(t.file) + '" loading="lazy">'
          : '<img src="/api/cover/' + encodeURIComponent(t.file) + '" loading="lazy" onerror="loadCachedImg(this,\'' + encodeURIComponent(t.file).replace(/'/g,"\\'") + '\')">')
      : '';
    if (isEditMode) {
      html += '<div class="playlist-item' + (i === currentIdx ? ' active' : '') + '" data-idx="' + i + '"'
        + ' draggable="true" ondragstart="onDragStart(event,' + i + ')" ondragend="onDragEnd(event)"'
        + ' ondragover="onDragOver(event,' + i + ')" ondrop="onDrop(event,' + i + ')"'
        + ' ontouchstart="onTouchDragStart(event,' + i + ')">'
        + '<span class="drag-handle">&#9776;</span>'
        + '<div class="cover-thumb">' + coverHtml + '</div>'
        + '<div class="info"><div class="name-row"><span class="name">' + esc(t.title) + '</span>' + fmtBadgeHtml(t) + '</div>'
        + '<div class="artist">' + esc(t.artist) + '</div></div></div>';
    } else if (selectionMode) {
      var selOn = !!selectedFiles[t.file];
      html += '<div class="playlist-item' + (i === currentIdx ? ' active' : '') + (selOn ? ' selected' : '') + '"'
        + ' onclick="toggleSelect(' + i + ')"'
        + ' oncontextmenu="event.preventDefault();showCtxMenu(event,' + i + ')"'
        + ' data-longpress="' + i + '">'
        + '<div class="sel-circle' + (selOn ? ' on' : '') + '"></div>'
        + '<div class="cover-thumb">' + coverHtml + '</div>'
        + '<div class="info"><div class="name-row"><span class="name">' + esc(t.title) + '</span>' + fmtBadgeHtml(t) + '</div>'
        + '<div class="artist">' + esc(t.artist) + '</div></div></div>';
    } else {
      var offDisabled = _isOffline && !isTrackCached(t.file);
      var queuedNext = (t.file === _forceNextFile);
      html += '<div class="playlist-item' + (i === currentIdx ? ' active' : '') + (offDisabled ? ' disabled' : '')
        + (queuedNext ? ' queued-next' : '') + '"'
        + (offDisabled ? '' : ' onclick="playFromList(' + i + ')"')
        + (offDisabled ? ' style="opacity:0.3;pointer-events:none"' : '')
        + ' oncontextmenu="event.preventDefault();showCtxMenu(event,' + i + ')"'
        + ' data-longpress="' + i + '">'
        + '<div class="cover-thumb">' + coverHtml + '</div>'
        + '<div class="info"><div class="name-row"><span class="name">' + esc(t.title) + '</span>' + fmtBadgeHtml(t)
        + (queuedNext ? '<span class="next-badge" data-tip="Играет следующим">следующий</span>' : '') + '</div>'
        + '<div class="artist">' + esc(t.artist) + '</div></div>'
        + (isTrackCached(t.file)
          ? '<span style="width:6px;height:6px;border-radius:50%;background:#52b788;flex-shrink:0" data-tip="В кэше"></span>'
          : (!offDisabled ? '<button class="track-edit-btn" onclick="event.stopPropagation();cacheTrack(\'' + esc(t.file).replace(/'/g,"\\'") + '\',function(ok){if(ok)renderTracks()})" data-tip="Кэшировать"><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg></button>' : ''))
        + (!offDisabled && userRole !== 'demo' ? '<button class="track-edit-btn" onclick="event.stopPropagation();openTrackEdit(' + i + ')" data-tip="Сведения о треке"><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></button>' : '')
        + '</div>';
    }
  }
  document.getElementById('trackList').innerHTML = html;
  hydrateCoverThumbs(document.getElementById('trackList'));
}

function hydrateCoverThumbs(root) {
  if (!root) return;
  var imgs = root.querySelectorAll('img[data-cfile]');
  for (var k = 0; k < imgs.length; k++) (function(img) {
    var file = decodeURIComponent(img.getAttribute('data-cfile'));
    img.removeAttribute('data-cfile');
    getCachedCover(file, function(buf) {
      if (buf) {
        img.src = URL.createObjectURL(new Blob([buf]));
      } else if (!_isOffline) {
        img.src = '/api/cover/' + encodeURIComponent(file);
        cacheCover(file);
      } else {
        img.style.visibility = 'hidden';
      }
    });
  })(imgs[k]);
}

function renderAlbums() {
  var html = '';
  var indices = filteredAlbums;
  if (!indices) {
    indices = [];
    for (var j = 0; j < albums.length; j++) indices.push(j);
  }
  for (var ai = 0; ai < indices.length; ai++) {
    var a = indices[ai];
    var alb = albums[a];
    var coverHtml = alb.cover_file
      ? (isTrackCached(alb.cover_file)
          ? '<img data-cfile="' + encodeURIComponent(alb.cover_file) + '" loading="lazy">'
          : '<img src="/api/cover/' + encodeURIComponent(alb.cover_file) + '" loading="lazy" onerror="loadCachedImg(this,\'' + encodeURIComponent(alb.cover_file).replace(/'/g,"\\'") + '\')">')
      : '';
    var isExp = expandedAlbum === a;
    // Check if all album tracks are cached
    var albCached = 0;
    for (var ci = 0; ci < alb.tracks.length; ci++) {
      if (isTrackCached(tracks[alb.tracks[ci]].file)) albCached++;
    }
    var allCached = albCached === alb.tracks.length;
    var cacheIcon = allCached
      ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>'
      : '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>';
    html += '<div class="album-card' + (isExp ? ' active' : '') + '" onclick="toggleAlbum(' + a + ')">'
      + '<div class="album-cover">' + coverHtml + '</div>'
      + '<div class="album-info"><div class="album-name">' + esc(alb.name) + '</div>'
      + '<div class="album-artist">' + esc(alb.artist) + '</div>'
      + '<div class="album-count">' + alb.tracks.length + ' треков'
      + (albCached > 0 ? ' <span style="color:rgba(255,255,255,0.3)">(' + albCached + ' в кэше)</span>' : '')
      + '</div>'
      + '</div>'
      + '<button class="shuffle-btn" style="margin-left:auto;flex-shrink:0;opacity:0.5" onclick="event.stopPropagation();cacheAlbum(' + a + ')" data-tip="' + (allCached ? 'Альбом в кэше' : 'Кэшировать альбом') + '">' + cacheIcon + '</button>'
      + '</div>';
    html += '<div class="album-tracks' + (isExp ? ' open' : '') + '" id="albumTracks_' + a + '">';
    for (var ti = 0; ti < alb.tracks.length; ti++) {
      var idx = alb.tracks[ti];
      var t = tracks[idx];
      var cachedDot = isTrackCached(t.file) ? '<span style="width:6px;height:6px;border-radius:50%;background:#52b788;flex-shrink:0;margin-left:auto" data-tip="В кэше"></span>' : '';
      html += '<div class="playlist-item' + (idx === currentIdx ? ' active' : '') + '" onclick="event.stopPropagation();playFromAlbum(' + a + ',' + idx + ')" style="padding-left:36px">'
        + '<div class="info"><div class="name">' + esc(t.title) + '</div></div>' + cachedDot + '</div>';
    }
    html += '</div>';
  }
  document.getElementById('albumList').innerHTML = html;
  hydrateCoverThumbs(document.getElementById('albumList'));
}

function cacheAlbum(albumIdx) {
  var alb = albums[albumIdx];
  if (!alb) return;
  var files = [];
  for (var i = 0; i < alb.tracks.length; i++) {
    var f = tracks[alb.tracks[i]].file;
    if (!isTrackCached(f)) files.push(f);
  }
  if (!files.length) { showToast('Альбом уже в кэше'); return; }
  beginCaching(files);
}

function toggleAlbum(i) {
  var wasOpen = expandedAlbum === i;
  // Close previous
  if (expandedAlbum !== null && expandedAlbum !== i) {
    var prev = document.getElementById('albumTracks_' + expandedAlbum);
    if (prev) prev.classList.remove('open');
    var prevCard = prev ? prev.previousElementSibling : null;
    if (prevCard) prevCard.classList.remove('active');
  }
  expandedAlbum = wasOpen ? null : i;
  var el = document.getElementById('albumTracks_' + i);
  var card = el ? el.previousElementSibling : null;
  if (el) {
    if (wasOpen) {
      el.classList.remove('open');
      if (card) card.classList.remove('active');
    } else {
      el.classList.add('open');
      if (card) card.classList.add('active');
    }
  }
}

// Кнопка «Скачать ZIP-архив» относится к каталогу треков, а не к строке
// заголовка вообще: на DROPS, альбомах и плейлистах скачивать нечего, а она
// там показывалась. Условие живёт в одном месте — её дёргают из showTab,
// applyConfig и выхода из режима выделения.
function syncDownloadBtn() {
  var el = document.getElementById('downloadCatalogBtn');
  if (!el) return;
  el.style.display = (isAdmin && !_isOffline && activeTab === 'tracks' && !selectionMode) ? '' : 'none';
}

function showTab(tab) {
  activeTab = tab;
  var tabs = ['tracks', 'albums', 'playlists', 'new'];
  var panels = {tracks: 'trackList', albums: 'albumList', playlists: 'playlistsList', new: 'newList'};
  for (var i = 0; i < tabs.length; i++) {
    var btn = document.getElementById('tab' + tabs[i].charAt(0).toUpperCase() + tabs[i].slice(1));
    if (btn) btn.className = 'playlist-tab' + (tabs[i] === tab ? ' active' : '');
    var panel = document.getElementById(panels[tabs[i]]);
    if (panel) {
      panel.className = panel.className.replace(/tab-panel-\w+/g, '').trim() + (tabs[i] === tab ? ' tab-panel-visible' : ' tab-panel-hidden');
    }
  }
  // Show cache buttons only on tracks tab
  var showCache = tab === 'tracks';
  document.getElementById('cacheBtn').style.display = showCache ? '' : 'none';
  document.getElementById('cachedOnlyBtn').style.display = showCache ? '' : 'none';
  syncDownloadBtn();                       // ZIP каталога — тоже только там
  // «Перемешать» относится к списку треков: на альбомах, плейлистах и DROPS
  // перемешивать нечего, а кнопка там висела.
  document.getElementById('shuffleListBtn').style.display = showCache ? '' : 'none';
  document.getElementById('relSubTabs').style.display = (tab === 'new') ? 'flex' : 'none';
  // Уход со вкладки отрывок не обрывает: он ведёт плеер, и музыка не должна
  // замолкать оттого, что пошли смотреть треки. Останавливает его только явный
  // выбор другой музыки.
  if (tab === 'albums') {
    document.getElementById('playlistHeader').textContent = (filteredAlbums ? filteredAlbums.length + ' / ' : '') + albums.length + ' альбомов';
    document.getElementById('editBtn').style.display = 'none';
    if (isEditMode) cancelEdit();
  } else if (tab === 'new') {
    document.getElementById('editBtn').style.display = 'none';
    if (isEditMode) cancelEdit();
    syncRelHeader();
    if (!relF().data) loadReleases();
    else renderReleases();
  } else if (tab === 'playlists') {
    // Set the count from what's already loaded: loadUserPlaylists() refreshes
    // from the server asynchronously, so leaving the header to it kept the
    // previous tab's text («xxx альбомов») on screen until the reply arrived —
    // and forever if it never did.
    document.getElementById('playlistHeader').textContent = plHeaderText();
    document.getElementById('editBtn').style.display = 'none';
    if (isEditMode) cancelEdit();
    loadUserPlaylists();
  } else {
    document.getElementById('playlistHeader').textContent = (filteredTracks ? filteredTracks.length + ' / ' : '') + tracks.length + ' треков';
    document.getElementById('editBtn').style.display = (isNumberedCatalog && userRole !== 'demo') ? '' : 'none';
  }
}

// ── Blob URL pre-cache (keeps ready-to-use blob URLs in memory for instant playback) ──
// Tiny silent WAV used as bridge src to keep iOS audio session alive during async IDB load
var _silentBlobUrl = (function() {
  var h = new Uint8Array([
    0x52,0x49,0x46,0x46, 0x25,0x00,0x00,0x00, 0x57,0x41,0x56,0x45,
    0x66,0x6d,0x74,0x20, 0x10,0x00,0x00,0x00, 0x01,0x00,0x01,0x00,
    0x44,0xac,0x00,0x00, 0x44,0xac,0x00,0x00, 0x01,0x00,0x08,0x00,
    0x64,0x61,0x74,0x61, 0x01,0x00,0x00,0x00, 0x80
  ]);
  return URL.createObjectURL(new Blob([h], {type:'audio/wav'}));
})();
var _blobUrlCache = {}; // file -> blob URL

function makeBlobUrl(buf, file) {
  var ext = file.split('.').pop().toLowerCase();
  var mimeMap = {mp3:'audio/mpeg',flac:'audio/flac',m4a:'audio/mp4',ogg:'audio/ogg',wav:'audio/wav',aac:'audio/aac',opus:'audio/ogg',aiff:'audio/aiff',aif:'audio/aiff',alac:'audio/mp4'};
  return URL.createObjectURL(new Blob([buf], {type: mimeMap[ext] || 'audio/mpeg'}));
}

function prepareBlobUrl(file) {
  if (_blobUrlCache[file] || !isTrackCached(file)) return;
  getCachedAudio(file, function(buf) {
    if (!buf) { delete cachedFiles[cacheKey(file)]; return; }
    _blobUrlCache[file] = makeBlobUrl(buf, file);
  });
}

function prepareNearbyBlobs() {
  if (playQueue.length === 0) return;
  var startPos = playQueuePos >= 0 ? playQueuePos : 0;
  // Evict distant blob URLs to reduce iOS memory pressure
  var nearSet = {};
  // Трек, выбранный «играть следующим», обязан остаться готовым: иначе его
  // блоб вытеснялся как «далёкий», и переключение в фоне попадало в пустоту.
  if (_forceNextFile) nearSet[_forceNextFile] = true;
  for (var n = 0; n <= 3; n++) {
    var np = startPos + (n <= 2 ? n : -1);
    if (np < 0) np += playQueue.length;
    if (np >= playQueue.length) np -= playQueue.length;
    var nf = tracks[playQueue[np]] ? tracks[playQueue[np]].file : null;
    if (nf) nearSet[nf] = true;
  }
  Object.keys(_blobUrlCache).forEach(function(f) {
    if (!nearSet[f]) {
      try { URL.revokeObjectURL(_blobUrlCache[f]); } catch(e) {}
      delete _blobUrlCache[f];
    }
  });
  // Prepare current + 2 next + 1 prev
  for (var d = 0; d <= 3; d++) {
    var pos = startPos + (d <= 2 ? d : -1);
    if (pos < 0) pos += playQueue.length;
    if (pos >= playQueue.length) pos -= playQueue.length;
    var idx = playQueue[pos];
    if (idx >= 0 && idx < tracks.length) prepareBlobUrl(tracks[idx].file);
  }
  if (_forceNextFile) prepareBlobUrl(_forceNextFile);
}

function selectTrack(i, autoplay) {
  if (i < 0 || i >= tracks.length) return;
  exitPreviewPlayerUI();   // включили трек из библиотеки — интерфейс DROPS уходит
  // Безусловно, а не только внутри exitPreviewPlayerUI: тот выходит сразу, если
  // флаг уже сброшен, и панели остались бы спрятанными. Вызов идемпотентный.
  syncPreviewChrome();
  // Любая недоигранная рампа отменяется здесь: ручное переключение посреди
  // затухания иначе оставило бы новый трек тихим.
  fadeCancel(false);
  // Прогретый хвост оставляем: startCrossfade поднимает именно его. Гасим
  // только звучащий — то есть при ручном переключении.
  if (tailAudio && !tailAudio.paused) tailStop();
  // Рампу поднимаем прямо здесь, а не по событию play: оно может не прийти
  // вовсе (браузер отклонил запуск, трек не догрузился), и трек остался бы
  // беззвучным. Рампа считается по времени, поэтому к концу громкость будет
  // на месте в любом случае.
  if (fadeOn()) fadeRamp(0, _userVolume, FADE_MS);
  else { try { audio.volume = _userVolume; } catch (e) {} }
  currentIdx = i;
  _trackSrcGen++;
  resetPlayMeter();
  var t = tracks[i];

  vinylAngle = 0;
  vinylSpeed = 0;

  // When we're about to play the new track, don't pause first: an explicit
  // pause() in the background can make iOS release the audio session, so the
  // following play() (e.g. from a lock-screen / CarPlay next/prev) is deferred
  // until the app is foregrounded. Assigning a new src already stops the old
  // track. For auto-advance this is a no-op (the track has already ended), so
  // it doesn't affect that path.
  if (!autoplay) audio.pause();
  // Reset lock screen position immediately so iOS doesn't show stale time
  if ('mediaSession' in navigator) {
    try { navigator.mediaSession.setPositionState(); } catch(e) {}
  }
  var streamUrl = '/api/stream/' + encodeURIComponent(t.file);
  var genAtLoad = _trackSrcGen;
  _ctxRestored = true;   // an explicit choice replaces whatever was stored
  if (!_ctxRestoring) { _ctxPlayed = false; _pendingSeek = 0; }
  setTimeout(function() { savePlaybackContext(true); }, 0);

  function doPlay() {
    if (!autoplay) return;
    var p = ourAudioPlay();
    if (p && p.then) p.then(function() {
      if (!_pwaAudioChecked && window.navigator.standalone) {
        _pwaAudioChecked = true;
        setTimeout(function() {
          if (audio.currentTime < 0.01 && !audio.paused) {
            setPlayState(false);
            audio.pause();
            _pwaRecoverAudio();
          }
        }, 2000);
      }
    }).catch(function(err) {
      if (err && (err.name === 'AbortError' || /aborted/i.test(err.message || ''))) return;
      console.error('play() failed:', err);
      showToast('Ошибка воспроизведения: ' + err.message);
    });
    setPlayState(true);
  }

  function watchDuration(isBlob) {
    if (!isBlob) return;
    setTimeout(function() {
      if (genAtLoad !== _trackSrcGen) return;
      if (!isFinite(audio.duration) || audio.duration <= 0) {
        if (!_isOffline) {
          var curTime = audio.currentTime || 0;
          var wasPlaying = !audio.paused;
          try { URL.revokeObjectURL(_blobUrlCache[t.file]); } catch(e) {}
          delete _blobUrlCache[t.file];
          audio.addEventListener('loadedmetadata', function onceLm() {
            audio.removeEventListener('loadedmetadata', onceLm);
            try { audio.currentTime = curTime; } catch(e) {}
            if (wasPlaying) ourAudioPlay().catch(function(){});
          });
          setAudioSrc(streamUrl);
        }
      }
    }, 4000);
  }

  if (_blobUrlCache[t.file]) {
    setAudioSrc(_blobUrlCache[t.file]);
    doPlay();
    watchDuration(true);
  } else if (isTrackCached(t.file)) {
    // iOS requires play() synchronously within MediaSession/gesture callback.
    // Start with stream URL (or silent placeholder offline) to keep audio session,
    // then swap to blob when IDB read completes.
    setAudioSrc(_isOffline ? _silentBlobUrl : streamUrl);
    doPlay();
    // Skip the async blob swap when locked/backgrounded and streaming is
    // available: swapping src pauses playback and the re-play() runs outside the
    // MediaSession gesture, which iOS blocks on the lock screen / CarPlay — the
    // track would switch visually but stay silent until you reopen the app.
    // The blob is still warmed by prepareNearbyBlobs for the next switch.
    if (document.hidden && !_isOffline) {
      prepareBlobUrl(t.file);
    } else {
      getCachedAudio(t.file, function(buf) {
        if (genAtLoad !== _trackSrcGen) return;
        if (buf) {
          _blobUrlCache[t.file] = makeBlobUrl(buf, t.file);
          setAudioSrc(_blobUrlCache[t.file]);
          if (autoplay) ourAudioPlay().catch(function(){});
          watchDuration(true);
        } else {
          delete cachedFiles[cacheKey(t.file)];
          if (!_isOffline) {
            setAudioSrc(streamUrl);
            if (autoplay) ourAudioPlay().catch(function(){});
          }
        }
      });
    }
  } else {
    setAudioSrc(streamUrl);
    doPlay();
  }
  setTimeout(prepareNearbyBlobs, 200);
  var titleEl = document.getElementById('trackTitle');
  var artistEl = document.getElementById('trackArtist');
  // Fade out, swap text, fade in
  titleEl.style.opacity = '0';
  artistEl.style.opacity = '0';
  setTimeout(function() {
    titleEl.textContent = t.title;
    artistEl.textContent = t.artist;
    titleEl.style.opacity = '1';
    artistEl.style.opacity = '1';
    // Format badge next to the now-playing title (not shown on the OS lockscreen)
    var _pbe = document.getElementById('trackTitleBadge');
    if (_pbe) {
      var _pb = fmtBadge(t);
      if (_pb) {
        _pbe.textContent = _pb;
        _pbe.className = 'fmt-badge fmt-badge-player fmt-' + _pb.toLowerCase();
        _pbe.style.display = '';
      } else { _pbe.style.display = 'none'; }
    }
    // Cassette label + cover
    document.getElementById('cassetteTitle').textContent = t.title;
    document.getElementById('cassetteArtist').textContent = t.artist;
    var ccov = document.getElementById('cassetteCover');
    var ccph = document.getElementById('cassetteCoverPh');
    setCoverSrc(ccov, t.file, t.has_cover, ccph);
    // iPod sync
    _ipodSyncTrack(t);
    if (_playerMode === 'ipod' && _ipodListMode) {
      _ipodSelectedIdx = i;
      _ipodRenderList();
    }
  }, 150);

  var img = document.getElementById('vinylCover');
  if (t.has_cover) {
    var coverUrl = '/api/cover/' + encodeURIComponent(t.file);
    if (isTrackCached(t.file)) {
      getCachedCover(t.file, function(buf) {
        if (buf) {
          var blob = new Blob([buf]);
          if (img._coverUrl) URL.revokeObjectURL(img._coverUrl);
          img._coverUrl = URL.createObjectURL(blob);
          img.src = img._coverUrl;
        } else if (!_isOffline) {
          img.src = coverUrl;
          cacheCover(t.file);   // missing or just dropped as invalid — refill it
        } else {
          // Offline with no usable artwork: show the placeholder. Falling
          // through left the previous track's cover spinning on the record.
          img.style.display = 'none';
          document.getElementById('vinylPlaceholder').style.display = '';
          return;
        }
        img.style.display = '';
        document.getElementById('vinylPlaceholder').style.display = 'none';
        img.onload = function() { extractColor(img); };
      });
    } else if (!_isOffline) {
      img.src = coverUrl;
      img.style.display = '';
      document.getElementById('vinylPlaceholder').style.display = 'none';
      img.onload = function() { extractColor(img); };
    } else {
      img.style.display = 'none';
      document.getElementById('vinylPlaceholder').style.display = '';
    }
  } else {
    img.style.display = 'none';
    document.getElementById('vinylPlaceholder').style.display = '';
    randomBackground();
  }

  updateActiveHighlight();
  scrollToActive();
  updateMediaSession(t);
  autoMetaForTrack(t);
  showMobileControls();
  // Prefetch next track in queue
  setTimeout(prefetchNext, 500);
}

// Превью ведёт плеер, только пока у него действительно есть что играть.
// Одного флага мало: если элемент уже отпущен, кнопки обязаны вернуться
// основному плееру — иначе нажатие с локскрина уходит в пустоту.
function previewOwnsTransport() {
  return _previewMode && !!(previewAudio.currentSrc || previewAudio.src);
}

function togglePlay() {
  if (previewOwnsTransport()) {
    if (previewAudio.paused) {
      var pp = previewAudio.play();
      if (pp && pp.catch) pp.catch(function(){});
      setPlayState(true);
    } else {
      previewAudio.pause();
      setPlayState(false);
    }
    paintPreviewState();
    return;
  }
  if (currentIdx < 0 && tracks.length > 0) {
    playFromList(0);
    return;
  }
  if (isPlaying) {
    audio.pause();
    setPlayState(false);
  } else {
    ourAudioPlay();
    setPlayState(true);
  }
}

function setPlayState(playing) {
  isPlaying = playing;
  document.getElementById('playIcon').innerHTML = playing
    ? '<path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>'
    : '<path d="M8 5v14l11-7z"/>';
  // Sync mobile buttons
  var mb = document.getElementById('mobilePlayBtn');
  if (mb) mb.innerHTML = playing
    ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>'
    : '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
  widgetPublish();
}

function showMobileControls() {
  // Don't show on vinyl view — player has its own controls
  if (document.body.classList.contains('mobile-view-vinyl')) return;
  var pb = document.getElementById('mobilePlayBtn');
  var nb = document.getElementById('mobileNextBtn');
  if (pb) pb.classList.add('show');
  if (nb) nb.classList.add('show');
}

var isShuffled = false;

function buildDefaultQueue() {
  stopRadio();          // сменили каталог, поиск или сортировку — станция не о том
  playQueue = getVisibleIndices();
  playQueuePos = -1;
  if (isShuffled) shuffleArray(playQueue);
  setTimeout(prepareNearbyBlobs, 100);
}

function shuffleArray(arr) {
  for (var i = arr.length - 1; i > 0; i--) {
    var j = Math.floor(Math.random() * (i + 1));
    var tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
  }
  return arr;
}

function toggleShuffle() {
  isShuffled = !isShuffled;
  syncShuffleUI();
  if (isShuffled) {
    // Shuffle current queue, keep current track at position 0
    var cur = playQueue[playQueuePos];
    var rest = playQueue.filter(function(x) { return x !== cur; });
    shuffleArray(rest);
    playQueue = [cur].concat(rest);
    playQueuePos = 0;
  } else {
    // Restore order
    var cur = currentIdx;
    buildDefaultQueue();
    playQueuePos = playQueue.indexOf(cur);
    if (playQueuePos < 0) playQueuePos = 0;
  }
}

function getVisibleIndices() {
  var indices = filteredTracks;
  if (!indices) {
    indices = [];
    for (var j = 0; j < tracks.length; j++) indices.push(j);
  }
  if (showCachedOnly) {
    indices = indices.filter(function(idx) { return isTrackCached(tracks[idx].file); });
  }
  return indices;
}

function toggleShuffleFromList() {
  if (!isShuffled) {
    isShuffled = true;
    syncShuffleUI();
    var indices = getVisibleIndices();
    shuffleArray(indices);
    playQueue = indices;
    playQueuePos = 0;
    if (playQueue.length) selectTrack(playQueue[0], true);
  } else {
    toggleShuffle();
  }
}

function syncShuffleUI() {
  var b1 = document.getElementById('shuffleListBtn');
  var b2 = document.getElementById('shufflePlayerBtn');
  if (b1) b1.classList.toggle('active', isShuffled);
  if (b2) b2.classList.toggle('active', isShuffled);
}

function playFromList(trackIdx) {
  stopRadio();          // выбрали трек руками — станция больше не ведёт
  // Build queue from current visible list respecting cachedOnly filter
  var indices = getVisibleIndices();
  playQueue = indices.slice();
  playQueuePos = playQueue.indexOf(trackIdx);
  if (playQueuePos < 0) playQueuePos = 0;
  selectTrack(trackIdx, true);
}

function playFromAlbum(albumIdx, trackIdx) {
  stopRadio();
  // Build queue from album tracks
  var alb = albums[albumIdx];
  if (!alb) return;
  playQueue = alb.tracks.slice();
  playQueuePos = playQueue.indexOf(trackIdx);
  if (playQueuePos < 0) playQueuePos = 0;
  selectTrack(trackIdx, true);
}

// Re-sync playQueuePos to the track that is actually playing. The queue can be
// rebuilt in the background (buildDefaultQueue resets the position to -1), which
// would otherwise make auto-advance jump to the first track instead of the next.
function _syncQueuePos() {
  if (playQueuePos < 0 || playQueue[playQueuePos] !== currentIdx) {
    var p = playQueue.indexOf(currentIdx);
    if (p >= 0) playQueuePos = p;
  }
}

function prevTrack() {
  if (previewOwnsTransport()) { stepPreview(-1); return; }
  if (audio.currentTime > 3) { audio.currentTime = 0; return; }
  if (playQueue.length > 0) {
    _syncQueuePos();
    playQueuePos--;
    if (playQueuePos < 0) playQueuePos = playQueue.length - 1;
    selectTrack(playQueue[playQueuePos], isPlaying);
  }
}

function nextTrack() {
  if (previewOwnsTransport()) { stepPreview(1); return; }
  var forced = forcedNextIndex();
  if (forced >= 0) {
    _forceNextFile = null;
    // Move queue position to this track so next continues from there
    var qPos = playQueue.indexOf(forced);
    if (qPos >= 0) playQueuePos = qPos;
    selectTrack(forced, isPlaying);
    renderTracks();
    return;
  }
  if (playQueue.length > 0) {
    _syncQueuePos();
    playQueuePos++;
    // Радио досыпает заранее: добор ровно в момент окончания дал бы паузу
    // между треками на время отбора.
    if (radioOn && playQueuePos >= playQueue.length - 5) {
      var ahead = radioBatch();
      if (ahead.length) playQueue = playQueue.concat(ahead);
    }
    if (playQueuePos >= playQueue.length) {
      // Очередь кончилась — не заворачиваемся на первый трек каталога, а
      // продолжаем подбором. Раньше здесь был playQueuePos = 0, и после
      // альбома или плейлиста начиналась музыка «сначала списка».
      var more = radioOn ? radioBatch() : buildContinuation();
      if (more.length) playQueue = playQueue.concat(more);
      else playQueuePos = 0;
    }
    if (playQueuePos >= playQueue.length) playQueuePos = 0;
    selectTrack(playQueue[playQueuePos], isPlaying);
  }
}

// ── Радиостанция ──
// Бесконечный поток по критериям. Всё считается локально: в сеть радио не
// ходит ни разу, поэтому работает и офлайн (там пул сам сужается до кэша).
var radioOn = false;
var radioCfg = null;

function radioDefaults() {
  return {eras: [], genres: [], yearFrom: 0, yearTo: 0,
          activity: 'any',        // any | often | rare | never | forgotten
          lossless: false, cachedOnly: false, skipShort: true,
          fade: false,            // плавные переходы между треками
          groupByArtist: false,   // подряд несколько треков одного артиста
          variety: 'balanced'};   // familiar | balanced | surprise
}

// ── Плавные переходы между треками ──
//
// Только на десктопе и только через audio.volume. Web Audio дал бы затухание и
// на iOS, но createMediaElementSource — дорога в один конец: после неё звук
// элемента идёт исключительно через AudioContext, а усыплённый в фоне контекст
// означал бы тишину при работающем на вид плеере. Ради украшения ломать
// фоновое воспроизведение нельзя, поэтому на iOS опции просто нет — там же, где
// уже спрятан ползунок громкости, по той же причине.
var _volumeWorks = (function() {
  if (_isIOS) return false;
  try {
    var probe = document.createElement('audio');
    probe.volume = 0.5;
    return probe.volume === 0.5;   // на iOS остаётся 1
  } catch (e) { return false; }
})();

var FADE_MS = 4000;
var _userVolume = 0.8;        // куда возвращаться: положение ползунка
var _fadeTimer = null;
var _tailTimer = null;
var _fading = false;
var _tailArmed = '';          // источник, под который хвост уже прогрет

// Вспомогательный элемент для кроссфейда создаётся ЛЕНИВО и только на
// десктопе. Это не оптимизация, а откат поломки: постоянно висящий в разметке
// третий <audio> ломал на iPhone возобновление с локскрина — система
// привязывает аудио-сессию и Now Playing к конкретному элементу, лишний её
// путает, и после паузы трек «играл» с идущим временем, но беззвучно.
// Кроссфейд там всё равно недоступен: он держится на audio.volume, а на iOS
// громкость аппаратная.
var tailAudio = null;

function tailEl() {
  if (tailAudio) return tailAudio;
  if (!_volumeWorks) return null;      // на iOS не создаём вовсе
  tailAudio = document.createElement('audio');
  tailAudio.preload = 'none';
  document.body.appendChild(tailAudio);
  return tailAudio;
}

function fadeOn() { return _volumeWorks && radioOn && radioCfg && radioCfg.fade; }

// Любой выход из затухания обязан вернуть громкость: оборванная рампа иначе
// оставила бы музыку тихой до перезапуска.
function fadeCancel(restore) {
  if (_fadeTimer) { clearInterval(_fadeTimer); _fadeTimer = null; }
  _fading = false;
  if (restore !== false) { try { audio.volume = _userVolume; } catch (e) {} }
}

function tailStop() {
  if (_tailTimer) { clearInterval(_tailTimer); _tailTimer = null; }
  _tailArmed = '';
  if (!tailAudio) return;
  try { tailAudio.pause(); tailAudio.removeAttribute('src'); } catch (e) {}
}

// Хвост прогреваем заранее. Раньше он начинал грузиться в момент перехода, и
// пока шла загрузка, уходящий трек был уже заглушён, а приходящий ещё не
// зазвучал — вместо перехода получался провал в тишину.
function tailArm() {
  var el = tailEl();
  if (!el || _tailArmed === audio.src || !audio.src) return;
  _tailArmed = audio.src;
  try {
    el.preload = 'auto';
    el.src = audio.src;
    el.volume = 0;             // греем молча
    el.load();
  } catch (e) { _tailArmed = ''; }
}

// Считаем по реальному времени, а не по числу шагов: в неактивной вкладке
// setInterval зажимается до секунды, и пошаговая рампа растягивалась бы на
// минуты, а трек не переключался бы вовсе. По времени она доходит до конца
// при любой частоте тиков — просто грубее.
function rampEl(el, from, to, ms, done) {
  var t0 = Date.now();
  try { el.volume = from; } catch (e) {}
  return setInterval(function() {
    var k = Math.min(1, (Date.now() - t0) / ms);
    try { el.volume = Math.max(0, Math.min(1, from + (to - from) * k)); } catch (e) {}
    if (k >= 1 && done) done();
  }, 50);
}

function fadeRamp(from, to, ms, done) {
  if (_fadeTimer) clearInterval(_fadeTimer);
  _fadeTimer = rampEl(audio, from, to, ms, function() {
    clearInterval(_fadeTimer); _fadeTimer = null;
    if (done) done();
  });
}

// Гасим на исходе трека и сразу переходим к следующему: если ждать штатного
// ended, последние секунды были бы просто тишиной.
var TAIL_ARM_LEAD = 6;        // за сколько секунд до перехода греть хвост

function fadeCheck() {
  if (_fading || !fadeOn() || audio.paused) return;
  var d = audio.duration;
  if (!isFinite(d) || d <= 0) return;
  var left = d - audio.currentTime;
  if (left <= FADE_MS / 1000 + TAIL_ARM_LEAD) tailArm();
  if (left > FADE_MS / 1000) return;
  _fading = true;
  startCrossfade();
}

// Настоящий кроссфейд: уходящий трек доигрывает во вспомогательном элементе,
// затухая, а основной в это же время уже поднимает следующий с нуля. Роли
// элементов не меняются — вся обвязка (MediaSession, виджет, контекст
// воспроизведения) остаётся на основном, и он с первой секунды показывает
// новый трек, как и положено.
function startCrossfade() {
  var src = audio.src, pos = audio.currentTime, vol = audio.volume;
  var armed = _tailArmed;
  _fading = false;
  // tailStop() здесь звать нельзя: он сбросил бы прогретый хвост, ради
  // которого всё и затевалось. Достаточно погасить прошлую рампу.
  if (_tailTimer) { clearInterval(_tailTimer); _tailTimer = null; }
  nextTrack();          // основной уходит на следующий трек и всплывает с нуля
  // Хвост поднимаем ПОСЛЕ переключения: selectTrack сбрасывает рампы, и
  // запущенный раньше хвост он бы тут же погасил.
  if (!src || armed !== src || !tailAudio) return;   // не прогрет — лучше без перехода, чем с дырой
  try {
    // Перемотка и запуск мгновенны: элемент уже загружен tailArm().
    try { tailAudio.currentTime = pos; } catch (e) {}
    _tailTimer = rampEl(tailAudio, vol, 0, FADE_MS, tailStop);
    var tp = tailAudio.play();
    if (tp && tp.catch) tp.catch(function(){ tailStop(); });
  } catch (e) { tailStop(); }
}



function radioKey() { return '_vc_radio_' + _curFolder; }

function loadRadioCfg() {
  var d = radioDefaults();
  try {
    var saved = JSON.parse(localStorage.getItem(radioKey()) || 'null');
    if (saved) for (var k in d) if (saved[k] !== undefined) d[k] = saved[k];
  } catch (e) {}
  radioCfg = d;
  return d;
}

function saveRadioCfg() { lsSet(radioKey(), JSON.stringify(radioCfg)); }

// Жанры в тегах раздроблены на синонимы: «Alternative», «Alt. Rock»,
// «Alternative & Indie», «Alternative/Experiment» — это одно и то же, и выбор
// из трёх десятков таких пунктов был бы бесполезен. Схлопываем по корню.
// Пары «корень -> во что сложить», в порядке приоритета.
//
// Корни намеренно короткие и обрезанные: в тегах одно и то же пишут как
// «Alternative», «Alt. Rock», «Alternative & Indie», «alternetive» с опечаткой,
// «альтернатива», «альтернативный рок». Полные слова ловили только часть из
// них, префикс «altern» / «альтернат» ловит все. Русские и английские
// написания ведут в одну корзину: библиотека смешанная.
//
// Порядок значим: «альтернативный рок» должен попасть в alternative, а не в
// rock, поэтому более узкие корни идут раньше «поп» и «рок».
var RADIO_GENRE_ROOTS = [
  ['хип', 'hip-hop'], ['hip', 'hip-hop'], ['рэп', 'hip-hop'], ['рап', 'hip-hop'], ['rap', 'hip-hop'],
  ['альтернат', 'alternative'], ['altern', 'alternative'], ['alt.', 'alternative'],
  ['панк', 'punk'], ['punk', 'punk'],
  ['метал', 'metal'], ['metal', 'metal'],
  ['индастр', 'industrial'], ['industr', 'industrial'],
  ['инди', 'indie'], ['indie', 'indie'],
  ['электро', 'electronic'], ['electro', 'electronic'],
  ['танц', 'dance'], ['dance', 'dance'], ['house', 'house'], ['techno', 'techno'], ['trance', 'trance'],
  ['джаз', 'jazz'], ['jazz', 'jazz'],
  ['класси', 'classical'], ['classic', 'classical'],
  ['фолк', 'folk'], ['folk', 'folk'],
  ['блюз', 'blues'], ['blues', 'blues'],
  ['регги', 'reggae'], ['reggae', 'reggae'],
  ['кантри', 'country'], ['country', 'country'],
  ['шансон', 'chanson'], ['chanson', 'chanson'],
  ['soul', 'soul'], ['r&b', 'r&b'], ['rnb', 'r&b'],
  ['саундтр', 'soundtrack'], ['soundtrack', 'soundtrack'],
  ['поп', 'pop'], ['pop', 'pop'],
  ['рок', 'rock'], ['rock', 'rock']
];

function radioGenreKey(g) {
  g = (g || '').toLowerCase().replace(/\s+/g, ' ').trim();
  if (!g) return '';
  for (var i = 0; i < RADIO_GENRE_ROOTS.length; i++) {
    if (g.indexOf(RADIO_GENRE_ROOTS[i][0]) >= 0) return RADIO_GENRE_ROOTS[i][1];
  }
  return g;
}

function radioGenreBuckets() {
  var by = {};
  for (var i = 0; i < tracks.length; i++) {
    var k = radioGenreKey(tracks[i].gen);
    if (!k) continue;
    if (!by[k]) by[k] = {key: k, label: tracks[i].gen, n: 0};
    by[k].n++;
  }
  var out = [];
  for (var k2 in by) out.push(by[k2]);
  out.sort(function(a, b){ return b.n - a.n; });
  return out;
}

// «Часто» и «редко» — это ДОЛЯ, а не порог по числу прослушиваний.
//
// Порог вырождается: если послушать все треки поровну, скажем по четыре раза,
// то и верхний квартиль, и нижний равны четырём — условие «не меньше четырёх»
// выполняется для всей библиотеки, и «часто слушаю» перестаёт что-либо
// отбирать. С «редко» ровно та же беда с другой стороны.
//
// Поэтому берём верхнюю и нижнюю четверть рейтинга прослушанного. Это всегда
// относительно активности самого пользователя: «часто» означает «чаще, чем
// остальное у вас», а не «больше N раз».
var RADIO_ACTIVITY_SHARE = 0.25;

function radioActivityRank() {
  var played = [];
  for (var i = 0; i < tracks.length; i++) {
    var n = playCountOf(tracks[i].file);
    if (n) played.push({i: i, n: n, last: lastPlayedOf(tracks[i].file)});
  }
  // При равном числе прослушиваний «более частым» считаем то, что слушали
  // недавнее, — иначе на ровном распределении порядок был бы случайным.
  played.sort(function(a, b){ return (b.n - a.n) || (b.last - a.last); });
  var k = Math.max(1, Math.round(played.length * RADIO_ACTIVITY_SHARE));
  var top = {}, bottom = {};
  for (var t = 0; t < k && t < played.length; t++) top[played[t].i] = true;
  for (var b = 0; b < k && b < played.length; b++) {
    var idx = played[played.length - 1 - b].i;
    // На совсем короткой истории четверти сверху и снизу пересекаются —
    // один и тот же трек не должен быть одновременно частым и редким.
    if (!top[idx]) bottom[idx] = true;
  }
  return {top: top, bottom: bottom, played: played.length, share: k};
}

var RADIO_FORGOTTEN_DAYS = 90;

function radioCandidates(cfg) {
  cfg = cfg || radioCfg || radioDefaults();
  var eras = eraIndexMap(), rank = radioActivityRank();
  var cutoff = Date.now() / 1000 - RADIO_FORGOTTEN_DAYS * 86400;
  var useEras = cfg.eras && cfg.eras.length;
  var useGenres = cfg.genres && cfg.genres.length;
  var out = [];
  for (var i = 0; i < tracks.length; i++) {
    var t = tracks[i];
    if (cfg.cachedOnly || _isOffline) { if (!isTrackCached(t.file)) continue; }
    if (cfg.lossless && !t.fmt) continue;
    if (cfg.skipShort && t.dur && t.dur < 60) continue;
    if (useEras && cfg.eras.indexOf(eras[i]) < 0) continue;
    if (useGenres && cfg.genres.indexOf(radioGenreKey(t.gen)) < 0) continue;
    // Год есть не у всех треков, поэтому фильтр по нему отсекает и безгодовые:
    // иначе «музыка 2010-х» тихо притаскивала бы половину библиотеки без года.
    if (cfg.yearFrom && (!t.yr || t.yr < cfg.yearFrom)) continue;
    if (cfg.yearTo && (!t.yr || t.yr > cfg.yearTo)) continue;
    var n = playCountOf(t.file);
    if (cfg.activity === 'often' && !rank.top[i]) continue;
    if (cfg.activity === 'rare' && !rank.bottom[i]) continue;
    if (cfg.activity === 'never' && n !== 0) continue;
    if (cfg.activity === 'forgotten' && (n === 0 || lastPlayedOf(t.file) > cutoff)) continue;
    out.push(i);
  }
  return out;
}

// Затравка — не случайный трек каталога: станция должна начинаться с чего-то
// своего. Берём взвешенно среди прослушанного внутри отбора, а если истории по
// этим критериям нет — случайный из отбора.
var _lastSeedArtist = '';

function radioSeed(pool) {
  // Половину запусков берём чистый случай. Только по истории станция всегда
  // начинала с самого слушаемого артиста, и включение «рок» раз за разом
  // открывалось одним и тем же именем.
  var from;
  if (Math.random() < 0.5) {
    from = pool;
  } else {
    var weighted = [];
    for (var i = 0; i < pool.length; i++) {
      var n = playCountOf(tracks[pool[i]].file);
      for (var r = 0; r < Math.min(4, n); r++) weighted.push(pool[i]);
    }
    from = weighted.length ? weighted : pool;
  }
  // Пара попыток не начать тем же артистом, что и в прошлый раз.
  var pick = from[Math.floor(Math.random() * from.length)];
  for (var tries = 0; tries < 6; tries++) {
    var a = contNorm(tracks[pick] && tracks[pick].artist);
    if (!a || a !== _lastSeedArtist) break;
    pick = from[Math.floor(Math.random() * from.length)];
  }
  _lastSeedArtist = contNorm(tracks[pick] && tracks[pick].artist);
  return pick;
}

function radioBatch() {
  var pool = radioCandidates();
  if (!pool.length) return [];
  _artistSalt = {};        // каждая порция — своя перетасовка артистов
  var list = buildContinuation(pool, radioCfg && radioCfg.variety);
  list = (radioCfg && radioCfg.groupByArtist) ? groupTracksByArtist(list)
                                              : spreadTracksByArtist(list);
  return list;
}

// Разводит треки одного артиста, сохраняя порядок по релевантности.
//
// Ранжирование само по себе их слипает, и это не случайность: затравка даёт
// своему артисту +3, перетасовка artistSalt постоянна внутри порции — то есть
// поднимает разом ВСЕ треки удачливого артиста, — а квота пускает в порцию до
// четырёх штук. Вместе получалось «три-четыре песни одного, потом столько же
// другого», хотя группировка выключена.
//
// Идём по списку в порядке счёта и берём первый трек, чей артист не звучал
// последние SPREAD_GAP позиций. Если подходящих нет, берём лучший из
// оставшихся: иначе на библиотеке из пары артистов цикл не завершился бы.
var SPREAD_GAP = 3;

function spreadTracksByArtist(list) {
  var out = [], pending = list.slice(), lastAt = {};
  while (pending.length) {
    var picked = -1;
    for (var i = 0; i < pending.length; i++) {
      var k = contNorm(tracks[pending[i]].artist) || '?';
      var la = lastAt[k];
      if (la === undefined || out.length - la >= SPREAD_GAP) { picked = i; break; }
    }
    if (picked < 0) picked = 0;
    var idx = pending.splice(picked, 1)[0];
    lastAt[contNorm(tracks[idx].artist) || '?'] = out.length;
    out.push(idx);
  }
  return out;
}

// Порция блоками: подряд идут треки одного артиста, потом следующий. Порядок
// артистов берём из уже посоленного списка, поэтому он каждый раз свой.
function groupTracksByArtist(list) {
  var byArtist = {}, order = [], i;
  for (i = 0; i < list.length; i++) {
    var k = contNorm(tracks[list[i]].artist) || '?';
    if (!byArtist[k]) { byArtist[k] = []; order.push(k); }
    byArtist[k].push(list[i]);
  }
  var out = [];
  for (i = 0; i < order.length; i++) out = out.concat(byArtist[order[i]]);
  return out;
}

function startRadio() {
  if (!tracks.length) return;
  var pool = radioCandidates();
  if (!pool.length) { showToast('Под эти критерии ничего не нашлось'); return; }
  radioOn = true;
  var seed = radioSeed(pool);
  playQueue = [seed];
  playQueuePos = 0;
  playQueue = playQueue.concat(buildContinuation(pool, radioCfg.variety));
  syncRadioBtn();
  selectTrack(seed, true);
  showToast('Радиостанция: ' + radioSummary());
}

function stopRadio() {
  if (!radioOn) return;
  radioOn = false;
  fadeCancel(true);        // станция выключена — громкость обратно на место
  tailStop();
  syncRadioBtn();
}

// Правка критериев на ходу: сыгранное и текущий трек не трогаем, пересобираем
// только хвост — иначе изменение настроек обрывало бы играющую песню.
function applyRadioChanges() {
  saveRadioCfg();
  if (!radioOn) { startRadio(); return; }
  var pool = radioCandidates();
  if (!pool.length) { showToast('Под эти критерии ничего не нашлось'); return; }
  playQueue = playQueue.slice(0, playQueuePos + 1).concat(buildContinuation(pool, radioCfg.variety));
  showToast('Радиостанция: ' + radioSummary());
}

function radioSummary() {
  var c = radioCfg || radioDefaults(), parts = [];
  if (c.eras && c.eras.length) parts.push(c.eras.slice().sort().reverse().join(', '));
  if (c.genres && c.genres.length) parts.push(c.genres.join(', '));
  if (c.yearFrom || c.yearTo) parts.push((c.yearFrom || '…') + '–' + (c.yearTo || '…'));
  var act = {often: 'часто слушаю', rare: 'редко слушаю', never: 'ещё не слушал',
             forgotten: 'давно не слушал'}[c.activity];
  if (act) parts.push(act);
  if (c.lossless) parts.push('lossless');
  return parts.length ? parts.join(' · ') : 'вся библиотека';
}

function syncRadioBtn() {
  var b = document.getElementById('radioBtn');
  if (b) b.style.display = radioOn ? '' : 'none';
  syncRadioGlow();
  if (activeTab === 'playlists') renderPlaylists();
}

// ── Окно критериев ──
// Живой счётчик подходящих треков обязателен: жанр есть далеко не у всех
// файлов, и без него пользователь не понимал бы, почему станция вдруг стала
// крутить полтора альбома.
function openRadioModal() {
  if (!tracks.length) { showToast('Сначала откройте каталог'); return; }
  if (!radioCfg) loadRadioCfg();
  renderRadioBody();
  document.getElementById('radioGo').textContent = radioOn ? 'Обновить' : 'Включить';
  document.getElementById('radioOverlay').classList.add('show');
}

function closeRadioModal() { document.getElementById('radioOverlay').classList.remove('show'); }

function radioChip(on, label, n, onclick) {
  return '<button class="radio-chip' + (on ? ' on' : '') + '" onclick="' + onclick + '">'
    + esc(label) + (n !== null && n !== undefined ? '<span class="n">' + n + '</span>' : '') + '</button>';
}

function renderRadioBody() {
  var c = radioCfg, html = '', i;

  // Периоды показываем только когда они размечены — иначе это пустой раздел.
  if (eraConfig.enabled && eraConfig.eras && eraConfig.eras.length) {
    var years = eraConfig.eras.map(function(e){ return e.year; }).sort().reverse();
    html += '<div class="radio-group"><div class="radio-group-title">Периоды прослушивания</div><div class="radio-chips">';
    for (i = 0; i < years.length; i++) {
      html += radioChip(c.eras.indexOf(years[i]) >= 0, String(years[i]),
                        eraTrackFiles(eraConfig.eras.filter(function(e){ return e.year === years[i]; })[0]).length,
                        'radioToggle(\'eras\',' + years[i] + ')');
    }
    html += '</div></div>';
  }

  // Потолка по числу жанров нет намеренно: они выводятся из тегов при каждой
  // отрисовке, и новые — в том числе дозаполненные прогоном метаданных —
  // должны появляться здесь сами, а не упираться в «первые N». Зато редкие
  // прячем за отдельным чипом: жанров с одной-двумя песнями много, и в общем
  // ряду они только мешают выбрать нужное.
  var buckets = radioGenreBuckets();
  if (buckets.length) {
    html += '<div class="radio-group"><div class="radio-group-title">Жанры</div><div class="radio-chips">';
    var small = 0;
    for (i = 0; i < buckets.length; i++) {
      var b = buckets[i];
      var picked = c.genres.indexOf(b.key) >= 0;
      // Выбранный жанр показываем всегда, даже если он редкий: иначе снять
      // такой фильтр было бы нечем — чипа на экране просто нет.
      if (!picked && b.n < RADIO_GENRE_MIN) {
        small++;
        if (!_radioGenresOpen) continue;
      }
      html += radioChip(picked, b.key, b.n,
                        'radioToggle(\'genres\',\'' + b.key.replace(/'/g, "\\'") + '\')');
    }
    // Кнопка — такой же чип и всегда последняя, в обоих состояниях списка.
    if (small) {
      html += radioChip(false, _radioGenresOpen ? 'скрыть' : 'ещё ' + small, null, 'radioMoreGenres()');
    }
    html += '</div></div>';
  }

  html += '<div class="radio-group"><div class="radio-group-title">Что играть</div><div class="radio-chips">';
  var acts = [['any', 'любые'], ['often', 'часто слушаю'], ['rare', 'редко слушаю'],
              ['never', 'ещё не слушал'], ['forgotten', 'давно не слушал']];
  for (i = 0; i < acts.length; i++) {
    html += radioChip(c.activity === acts[i][0], acts[i][1], null, 'radioSet(\'activity\',\'' + acts[i][0] + '\')');
  }
  html += '</div></div>';

  html += '<div class="radio-group"><div class="radio-group-title">Характер подбора</div><div class="radio-chips">';
  var vars_ = [['familiar', 'чаще любимое'], ['balanced', 'вперемешку'], ['surprise', 'больше нового']];
  for (i = 0; i < vars_.length; i++) {
    html += radioChip(c.variety === vars_[i][0], vars_[i][1], null, 'radioSet(\'variety\',\'' + vars_[i][0] + '\')');
  }
  html += '</div></div>';

  html += '<div class="radio-group"><div class="radio-group-title">Год выпуска</div><div class="radio-years">'
    + '<select id="radioYearFrom" onchange="radioYears()">' + radioYearOptions(c.yearFrom) + '</select>'
    + '<span style="color:rgba(255,255,255,0.25)">–</span>'
    + '<select id="radioYearTo" onchange="radioYears()">' + radioYearOptions(c.yearTo) + '</select>'
    + '</div></div>';

  html += '<div class="radio-group"><div class="radio-group-title">Дополнительно</div><div class="radio-chips">'
    + radioChip(c.lossless, 'только lossless', null, 'radioFlip(\'lossless\')')
    + radioChip(c.cachedOnly, 'только из кэша', null, 'radioFlip(\'cachedOnly\')')
    + radioChip(c.groupByArtist, 'блоками по артистам', null, 'radioFlip(\'groupByArtist\')')
    // На iOS audio.volume не действует, поэтому там опции нет вовсе — обещать
    // то, чего не будет, хуже, чем не предлагать.
    + (_volumeWorks ? radioChip(c.fade, 'плавные переходы', null, 'radioFlip(\'fade\')') : '')
    + '</div></div>';

  document.getElementById('radioBody').innerHTML = html;
  radioCountUpdate();
}

// Годы берём из библиотеки, а не диапазоном «от 1900»: вводить руками год,
// которого в каталоге нет, бессмысленно.
function radioYearOptions(selected) {
  var seen = {}, years = [], i;
  for (i = 0; i < tracks.length; i++) if (tracks[i].yr) seen[tracks[i].yr] = 1;
  for (var y in seen) years.push(parseInt(y, 10));
  years.sort(function(a, b){ return b - a; });
  var h = '<option value="0">любой</option>';
  for (i = 0; i < years.length; i++) {
    h += '<option value="' + years[i] + '"' + (selected === years[i] ? ' selected' : '') + '>' + years[i] + '</option>';
  }
  return h;
}

// Случайные критерии: система решает сама. Наугад легко получить «джаз +
// lossless + 2003 год» и три трека, поэтому перебираем варианты и берём первый,
// под который набирается достойный пул; если ни один не набрал — самый широкий
// из опробованных.
var RADIO_RANDOM_MIN = 40;

function radioRandomize() {
  var buckets = radioGenreBuckets();
  var eraYears = (eraConfig.enabled && eraConfig.eras)
    ? eraConfig.eras.map(function(e){ return e.year; }) : [];
  var acts = ['any', 'any', 'often', 'rare', 'never', 'forgotten'];
  var vars_ = ['familiar', 'balanced', 'surprise'];
  var best = null, bestN = -1;
  for (var attempt = 0; attempt < 40; attempt++) {
    var c = radioDefaults();
    c.variety = vars_[Math.floor(Math.random() * vars_.length)];
    var roll = Math.random();
    if (buckets.length && roll < 0.45) {
      c.genres = [buckets[Math.floor(Math.random() * Math.min(buckets.length, 6))].key];
    } else if (eraYears.length && roll < 0.75) {
      var pick = eraYears[Math.floor(Math.random() * eraYears.length)];
      c.eras = [pick];
      // Иногда два соседних периода — так подборка выходит шире и живее.
      if (eraYears.length > 1 && Math.random() < 0.4) {
        var pick2 = eraYears[Math.floor(Math.random() * eraYears.length)];
        if (pick2 !== pick) c.eras.push(pick2);
      }
    } else {
      c.activity = acts[Math.floor(Math.random() * acts.length)];
    }
    var n = radioCandidates(c).length;
    if (n >= RADIO_RANDOM_MIN) { best = c; bestN = n; break; }
    if (n > bestN) { best = c; bestN = n; }
  }
  if (!best) return;
  radioCfg = best;
  renderRadioBody();
  showToast('Случайные критерии: ' + radioSummary());
}

function radioCountUpdate() {
  var n = radioCandidates().length;
  var el = document.getElementById('radioCount');
  el.className = 'radio-count' + (n ? '' : ' empty');
  el.innerHTML = n
    ? 'Подходит <b>' + n + '</b> ' + relPlural(n, 'трек', 'трека', 'треков') + ' из ' + tracks.length
    : '<b>Ни одного трека</b> – критерии слишком узкие';
  document.getElementById('radioGo').disabled = !n;
}

function radioToggle(field, value) {
  var arr = radioCfg[field], i = arr.indexOf(value);
  if (i >= 0) arr.splice(i, 1); else arr.push(value);
  renderRadioBody();
}

function radioSet(field, value) { radioCfg[field] = value; renderRadioBody(); }

function radioMoreGenres() { _radioGenresOpen = !_radioGenresOpen; renderRadioBody(); }
function radioFlip(field) { radioCfg[field] = !radioCfg[field]; renderRadioBody(); }

function radioYears() {
  radioCfg.yearFrom = parseInt(document.getElementById('radioYearFrom').value, 10) || 0;
  radioCfg.yearTo = parseInt(document.getElementById('radioYearTo').value, 10) || 0;
  radioCountUpdate();
}

function radioApply() {
  closeRadioModal();
  applyRadioChanges();
}

// Перемотки у отрывка нет — он и так тридцатисекундный, поэтому ⏮/⏭ ходят по
// трекам релиза по кругу.
function stepPreview(dir) {
  if (!_previewTracks.length) return;
  var n = _previewTrack < 0 ? 0 : _previewTrack + dir;
  if (n < 0) n = _previewTracks.length - 1;
  if (n >= _previewTracks.length) n = 0;
  _previewTrack = -1;          // иначе playPreview посчитает это повторным нажатием и остановит
  playPreview(n);
}

// ── Автопродолжение очереди ──
// Считается целиком по локальной библиотеке: ни одного запроса в сеть, поэтому
// работает и офлайн. Всё, что нужно, уже есть — артист, жанр, год, разметка
// периодов и история прослушиваний.
var CONT_BATCH = 25;
var CONT_PER_ARTIST = 4;     // иначе продолжение вырождается в дискографию одного артиста
var CONT_PER_ARTIST_GROUPED = 7;  // при группировке блоки должны быть заметными
var _eraByIndex = null;      // индекс трека -> год периода; строится по разметке

function eraIndexMap() {
  if (_eraByIndex) return _eraByIndex;
  _eraByIndex = {};
  if (eraConfig.enabled && eraConfig.eras) {
    for (var e = 0; e < eraConfig.eras.length; e++) {
      var r = resolveEra(eraConfig.eras[e]);
      for (var i = r.from; i <= r.to && i < tracks.length; i++) _eraByIndex[i] = eraConfig.eras[e].year;
    }
  }
  return _eraByIndex;
}

function contNorm(v) { return (v || '').toString().toLowerCase().trim(); }

// Случайный вес артиста, постоянный внутри одной порции и новый в следующей.
// Без него порядок диктовался только близостью и историей, и станция раз за
// разом открывалась одним и тем же именем — «будто по шаблону».
var _artistSalt = {};

function artistSalt(key) {
  if (_artistSalt[key] === undefined) _artistSalt[key] = Math.random();
  return _artistSalt[key];
}

// pool — индексы, из которых разрешено выбирать (радиостанция передаёт свой
// отбор по критериям). Без него берём всю библиотеку.
function buildContinuation(pool, variety) {
  if (!tracks.length) return [];
  var seedIdx = playQueue.length ? playQueue[playQueue.length - 1] : currentIdx;
  var seed = tracks[seedIdx];
  if (!seed) return [];
  // Недавно игравшее исключаем, иначе продолжение крутит один и тот же круг.
  var recent = {};
  for (var q = Math.max(0, playQueue.length - 150); q < playQueue.length; q++) recent[playQueue[q]] = true;
  var eras = eraIndexMap();
  // Жанр сводим к тому же корню, что и в критериях радио: сырыми строками
  // «Alternative» и «Alt. Rock» считались разными жанрами и вес не давали.
  var seedArtist = contNorm(seed.artist), seedGenre = radioGenreKey(seed.gen);
  var seedEra = eras[seedIdx], seedYear = seed.yr || 0;
  // «Знакомое» усиливает вес истории, «сюрприз» — случайности. По умолчанию
  // поровну, как было до появления радио.
  var rnd = variety === 'surprise' ? 3.5 : (variety === 'familiar' ? 0.5 : 1.2);
  var histW = variety === 'familiar' ? 3 : (variety === 'surprise' ? 0.5 : 1.5);
  var scored = [];
  var list = pool || null;
  var total = list ? list.length : tracks.length;
  for (var k0 = 0; k0 < total; k0++) {
    var i = list ? list[k0] : k0;
    if (recent[i]) continue;
    var t = tracks[i];
    if (_isOffline && !isTrackCached(t.file)) continue;   // офлайн играем только кэш
    var sc = Math.random() * rnd;                          // без этого продолжение всегда одинаковое
    if (seedArtist && contNorm(t.artist) === seedArtist) sc += 3;
    if (seedEra && eras[i] === seedEra) sc += 1.5;
    if (seedGenre && radioGenreKey(t.gen) === seedGenre) sc += 1;
    if (seedYear && t.yr && Math.abs(t.yr - seedYear) <= 2) sc += 0.5;
    var n = playCountOf(t.file);
    sc += n ? Math.min(histW, Math.log(1 + n) / Math.LN2 / 2 * (histW / 1.5)) : 0.3;
    // Соль только для радио: обычному продолжению очереди перетасовка артистов
    // ни к чему, там важна близость к текущему треку.
    if (list) sc += (artistSalt(contNorm(t.artist) || '?') - 0.5) * 2.2;
    scored.push({i: i, sc: sc});
  }
  scored.sort(function(a, b){ return b.sc - a.sc; });
  // Квота на артиста: без неё вес «тот же артист» перевешивал всё остальное, и
  // после трека Oxxxymiron продолжение целиком состояло из Oxxxymiron. Радио
  // так себя не ведёт — родственное должно быть первым, но не единственным.
  var out = [], perArtist = {};
  for (var pass = 0; pass < 2 && out.length < CONT_BATCH; pass++) {
    for (var k = 0; k < scored.length && out.length < CONT_BATCH; k++) {
      var idx = scored[k].i;
      if (out.indexOf(idx) >= 0) continue;
      var a = contNorm(tracks[idx].artist) || '?';
      // Второй проход добирает остаток, если квоты не дали набрать порцию.
      var quota = (radioOn && radioCfg && radioCfg.groupByArtist) ? CONT_PER_ARTIST_GROUPED : CONT_PER_ARTIST;
      if (pass === 0 && (perArtist[a] || 0) >= quota) continue;
      perArtist[a] = (perArtist[a] || 0) + 1;
      out.push(idx);
    }
  }
  return out;
}

// ── Timeline scrub (tap + drag, mouse & touch) ──
(function() {
  var wrap = document.getElementById('progressWrap');
  if (!wrap) return;
  var bar = wrap.querySelector('.progress-bg');
  var seeking = false;

  function pctFromX(clientX) {
    var rect = bar.getBoundingClientRect();
    var p = (clientX - rect.left) / rect.width;
    return Math.max(0, Math.min(1, p));
  }
  function preview(pct) {
    document.getElementById('progressFill').style.width = (pct * 100) + '%';
    document.getElementById('timeCurrent').textContent = formatTime(pct * audio.duration);
  }
  function commit(pct) {
    if (!audio.duration || isNaN(audio.duration)) return;
    audio.currentTime = pct * audio.duration;
  }

  wrap.addEventListener('pointerdown', function(e) {
    if (!audio.duration || isNaN(audio.duration)) return;
    seeking = true;
    wrap.classList.add('dragging');
    try { wrap.setPointerCapture(e.pointerId); } catch (err) {}
    var pct = pctFromX(e.clientX);
    preview(pct);
    commit(pct);
    e.preventDefault();
  });
  wrap.addEventListener('pointermove', function(e) {
    if (!seeking) return;
    var pct = pctFromX(e.clientX);
    preview(pct);
    commit(pct);
    e.preventDefault();
  });
  function end(e) {
    if (!seeking) return;
    seeking = false;
    wrap.classList.remove('dragging');
    try { wrap.releasePointerCapture(e.pointerId); } catch (err) {}
  }
  wrap.addEventListener('pointerup', end);
  wrap.addEventListener('pointercancel', end);
})();

function setVolume(v) {
  _userVolume = parseFloat(v);
  // Двинули ползунок посреди затухания — слушаем человека, а не рампу.
  if (_fadeTimer) fadeCancel(false);
  audio.volume = _userVolume;
}

function updateActiveHighlight() {
  // Remove old active
  var old = document.querySelectorAll('.playlist-item.active');
  for (var i = 0; i < old.length; i++) old[i].classList.remove('active');
  // Find new active by onclick attribute containing the current index
  var all = document.querySelectorAll('.playlist-item');
  for (var j = 0; j < all.length; j++) {
    var onclick = all[j].getAttribute('onclick') || '';
    if (onclick.indexOf('(' + currentIdx + ')') >= 0 || onclick.indexOf(',' + currentIdx + ')') >= 0) {
      all[j].classList.add('active');
    }
  }
}

function scrollToActive() {
  var item = document.querySelector('.playlist-item.active');
  if (item) {
    var container = item.closest('.playlist-list') || item.closest('.coverflow-wrap');
    if (container) {
      var itemTop = item.offsetTop - container.offsetTop;
      var itemH = item.offsetHeight;
      var scrollTop = container.scrollTop;
      var containerH = container.clientHeight;
      // Only scroll if item is outside visible area
      if (itemTop < scrollTop || itemTop + itemH > scrollTop + containerH) {
        container.scrollTo({ top: itemTop - containerH / 2 + itemH / 2, behavior: 'smooth' });
      }
    }
  }
}

// ── Цвет рамки радиостанции ──
// Три оттенка из одного среднего цвета обложки: базовый и два сдвига по кругу.
// Через HSL, а не сложением к RGB, — иначе тёмная или блёклая обложка давала бы
// грязно-серую рамку вместо переливов.
function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  var mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  var h = 0, l = (mx + mn) / 2, sat = 0;
  if (d) {
    sat = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    if (mx === r) h = ((g - b) / d + (g < b ? 6 : 0));
    else if (mx === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
  }
  return [h, sat, l];
}

function hslCss(h, sat, l) {
  h = ((h % 360) + 360) % 360;
  return 'hsl(' + Math.round(h) + ',' + Math.round(Math.min(1, Math.max(0, sat)) * 100)
       + '%,' + Math.round(Math.min(1, Math.max(0, l)) * 100) + '%)';
}

var ACCENT_MS = 1600;
var _accentCur = null;        // [h, s, l], откуда ведём переход
var _accentTimer = null;

function applyAccent(h, sat, lig) {
  var root = document.documentElement.style;
  root.setProperty('--rg1', hslCss(h, sat, lig));
  root.setProperty('--rg2', hslCss(h + 55, sat, lig + 0.04));
  root.setProperty('--rg3', hslCss(h - 45, sat, lig - 0.03));
}

function setRadioAccent(r, g, b) {
  var hsl = rgbToHsl(r, g, b);
  // Обложки часто малонасыщенные, а рамка должна читаться — поднимаем
  // насыщенность и держим светлоту в узком коридоре.
  var target = [hsl[0],
                Math.max(0.55, Math.min(0.9, hsl[1] + 0.3)),
                Math.max(0.5, Math.min(0.68, hsl[2] + 0.18))];
  if (!_accentCur) {                       // первый трек — ставим сразу
    _accentCur = target.slice();
    applyAccent(target[0], target[1], target[2]);
    return;
  }
  // Цвет переводим плавно: смена трека с другой обложкой иначе била по глазам
  // резким скачком оттенка. Пользовательские свойства сами не анимируются
  // (для transition им нужен @property), поэтому считаем переход вручную.
  var from = _accentCur.slice(), t0 = Date.now();
  // По короткой дуге круга: из 350° в 10° прямая интерполяция прокрутила бы
  // весь спектр в обратную сторону.
  var dh = ((target[0] - from[0] + 540) % 360) - 180;
  if (_accentTimer) clearInterval(_accentTimer);
  _accentTimer = setInterval(function() {
    var k = Math.min(1, (Date.now() - t0) / ACCENT_MS);
    var h = from[0] + dh * k;
    var sv = from[1] + (target[1] - from[1]) * k;
    var lv = from[2] + (target[2] - from[2]) * k;
    applyAccent(h, sv, lv);
    _accentCur = [h, sv, lv];
    if (k >= 1) {
      clearInterval(_accentTimer); _accentTimer = null;
      _accentCur = target.slice();
    }
  }, 40);
}

function syncRadioGlow() {
  var ids = ['radioGlow', 'radioBloom', 'radioHalo'];
  for (var i = 0; i < ids.length; i++) {
    var el = document.getElementById(ids[i]);
    if (el) el.classList.toggle('on', radioOn);
  }
}

function extractColor(img) {
  try {
    var canvas = document.createElement('canvas');
    canvas.width = 50; canvas.height = 50;
    var ctx = canvas.getContext('2d');
    var tmpImg = new Image();
    tmpImg.crossOrigin = 'anonymous';
    tmpImg.onload = function() {
      ctx.drawImage(tmpImg, 0, 0, 50, 50);
      var data = ctx.getImageData(0, 0, 50, 50).data;
      var r = 0, g = 0, b = 0, count = 0;
      for (var i = 0; i < data.length; i += 16) {
        r += data[i]; g += data[i+1]; b += data[i+2]; count++;
      }
      // Рамке радио нужен неприглушённый цвет, поэтому берём среднее до
      // затемнения: фону оно идёт с коэффициентом 0.45, а свечению — как есть.
      setRadioAccent(r / count, g / count, b / count);
      r = Math.floor(r / count * 0.45);
      g = Math.floor(g / count * 0.45);
      b = Math.floor(b / count * 0.45);
      var mx = Math.max(r, g, b);
      if (mx < 30) { r += 20; g += 20; b += 20; }
      setBgPlaying(r, g, b);
    };
    tmpImg.src = img.src;
  } catch(e) {}
}

var bgPalette = [
  [45, 20, 60], [20, 40, 65], [55, 25, 20], [15, 50, 40],
  [50, 30, 50], [25, 25, 55], [55, 40, 15], [20, 45, 50],
  [45, 15, 35], [30, 50, 25], [50, 20, 45], [20, 35, 55],
];

function randomBackground() {
  var c = bgPalette[Math.floor(Math.random() * bgPalette.length)];
  setBgPlaying(c[0], c[1], c[2]);
}

// ── Smooth canvas background ──
var bgOrbs = [];
var bgBaseR = 17, bgBaseG = 17, bgBaseB = 17;
var bgTargetR = 17, bgTargetG = 17, bgTargetB = 17;
var bgCvs, bgCtx;

function initBgCanvas() {
  bgCvs = document.getElementById('bgC');
  bgCtx = bgCvs.getContext('2d');
  resizeBgCanvas();
  window.addEventListener('resize', resizeBgCanvas);
  // Idle orbs
  bgOrbs = [
    {x:0.2, y:0.4, r:0.7, cr:120, cg:40, cb:200, a:0.35, sx:0.07, sy:0.05},
    {x:0.8, y:0.3, r:0.6, cr:200, cg:160, cb:30, a:0.3, sx:-0.06, sy:0.08},
    {x:0.5, y:0.8, r:0.65, cr:30, cg:120, cb:200, a:0.28, sx:0.05, sy:-0.06},
    {x:0.7, y:0.6, r:0.55, cr:200, cg:50, cb:80, a:0.22, sx:-0.08, sy:-0.04},
  ];
  requestAnimationFrame(drawBg);
}

function resizeBgCanvas() {
  if (!bgCvs) return;
  // Треть, а не половина: на холсте только размытые градиенты, разглядеть
  // разницу невозможно, а пикселей становится вдвое с лишним меньше. Каждый
  // кадр — пять заливок во весь холст, поэтому цена прямо пропорциональна
  // площади: при окне 1512×950 это падение с 54 до 24 млн операций в секунду.
  bgCvs.width = Math.floor(window.innerWidth / 3);
  bgCvs.height = Math.floor(window.innerHeight / 3);
}

var _bgLastFrame = 0;
var _bgColorSettling = false;   // пока цвет фона доезжает — рисуем чаще
var _bgForceDraw = true;        // перерисовать один кадр даже в статичном режиме
var _armAtRest = false;

function drawBg(t) {
  requestAnimationFrame(drawBg);
  if (!bgCtx) return;
  // Движение пятен привязано ко времени (s = t * 0.0002), а не к номеру кадра,
  // поэтому частоту можно снижать без изменения картинки — меняется только
  // плавность. На паузе хватает ~12 кадров в секунду: пятна дрейфуют медленно,
  // разницы не видно, а четыре радиальных градиента на кадр — постоянная
  // работа для видеоядра.
  // Статичный фон: пятна замирают там, где были, но градиент под цвет обложки
  // продолжает жить — при смене трека цвет доезжает и кадр перерисовывается.
  if (perfCfg.bgStatic && !_bgColorSettling && !_bgForceDraw) return;
  _bgForceDraw = false;
  var need = (isPlaying || _bgColorSettling) ? 33 : 80;
  if (t - _bgLastFrame < need || !_uiActive) return;
  var elapsed = _bgLastFrame ? (t - _bgLastFrame) : need;
  _bgLastFrame = t;

  var w = bgCvs.width, h = bgCvs.height;
  var s = t * 0.0002;

  // Переход цвета считаем по прошедшему времени, иначе на редких кадрах он
  // растянулся бы: раньше шаг был фиксированный, «на кадр».
  var k = Math.min(1, 0.04 * (elapsed / 33));
  bgBaseR += (bgTargetR - bgBaseR) * k;
  bgBaseG += (bgTargetG - bgBaseG) * k;
  bgBaseB += (bgTargetB - bgBaseB) * k;
  _bgColorSettling = Math.abs(bgTargetR - bgBaseR) + Math.abs(bgTargetG - bgBaseG)
                   + Math.abs(bgTargetB - bgBaseB) > 1.5;

  bgCtx.fillStyle = 'rgb('+Math.round(bgBaseR)+','+Math.round(bgBaseG)+','+Math.round(bgBaseB)+')';
  bgCtx.fillRect(0, 0, w, h);

  var maxDim = Math.max(w, h);
  for (var i = 0; i < bgOrbs.length; i++) {
    var o = bgOrbs[i];
    var cx = (o.x + Math.sin(s * (0.7 + i * 0.4) + i * 1.5) * 0.25) * w;
    var cy = (o.y + Math.cos(s * (0.5 + i * 0.3) + i * 2.5) * 0.2) * h;
    var radius = o.r * maxDim;

    var grad = bgCtx.createRadialGradient(cx, cy, 0, cx, cy, radius);
    grad.addColorStop(0, 'rgba('+o.cr+','+o.cg+','+o.cb+','+o.a+')');
    grad.addColorStop(0.4, 'rgba('+o.cr+','+o.cg+','+o.cb+','+(o.a*0.5)+')');
    grad.addColorStop(1, 'rgba('+o.cr+','+o.cg+','+o.cb+',0)');
    bgCtx.fillStyle = grad;
    bgCtx.fillRect(0, 0, w, h);
  }
}

function setBgPlaying(r, g, b) {
  bgTargetR = r; bgTargetG = g; bgTargetB = b;
  _bgForceDraw = true;
  // Contrasting orbs: shifted hues, brighter, more opaque
  if (bgOrbs.length >= 4) {
    // Warm highlight (shifted toward yellow/pink)
    bgOrbs[0].cr = Math.min(255, r + 120); bgOrbs[0].cg = Math.min(255, g + 80); bgOrbs[0].cb = Math.min(255, b + 40); bgOrbs[0].a = 0.4;
    // Complementary cool (inverted hue influence)
    bgOrbs[1].cr = Math.min(255, 255 - Math.floor(r*0.4)); bgOrbs[1].cg = Math.min(255, Math.floor(g * 1.5)); bgOrbs[1].cb = Math.min(255, Math.floor(b * 1.6)); bgOrbs[1].a = 0.3;
    // Deep shifted
    bgOrbs[2].cr = Math.floor(r * 0.4); bgOrbs[2].cg = Math.min(255, Math.floor(g * 0.6)); bgOrbs[2].cb = Math.min(255, Math.floor(b * 2)); bgOrbs[2].a = 0.3;
    // Accent glow
    bgOrbs[3].cr = Math.min(255, r + 60); bgOrbs[3].cg = Math.floor(g * 0.3); bgOrbs[3].cb = Math.min(255, b + 100); bgOrbs[3].a = 0.25;
  }
}

function setBgIdle() {
  bgTargetR = 17; bgTargetG = 17; bgTargetB = 17;
  if (bgOrbs.length >= 4) {
    bgOrbs[0].cr = 120; bgOrbs[0].cg = 40; bgOrbs[0].cb = 200; bgOrbs[0].a = 0.35;
    bgOrbs[1].cr = 200; bgOrbs[1].cg = 160; bgOrbs[1].cb = 30; bgOrbs[1].a = 0.3;
    bgOrbs[2].cr = 30; bgOrbs[2].cg = 120; bgOrbs[2].cb = 200; bgOrbs[2].a = 0.28;
    bgOrbs[3].cr = 200; bgOrbs[3].cg = 50; bgOrbs[3].cb = 80; bgOrbs[3].a = 0.22;
  }
}

function esc(s) {
  var d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

// ── Media Session API (lock screen controls) ──
var _mediaSessionArtUrl = null;
function updateMediaSession(t) {
  _widgetTrack = {title: t.title || '', artist: t.artist || '', album: t.album || '', file: t.file || ''};
  widgetPublish();
  if (!('mediaSession' in navigator)) return;
  function apply(artwork) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: t.title || '',
      artist: t.artist || '',
      album: t.album || '',
      artwork: artwork
    });
  }
  if (_mediaSessionArtUrl) { URL.revokeObjectURL(_mediaSessionArtUrl); _mediaSessionArtUrl = null; }
  if (!t.has_cover) { apply([]); return; }
  var netUrl = '/api/cover/' + encodeURIComponent(t.file);
  if (isTrackCached(t.file)) {
    getCachedCover(t.file, function(buf) {
      if (buf) {
        _mediaSessionArtUrl = URL.createObjectURL(new Blob([buf], {type:'image/jpeg'}));
        apply([{src:_mediaSessionArtUrl, sizes:'512x512', type:'image/jpeg'}]);
      } else {
        apply([{src:netUrl, sizes:'512x512', type:'image/jpeg'}]);
        if (!_isOffline) cacheCover(t.file);
      }
    });
  } else {
    apply([{src:netUrl, sizes:'512x512', type:'image/jpeg'}]);
  }
}

// ── Playback context (survives an iOS audio interruption and an app restart) ──
// iOS hands the Now Playing slot to whatever app grabs audio focus and never
// gives it back on its own, and it suspends a backgrounded PWA aggressively.
// Two things follow: the OS must always see an accurate mediaSession state
// (a stale 'none' is what makes iOS drop us from Control Center entirely), and
// the current track + position must live outside the page, so reopening the app
// continues where the music stopped instead of starting from nothing.
var PLAY_CTX_KEY = '_vc_playctx';
var _ctxSavedAt = 0;
var _ctxRestored = false;
var _ctxRestoring = false;     // suppress saves while we're seeking back into place
var _ctxPlayed = false;        // has the current track actually played this session
var _pendingSeek = 0;          // restored position not applied yet (media still loading)
var _wasInterrupted = false;   // paused by the system, not by the user

// Assigning src runs the media load algorithm, which pauses the element and
// fires 'pause' — indistinguishable from the system taking audio away unless we
// flag it. 'loadstart' always follows that pause, so it clears the flag
// deterministically (with a timeout in case the element had nothing loaded).
var _swappingSrc = false;
function setAudioSrc(url) {
  _swappingSrc = true;
  audio.src = url;
  setTimeout(function() { _swappingSrc = false; }, 2000);
}

function setMediaPlaybackState(state) {
  if (!('mediaSession' in navigator)) return;
  try { navigator.mediaSession.playbackState = state; } catch(e) {}
}

function savePlaybackContext(force) {
  // While restoring, currentTime is still 0 and the seek hasn't landed yet —
  // saving now would overwrite the very position we're restoring to.
  if (_ctxRestoring) return;
  if (currentIdx < 0 || currentIdx >= tracks.length) return;
  var now = Date.now();
  if (!force && now - _ctxSavedAt < 4000) return;   // timeupdate fires ~4×/s
  var file = tracks[currentIdx].file;
  var pos = audio.currentTime || 0;
  // A restored track sits at position 0 until its media loads and we can seek.
  // If the app is closed in that window (or the load stalls), writing that 0
  // would quietly erase the place we were restoring to — keep the stored one
  // until this track has actually played.
  if (pos < 1 && !_ctxPlayed) {
    try {
      var prev = JSON.parse(localStorage.getItem(PLAY_CTX_KEY) || 'null');
      if (prev && prev.file === file && prev.position > 1) return;
    } catch (e) {}
  }
  _ctxSavedAt = now;
  try {
    localStorage.setItem(PLAY_CTX_KEY, JSON.stringify({
      folder: document.getElementById('folderSelect').value,
      file: file,
      position: pos,
      ts: now
    }));
  } catch (e) {}
}

// Apply a restored position as soon as the media is far enough along to accept
// a seek. preload can stall indefinitely, so this is also retried when playback
// finally starts rather than relying on 'loadedmetadata' alone.
function applyPendingSeek() {
  if (_pendingSeek <= 1) return;
  if (!audio.duration || isNaN(audio.duration)) return;
  if (audio.currentTime > 1) { _pendingSeek = 0; return; }
  try { audio.currentTime = _pendingSeek; } catch (e) { return; }
  _pendingSeek = 0;
  _ctxRestoring = false;
  onTimeUpdate();
}

// Called once per catalog load: put the player back on the track it stopped on,
// loaded and seeked but NOT playing — iOS blocks playback without a gesture, and
// music starting by itself when you open the app would be wrong anyway. The
// point is that the first tap (in the app, on the lock screen, or in Control
// Center) continues instead of restarting.
function restorePlaybackContext() {
  if (_ctxRestored || currentIdx >= 0 || !tracks.length) return;
  var st = null;
  try { st = JSON.parse(localStorage.getItem(PLAY_CTX_KEY) || 'null'); } catch (e) {}
  if (!st || !st.file) return;
  if (st.folder && st.folder !== document.getElementById('folderSelect').value) return;
  var idx = -1;
  for (var i = 0; i < tracks.length; i++) {
    if (tracks[i].file === st.file) { idx = i; break; }
  }
  if (idx < 0) return;
  _ctxRestored = true;
  _ctxRestoring = true;
  _ctxPlayed = false;
  selectTrack(idx, false);
  _pendingSeek = st.position || 0;
  if (_pendingSeek > 1) {
    applyPendingSeek();
  } else {
    _ctxRestoring = false;
  }
  setMediaPlaybackState('paused');
}

// Re-publish everything the OS reads for its Now Playing widget. Worth doing
// whenever we come back to the foreground: after another app took audio focus
// this is the only lever a web page has to reclaim the entry.
function refreshNowPlaying() {
  if (currentIdx < 0 || currentIdx >= tracks.length) return;
  initMediaSession();   // перевесить обработчики: набор команд система читает при захвате слота
  updateMediaSession(tracks[currentIdx]);
  onTimeUpdate();
  setMediaPlaybackState(audio.paused ? 'paused' : 'playing');
}

function initPlaybackContext() {
  audio.addEventListener('loadedmetadata', applyPendingSeek);

  audio.addEventListener('play', function() {
    // Одновременно играть отрывок из «Новинок» и трек из плеера нельзя —
    // побеждает тот, что запустили последним.
    if (typeof stopPreview === 'function') stopPreview();
    _wasInterrupted = false;
    _ctxPlayed = true;
    _ctxRestoring = false;
    applyPendingSeek();   // preload may have stalled; the seek lands now
    if (!isPlaying) setPlayState(true);
    setMediaPlaybackState('playing');
  });

  audio.addEventListener('loadstart', function() { _swappingSrc = false; });

  audio.addEventListener('pause', function() {
    // Our own track switch (see setAudioSrc) and the end of a track both fire
    // 'pause' without anything having gone wrong.
    if (_swappingSrc || audio.ended) return;
    if (isPlaying) {
      // We never asked for this pause, so something took the audio away: another
      // app started playing, a call came in, headphones were unplugged.
      _wasInterrupted = true;
      setPlayState(false);
    }
    // 'paused' rather than leaving it at 'none': it keeps the page registered as
    // a media session, which is what lets the lock screen resume us later.
    setMediaPlaybackState('paused');
    savePlaybackContext(true);
  });

  document.addEventListener('visibilitychange', function() {
    if (document.hidden) { savePlaybackContext(true); return; }
    refreshNowPlaying();
  });

  // pagehide is the last reliable hook before iOS suspends or discards the PWA.
  window.addEventListener('pagehide', function() { savePlaybackContext(true); });
}

// ── Опыт: восстановление застрявшего конвейера ──
// Замер: после плея с локскрина элемент отдаёт play и playing, play()
// разрешается, readyState=4 — а currentTime стоит намертво и не оживает даже
// при возврате на передний план. Перемотка не помогла, удержание сессии
// сделало хуже (play() повисал без ответа). Остаётся пересборка.
// Шаг 1 — цикл pause/play, самый дешёвый. Шаг 2 — load(), который поднимает
// конвейер заново. src не трогаем ни на одном шаге: именно подмена src
// однажды увела Пункт управления другому приложению.
// По умолчанию включено: без пересборки трек остаётся замороженным даже
// после разблокировки, с ней — доигрывает с того же места.
var _recoverOn = true;
try { _recoverOn = localStorage.getItem('_vc_recover') !== '0'; } catch (e) {}
var _stuckAt = -1;   // позиция, на которой застряло возобновление; -1 — всё в порядке

function recoverCycle(t0) {
  try { audio.pause(); } catch (e) {}
  var p = ourAudioPlay();
  mediaLog('rec:cycle', mediaLogState());
  if (p && p.then) {
    p.then(function() { mediaLog('rec:cycle>ok'); },
           function(err) { mediaLog('rec:cycle>rej', (err && err.name) || '?'); });
  }
  setTimeout(function() {
    if (audio.currentTime > t0 + 0.05) { mediaLog('rec:cycle>moving', mediaLogState()); return; }
    recoverReload(t0);
  }, 800);
}

function recoverReload(t0) {
  mediaLog('rec:load', mediaLogState());
  var fired = false;
  function onMeta() {
    audio.removeEventListener('loadedmetadata', onMeta);
    if (fired) return;
    fired = true;
    try { audio.currentTime = t0; } catch (e) {}
    var p = ourAudioPlay();
    mediaLog('rec:load>play', mediaLogState());
    if (p && p.then) {
      p.then(function() { mediaLog('rec:load>ok'); },
             function(err) { mediaLog('rec:load>rej', (err && err.name) || '?'); });
    }
    setTimeout(function() {
      mediaLog(audio.currentTime > t0 + 0.05 ? 'rec:load>moving' : 'rec:load>frozen', mediaLogState());
    }, 900);
  }
  audio.addEventListener('loadedmetadata', onMeta);
  try { audio.load(); } catch (e) { mediaLog('rec:load>throw', e.name || '?'); }
}

function renderScratchBtn() {
  var b = document.getElementById('scratchCtxBtn');
  if (b) b.textContent = 'Звук скретча: ' + (_scratchOff ? 'выкл' : 'вкл');
}

function toggleScratchCtx() {
  _scratchOff = !_scratchOff;
  lsSet('_vc_noctx', _scratchOff ? '1' : '0');
  mediaLog('ac:' + (_scratchOff ? 'disabled' : 'enabled'));
  if (_scratchOff) scratchCtxRelease();
  renderScratchBtn();
}

function renderRecoverBtn() {
  var b = document.getElementById('recoverBtn');
  if (b) b.textContent = 'Пересборка при застревании: ' + (_recoverOn ? 'вкл' : 'выкл');
}

function toggleRecover() {
  _recoverOn = !_recoverOn;
  lsSet('_vc_recover', _recoverOn ? '1' : '0');
  if (!_recoverOn) _stuckAt = -1;
  mediaLog('rec:' + (_recoverOn ? 'enabled' : 'disabled'));
  renderRecoverBtn();
}

// ── Проверка возобновления ──
// Замер показал: после плея с локскрина элемент отдаёт play и playing,
// play() разрешается, readyState=4 — а currentTime стоит намертво. Проверка
// идёт всегда: сам факт «часы стоят» нужно видеть в журнале в любом случае.
function resumeWatch() {
  var t0 = audio.currentTime;
  setTimeout(function() {
    if (audio.paused) return;
    if (audio.currentTime > t0 + 0.05) { _stuckAt = -1; mediaLog('resume:ok', mediaLogState()); return; }
    mediaLog('resume:stuck', mediaLogState());
    if (!_recoverOn) return;
    // В фоне пересобирать нечего: load() там встаёт на readyState=1 и висит
    // до разблокировки. Запоминаем позицию и чиним, когда экран вернётся.
    if (document.hidden) { _stuckAt = t0; mediaLog('rec:deferred'); return; }
    recoverCycle(t0);
  }, 700);
}

// ── Журнал медиа-событий ──
// iOS вправе заморозить standalone-приложение, у которого не осталось
// играющего звука. Регистрация в Пункте управления при этом живёт, кнопки
// нажимаются, но исполнять команду уже некому. Отличить это от «обработчик
// отработал, а play() отклонили» без внешнего отладчика нельзя, поэтому
// события пишутся на самом устройстве и читаются после разблокировки.
var MEDIA_LOG_MAX = 80;
var _mediaLog = null;

function mediaLogAll() {
  if (_mediaLog) return _mediaLog;
  try { _mediaLog = JSON.parse(localStorage.getItem('_vc_medialog') || '[]'); }
  catch (e) { _mediaLog = []; }
  if (!_mediaLog || !_mediaLog.length) _mediaLog = _mediaLog || [];
  return _mediaLog;
}

function mediaLog(tag, extra) {
  var log = mediaLogAll();
  var rec = {t: Date.now(), e: tag};
  if (extra) rec.x = extra;
  log.push(rec);
  while (log.length > MEDIA_LOG_MAX) log.shift();
  lsSet('_vc_medialog', JSON.stringify(log));
}

// Снимок, по которому потом разбирают запись. hidden отличает блокировку
// и сворачивание от простой потери фокуса.
var _ourPlayAt = 0;

function ourAudioPlay() {
  _ourPlayAt = Date.now();
  return audio.play();
}

function mediaLogState() {
  var u = audio.currentSrc || audio.src || '';
  // Источник решает всё: из офлайн-кэша играем blob:, без кэша — поток с
  // сервера. Пока это не различали, PWA и браузер сравнивались нечестно.
  var kind = !u ? 'none' : (u.indexOf('blob:') === 0 ? 'blob' : 'http');
  return (document.hidden ? 'hid' : 'vis')
    + ' p=' + (audio.paused ? 1 : 0)
    + ' rs=' + audio.readyState
    + ' ' + kind
    + ' t=' + (audio.currentTime || 0).toFixed(1)
    + ' v=' + audio.volume + (audio.muted ? ' MUTED' : '')
    + ' ac=' + (audioCtx ? audioCtx.state : '-')
    + ' act=' + (audioCtx ? audioCtx.currentTime.toFixed(1) : '-');
}

// Часы элемента при заблокированном экране. Если звука нет, а t растёт —
// элемент играет «в никуда», и лечить надо маршрут звука, а не запуск.
var _mediaTickAt = 0;

// События элемента и жизненного цикла страницы. Отдельным слушателем, а не
// внутри существующих, чтобы диагностика не влияла на логику.
function initMediaLogging() {
  var evs = ['play', 'pause', 'playing', 'waiting', 'stalled', 'suspend', 'ended', 'error'];
  for (var i = 0; i < evs.length; i++) {
    (function(name) {
      audio.addEventListener(name, function() {
        var extra = mediaLogState();
        if (name === 'error' && audio.error) extra = 'code=' + audio.error.code + ' ' + extra;
        var tag = 'audio:' + name;
        if (name === 'play' && Date.now() - _ourPlayAt > 500) tag += '(ext)';
        mediaLog(tag, extra);
      });
    })(evs[i]);
  }
  document.addEventListener('visibilitychange', function() {
    mediaLog(document.hidden ? 'page:hidden' : 'page:visible', mediaLogState());
    if (!document.hidden) acRevive('visible');
    if (document.hidden || _stuckAt < 0) return;
    var t0 = _stuckAt;
    _stuckAt = -1;
    if (audio.paused || audio.currentTime > t0 + 0.05) return;   // ожило само
    mediaLog('rec:onvisible', mediaLogState());
    recoverCycle(t0);
  });
  window.addEventListener('pagehide', function() { mediaLog('page:pagehide', mediaLogState()); });
  // Page Lifecycle: в Safari может не поддерживаться, но если придёт - это
  // прямое доказательство заморозки.
  document.addEventListener('freeze', function() { mediaLog('page:freeze', mediaLogState()); });
  document.addEventListener('resume', function() { mediaLog('page:resume', mediaLogState()); });
}

// Перезапуск текущего трека без потери места. Голый selectTrack начинает с
// нуля: он обнуляет _pendingSeek, и отказ play() в фоне откатывал песню в
// начало — в журналах это видно как t=0.0 сразу после AbortError.
function reloadCurrentKeepingPos() {
  if (currentIdx < 0) return;
  var pos = audio.currentTime || 0;
  selectTrack(currentIdx, true);
  if (pos > 1) _pendingSeek = pos;
}

// Регистрация обработчиков виджета молчала в try/catch: отказ iOS выглядел бы
// как отсутствие кнопок, и причину было бы не отличить от чего угодно ещё.
function setMediaAction(name, fn) {
  try {
    navigator.mediaSession.setActionHandler(name, fn);
  } catch (e) {
    mediaLog('ms:reject', name + ' ' + ((e && e.name) || '?'));
  }
}

function initMediaSession() {
  if (!('mediaSession' in navigator)) return;
  navigator.mediaSession.setActionHandler('play', function() {
    mediaLog('ms:play', mediaLogState());
    acRevive('ms:play');
    if (currentIdx < 0 && tracks.length > 0) { mediaLog('ms:play>first'); selectTrack(0, true); return; }
    if (currentIdx < 0) { mediaLog('ms:play>noidx'); return; }
    // A restored track may have lost its src (iOS unloads media in suspended
    // pages), in which case play() would silently reject — reload it instead.
    if (!audio.currentSrc && !audio.src) { mediaLog('ms:play>reload'); reloadCurrentKeepingPos(); return; }
    var p = ourAudioPlay();
    if (p && p.then) {
      p.then(function() { mediaLog('ms:play>ok', mediaLogState()); resumeWatch(); },
             function(err) {
               mediaLog('ms:play>reject', (err && err.name) || '?');
               reloadCurrentKeepingPos();
             });
    }
    setPlayState(true);
  });
  navigator.mediaSession.setActionHandler('pause', function() {
    mediaLog('ms:pause', mediaLogState());
    acRevive('ms:pause');
    _stuckAt = -1;
    audio.pause(); setPlayState(false);
    mediaLog('ms:pause>after', mediaLogState());
  });
  setMediaAction('previoustrack', function() { mediaLog('ms:prev', mediaLogState()); prevTrack(); });
  setMediaAction('nexttrack', function() { mediaLog('ms:next', mediaLogState()); nextTrack(); });
  setMediaAction('seekto', function(d) {
    if (d.seekTime !== undefined && audio.duration) audio.currentTime = d.seekTime;
  });
  // iOS: override seek buttons to act as prev/next
  setMediaAction('seekbackward', function() { mediaLog('ms:seekback'); prevTrack(); });
  setMediaAction('seekforward', function() { mediaLog('ms:seekfwd'); nextTrack(); });
}

// Update position state for lock screen progress bar
function onTimeUpdate() {
  if (!audio.paused && Date.now() - _mediaTickAt > 5000) {
    _mediaTickAt = Date.now();
    mediaLog('tick', mediaLogState());
  }
  playMeterTick();
  fadeCheck();
  if (!audio.paused) savePlaybackContext();
  if ('mediaSession' in navigator && audio.duration && !isNaN(audio.duration)) {
    try {
      navigator.mediaSession.setPositionState({
        duration: audio.duration,
        playbackRate: audio.playbackRate,
        position: audio.currentTime
      });
    } catch(e) {}
  }
}

// ── История прослушиваний ──
// Отметка ставится по факту прослушивания, а не по нажатию play: копим реально
// проигранное время между тиками timeupdate. Считать по currentTime нельзя —
// перемотка вперёд накрутила бы счётчик, а повтор трека не засчитался бы вовсе.
//
// Ключ — обезномеренное имя файла (cacheKey): каталог перенумеровывается при
// каждом импорте, и по полному имени история обнулялась бы.
var PLAY_MIN_SEC = 240;        // столько достаточно и для часовой записи
var PLAY_MIN_RATIO = 0.5;
var _playAcc = 0;              // накоплено секунд по текущему треку
var _playTick = 0;             // отметка предыдущего тика
var _playCounted = false;
var _playCounts = {};          // ключ -> {n, first, last} для текущего каталога
var _playsFolder = '';
var _flushingPlays = false;

function resetPlayMeter() { _playAcc = 0; _playTick = 0; _playCounted = false; }

function playMeterTick() {
  if (audio.paused) { _playTick = 0; return; }
  var now = Date.now() / 1000;
  if (_playTick) {
    var d = now - _playTick;
    // Промежуток больше пяти секунд — это не воспроизведение, а перемотка,
    // сон устройства или заторможенная фоновая вкладка.
    if (d > 0 && d < 5) _playAcc += d;
  }
  _playTick = now;
  if (_playCounted) return;
  var dur = audio.duration;
  var need = (isFinite(dur) && dur > 0) ? Math.min(PLAY_MIN_SEC, dur * PLAY_MIN_RATIO) : PLAY_MIN_SEC;
  if (_playAcc >= need) { _playCounted = true; countPlay(currentIdx); }
}

function countPlay(idx) {
  var t = tracks[idx];
  if (!t || !t.file || !_playsFolder) return;
  var key = cacheKey(t.file), at = Date.now() / 1000;
  var e = _playCounts[key];
  if (!e) { e = _playCounts[key] = {n: 0, first: at, last: 0}; }
  e.n++; e.last = at;
  savePlaysMirror(_playsFolder);
  refreshSmartPlaylists();       // «Недавно слушал» меняется прямо сейчас
  sendPlays(_playsFolder, [{file: t.file, at: at}]);
}

function sendPlays(folder, list) {
  fetch('/api/plays', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'record', folder: folder, plays: list})})
    .then(function(r){ return r.json(); })
    .then(function(d){ if (!d || !d.ok) throw new Error('offline'); })
    .catch(function(){ queuePlays(folder, list); });
}

// Офлайн-записей в приложении нет, но история — исключение того же рода, что и
// отметки в DROPS: терять факт прослушивания из-за отсутствия сети незачем.
function queuePlays(folder, list) {
  relDbGet('pendingPlays', function(p) {
    p = p || {};
    p[folder] = (p[folder] || []).concat(list).slice(-2000);
    relDbSet('pendingPlays', p);
  });
}

// Досылаем очередь по ВСЕМ каталогам, а не только по открытому: сервер —
// единственный источник правды для аналитики и рекомендаций, и прослушивания,
// сделанные в PWA без сети, обязаны туда доехать, даже если в этот каталог
// больше не заходят. Вызывается при загрузке счётчиков, при возврате связи и
// на старте.
function flushPlays() {
  if (_flushingPlays) return;
  relDbGet('pendingPlays', function(p) {
    var folders = p ? Object.keys(p) : [];
    if (!folders.length) return;
    _flushingPlays = true;
    var i = 0;
    (function next() {
      if (i >= folders.length) {
        _flushingPlays = false;
        relDbSet('pendingPlays', p);
        if (_playsFolder && !p[_playsFolder]) loadPlays(_playsFolder, true);
        return;
      }
      var folder = folders[i++];
      var list = p[folder] || [];
      if (!list.length) { delete p[folder]; next(); return; }
      fetch('/api/plays', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'record', folder: folder, plays: list})})
        .then(function(r){ return r.json(); })
        .then(function(d) { if (d && d.ok) delete p[folder]; next(); })
        .catch(function(){ _flushingPlays = false; relDbSet('pendingPlays', p); });
    })();
  });
}

function savePlaysMirror(folder) { relDbSet('plays:' + folder, _playCounts); }

// Зеркало нужно ради умных плейлистов: без сервера они считаются на клиенте, и
// счётчики должны быть под рукой так же, как список треков в localStorage.
function loadPlays(folder, silent) {
  if (!folder) return;
  _playsFolder = folder;
  if (!silent) {
    _playCounts = {};
    relDbGet('plays:' + folder, function(m) {
      if (m && !Object.keys(_playCounts).length) { _playCounts = m; refreshSmartPlaylists(); }
    });
  }
  fetch('/api/plays', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'list', folder: folder})})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (!d || !d.ok || !d.plays) throw new Error('offline');
      _playCounts = d.plays;
      savePlaysMirror(folder);
      refreshSmartPlaylists();
      flushPlays();
    })
    .catch(function(){});             // офлайн — остаёмся на зеркале
}

function playCountOf(file) {
  var e = _playCounts[cacheKey(file)];
  return e ? (e.n || 0) : 0;
}

function lastPlayedOf(file) {
  var e = _playCounts[cacheKey(file)];
  return e ? (e.last || 0) : 0;
}

// ── Desktop widget bridge (Übersicht) ──
// Publishes now-playing state to the server and polls for control commands
// issued by the desktop widget. Localhost-only on the server side.
var _widgetTrack = {title:'', artist:'', album:'', file:''};
var _widgetLastState = '';

function widgetPublish() {
  // Виджет живёт только на Маке, и его ручки на сервере доступны лишь с
  // localhost. На телефоне этот мост слал 30 запросов в минуту в никуда: они
  // будили радиомодем и не давали ему уснуть — на батарее это заметнее любой
  // анимации. Плюс каждый запрос занимал однопоточный сервер.
  if (!isLocal || !widgetPossible) return;
  try {
    var dur = (audio && audio.duration && !isNaN(audio.duration)) ? audio.duration : 0;
    var pos = (audio && audio.currentTime) ? audio.currentTime : 0;
    var payload = JSON.stringify({
      playing: !!isPlaying,
      title: _widgetTrack.title || '',
      artist: _widgetTrack.artist || '',
      album: _widgetTrack.album || '',
      file: _widgetTrack.file || '',
      position: Math.round(pos),
      duration: Math.round(dur)
    });
    // На паузе состояние не меняется, а раньше оно отправлялось каждые две
    // секунды одинаковым. Позиция округлена до секунды, поэтому во время
    // воспроизведения отправка идёт раз в секунду, как и нужно виджету.
    if (payload === _widgetLastState) return;
    _widgetLastState = payload;
    fetch('/api/widget/state', {method:'POST', headers:{'Content-Type':'application/json'},
      body: payload}).catch(function(){});
  } catch(e) {}
}
function widgetPoll() {
  // Видимость здесь проверять НЕЛЬЗЯ: виджет для того и нужен, чтобы управлять
  // плеером, когда браузер не на переднем плане. Гасить опрос по _uiActive
  // значило бы сломать саму функцию. А вот там, где виджета не бывает —
  // телефон, сервер не под macOS — опрашивать нечего.
  if (!isLocal || !widgetPossible) { _widgetNextMs = WIDGET_IDLE_MS; return; }
  fetch('/api/widget/command', {cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
    if (!d) return;
    _widgetNextMs = d.widget ? WIDGET_FAST_MS : WIDGET_IDLE_MS;
    if (!d.cmd) return;
    switch (d.cmd) {
      case 'play':   if (!isPlaying) togglePlay(); break;
      case 'pause':  if (isPlaying) togglePlay(); break;
      case 'toggle': togglePlay(); break;
      case 'next':   nextTrack(); break;
      case 'prev':   prevTrack(); break;
    }
  }).catch(function(){});
}
var _widgetPollTimer = null, _widgetPubTimer = null;
var widgetPossible = true;      // до ответа конфига считаем, что возможен
var WIDGET_FAST_MS = 1000;      // виджет запущен — команды нужны быстро
var WIDGET_IDLE_MS = 10000;     // не запущен — просто проверяем, не появился ли
var _widgetNextMs = WIDGET_IDLE_MS;

// Опрос сам подстраивает частоту. Раньше он всегда шёл раз в секунду, даже
// когда виджет не запущен, а это 60 запросов в минуту в пустоту — и к
// однопоточному серверу, и к батарее. Теперь пока виджета нет, спрашиваем раз
// в десять секунд; сервер отвечает флагом, запущен ли он, и опрос ускоряется
// сам.
function widgetTick() {
  _widgetPollTimer = setTimeout(widgetTick, _widgetNextMs);
  widgetPoll();
}

function initWidgetBridge() {
  if (_widgetPollTimer) return;
  widgetTick();
  _widgetPubTimer = setInterval(widgetPublish, 2000);
  widgetPublish();
}

// ── Config / Folders ──
var currentUser = '';
var isAdmin = false;
var userRole = 'user';
var isLocal = true;

var _isOffline = false;

function applyConfig(cfg) {
  currentUser = cfg.username || '';
  isAdmin = cfg.is_admin || false;
  userRole = cfg.role || 'user';
  savedFolders = cfg.folders || [];
  renderFolderSelect();
  if (!_isOffline) syncNetworkState();
  var isDemo = userRole === 'demo';
  isLocal = cfg.is_local !== false; // true if server says client is local, default true for cached
  // Виджет бывает только под macOS: сервер на Windows или Linux сообщает false,
  // и опрос там не запускается вовсе.
  widgetPossible = cfg.widget_possible !== false;
  document.getElementById('adminBtn').style.display = isAdmin ? '' : 'none';
  var showNetToggles = isAdmin && !_isOffline && isLocal;
  document.getElementById('networkToggles').style.display = showNetToggles ? 'flex' : 'none';
  document.getElementById('metaVkRow').classList.toggle('has-toggles', showNetToggles);
  document.getElementById('vkBtnIcon').classList.toggle('force-hidden', isDemo || _isOffline);
  // Зеркало решает, показывать ли вкладку без сервера.
  relLoadMirror('new', function(){
    relLoadMirror('foryou', function(){ syncNewTabVisibility(); });
  });
  relLoadArtSeen();
  flushPlays();            // прослушивания без сети обязаны доехать на сервер
  flushEras();             // разметка периодов
  flushPlaylists();        // и плейлисты, собранные без связи
  syncNewTabVisibility();
  syncDownloadBtn();
  document.getElementById('metaVkRow').style.display = (isDemo || _isOffline) ? 'none' : '';
  document.getElementById('addFolderBtn').style.display = (isDemo || _isOffline) ? 'none' : '';
  document.getElementById('removeFolderBtn').style.display = (isDemo || _isOffline) ? 'none' : '';
}

function showOfflineBanner(show) {
  var banner = document.getElementById('offlineBanner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'offlineBanner';
    // Полосой во всю ширину он закрывал строку каталога и поиск — заметную
    // часть и без того тесного экрана. Компактный бейдж в свободном нижнем
    // углу сообщает ровно то же и ничего не перекрывает.
    banner.style.cssText = 'position:fixed;z-index:9999;left:10px;'
      + 'bottom:calc(10px + env(safe-area-inset-bottom, 0px));'
      + 'background:rgba(233,69,96,0.92);color:#fff;padding:5px 11px;border-radius:999px;'
      + 'font-size:11px;font-weight:600;letter-spacing:0.02em;cursor:pointer;'
      + 'display:flex;align-items:center;gap:6px;'
      + 'box-shadow:0 2px 12px rgba(0,0,0,0.4);transition:opacity .3s;'
      + 'backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);';
    // Нажимается: чаще всего «офлайн» здесь означает, что у сервера сменился
    // адрес в сети, и это способ перевести приложение на новый в одно касание.
    banner.innerHTML = '<span style="width:6px;height:6px;border-radius:50%;background:#fff;opacity:0.9"></span>офлайн';
    banner.title = 'Нажмите, чтобы указать адрес сервера';
    banner.onclick = function() { openServerDialog(); };
    document.body.appendChild(banner);
  }
  banner.style.pointerEvents = show ? 'auto' : 'none';
  banner.style.opacity = show ? '1' : '0';
}

// ── Server address rescue ──
// The PWA is bound to the origin it was installed from (https://<LAN-IP>:7656).
// Move the Mac to another Wi-Fi and that IP changes, so the installed app points
// at a dead origin — and "Обновить приложение" can't help, /reset is same-origin.
// This dialog lets you jump to the server's current address without deleting the
// PWA, and pushes the stable <hostname>.local address, which survives a network
// change for good (origin unchanged → Service Worker and track cache survive).
var SRV_PORT = 'PORT_PLACEHOLDER';

function normalizeServerUrl(v) {
  v = (v || '').trim().replace(/\/+$/, '');
  if (!v) return '';
  if (!/^https?:\/\//i.test(v)) {
    var bare = v.split('/')[0];
    var isLoopback = /^(localhost|127\.0\.0\.1)(:|$)/i.test(bare);
    v = (isLoopback ? 'http://' : 'https://') + v;
  }
  try {
    var u = new URL(v);
    if (!u.port) u.port = /^(localhost|127\.0\.0\.1)$/i.test(u.hostname) ? '7666' : SRV_PORT;
    return u.protocol + '//' + u.host;
  } catch (e) {
    return '';
  }
}

function savedServerHosts() {
  try { return JSON.parse(localStorage.getItem('_vc_hosts') || '[]') || []; } catch (e) { return []; }
}

function rememberServerHost(url) {
  var list = savedServerHosts().filter(function(u) { return u !== url; });
  list.unshift(url);
  try { localStorage.setItem('_vc_hosts', JSON.stringify(list.slice(0, 8))); } catch (e) {}
}

// Every address we've ever seen for this server: the stable .local one first,
// then the LAN IPs the server last reported, then anything typed by hand.
function knownServerUrls() {
  var out = [];
  function add(u) { u = normalizeServerUrl(u); if (u && out.indexOf(u) < 0) out.push(u); }
  var cfg = {};
  try { cfg = JSON.parse(localStorage.getItem('_vc_config') || '{}'); } catch (e) {}
  add(cfg.lan_host_url);
  (cfg.all_urls || []).forEach(add);
  savedServerHosts().forEach(add);
  add(location.origin);
  return out;
}

// A no-cors probe: a resolved promise means the host answered AND its
// certificate is already trusted on this device. A rejection is ambiguous
// (unreachable, or just an unaccepted self-signed cert), so we only ever mark
// the confirmed ones.
function probeServer(url, cb) {
  var done = false;
  var timer = setTimeout(function() { if (!done) { done = true; cb(false); } }, 4000);
  function finish(ok) { if (!done) { done = true; clearTimeout(timer); cb(ok); } }
  if (url === location.origin) {
    // Same origin: the SW answers a dead server with a 200 {error:'offline'},
    // so the status code proves nothing — the body does.
    fetch('/api/version', {cache: 'no-store'})
      .then(function(r) { return r.json(); })
      .then(function(d) { finish(!!(d && d.version)); })
      .catch(function() { finish(false); });
  } else {
    fetch(url + '/api/version', {mode: 'no-cors', cache: 'no-store'})
      .then(function() { finish(true); })
      .catch(function() { finish(false); });
  }
}

function renderServerList() {
  var box = document.getElementById('srvList');
  var urls = knownServerUrls();
  box.innerHTML = '';
  urls.forEach(function(url, i) {
    var isCurrent = (url === location.origin);
    var isStable = /\.local(:|$)/i.test(url.replace(/^https?:\/\//, ''));
    var btn = document.createElement('button');
    btn.className = 'folder-btn folder-btn-secondary';
    btn.style.cssText = 'padding:10px 12px;text-align:left;white-space:normal;display:flex;align-items:center;gap:8px';
    btn.innerHTML = '<span id="srvDot' + i + '" style="width:7px;height:7px;border-radius:50%;background:rgba(255,255,255,0.15);flex-shrink:0"></span>'
      + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;font-size:13px">' + url + '</span>'
      + (isStable ? '<span style="font-size:10px;color:#52b788;flex-shrink:0">стабильный</span>' : '')
      + (isCurrent ? '<span style="font-size:10px;color:rgba(255,255,255,0.3);flex-shrink:0">сейчас</span>' : '');
    btn.onclick = function() { goToServer(url); };
    box.appendChild(btn);
    probeServer(url, function(ok) {
      var dot = document.getElementById('srvDot' + i);
      if (dot && ok) dot.style.background = '#52b788';
    });
  });
  if (!urls.length) box.innerHTML = '<div style="font-size:12px;color:rgba(255,255,255,0.25)">Нет сохранённых адресов</div>';

  var cfg = {};
  try { cfg = JSON.parse(localStorage.getItem('_vc_config') || '{}'); } catch (e) {}
  var stable = normalizeServerUrl(cfg.lan_host_url);
  var hint = 'Офлайн-кэш браузер хранит отдельно для каждого адреса, поэтому после перехода на новый IP треки скачиваются заново.';
  if (stable && stable !== location.origin) {
    hint += ' Чтобы это не повторялось, установите PWA из Safari по адресу <b style="color:rgba(255,255,255,0.55)">' + stable + '</b> — он не меняется при смене Wi-Fi. (Chrome имена .local не резолвит, там пользуйтесь IP.)';
  } else if (stable) {
    hint += ' Сейчас приложение уже открыто по стабильному адресу — смена сети ему не страшна.';
  }
  document.getElementById('srvHint').innerHTML = hint;
}

function goToServer(url) {
  if (!url) return;
  rememberServerHost(url);
  if (url === location.origin) { location.href = '/'; return; }
  location.href = url + '/';
}

function goToManualServer() {
  var url = normalizeServerUrl(document.getElementById('srvManual').value);
  if (!url) { showToast('Введите IP или имя хоста'); return; }
  goToServer(url);
}

function openServerDialog() {
  var prof = document.getElementById('profileOverlay');
  if (prof) prof.classList.remove('show');
  document.getElementById('srvCurrent').textContent = location.origin;
  document.getElementById('srvManual').value = '';
  renderServerList();
  document.getElementById('serverOverlay').classList.add('show');
}

function loadConfig() {
  var t0 = Date.now();
  var hadCache = false;
  try {
    var saved = localStorage.getItem('_vc_config');
    if (saved) {
      var cached = JSON.parse(saved);
      applyConfig(cached);
      if (cached.last_folder) {
        document.getElementById('folderSelect').value = cached.last_folder;
        loadFolderCacheFirst(cached.last_folder);
      }
      hadCache = true;
    }
  } catch(e){}
  if (!hadCache) showLoadingIndicator();
  fetch('/api/config').then(function(r){return r.json()}).then(function(cfg) {
    if (cfg.error === 'unauthorized') {
      if ('caches' in window) caches.keys().then(function(n){n.filter(function(k){return k.startsWith('app-')}).forEach(function(k){caches.delete(k)})});
      window.location.reload();
      return;
    }
    // The SW answers {error:'offline'} when it couldn't reach the server. With a
    // cached config we used to just return — so a PWA whose origin died (the
    // Mac's LAN IP changed) looked perfectly alive, played nothing and offered
    // no way out. Retry a few times to ride out a restart, then go offline for
    // real, which also surfaces the tappable banner → «Подключение к серверу».
    if (cfg.error === 'offline') { if (hadCache) retryConfigThenGoOffline(); else enterOfflineMode(); return; }
    _isOffline = false;
    showOfflineBanner(false);
    lsSet('_vc_config', JSON.stringify(cfg));
    applyConfig(cfg);
    if (cfg.last_folder) {
      document.getElementById('folderSelect').value = cfg.last_folder;
      loadFolder(cfg.last_folder);
    }
    // Cache the state of every catalog in the background so you can switch to
    // (and play cached tracks from) any catalog, even offline or over a flaky LAN.
    setTimeout(function(){ prefetchAllFolderStates(cfg.folders, cfg.last_folder); }, 3000);
    // Pick up a caching run that a lost connection (or a cert re-prompt) cut short.
    setTimeout(resumeCacheQueue, 4000);
  }).catch(function() {
    if (!hadCache) enterOfflineMode();
  });
}

// Server unreachable but we have a cached config: give it a few seconds (server
// restart, LAN/HTTPS toggle, Wi-Fi blip) before declaring the app offline.
var _offlineRetryPending = false;
function retryConfigThenGoOffline(attemptsLeft) {
  if (attemptsLeft === undefined) {
    if (_offlineRetryPending) return;
    _offlineRetryPending = true;
    attemptsLeft = 3;
  }
  if (_isOffline) { _offlineRetryPending = false; return; }
  if (attemptsLeft <= 0) {
    _offlineRetryPending = false;
    enterOfflineMode();
    return;
  }
  setTimeout(function() {
    fetch('/api/config', {cache: 'no-store'}).then(function(r){ return r.json(); }).then(function(cfg) {
      if (cfg && cfg.error) throw new Error('offline');
      _offlineRetryPending = false;
      _isOffline = false;
      showOfflineBanner(false);
      lsSet('_vc_config', JSON.stringify(cfg));
      applyConfig(cfg);
    }).catch(function() { retryConfigThenGoOffline(attemptsLeft - 1); });
  }, 2500);
}

var _statesPrefetched = false;
// localStorage writer that makes room instead of silently failing. Catalog
// blobs (_vc_folder_*) are the big, most-regenerable entries, so they are the
// first thing evicted when the quota is hit — playlists and config must win.
function lsSet(key, value) {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch (e) {
    var victims = Object.keys(localStorage).filter(function(k) {
      return k.indexOf('_vc_folder_') === 0 && k !== key;
    }).sort(function(a, b) {
      return (localStorage.getItem(b) || '').length - (localStorage.getItem(a) || '').length;
    });
    for (var i = 0; i < victims.length; i++) {
      try { localStorage.removeItem(victims[i]); } catch (e2) {}
      try { localStorage.setItem(key, value); return true; } catch (e3) {}
    }
    return false;
  }
}

function prefetchAllFolderStates(folders, skipPath) {
  if (_statesPrefetched || _isOffline || !folders || !folders.length) return;
  _statesPrefetched = true;
  // Scanning is heavy (reads tags for every file); cap background prefetch so a
  // large number of catalogs doesn't hammer the single-threaded server. Beyond
  // the cap, catalogs are still cached on first visit.
  var list = folders.filter(function(f){ return f && f !== skipPath; }).slice(0, 12);
  var i = 0;
  (function next() {
    if (i >= list.length || _isOffline) return;
    var path = list[i++];
    // prefetch=1 → no server-side state change (current/last folder untouched)
    fetch('/api/scan?path=' + encodeURIComponent(path) + '&prefetch=1')
      .then(function(r){ return r.json(); })
      .then(function(data) {
        if (data && !data.error && data.tracks) {
          lsSet('_vc_folder_' + path, JSON.stringify(data));
        }
        // Playlists live in a separate endpoint and were never prefetched, so
        // offline every catalog but the last-opened one looked empty.
        return fetch('/api/playlists', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({folder: path, action: 'list'})})
          .then(function(r){ return r.json(); })
          .then(function(d) {
            if (d && d.playlists) lsSet('_vc_playlists_' + path, JSON.stringify(d.playlists));
          });
      })
      .catch(function(){})
      .then(function(){ setTimeout(next, 400); });  // gentle on the single-threaded server
  })();
}

function showLoadingIndicator() {
  document.getElementById('trackList').innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 20px;color:rgba(255,255,255,0.3)">'
    + '<div class="loading-spinner"></div><div style="margin-top:12px;font-size:13px">Загрузка...</div></div>';
}

function enterOfflineMode() {
  _isOffline = true;
  showOfflineBanner(true);
  syncNewTabVisibility();
  // Auto-activate cached-only filter
  showCachedOnly = true;
  var cBtn = document.getElementById('cachedOnlyBtn');
  if (cBtn) cBtn.classList.add('active');
  showToast('Офлайн — показаны только кэшированные треки');
  // Restore config from localStorage
  try {
    var saved = localStorage.getItem('_vc_config');
    if (saved) {
      var cfg = JSON.parse(saved);
      applyConfig(cfg);
      if (cfg.last_folder) {
        document.getElementById('folderSelect').value = cfg.last_folder;
        loadFolderOffline(cfg.last_folder);
      }
    }
  } catch(e){}
}

function renderFolderSelect() {
  var sel = document.getElementById('folderSelect');
  var val = sel.value;
  sel.innerHTML = '<option value="">-- Выберите каталог --</option>';
  for (var i = 0; i < savedFolders.length; i++) {
    var o = document.createElement('option');
    o.value = savedFolders[i];
    o.textContent = savedFolders[i].split('/').pop() || savedFolders[i];
    sel.appendChild(o);
  }
  if (val) sel.value = val;
}

function onFolderSelect(val) {
  if (val) loadFolder(val);
}

function toggleAddFolder() {
  var row = document.getElementById('addFolderRow');
  row.classList.toggle('show');
  if (row.classList.contains('show')) {
    document.getElementById('newFolderPath').focus();
  }
}

// ── File browser ──
var browseCurrentPath = '';

function openBrowse() {
  document.getElementById('browseOverlay').classList.add('show');
  var initial = document.getElementById('newFolderPath').value.trim() || '';
  browseTo(initial);
}

function browseTo(path) {
  fetch('/api/browse?path=' + encodeURIComponent(path || ''))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      browseCurrentPath = d.current;
      document.getElementById('browsePath').value = d.current;
      var html = '';
      for (var i = 0; i < d.items.length; i++) {
        var item = d.items[i];
        if (item.is_dir) {
          html += '<div class="browse-item is-dir" onclick="browseTo(\'' + item.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'") + '\')">'
            + '<span class="bi-icon">&#128193;</span>'
            + '<span class="bi-name">' + esc(item.name) + '</span></div>';
        } else {
          html += '<div class="browse-item is-file">'
            + '<span class="bi-icon">&#9835;</span>'
            + '<span class="bi-name">' + esc(item.name) + '</span></div>';
        }
      }
      if (d.music_count > 0) {
        html += '<div class="browse-info">' + d.music_count + ' аудиофайлов в этой папке</div>';
      }
      document.getElementById('browseList').innerHTML = html;
    });
}

function browseSelect() {
  if (!browseCurrentPath) return;
  document.getElementById('newFolderPath').value = browseCurrentPath;
  document.getElementById('browseOverlay').classList.remove('show');
}

function addFolderFromInput() {
  var input = document.getElementById('newFolderPath');
  var path = input.value.trim();
  if (!path) return;
  if (savedFolders.indexOf(path) < 0) savedFolders.push(path);
  renderFolderSelect();
  document.getElementById('folderSelect').value = path;
  input.value = '';
  document.getElementById('addFolderRow').classList.remove('show');
  loadFolder(path);
}

function removeCurrentFolder() {
  var sel = document.getElementById('folderSelect');
  var path = sel.value;
  if (!path) { showToast('Каталог не выбран'); return; }
  var name = path.split('/').pop() || path;
  showConfirm('Удалить каталог «' + name + '» из списка?', function() {
    fetch('/api/remove_folder', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: path})});
    var idx = savedFolders.indexOf(path);
    if (idx >= 0) savedFolders.splice(idx, 1);
    renderFolderSelect();
    tracks = []; albums = []; currentIdx = -1; filteredTracks = null; filteredAlbums = null;
    renderTracks(); renderAlbums();
    document.getElementById('playlistHeader').textContent = '0 треков';
    showToast('Каталог удалён');
  });
}

function showConfirm(text, onYes, yesLabel) {
  document.getElementById('confirmText').textContent = text;
  var btn = document.getElementById('confirmYes');
  btn.textContent = yesLabel || 'Да';
  document.getElementById('confirmOverlay').classList.add('show');
  var newBtn = btn.cloneNode(true);
  btn.parentNode.replaceChild(newBtn, btn);
  newBtn.addEventListener('click', function() { closeConfirm(); onYes(); });
}

function closeConfirm() {
  document.getElementById('confirmOverlay').classList.remove('show');
}

function applyFolderData(data) {
  tracks = data.tracks;
  albums = data.albums;
  filteredTracks = null;
  filteredAlbums = null;
  document.getElementById('searchInput').value = '';
  document.getElementById('searchClear').classList.remove('show');
  isEditMode = false;
  document.getElementById('editControls').style.display = 'none';
  if (_isOffline) {
    // In offline mode, force cached-only view
    showCachedOnly = true;
    var btn = document.getElementById('cachedOnlyBtn');
    if (btn) btn.classList.add('active');
  }
  // Тот же каталог с тем же числом треков — это просто обновление, а не смена.
  // Различать обязательно: loadFolder срабатывает при каждом возврате PWA из
  // фона, и безусловный сброс молча выключал радиостанцию, подменяя её очередь
  // обычным списком — дальше играл следующий трек по порядку.
  var sameFolder = (_loadedFolder === _curFolder && _loadedCount === tracks.length);
  _loadedFolder = _curFolder;
  _loadedCount = tracks.length;

  loadPlays(_curFolder);
  loadEras(_curFolder);
  loadRadioCfg();          // критерии свои у каждого каталога
  renderTracks();
  renderAlbums();
  checkIfNumbered();
  if (!sameFolder) {
    _forceNextFile = null; // отметка относилась к прежнему каталогу
    stopRadio();           // прежняя станция вела по трекам прошлого каталога
    buildDefaultQueue();
  } else if (!radioOn) {
    // Каталог тот же, но станция не ведёт — очередь можно пересобрать штатно.
    buildDefaultQueue();
  }
  // Put the player back where it stopped (paused) before anything else touches
  // currentIdx — this is what keeps the context across an app restart.
  restorePlaybackContext();
  // Playlists must refresh with the catalog too (like tracks/albums). Show this
  // folder's cached playlists immediately, then refresh from the server.
  var folder = document.getElementById('folderSelect').value;
  if (folder) {
    expandedPlaylist = null;  // collapse any playlist expanded in the previous catalog
    try {
      var cachedPl = localStorage.getItem('_vc_playlists_' + folder);
      userPlaylists = cachedPl ? JSON.parse(cachedPl) : [];
    } catch(e){ userPlaylists = []; }
    renderPlaylists();
    if (activeTab === 'playlists') document.getElementById('playlistHeader').textContent = plHeaderText();
    if (!_isOffline) {
      fetch('/api/playlists', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({folder: folder, action: 'list'})})
      .then(function(r){return r.json()}).then(function(d) {
        if (!d.playlists) return;
        userPlaylists = d.playlists;
        lsSet('_vc_playlists_' + folder, JSON.stringify(userPlaylists));
        renderPlaylists();
        if (activeTab === 'playlists') document.getElementById('playlistHeader').textContent = plHeaderText();
      }).catch(function(){});
    }
  }
  if (activeTab === 'albums') {
    document.getElementById('playlistHeader').textContent = albums.length + ' альбомов';
  } else if (activeTab === 'tracks') {
    updateTrackCounter();
  }
}

function loadFolderCacheFirst(path) {
  // Show cached data instantly, then update from server
  try {
    var saved = localStorage.getItem('_vc_folder_' + path);
    if (saved) {
      applyFolderData(JSON.parse(saved));
      return;
    }
  } catch(e){}
  showLoadingIndicator();
}

var _loadedFolder = '';   // каталог, данные которого сейчас применены
var _loadedCount = -1;    // и сколько в нём было треков

// Путь открытого каталога. Раньше его брали из <select>, но значение там
// выставляется отдельно от загрузки и на момент applyFolderData могло быть
// пустым — история и периоды тогда цеплялись к пустому каталогу.
var _curFolder = '';

function _applyCachedFolder(path) {
  // Show a catalog from its cached state (track list saved on a previous scan).
  _curFolder = path;
  try {
    var saved = localStorage.getItem('_vc_folder_' + path);
    if (saved) { applyFolderData(JSON.parse(saved)); return true; }
  } catch(e){}
  return false;
}

function loadFolder(path, retries) {
  if (!path) return;
  _curFolder = path;
  if (_isOffline) { loadFolderOffline(path); return; }
  if (retries === undefined) retries = 2;
  // Cache-first: switch to the catalog's cached state instantly, then refresh
  // from the server. This makes switching catalogs always work — and keeps
  // cached tracks playable — even if the scan hiccups over LAN/WAN (the SW can
  // return {error:'offline'} on a transient failure).
  var hadCache = _applyCachedFolder(path);
  if (!hadCache && !tracks.length) showLoadingIndicator();
  fetch('/api/scan?path=' + encodeURIComponent(path))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        // Transient offline/SW failure — retry, then keep the cached catalog.
        if (retries > 0) { setTimeout(function(){ loadFolder(path, retries - 1); }, 1000); return; }
        if (data.error === 'offline') { if (!hadCache && !tracks.length) enterOfflineMode(); return; }
        if (!hadCache) showToast(data.error);
        return;
      }
      lsSet('_vc_folder_' + path, JSON.stringify(data));
      applyFolderData(data);
    })
    .catch(function() {
      if (retries > 0) { setTimeout(function(){ loadFolder(path, retries - 1); }, 1000); return; }
      // Network failed and no fresh data — keep cached catalog if we have it.
      if (!hadCache && !tracks.length) enterOfflineMode();
    });
}

function loadFolderOffline(path) {
  _curFolder = path;
  try {
    var saved = localStorage.getItem('_vc_folder_' + path);
    if (saved) {
      applyFolderData(JSON.parse(saved));
      return;
    }
  } catch(e){}
  document.getElementById('playlistHeader').textContent = 'Нет кэшированных данных';
}

// ── Network state sync ──
function setToggle(id, dotId, on) {
  document.getElementById(id).checked = on;
  var dot = document.getElementById(dotId);
  dot.style.left = on ? '16px' : '2px';
  dot.style.background = on ? '#e94560' : '#888';
}

// The address list is bulky and rarely needed, so it lives behind the "i"
// spoiler and starts collapsed on every load. Content and open/closed state are
// tracked separately: the panel may hold text while staying hidden.
var _lanInfoOpen = false;
var _lanInfoHasContent = false;

function applyLanInfoVisibility() {
  var info = document.getElementById('lanInfo');
  var btn = document.getElementById('lanInfoBtn');
  if (btn) {
    btn.style.display = _lanInfoHasContent ? 'flex' : 'none';
    btn.classList.toggle('open', _lanInfoOpen && _lanInfoHasContent);
  }
  if (info) info.style.display = (_lanInfoOpen && _lanInfoHasContent) ? '' : 'none';
}

function setLanInfo(html, forceOpen) {
  var info = document.getElementById('lanInfo');
  if (!info) return;
  _lanInfoHasContent = !!html;
  info.innerHTML = html || '';
  // Progress messages from an LAN/WAN switch the user just flipped are worth
  // unfolding by themselves; the plain address refresh never forces anything.
  if (forceOpen && _lanInfoHasContent) _lanInfoOpen = true;
  applyLanInfoVisibility();
}

function toggleLanInfo() {
  _lanInfoOpen = !_lanInfoOpen;
  applyLanInfoVisibility();
}

function syncNetworkState(retriesLeft) {
  if (!isAdmin) return;
  if (retriesLeft === undefined) retriesLeft = 5;
  Promise.all([
    fetch('/api/config').then(function(r){return r.json()}),
    fetch('/api/wan/status').then(function(r){return r.json()})
  ]).then(function(results) {
    var cfg = results[0];
    var wan = results[1];
    // If server just restarted, endpoints may briefly return {error:'offline'} via SW
    if (cfg && cfg.error) throw new Error('not-ready');
    var parts = [];

    setToggle('publicToggle', 'publicDot', cfg.public);
    setToggle('wanToggle', 'wanDot', wan.active);

    // Show network info only on server machine
    if (cfg.is_local) {
      if (wan.active && wan.url) {
        parts.push('<span style="color:#52b788">&#9679;</span> WAN: <a href="' + wan.url + '" target="_blank" class="net-link">' + wan.url + '</a>');
      } else if (cfg.public && cfg.all_urls && cfg.all_urls.length) {
        var lanPart = '<span style="color:#52b788">&#9679;</span> LAN:';
        for (var u = 0; u < cfg.all_urls.length; u++) {
          lanPart += ' <a href="' + cfg.all_urls[u] + '" target="_blank" class="net-link">' + cfg.all_urls[u] + '</a>';
        }
        parts.push(lanPart);
        // The IPs above change with every new Wi-Fi; the .local name doesn't, so
        // it's the address a phone should install the PWA from.
        if (cfg.lan_host_url) {
          parts.push('<span style="color:#52b788">&#9679;</span> Для PWA на iPhone (Safari, адрес не меняется при смене сети): <a href="' + cfg.lan_host_url + '" target="_blank" class="net-link">' + cfg.lan_host_url + '</a>');
        }
      }
    }

    setLanInfo(parts.length ? parts.join('<br>') : '');
  }).catch(function() {
    if (retriesLeft > 0) setTimeout(function(){ syncNetworkState(retriesLeft - 1); }, 1500);
  });
}

function togglePublic(enabled) {
  setToggle('publicToggle', 'publicDot', enabled);
  setLanInfo(enabled ? 'Подключаю LAN...' : 'Отключаю LAN...', true);

  if (!enabled) {
    // Disable WAN too if it's on
    if (document.getElementById('wanToggle').checked) {
      fetch('/api/wan/stop', {method:'POST'});
      setToggle('wanToggle', 'wanDot', false);
    }
  }

  fetch('/api/public', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({enabled: enabled})})
  .then(function(r){return r.json()})
  .then(function(d) {
    // Surface real server errors instead of misreporting them as state changes
    if (d.error === 'unauthorized') {
      info.textContent = 'Сессия истекла, нужно войти заново';
      setToggle('publicToggle', 'publicDot', !enabled);
      setTimeout(function() { window.location.href = '/'; }, 1500);
      return;
    }
    if (d.ok === false || (d.error && d.public === undefined)) {
      info.textContent = 'Ошибка: ' + (d.error || 'не удалось переключить LAN');
      setToggle('publicToggle', 'publicDot', !enabled);
      return;
    }
    setToggle('publicToggle', 'publicDot', !!d.public);
    // Installed apps (Add to Dock / PWA) can't load the self-signed HTTPS page —
    // there's no "proceed anyway" prompt — so redirecting there breaks the app
    // and you can't even toggle LAN back off. Only redirect when we're certain
    // this is a normal browser tab (display-mode: browser); otherwise stay on
    // the always-working local page. Fail-safe: anything uncertain → no redirect.
    var _browserTab = !!(window.matchMedia && window.matchMedia('(display-mode: browser)').matches)
                      && window.navigator.standalone !== true;
    if (d.redirect_url && _browserTab) {
      info.textContent = d.public ? 'LAN включён, переключаюсь на HTTPS…' : 'LAN выключен, возврат на локальный адрес…';
      setTimeout(function(){ window.location.href = d.redirect_url; }, d.public ? 2500 : 1500);
    } else {
      showToast(d.public ? 'LAN включён' : 'LAN выключен');
      setTimeout(function(){ syncNetworkState(); }, 2000);
    }
  }).catch(function(){ info.textContent = 'Ошибка соединения'; });
}

// ── WAN (Cloudflare Tunnel) ──
function toggleWan(enabled) {
  if (enabled) {
    document.getElementById('wanModeOverlay').classList.add('show');
  } else {
    setToggle('wanToggle', 'wanDot', false);
    fetch('/api/wan/stop', {method:'POST'}).then(function() {
      syncNetworkState();
      showToast('WAN остановлен');
    });
  }
}

function startWanMode(mode) {
  document.getElementById('wanModeOverlay').classList.remove('show');
  setToggle('wanToggle', 'wanDot', true);

  // Auto-enable LAN
  if (!document.getElementById('publicToggle').checked) {
    setToggle('publicToggle', 'publicDot', true);
    fetch('/api/public', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({enabled: true})});
  }

  if (mode === 'tunnel') {
    setLanInfo('<span style="color:#e9a545">&#9679;</span> Запускаю туннель...', true);
    fetch('/api/wan/start', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mode: 'tunnel'})}).then(function() {
      wanPollCount = 0;
      pollWanStatus();
    });
  } else if (mode === 'static') {
    var ip = document.getElementById('wanStaticIp').value.trim();
    var port = document.getElementById('wanStaticPort').value.trim() || 'PORT_PLACEHOLDER';
    if (!ip) { showToast('Введите IP-адрес'); setToggle('wanToggle', 'wanDot', false); return; }
    fetch('/api/wan/start', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mode: 'static', ip: ip, port: port})}).then(function(r) {return r.json()}).then(function(d) {
      if (d.url) {
        syncNetworkState();
        showToast('WAN: ' + d.url);
      }
    });
  }
}

var wanPollCount = 0;
function pollWanStatus() {
  fetch('/api/wan/status').then(function(r){return r.json()}).then(function(d) {
    if (d.url) {
      wanPollCount = 0;
      syncNetworkState();
      showToast('WAN туннель активен');
    } else {
      wanPollCount++;
      if (wanPollCount < 25) {
        setLanInfo('<span style="color:#e9a545">&#9679;</span> Запускаю туннель... (' + wanPollCount + 'с)', true);
        setTimeout(pollWanStatus, 1000);
      } else {
        wanPollCount = 0;
        setToggle('wanToggle', 'wanDot', false);
        syncNetworkState();
        showToast('Не удалось запустить туннель');
      }
    }
  });
}

// ── Search ──
var searchTimer = null;
var filteredAlbums = null;

function searchArtist(name) {
  if (!name || !name.trim()) return;
  // Switch to playlist view on mobile
  if (window.innerWidth <= 768) mobileShow('playlist');
  // Switch to tracks tab
  showTab('tracks');
  var input = document.getElementById('searchInput');
  input.value = name.trim();
  onSearchInput(name.trim());
  input.focus();
}

// ── Search matching ──
// The list shows tag metadata (title/artist/album), but the filename on disk
// usually spells the same track differently: «0005. DarkLux, XGODEN - Smack That
// (Slowed & Reverb).mp3» versus title «Smack That(Slowed & Reverb)» and artist
// «DarkLux/XGODEN». Searching only one of the two sources loses whichever
// spelling the user happens to remember, so the haystack holds both and all
// punctuation is flattened to spaces on both sides.
function normSearch(s) {
  return (s || '').toLowerCase()
    .replace(/[\/\\,;&()\[\]{}«»"'`~!?:+_.\-–—]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

// Every term must appear somewhere in the haystack, so «крид malo» finds a track
// whose artist comes from the tag and whose title comes from the filename.
function searchTerms(q) {
  return normSearch(q).split(' ').filter(function(x) { return x; });
}

function matchesTerms(hay, terms) {
  for (var i = 0; i < terms.length; i++) {
    if (hay.indexOf(terms[i]) < 0) return false;
  }
  return true;
}

// Cached per track — rebuilt automatically whenever the catalog is reloaded.
function trackHay(t) {
  if (t._hay === undefined) {
    var fromFile = (t.file || '').replace(/^\d+[.\s]+/, '').replace(/\.[a-z0-9]{2,5}$/i, '');
    t._hay = normSearch(t.title + ' ' + t.artist + ' ' + t.album + ' ' + fromFile);
  }
  return t._hay;
}

function onSearchInput(q) {
  var btn = document.getElementById('searchClear');
  btn.classList.toggle('show', q.length > 0);
  clearTimeout(searchTimer);
  q = q.trim().toLowerCase();
  if (!q || !searchTerms(q).length) {
    filteredTracks = null;
    filteredAlbums = null;
    renderTracks();
    renderAlbums();
    document.getElementById('playlistHeader').textContent =
      activeTab === 'albums' ? albums.length + ' альбомов' : tracks.length + ' треков';
    return;
  }
  searchTimer = setTimeout(function() {
    var terms = searchTerms(q);
    // Filter tracks
    filteredTracks = [];
    for (var i = 0; i < tracks.length; i++) {
      if (matchesTerms(trackHay(tracks[i]), terms)) filteredTracks.push(i);
    }
    // Filter albums
    filteredAlbums = [];
    for (var a = 0; a < albums.length; a++) {
      var alb = albums[a];
      if (matchesTerms(normSearch(alb.name + ' ' + alb.artist), terms)) {
        filteredAlbums.push(a);
      } else {
        // Check if any track in album matches
        for (var ti = 0; ti < alb.tracks.length; ti++) {
          var t = tracks[alb.tracks[ti]];
          if (t && matchesTerms(trackHay(t), terms)) {
            filteredAlbums.push(a);
            break;
          }
        }
      }
    }
    renderTracks();
    renderAlbums();
    if (activeTab === 'albums') {
      document.getElementById('playlistHeader').textContent = filteredAlbums.length + ' / ' + albums.length + ' альбомов';
    } else {
      document.getElementById('playlistHeader').textContent = filteredTracks.length + ' / ' + tracks.length + ' треков';
    }
  }, 200);
}

function clearSearch() {
  var input = document.getElementById('searchInput');
  input.value = '';
  document.getElementById('searchClear').classList.remove('show');
  filteredTracks = null;
  filteredAlbums = null;
  renderTracks();
  renderAlbums();
  document.getElementById('playlistHeader').textContent =
    activeTab === 'albums' ? albums.length + ' альбомов' : tracks.length + ' треков';
  input.focus();
}

// ── Toast ──
var _toastTimer = null;
function showToast(msg) {
  // Service Worker подменяет неудачный /api/* ответом {error:'offline'} со
  // статусом 200, и этот код утекал в интерфейс как есть — пользователь видел
  // просто «offline». Переводим здесь, чтобы не править полтора десятка мест.
  if (msg === 'offline') msg = 'Нет связи с сервером — изменение не сохранено';
  var t = document.getElementById('toast');
  if (_toastTimer) clearTimeout(_toastTimer);
  t.textContent = msg;
  t.classList.add('show');
  var dur = Math.max(2500, Math.min(msg.length * 60, 5000));
  _toastTimer = setTimeout(function(){ t.classList.remove('show'); _toastTimer = null; }, dur);
}

// ── Meta search ──
var autoMetaEnabled = false;

function startMetaSearch() {
  var path = document.getElementById('folderSelect').value;
  if (!path) { showToast('Сначала выберите каталог'); return; }
  document.getElementById('metaConfirmOverlay').classList.add('show');
  document.getElementById('autoMetaCheck').checked = autoMetaEnabled;
}

function metaConfirmGo() {
  autoMetaEnabled = document.getElementById('autoMetaCheck').checked;
  document.getElementById('metaConfirmOverlay').classList.remove('show');
  var path = document.getElementById('folderSelect').value;
  if (path) doMetaSearch(path);
}

function metaConfirmClose() {
  autoMetaEnabled = document.getElementById('autoMetaCheck').checked;
  document.getElementById('metaConfirmOverlay').classList.remove('show');
}

function autoMetaForTrack(t) {
  if (!autoMetaEnabled || !t || !t.file) return;
  if (t.has_cover && t.artist && t.album) return;
  var path = document.getElementById('folderSelect').value;
  if (!path) return;
  fetch('/api/meta/single', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder: path, file: t.file})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok && d.updated) {
      showToast('Meta: ' + (d.artist || '') + ' — ' + (d.album || ''));
      // Refresh current track info
      if (currentIdx >= 0 && tracks[currentIdx].file === t.file) {
        if (d.artist) { tracks[currentIdx].artist = d.artist; tracks[currentIdx]._hay = undefined; }
        if (d.album) tracks[currentIdx].album = d.album;
        if (d.has_cover) tracks[currentIdx].has_cover = true;
        document.getElementById('trackArtist').textContent = d.artist || '';
        updateMediaSession(tracks[currentIdx]);
        if (d.has_cover) {
          var img = document.getElementById('vinylCover');
          img.src = '/api/cover/' + encodeURIComponent(t.file) + '?t=' + Date.now();
          img.style.display = '';
          document.getElementById('vinylPlaceholder').style.display = 'none';
          img.onload = function() { extractColor(img); };
        }
      }
      renderTracks();
    }
  });
}

function doMetaSearch(path) {
  fetch('/api/meta/start', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({path: path}) })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.already_running) {
      showToast('Поиск уже идёт');
      document.getElementById('metaOverlay').classList.add('show');
      pollMeta();
      return;
    }
    if (d.ok) {
      document.getElementById('metaOverlay').classList.add('show');
      document.getElementById('metaLog').textContent = 'Запуск...';
      document.getElementById('metaBarFill').style.width = '0%';
      document.getElementById('metaProgress').textContent = '';
      pollMeta();
    } else {
      showToast(d.error || 'Ошибка');
    }
  });
}

function cancelMeta() {
  fetch('/api/meta/cancel', {method:'POST'});
  showToast('Отменяю...');
}

function pollMeta() {
  fetch('/api/meta/status').then(function(r) { return r.json(); }).then(function(d) {
    var pct = d.total > 0 ? Math.round(d.progress / d.total * 100) : 0;
    document.getElementById('metaBarFill').style.width = pct + '%';
    document.getElementById('metaProgress').textContent =
      d.total > 0 ? d.progress + ' / ' + d.total + ' (' + pct + '%)' : '';
    document.getElementById('metaLog').textContent = d.log.join('\n');
    document.getElementById('metaLog').scrollTop = document.getElementById('metaLog').scrollHeight;
    if (d.running) setTimeout(pollMeta, 800);
    else if (d.done) {
      document.getElementById('metaProgress').textContent = 'Сканирование завершено';
      // Load proposals for review
      loadMetaProposals();
    }
  });
}

function loadMetaProposals() {
  fetch('/api/meta/proposals', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
  .then(function(r){return r.json()}).then(function(d) {
    if (!d.ok || !d.proposals.length) {
      document.getElementById('metaProposals').style.display = 'none';
      showToast('Нет предложений для обновления');
      return;
    }
    document.getElementById('metaProposals').style.display = '';
    renderMetaProposals(d.proposals);
  });
}

function renderMetaProposals(proposals) {
  var html = '';
  for (var i = 0; i < proposals.length; i++) {
    var p = proposals[i];
    var changes = [];
    if (p.new_artist && p.new_artist !== p.old_artist && !p.old_artist) changes.push('артист: <b>' + esc(p.new_artist) + '</b>');
    if (p.new_album && p.new_album !== p.old_album && !p.old_album) changes.push('альбом: <b>' + esc(p.new_album) + '</b>');
    if (p.new_has_cover && !p.old_has_cover) changes.push('+ обложка');
    if (!changes.length) changes.push('обновление данных');
    html += '<label style="display:flex;align-items:flex-start;gap:8px;padding:8px 10px;border-bottom:1px solid rgba(255,255,255,0.04);cursor:pointer">'
      + '<input type="checkbox" checked class="meta-proposal-check" data-file="' + esc(p.file) + '" style="accent-color:#e94560;margin-top:3px;flex-shrink:0">'
      + '<div style="flex:1;min-width:0;font-size:12px">'
      + '<div style="color:#eee;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(p.file) + '</div>'
      + '<div style="color:rgba(255,255,255,0.4);margin-top:2px">' + changes.join(' · ') + '</div>'
      + '</div></label>';
  }
  document.getElementById('metaProposalList').innerHTML = html;
}

function metaToggleAll(checked) {
  var checks = document.querySelectorAll('.meta-proposal-check');
  for (var i = 0; i < checks.length; i++) checks[i].checked = checked;
}

function applyMetaProposals() {
  var checks = document.querySelectorAll('.meta-proposal-check:checked');
  if (!checks.length) { showToast('Ничего не выбрано'); return; }
  var files = [];
  for (var i = 0; i < checks.length; i++) files.push(checks[i].getAttribute('data-file'));
  var folder = document.getElementById('folderSelect').value;
  showConfirm('Применить метаданные к ' + files.length + ' трекам?', function() {
    document.getElementById('metaProposals').style.display = 'none';
    document.getElementById('metaLog').textContent = 'Применяю...';
    fetch('/api/meta/apply', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({folder: folder, files: files})})
    .then(function(r){return r.json()}).then(function(d) {
      if (d.ok) { pollMeta(); } else { showToast(d.error); }
    });
  }, 'Применить');
}

function closeMetaModal() {
  document.getElementById('metaOverlay').classList.remove('show');
}

// ── VK Download ──
function openVkModal() {
  document.getElementById('vkOverlay').classList.add('show');
  checkVkAuth();
  // Folder hint & mode validation
  var folder = document.getElementById('folderSelect').value;
  var hint = document.getElementById('vkFolderHint');
  var modeEl = document.getElementById('vkMode');
  if (folder) {
    var name = folder.split('/').pop() || folder;
    hint.textContent = 'Треки будут добавлены в каталог: ' + name;
    if (tracks.length === 0) {
      modeEl.innerHTML = '<option value="prepend">В начало</option>';
      modeEl.disabled = true;
    } else {
      modeEl.innerHTML = '<option value="prepend">В начало</option><option value="append">В конец</option>';
      modeEl.disabled = false;
    }
  } else {
    hint.textContent = 'Сначала выберите каталог';
  }
  // Local import is only meaningful when the browser runs on the server machine
  document.getElementById('localImportBlock').style.display = (isLocal && userRole !== 'demo') ? '' : 'none';
  updateLocalPosVis();
  if (vkPolling) pollVk();
}

function updateLocalPosVis() {
  var mode = document.getElementById('localMode').value;
  var posEl = document.getElementById('localPos');
  posEl.style.display = mode === 'position' ? '' : 'none';
  var maxPos = (tracks.length || 0) + 1;
  posEl.max = maxPos;
  if (parseInt(posEl.value, 10) > maxPos) posEl.value = maxPos;
  if (parseInt(posEl.value, 10) < 1 || isNaN(parseInt(posEl.value, 10))) posEl.value = 1;
}

function pickLocalFiles() {
  var folder = document.getElementById('folderSelect').value;
  if (!folder) { showToast('Выберите каталог'); return; }
  showToast('Открываю проводник...');
  fetch('/api/local/pick', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
  .then(function(r){return r.json()}).then(function(d) {
    if (!d.ok) { showToast(d.error || 'Ошибка'); return; }
    pollLocalPick();
  }).catch(function(){ showToast('Ошибка связи с сервером'); });
}

function pollLocalPick() {
  fetch('/api/local/pick/status').then(function(r){return r.json()}).then(function(d) {
    if (!d.ok) { showToast(d.error || 'Ошибка'); return; }
    if (d.picking) { setTimeout(pollLocalPick, 600); return; }
    if (!d.files || !d.files.length) { showToast('Файлы не выбраны'); return; }
    startLocalImport(d.files);
  }).catch(function(){ showToast('Ошибка связи с сервером'); });
}

function startLocalImport(files) {
  var folder = document.getElementById('folderSelect').value;
  var mode = document.getElementById('localMode').value;
  var position = parseInt(document.getElementById('localPos').value, 10) || 1;
  var runMeta = document.getElementById('localRunMeta').checked;
  fetch('/api/local/import', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({files:files, folder:folder, mode:mode, position:position, run_meta:runMeta})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.already_running) { showToast('Загрузка уже идёт'); pollVk(); return; }
    if (!d.ok) { showToast(d.error || 'Ошибка'); return; }
    document.getElementById('vkProgressSection').style.display = '';
    vkPolling = true;
    pollVk();
  }).catch(function(){ showToast('Ошибка связи с сервером'); });
}

function closeVkModal() {
  document.getElementById('vkOverlay').classList.remove('show');
}

var vkPolling = false;

function checkVkAuth() {
  fetch('/api/vk/status').then(function(r){return r.json()}).then(function(d) {
    if (!d.has_vk) {
      document.getElementById('vkAuthStatus').innerHTML = '<span style="color:#e94560">vkpymusic не установлен</span>';
      return;
    }
    if (d.authenticated) {
      document.getElementById('vkAuthStatus').innerHTML = '<span style="color:#52b788">VK авторизован</span>';
      document.getElementById('vkAuthForm').style.display = 'none';
    } else {
      document.getElementById('vkAuthStatus').innerHTML = '<span style="color:#e94560">Не авторизован</span> <button class="folder-btn folder-btn-primary" style="padding:4px 12px;font-size:11px;margin-left:8px" onclick="doVkAuth()">Войти</button>';
      document.getElementById('vkAuthForm').style.display = 'none';
    }
    if (d.running) {
      document.getElementById('vkProgressSection').style.display = '';
      vkPolling = true;
      pollVk();
    }
  });
}

function doVkAuth() {
  var url = 'https://oauth.vk.com/authorize?client_id=2685278&scope=audio&redirect_uri=https://oauth.vk.com/blank.html&response_type=token&v=5.131';
  window.open(url, '_blank');
  document.getElementById('vkAuthForm').style.display = '';
  document.getElementById('vkTokenInput').focus();
}

function submitVkToken() {
  var raw = document.getElementById('vkTokenInput').value.trim();
  if (!raw) return;
  fetch('/api/vk/auth', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url: raw})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok) {
      showToast('VK авторизован');
      checkVkAuth();
    } else {
      showToast(d.error || 'Ошибка');
    }
  });
}

function startVkDownload() {
  var folder = document.getElementById('folderSelect').value;
  if (!folder) { showToast('Выберите каталог'); return; }
  var raw = document.getElementById('vkUrls').value.trim();
  if (!raw) { showToast('Введите ссылки'); return; }
  var urls = raw.split('\n').map(function(s){return s.trim()}).filter(function(s){return s.length > 0});
  if (!urls.length) { showToast('Введите ссылки'); return; }

  var mode = document.getElementById('vkMode').value;
  var order = document.getElementById('vkOrder').value;
  var runMeta = document.getElementById('vkRunMeta').checked;

  fetch('/api/vk/download', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({urls:urls, folder:folder, order:order, mode:mode, run_meta:runMeta})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.already_running) { showToast('Загрузка уже идёт'); pollVk(); return; }
    if (!d.ok) { showToast(d.error || 'Ошибка'); return; }
    document.getElementById('vkProgressSection').style.display = '';
    vkPolling = true;
    pollVk();
  });
}

function pollVk() {
  fetch('/api/vk/status').then(function(r){return r.json()}).then(function(d) {
    var pct = d.total > 0 ? Math.round(d.progress / d.total * 100) : 0;
    document.getElementById('vkBarFill').style.width = pct + '%';
    document.getElementById('vkProgress').textContent =
      d.total > 0 ? d.progress + ' / ' + d.total + ' (' + pct + '%)' : '';
    document.getElementById('vkLog').textContent = d.log.join('\n');
    document.getElementById('vkLog').scrollTop = document.getElementById('vkLog').scrollHeight;
    if (d.running) setTimeout(pollVk, 800);
    else {
      vkPolling = false;
      if (d.done) {
        showToast('Загрузка завершена!');
        vkQueue = [];
        renderVkQueue();
        var curFolder = document.getElementById('folderSelect').value;
        if (curFolder) loadFolder(curFolder);
      }
    }
  });
}

function cancelVkDownload() {
  fetch('/api/vk/cancel', {method:'POST'});
  showToast('Отменяю загрузку...');
}

// ── VK Tabs & Search ──
function showImpTab(tab) {
  var tabs = document.querySelectorAll('.imp-tab');
  for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove('active');
  var btn = document.getElementById('impTab' + tab.charAt(0).toUpperCase() + tab.slice(1));
  if (btn) btn.classList.add('active');
  document.getElementById('impVk').style.display = tab === 'vk' ? '' : 'none';
  document.getElementById('impExternal').style.display = (tab !== 'vk' && tab !== 'search') ? '' : 'none';
  document.getElementById('impSearch').style.display = tab === 'search' ? '' : 'none';
}
// Keep old name for compat
function showVkTab(t) { showImpTab(t === 'playlist' ? 'vk' : 'search'); }

var impMatches = [];
var impOriginalTracks = []; // full track list from external platform
var impRetryTimer = null;
var impRetryTime = 0;

function importExternal() {
  var url = document.getElementById('impExtUrl').value.trim();
  if (!url) { showToast('Вставьте ссылку'); return; }
  document.getElementById('impExtStatus').textContent = 'Загружаю плейлист и ищу треки в VK...';
  document.getElementById('impMatchList').innerHTML = '';
  document.getElementById('impMatchActions').style.display = 'none';
  fetch('/api/import/parse', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url: url})})
  .then(function(r){return r.json()}).then(function(d) {
    if (!d.ok) { document.getElementById('impExtStatus').textContent = d.error || 'Ошибка'; return; }
    impMatches = d.matches || [];
    impOriginalTracks = d.matches ? d.matches.map(function(m) { return {artist: m.original_artist, title: m.original_title}; }) : [];
    updateImpStatus(d);
    renderImpMatches();
    if (impMatches.length) document.getElementById('impMatchActions').style.display = '';
    // If there was a captcha warning, start retry timer
    if (d.warning) startRetryTimer();
    else enableRetryBtn();
  });
}

function updateImpStatus(d) {
  var matched = impMatches.filter(function(m){return m.matched}).length;
  var total = impMatches.length;
  var status = 'Сопоставлено: ' + matched + ' из ' + total + ' треков';
  if (d && d.platform) status += ' (' + d.platform + ')';
  if (d && d.warning) status += '\n⚠ ' + d.warning;
  document.getElementById('impExtStatus').textContent = status;
  if (d && d.warning) showToast(d.warning);
}

function startRetryTimer() {
  var btn = document.getElementById('impRetryBtn');
  btn.disabled = true;
  impRetryTime = 15 * 60; // 15 minutes
  if (impRetryTimer) clearInterval(impRetryTimer);
  updateRetryLabel();
  impRetryTimer = setInterval(function() {
    impRetryTime--;
    if (impRetryTime <= 0) {
      clearInterval(impRetryTimer);
      impRetryTimer = null;
      enableRetryBtn();
    } else {
      updateRetryLabel();
    }
  }, 1000);
}

function updateRetryLabel() {
  var btn = document.getElementById('impRetryBtn');
  var m = Math.floor(impRetryTime / 60);
  var s = impRetryTime % 60;
  btn.textContent = 'Повторить (' + m + ':' + ('0'+s).slice(-2) + ')';
}

function enableRetryBtn() {
  var btn = document.getElementById('impRetryBtn');
  var unmatched = impMatches.filter(function(m){return !m.matched}).length;
  if (unmatched > 0) {
    btn.disabled = false;
    btn.textContent = 'Повторить (' + unmatched + ' ненайд.)';
  } else {
    btn.disabled = true;
    btn.textContent = 'Все найдены';
  }
}

function retryUnmatched() {
  // Collect unmatched tracks
  var unmatched = [];
  for (var i = 0; i < impMatches.length; i++) {
    if (!impMatches[i].matched) {
      unmatched.push({artist: impMatches[i].original_artist, title: impMatches[i].original_title, idx: i});
    }
  }
  if (!unmatched.length) { showToast('Все треки найдены'); return; }
  document.getElementById('impExtStatus').textContent = 'Повторный поиск ' + unmatched.length + ' треков...';
  document.getElementById('impRetryBtn').disabled = true;
  document.getElementById('impRetryBtn').textContent = 'Ищу...';

  // Send only unmatched for re-search
  var queries = unmatched.map(function(u) { return {artist: u.artist, title: u.title}; });
  fetch('/api/import/retry', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({tracks: queries})})
  .then(function(r){return r.json()}).then(function(d) {
    if (!d.ok) { showToast(d.error || 'Ошибка'); startRetryTimer(); return; }
    // Update matches in place
    var newMatches = d.matches || [];
    for (var i = 0; i < newMatches.length; i++) {
      var origIdx = unmatched[i].idx;
      if (newMatches[i].matched) {
        impMatches[origIdx] = newMatches[i];
      }
    }
    updateImpStatus(d);
    renderImpMatches();
    if (d.warning) startRetryTimer();
    else enableRetryBtn();
  });
}

function renderImpMatches() {
  var html = '';
  for (var i = 0; i < impMatches.length; i++) {
    var m = impMatches[i];
    var dur = m.vk_duration ? Math.floor(m.vk_duration/60)+':'+('0'+m.vk_duration%60).slice(-2) : '';
    if (m.matched && m.has_url) {
      html += '<div class="imp-match" draggable="true" data-ii="'+i+'" ondragstart="impDragStart(event,'+i+')" ondragover="impDragOver(event,'+i+')" ondrop="impDrop(event,'+i+')" ondragend="impDragEnd(event)">'
        + '<input type="checkbox" checked class="imp-check" data-idx="'+i+'" style="accent-color:#e94560;margin-top:2px;flex-shrink:0">'
        + '<div class="orig"><div>'+esc(m.original_artist)+' — '+esc(m.original_title)+'</div></div>'
        + '<div class="vk"><div>'+esc(m.vk_artist)+' — '+esc(m.vk_title)+' <span style="color:rgba(255,255,255,0.2)">'+dur+'</span></div>'
        + '<button class="folder-btn folder-btn-secondary" style="padding:2px 6px;font-size:10px;margin-top:2px" onclick="reSearchTrack('+i+')">найти другую версию</button></div>'
        + '<span class="drag-handle" style="cursor:grab;color:rgba(255,255,255,0.15)">≡</span></div>';
    } else {
      html += '<div class="imp-match" style="opacity:0.4">'
        + '<input type="checkbox" disabled style="margin-top:2px;flex-shrink:0">'
        + '<div class="orig"><div>'+esc(m.original_artist)+' — '+esc(m.original_title)+'</div></div>'
        + '<div class="nomatch">Не найдено в VK</div></div>';
    }
  }
  document.getElementById('impMatchList').innerHTML = html;
}

function reSearchTrack(idx) {
  var m = impMatches[idx];
  var q = prompt('Поиск в VK:', m.original_artist + ' ' + m.original_title);
  if (!q) return;
  fetch('/api/import/re_search', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({query: q})})
  .then(function(r){return r.json()}).then(function(d) {
    if (!d.ok || !d.results.length) { showToast('Не найдено'); return; }
    // Show options
    var html = '';
    for (var i = 0; i < d.results.length; i++) {
      var r = d.results[i];
      html += '<div style="padding:6px;cursor:pointer;border-bottom:1px solid rgba(255,255,255,0.04)" onclick="pickReSearch('+idx+','+i+')" class="playlist-item">'
        + '<div class="info"><div class="name" style="font-size:11px">'+esc(r.vk_title)+'</div>'
        + '<div class="artist" style="font-size:10px">'+esc(r.vk_artist)+'</div></div></div>';
    }
    document.getElementById('impMatchList').innerHTML = html;
    window._reSearchResults = d.results;
    window._reSearchIdx = idx;
  });
}

function pickReSearch(idx, resultIdx) {
  var r = window._reSearchResults[resultIdx];
  impMatches[idx].vk_artist = r.vk_artist;
  impMatches[idx].vk_title = r.vk_title;
  impMatches[idx].vk_id = r.vk_id;
  impMatches[idx].vk_duration = r.vk_duration;
  impMatches[idx].has_url = r.has_url;
  impMatches[idx].matched = true;
  renderImpMatches();
}

function impToggleAll(checked) {
  var checks = document.querySelectorAll('.imp-check');
  for (var i = 0; i < checks.length; i++) checks[i].checked = checked;
}

// Drag reorder for import matches
var impDragIdx = null;
function impDragStart(e,i) { impDragIdx = i; e.target.closest('.imp-match').style.opacity='0.4'; }
function impDragEnd(e) { impDragIdx = null; var el = e.target.closest('.imp-match'); if(el) el.style.opacity=''; }
function impDragOver(e,i) { e.preventDefault(); }
function impDrop(e,targetIdx) {
  e.preventDefault();
  if (impDragIdx === null || impDragIdx === targetIdx) return;
  var item = impMatches.splice(impDragIdx, 1)[0];
  impMatches.splice(targetIdx, 0, item);
  impDragIdx = null;
  renderImpMatches();
}

function downloadImportMatches() {
  var checks = document.querySelectorAll('.imp-check:checked');
  if (!checks.length) { showToast('Выберите треки'); return; }
  var folder = document.getElementById('folderSelect').value;
  if (!folder) { showToast('Выберите каталог'); return; }
  var ids = [];
  for (var i = 0; i < checks.length; i++) {
    var idx = parseInt(checks[i].getAttribute('data-idx'));
    if (impMatches[idx] && impMatches[idx].vk_id) ids.push(impMatches[idx].vk_id);
  }
  if (!ids.length) { showToast('Нет доступных треков'); return; }
  var mode = document.getElementById('impExtMode').value;
  var runMeta = document.getElementById('impExtMeta').checked;
  fetch('/api/vk/download_tracks', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder: folder, track_ids: ids, mode: mode, run_meta: runMeta})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok) { document.getElementById('vkProgressSection').style.display=''; vkPolling=true; pollVk(); }
    else showToast(d.error || 'Ошибка');
  });
}

var vkSearchResults = [];
var vkQueue = []; // [{id, title, artist, duration}, ...]

function vkSearchTracks() {
  var q = document.getElementById('vkSearchQuery').value.trim();
  if (!q) return;
  document.getElementById('vkSearchResults').innerHTML = '<div style="padding:12px;color:rgba(255,255,255,0.3);text-align:center">Поиск...</div>';
  fetch('/api/vk/search', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({query: q})})
  .then(function(r){return r.json()}).then(function(d) {
    if (!d.ok) { showToast(d.error || 'Ошибка'); return; }
    vkSearchResults = d.results || [];
    renderVkSearchResults();
  });
}

function renderVkSearchResults() {
  var html = '';
  if (!vkSearchResults.length) {
    html = '<div style="padding:12px;color:rgba(255,255,255,0.3);text-align:center">Ничего не найдено</div>';
  }
  var queueIds = vkQueue.map(function(q){return q.id});
  for (var i = 0; i < vkSearchResults.length; i++) {
    var r = vkSearchResults[i];
    var id = r.owner_id + '_' + r.track_id;
    var dur = Math.floor(r.duration/60) + ':' + ('0'+r.duration%60).slice(-2);
    var inQueue = queueIds.indexOf(id) >= 0;
    var avail = r.has_url;
    html += '<div class="playlist-item" style="' + (!avail ? 'opacity:0.3' : '') + '">'
      + '<div class="info" style="flex:1;min-width:0"><div class="name">' + esc(r.title) + '</div>'
      + '<div class="artist">' + esc(r.artist) + ' · ' + dur + '</div></div>'
      + (avail ? '<button class="folder-btn ' + (inQueue ? 'folder-btn-primary' : 'folder-btn-secondary') + '" style="padding:4px 10px;font-size:11px;flex-shrink:0" onclick="toggleVkQueue(' + i + ')">' + (inQueue ? '✓' : '+') + '</button>' : '')
      + '</div>';
  }
  document.getElementById('vkSearchResults').innerHTML = html;
}

function toggleVkQueue(idx) {
  var r = vkSearchResults[idx];
  var id = r.owner_id + '_' + r.track_id;
  var pos = -1;
  for (var i = 0; i < vkQueue.length; i++) { if (vkQueue[i].id === id) { pos = i; break; } }
  if (pos >= 0) {
    vkQueue.splice(pos, 1);
  } else {
    vkQueue.push({id: id, title: r.title, artist: r.artist, duration: r.duration});
  }
  renderVkSearchResults();
  renderVkQueue();
}

function renderVkQueue() {
  var el = document.getElementById('vkQueue');
  var section = document.getElementById('vkQueueSection');
  if (!vkQueue.length) { section.style.display = 'none'; return; }
  section.style.display = '';
  var html = '';
  for (var i = 0; i < vkQueue.length; i++) {
    var q = vkQueue[i];
    var dur = Math.floor(q.duration/60) + ':' + ('0'+q.duration%60).slice(-2);
    html += '<div class="playlist-item" draggable="true" data-qi="' + i + '"'
      + ' ondragstart="vkQueueDragStart(event,' + i + ')" ondragover="vkQueueDragOver(event,' + i + ')" ondrop="vkQueueDrop(event,' + i + ')" ondragend="vkQueueDragEnd(event)">'
      + '<span class="drag-handle" style="cursor:grab;color:rgba(255,255,255,0.2);margin-right:6px">≡</span>'
      + '<div class="info" style="flex:1;min-width:0"><div class="name" style="font-size:12px">' + esc(q.title) + '</div>'
      + '<div class="artist" style="font-size:11px">' + esc(q.artist) + ' · ' + dur + '</div></div>'
      + '<button class="folder-btn-icon" style="width:22px;height:22px;font-size:11px;color:#e94560;flex-shrink:0" onclick="removeVkQueue(' + i + ')">&times;</button>'
      + '</div>';
  }
  el.innerHTML = html;
}

// Queue drag reorder
var vkQDragIdx = null;
function vkQueueDragStart(e, i) { vkQDragIdx = i; e.dataTransfer.effectAllowed = 'move'; e.target.closest('.playlist-item').style.opacity = '0.4'; }
function vkQueueDragEnd(e) { vkQDragIdx = null; e.target.closest('.playlist-item').style.opacity = ''; }
function vkQueueDragOver(e, i) { e.preventDefault(); }
function vkQueueDrop(e, targetIdx) {
  e.preventDefault();
  if (vkQDragIdx === null || vkQDragIdx === targetIdx) return;
  var item = vkQueue.splice(vkQDragIdx, 1)[0];
  vkQueue.splice(targetIdx, 0, item);
  vkQDragIdx = null;
  renderVkQueue();
}
function removeVkQueue(i) { vkQueue.splice(i, 1); renderVkSearchResults(); renderVkQueue(); }

function vkDownloadSelected() {
  if (!vkQueue.length) { showToast('Добавьте треки в очередь'); return; }
  var folder = document.getElementById('folderSelect').value;
  if (!folder) { showToast('Выберите каталог'); return; }
  var ids = vkQueue.map(function(q){return q.id});
  var mode = document.getElementById('vkSearchMode').value;
  var runMeta = document.getElementById('vkSearchMeta').checked;
  fetch('/api/vk/download_tracks', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder: folder, track_ids: ids, mode: mode, run_meta: runMeta})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok) {
      document.getElementById('vkProgressSection').style.display = '';
      vkPolling = true;
      pollVk();
    } else {
      showToast(d.error || 'Ошибка');
    }
  });
}

// ── Mobile view toggle ──
// ── Desktop sidebar toggle ──
function toggleSidebar() {
  var app = document.querySelector('.app');
  var collapsed = app.classList.toggle('sidebar-collapsed');
  document.getElementById('sidebarIcon').innerHTML = collapsed
    ? '<path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/>'
    : '<path d="M8.59 16.59L10 18l6-6-6-6-1.41 1.41L13.17 12z"/>';
}

function mobileShow(view) {
  document.body.classList.remove('mobile-view-vinyl', 'mobile-view-playlist');
  document.body.classList.add('mobile-view-' + view);
  document.getElementById('btnVinyl').classList.toggle('active', view === 'vinyl');
  document.getElementById('btnPlaylist').classList.toggle('active', view === 'playlist');
  document.getElementById('toggleBg').classList.toggle('right', view === 'playlist');
  // Hide play/next buttons on vinyl view — player has its own controls
  var pb = document.getElementById('mobilePlayBtn');
  var nb = document.getElementById('mobileNextBtn');
  if (view === 'vinyl') {
    if (pb) pb.classList.remove('show');
    if (nb) nb.classList.remove('show');
  } else if (currentIdx >= 0) {
    if (pb) pb.classList.add('show');
    if (nb) nb.classList.add('show');
  }
}

function mobileToggleView() {
  var isPlaylist = document.body.classList.contains('mobile-view-playlist');
  mobileShow(isPlaylist ? 'vinyl' : 'playlist');
}

// Set default mobile view
if (window.innerWidth <= 768) {
  document.body.classList.add('mobile-view-playlist');
}

// keyboard
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
  if (e.code === 'ArrowRight') { nextTrack(); }
  if (e.code === 'ArrowLeft') { prevTrack(); }
  if (e.code === 'Escape') { closeMetaModal(); }
});

// ── Profile & Admin ──
// Показываем сборку страницы и сборку сервера рядом. Если они разошлись —
// значит браузер держит старую страницу из кэша Service Worker, и виноват не
// сервер. Без этого «я обновил, но ничего не поменялось» приходится
// диагностировать вслепую.
function renderBuildInfo() {
  var el = document.getElementById('buildInfo');
  if (!el) return;
  var mine = 'APP_BUILD_HASH';
  el.innerHTML = 'Сборка приложения: <b style="color:rgba(255,255,255,0.5)">' + mine + '</b>'
    + (_isOffline ? ' &middot; <span style="color:#e94560">офлайн</span>' : '');
  fetch('/api/version', {cache: 'no-store'}).then(function(r){ return r.json(); }).then(function(d) {
    if (!d || !d.version) return;
    var same = d.version === mine;
    el.innerHTML = 'Сборка приложения: <b style="color:rgba(255,255,255,0.5)">' + mine + '</b>'
      + '<br>Сборка сервера: <b style="color:' + (same ? 'rgba(255,255,255,0.5)' : '#e94560') + '">'
      + d.version + '</b>'
      + (same ? ' &middot; актуально'
              : '<br><span style="color:#e94560">Страница устарела — нажмите «Обновить приложение»</span>')
      + (_isOffline ? '<br><span style="color:#e94560">Сервер сейчас недоступен (офлайн-режим)</span>' : '');
  }).catch(function() {
    el.innerHTML += '<br><span style="color:#e94560">Сервер не отвечает</span>';
  });
}

// Пауза между записями важнее самих меток: разрыв в минуты на месте нажатия
// и есть доказательство того, что страницу заморозили.
function mediaLogLine(rec, prev) {
  function p2(n) { return (n < 10 ? '0' : '') + n; }
  var d = new Date(rec.t);
  var gap = prev ? ' +' + (Math.round((rec.t - prev.t) / 100) / 10) + 's' : '';
  return p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds())
    + gap + '  ' + rec.e + (rec.x ? '  ' + rec.x : '');
}

function renderMediaLog() {
  var el = document.getElementById('mediaLogText');
  if (!el) return;
  _mediaLog = null;
  var log = mediaLogAll();
  if (!log.length) { el.textContent = 'Пусто. Запустите трек, заблокируйте экран, нажмите паузу и плей — потом вернитесь сюда.'; return; }
  var out = [];
  for (var i = 0; i < log.length; i++) out.push(mediaLogLine(log[i], i ? log[i - 1] : null));
  el.textContent = out.join('\n');
  el.scrollTop = el.scrollHeight;
}

function toggleMediaLog() {
  var box = document.getElementById('mediaLogBox');
  if (!box) return;
  var open = box.style.display === 'none';
  box.style.display = open ? 'block' : 'none';
  if (open) { renderMediaLog(); renderScratchBtn(); renderRecoverBtn(); }
}

function copyMediaLog() {
  var el = document.getElementById('mediaLogText');
  if (!el) return;
  var text = el.textContent || '';
  // Safari отдаёт clipboard API не во всех контекстах, поэтому запасной путь
  // через скрытое поле и execCommand остаётся обязательным.
  function fallback() {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.setSelectionRange(0, ta.value.length);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    showToast(ok ? 'Журнал скопирован' : 'Не удалось скопировать');
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function() {
      showToast('Журнал скопирован');
    }, fallback);
    return;
  }
  fallback();
}

function clearMediaLog() {
  _mediaLog = [];
  lsSet('_vc_medialog', '[]');
  renderMediaLog();
}

// ── Настройки графики ──
// Каждый пункт снимает свой вид постоянной работы. Хранятся отдельно от
// серверных настроек: это свойство устройства, а не учётной записи — на
// ноутбуке и на телефоне разумны разные значения.
var perfCfg = {pauseBlur: false, noBlur: false, bgStatic: false, radioStatic: false};

function perfLoad() {
  try {
    var saved = JSON.parse(localStorage.getItem('_vc_perf') || 'null');
    if (saved) for (var k in perfCfg) if (saved[k] !== undefined) perfCfg[k] = !!saved[k];
  } catch (e) {}
  perfApply();
}

function perfApply() {
  var root = document.documentElement;
  root.classList.toggle('perf-noblur', perfCfg.noBlur);
  root.classList.toggle('perf-radiostatic', perfCfg.radioStatic);
  _bgForceDraw = true;        // при выключении статики фон надо перерисовать
  syncUiActive();             // правило простоя зависит от pauseBlur
  var ids = {pauseBlur: 'perfPauseBlur', noBlur: 'perfNoBlur',
             bgStatic: 'perfBgStatic', radioStatic: 'perfRadioStatic'};
  for (var k in ids) {
    var el = document.getElementById(ids[k]);
    if (el) el.checked = perfCfg[k];
  }
}

function perfSet(key, on) {
  perfCfg[key] = !!on;
  lsSet('_vc_perf', JSON.stringify(perfCfg));
  perfApply();
}

// Раздел свёрнут по умолчанию: настройки редкие, а профиль открывают ради
// другого. Состояние не запоминаем — каждое открытие профиля начинается со
// свёрнутого вида.
function perfToggleOpen() {
  var body = document.getElementById('perfBody');
  var btn = document.getElementById('perfToggle');
  var open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  btn.classList.toggle('open', open);
}

function openProfile() {
  perfApply();
  document.getElementById('perfBody').style.display = 'none';
  document.getElementById('perfToggle').classList.remove('open');
  document.getElementById('profileUser').textContent = 'Пользователь: ' + currentUser;
  document.getElementById('profOldPw').value = '';
  document.getElementById('profNewPw').value = '';
  // Show cache stats
  var count = Object.keys(cachedFiles).length;
  var infoEl = document.getElementById('profileCacheInfo');
  infoEl.textContent = count ? count + ' треков в кэше' : 'Кэш пуст';
  renderBuildInfo();
  document.getElementById('profileOverlay').classList.add('show');
  // Calculate cache size asynchronously
  if (count) {
    try {
      openCacheDB(function(db) {
        try {
          var tx = db.transaction('audio', 'readonly');
          var store = tx.objectStore('audio');
          var req = store.openCursor();
          var totalBytes = 0;
          req.onsuccess = function(e) {
            var cursor = e.target.result;
            if (cursor) {
              var val = cursor.value;
              if (val) totalBytes += (val.byteLength || val.size || 0);
              cursor.continue();
            } else {
              var sizeStr;
              if (totalBytes >= 1024 * 1024 * 1024) {
                sizeStr = (totalBytes / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
              } else {
                sizeStr = (totalBytes / (1024 * 1024)).toFixed(0) + ' MB';
              }
              infoEl.textContent = count + ' треков в кэше (' + sizeStr + ')';
            }
          };
          req.onerror = function() {
            infoEl.textContent = count + ' треков в кэше';
          };
        } catch(ex) {
          infoEl.textContent = count + ' треков в кэше';
        }
      });
    } catch(ex) {}
  }
}

function changeMyPassword() {
  var old_pw = document.getElementById('profOldPw').value;
  var new_pw = document.getElementById('profNewPw').value;
  if (!old_pw || !new_pw) { showToast('Заполните оба поля'); return; }
  fetch('/api/profile/change_password', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({old_password: old_pw, new_password: new_pw})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok) { showToast('Пароль изменён'); document.getElementById('profileOverlay').classList.remove('show'); }
    else showToast(d.error || 'Ошибка');
  });
}

function doLogout() {
  fetch('/api/auth/logout', {method:'POST'}).then(function() {
    // Clear only app caches (preserve audio IndexedDB)
    if ('caches' in window) {
      caches.keys().then(function(names) {
        return Promise.all(names.filter(function(n){return n.startsWith('app-')}).map(function(n) { return caches.delete(n); }));
      }).then(function() { window.location.reload(); });
    } else {
      window.location.reload();
    }
  });
}

function openAdmin() {
  if (!isAdmin) return;
  document.getElementById('adminOverlay').classList.add('show');
  loadAdminUsers();
  // Load current music root
  fetch('/api/config').then(function(r){return r.json()}).then(function(cfg) {
    document.getElementById('adminMusicRoot').value = cfg.music_root || '';
  });
}

function saveMusicRoot() {
  var root = document.getElementById('adminMusicRoot').value.trim();
  if (!root) { showToast('Введите путь'); return; }
  fetch('/api/admin/set_music_root', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({music_root: root})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok) showToast('Корневая папка сохранена'); else showToast(d.error || 'Ошибка');
  });
}

function loadAdminUsers() {
  fetch('/api/admin/users').then(function(r){return r.json()}).then(function(d) {
    var html = '';
    var users = d.users || [];
    for (var i = 0; i < users.length; i++) {
      var u = users[i];
      var foldersHtml = '';
      for (var fi = 0; fi < u.folders.length; fi++) {
        var fname = u.folders[fi].split('/').pop() || u.folders[fi];
        foldersHtml += '<span style="display:inline-flex;align-items:center;gap:2px;background:rgba(255,255,255,0.06);padding:2px 8px;border-radius:4px;font-size:10px;margin:1px">'
          + esc(fname)
          + '<button style="background:none;border:none;color:rgba(255,255,255,0.3);cursor:pointer;font-size:10px;padding:0 2px" onclick="event.stopPropagation();adminRemoveFolder(\'' + esc(u.username) + '\',\'' + u.folders[fi].replace(/\\/g,'\\\\').replace(/'/g,"\\'") + '\')">&times;</button></span>';
      }
      html += '<div style="padding:10px;border-bottom:1px solid rgba(255,255,255,0.06)">'
        + '<div style="display:flex;align-items:center;gap:8px">'
        + '<div style="flex:1"><b>' + esc(u.username) + '</b>'
        + ' <span style="color:' + (u.role==='admin'?'#e94560':u.role==='demo'?'#e9a545':'#52b788') + ';font-size:10px">' + (u.role||'user') + '</span></div>'
        + '<button class="folder-btn-icon admin-pw-btn" onclick="adminChangePassword(\'' + esc(u.username) + '\')" title="Сменить пароль"><img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIGZpbGw9IiM4ODgiIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyLjY1IDEwYTYgNiAwIDEgMCAwIDRIMTd2M2gzdi0zaDJ2LTRoLTkuMzV6TTcgMTRhMiAyIDAgMSAxIDAtNCAyIDIgMCAwIDEgMCA0eiIvPjwvc3ZnPg=="></button>'
        + '<button class="folder-btn-icon" style="width:26px;height:26px;font-size:13px" onclick="adminAddFolder(\'' + esc(u.username) + '\')" title="Добавить каталог">+</button>'
        + (u.is_admin ? '' : '<button class="folder-btn-icon" style="width:26px;height:26px;font-size:13px;color:#e94560" onclick="adminDeleteUser(\'' + esc(u.username) + '\')" title="Удалить">&times;</button>')
        + '</div>'
        + (u.folders.length ? '<div style="margin-top:6px">' + foldersHtml + '</div>' : '<div style="font-size:10px;color:rgba(255,255,255,0.2);margin-top:4px">Нет каталогов</div>')
        + '</div>';
    }
    document.getElementById('adminUserList').innerHTML = html || '<div style="color:rgba(255,255,255,0.3);padding:12px">Нет пользователей</div>';
  });
}

// ── Новинки артистов ──
// Раздел строится от библиотеки: сервер ранжирует артистов по числу треков и
// свежести их добавления, ходит за релизами в iTunes (запасной — Deezer) и
// отмечает то, чего в каталоге ещё нет. Ничего не зашито: пополнили библиотеку —
// поменялся и список отслеживаемых артистов.
// В DROPS две ленты: NEW — новинки артистов библиотеки, 4YOU — остальной их
// каталог. Растут они из одного серверного кэша артистов, но состояние у
// каждой своё: данные, порция, набор узлов карточек и зеркало. Общий набор
// узлов означал бы, что переключение подвкладки пересоздаёт все <img> и
// обложки грузятся заново — ровно то, от чего мы уходили.
var REL_TABS = ['new', 'foryou'];
var _relTab = 'new';
var relFeeds = {
  'new':    {mode: 'new',    mirrorKey: 'feed',     lsKey: '_vc_rellimit'},
  'foryou': {mode: 'foryou', mirrorKey: 'feed4you', lsKey: '_vc_fylimit'}
};

function relF(tab) { return relFeeds[tab || _relTab]; }

// Развёрнутый релиз запоминаем по устойчивому ключу, а не по индексу в массиве:
// лента перестраивается при каждом обновлении, порядок и состав меняются, и
// индекс начинает указывать на чужой альбом — список треков «перепрыгивал» на
// соседнюю карточку.
function relKey(it) { return (it.source || 'itunes') + ':' + it.rid; }

function relSpinner() {
  document.getElementById('newList').innerHTML =
    '<div style="display:flex;flex-direction:column;align-items:center;padding:40px 20px;color:rgba(255,255,255,0.3)">'
    + '<div class="loading-spinner"></div><div style="margin-top:12px;font-size:13px">Собираю новинки...</div></div>';
}

// Лента из зеркала: у копии нет прогресса проверки, всё остальное — как у
// серверного ответа, поэтому renderReleases не различает их.
function relFeedFromMirror(f) {
  return {items: f.items, updated_at: f.updated_at, saved_at: f.saved_at,
          found: f.found, artists_total: f.artists_total, artists_checked: f.artists_checked,
          refreshing: false, progress: {done: 0, total: 0}};
}

function relEsc(s) { return esc(s || ''); }

function loadReleases(silent, tab) {
  // Раньше здесь стоял выход по _isOffline — из-за него в офлайне мы даже не
  // доходили до зеркала, ради которого всё и делалось. Запрос всё равно
  // мгновенный: Service Worker отвечает {error:'offline'} не ходя в сеть.
  tab = tab || _relTab;
  var f = relFeeds[tab];
  if (!f || f.loading) return;
  f.loading = true;
  var mine = function() { return tab === _relTab; };   // рисуем только активную
  // Спиннер — только когда показать нечего. Есть зеркало — рисуем его, не
  // дожидаясь сервера: копия уже собрана и отличается от свежей ленты лишь
  // временем проверки. Раньше зеркало читалось только в .catch(), поэтому при
  // живом сервере каждое открытие приложения начиналось со спиннера и лента
  // выглядела так, будто собирается заново.
  if (!silent && !f.data && !f.hasMirror && mine()) relSpinner();
  if (!f.data) {
    relLoadMirror(tab, function(m) {
      if (f.data) return;                   // сеть успела первой — не затираем
      if (m && m.items && m.items.length) {
        f.data = relFeedFromMirror(m);
        if (mine()) renderReleases();
      } else if (!silent && mine()) {
        relSpinner();                       // зеркала не оказалось
      }
    });
  }
  var wasPolling = !!f.poll;
  fetch('/api/releases?limit=' + f.limit + (f.mode === 'new' ? '' : '&mode=' + f.mode))
      .then(function(r){ return r.json(); }).then(function(d) {
    f.loading = false;
    if (!d || d.error) throw new Error('offline');
    f.offline = false;
    f.data = d;
    if (mine()) renderReleases();
    relSaveMirror(tab);
    relSaveStarredLocal();
    relFlushStars();          // отметки, поставленные без сервера
    if (f.poll) { clearTimeout(f.poll); f.poll = null; }
    if (d.refreshing) f.poll = setTimeout(function(){ loadReleases(true, tab); }, 4000);
    // Проверка перебирает кэш артистов, из которого растут обе ленты. Соседнюю
    // не перезапрашиваем сразу (она может быть даже не открыта) — просто метим
    // как устаревшую, и она перечитается при следующем показе.
    else if (wasPolling) relInvalidate(tab);
  }).catch(function() {
    f.loading = false;
    // Сервера нет — показываем последнюю синхронизированную копию, а не пустоту.
    relLoadMirror(tab, function(m) {
      f.offline = true;
      if (m && m.items && m.items.length) {
        f.data = relFeedFromMirror(m);
        if (mine()) renderReleases();
      } else if (!f.data && mine()) {
        document.getElementById('newList').innerHTML =
          '<div class="rel-note" style="text-align:center;padding:32px 20px">Сервер недоступен, '
          + 'а сохранённой копии дропов ещё нет.<br><button class="folder-btn folder-btn-primary" '
          + 'style="margin-top:14px;font-size:12px" onclick="refreshReleases()">Проверить через iTunes</button></div>';
      }
      syncNewTabVisibility();
    });
  });
}

// Соседние ленты после проверки артистов: данные сбрасываем, зеркало нет —
// при открытии копия нарисуется мгновенно, а сеть догонит.
function relInvalidate(exceptTab) {
  for (var i = 0; i < REL_TABS.length; i++) {
    var t = REL_TABS[i];
    if (t === exceptTab) continue;
    // Ленту, на которую сейчас смотрят, гасить нельзя: renderReleases с пустыми
    // данными выходит сразу, и на экране остались бы карточки, которых уже нет
    // в состоянии. Её перезапрашиваем на месте.
    if (t === _relTab && relFeeds[t].data) loadReleases(true, t);
    else relFeeds[t].data = null;
  }
}

// Переключение подвкладки. Раскрытый релиз относился к прежней ленте, поэтому
// превью останавливаем; узлы карточек у лент раздельные, так что возврат
// назад уже не грузит обложки заново.
function showRelTab(tab) {
  if (tab === _relTab || !relFeeds[tab]) return;
  stopPreview();
  _relTab = tab;
  lsSet('_vc_reltab', tab);
  var scroller = document.getElementById('newList');
  if (scroller) scroller.scrollTop = 0;
  syncRelHeader();
  if (relF().data) renderReleases();
  else { if (scroller) scroller.innerHTML = ''; loadReleases(); }
}

// Заголовок строки: количество слева, подвкладки справа.
function syncRelHeader() {
  var d = relF().data;
  var n = d && d.items ? d.items.length : 0;
  document.getElementById('playlistHeader').textContent =
    d ? (n + ' ' + relPlural(n, 'дроп', 'дропа', 'дропов')) : 'Дропы';
  var btnNew = document.getElementById('relTabNew'), btnFy = document.getElementById('relTabFy');
  if (btnNew) btnNew.className = 'rel-subtab' + (_relTab === 'new' ? ' active' : '');
  if (btnFy) btnFy.className = 'rel-subtab' + (_relTab === 'foryou' ? ' active' : '');
}

// Раздел живёт только онлайн: данные приходят с сервера, а офлайн вкладка
// показывала бы пустоту. Прячем её и уводим с неё, если она была открыта.
function syncNewTabVisibility() {
  var tab = document.getElementById('tabNew');
  if (!tab) return;
  // Вкладку прячем только если и сервера нет, и показать нечего.
  var keep = !_isOffline || relFeeds['new'].hasMirror || relFeeds['foryou'].hasMirror;
  tab.style.display = keep ? '' : 'none';
  if (!keep) {
    if (typeof stopPreview === 'function') stopPreview();
    if (activeTab === 'new') showTab('tracks');
  } else {
    updateReleasesBadge();   // вернулись онлайн — счётчик тоже возвращаем
  }
}

function updateReleasesBadge() {
  // Счётчик на вкладке убран намеренно: он почти всегда упирался в «99+» и не
  // нёс информации. Число видно в шапке списка.
}

// В NEW карточки разложены по свежести, в 4YOU это бессмысленно: там всё
// старее окна новинок и попало бы в одну кучу «Раньше». Поэтому у 4YOU группа
// одна на всю ленту — заголовок печатается один раз, при смене подписи.
function relGroupFor(it) {
  if (it.starred) return 'Избранное';
  return _relTab === 'foryou' ? 'Рекомендации' : relGroupOf(it.date);
}

function relGroupOf(dateStr) {
  var today = new Date(); today.setHours(0,0,0,0);
  var d = new Date(dateStr + 'T00:00:00');
  var days = Math.round((d - today) / 86400000);
  if (days > 0) return 'Скоро';
  if (days >= -30) return 'За месяц';
  if (days >= -90) return 'За три месяца';
  if (days >= -180) return 'За полгода';
  return 'Раньше';
}

function relPlural(n, one, few, many) {
  var d = n % 10, h = n % 100;
  if (d === 1 && h !== 11) return one;
  if (d >= 2 && d <= 4 && (h < 12 || h > 14)) return few;
  return many;
}

function relWhen(ts) {
  var mins = Math.round((Date.now() / 1000 - ts) / 60);
  if (mins < 1) return 'только что';
  if (mins < 60) return mins + ' мин назад';
  var h = Math.round(mins / 60);
  if (h < 24) return h + ' ч назад';
  return Math.round(h / 24) + ' дн назад';
}

function relDateLabel(dateStr) {
  var m = ['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
  var d = new Date(dateStr + 'T00:00:00');
  if (isNaN(d)) return dateStr;
  var s = d.getDate() + ' ' + m[d.getMonth()];
  var now = new Date();
  if (d.getFullYear() !== now.getFullYear()) s += ' ' + d.getFullYear();
  return s;
}

function renderReleases() {
  var box = document.getElementById('newList');
  var feed = relF();
  if (!feed.data) return;
  var releasesData = feed.data;              // ниже читаем только активную ленту
  var isFy = _relTab === 'foryou';
  var items = releasesData.items || [];

  // Три независимых блока: строка состояния и подвал меняются часто (прогресс
  // проверки), а карточки — почти никогда. Раньше всё это перерисовывалось
  // одним innerHTML, из-за чего <img> пересоздавались и обложки грузились
  // заново на каждый тик.
  if (!document.getElementById('relCards')) {
    box.innerHTML = '<div id="relStatus"></div><div id="relCards"></div><div id="relFooter"></div>';
  }
  var statusEl = document.getElementById('relStatus');
  var cardsEl = document.getElementById('relCards');
  var footEl = document.getElementById('relFooter');

  if (activeTab === 'new') syncRelHeader();

  if (releasesData.refreshing) {
    var p = releasesData.progress || {};
    var pct = p.total ? Math.round(p.done / p.total * 100) : 0;
    statusEl.innerHTML = '<div class="rel-note"><span style="color:#e9a545">&#9679;</span> Проверяю артистов'
      + (p.total ? ' — ' + p.done + ' из ' + p.total + ' (' + pct + '%)' : '...') + '</div>';
  } else {
    // Кнопка проверки живёт сверху: за ней не надо прокручивать сотни карточек.
    statusEl.innerHTML = '<div class="rel-top">'
      + '<span class="rel-top-note">'
      + (feed.offline && releasesData.saved_at
          ? '<span style="color:#e9a545">&#9679;</span> Копия от ' + relWhen(releasesData.saved_at)
          : (releasesData.updated_at ? 'Проверено ' + relWhen(releasesData.updated_at) : ''))
      + '</span>'
      + '<button class="folder-btn folder-btn-secondary rel-top-btn" onclick="refreshReleases()">Проверить</button>'
      + '</div>';
  }

  if (!items.length) {
    cardsEl.innerHTML = '<div class="rel-note" style="text-align:center;padding:32px 20px">'
      + (releasesData.refreshing
          ? 'Проверяю артистов — это занимает пару минут, можно уйти с вкладки.'
          : (releasesData.artists_checked
              ? (isFy
                  ? 'У артистов библиотеки не нашлось ничего, чего у вас ещё нет.'
                  : 'Новинок пока нет.')
              : (isFy ? 'Подборка растёт из того же кэша артистов, что и NEW. ' : 'Дропы ещё не собраны.<br>')
                + 'Проверка ходит в iTunes и Deezer по вашим артистам '
                + '(' + releasesData.artists_total + ' в библиотеке) и занимает пару минут. '
                + 'Сама она не запускается.'
                + '<br><button class="folder-btn folder-btn-primary" style="margin-top:14px;font-size:12px" '
                + 'onclick="refreshReleases()">Проверить новинки</button>'))
      + '</div>';
    feed.nodes = {};
    footEl.innerHTML = '';
    return;
  }

  // Узлы карточек переиспользуются, поэтому перерисовка дешёвая и обложки не
  // моргают — ни при клике, ни при отметке, ни при обновлении ленты.
  syncReleaseCards(items);
  applyExpansion();

  var more = (releasesData.found || 0) - items.length;
  footEl.innerHTML =
       (more > 0 && !feed.offline
        ? '<div style="padding:4px 8px 0"><button class="folder-btn folder-btn-secondary" '
          + 'style="width:100%;font-size:12px;padding:10px" onclick="relShowMore()">'
          + 'Показать ещё ' + Math.min(REL_PAGE, more) + ' из ' + more + '</button></div>'
        : '')
       + '<div class="rel-note">Показано ' + items.length
       + (releasesData.found ? ' из ' + releasesData.found + ' найденных' : '')
       + (isFy ? '. Отбор — по близости артиста; то, что уже есть в библиотеке, скрыто.'
               : '. Отбор — по близости артиста и свежести релиза.')
       + '<br>Проверено артистов: ' + releasesData.artists_checked
       + ' из ' + releasesData.artists_total + '. Обновляется только по кнопке.</div>';
}

var REL_ICON_PLAY  = '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
var REL_ICON_PAUSE = '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
var REL_ICON_STAR  = '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>';

// Узлы карточек переиспользуются: при любой перерисовке существующие элементы
// переставляются, а не создаются заново. Иначе каждый <img> рождался пустым и
// обложки «пропадали и появлялись» — при клике, при отметке, при обновлении.


// Прокси обложек умирает вместе с сервером, а картинки mzstatic/dzcdn грузятся
// как <img> и без него. Поэтому первый отказ — повод сходить к источнику
// напрямую, и только второй — поставить заглушку. Важно с тех пор, как лента
// рисуется из зеркала до того, как известно, жив ли сервер.
function relArtFallback(img) {
  var direct = img.getAttribute('data-direct');
  if (direct) {
    img.removeAttribute('data-direct');
    img.src = direct;
    return;
  }
  img.outerHTML = '<div class="rel-art-ph">&#9834;</div>';
}

function releaseCardHtml(it) {
  var key = it.key || relKey(it);
  var g = relGroupOf(it.date);
  var soon = g === 'Скоро';
  var kindLabel = soon ? 'скоро' : (it.kind === 'album' ? 'альбом' : it.kind === 'ep' ? 'EP' : 'сингл');
  var kindCls = soon ? 'kind-soon' : ('kind-' + it.kind);
  // Через свой сервер: он кладёт картинку на диск, поэтому она переживает и
  // перезапуск сервера, и чистку кэша браузера.
  // Адрес держим в data-src, а не в src: загрузкой управляем сами (см.
  // relWarmArt). С loading="lazy" обложки появлялись только под прокрутку, и
  // список выглядел полупустым.
  var artSrc = relF().offline ? it.art : ('/api/releases/art?u=' + encodeURIComponent(it.art));
  // Адрес всегда в data-src: байты берутся из своего кэша, а он читается
  // асинхронно. Ставить src сразу, как раньше, больше нельзя — это опять
  // отдало бы картинку HTTP-кэшу браузера, который iOS вытесняет.
  // data-art — ключ кэша: исходный адрес обложки, один и тот же в онлайне и
  // офлайне, поэтому запись не задваивается.
  var art = it.art
    ? '<img class="rel-art" data-src="' + relEsc(artSrc) + '" data-art="' + relEsc(it.art) + '"'
      + (artSrc !== it.art ? ' data-direct="' + relEsc(it.art) + '"' : '')
      + ' decoding="async" onerror="relArtFallback(this)">'
    : '<div class="rel-art-ph">&#9834;</div>';
  var stop = 'event.stopPropagation();';
  return art
    + '<div class="rel-body">'
    +   '<div class="rel-artist">' + relEsc(it.artist) + '</div>'
    +   '<div class="rel-title">' + relEsc(it.title) + '</div>'
    +   '<div class="rel-meta">'
    +     '<span class="rel-badge ' + kindCls + '">' + kindLabel + '</span>'
    +     '<span class="rel-date">' + relDateLabel(it.date) + '</span>'
    +     (it.tracks > 1 ? '<span class="rel-date">' + it.tracks + ' трек.</span>' : '')
    +     (it.in_library ? '<span class="rel-have">&#10003; в библиотеке</span>' : '')
    +   '</div>'
    + '</div>'
    + '<div class="rel-actions">'
    +   '<button class="rel-btn rel-btn-star' + (it.starred ? ' on' : '') + '" data-key="' + relEsc(key) + '"'
    +     ' data-tip="Добавить в избранное" onclick="' + stop + 'toggleStar(\'' + relEsc(key) + '\')">' + REL_ICON_STAR + '</button>'
    +   (it.rid ? '<button class="rel-btn rel-btn-prev" data-key="' + relEsc(key) + '"'
        + ' data-tip="Прослушать отрывок" onclick="' + stop + 'togglePreview(\'' + relEsc(key) + '\', true)">'
        + REL_ICON_PLAY + '</button>' : '')
    +   (it.in_library || userRole === 'demo' ? ''
        : '<button class="rel-btn rel-btn-get" data-tip="Найти и скачать" onclick="' + stop + 'getRelease(\'' + relEsc(key) + '\')">'
          + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg></button>')
    +   (it.url ? '<a class="rel-btn" href="' + relEsc(it.url) + '" target="_blank" rel="noopener" data-tip="Открыть у источника" onclick="' + stop + '">'
          + '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7zM5 5h5V3H3v18h18v-7h-2v5H5V5z"/></svg></a>' : '')
    + '</div>';
}

function relState(it) {
  return (it.in_library ? '1' : '0') + (it.starred ? 'S' : '-') + (it.tracks || 0) + it.date;
}

var _relRenderToken = 0;
var _artWarmToken = 0;

// Обложки грузим сами, строго по одной за раз и с паузой. Сервер однопоточный:
// первый показ каждой картинки — это его поход к Apple примерно на секунду, и
// пока он идёт, всё остальное ждёт. Поэтому держим один запрос в воздухе, даём
// серверу передышку между ними и полностью замолкаем, пока играет музыка —
// подгрузка обложек не стоит заикания в воспроизведении.
function relWarmArt() {
  var token = ++_artWarmToken;
  var busy = false;

  function nextImg() {
    return document.querySelector('#relCards img.rel-art[data-src]');
  }

  function pump() {
    if (token !== _artWarmToken || busy) return;
    if (activeTab !== 'new' || (audio && !audio.paused)) { setTimeout(pump, 2000); return; }
    var img = nextImg();
    if (!img) return;                       // всё загружено
    var url = img.getAttribute('data-src');
    var key = img.getAttribute('data-art') || url;   // ключ кэша — исходный адрес
    img.removeAttribute('data-src');
    busy = true;

    // fast=true — картинка пришла из своего кэша, сети не было, поэтому и
    // паузы не нужно: она существует только чтобы не забивать однопоточный
    // сервер запросами к Apple.
    function done(fast) {
      busy = false;
      if (token === _artWarmToken) setTimeout(pump, fast ? 0 : 250);
    }

    relArtGet(key, function(buf) {
      if (buf && buf.byteLength) {
        try { img.src = URL.createObjectURL(new Blob([buf])); } catch (e) {}
        done(true);
        return;
      }
      if (_isOffline) { relArtFallback(img); done(true); return; }
      // Качаем байтами, а не через <img>: только так их можно сохранить.
      fetch(url).then(function(r) {
        if (!r.ok) throw new Error('http');
        // 200 не значит картинку: при истёкшей сессии сюда приезжает JSON, и
        // без проверки типа он осел бы в кэше вместо обложки — навсегда.
        if (!/^image\//.test(r.headers.get('Content-Type') || '')) throw new Error('type');
        return r.arrayBuffer();
      }).then(function(bytes) {
        if (!bytes || bytes.byteLength < 100) throw new Error('empty');
        relArtPut(key, bytes);
        try { img.src = URL.createObjectURL(new Blob([bytes])); } catch (e) {}
        done(false);
      }).catch(function() {
        relArtFallback(img);
        done(false);
      });
    });
  }

  pump();
}


// Список длинный (у большой библиотеки под две тысячи карточек), поэтому строим
// порциями: первая появляется сразу, остальные дорисовываются между кадрами.
// Одним куском это занимало больше секунды на десктопе — на телефоне вкладка
// заметно подвисала бы при каждом открытии.
function syncReleaseCards(items) {
  var cardsEl = document.getElementById('relCards');
  if (!cardsEl) return;
  var token = ++_relRenderToken;
  // Очистка схлопывает высоту контейнера, и браузер прижимает scrollTop к нулю.
  // Запоминаем позицию и возвращаем её, когда высота восстановится.
  var scroller = document.getElementById('newList');
  var keepScroll = scroller ? scroller.scrollTop : 0;
  var nodes = relF().nodes;
  cardsEl.innerHTML = '';        // узлы остаются в feed.nodes и переиспользуются
  var used = {}, lastGroup = null, i = 0;

  function addOne(it, frag) {
    var key = it.key || relKey(it);
    used[key] = true;
    var g = relGroupFor(it);
    if (g !== lastGroup) {
      var t = document.createElement('div');
      t.className = 'rel-group-title';
      t.textContent = g;
      frag.appendChild(t);
      lastGroup = g;
    }
    var node = nodes[key];
    var state = relState(it);
    if (!node) {
      node = document.createElement('div');
      node.setAttribute('data-key', key);
      if (it.rid) {
        node.setAttribute('onclick', "togglePreview('" + key.replace(/'/g, "\\'") + "')");
        node.style.cursor = 'pointer';
      }
      node.innerHTML = releaseCardHtml(it);
      nodes[key] = node;
    } else if (node._state !== state) {
      // Состояние изменилось — перерисовываем всё, кроме обложки: она уже
      // загружена, а новый <img> пришлось бы ждать заново.
      var img = node.firstChild;
      node.innerHTML = releaseCardHtml(it);
      if (img && img.tagName === 'IMG' && node.firstChild && node.firstChild.tagName === 'IMG') {
        node.replaceChild(img, node.firstChild);
      }
    }
    node._state = state;
    node.className = 'rel-card ' + (it.in_library ? 'rel-owned' : 'rel-missing')
                   + (it.starred ? ' rel-starred' : '');
    frag.appendChild(node);
  }

  function step() {
    if (token !== _relRenderToken) return;   // началась новая отрисовка
    var frag = document.createDocumentFragment();
    for (var n = 0; i < items.length && n < 150; n++, i++) addOne(items[i], frag);
    cardsEl.appendChild(frag);
    if (scroller && keepScroll && scroller.scrollHeight > keepScroll) {
      scroller.scrollTop = keepScroll;
    }
    if (i < items.length) { setTimeout(step, 0); return; }
    for (var k in nodes) if (!used[k]) delete nodes[k];
    if (scroller && keepScroll) scroller.scrollTop = keepScroll;
    applyExpansion();
    relWarmArt();
  }
  step();                                     // первая порция — синхронно
}

// Отметка: закрепляет релиз наверху и переживает обновление списка — сервер
// хранит и ключ, и снимок релиза.
function toggleStar(key) {
  var it = findRelease(key);
  if (!it) return;
  var on = !it.starred;
  it.starred = on;
  it.key = key;
  resortReleases();
  relSaveStarredLocal();
  fetch('/api/releases/star', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key: key, on: on, item: on ? it : null})})
    .then(function(r){ return r.json(); })
    .then(function(d){ if (!d || !d.ok) throw new Error('no server'); })
    .catch(function() {
      // Сервера нет — отметка остаётся локально и уедет при первом же успешном
      // ответе. Откатывать её было бы хуже: пользователь своё действие сделал.
      relQueueStar(key, on, it);
      showToast(on ? 'В избранном (синхронизируется позже)' : 'Убрано (синхронизируется позже)');
    });
  showToast(on ? 'В избранном' : 'Убрано из избранного');
}

function resortReleases() {
  var d = relF().data;
  if (!d) return;
  // В NEW читается по свежести, в 4YOU — по близости артиста: там дата ни о
  // чём не говорит, весь состав старее окна новинок.
  if (_relTab === 'foryou') {
    d.items.sort(function(a, b) { return (b.rank || 0) - (a.rank || 0); });
  } else {
    d.items.sort(function(a, b) {
      if (a.date !== b.date) return a.date < b.date ? 1 : -1;
      return (b.artist_score || 0) - (a.artist_score || 0);
    });
  }
  d.items.sort(function(a, b) { return (a.starred ? 0 : 1) - (b.starred ? 0 : 1); });
  syncReleaseCards(d.items);
  applyExpansion();
}

// Разворачивание/сворачивание — точечная операция над двумя узлами, без
// пересборки списка: карточки и их <img> остаются на месте.
function applyExpansion() {
  var cardsEl = document.getElementById('relCards');
  if (!cardsEl) return;
  var old = cardsEl.querySelector('.rel-tracks');
  if (old) old.remove();
  var prev = cardsEl.querySelector('.rel-card.expanded');
  if (prev) prev.classList.remove('expanded');
  if (!_previewKey) { paintPreviewState(); return; }
  var card = cardsEl.querySelector('.rel-card[data-key="' + _previewKey + '"]');
  if (!card) { paintPreviewState(); return; }
  card.classList.add('expanded');
  card.insertAdjacentHTML('afterend', previewTracksHtml(card.classList.contains('rel-owned'),
                                                        card.classList.contains('rel-starred')));
  paintPreviewState();
}

function relShowMore() {
  var f = relF();
  f.limit = Math.min(REL_LIMIT_MAX, f.limit + REL_PAGE);
  lsSet(f.lsKey, String(f.limit));
  showToast('Загружаю ещё...');
  loadReleases(true, _relTab);
}

function refreshReleases() {
  showToast('Запускаю проверку новинок...');
  fetch('/api/releases/refresh', {method: 'POST'})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (!d || !d.ok) throw new Error('no server');
      // Проверка перебирает общий кэш артистов, поэтому устаревают обе ленты.
      setTimeout(function(){ loadReleases(true, _relTab); relInvalidate(_relTab); }, 800);
    })
    .catch(function() {
      // Сервера нет — обходим артистов сами. Урезанно: без Deezer и без
      // серверного кэша обложек, зато работает вдали от дома.
      showToast('Сервер недоступен — проверяю через iTunes');
      relFeeds['new'].offline = true;
      relFeeds['foryou'].offline = true;
      relSweepDirect();
    });
}

// Передаём релиз в существующее окно импорта: подставляем «артист — название»
// и сразу запускаем поиск, чтобы не набирать руками.
function getRelease(key) {
  var it = findRelease(key);
  if (!it) return;
  if (!document.getElementById('folderSelect').value) {
    showToast('Сначала выберите каталог');
    return;
  }
  openVkModal();
  showImpTab('search');
  var input = document.getElementById('vkSearchQuery');
  input.value = it.artist + ' ' + it.title;
  vkSearchTracks();
}

// ── Автономный режим «Новинок» ──
// Лента и избранное зеркалятся в IndexedDB, поэтому вкладка живёт и без
// сервера. iTunes Search API отдаёт access-control-allow-origin: *, так что
// браузер может обойти артистов сам. Deezer из браузера недоступен (у него нет
// этого заголовка), поэтому запасной источник и кэш обложек остаются серверными
// — автономный режим сознательно урезанный, а не равноценный.
var REL_DB = 'vinylNew';
var REL_PAGE = 300;           // порция дропов
var REL_LIMIT_MAX = 3000;     // столько же максимум принимает сервер
var _relSweeping = false;

// Сколько просим у сервера. Значение переживает перезапуск: иначе каждое
// открытие PWA откатывало ленту к первой порции, и «Показать ещё» надо было
// жать заново — при том что расширенная лента уже лежит в зеркале.
function relSavedLimit(key) {
  var n = parseInt(localStorage.getItem(key), 10);
  return (n >= REL_PAGE && n <= REL_LIMIT_MAX) ? n : REL_PAGE;
}

(function relInitFeeds() {
  for (var i = 0; i < REL_TABS.length; i++) {
    var f = relFeeds[REL_TABS[i]];
    f.data = null; f.nodes = {}; f.poll = null;
    f.loading = false; f.offline = false; f.hasMirror = false;
    f.limit = relSavedLimit(f.lsKey);
  }
  var saved = localStorage.getItem('_vc_reltab');
  if (saved && relFeeds[saved]) _relTab = saved;
})();

// Обложки, которые уже доезжали до этого клиента. Сервер отдаёт их с
// max-age=30 дней, так что повторный показ берётся из HTTP-кэша — но очередь
// прогрева об этом не знала и всё равно вела их по одной с паузой 250 мс, а
// при играющей музыке стояла совсем. На срезе в 300 карточек это выглядело
// как «дропы грузятся заново» при каждом открытии приложения.
// Байты обложек релизов лежат в той же базе, что и зеркало ленты. Раньше их
// держал только HTTP-кэш браузера, а iOS вытесняет его агрессивно: реестр
// помнил, что картинка «была», ставил адрес сразу — но байтов уже не было, и
// офлайн обложки не показывались вовсе. У обложек треков такой проблемы нет,
// они с самого начала лежат в IndexedDB; здесь тот же приём.
var ART_SEEN_MAX = 900;       // ~45 МБ при обложках 600×600

function relArtKey(url) { return 'art:' + url; }
function relArtGet(url, cb) { relDbGet(relArtKey(url), cb); }

function relArtPut(url, buf) {
  relDbSet(relArtKey(url), buf);
  relMarkArtSeen(url);
}
var _artSeen = {};
var _artSeenTimer = null;
var _artSeenLoaded = false;

function relLoadArtSeen() {
  if (_artSeenLoaded) return;   // конфиг применяется не один раз за сессию
  _artSeenLoaded = true;
  relDbGet('artSeen', function(list) {
    if (!list || !list.length) return;
    for (var i = 0; i < list.length; i++) _artSeen[list[i]] = 1;
  });
}

function relMarkArtSeen(url) {
  if (!url || _artSeen[url]) return;
  _artSeen[url] = 1;
  // Пишем пачкой: прогрев отмечает по обложке каждые 250 мс, и транзакция на
  // каждую из них — это сотни записей подряд на ровном месте.
  if (_artSeenTimer) return;
  _artSeenTimer = setTimeout(function() {
    _artSeenTimer = null;
    var keys = Object.keys(_artSeen);
    if (keys.length > ART_SEEN_MAX) {
      var drop = keys.slice(0, keys.length - ART_SEEN_MAX);
      keys = keys.slice(keys.length - ART_SEEN_MAX);   // порядок вставки = давность
      // Вместе с ключом убираем и байты, иначе база растёт бесконечно.
      for (var d = 0; d < drop.length; d++) relDbDel(relArtKey(drop[d]));
      _artSeen = {};
      for (var i = 0; i < keys.length; i++) _artSeen[keys[i]] = 1;
    }
    relDbSet('artSeen', keys);
  }, 3000);
}

function relDb(cb) {
  var req = indexedDB.open(REL_DB, 1);
  req.onupgradeneeded = function(e) {
    var db = e.target.result;
    if (!db.objectStoreNames.contains('kv')) db.createObjectStore('kv');
  };
  req.onsuccess = function(e) { cb(e.target.result); };
  req.onerror = function() { cb(null); };
}

function relDbSet(key, val) {
  relDb(function(db) {
    if (!db) return;
    try { db.transaction('kv', 'readwrite').objectStore('kv').put(val, key); } catch (e) {}
  });
}

function relDbDel(key) {
  relDb(function(db) {
    if (!db) return;
    try { db.transaction('kv', 'readwrite').objectStore('kv').delete(key); } catch (e) {}
  });
}

function relDbGet(key, cb) {
  relDb(function(db) {
    if (!db) { cb(null); return; }
    try {
      var r = db.transaction('kv', 'readonly').objectStore('kv').get(key);
      r.onsuccess = function() { cb(r.result || null); };
      r.onerror = function() { cb(null); };
    } catch (e) { cb(null); }
  });
}

// Артисты ранжируются из каталогов, уже лежащих в localStorage — тем же
// правилом, что и на сервере: логарифм от числа треков на свежесть добавления.
function relSplitArtists(v) {
  var out = [];
  (v || '').split(/[\/,;]|\bfeat\b\.?|\bft\b\.?/i).forEach(function(part) {
    part = part.replace(/^[\s.,;:\-–—]+/, '').replace(/[\s\-–—]+$/, '');
    if (part) out.push(part);
  });
  return out;
}

function relLocalCatalogs() {
  var out = [];
  Object.keys(localStorage).forEach(function(k) {
    if (k.indexOf('_vc_folder_') !== 0) return;
    try {
      var d = JSON.parse(localStorage.getItem(k));
      if (d && d.tracks && d.tracks.length) out.push(d.tracks);
    } catch (e) {}
  });
  return out;
}

function relRankArtists() {
  var stats = {};
  relLocalCatalogs().forEach(function(ts) {
    var n = ts.length || 1;
    for (var i = 0; i < ts.length; i++) {
      var fresh = 1 - i / n;
      var names = relSplitArtists(ts[i].artist);
      for (var j = 0; j < names.length; j++) {
        var key = names[j].toLowerCase();
        var e = stats[key] || (stats[key] = {name: names[j], count: 0, fresh: 0});
        e.count++;
        if (fresh > e.fresh) e.fresh = fresh;
      }
    }
  });
  var arr = [];
  Object.keys(stats).forEach(function(k) {
    var e = stats[k];
    e.key = k;
    e.score = (Math.log(1 + e.count) / Math.LN2) * (0.5 + e.fresh);
    arr.push(e);
  });
  arr.sort(function(a, b) { return b.score - a.score; });
  return arr;
}

function relOwnedNames() {
  var owned = {};
  relLocalCatalogs().forEach(function(ts) {
    for (var i = 0; i < ts.length; i++) {
      var t = ts[i];
      relSplitArtists(t.artist).forEach(function(a) {
        var k = a.toLowerCase();
        var set = owned[k] || (owned[k] = {});
        set[normSearch(t.album)] = true;
        set[normSearch(t.title)] = true;
      });
    }
  });
  return owned;
}

function relJson(url, cb) {
  fetch(url).then(function(r){ return r.ok ? r.json() : null; }).then(cb).catch(function(){ cb(null); });
}

function relItunesReleases(name, cb) {
  relJson('https://itunes.apple.com/search?term=' + encodeURIComponent(name)
          + '&entity=musicArtist&limit=5', function(d) {
    var found = null, want = normSearch(name);
    ((d && d.results) || []).forEach(function(c) {
      if (!found && normSearch(c.artistName) === want) found = c;
    });
    if (!found) { cb([]); return; }
    relJson('https://itunes.apple.com/lookup?id=' + found.artistId
            + '&entity=album&limit=25&sort=recent', function(a) {
      var out = [];
      ((a && a.results) || []).forEach(function(x) {
        if (x.wrapperType !== 'collection') return;
        var date = (x.releaseDate || '').slice(0, 10);
        if (!date) return;
        var n = x.trackCount || 0;
        out.push({
          rid: x.collectionId, source: 'itunes',
          title: (x.collectionName || '').replace(/\s*[-–—]\s*(Single|EP)$/, ''),
          date: date, tracks: n,
          kind: n === 1 ? 'single' : (n <= 6 ? 'ep' : 'album'),
          art: (x.artworkUrl100 || '').replace('100x100bb', '600x600bb'),
          url: x.collectionViewUrl || ''
        });
      });
      cb(out);
    });
  });
}

// Обход артистов прямо из браузера. Тот же темп, что на сервере: iTunes при
// частых запросах рвёт соединение.
// Один обход артистов кормит обе ленты: iTunes отдаёт весь список релизов
// разом, и делить его по дате дешевле, чем ходить туда второй раз.
function relSweepDirect() {
  if (_relSweeping) return;
  var ranked = relRankArtists();
  if (!ranked.length) { showToast('Нет данных о каталоге для автономной проверки'); return; }
  var budget = Math.max(20, Math.min(80, Math.round(Math.pow(ranked.length, 0.6))));
  var list = ranked.slice(0, budget);
  var owned = relOwnedNames();
  var cutoff = new Date(Date.now() - 400 * 86400000).toISOString().slice(0, 10);
  var fresh = [], older = [], i = 0;
  _relSweeping = true;

  function progress(done) {
    for (var t = 0; t < REL_TABS.length; t++) {
      var f = relFeeds[REL_TABS[t]];
      f.data = f.data || {items: [], artists_total: ranked.length, artists_checked: 0};
      f.data.refreshing = true;
      f.data.progress = {done: done, total: list.length};
    }
    renderReleases();
  }
  progress(0);

  (function step() {
    if (i >= list.length) {
      _relSweeping = false;
      relFinishSweep('new', relDedup(fresh), ranked.length, list.length,
                     function(sn){ return (sn.date || '') >= cutoff; }, false);
      relFinishSweep('foryou', relRoundRobin(relDedup(older)), ranked.length, list.length,
                     function(sn){ return (sn.date || '') < cutoff; }, true);
      showToast('Проверено артистов: ' + list.length);
      return;
    }
    var a = list[i++];
    progress(i);
    relItunesReleases(a.name, function(rels) {
      var have = owned[a.key] || {};
      rels.forEach(function(r) {
        r.artist = a.name;
        r.artist_score = Math.round(a.score * 1000) / 1000;
        r.in_library = !!have[normSearch(r.title)];
        r.key = r.source + ':' + r.rid;
        if (r.date >= cutoff) fresh.push(r);
        else if (!r.in_library) older.push(r);   // в 4YOU только то, чего нет
      });
      setTimeout(step, 1200);
    });
  })();
}

function relFinishSweep(tab, items, artistsTotal, checked, want, keepOrder) {
  var f = relFeeds[tab];
  relApplyStarred(items, want, keepOrder, function(finalItems) {
    f.data = {items: finalItems.slice(0, 300), updated_at: Date.now() / 1000,
              found: finalItems.length, artists_total: artistsTotal, artists_checked: checked,
              refreshing: false, progress: {done: 0, total: 0}, starred_count: 0};
    f.nodes = {};
    if (tab === _relTab) renderReleases();
    relSaveMirror(tab);
  });
}

// Совместка приезжает от каждого участника — без этого она задваивалась бы.
function relDedup(items) {
  var seen = {}, out = [];
  for (var i = 0; i < items.length; i++) {
    if (seen[items[i].key]) continue;
    seen[items[i].key] = 1;
    out.push(items[i]);
  }
  return out;
}

// Тот же приём, что на сервере: артисты выдаются по кругу. При простой
// сортировке по весу первые два десятка карточек занял бы один артист — у
// каждого их в выдаче iTunes до 25 штук.
function relRoundRobin(items) {
  var buckets = {}, order = [];
  for (var i = 0; i < items.length; i++) {
    var k = (items[i].artist || '').toLowerCase();
    if (!buckets[k]) { buckets[k] = []; order.push(k); }
    buckets[k].push(items[i]);
  }
  order.sort(function(a, b) {
    return (buckets[b][0].artist_score || 0) - (buckets[a][0].artist_score || 0);
  });
  for (var j = 0; j < order.length; j++) {
    buckets[order[j]].sort(function(a, b) { return a.date < b.date ? 1 : (a.date > b.date ? -1 : 0); });
  }
  var out = [], row = 0, added = true;
  while (added) {
    added = false;
    for (var n = 0; n < order.length; n++) {
      var b = buckets[order[n]];
      if (row < b.length) { b[row].rank = b[row].artist_score || 0; out.push(b[row]); added = true; }
    }
    row++;
  }
  return out;
}

// ── Зеркало и очередь избранного ──
function relSaveMirror(tab) {
  var f = relFeeds[tab || _relTab], d = f.data;
  if (!d) return;
  relDbSet(f.mirrorKey, {items: d.items, updated_at: d.updated_at,
                         found: d.found, artists_total: d.artists_total,
                         artists_checked: d.artists_checked, saved_at: Date.now() / 1000});
  f.hasMirror = true;
}

function relLoadMirror(tab, cb) {
  var f = relFeeds[tab || _relTab];
  relDbGet(f.mirrorKey, function(m) {
    f.hasMirror = !!(m && m.items && m.items.length);
    cb(m);
  });
}

function relPendingStars(cb) { relDbGet('pendingStars', function(p){ cb(p || {}); }); }

function relQueueStar(key, on, item) {
  relPendingStars(function(p) {
    p[key] = {on: on, item: on ? item : null};
    relDbSet('pendingStars', p);
  });
}

// Отметки, поставленные без сервера, уезжают на него при первом же успешном
// ответе — иначе они остались бы только на этом устройстве.
function relFlushStars() {
  relPendingStars(function(p) {
    var keys = Object.keys(p);
    if (!keys.length) return;
    var i = 0, sent = 0;
    (function next() {
      if (i >= keys.length) {
        relDbSet('pendingStars', {});
        // Лента уже отрисована по данным сервера, который об этих отметках ещё
        // не знал — перезапрашиваем, иначе они выглядели бы снятыми.
        if (sent) setTimeout(function(){ loadReleases(true, _relTab); relInvalidate(_relTab); }, 400);
        return;
      }
      var k = keys[i++], v = p[k];
      fetch('/api/releases/star', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({key: k, on: v.on, item: v.item})})
        .then(function(r){ return r.json(); })
        .then(function(d){ if (d && d.ok) sent++; next(); })
        .catch(function(){ next(); });   // не отправилось — останется в очереди
    })();
  });
}

// want() отсеивает чужие снимки: хранилище отметок общее для обеих лент, и
// без него закреплённое в 4YOU всплывало бы наверху NEW.
function relApplyStarred(items, want, keepOrder, cb) {
  relDbGet('starred', function(st) {
    st = st || {};
    var seen = {};
    items.forEach(function(it) { it.starred = !!st[it.key]; seen[it.key] = true; });
    Object.keys(st).forEach(function(k) {
      if (seen[k] || !st[k] || !want(st[k])) return;
      var snap = JSON.parse(JSON.stringify(st[k]));
      snap.starred = true; snap.key = k;
      items.push(snap);
    });
    // В 4YOU порядок уже задан кругом по артистам, пересортировка по дате его
    // сломала бы.
    if (!keepOrder) items.sort(function(a, b) { return a.date < b.date ? 1 : (a.date > b.date ? -1 : 0); });
    items.sort(function(a, b) { return (a.starred ? 0 : 1) - (b.starred ? 0 : 1); });
    cb(items);
  });
}

// Хранилище отметок общее для NEW и 4YOU, поэтому снимок нельзя переписывать
// по одной ленте: вторая может быть ещё не загружена, и её отметки пропали бы.
// Загруженная лента авторитетна для своих ключей, остальные записи не трогаем.
function relSaveStarredLocal() {
  relDbGet('starred', function(prev) {
    var st = prev || {};
    for (var t = 0; t < REL_TABS.length; t++) {
      var d = relFeeds[REL_TABS[t]].data;
      if (!d || !d.items) continue;
      for (var i = 0; i < d.items.length; i++) {
        var it = d.items[i];
        if (it.starred) st[it.key] = it; else delete st[it.key];
      }
    }
    relDbSet('starred', st);
  });
}

// ── Превью релизов ──
// Отдельный audio-элемент: в основном живёт контекст воспроизведения (текущий
// трек, позиция, Now Playing), и подменять в нём src ради 30-секундного отрывка
// значило бы этот контекст потерять.
var previewAudio = document.getElementById('previewEl');
var _previewKey = null;        // ключ развёрнутого релиза (source:rid)
var _previewTrack = -1;        // играющий трек внутри релиза
var _previewTracks = [];

function stopPreview() {
  try { previewAudio.pause(); } catch (e) {}
  previewAudio.removeAttribute('src');
  _previewTrack = -1;
  paintPreviewState();
}

function closePreview() {
  exitPreviewPlayerUI();
  stopPreview();
  _previewKey = null; _previewTracks = [];
  applyExpansion();
}

function findRelease(key) {
  var d = relF().data;
  if (!d) return null;
  for (var i = 0; i < d.items.length; i++) {
    // Ключ берём тот, что прислал сервер: у записей без rid он строится из
    // названия и даты, и relKey() дал бы другой.
    if ((d.items[i].key || relKey(d.items[i])) === key) return d.items[i];
  }
  return null;
}

// autoplay=true — только для кнопки ▶. Нажатие на саму карточку лишь
// раскрывает список: смотреть состав альбома, не перебивая то, что играет, —
// нормальное желание, а музыка, стартующая от простого клика, только мешает.
function togglePreview(key, autoplay) {
  var it = findRelease(key);
  if (!it || !it.rid) { showToast('Для этого релиза превью недоступно'); return; }

  if (_previewKey === key) {
    if (!autoplay) { closePreview(); return; }          // клик по карточке — свернуть

    if (_previewTrack >= 0 && !previewAudio.paused) { stopPreview(); return; }
    if (_previewTracks.length) playPreview(_previewTrack >= 0 ? _previewTrack : 0);
    return;
  }

  stopPreview();
  _previewKey = key; _previewTracks = [];
  // Только точечная операция: renderReleases() пересобирал весь список, из-за
  // чего контейнер на миг схлопывался и прокрутку выбрасывало в середину.
  applyExpansion();

  if (!autoplay) {
    fetch('/api/releases/tracks?source=' + encodeURIComponent(it.source || 'itunes')
          + '&rid=' + encodeURIComponent(it.rid)
          + '&artist=' + encodeURIComponent(it.artist)
          + '&title=' + encodeURIComponent(it.title))
      .then(function(r){ return r.json(); })
      .then(function(d) {
        if (_previewKey !== key) return;
        _previewTracks = (d && d.tracks) || [];
        applyExpansion();
        if (!_previewTracks.length) showToast('Превью для этого релиза не нашлось');
      })
      .catch(function(){ if (_previewKey === key) showToast('Не удалось загрузить список'); });
    return;
  }

  // Разблокируем элемент прямо в обработчике клика: список треков приезжает
  // после fetch, а play() за пределами жеста браузер отклоняет (на iOS —
  // всегда). Проигрываем тишину сейчас, подменим src, когда придут треки.
  try {
    previewAudio.src = _silentBlobUrl;
    var warm = previewAudio.play();
    if (warm && warm.catch) warm.catch(function(){});
  } catch (e) {}

  // артист и название нужны серверу, чтобы найти тот же релиз во втором
  // источнике, если основной ничего не отдал
  fetch('/api/releases/tracks?source=' + encodeURIComponent(it.source || 'itunes')
        + '&rid=' + encodeURIComponent(it.rid)
        + '&artist=' + encodeURIComponent(it.artist)
        + '&title=' + encodeURIComponent(it.title))
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (_previewKey !== key) return;            // пока грузилось, открыли другое
      _previewTracks = (d && d.tracks) || [];
      applyExpansion();
      if (_previewTracks.length) playPreview(0);
      else showToast('Превью для этого релиза не нашлось');
    })
    .catch(function(){ if (_previewKey === key) showToast('Не удалось загрузить превью'); });
}

// ── Отрывок DROPS в интерфейсе плеера ──
// Звук идёт через отдельный <audio> (основной хранит контекст воспроизведения),
// но показывать отрывок должен обычный плеер: обложка релиза, название, бейдж
// и рабочие кнопки. Возврат к обычному виду — при первом же треке из библиотеки.
var _previewMode = false;

function previewArtSrc(rel) {
  if (!rel || !rel.art) return '';
  return relF().offline ? rel.art : ('/api/releases/art?u=' + encodeURIComponent(rel.art));
}

// Всё, что зависит от того, ведёт ли плеер отрывок: перемотки у него нет,
// громкостью он не управляется. Обе панели гасим и возвращаем здесь, в одном
// месте — иначе легко оставить путь, на котором вернулось только одно.
function syncPreviewChrome() {
  var pw = document.getElementById('progressWrap');
  if (pw) pw.classList.toggle('no-seek', _previewMode);
  var vw = document.querySelector('.volume-wrap');
  if (vw) vw.classList.toggle('no-volume', _previewMode);
}

function enterPreviewPlayerUI(rel, tr) {
  // Отрывок перехватывает плеер — значит станция больше не ведёт, как и при
  // выборе плейлиста или трека. Без этого её рамка оставалась висеть поверх
  // чужого воспроизведения.
  stopRadio();
  _previewMode = true;
  var titleEl = document.getElementById('trackTitle');
  var artistEl = document.getElementById('trackArtist');
  titleEl.textContent = (tr && tr.title) || rel.title || '';
  artistEl.textContent = rel.artist || '';
  titleEl.style.opacity = '1'; artistEl.style.opacity = '1';
  titleEl.removeAttribute('data-idle');
  var badge = document.getElementById('trackTitleBadge');
  if (badge) {
    badge.textContent = 'DROPS';
    badge.className = 'fmt-badge fmt-badge-player fmt-drops';
    badge.style.display = '';
  }
  var ct = document.getElementById('cassetteTitle'), ca = document.getElementById('cassetteArtist');
  if (ct) ct.textContent = titleEl.textContent;
  if (ca) ca.textContent = artistEl.textContent;

  var src = previewArtSrc(rel);
  var img = document.getElementById('vinylCover');
  var ph = document.getElementById('vinylPlaceholder');
  if (src && img) {
    // Фон подстраивается под обложку тем же путём, что и для обычных треков.
    img.onload = function(){ extractColor(img); img.onload = null; };
    img.src = src;
    img.style.display = '';
    if (ph) ph.style.display = 'none';
    var ccov = document.getElementById('cassetteCover'), cph = document.getElementById('cassetteCoverPh');
    if (ccov) { ccov.src = src; ccov.style.display = ''; if (cph) cph.style.display = 'none'; }
  }
  syncPreviewChrome();
  setPlayState(!previewAudio.paused);
}

function exitPreviewPlayerUI() {
  if (!_previewMode) return;
  _previewMode = false;
  try { previewAudio.pause(); } catch (e) {}
  previewAudio.removeAttribute('src');
  _previewTrack = -1;
  var badge = document.getElementById('trackTitleBadge');
  if (badge) badge.style.display = 'none';
  syncPreviewChrome();
  paintPreviewState();
}

function playPreview(n) {
  var t = _previewTracks[n];
  if (!t) return;
  if (_previewTrack === n && !previewAudio.paused) { stopPreview(); return; }
  // Останавливаем основной плеер до превью. setPlayState(false) сначала —
  // иначе обработчик pause посчитает это системным прерыванием.
  if (!audio.paused) { setPlayState(false); audio.pause(); }
  _previewTrack = n;
  // Через свой сервер: он исправляет Content-Type, который у Apple нестандартный
  previewAudio.src = '/api/releases/preview?u=' + encodeURIComponent(t.preview);
  var rel = findRelease(_previewKey);
  if (rel) enterPreviewPlayerUI(rel, t);
  var p = previewAudio.play();
  if (p && p.catch) p.catch(function(err) {
    // NotAllowedError здесь означает, что жест не дошёл — не пугаем формулировкой
    // про формат, она сбивает с толку.
    showToast(err && err.name === 'NotAllowedError'
      ? 'Нажмите ещё раз, чтобы включить звук'
      : 'Не удалось воспроизвести отрывок');
    _previewTrack = -1; paintPreviewState();
  });
  paintPreviewState();
}

// Точечная перерисовка строк — полный renderReleases на каждом тике прогресса
// сбрасывал бы прокрутку списка.
function paintPreviewState() {
  var rows = document.querySelectorAll('#newList .rel-track');
  for (var i = 0; i < rows.length; i++) {
    var on = parseInt(rows[i].getAttribute('data-n'), 10) === _previewTrack;
    rows[i].classList.toggle('playing', on);
    var bar = rows[i].querySelector('.rel-track-bar');
    if (bar && !on) bar.style.width = '0';
  }
  var all = document.querySelectorAll('#newList .rel-btn-prev');
  for (var j = 0; j < all.length; j++) {
    var isOpen = all[j].getAttribute('data-key') === _previewKey;
    var playing = isOpen && _previewTrack >= 0 && !previewAudio.paused;
    all[j].classList.toggle('rel-playing', playing);
    var want = playing ? REL_ICON_PAUSE : REL_ICON_PLAY;
    if (all[j].innerHTML !== want) all[j].innerHTML = want;
  }
}

previewAudio.addEventListener('timeupdate', function() {
  if (_previewTrack < 0 || !previewAudio.duration) return;
  var row = document.querySelector('#newList .rel-track[data-n="' + _previewTrack + '"] .rel-track-bar');
  if (row) row.style.width = (previewAudio.currentTime / previewAudio.duration * 100) + '%';
});
previewAudio.addEventListener('play', function(){
  paintPreviewState();
  if (previewOwnsTransport()) setPlayState(true);
});
previewAudio.addEventListener('pause', function(){
  paintPreviewState();
  // Только пока отрывок действительно ведёт плеер. Обработчик play основного
  // элемента зовёт stopPreview(), тот ставит превью на паузу, и это событие
  // прилетало уже ПОСЛЕ setPlayState(true) — состояние сбрасывалось обратно
  // на каждом запуске музыки.
  if (previewOwnsTransport()) setPlayState(false);
});
previewAudio.addEventListener('ended', function() {
  // Дослушали отрывок — идём к следующему треку релиза, как в обычном плеере
  if (_previewTrack >= 0 && _previewTrack + 1 < _previewTracks.length) playPreview(_previewTrack + 1);
  else { stopPreview(); if (_previewMode) setPlayState(false); }
});
previewAudio.addEventListener('error', function() {
  if (_previewTrack < 0) return;
  _previewTrack = -1; paintPreviewState();
});

function previewTracksHtml(owned, starred) {
  var cls = 'rel-tracks' + (starred ? ' starred' : (owned ? ' owned' : ' missing'));
  if (!_previewTracks.length) {
    return '<div class="' + cls + '"><div class="rel-track"><div class="rel-track-name" '
         + 'style="color:rgba(255,255,255,0.3)">Загружаю превью...</div></div></div>';
  }
  var h = '<div class="' + cls + '">';
  for (var i = 0; i < _previewTracks.length; i++) {
    var t = _previewTracks[i];
    var mm = Math.floor((t.duration || 0) / 60), ss = ('0' + ((t.duration || 0) % 60)).slice(-2);
    h += '<div class="rel-track' + (i === _previewTrack ? ' playing' : '') + '" data-n="' + i + '"'
       + ' onclick="playPreview(' + i + ')">'
       + '<span class="rel-track-n">' + (t.n || i + 1) + '</span>'
       + '<span class="rel-track-name">' + relEsc(t.title) + '</span>'
       + (t.duration ? '<span class="rel-track-dur">' + mm + ':' + ss + '</span>' : '')
       + '<span class="rel-track-bar"></span>'
       + '</div>';
  }
  return h + '</div>';
}

// ── Playlists ──
// ── Периоды прослушивания и умные плейлисты ──
// Состав умных плейлистов не хранится, а вычисляется из каталога и истории,
// поэтому они одинаково работают с сервером и офлайн: и список треков, и
// счётчики, и разметка периодов уже лежат на клиенте.
var eraConfig = {enabled: false, eras: []};
var smartPlaylists = [];
var SMART_LIMIT = 200;

function erasKey(folder) { return '_vc_eras_' + folder; }

function loadEras(folder) {
  if (!folder) { eraConfig = {enabled: false, eras: []}; return; }
  try {
    var saved = localStorage.getItem(erasKey(folder));
    eraConfig = saved ? JSON.parse(saved) : {enabled: false, eras: []};
  } catch (e) { eraConfig = {enabled: false, eras: []}; }
  refreshSmartPlaylists();
  fetch('/api/eras', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'list', folder: folder})})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (!d || !d.ok || !d.eras) throw new Error('offline');
      eraConfig = d.eras;
      lsSet(erasKey(folder), JSON.stringify(eraConfig));
      refreshSmartPlaylists();
    })
    .catch(function(){});      // офлайн — остаёмся на зеркале
}

function saveEras() {
  var folder = _curFolder;
  if (!folder) return;
  lsSet(erasKey(folder), JSON.stringify(eraConfig));
  refreshSmartPlaylists();
  var payload = {action: 'save', folder: folder,
                 enabled: eraConfig.enabled, eras: eraConfig.eras};
  fetch('/api/eras', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)})
    .then(function(r){ return r.json(); })
    .then(function(d){ if (!d || !d.ok) throw new Error('offline'); })
    .catch(function() {
      queueEras(folder, payload);
      showToast('Периоды сохранены, уйдут на сервер при подключении');
    });
}

// Номер из имени файла. Он не равен позиции в списке: в нумерации бывают
// пропуски (в этой библиотеке разошлись 2435 файлов из 2440), а размечает
// пользователь по тем номерам, которые видит у себя в каталоге.
function eraFileNo(i) {
  var t = tracks[i];
  if (!t) return i + 1;
  var m = /^(\d+)\./.exec(t.file);
  return m ? parseInt(m[1], 10) : i + 1;
}

// Номер -> позиция. Файлы отсортированы по имени, то есть номера возрастают,
// поэтому берём ближайший существующий: введённого номера может просто не быть.
function eraPosOfNumber(num) {
  num = parseInt(num, 10);
  if (!num || !tracks.length) return 0;
  var best = 0, bestDist = Infinity;
  for (var i = 0; i < tracks.length; i++) {
    var d = Math.abs(eraFileNo(i) - num);
    if (d < bestDist) { bestDist = d; best = i; }
    else if (eraFileNo(i) > num && bestDist < Infinity) break;   // номера растут
  }
  return best;
}

// Якорь — обезномеренное имя файла на краю диапазона. Номер как граница жил бы
// до первого импорта: он перенумеровывает каталог целиком.
// Разметку, сделанную без сервера, досылаем при первом же подключении — иначе
// она осталась бы только на этом устройстве, и плейлисты по периодам на других
// не появились бы вовсе. Тот же приём, что у прослушиваний и отметок в DROPS.
function queueEras(folder, payload) {
  relDbGet('pendingEras', function(p) {
    p = p || {};
    p[folder] = payload;
    relDbSet('pendingEras', p);
  });
}

function flushEras() {
  relDbGet('pendingEras', function(p) {
    var folders = p ? Object.keys(p) : [];
    if (!folders.length) return;
    var i = 0;
    (function next() {
      if (i >= folders.length) { relDbSet('pendingEras', p); return; }
      var f = folders[i++];
      fetch('/api/eras', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(p[f])})
        .then(function(r){ return r.json(); })
        .then(function(d){ if (d && d.ok) delete p[f]; next(); })
        .catch(function(){ relDbSet('pendingEras', p); });   // не ушло — ждёт дальше
    })();
  });
}

function eraAnchorAt(pos) {
  var t = tracks[Math.max(0, Math.min(tracks.length - 1, pos - 1))];
  return t ? cacheKey(t.file) : '';
}

function eraIndexOf(anchor, hintPos) {
  var hint = Math.max(0, Math.min(tracks.length - 1, (hintPos || 1) - 1));
  if (anchor) {
    // Берём совпадение, ближайшее к сохранённому номеру, а не первое: в
    // каталоге встречаются файлы с одинаковым именем после снятия номера (в
    // этой библиотеке — 69 пар), и первое совпадение растягивало период на
    // десятки треков. Перенумерация сдвигает файл на единицы позиций, так что
    // ближайший — он и есть.
    var best = -1, bestDist = Infinity;
    for (var i = 0; i < tracks.length; i++) {
      if (cacheKey(tracks[i].file) !== anchor) continue;
      var d = Math.abs(i - hint);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    if (best >= 0) return best;
  }
  // Файл-якорь удалили — падаем на сохранённый номер, чтобы разметка не
  // разваливалась целиком из-за одного трека.
  return hint;
}

function resolveEra(era) {
  // Самый свежий период открыт сверху: новые треки приходят первыми номерами и
  // должны попадать в текущий год сами, без правки границ.
  var from = era.open_start ? 0 : eraIndexOf(era.from, era.from_pos);
  var to = eraIndexOf(era.to, era.to_pos);
  return from <= to ? {from: from, to: to} : {from: to, to: from};
}

function eraTrackFiles(era) {
  var r = resolveEra(era), files = [];
  for (var i = r.from; i <= r.to && i < tracks.length; i++) files.push(tracks[i].file);
  return files;
}

function buildSmartPlaylists() {
  var out = [], i;
  if (!tracks.length) return out;
  if (eraConfig.enabled && eraConfig.eras && eraConfig.eras.length) {
    // Каталог отсортирован свежим вверх, поэтому и периоды — от новых к старым.
    var eras = eraConfig.eras.slice().sort(function(a, b){ return (b.year || 0) - (a.year || 0); });
    for (i = 0; i < eras.length; i++) {
      var files = eraTrackFiles(eras[i]);
      if (files.length) {
        out.push({id: 'era:' + eras[i].year, name: 'Слушал в ' + eras[i].year,
                  tracks: files, smart: true, era: eras[i].year});
      }
    }
  }
  var played = [];
  for (i = 0; i < tracks.length; i++) if (playCountOf(tracks[i].file)) played.push(tracks[i]);
  if (played.length) {
    var recent = played.slice().sort(function(a, b){ return lastPlayedOf(b.file) - lastPlayedOf(a.file); });
    out.push({id: 'smart:recent', name: 'Недавно слушал', smart: true,
              tracks: recent.slice(0, SMART_LIMIT).map(function(t){ return t.file; })});
    var often = played.slice().sort(function(a, b){ return playCountOf(b.file) - playCountOf(a.file); });
    out.push({id: 'smart:often', name: 'Чаще всего', smart: true,
              note: (function(m){ return 'до ' + m + ' ' + relPlural(m, 'прослушивания', 'прослушиваний', 'прослушиваний'); })(playCountOf(often[0].file)),
              tracks: often.slice(0, SMART_LIMIT).map(function(t){ return t.file; })});
    // Пока истории нет, «ещё не слушал» — это весь каталог, и плейлист бесполезен.
    var never = tracks.filter(function(t){ return !playCountOf(t.file); });
    if (never.length) {
      out.push({id: 'smart:never', name: 'Ещё не слушал', smart: true,
                tracks: never.slice(0, SMART_LIMIT).map(function(t){ return t.file; })});
    }
  }
  return out;
}

// В заголовке — оба списка: умные плейлисты такие же строки этого экрана, и
// «0 плейлистов» при трёх видимых карточках выглядело ошибкой.
function plHeaderText() {
  var n = userPlaylists.length + smartPlaylists.length;
  return n + ' ' + relPlural(n, 'плейлист', 'плейлиста', 'плейлистов');
}

function refreshSmartPlaylists() {
  _eraByIndex = null;        // разметка или каталог поменялись
  smartPlaylists = buildSmartPlaylists();
  if (activeTab === 'playlists') {
    document.getElementById('playlistHeader').textContent = plHeaderText();
    renderPlaylists();
  }
}

// Умный плейлист во всём остальном ведёт себя как обычный, поэтому поиск по id
// должен видеть оба списка.
function findPlaylist(id) {
  var pl = userPlaylists.find(function(p){ return p.id === id; });
  return pl || smartPlaylists.find(function(p){ return p.id === id; }) || null;
}

var userPlaylists = [];
var plEditId = null;
var plEditTracks = [];

// ── Записи плейлистов и офлайн ──
//
// Правило намеренно несимметричное. Создание, переименование, добавление
// треков и порядок уезжают на сервер: это осознанная работа, терять её из-за
// пропавшей связи нельзя. Удаление — не уезжает: сервер здесь истина, а на
// клиенте легко нажать не то, и разослать случайную потерю по всем устройствам
// хуже, чем не выполнить удаление. Удалённое без сети вернётся при первой же
// синхронизации — список просто перечитывается с сервера.
var PL_QUEUED_ACTIONS = {create: 1, update: 1, reorder: 1};
var _flushingPl = false;

function plTempId() {
  return 'loc' + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36);
}

function plMirror() {
  if (_curFolder) lsSet('_vc_playlists_' + _curFolder, JSON.stringify(userPlaylists));
}

// Отправка с очередью. onOk зовётся и при постановке в очередь: для интерфейса
// изменение уже случилось, иначе пользователь видел бы, что его правка пропала.
function plWrite(payload, onOk) {
  fetch('/api/playlists', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      // SW подменяет неудачный /api/* телом {error:'offline'} со статусом 200,
      // поэтому смотрим на тело, а не на r.ok.
      if (!d || d.error || !d.ok) throw new Error('offline');
      if (onOk) onOk(d, false);
    })
    .catch(function() {
      if (!PL_QUEUED_ACTIONS[payload.action]) {
        showToast('Нет связи с сервером — изменение не сохранено');
        return;
      }
      queuePlaylistOp(payload);
      if (onOk) onOk({ok: true}, true);
    });
}

function queuePlaylistOp(payload) {
  relDbGet('pendingPlaylists', function(q) {
    q = q || [];
    // Правка плейлиста, созданного тут же без сети, вливается в его «create»:
    // отдельный «update» сервер не понял бы — у него нет такого id, он
    // присваивает свой.
    for (var i = 0; i < q.length; i++) {
      if (q[i].id !== payload.id) continue;
      if (payload.action === 'update' && (q[i].action === 'create' || q[i].action === 'update')) {
        if (payload.name !== undefined) q[i].name = payload.name;
        if (payload.tracks !== undefined) q[i].tracks = payload.tracks;
        relDbSet('pendingPlaylists', q);
        return;
      }
    }
    if (payload.action === 'reorder') {
      // Порядок важен только последний.
      q = q.filter(function(op){ return op.action !== 'reorder'; });
    }
    q.push(payload);
    relDbSet('pendingPlaylists', q.slice(-200));
  });
}

// Досылаем накопленное по очереди: порядок важен, «create» должен уйти раньше
// правок того же плейлиста.
function flushPlaylists() {
  if (_flushingPl) return;
  relDbGet('pendingPlaylists', function(q) {
    if (!q || !q.length) return;
    _flushingPl = true;
    var i = 0;
    (function next() {
      if (i >= q.length) {
        _flushingPl = false;
        relDbSet('pendingPlaylists', []);
        loadUserPlaylists();     // итог берём с сервера: он присвоил свои id
        showToast('Плейлисты синхронизированы');
        return;
      }
      var op = q[i++];
      fetch('/api/playlists', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(op)})
        .then(function(r){ return r.json(); })
        .then(function(d) {
          if (!d || d.error) throw new Error('offline');
          next();
        })
        .catch(function() {
          _flushingPl = false;
          relDbSet('pendingPlaylists', q.slice(i - 1));   // с неотправленного
        });
    })();
  });
}

function loadUserPlaylists() {
  var folder = document.getElementById('folderSelect').value;
  function setHeader() {
    if (activeTab === 'playlists') {
      document.getElementById('playlistHeader').textContent = plHeaderText();
    }
  }
  if (!folder) { userPlaylists = []; renderPlaylists(); setHeader(); return; }
  if (_isOffline) {
    try {
      var saved = localStorage.getItem('_vc_playlists_' + folder);
      userPlaylists = saved ? JSON.parse(saved) : [];
    } catch(e){ userPlaylists = []; }
    renderPlaylists();
    setHeader();
    return;
  }
  fetch('/api/playlists', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder: folder, action: 'list'})})
  .then(function(r){return r.json()}).then(function(d) {
    // An unreachable server comes back as {error:'offline'} from the SW with a
    // 200, so an unguarded `d.playlists || []` used to wipe both the list and
    // its localStorage copy — the very cache the offline mode reads from.
    if (!d || d.error || !d.playlists) { setHeader(); return; }
    userPlaylists = d.playlists;
    lsSet('_vc_playlists_' + folder, JSON.stringify(userPlaylists));
    renderPlaylists();
    setHeader();
  }).catch(function(){ setHeader(); });
}

var expandedPlaylist = null;

// Умные и обычные плейлисты рисуются одним списком: карточка и раскрытый
// список треков у них общие, различий ровно два — у умных нет перетаскивания и
// редактирования, потому что их состав вычисляется, а не хранится.
function plCardHtml(pl, idx) {
  var smart = idx < 0;
  var isExp = expandedPlaylist === pl.id;
  var esc_id = pl.id.replace(/'/g, "\\'");
  var html = '<div class="album-card ' + (smart ? 'pl-smart-card' : 'pl-drag-card')
    + (isExp ? ' active' : '') + '" data-plid="' + esc(pl.id) + '"'
    + (smart ? '' : ' data-plidx="' + idx + '" draggable="true"')
    + ' onclick="togglePlaylistExpand(\'' + esc_id + '\')"'
    + (smart ? '' : ' oncontextmenu="showPlCtxMenu(event,\'' + esc_id + '\')" data-longpress-pl="' + esc(pl.id) + '"')
    + '>'
    + '<div class="album-cover" style="position:relative;overflow:hidden">' + buildPlCover(pl) + '</div>'
    + '<div class="album-info"><div class="album-name">' + esc(pl.name) + '</div>'
    + '<div class="album-count">' + pl.tracks.length + ' ' + relPlural(pl.tracks.length, 'трек', 'трека', 'треков')
    + (smart && pl.note ? ' · ' + esc(pl.note) : '') + '</div></div>'
    + '<button class="shuffle-btn" style="width:32px;height:32px;flex-shrink:0" onclick="event.stopPropagation();cachePlaylist(\'' + esc_id + '\')" data-tip="Кэшировать"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg></button>'
    + (!smart && userRole !== 'demo' ? '<button class="shuffle-btn" style="width:32px;height:32px;flex-shrink:0" onclick="event.stopPropagation();editPlaylist(\'' + esc_id + '\')" data-tip="Редактировать"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 000-1.41l-2.34-2.34a1 1 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg></button>' : '')
    + '</div>';
  html += '<div class="album-tracks' + (isExp ? ' open' : '') + '">';
  if (isExp) {
    html += '<div style="padding:6px 12px"><button class="folder-btn folder-btn-primary" style="width:100%;font-size:11px;padding:6px" onclick="event.stopPropagation();playPlaylist(\'' + esc_id + '\')">&#9654; Воспроизвести</button></div>';
    for (var ti = 0; ti < pl.tracks.length; ti++) {
      var file = pl.tracks[ti];
      var t = tracks.find(function(tr){return tr.file===file});
      if (!t) continue;
      var trackIdx = tracks.indexOf(t);
      var plCachedDot = isTrackCached(file) ? '<span style="width:5px;height:5px;border-radius:50%;background:#52b788;flex-shrink:0;margin-left:auto"></span>' : '';
      var plOffDis = _isOffline && !isTrackCached(file);
      html += '<div class="playlist-item' + (trackIdx === currentIdx ? ' active' : '') + '"'
        + (plOffDis ? ' style="padding-left:20px;opacity:0.3;pointer-events:none"' : ' style="padding-left:20px"')
        + (plOffDis ? '' : ' onclick="event.stopPropagation();playFromPlaylist(\'' + esc_id + '\',' + ti + ')"') + '>'
        + '<div class="info"><div class="name" style="font-size:12px">' + esc(t.title) + '</div>'
        + '<div class="artist" style="font-size:11px">' + esc(t.artist) + '</div></div>' + plCachedDot + '</div>';
    }
  }
  html += '</div>';
  return html;
}

// Два раздела с постоянными заголовками: у умных и пользовательских плейлистов
// разная природа (состав вычисляется против составленного руками), и без явной
// границы они читались как один список. Кнопки разложены по смыслу: «Периоды»
// настраивает умные, «Создать» — пользовательские.
function renderPlaylists() {
  var demo = userRole === 'demo';
  var html = '<div class="pl-section"><span class="pl-section-title">Умные плейлисты</span>'
    + (demo ? '' : '<button class="folder-btn folder-btn-secondary" onclick="openErasModal()" data-tip="Периоды прослушивания">Периоды</button>')
    + '</div>';
  // Радиостанция — не плейлист: состава у неё нет, она его порождает. Поэтому
  // своя карточка, а не строка в общем списке.
  html += '<div class="album-card radio-card' + (radioOn ? ' on' : '') + '" onclick="openRadioModal()">'
    + '<div class="album-cover" style="display:flex;align-items:center;justify-content:center;background:rgba(233,69,96,0.12);color:#e94560">'
    + '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a1 1 0 0 1 .45 1.9L8.2 6h11.3A2.5 2.5 0 0 1 22 8.5v10A2.5 2.5 0 0 1 19.5 21h-15A2.5 2.5 0 0 1 2 18.5v-10a2.5 2.5 0 0 1 1.6-2.33l8-3.98A1 1 0 0 1 12 2zm5 7a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zm0 2a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3zM5 10h6v2H5v-2zm0 4h6v2H5v-2z"/></svg></div>'
    + '<div class="album-info"><div class="album-name">Радиостанция</div>'
    + '<div class="album-count">' + (radioOn ? 'играет · ' + esc(radioSummary()) : 'бесконечный поток по критериям') + '</div></div>'
    + '</div>';
  if (smartPlaylists.length) {
    for (var si = 0; si < smartPlaylists.length; si++) html += plCardHtml(smartPlaylists[si], -1);
  } else {
    html += '<div class="rel-note" style="padding:2px 14px 8px">Соберутся сами, когда накопится история прослушиваний'
      + (demo ? '' : ' или вы разметите периоды') + '.</div>';
  }
  html += '<div class="pl-section"><span class="pl-section-title">Мои плейлисты</span>'
    + (demo ? '' : '<button class="folder-btn folder-btn-secondary" onclick="createPlaylist()">+ Создать</button>')
    + '</div>';
  if (!userPlaylists.length) {
    html += '<div class="rel-note" style="padding:2px 14px 8px">Пока ни одного.</div>';
  }
  for (var i = 0; i < userPlaylists.length; i++) html += plCardHtml(userPlaylists[i], i);
  document.getElementById('playlistsList').innerHTML = html;
  initPlDrag();
}

// ── Окно разметки периодов ──
// Правим в номерах — так думает пользователь, глядя на список. В якоря номера
// переводятся при сохранении.
var _eraDraft = [];
var RADIO_GENRE_MIN = 10;     // мельче — прячем за чипом «ещё»
var _radioGenresOpen = false;

function openErasModal() {
  if (!tracks.length) { showToast('Сначала откройте каталог'); return; }
  _eraDraft = (eraConfig.eras || []).map(function(e) {
    var r = resolveEra(e);
    return {year: e.year, from: eraFileNo(r.from), to: eraFileNo(r.to)};
  });
  _eraDraft.sort(function(a, b){ return a.from - b.from; });
  document.getElementById('erasEnabled').checked = !!eraConfig.enabled;
  renderEraRows();
  document.getElementById('erasOverlay').classList.add('show');
}

function closeErasModal() { document.getElementById('erasOverlay').classList.remove('show'); }

function renderEraRows() {
  var html = '';
  for (var i = 0; i < _eraDraft.length; i++) {
    var d = _eraDraft[i];
    html += '<div class="era-row">'
      + '<input type="number" style="width:74px" placeholder="год" value="' + esc(String(d.year || '')) + '" oninput="setEraField(' + i + ',\'year\',this.value)">'
      + '<span class="era-sep">треки №</span>'
      + '<input type="number" style="flex:1" placeholder="с" value="' + esc(String(d.from || '')) + '" oninput="setEraField(' + i + ',\'from\',this.value)">'
      + '<span class="era-sep">–</span>'
      + '<input type="number" style="flex:1" placeholder="по" value="' + esc(String(d.to || '')) + '" oninput="setEraField(' + i + ',\'to\',this.value)">'
      + '<button class="shuffle-btn" style="width:30px;height:30px;flex-shrink:0;color:#e94560" onclick="removeEraRow(' + i + ')" data-tip="Убрать">&times;</button>'
      + '</div>'
      + '<div class="era-bound">'
      +   '<button class="era-pick-link" id="eraBoundFrom' + i + '" onclick="openEraPick(' + i + ',\'from\')"></button>'
      +   '<button class="era-pick-link" id="eraBoundTo' + i + '" onclick="openEraPick(' + i + ',\'to\')"></button>'
      + '</div>';
  }
  document.getElementById('erasRows').innerHTML = html;
  for (var k = 0; k < _eraDraft.length; k++) paintEraBounds(k);
  document.getElementById('erasHint').textContent =
    'В каталоге ' + tracks.length + ' треков, номера с ' + eraFileNo(0) + ' по ' + eraFileNo(tracks.length - 1)
    + '. Первый в списке — самый свежий.';
}

function setEraField(i, field, value) {
  if (!_eraDraft[i]) return;
  _eraDraft[i][field] = value;      // перерисовку не делаем: сбросился бы фокус
  paintEraBounds(i);
}

// Показываем, какие треки легли на края: номер в имени файла и позиция в списке
// расходятся, и без этого пользователь не мог бы проверить себя.
function paintEraBounds(i) {
  var d = _eraDraft[i];
  if (!d) return;
  var pair = [['From', d.from], ['To', d.to]];
  for (var k = 0; k < pair.length; k++) {
    var el = document.getElementById('eraBound' + pair[k][0] + i);
    if (!el) continue;
    var t = tracks[eraPosOfNumber(pair[k][1])];
    var label = k === 0 ? 'с: ' : 'по: ';
    el.textContent = t ? (label + (t.artist || '?') + ' · ' + (t.title || '?')) : (label + 'выбрать трек');
  }
}

// ── Выбор границы по списку треков ──
var _eraPickTarget = null;
var ERA_PICK_MAX = 400;      // 2440 строк разом подвешивают вкладку

function openEraPick(i, field) {
  if (!_eraDraft[i]) return;
  _eraPickTarget = {i: i, field: field};
  document.getElementById('eraPickTitle').textContent =
    'Период ' + (_eraDraft[i].year || '') + ': ' + (field === 'from' ? 'первый трек' : 'последний трек');
  var q = document.getElementById('eraPickSearch');
  q.value = '';
  renderEraPick('');
  document.getElementById('eraPickOverlay').classList.add('show');
  // Сразу подкручиваем к текущей границе — обычно правят рядом с ней.
  var cur = document.getElementById('eraPickCur');
  if (cur && cur.scrollIntoView) cur.scrollIntoView({block: 'center'});
}

function closeEraPick() {
  document.getElementById('eraPickOverlay').classList.remove('show');
  _eraPickTarget = null;
}

function renderEraPick(query) {
  if (!_eraPickTarget) return;
  var q = normSearch(query || '');
  var curNo = _eraDraft[_eraPickTarget.i] ? _eraDraft[_eraPickTarget.i][_eraPickTarget.field] : 0;
  var curPos = eraPosOfNumber(curNo);
  var html = '', shown = 0, total = 0;
  for (var i = 0; i < tracks.length; i++) {
    var t = tracks[i];
    if (q && normSearch((t.artist || '') + ' ' + (t.title || '') + ' ' + t.file).indexOf(q) < 0) continue;
    total++;
    if (shown >= ERA_PICK_MAX) continue;
    shown++;
    html += '<div class="era-pick-row"' + (i === curPos ? ' id="eraPickCur" style="background:rgba(233,69,96,0.12)"' : '')
      + ' onclick="pickEraTrack(' + i + ')">'
      + '<span class="era-pick-no">' + eraFileNo(i) + '</span>'
      + '<span class="era-pick-name">' + esc(t.artist || '') + ' · ' + esc(t.title || '') + '</span></div>';
  }
  document.getElementById('eraPickList').innerHTML = html || '<div style="padding:14px;color:rgba(255,255,255,0.3);font-size:12px">Ничего не нашлось</div>';
  document.getElementById('eraPickNote').textContent = total > shown
    ? ('Показано ' + shown + ' из ' + total + ' – уточните поиск')
    : (total ? ('Найдено: ' + total) : '');
}

function pickEraTrack(i) {
  if (!_eraPickTarget) return;
  var d = _eraDraft[_eraPickTarget.i];
  if (d) d[_eraPickTarget.field] = eraFileNo(i);
  closeEraPick();
  renderEraRows();
}

function addEraRow() {
  var last = _eraDraft.length ? _eraDraft[_eraDraft.length - 1] : null;
  var from = last ? (parseInt(last.to, 10) || 0) + 1 : eraFileNo(0);
  _eraDraft.push({year: '', from: from, to: eraFileNo(tracks.length - 1)});
  renderEraRows();
}

function removeEraRow(i) { _eraDraft.splice(i, 1); renderEraRows(); }

function applyEras() {
  var rows = [];
  for (var i = 0; i < _eraDraft.length; i++) {
    var d = _eraDraft[i];
    var year = parseInt(d.year, 10);
    if (!year) continue;                       // строка без года — просто пустая
    // Ввод — в номерах файлов, хранение — в позициях и якорях.
    var from = eraPosOfNumber(d.from) + 1, to = eraPosOfNumber(d.to) + 1;
    if (from > to) { var sw = from; from = to; to = sw; }
    rows.push({year: year, from_pos: from, to_pos: to,
               from: eraAnchorAt(from), to: eraAnchorAt(to),
               // Период, начинающийся с первого трека каталога, остаётся открытым
               // сверху: новые треки приходят наверх и должны попадать в текущий
               // год сами, без правки границ.
               open_start: from === 1});
  }
  eraConfig.enabled = document.getElementById('erasEnabled').checked;
  eraConfig.eras = rows;
  saveEras();
  closeErasModal();
  showToast(eraConfig.enabled ? 'Периодов: ' + rows.length : 'Периоды выключены');
}

function togglePlaylistExpand(id) {
  expandedPlaylist = expandedPlaylist === id ? null : id;
  renderPlaylists();
}

// ── Playlist drag-and-drop reorder (desktop + mobile long-press) ──
var _plDragIdx = null;
var _plDragEl = null;
var _plTouchTimer = null;
var _plTouchDragging = false;
var _plGhost = null;
var _plCards = [];

function initPlDrag() {
  _plCards = Array.from(document.querySelectorAll('.pl-drag-card'));
  // Container-level dragover/drop to catch drops between cards (on album-tracks divs)
  var container = document.getElementById('playlistsList');
  container.ondragover = function(e) { e.preventDefault(); };
  container.ondrop = function(e) {
    e.preventDefault();
    _plCards.forEach(function(c){c.classList.remove('pl-drag-over')});
    if (_plDragIdx === null) return;
    // Find nearest card by Y position
    var y = e.clientY;
    var toIdx = _plDragIdx;
    for (var ci = 0; ci < _plCards.length; ci++) {
      var rect = _plCards[ci].getBoundingClientRect();
      if (y >= rect.top && y <= rect.bottom) { toIdx = parseInt(_plCards[ci].dataset.plidx); break; }
      if (y < rect.top) { toIdx = parseInt(_plCards[ci].dataset.plidx); break; }
    }
    if (_plDragIdx !== toIdx) plReorder(_plDragIdx, toIdx);
    _plDragIdx = null;
  };
  _plCards.forEach(function(card) {
    // Desktop drag
    card.addEventListener('dragstart', function(e) {
      _plDragIdx = parseInt(card.dataset.plidx);
      card.style.opacity = '0.4';
      e.dataTransfer.effectAllowed = 'move';
    });
    card.addEventListener('dragend', function() {
      card.style.opacity = '';
      _plDragIdx = null;
      _plCards.forEach(function(c){c.classList.remove('pl-drag-over')});
    });
    card.addEventListener('dragover', function(e) { e.preventDefault(); });
    card.addEventListener('dragenter', function(e) {
      e.preventDefault();
      _plCards.forEach(function(c){c.classList.remove('pl-drag-over')});
      card.classList.add('pl-drag-over');
    });
    card.addEventListener('drop', function(e) {
      e.preventDefault();
      card.classList.remove('pl-drag-over');
      var toIdx = parseInt(card.dataset.plidx);
      if (_plDragIdx !== null && _plDragIdx !== toIdx) {
        plReorder(_plDragIdx, toIdx);
      }
      _plDragIdx = null;
    });
    // Mobile long-press drag
    card.addEventListener('touchstart', function(e) {
      if (e.touches.length !== 1) return;
      var startY = e.touches[0].clientY;
      _plTouchDragging = false;
      _plDragIdx = parseInt(card.dataset.plidx);
      _plTouchTimer = setTimeout(function() {
        _plTouchDragging = true;
        _plDragEl = card;
        card.style.opacity = '0.4';
        // Create ghost element
        _plGhost = document.createElement('div');
        _plGhost.textContent = card.querySelector('.album-name').textContent;
        _plGhost.style.cssText = 'position:fixed;left:16px;padding:8px 16px;background:#e94560;color:#fff;border-radius:8px;font-size:13px;pointer-events:none;z-index:9999;transition:none;';
        _plGhost.style.top = startY + 'px';
        document.body.appendChild(_plGhost);
      }, 400);
    }, {passive: true});
    card.addEventListener('touchmove', function(e) {
      if (!_plTouchDragging) {
        clearTimeout(_plTouchTimer);
        return;
      }
      e.preventDefault();
      var touch = e.touches[0];
      if (_plGhost) _plGhost.style.top = touch.clientY + 'px';
      // Find card under finger
      _plCards.forEach(function(c){c.classList.remove('pl-drag-over')});
      var el = document.elementFromPoint(touch.clientX, touch.clientY);
      if (el) {
        var target = el.closest('.pl-drag-card');
        if (target) target.classList.add('pl-drag-over');
      }
    }, {passive: false});
    card.addEventListener('touchend', function(e) {
      clearTimeout(_plTouchTimer);
      if (!_plTouchDragging) { _plDragIdx = null; return; }
      _plTouchDragging = false;
      if (_plDragEl) _plDragEl.style.opacity = '';
      _plDragEl = null;
      if (_plGhost) { _plGhost.remove(); _plGhost = null; }
      _plCards.forEach(function(c){c.classList.remove('pl-drag-over')});
      // Find drop target
      if (e.changedTouches.length) {
        var touch = e.changedTouches[0];
        var el = document.elementFromPoint(touch.clientX, touch.clientY);
        if (el) {
          var target = el.closest('.pl-drag-card');
          if (target) {
            var toIdx = parseInt(target.dataset.plidx);
            if (_plDragIdx !== null && _plDragIdx !== toIdx) {
              plReorder(_plDragIdx, toIdx);
            }
          }
        }
      }
      _plDragIdx = null;
    });
    card.addEventListener('touchcancel', function() {
      clearTimeout(_plTouchTimer);
      _plTouchDragging = false;
      if (_plDragEl) _plDragEl.style.opacity = '';
      _plDragEl = null;
      if (_plGhost) { _plGhost.remove(); _plGhost = null; }
      _plCards.forEach(function(c){c.classList.remove('pl-drag-over')});
      _plDragIdx = null;
    });
  });
}

function plReorder(fromIdx, toIdx) {
  var item = userPlaylists.splice(fromIdx, 1)[0];
  userPlaylists.splice(toIdx, 0, item);
  expandedPlaylist = null;
  renderPlaylists();
  // Save to server
  var order = userPlaylists.map(function(p){return p.id});
  var folder = document.getElementById('folderSelect').value;
  // Локальный список уже переставлен выше, поэтому здесь только отправка.
  plWrite({folder: folder, action: 'reorder', order: order}, function(d, queued) {
    if (queued) plMirror();
  });
}

function playFromPlaylist(plId, trackIndex) {
  stopRadio();
  var pl = findPlaylist(plId);
  if (!pl) return;
  playQueue = [];
  for (var i = 0; i < pl.tracks.length; i++) {
    var idx = tracks.findIndex(function(t){return t.file===pl.tracks[i]});
    if (idx >= 0) playQueue.push(idx);
  }
  // Find position in queue
  var file = pl.tracks[trackIndex];
  var tIdx = tracks.findIndex(function(t){return t.file===file});
  playQueuePos = playQueue.indexOf(tIdx);
  if (playQueuePos < 0) playQueuePos = 0;
  selectTrack(playQueue[playQueuePos], true);
}

// Обложка на карточке плейлиста. Раньше <img> вели прямо на /api/cover/ и без
// запасного пути: без сервера запрос падал, и браузер рисовал свой значок
// битой картинки — хотя обложка лежала в кэше, а на совсем пустой случай у нас
// есть заглушка. Список треков этой болезнью не болел, там onerror был.
function plCoverImg(file) {
  var enc = encodeURIComponent(file);
  return '<img src="/api/cover/' + enc + '" style="width:100%;height:100%;object-fit:cover"'
       + ' onerror="plCoverFallback(this,\'' + enc + '\')">';
}

function plCoverFallback(img, encodedFile) {
  img.onerror = null;                       // без этого возможна петля
  getCachedCover(decodeURIComponent(encodedFile), function(buf) {
    if (buf) { img.src = URL.createObjectURL(new Blob([buf])); return; }
    // В кэше тоже нет — показываем свою заглушку, а не битую картинку.
    var ph = document.createElement('div');
    ph.className = 'pl-cover-ph';
    ph.innerHTML = '&#9835;';
    if (img.parentNode) img.parentNode.replaceChild(ph, img);
  });
}

function buildPlCover(pl) {
  // 4 covers from last 4 tracks
  var covers = [];
  for (var i = pl.tracks.length - 1; i >= 0 && covers.length < 4; i--) {
    var file = pl.tracks[i];
    var t = tracks.find(function(tr){return tr.file === file});
    if (t && t.has_cover) covers.push(t.file);
  }
  if (covers.length === 0) return '<div class="pl-cover-ph">&#9835;</div>';
  if (covers.length < 4) return plCoverImg(covers[0]);
  return '<div style="display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;width:100%;height:100%">'
    + covers.map(plCoverImg).join('')
    + '</div>';
}

function createPlaylist() {
  plEditId = null;
  plEditTracks = [];
  document.getElementById('plEditName').value = '';
  document.getElementById('plEditTitle').textContent = 'Новый плейлист';
  document.getElementById('plDeleteBtn').style.display = 'none';
  renderPlEditTracks();
  document.getElementById('plEditOverlay').classList.add('show');
}

function editPlaylist(id) {
  var pl = userPlaylists.find(function(p){return p.id===id});
  if (!pl) return;
  plEditId = id;
  plEditTracks = pl.tracks.slice();
  document.getElementById('plEditName').value = pl.name;
  document.getElementById('plEditTitle').textContent = 'Редактировать';
  document.getElementById('plDeleteBtn').style.display = '';
  renderPlEditTracks();
  document.getElementById('plEditOverlay').classList.add('show');
}

function renderPlEditTracks() {
  var html = '';
  for (var i = 0; i < plEditTracks.length; i++) {
    var file = plEditTracks[i];
    var t = tracks.find(function(tr){return tr.file === file});
    var name = t ? esc(t.title) : esc(file);
    var artist = t ? esc(t.artist) : '';
    html += '<div class="playlist-item" draggable="true" data-pi="'+i+'" ondragstart="pleDragStart(event,'+i+')" ondragover="pleDragOver(event,'+i+')" ondrop="pleDrop(event,'+i+')" ondragend="pleDragEnd(event)">'
      + '<span class="drag-handle" style="cursor:grab;color:rgba(255,255,255,0.2)">&#8801;</span>'
      + '<div class="info" style="flex:1;min-width:0"><div class="name" style="font-size:12px">' + name + '</div>'
      + (artist ? '<div class="artist" style="font-size:11px">' + artist + '</div>' : '') + '</div>'
      + '<button class="track-edit-btn" onclick="plEditTracks.splice('+i+',1);renderPlEditTracks()" style="color:#e94560">&times;</button></div>';
  }
  if (!html) html = '<div style="padding:16px;text-align:center;color:rgba(255,255,255,0.2);font-size:12px">Добавьте треки</div>';
  document.getElementById('plEditTracks').innerHTML = html;
}

var pleDragIdx = null;
function pleDragStart(e, i) {
  pleDragIdx = i;
  e.dataTransfer.effectAllowed = 'move';
  e.target.closest('.playlist-item').classList.add('dragging');
}
function pleDragEnd(e) {
  pleDragIdx = null;
  var items = document.querySelectorAll('#plEditTracks .playlist-item');
  for (var j = 0; j < items.length; j++) items[j].classList.remove('dragging', 'drag-over');
}
function pleDragOver(e, i) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  var items = document.querySelectorAll('#plEditTracks .playlist-item');
  for (var j = 0; j < items.length; j++) items[j].classList.remove('drag-over');
  e.target.closest('.playlist-item').classList.add('drag-over');
}
function pleDrop(e, t) {
  e.preventDefault();
  if (pleDragIdx === null || pleDragIdx === t) return;
  var item = plEditTracks.splice(pleDragIdx, 1)[0];
  plEditTracks.splice(t, 0, item);
  pleDragIdx = null;
  renderPlEditTracks();
}

function plAddTracks() {
  var html = '';
  for (var i = 0; i < tracks.length; i++) {
    var t = tracks[i];
    var inPl = plEditTracks.indexOf(t.file) >= 0;
    html += '<label class="playlist-item pl-add-item" data-search="' + esc(t.title+' '+t.artist).toLowerCase() + '" style="cursor:pointer">'
      + '<input type="checkbox" value="' + esc(t.file) + '"' + (inPl ? ' checked' : '') + ' style="accent-color:#e94560;flex-shrink:0">'
      + '<div class="info" style="flex:1;min-width:0"><div class="name" style="font-size:12px">' + esc(t.title) + '</div>'
      + '<div class="artist" style="font-size:11px">' + esc(t.artist) + '</div></div></label>';
  }
  document.getElementById('plAddList').innerHTML = html;
  document.getElementById('plAddSearch').value = '';
  document.getElementById('plAddOverlay').classList.add('show');
}

function filterPlAddTracks(q) {
  q = q.toLowerCase();
  var items = document.querySelectorAll('.pl-add-item');
  for (var i = 0; i < items.length; i++) {
    items[i].style.display = !q || items[i].getAttribute('data-search').indexOf(q) >= 0 ? '' : 'none';
  }
}

function confirmPlAdd(where) {
  var checks = document.querySelectorAll('#plAddList input:checked');
  var files = [];
  for (var i = 0; i < checks.length; i++) files.push(checks[i].value);
  // Add new files that aren't already in plEditTracks
  var newFiles = [];
  for (var j = 0; j < files.length; j++) {
    if (plEditTracks.indexOf(files[j]) < 0) newFiles.push(files[j]);
  }
  if (where === 'start') {
    plEditTracks = newFiles.concat(plEditTracks);
  } else if (where === 'order') {
    // Merge: all tracks (old + new) sorted by their position in the main tracklist
    var allFiles = {};
    for (var k = 0; k < plEditTracks.length; k++) allFiles[plEditTracks[k]] = true;
    for (var m = 0; m < newFiles.length; m++) allFiles[newFiles[m]] = true;
    plEditTracks = [];
    for (var n = 0; n < tracks.length; n++) {
      if (allFiles[tracks[n].file]) plEditTracks.push(tracks[n].file);
    }
  } else {
    plEditTracks = plEditTracks.concat(newFiles);
  }
  document.getElementById('plAddOverlay').classList.remove('show');
  renderPlEditTracks();
}

function deletePlEdit() {
  if (!plEditId) return;
  var pl = userPlaylists.find(function(p){return p.id===plEditId});
  var name = pl ? pl.name : 'плейлист';
  showConfirm('Удалить плейлист «' + name + '»?', function() {
    var folder = document.getElementById('folderSelect').value;
    fetch('/api/playlists', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({folder: folder, action: 'delete', id: plEditId})})
    .then(function(r){return r.json()}).then(function(d) {
      // Удаление не откладывается: сервер здесь истина. Без связи оно просто не
      // происходит, и об этом надо сказать — молчание выглядело бы как успех.
      if (!d || d.error || !d.ok) { showToast('Удаление доступно только при связи с сервером'); return; }
      showToast('Плейлист удалён');
      document.getElementById('plEditOverlay').classList.remove('show');
      loadUserPlaylists();
    }).catch(function(){ showToast('Удаление доступно только при связи с сервером'); });
  }, 'Удалить');
}

function savePlEdit() {
  var folder = _curFolder || document.getElementById('folderSelect').value;
  var name = document.getElementById('plEditName').value.trim() || 'Плейлист';
  var isNew = !plEditId;
  // Новому плейлисту нужен временный id: без сети сервер его ещё не присвоил, а
  // показать и дать править нужно уже сейчас. При досылке сервер выдаст свой,
  // и список перечитается.
  var id = plEditId || plTempId();
  var tracks_ = plEditTracks.slice();
  var body = {folder: folder, action: isNew ? 'create' : 'update',
              name: name, tracks: tracks_, id: id};
  plWrite(body, function(d, queued) {
    if (queued) {
      // Применяем у себя: для пользователя правка уже состоялась.
      if (isNew) userPlaylists.push({id: id, name: name, tracks: tracks_});
      else for (var i = 0; i < userPlaylists.length; i++) {
        if (userPlaylists[i].id === id) { userPlaylists[i].name = name; userPlaylists[i].tracks = tracks_; }
      }
      plMirror();
      renderPlaylists();
      showToast((isNew ? 'Плейлист создан' : 'Плейлист обновлён') + ' — уйдёт на сервер при подключении');
    } else {
      showToast(isNew ? 'Плейлист создан' : 'Плейлист обновлён');
      loadUserPlaylists();
    }
    document.getElementById('plEditOverlay').classList.remove('show');
  });
}

function playPlaylist(id) {
  stopRadio();          // попросили конкретный плейлист — станция больше не ведёт
  var pl = findPlaylist(id);
  if (!pl || !pl.tracks.length) return;
  // Build play queue from playlist tracks
  playQueue = [];
  for (var i = 0; i < pl.tracks.length; i++) {
    var idx = tracks.findIndex(function(t){return t.file===pl.tracks[i]});
    if (idx >= 0) playQueue.push(idx);
  }
  if (playQueue.length) {
    playQueuePos = 0;
    selectTrack(playQueue[0], true);
    showToast('Играет: ' + pl.name);
  }
}

function showImportHelp() {
  document.getElementById('importHelpOverlay').classList.add('show');
}

function showRolesHelp() {
  document.getElementById('rolesHelpOverlay').classList.add('show');
}

function adminCreateUser() {
  var u = document.getElementById('newUserName').value.trim();
  var p = document.getElementById('newUserPw').value;
  if (!u || !p) { showToast('Заполните логин и пароль'); return; }
  var role = document.getElementById('newUserRole').value;
  fetch('/api/admin/create_user', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: u, password: p, role: role})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok) { showToast('Пользователь создан'); document.getElementById('newUserName').value=''; document.getElementById('newUserPw').value=''; loadAdminUsers(); }
    else showToast(d.error || 'Ошибка');
  });
}

function adminDeleteUser(username) {
  showConfirm('Удалить пользователя «' + username + '»?', function() {
    fetch('/api/admin/delete_user', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username: username})})
    .then(function(r){return r.json()}).then(function(d) {
      if (d.ok) { showToast('Удалён'); loadAdminUsers(); } else showToast(d.error);
    });
  }, 'Удалить');
}

var _pwChangeTarget = '';

function adminChangePassword(username) {
  _pwChangeTarget = username;
  document.getElementById('pwChangeUser').textContent = 'Пользователь: ' + username;
  document.getElementById('pwChangeNew').value = '';
  document.getElementById('pwChangeConfirm').value = '';
  document.getElementById('pwChangeOverlay').classList.add('show');
  document.getElementById('pwChangeNew').focus();
}

function submitPwChange() {
  var pw = document.getElementById('pwChangeNew').value;
  var pw2 = document.getElementById('pwChangeConfirm').value;
  if (!pw) { showToast('Введите пароль'); return; }
  if (pw !== pw2) { showToast('Пароли не совпадают'); return; }
  fetch('/api/admin/change_password', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: _pwChangeTarget, password: pw})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok) { showToast('Пароль изменён'); document.getElementById('pwChangeOverlay').classList.remove('show'); }
    else showToast(d.error);
  });
}

function togglePwVis(inputId, btn) {
  var inp = document.getElementById(inputId);
  if (inp.type === 'password') { inp.type = 'text'; btn.classList.add('visible'); }
  else { inp.type = 'password'; btn.classList.remove('visible'); }
}

function adminAddFolder(username) {
  var path = prompt('Путь к каталогу для ' + username + ':');
  if (!path) return;
  fetch('/api/admin/users').then(function(r){return r.json()}).then(function(d) {
    var users = d.users || [];
    for (var i = 0; i < users.length; i++) {
      if (users[i].username === username) {
        var folders = users[i].folders.slice();
        if (folders.indexOf(path) < 0) folders.push(path);
        fetch('/api/admin/set_folders', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({username: username, folders: folders})})
        .then(function(r){return r.json()}).then(function(dd) {
          if (dd.ok) { showToast('Каталог добавлен'); loadAdminUsers(); } else showToast(dd.error);
        });
        break;
      }
    }
  });
}

function adminRemoveFolder(username, folder) {
  fetch('/api/admin/users').then(function(r){return r.json()}).then(function(d) {
    var users = d.users || [];
    for (var i = 0; i < users.length; i++) {
      if (users[i].username === username) {
        var folders = users[i].folders.filter(function(f) { return f !== folder; });
        fetch('/api/admin/set_folders', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({username: username, folders: folders})})
        .then(function(r){return r.json()}).then(function(dd) {
          if (dd.ok) { showToast('Каталог удалён'); loadAdminUsers(); }
        });
        break;
      }
    }
  });
}

// ── Edit mode (drag reorder) ──
var isEditMode = false;
var isNumberedCatalog = false;
var editOrder = []; // array of filenames in current drag order

function checkIfNumbered() {
  if (!tracks.length) { isNumberedCatalog = false; return; }
  var numbered = 0;
  for (var i = 0; i < tracks.length; i++) {
    if (/^\d+\.\s/.test(tracks[i].file)) numbered++;
  }
  isNumberedCatalog = (numbered / tracks.length) > 0.8;
  document.getElementById('editBtn').style.display = (isNumberedCatalog && userRole !== 'demo') ? '' : 'none';
}

function startEdit() {
  isEditMode = true;
  editOrder = tracks.map(function(t) { return t.file; });
  document.getElementById('editBtn').style.display = 'none';
  document.getElementById('shuffleListBtn').style.display = 'none';
  document.getElementById('editControls').style.display = 'flex';
  renderTracks();
}

function cancelEdit() {
  isEditMode = false;
  document.getElementById('editBtn').style.display = isNumberedCatalog ? '' : 'none';
  document.getElementById('shuffleListBtn').style.display = '';
  document.getElementById('editControls').style.display = 'none';
  renderTracks();
}

function saveEdit() {
  var folder = document.getElementById('folderSelect').value;
  if (!folder) return;
  showConfirm('Сохранить новый порядок треков?', function() {
    fetch('/api/reorder', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({folder: folder, order: editOrder})})
    .then(function(r){return r.json()}).then(function(d) {
      if (d.ok) {
        showToast('Порядок сохранён');
        isEditMode = false;
        document.getElementById('editBtn').style.display = isNumberedCatalog ? '' : 'none';
        document.getElementById('editControls').style.display = 'none';
        loadFolder(folder);
      } else {
        showToast(d.error || 'Ошибка');
      }
    });
  }, 'Сохранить');
}

// Drag and drop handlers
var dragIdx = null;

function onDragStart(e, idx) {
  dragIdx = idx;
  e.dataTransfer.effectAllowed = 'move';
  e.target.closest('.playlist-item').classList.add('dragging');
  lastDragClientY = e.clientY;
  startDragAutoScroll();
}

function onDragEnd(e) {
  dragIdx = null;
  stopDragAutoScroll();
  var items = document.querySelectorAll('.playlist-item');
  for (var i = 0; i < items.length; i++) {
    items[i].classList.remove('dragging', 'drag-over');
  }
}

// ── Auto-scroll during drag ──
var dragAutoScrollId = null;
var lastDragClientY = 0;

function dragAutoScrollTick() {
  var tl = document.getElementById('trackList');
  if (!tl || (dragIdx === null && touchDragIdx === null)) {
    dragAutoScrollId = null;
    return;
  }
  var rect = tl.getBoundingClientRect();
  var y = lastDragClientY;
  var edge = 120; // px from edge where scroll starts
  var maxSpeed = 60; // px per frame at edge
  var speed = 0;

  if (y > rect.bottom - edge) {
    // Near bottom — scroll down
    var ratio = Math.min(1, (y - (rect.bottom - edge)) / edge);
    speed = ratio * ratio * maxSpeed; // quadratic acceleration
  } else if (y < rect.top + edge) {
    // Near top — scroll up
    var ratio = Math.min(1, ((rect.top + edge) - y) / edge);
    speed = -(ratio * ratio * maxSpeed);
  }

  if (speed !== 0) tl.scrollTop += speed;
  dragAutoScrollId = requestAnimationFrame(dragAutoScrollTick);
}

function startDragAutoScroll() {
  if (!dragAutoScrollId) dragAutoScrollId = requestAnimationFrame(dragAutoScrollTick);
}

function stopDragAutoScroll() {
  if (dragAutoScrollId) { cancelAnimationFrame(dragAutoScrollId); dragAutoScrollId = null; }
}

function onDragOver(e, idx) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  lastDragClientY = e.clientY;
  startDragAutoScroll();
  var items = document.querySelectorAll('.playlist-item');
  for (var i = 0; i < items.length; i++) items[i].classList.remove('drag-over');
  e.target.closest('.playlist-item').classList.add('drag-over');
}

function onDrop(e, targetIdx) {
  e.preventDefault();
  if (dragIdx === null || dragIdx === targetIdx) return;
  var item = editOrder.splice(dragIdx, 1)[0];
  editOrder.splice(targetIdx, 0, item);
  // Also reorder tracks array for display
  var tItem = tracks.splice(dragIdx, 1)[0];
  tracks.splice(targetIdx, 0, tItem);
  // Update IDs
  for (var i = 0; i < tracks.length; i++) tracks[i].id = i;
  dragIdx = null;
  renderTracks();
}

// Touch drag for mobile
var touchDragIdx = null;
var touchDragEl = null;
var touchStartY = 0;
var touchClone = null;

function onTouchDragStart(e, idx) {
  touchDragIdx = idx;
  touchStartY = e.touches[0].clientY;
  lastDragClientY = touchStartY;
  startDragAutoScroll();
  touchDragEl = e.target.closest('.playlist-item');
  // Create visual clone
  touchClone = touchDragEl.cloneNode(true);
  touchClone.style.position = 'fixed';
  touchClone.style.width = touchDragEl.offsetWidth + 'px';
  touchClone.style.opacity = '0.8';
  touchClone.style.zIndex = '100';
  touchClone.style.pointerEvents = 'none';
  touchClone.style.background = 'rgba(233,69,96,0.2)';
  touchClone.style.borderRadius = '8px';
  document.body.appendChild(touchClone);
  touchDragEl.style.opacity = '0.3';
  e.preventDefault();
}

function onTouchDragMove(e) {
  if (touchDragIdx === null) return;
  var y = e.touches[0].clientY;
  lastDragClientY = y;
  startDragAutoScroll();
  if (touchClone) {
    touchClone.style.top = (y - 25) + 'px';
    touchClone.style.left = touchDragEl.getBoundingClientRect().left + 'px';
  }
  var items = document.querySelectorAll('.playlist-item[data-idx]');
  for (var i = 0; i < items.length; i++) {
    var rect = items[i].getBoundingClientRect();
    items[i].classList.remove('drag-over');
    if (y > rect.top && y < rect.bottom) {
      items[i].classList.add('drag-over');
    }
  }
}

function onTouchDragEnd(e) {
  if (touchDragIdx === null) return;
  stopDragAutoScroll();
  if (touchClone) { touchClone.remove(); touchClone = null; }
  if (touchDragEl) { touchDragEl.style.opacity = ''; }
  // Find drop target
  var items = document.querySelectorAll('.playlist-item[data-idx]');
  var targetIdx = touchDragIdx;
  for (var i = 0; i < items.length; i++) {
    if (items[i].classList.contains('drag-over')) {
      targetIdx = parseInt(items[i].getAttribute('data-idx'));
      items[i].classList.remove('drag-over');
    }
  }
  if (touchDragIdx !== targetIdx) {
    var item = editOrder.splice(touchDragIdx, 1)[0];
    editOrder.splice(targetIdx, 0, item);
    var tItem = tracks.splice(touchDragIdx, 1)[0];
    tracks.splice(targetIdx, 0, tItem);
    for (var j = 0; j < tracks.length; j++) tracks[j].id = j;
  }
  touchDragIdx = null;
  touchDragEl = null;
  renderTracks();
}

// Fix viewport on iOS rotation
// Lock to portrait on mobile
if (window.innerWidth <= 768 && screen.orientation && screen.orientation.lock) {
  screen.orientation.lock('portrait').catch(function(){});
}
window.addEventListener('orientationchange', function() {
  setTimeout(function() { window.scrollTo(0,0); document.body.style.height = window.innerHeight + 'px'; }, 300);
});
window.addEventListener('resize', function() {
  document.body.style.height = window.innerHeight + 'px';
});

// Block pinch zoom and double-tap zoom
document.addEventListener('gesturestart', function(e) { e.preventDefault(); });
document.addEventListener('gesturechange', function(e) { e.preventDefault(); });
document.addEventListener('gestureend', function(e) { e.preventDefault(); });
document.addEventListener('touchstart', function(e) {
  if (e.touches.length > 1) e.preventDefault();
}, {passive: false});
var lastTap = 0;
document.addEventListener('touchend', function(e) {
  var now = Date.now();
  if (now - lastTap < 300 && e.target.tagName !== 'BUTTON' && e.target.tagName !== 'INPUT' && e.target.tagName !== 'SELECT') {
    e.preventDefault();
  }
  lastTap = now;
}, {passive: false});

// Touch drag for edit mode
document.addEventListener('touchmove', function(e) {
  if (touchDragIdx !== null) { onTouchDragMove(e); e.preventDefault(); }
}, {passive: false});
document.addEventListener('touchend', function(e) {
  if (touchDragIdx !== null) onTouchDragEnd(e);
});


// ── Header tap → scroll to top ──
function downloadCatalog() {
  var folder = document.getElementById('folderSelect').value;
  if (!folder) { showToast('Выберите каталог'); return; }
  showConfirm('Скачать все треки каталога как ZIP-архив?', function() {
    showToast('Подготовка архива...');
    window.location.href = '/api/admin/download_catalog?path=' + encodeURIComponent(folder);
  }, 'Скачать');
}

// ── Track Edit ──
var editingTrackIdx = -1;

// ── Context menu ──
var _ctxIdx = -1;
var _ctxLongTimer = null;

var _ctxSvgs = {
  next: 'M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z',
  cache: 'M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z',
  select: 'M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z',
  meta: 'M12 3v10.55A4 4 0 1014 17V7h4V3z',
  del: 'M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z'
};
function _ctxSvg(p) { return '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="' + p + '"/></svg>'; }
function _ctxPlSubmenu(handler) {
  if (userPlaylists.length === 0) return '<div class="ctx-item" style="color:rgba(255,255,255,0.3);pointer-events:none">Нет плейлистов</div>';
  var h = '';
  for (var p = 0; p < userPlaylists.length; p++) {
    var pl = userPlaylists[p];
    h += '<div class="ctx-item" onclick="' + handler + '(\'' + pl.id + '\',\'start\')">' + esc(pl.name) + ' <span style="color:rgba(255,255,255,0.25);margin-left:auto;font-size:10px">в начало</span></div>';
    h += '<div class="ctx-item" onclick="' + handler + '(\'' + pl.id + '\',\'end\')">' + esc(pl.name) + ' <span style="color:rgba(255,255,255,0.25);margin-left:auto;font-size:10px">в конец</span></div>';
  }
  return h;
}

function showCtxMenu(e, idx) {
  _ctxIdx = idx;
  var menu = document.getElementById('ctxMenu');
  var html = '';
  if (selectionMode && selectedCount() > 0) {
    // Bulk actions on the selected tracks (no "play next" — meaningless for many).
    html += '<div class="ctx-sub-header">Выбрано: ' + selectedCount() + '</div>';
    html += '<div class="ctx-item" onclick="bulkMeta()">' + _ctxSvg(_ctxSvgs.meta) + ' Запросить мета-данные</div>';
    html += '<div class="ctx-item" onclick="bulkCache()">' + _ctxSvg(_ctxSvgs.cache) + ' Кэшировать</div>';
    html += '<div class="ctx-sep"></div><div class="ctx-sub-header">Добавить в плейлист</div>';
    html += '<div class="ctx-sub">' + _ctxPlSubmenu('bulkAddToPlaylist') + '</div>';
    html += '<div class="ctx-sep"></div>';
    html += '<div class="ctx-item danger" onclick="bulkDelete()">' + _ctxSvg(_ctxSvgs.del) + ' Удалить выбранные</div>';
  } else {
    var isCached = idx >= 0 && idx < tracks.length && isTrackCached(tracks[idx].file);
    var _qn = (_ctxIdx >= 0 && tracks[_ctxIdx] && tracks[_ctxIdx].file === _forceNextFile);
    html += '<div class="ctx-item" onclick="ctxPlayNext()">' + _ctxSvg(_ctxSvgs.next)
          + (_qn ? ' Убрать из очереди' : ' Играть следующим') + '</div>';
    html += '<div class="ctx-item" onclick="ctxToggleCache()">' + _ctxSvg(_ctxSvgs.cache) + ' ' + (isCached ? 'Удалить из кэша' : 'Кэшировать') + '</div>';
    html += '<div class="ctx-item" onclick="ctxSelectStart()">' + _ctxSvg(_ctxSvgs.select) + ' Выбрать</div>';
    html += '<div class="ctx-sep"></div><div class="ctx-sub-header">Добавить в плейлист</div>';
    html += '<div class="ctx-sub">' + _ctxPlSubmenu('ctxAddToPlaylist') + '</div>';
    html += '<div class="ctx-sep"></div>';
    html += '<div class="ctx-item danger" onclick="ctxDelete()">' + _ctxSvg(_ctxSvgs.del) + ' Удалить</div>';
  }
  menu.innerHTML = html;
  // Position
  var x = e.clientX || (e.touches && e.touches[0] ? e.touches[0].clientX : 100);
  var y = e.clientY || (e.touches && e.touches[0] ? e.touches[0].clientY : 100);
  menu.style.left = Math.min(x, window.innerWidth - 200) + 'px';
  menu.style.top = Math.min(y, window.innerHeight - 300) + 'px';
  menu.classList.add('show');
  // Close on outside click/tap. Must IGNORE events inside the menu: otherwise a
  // touchstart on a menu button pre-closes the menu, and the following click
  // falls through to the track row beneath the button (switching tracks).
  setTimeout(function() {
    document.addEventListener('click', _ctxOutside, true);
    document.addEventListener('touchstart', _ctxOutside, true);
  }, 50);
}

function _ctxOutside(e) {
  if (e.target && e.target.closest && e.target.closest('#ctxMenu')) return;
  hideCtxMenu();
}

function hideCtxMenu() {
  document.getElementById('ctxMenu').classList.remove('show');
  _ctxIdx = -1;
  document.removeEventListener('click', _ctxOutside, true);
  document.removeEventListener('touchstart', _ctxOutside, true);
}

// Что играть следующим, помним по имени файла, а не по индексу: массив tracks
// пересобирается при перезагрузке каталога, поиске и сортировке, и сохранённый
// индекс начинал указывать на другую песню.
var _forceNextFile = null;

function forcedNextIndex() {
  if (!_forceNextFile) return -1;
  for (var i = 0; i < tracks.length; i++) {
    if (tracks[i].file === _forceNextFile) return i;
  }
  return -1;
}

function ctxPlayNext() {
  var idx = _ctxIdx;
  hideCtxMenu();
  if (idx < 0 || idx >= tracks.length) return;
  var file = tracks[idx].file;
  if (_forceNextFile === file) {          // повторный выбор снимает отметку
    _forceNextFile = null;
    showToast('Отменено');
  } else {
    _forceNextFile = file;
    // Готовим трек заранее: на iOS переключение с виджета или экрана блокировки
    // происходит в фоне, где загрузка по сети затормаживается, и неподготовленный
    // трек остаётся молчать до открытия приложения.
    prepareBlobUrl(file);
    showToast(tracks[idx].title + ' — следующий');
  }
  renderTracks();
}

function ctxAddToPlaylist(plId, where) {
  var idx = _ctxIdx;
  hideCtxMenu();
  if (idx < 0 || idx >= tracks.length) return;
  var file = tracks[idx].file;
  var pl = userPlaylists.find(function(p) { return p.id === plId; });
  if (!pl) return;
  // Check if already in playlist
  if (pl.tracks.indexOf(file) >= 0) { showToast('Уже в плейлисте'); return; }
  var newTracks = pl.tracks.slice();
  if (where === 'start') newTracks.unshift(file);
  else newTracks.push(file);
  var folder = document.getElementById('folderSelect').value;
  plWrite({folder: folder, action: 'update', id: plId, tracks: newTracks}, function(d, queued) {
    pl.tracks = newTracks;
    plMirror();
    showToast('Добавлено в «' + pl.name + '»' + (queued ? ' — уйдёт на сервер при подключении' : ''));
  });
}

function ctxDelete() {
  var idx = _ctxIdx;
  hideCtxMenu();
  if (idx < 0 || idx >= tracks.length) return;
  var t = tracks[idx];
  showConfirm('Удалить «' + t.title + '»?\nФайл будет удалён с диска.', function() {
    var folder = document.getElementById('folderSelect').value;
    fetch('/api/track/delete', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({folder: folder, file: t.file})})
    .then(function(r){return r.json()}).then(function(d) {
      if (d.ok) {
        // If playing this track, stop
        if (currentIdx === idx) { audio.pause(); setPlayState(false); }
        showToast('Удалено');
        loadFolder(folder);
      } else {
        showToast(d.error || 'Ошибка');
      }
    });
  }, 'Удалить');
}

function ctxToggleCache() {
  var idx = _ctxIdx;
  hideCtxMenu();
  if (idx < 0 || idx >= tracks.length) return;
  var file = tracks[idx].file;
  if (isTrackCached(file)) {
    uncacheTrack(file);
  } else {
    cacheTrack(file, function(ok) { if (ok) { renderTracks(); showToast('Кэшировано'); } });
  }
}

// ── Multi-select (bulk management) ──
var selectionMode = false;
var selectedFiles = {};  // file -> true
function selectedCount() { return Object.keys(selectedFiles).length; }
function getSelectedFilesInOrder() {
  var out = [];
  for (var i = 0; i < tracks.length; i++) if (selectedFiles[tracks[i].file]) out.push(tracks[i].file);
  return out;
}
function updateSelectHeader() {
  document.getElementById('playlistHeader').textContent = 'Выбрано: ' + selectedCount();
}
function ctxSelectStart() {
  var idx = _ctxIdx;
  hideCtxMenu();
  selectionMode = true;
  selectedFiles = {};
  if (idx >= 0 && idx < tracks.length) selectedFiles[tracks[idx].file] = true;
  // Hide controls that are irrelevant while selecting; show the select bar.
  ['cacheBtn','cachedOnlyBtn','shuffleListBtn','editBtn','downloadCatalogBtn'].forEach(function(id){
    var el = document.getElementById(id); if (el) el.style.display = 'none';
  });
  document.getElementById('selectControls').style.display = 'flex';
  updateSelectHeader();
  renderTracks();
}
function toggleSelect(i) {
  if (i < 0 || i >= tracks.length) return;
  var f = tracks[i].file;
  if (selectedFiles[f]) delete selectedFiles[f]; else selectedFiles[f] = true;
  updateSelectHeader();
  renderTracks();
}
function exitSelection() {
  selectionMode = false;
  selectedFiles = {};
  document.getElementById('selectControls').style.display = 'none';
  // Restore header controls to their normal visibility.
  var onTracks = activeTab === 'tracks';
  document.getElementById('shuffleListBtn').style.display = onTracks ? '' : 'none';
  document.getElementById('cacheBtn').style.display = onTracks ? '' : 'none';
  document.getElementById('cachedOnlyBtn').style.display = onTracks ? '' : 'none';
  syncDownloadBtn();
  document.getElementById('editBtn').style.display = (isNumberedCatalog && userRole !== 'demo') ? '' : 'none';
  updateTrackCounter();
  renderTracks();
}
function openBulkMenu(e) {
  if (selectedCount() === 0) { showToast('Ничего не выбрано'); return; }
  showCtxMenu(e, -1);
}

function bulkAddToPlaylist(plId, where) {
  var files = getSelectedFilesInOrder();
  hideCtxMenu();
  if (!files.length) return;
  var pl = userPlaylists.find(function(p) { return p.id === plId; });
  if (!pl) { exitSelection(); return; }
  var existing = pl.tracks.slice();
  var toAdd = files.filter(function(f) { return existing.indexOf(f) < 0; });
  if (!toAdd.length) { showToast('Уже в плейлисте'); exitSelection(); return; }
  var newTracks = where === 'start' ? toAdd.concat(existing) : existing.concat(toAdd);
  var folder = document.getElementById('folderSelect').value;
  plWrite({folder: folder, action: 'update', id: plId, tracks: newTracks}, function(d, queued) {
    pl.tracks = newTracks;
    plMirror();
    showToast('Добавлено ' + toAdd.length + ' в «' + pl.name + '»'
              + (queued ? ' — уйдёт на сервер при подключении' : ''));
  });
  exitSelection();
}

function bulkCache() {
  var files = getSelectedFilesInOrder();
  hideCtxMenu();
  if (!files.length) return;
  var todo = files.filter(function(f) { return !isTrackCached(f); });
  exitSelection();
  if (!todo.length) { showToast('Уже в кэше'); return; }
  beginCaching(todo);
}

function bulkDelete() {
  var files = getSelectedFilesInOrder();
  hideCtxMenu();
  if (!files.length) return;
  showConfirm('Удалить ' + files.length + ' трек(ов)?\nФайлы будут удалены с диска.', function() {
    var folder = document.getElementById('folderSelect').value;
    var playingFile = (currentIdx >= 0 && currentIdx < tracks.length) ? tracks[currentIdx].file : null;
    fetch('/api/track/delete_bulk', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({folder: folder, files: files})})
    .then(function(r){return r.json()}).then(function(d) {
      if (d.ok) {
        if (playingFile && files.indexOf(playingFile) >= 0) { audio.pause(); setPlayState(false); }
        showToast('Удалено: ' + (d.deleted != null ? d.deleted : files.length));
        exitSelection();
        loadFolder(folder);
      } else { showToast(d.error || 'Ошибка'); }
    });
  }, 'Удалить');
}

function bulkMeta() {
  var files = getSelectedFilesInOrder();
  hideCtxMenu();
  if (!files.length) return;
  var folder = document.getElementById('folderSelect').value;
  fetch('/api/meta/bulk', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder: folder, files: files})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.already_running) { showToast('Поиск мета-данных уже идёт'); return; }
    if (!d.ok) { showToast(d.error || 'Ошибка'); return; }
    showToast('Запрашиваю мета-данные для ' + files.length + ' трек(ов)...');
    exitSelection();
    pollBulkMeta(folder);
  });
}
function pollBulkMeta(folder) {
  fetch('/api/meta/status').then(function(r){return r.json()}).then(function(d) {
    if (d.running) { setTimeout(function(){ pollBulkMeta(folder); }, 1000); }
    else { showToast('Мета-данные обновлены'); loadFolder(folder); }
  }).catch(function(){});
}

function deleteEditTrack() {
  if (editingTrackIdx < 0 || editingTrackIdx >= tracks.length) return;
  var t = tracks[editingTrackIdx];
  var idx = editingTrackIdx;
  showConfirm('Удалить «' + t.title + '»?\nФайл будет удалён с диска.', function() {
    document.getElementById('trackEditOverlay').classList.remove('show');
    var folder = document.getElementById('folderSelect').value;
    fetch('/api/track/delete', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({folder: folder, file: t.file})})
    .then(function(r){return r.json()}).then(function(d) {
      if (d.ok) {
        if (currentIdx === idx) { audio.pause(); setPlayState(false); }
        showToast('Удалено');
        loadFolder(folder);
      } else { showToast(d.error || 'Ошибка'); }
    });
  }, 'Удалить');
}

// ── Playlist context menu ──
var _plCtxId = null;
function showPlCtxMenu(e, plId) {
  e.preventDefault();
  e.stopPropagation();
  _plCtxId = plId;
  var pl = userPlaylists.find(function(p){return p.id===plId});
  if (!pl) return;
  var menu = document.getElementById('plCtxMenu');
  var x = e.clientX || (e.touches && e.touches[0] ? e.touches[0].clientX : 100);
  var y = e.clientY || (e.touches && e.touches[0] ? e.touches[0].clientY : 100);
  menu.style.left = Math.min(x, window.innerWidth - 200) + 'px';
  menu.style.top = Math.min(y, window.innerHeight - 200) + 'px';
  menu.classList.add('show');
  setTimeout(function() {
    document.addEventListener('click', _plCtxOutside, true);
    document.addEventListener('touchstart', _plCtxOutside, true);
  }, 50);
}
function _plCtxOutside(e) {
  if (e.target && e.target.closest && e.target.closest('#plCtxMenu')) return;
  hidePlCtxMenu();
}
function hidePlCtxMenu() {
  document.getElementById('plCtxMenu').classList.remove('show');
  document.removeEventListener('click', _plCtxOutside, true);
  document.removeEventListener('touchstart', _plCtxOutside, true);
}
function plCtxDelete() {
  var id = _plCtxId;
  hidePlCtxMenu();
  if (!id) return;
  var pl = userPlaylists.find(function(p){return p.id===id});
  var name = pl ? pl.name : '';
  showConfirm('Удалить плейлист «' + name + '»?', function() {
    var folder = document.getElementById('folderSelect').value;
    fetch('/api/playlists', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({folder: folder, action: 'delete', id: id})})
    .then(function(r){return r.json()}).then(function(d) {
      if (!d || d.error || !d.ok) { showToast('Удаление доступно только при связи с сервером'); return; }
      showToast('Плейлист удалён');
      loadUserPlaylists();
    }).catch(function(){ showToast('Удаление доступно только при связи с сервером'); });
  }, 'Удалить');
}

// Long-press for mobile context menu (tracks + playlists)
(function() {
  document.addEventListener('touchstart', function(e) {
    // Track long-press
    var el = e.target.closest('[data-longpress]');
    if (el) {
      var idx = parseInt(el.getAttribute('data-longpress'));
      _ctxLongTimer = setTimeout(function() {
        _ctxLongTimer = null;
        showCtxMenu(e, idx);
      }, 500);
      return;
    }
    // Playlist long-press
    var plEl = e.target.closest('[data-longpress-pl]');
    if (plEl) {
      var plId = plEl.getAttribute('data-longpress-pl');
      _ctxLongTimer = setTimeout(function() {
        _ctxLongTimer = null;
        showPlCtxMenu(e, plId);
      }, 500);
    }
  }, {passive: true});
  document.addEventListener('touchmove', function() {
    if (_ctxLongTimer) { clearTimeout(_ctxLongTimer); _ctxLongTimer = null; }
  });
  document.addEventListener('touchend', function() {
    if (_ctxLongTimer) { clearTimeout(_ctxLongTimer); _ctxLongTimer = null; }
  });
})();

var _tiCoverData = null;      // выбранная обложка, до сохранения только здесь

function tiSet(id, val) {
  var el = document.getElementById(id);
  if (el) el.textContent = (val === '' || val === null || val === undefined) ? '—' : String(val);
}

function tiSetEdit(on) {
  document.getElementById('tiModal').classList.toggle('editing', !!on);
  if (on) document.getElementById('trackEditTitle').focus();
  else openTrackEdit(editingTrackIdx);    // «Отмена» — вернуть исходные значения
}

function tiPickCover() { document.getElementById('tiCoverInput').click(); }

function tiCoverChosen(input) {
  var f = input.files && input.files[0];
  input.value = '';
  if (!f) return;
  if (f.size > 4 * 1024 * 1024) { showToast('Обложка больше 4 МБ'); return; }
  var rd = new FileReader();
  rd.onload = function() {
    _tiCoverData = rd.result;             // data:image/...;base64,...
    var img = document.getElementById('tiCover');
    img.src = _tiCoverData;
    img.style.display = '';
    document.getElementById('tiCoverPh').style.display = 'none';
  };
  rd.readAsDataURL(f);
}

// Окно сведений о треке. Правка включается кнопкой: смотреть теги нужно часто,
// менять — редко, и случайная правка на ощупь никому не нужна.
function openTrackEdit(idx) {
  if (idx < 0 || idx >= tracks.length) return;
  editingTrackIdx = idx;
  _tiCoverData = null;
  var t = tracks[idx];

  document.getElementById('tiModal').classList.remove('editing');
  document.getElementById('trackEditFile').textContent = t.file;
  tiSet('tiHeadTitle', t.title || t.file);
  tiSet('tiHeadArtist', t.artist || '');

  var img = document.getElementById('tiCover'), ph = document.getElementById('tiCoverPh');
  img.style.display = 'none'; ph.style.display = '';
  img.onerror = function(){ img.style.display = 'none'; ph.style.display = ''; };
  img.onload = function(){ img.style.display = ''; ph.style.display = 'none'; };
  setCoverSrc(img, t.file, t.has_cover, ph);

  // Значения для просмотра и они же в поля правки.
  var pairs = [['tiValTitle', 'trackEditTitle', t.title || ''],
               ['tiValArtist', 'trackEditArtist', t.artist || ''],
               ['tiValAlbum', 'tiAlbum', t.album || ''],
               ['tiValAart', 'tiAart', t.aart || ''],
               ['tiValYear', 'tiYear', t.yr || ''],
               ['tiValGenre', 'tiGenre', t.gen || ''],
               ['tiValTrk', 'tiTrk', t.trk || '']];
  for (var i = 0; i < pairs.length; i++) {
    tiSet(pairs[i][0], pairs[i][2]);
    var inp = document.getElementById(pairs[i][1]);
    if (inp) inp.value = pairs[i][2] === '' ? '' : pairs[i][2];
  }

  var m = t.file.match(/^(\d+)\.\s/);
  var hint = document.getElementById('trackEditOrderHint');
  document.getElementById('trackEditOrder').value = m ? parseInt(m[1]) : '';
  tiSet('tiValOrder', m ? parseInt(m[1]) : '');
  hint.textContent = m ? '' : '(нет номера)';

  tiSet('tiValDur', t.dur ? formatTime(t.dur) : '');
  var q = [];
  if (t.fmt) q.push(t.fmt);
  if (t.br) q.push(t.br + ' кбит/с');
  tiSet('tiValQuality', q.join(' · '));

  document.getElementById('trackEditMeta').checked = false;
  document.getElementById('trackEditCacheRow').style.display = isTrackCached(t.file) ? 'flex' : 'none';
  document.getElementById('trackEditOverlay').classList.add('show');
}

function uncacheEditTrack() {
  if (editingTrackIdx < 0 || editingTrackIdx >= tracks.length) return;
  var key = cacheKey(tracks[editingTrackIdx].file);
  var audioKey = _audioKeyIndex[key] || key;
  var coverKey = _coverKeyIndex[key] || ('cover:' + key);
  // Remove audio and cover from cache
  openCacheDB(function(db) {
    var tx = db.transaction('audio', 'readwrite');
    var store = tx.objectStore('audio');
    store.delete(audioKey);
    store.delete(coverKey);
    tx.oncomplete = function() {
      delete cachedFiles[key]; delete _audioKeyIndex[key]; delete _coverKeyIndex[key];
      document.getElementById('trackEditCacheRow').style.display = 'none';
      renderTracks();
      renderAlbums();
      showToast('Удалено из кэша');
    };
  });
}

function saveTrackEdit() {
  if (editingTrackIdx < 0) return;
  var t = tracks[editingTrackIdx];
  var folder = document.getElementById('folderSelect').value;
  var title = document.getElementById('trackEditTitle').value.trim();
  var artist = document.getElementById('trackEditArtist').value.trim();
  var orderEl = document.getElementById('trackEditOrder');
  var order = orderEl.offsetParent ? parseInt(orderEl.value) || 0 : 0;
  var runMeta = document.getElementById('trackEditMeta').checked;
  if (!title) { showToast('Введите название'); return; }

  // If this track is playing, pause and remember position
  var wasPlaying = isPlaying && currentIdx === editingTrackIdx;
  var playPos = wasPlaying ? audio.currentTime : 0;
  if (wasPlaying) { audio.pause(); setPlayState(false); }

  fetch('/api/track/edit', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder: folder, file: t.file, title: title, artist: artist,
      order: order, run_meta: runMeta,
      // Пустое поле означает «не трогать тег», а не «стереть»: стереть его
      // случайно в окне правки слишком легко, а вернуть нечем.
      album: document.getElementById('tiAlbum').value.trim(),
      albumartist: document.getElementById('tiAart').value.trim(),
      genre: document.getElementById('tiGenre').value.trim(),
      year: document.getElementById('tiYear').value,
      track_no: document.getElementById('tiTrk').value,
      cover: _tiCoverData || ''})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok) {
      showToast(runMeta ? 'Трек обновлён, ищу мета-данные...' : 'Трек обновлён');
      document.getElementById('trackEditOverlay').classList.remove('show');
      // Metadata refresh runs in the background on the server — reload again
      // shortly so the freshly written album/cover show up.
      if (runMeta) setTimeout(function(){ loadFolder(document.getElementById('folderSelect').value); }, 5000);
      var wasCurrentTrack = currentIdx === editingTrackIdx;
      // Update player UI immediately if this is the current track
      if (wasCurrentTrack) {
        document.getElementById('trackTitle').textContent = title;
        document.getElementById('trackArtist').textContent = artist;
        if ('mediaSession' in navigator) {
          navigator.mediaSession.metadata = new MediaMetadata({
            title: title, artist: artist,
            album: navigator.mediaSession.metadata ? navigator.mediaSession.metadata.album : ''
          });
        }
      }
      // Reload folder to refresh track list
      loadFolder(folder);
      // Resume playback with new filename
      if (wasPlaying && d.new_file) {
        setTimeout(function() {
          for (var i = 0; i < tracks.length; i++) {
            if (tracks[i].file === d.new_file) {
              currentIdx = i;
              setAudioSrc('/api/stream/' + encodeURIComponent(d.new_file));
              audio.currentTime = playPos;
              ourAudioPlay();
              setPlayState(true);
              break;
            }
          }
        }, 1000);
      }
    } else {
      showToast(d.error || 'Ошибка');
    }
  });
}

function showAppInfo() {
  var el = document.getElementById('appInfoContent');
  el.innerHTML = '<div style="font-size:20px;font-weight:700;color:#e94560;margin-bottom:8px">' + _n + '</div>'
    + '<div style="font-size:13px;color:rgba(255,255,255,0.5);margin-bottom:12px">' + _p + '</div>'
    + '<div style="font-size:12px;color:rgba(255,255,255,0.35);line-height:1.5">' + _l + '</div>';
  document.getElementById('appInfoOverlay').classList.add('show');
}

function scrollTracklistTop() {
  ['trackList', 'albumList', 'playlistsList', 'newList'].forEach(function(id) {
    var el = document.getElementById(id);
    if (!el || !el.scrollTop) return;
    var start = el.scrollTop;
    el.scrollTo({top: 0, behavior: 'smooth'});
    // Chrome silently drops a programmatic smooth scroll on these long, lazily
    // populated lists (scroll anchoring wins), so jump outright if the animation
    // never started. Safari animates fine and never reaches the fallback.
    setTimeout(function() {
      if (el.scrollTop > 0 && el.scrollTop >= start) el.scrollTo({top: 0, behavior: 'instant'});
    }, 250);
  });
}

// ── Tooltips (JS, position:fixed) ──
(function() {
  var tip = document.getElementById('tipPopup');
  document.addEventListener('mouseover', function(e) {
    var el = e.target.closest('[data-tip]');
    if (!el) { tip.classList.remove('show'); return; }
    tip.textContent = el.getAttribute('data-tip');
    tip.classList.add('show');
    var r = el.getBoundingClientRect();
    var tw = tip.offsetWidth;
    var th = tip.offsetHeight;
    var left = r.left + r.width / 2 - tw / 2;
    if (left < 4) left = 4;
    if (left + tw > window.innerWidth - 4) left = window.innerWidth - tw - 4;
    tip.style.left = left + 'px';
    // Show above if near bottom of screen
    if (r.bottom + th + 10 > window.innerHeight) {
      tip.style.top = (r.top - th - 6) + 'px';
    } else {
      tip.style.top = (r.bottom + 6) + 'px';
    }
  });
  document.addEventListener('mouseout', function(e) {
    if (e.target.closest('[data-tip]')) tip.classList.remove('show');
  });
})();

// Init background, media session, and load config
// ── Offline Cache via IndexedDB (works on HTTP, LAN, WAN) ──
var cachedFiles = {};
var cacheQueue = [];
var cachingActive = false;
var cacheTotalCount = 0;
var showCachedOnly = false;
var _cacheDB = null;
var _isIOSDevice = /iPad|iPhone|iPod/.test(navigator.userAgent) || (/Mac/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);

function openCacheDB(cb) {
  if (_cacheDB) { cb(_cacheDB); return; }
  var req = indexedDB.open('vinylCache', 1);
  req.onupgradeneeded = function(e) {
    var db = e.target.result;
    if (!db.objectStoreNames.contains('audio')) db.createObjectStore('audio');
  };
  req.onsuccess = function(e) { _cacheDB = e.target.result; cb(_cacheDB); };
  req.onerror = function() { showToast('Не удалось открыть кэш'); };
}

// Tracks live on disk as "NN. Artist - Title.ext". Adding/removing tracks
// renumbers every file (and can change the padding width), so the "NN. " prefix
// is unstable. Key the offline cache by the number-stripped name instead, so a
// cached track keeps matching after a sync even though its filename changed.
function cacheKey(file) {
  if (!file) return file;
  return file.replace(/^\d+\.\s+/, '');
}

// Blobs are NOT moved to migrate old (full-filename) keys — copying ~1 GB of
// audio on iOS crashes the PWA. Instead we build a lightweight index from the
// key list alone (getAllKeys returns only strings, never the blob values), so a
// blob cached under an old numbered name is still found by its stable name.
//   cacheKey(storedKey) -> storedKey
var _audioKeyIndex = {}; // stable name -> actual audio key in IDB
var _coverKeyIndex = {}; // stable name -> actual cover key in IDB

function refreshCachedList() {
  openCacheDB(function(db) {
    var tx = db.transaction('audio', 'readonly');
    var store = tx.objectStore('audio');
    var req = store.getAllKeys();
    req.onsuccess = function() {
      cachedFiles = {};
      _audioKeyIndex = {};
      _coverKeyIndex = {};
      var keys = req.result || [];
      for (var i = 0; i < keys.length; i++) {
        var k = keys[i];
        if (k.indexOf('cover:') === 0) {
          _coverKeyIndex[cacheKey(k.slice(6))] = k;
        } else {
          var ak = cacheKey(k);
          cachedFiles[ak] = true;
          _audioKeyIndex[ak] = k;
        }
      }
      if (typeof renderTracks === 'function') renderTracks();
      prepareNearbyBlobs();
      backfillMissingCovers();
    };
  });
}

function isTrackCached(file) { return !!cachedFiles[cacheKey(file)]; }

// onDone(ok, reason): 'ok' | 'http' (server replied, file missing/forbidden)
// | 'net' (fetch rejected — connection or TLS died) | 'db' (IndexedDB full).
// The queue needs that distinction: one bad file should be skipped, a dead
// connection must stop the run instead of racing through it.
function cacheTrack(file, onDone) {
  var url = '/api/stream/' + encodeURIComponent(file);
  var key = cacheKey(file);
  fetch(url).then(function(r) {
    if (!r.ok) { var err = new Error('http ' + r.status); err.kind = 'http'; throw err; }
    var ct = r.headers.get('content-type') || '';
    if (ct.indexOf('application/json') === 0) {
      // 200 with a JSON body means an error envelope, not a track.
      var e2 = new Error('not audio'); e2.kind = 'http'; throw e2;
    }
    return r.arrayBuffer();
  }).then(function(buf) {
    if (!buf || buf.byteLength < 2048) { var e3 = new Error('too small'); e3.kind = 'http'; throw e3; }
    openCacheDB(function(db) {
      var tx = db.transaction('audio', 'readwrite');
      tx.objectStore('audio').put(buf, key);
      tx.oncomplete = function() {
        cachedFiles[key] = true;
        _audioKeyIndex[key] = key;
        // Also cache cover art if available
        cacheCover(file);
        if (onDone) onDone(true, 'ok');
      };
      tx.onerror = function() { if (onDone) onDone(false, 'db'); };
    });
  }).catch(function(e) { if (onDone) onDone(false, (e && e.kind) || 'net'); });
}

// A response can be HTTP 200 and still not be the file we asked for: when the
// session has expired the server used to answer /api/cover/ and /api/stream/
// with {"error":"unauthorized"} — 25 bytes of JSON, status 200. That got stored
// as the artwork (or as the track), and since it was "cached" nothing ever
// refetched it: the placeholder stayed forever, online and offline alike.
// Check what we actually received, both on the way in and on the way out — the
// server is fixed now, but caches poisoned by older builds must heal themselves.
function looksLikeImage(buf) {
  if (!buf || buf.byteLength < 12) return false;
  var b = new Uint8Array(buf, 0, 12);
  if (b[0] === 0xFF && b[1] === 0xD8 && b[2] === 0xFF) return true;                 // JPEG
  if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4E && b[3] === 0x47) return true; // PNG
  if (b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46) return true;                 // GIF
  if (b[0] === 0x42 && b[1] === 0x4D) return true;                                  // BMP
  if (b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 &&
      b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50) return true; // WEBP
  return false;
}

function dropCachedEntry(key, indexObj, indexKey) {
  openCacheDB(function(db) {
    try {
      var tx = db.transaction('audio', 'readwrite');
      tx.objectStore('audio').delete(key);
    } catch (e) {}
  });
  if (indexObj) delete indexObj[indexKey];
}

function cacheCover(file) {
  var url = '/api/cover/' + encodeURIComponent(file);
  fetch(url).then(function(r) {
    if (!r.ok) return null;
    var ct = r.headers.get('content-type') || '';
    if (ct.indexOf('image/') !== 0) return null;   // an error body, not artwork
    return r.arrayBuffer();
  }).then(function(buf) {
    if (!looksLikeImage(buf)) return;
    openCacheDB(function(db) {
      var tx = db.transaction('audio', 'readwrite');
      var ck = 'cover:' + cacheKey(file);
      tx.objectStore('audio').put(buf, ck);
      tx.oncomplete = function() { _coverKeyIndex[cacheKey(file)] = ck; };
    });
  }).catch(function() {});
}

function getCachedCover(file, cb) {
  // Prefer the actual stored key (may be a legacy numbered name), fall back to
  // the stable key for freshly-cached covers not yet in the index.
  var storedKey = _coverKeyIndex[cacheKey(file)] || ('cover:' + cacheKey(file));
  openCacheDB(function(db) {
    var tx = db.transaction('audio', 'readonly');
    var req = tx.objectStore('audio').get(storedKey);
    req.onsuccess = function() {
      var buf = req.result || null;
      if (buf && !looksLikeImage(buf)) {
        // Poisoned by an expired session — drop it so the usual network path
        // refills the artwork instead of showing a placeholder forever.
        dropCachedEntry(storedKey, _coverKeyIndex, cacheKey(file));
        buf = null;
      }
      cb(buf);
    };
    req.onerror = function() { cb(null); };
  });
}

// Fallback for broken cover images: try IndexedDB cache, else hide
function loadCachedImg(img, encodedFile) {
  var file = decodeURIComponent(encodedFile);
  img.onerror = null; // prevent loop
  getCachedCover(file, function(buf) {
    if (buf) {
      var blob = new Blob([buf]);
      img.src = URL.createObjectURL(blob);
    } else {
      img.style.visibility = 'hidden';
      if (!_isOffline) cacheCover(file); // backfill for next time
    }
  });
}

// Universal cover setter — prefers cached blob URL when track is cached
function setCoverSrc(img, file, hasCover, placeholderEl) {
  function showPh() {
    img.style.display = 'none';
    if (placeholderEl) placeholderEl.style.display = '';
  }
  function showImg() {
    img.style.display = '';
    if (placeholderEl) placeholderEl.style.display = 'none';
  }
  if (!hasCover) { showPh(); return; }
  if (isTrackCached(file)) {
    getCachedCover(file, function(buf) {
      if (buf) {
        if (img._coverUrl) URL.revokeObjectURL(img._coverUrl);
        img._coverUrl = URL.createObjectURL(new Blob([buf]));
        img.src = img._coverUrl;
        showImg();
      } else if (!_isOffline) {
        img.src = '/api/cover/' + encodeURIComponent(file);
        showImg();
        cacheCover(file); // backfill
      } else {
        showPh();
      }
    });
  } else if (!_isOffline) {
    img.src = '/api/cover/' + encodeURIComponent(file);
    showImg();
  } else {
    showPh();
  }
}

// Backfill cover IDB entries for tracks cached before cacheCover was added.
// Iterate the loaded tracks (not the cache keys): covers are fetched from the
// server by the real on-disk filename, while the cache itself is keyed by the
// number-stripped name.
function backfillMissingCovers() {
  if (_isOffline || typeof tracks === 'undefined' || !tracks.length) return;
  var list = tracks.filter(function(t) { return t.has_cover && isTrackCached(t.file); });
  var i = 0;
  function step() {
    if (i >= list.length) return;
    var t = list[i++];
    getCachedCover(t.file, function(buf) {
      if (!buf) cacheCover(t.file);
      setTimeout(step, 150); // avoid hammering network
    });
  }
  step();
}

function uncacheTrack(file) {
  var key = cacheKey(file);
  var audioKey = _audioKeyIndex[key] || key;
  var coverKey = _coverKeyIndex[key] || ('cover:' + key);
  openCacheDB(function(db) {
    var tx = db.transaction('audio', 'readwrite');
    var store = tx.objectStore('audio');
    store.delete(audioKey);
    store.delete(coverKey);
    tx.oncomplete = function() {
      delete cachedFiles[key]; delete _audioKeyIndex[key]; delete _coverKeyIndex[key];
      renderTracks(); renderAlbums(); showToast('Удалено из кэша');
    };
  });
}

function getCachedAudio(file, cb) {
  // Resolve to the actual stored key (a legacy numbered name for blobs cached
  // before this build), falling back to the stable key.
  var storedKey = _audioKeyIndex[cacheKey(file)] || cacheKey(file);
  openCacheDB(function(db) {
    var tx = db.transaction('audio', 'readonly');
    var req = tx.objectStore('audio').get(storedKey);
    req.onsuccess = function() {
      var buf = req.result || null;
      // Same 200-with-JSON poisoning as covers: no real track is 2 KB, and a
      // stored error body would just play silence. Returning null makes the
      // caller un-mark the track and fall back to streaming.
      if (buf && buf.byteLength < 2048) {
        dropCachedEntry(storedKey, _audioKeyIndex, cacheKey(file));
        delete cachedFiles[cacheKey(file)];
        buf = null;
      }
      cb(buf);
    };
    req.onerror = function() { cb(null); };
  });
}

function startCacheAll() {
  if (cachingActive) { stopCacheAll(); return; }
  var files = [];
  for (var i = 0; i < tracks.length; i++) {
    if (!isTrackCached(tracks[i].file)) files.push(tracks[i].file);
  }
  if (!files.length) { showToast('Все треки уже в кэше'); return; }
  if (_isIOSDevice && files.length > 100) {
    showConfirm('На iOS кэш ограничен ~1 ГБ и может быть очищен через 7 дней. Загрузить ' + files.length + ' треков?', function() {
      beginCaching(files);
    }, 'Загрузить');
    return;
  }
  beginCaching(files);
}

// The queue survives a reload: a long run over LAN/HTTPS gets killed whenever
// the server restarts or the phone stops trusting the certificate, and the only
// cure is reloading the page so Safari re-prompts for the cert. Persisting the
// remaining files means that reload costs nothing.
var CACHE_QUEUE_KEY = '_vc_cachequeue';
var _cacheFails = 0;        // consecutive failures that looked like connection loss
var _cacheSkipped = 0;      // files the server itself refused — skipped, not retried
var _cacheResumed = false;

function saveCacheQueue() {
  try {
    if (cachingActive && cacheQueue.length) {
      localStorage.setItem(CACHE_QUEUE_KEY, JSON.stringify({
        queue: cacheQueue, total: cacheTotalCount,
        folder: document.getElementById('folderSelect').value
      }));
    } else {
      localStorage.removeItem(CACHE_QUEUE_KEY);
    }
  } catch (e) {}
}

function beginCaching(files) {
  cacheQueue = files.slice();
  cacheTotalCount = files.length;
  cachingActive = true;
  _cacheFails = 0;
  _cacheSkipped = 0;
  showToast('Кэширование: 0/' + cacheTotalCount);
  updateCacheBtn();
  saveCacheQueue();
  cacheNextInQueue();
}

function cacheNextInQueue() {
  if (!cachingActive || !cacheQueue.length) {
    var was = cachingActive;
    cachingActive = false;
    updateCacheBtn();
    saveCacheQueue();
    if (was) {
      showToast(_cacheSkipped ? ('Кэширование завершено, пропущено: ' + _cacheSkipped) : 'Кэширование завершено');
      refreshCachedList();
    }
    return;
  }
  var done = cacheTotalCount - cacheQueue.length;
  showToast('Кэширование: ' + done + '/' + cacheTotalCount);
  var file = cacheQueue[0];
  updateCacheBtn();
  cacheTrack(file, function(ok, reason) {
    if (ok) {
      cacheQueue.shift();
      _cacheFails = 0;
      saveCacheQueue();
      cacheNextInQueue();
      return;
    }
    if (reason === 'http') {
      // The server answered and said no — the file is gone or forbidden.
      // Skipping keeps a single bad track from stalling the whole run.
      cacheQueue.shift();
      _cacheSkipped++;
      _cacheFails = 0;
      saveCacheQueue();
      cacheNextInQueue();
      return;
    }
    if (reason === 'db') {
      cachingActive = false;
      updateCacheBtn();
      saveCacheQueue();
      showToast('Хранилище устройства переполнено, кэширование остановлено');
      refreshCachedList();
      return;
    }
    // 'net' — the connection died. Leave the file at the head of the queue and
    // back off; without this the loop used to fail instantly on every remaining
    // track, so the counter raced to the end having downloaded nothing.
    _cacheFails++;
    if (_cacheFails < 4) { setTimeout(cacheNextInQueue, 1500 * _cacheFails); return; }
    pauseCachingOnError();
  });
}

function pauseCachingOnError() {
  cachingActive = false;
  _cacheFails = 0;
  updateCacheBtn();
  var left = cacheQueue.length;
  // Keep the queue on disk even though caching is no longer active.
  try {
    localStorage.setItem(CACHE_QUEUE_KEY, JSON.stringify({
      queue: cacheQueue, total: cacheTotalCount,
      folder: document.getElementById('folderSelect').value
    }));
  } catch (e) {}
  refreshCachedList();
  showConfirm('Соединение с сервером потеряно, осталось ' + left + ' треков.\n\nЧаще всего это HTTPS-сертификат, который браузер перестал принимать. Перезагрузить приложение? Останется подтвердить доверие к сертификату — загрузка продолжится сама.',
    function() { window.location.reload(); }, 'Перезагрузить');
}

// Called once after a successful config load: if a run was interrupted, pick it
// up where it stopped.
function resumeCacheQueue() {
  if (_cacheResumed || cachingActive || _isOffline) return;
  var st = null;
  try { st = JSON.parse(localStorage.getItem(CACHE_QUEUE_KEY) || 'null'); } catch (e) {}
  if (!st || !st.queue || !st.queue.length) return;
  var folder = document.getElementById('folderSelect').value;
  if (st.folder && folder && st.folder !== folder) return;
  _cacheResumed = true;
  cacheQueue = st.queue.filter(function(f) { return !isTrackCached(f); });
  if (!cacheQueue.length) { try { localStorage.removeItem(CACHE_QUEUE_KEY); } catch (e) {} return; }
  cacheTotalCount = st.total || cacheQueue.length;
  cachingActive = true;
  _cacheFails = 0;
  _cacheSkipped = 0;
  showToast('Продолжаю кэширование: осталось ' + cacheQueue.length);
  updateCacheBtn();
  cacheNextInQueue();
}

function stopCacheAll() {
  cacheQueue = [];
  cachingActive = false;
  updateCacheBtn();
  saveCacheQueue();
  showToast('Кэширование остановлено');
  refreshCachedList();
}

function updateCacheBtn() {
  var btn = document.getElementById('cacheBtn');
  if (btn) btn.classList.toggle('active', cachingActive);
}

function cachePlaylist(plId) {
  var pl = findPlaylist(plId);
  if (!pl) return;
  var files = pl.tracks.filter(function(f) { return !isTrackCached(f); });
  if (!files.length) { showToast('Плейлист уже в кэше'); return; }
  beginCaching(files);
}

function toggleCachedOnly() {
  showCachedOnly = !showCachedOnly;
  var btn = document.getElementById('cachedOnlyBtn');
  if (btn) btn.classList.toggle('active', showCachedOnly);
  renderTracks();
  updateTrackCounter();
}

function updateTrackCounter() {
  if (activeTab !== 'tracks') return;
  var vis = getVisibleIndices();
  if (showCachedOnly) {
    document.getElementById('playlistHeader').textContent = vis.length + ' из ' + tracks.length + ' треков';
  } else {
    document.getElementById('playlistHeader').textContent = tracks.length + ' треков';
  }
}

function clearAllCache() {
  var count = Object.keys(cachedFiles).length;
  if (!count) { showToast('Кэш пуст'); return; }
  showConfirm('Удалить все закэшированные треки (' + count + ')?', function() {
    openCacheDB(function(db) {
      var tx = db.transaction('audio', 'readwrite');
      tx.objectStore('audio').clear();
      tx.oncomplete = function() {
        cachedFiles = {};
        renderTracks();
        renderAlbums();
        document.getElementById('profileCacheInfo').textContent = 'Кэш пуст';
        showToast('Кэш очищен');
      };
    });
  }, 'Удалить');
}

// Init cache on load
openCacheDB(function() { refreshCachedList(); });

// Register Service Worker for offline app shell (HTTPS or localhost only)
if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1')) {
  navigator.serviceWorker.register('/sw.js').then(function(reg) {
    if (reg.active) {
      warmAppCache();
    } else {
      navigator.serviceWorker.ready.then(function() { warmAppCache(); });
    }
    navigator.serviceWorker.addEventListener('message', function(e) {
      if (e.data && e.data.action === 'reload') {
        window.location.reload();
      }
    });
    reg.addEventListener('updatefound', function() {
    });
  }).catch(function(e) { });
} else {
}
function warmAppCache() {
  if (!('caches' in window)) return;
  caches.open('app-APP_BUILD_HASH').then(function(cache) {
    cache.match('/').then(function(r) {
      if (!r) {
        // No cached page yet — fetch with credentials and store
        fetch('/', {credentials:'same-origin'}).then(function(resp) {
          if (resp.ok) cache.put('/', resp);
        });
      }
    });
  });
}

// ── Auto-update check ──
(function() {
  var curVersion = 'APP_BUILD_HASH';
  function checkUpdate() {
    fetch('/api/version', {cache: 'no-store'}).then(function(r){return r.json()}).then(function(d) {
      // The SW turns a failed /api/* call into {error:'offline'}, so a dead
      // server never rejects here — watch for that flag explicitly, otherwise a
      // PWA pointing at a stale LAN IP polls forever without noticing.
      if (d && d.error) { retryConfigThenGoOffline(); return; }
      if (_isOffline) {
        // Server is reachable again — rebuild state from the live config.
        _isOffline = false;
        showOfflineBanner(false);
        loadConfig();
        showToast('Подключение восстановлено');
        return;
      }
      if (d.version && d.version !== curVersion) {
        // Server has newer version — clear only SW app caches (NOT IndexedDB audio)
        showToast('Обновление приложения...');
        if ('caches' in window) {
          caches.keys().then(function(names) {
            // Only delete app-* caches, preserve everything else
            return Promise.all(names.filter(function(n){ return n.startsWith('app-'); }).map(function(n){return caches.delete(n)}));
          }).then(function() {
            if (navigator.serviceWorker) {
              navigator.serviceWorker.getRegistrations().then(function(regs) {
                regs.forEach(function(r){r.unregister()});
                setTimeout(function(){ window.location.reload(); }, 500);
              });
            } else { window.location.reload(); }
          });
        } else { window.location.reload(); }
      }
    }).catch(function(){});
  }
  // Check on load (after 5s) and every 60s
  setTimeout(checkUpdate, 5000);
  setInterval(function(){ if (_uiActive) checkUpdate(); }, 60000);
})();

perfLoad();
syncUiActive();
initBgCanvas();
// Hide volume slider on iOS (audio.volume is read-only)
if(_isIOS){var vw=document.querySelector('.volume-wrap input[type=range]');if(vw)vw.style.display='none';var vs=document.querySelector('.volume-wrap span');if(vs)vs.style.display='none';}
initMediaSession();
initMediaLogging();
initPlaybackContext();
initWidgetBridge();

// Detect online/offline transitions
window.addEventListener('online', function() {
  flushPlays();
  flushEras();
  flushPlaylists();
  if (_isOffline) {
    _isOffline = false;
    showOfflineBanner(false);
    syncNewTabVisibility();
    showCachedOnly = false;
    var btn = document.getElementById('cachedOnlyBtn');
    if (btn) btn.classList.remove('active');
    loadConfig();
    backfillMissingCovers();
    showToast('Подключение восстановлено');
  }
});
window.addEventListener('offline', function() {
  if (!_isOffline) enterOfflineMode();
});

loadConfig();
</script>
</body>
</html>"""


# ──────────────────── HTTP Server ────────────────────

LOGIN_PAGE = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="apple-touch-icon" sizes="180x180" href="/icon.png">
<link rel="icon" type="image/png" sizes="180x180" href="/icon.png">
<title></title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#111;color:#eee;display:flex;align-items:flex-start;justify-content:center;min-height:100vh;padding-top:15vh}
.login-card{background:#1c1c1c;border-radius:16px;padding:32px;width:340px;box-shadow:0 20px 60px rgba(0,0,0,0.5)}
.login-card h2{color:#e94560;margin-bottom:20px;text-align:center}
.login-card label{display:block;font-size:12px;color:rgba(255,255,255,0.5);margin-bottom:4px;margin-top:12px}
.login-card input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:rgba(255,255,255,0.06);color:#eee;font-size:14px;outline:none}
.login-card input:focus{border-color:#e94560}
.login-card button{width:100%;padding:12px;border-radius:8px;border:none;background:#e94560;color:#fff;font-size:14px;font-weight:600;cursor:pointer;margin-top:16px}
.login-card button:hover{background:#d13a54}
.login-card .error{color:#e94560;font-size:12px;margin-top:8px;text-align:center;min-height:16px}
.login-card .subtitle{font-size:12px;color:rgba(255,255,255,0.4);text-align:center;margin-bottom:4px}
</style></head><body>
<div class="login-card">
<h2 id="loginTitle"></h2>
<div class="subtitle" id="subtitle">Вход</div>
<form onsubmit="return doLogin()" id="loginForm">
<label>Логин</label><input type="text" id="lu" autocomplete="username" required>
<label>Пароль</label><input type="password" id="lp" autocomplete="current-password" required>
<div id="confirmPwField" style="display:none"><label>Подтвердите пароль</label><input type="password" id="lp2" autocomplete="new-password" disabled></div>
<div id="musicRootField" style="display:none">
<label>Корневая папка музыки</label><input type="text" id="mr" placeholder="~/VinylMusic">
<div style="font-size:10px;color:rgba(255,255,255,0.3);margin-top:2px">Папка для хранения музыки всех пользователей. Для каждого пользователя будет создана подпапка.</div>
</div>
<button type="submit" id="lbtn">Войти</button>
<div class="error" id="lerr"></div>
</form>
</div>
<script>
function _d(s){return decodeURIComponent(escape(atob(s.split('').reverse().join(''))));}
var _ln=_d("=QL0+CdhRLJ0gQJgiDyYpNXdtBSZkl2clRWaz5Wa");
var _n2=_d("==wYpNXdtBSZkl2clRWaz5Wa");
document.title=_ln;
document.getElementById('loginTitle').textContent=_n2;
fetch('/api/auth/check').then(function(r){return r.json()}).then(function(d){
  if(d.needs_setup){
    document.getElementById('subtitle').textContent='Создайте аккаунт администратора';
    document.getElementById('lbtn').textContent='Создать';
    document.getElementById('confirmPwField').style.display='';
    document.getElementById('lp2').disabled=false;
    document.getElementById('lp2').required=true;
    document.getElementById('musicRootField').style.display='';
    document.getElementById('mr').value=d.default_music_root||'';
    document.getElementById('loginForm').onsubmit=function(){return doSetup()};
  }
});
function doLogin(){
  var u=document.getElementById('lu').value,p=document.getElementById('lp').value;
  fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})})
  .then(function(r){return r.json()}).then(function(d){
    if(d.ok) window.location.reload(); else document.getElementById('lerr').textContent=d.error||'Ошибка';
  });return false;
}
function doSetup(){
  var u=document.getElementById('lu').value,p=document.getElementById('lp').value,p2=document.getElementById('lp2').value,mr=document.getElementById('mr').value;
  if(p!==p2){document.getElementById('lerr').textContent='Пароли не совпадают';return false;}
  fetch('/api/auth/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p,music_root:mr})})
  .then(function(r){return r.json()}).then(function(d){
    if(d.ok) window.location.reload(); else document.getElementById('lerr').textContent=d.error||'Ошибка';
  });return false;
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _is_demo(self, udata):
        return udata.get("role") == "demo" if udata else False

    def _deny_demo(self, udata):
        if self._is_demo(udata):
            self._respond_json({"ok": False, "error": "Недоступно для демо-аккаунта."})
            return True
        return False

    def _get_user(self):
        """Извлекает текущего пользователя из cookie."""
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("session="):
                token = part[len("session="):]
                return get_session_user(token)
        return None

    def _set_cookie(self, token):
        self.send_header("Set-Cookie", "session={}; Path=/; HttpOnly; SameSite=Strict; Max-Age={}".format(token, 86400*30))

    def _is_localhost(self):
        """True only for connections coming from the local machine."""
        ip = self.client_address[0] if self.client_address else ""
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _needs_auth(self, path):
        return not path.startswith("/api/auth/")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # PWA icon — 180x180 PNG vinyl record (RGB, no alpha — iOS compatible)
        if path == "/icon.png":
            import struct, zlib
            W = 180
            cx, cy = W // 2, W // 2
            pixels = []
            for y in range(W):
                row = []
                for x in range(W):
                    dx, dy = x - cx, y - cy
                    d = (dx*dx + dy*dy) ** 0.5
                    if d < 6:
                        row.extend([17, 17, 22])     # center hole
                    elif d < 30:
                        row.extend([233, 69, 96])    # red label
                    elif d < 33:
                        row.extend([40, 40, 40])     # label edge
                    elif d < 85:
                        g = int(22 + (d - 33) * 0.15) if int(d) % 4 < 2 else int(17 + (d - 33) * 0.12)
                        row.extend([g, g, g])        # grooves
                    elif d < 88:
                        row.extend([35, 35, 35])     # outer edge
                    else:
                        row.extend([17, 17, 22])     # background
                pixels.append(bytes([0] + row))  # filter byte + RGB
            raw = b''.join(pixels)
            def _png_chunk(ctype, data):
                c = ctype + data
                return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
            sig = b'\x89PNG\r\n\x1a\n'
            ihdr = struct.pack('>IIBBBBB', W, W, 8, 2, 0, 0, 0)  # 8-bit RGB
            png = sig + _png_chunk(b'IHDR', ihdr) + _png_chunk(b'IDAT', zlib.compress(raw, 9)) + _png_chunk(b'IEND', b'')
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            self.wfile.write(png)
            return

        # Reset page — clears SW cache, not intercepted by SW
        if path == "/reset":
            self._respond(200, "text/html", b"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Reset</title>
<style>body{background:#111;color:#eee;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{text-align:center;padding:20px}h2{color:#e94560}p{color:rgba(255,255,255,0.5);font-size:14px;margin:12px 0}</style></head>
<body><div class="box"><h2>Reset</h2><p id="s">Clearing cache...</p></div>
<script>
(async function(){
  var s=document.getElementById('s');
  try{
    if(navigator.serviceWorker){
      var regs=await navigator.serviceWorker.getRegistrations();
      for(var r of regs) await r.unregister();
      s.textContent='SW: '+regs.length+' cleared';
    }
    if(window.caches){
      var names=await caches.keys();
      for(var n of names) await caches.delete(n);
      s.textContent='Cache cleared. Redirecting...';
    } else { s.textContent='Done. Redirecting...'; }
  }catch(e){s.textContent=e.message;}
  setTimeout(function(){window.location.href='/';},1500);
})();
</script></body></html>""")
            return

        # Auth check
        if path.startswith("/api/auth/"):
            return self._handle_auth_get(path, parsed)

        user = self._get_user()
        users = load_users()

        # No users yet or not logged in — show login
        if path == "/" or path == "/index.html":
            if not users or not user:
                self._respond(200, "text/html", LOGIN_PAGE.encode("utf-8"))
                return
            build_hash = hashlib.md5(HTML_PAGE.encode()).hexdigest()[:8]
            page = HTML_PAGE.replace("PORT_PLACEHOLDER", str(SERVER_PORT)).replace("APP_BUILD_HASH", build_hash)
            self._respond(200, "text/html", page.encode("utf-8"))

        elif path == "/sw.js":
            # Inject build hash so SW updates when app changes
            build_hash = hashlib.md5(HTML_PAGE.encode()).hexdigest()[:8]
            sw_code = SW_JS.replace("BUILD_HASH", build_hash).replace("SW_PORT", str(SERVER_PORT))
            self._respond(200, "application/javascript", sw_code.encode("utf-8"))
            return

        # Version check — no auth needed, used for force-update
        if path == "/api/version":
            build_hash = hashlib.md5(HTML_PAGE.encode()).hexdigest()[:8]
            self._respond_json({"version": build_hash})
            return

        # Desktop widget — current now-playing state (localhost only, no auth)
        if path == "/api/widget/state":
            if not self._is_localhost():
                self._respond_json({"error": "forbidden"})
                return
            # Эту ручку читает сам виджет — значит он запущен. Другого признака
            # его присутствия у нас нет, и лучшего не нужно.
            global _widget_seen
            _widget_seen = time.time()
            with _widget_lock:
                st = dict(_widget_state)
            ts = st.pop("ts", 0)
            # age = seconds since the browser last reported state; large/None
            # means no live player tab is open, so controls won't be received.
            st["age"] = round(time.time() - ts, 1) if ts else None
            self._respond_json(st)
            return

        # Desktop widget — browser polls for a pending control command
        if path == "/api/widget/command":
            global _widget_command
            if not self._is_localhost():
                self._respond_json({"error": "forbidden"})
                return
            with _widget_lock:
                cmd = _widget_command
                _widget_command = None
            # Браузер по этому флагу решает, как часто спрашивать: пока виджета
            # нет, чаще раза в десять секунд незачем.
            alive = WIDGET_POSSIBLE and (time.time() - _widget_seen) < WIDGET_ALIVE_SEC
            self._respond_json({"cmd": cmd, "widget": alive})
            return

        if not user:
            # /api/cover/ и /api/stream/ читаются как двоичные данные, а не как
            # JSON: отдать сюда 200 с {"error": "unauthorized"} — значит скормить
            # клиенту 25 байт JSON вместо картинки или трека. Клиент видел
            # успешный ответ и клал этот мусор в офлайн-кэш навсегда. Для таких
            # эндпоинтов нужен честный 401.
            if path.startswith("/api/cover/") or path.startswith("/api/stream/"):
                self._respond(401, "text/plain", b"Unauthorized")
                return
            self._respond_json({"error": "unauthorized"})
            return

        udata = get_user_data(user)

        if path == "/api/config":
            folders = get_user_folders(user)
            last = get_user_last_folder(user)
            local_ip = get_local_ip()
            proto = "https" if _use_https else "http"
            lan_url = "{}://{}:{}".format(proto, local_ip, SERVER_PORT) if IS_PUBLIC else None
            all_urls = ["{}://{}:{}".format(proto, ip, SERVER_PORT) for ip in get_all_local_ips()] if IS_PUBLIC else []
            lan_host_url = get_lan_host_url() if IS_PUBLIC else None
            # Detect if client is the server machine (localhost or own IP)
            client_ip = self.client_address[0] if self.client_address else ''
            local_ips = set(['127.0.0.1', '::1'] + get_all_local_ips())
            is_local = client_ip in local_ips
            self._respond_json({
                "folders": folders,
                "last_folder": last,
                "public": IS_PUBLIC,
                "lan_url": lan_url,
                "all_urls": all_urls,
                # Стабильный адрес по mDNS-имени: не меняется при смене сети,
                # поэтому именно его надо использовать для установки PWA.
                "lan_host_url": lan_host_url,
                "username": user,
                "is_admin": udata.get("is_admin", False) if udata else False,
                "role": udata.get("role", "user") if udata else "user",
                "music_root": get_music_root(),
                "is_local": is_local,
                "widget_possible": WIDGET_POSSIBLE,
            })

        elif path == "/api/scan":
            params = parse_qs(parsed.query)
            folder = params.get("path", [""])[0]
            if not folder or not Path(folder).is_dir():
                self._respond_json({"error": "Папка не найдена: " + folder})
                return
            is_admin_user = udata.get("is_admin", False) if udata else False
            is_demo = self._is_demo(udata)
            # Demo users can only scan their assigned folders
            if is_demo:
                user_folders = get_user_folders(user)
                if folder not in user_folders:
                    self._respond_json({"error": "Недоступно для демо-аккаунта."})
                    return
            elif not is_admin_user and not is_path_within(folder, get_music_root()):
                self._respond_json({"error": "Доступ запрещён. Каталог вне корневой папки музыки."})
                return
            # prefetch=1: just return the catalog state for offline caching, without
            # making it the user's current/last folder.
            prefetch = params.get("prefetch", [""])[0] == "1"
            if not prefetch:
                _user_music_dirs[user] = folder
                if not is_demo:
                    add_user_folder(user, folder)
                set_user_last_folder(user, folder)
            track_list = scan_library(folder)
            album_list = group_by_album(track_list)
            self._respond_json({"tracks": track_list, "albums": album_list})

        elif path == "/api/search":
            params = parse_qs(parsed.query)
            q = params.get("q", [""])[0].lower().strip()
            if not q or not _user_music_dirs.get(user, ""):
                self._respond_json({"results": []})
                return
            track_list = scan_library(_user_music_dirs.get(user, ""))
            results = [t for t in track_list if q in "{} {} {}".format(t["title"], t["artist"], t["album"]).lower()]
            self._respond_json({"results": results})

        elif path == "/api/meta/status":
            ms = get_meta_state(user)
            self._respond_json({
                "running": ms["running"], "done": ms["done"],
                "progress": ms["progress"], "total": ms["total"],
                "log": ms["log"][-300:],
            })

        elif path == "/api/vk/status":
            vs = get_vk_state(user)
            authenticated = vs["service"] is not None
            self._respond_json({
                "authenticated": authenticated, "running": vs["running"],
                "done": vs["done"], "progress": vs["progress"],
                "total": vs["total"], "log": vs["log"][-300:], "has_vk": HAS_VK,
            })

        elif path == "/api/local/pick/status":
            if not self._is_localhost():
                self._respond_json({"ok": False, "error": "forbidden"})
                return
            with _local_pick_lock:
                st = _local_pick.get(user, {"picking": False, "done": False, "files": []})
            self._respond_json({
                "ok": True,
                "picking": st.get("picking", False),
                "done": st.get("done", False),
                "files": st.get("files", []),
            })

        elif path.startswith("/api/cover/"):
            filename = unquote(path[len("/api/cover/"):])
            # Verify user access to current MUSIC_DIR
            user_folders = get_user_folders(user)
            udir = _user_music_dirs.get(user, "") or get_user_last_folder(user)
            if not udir:
                self._respond(404, "text/plain", b"Not found")
                return
            if udir not in user_folders:
                self._respond(403, "text/plain", b"Forbidden")
                return
            _user_music_dirs[user] = udir
            filepath = _safe_path(udir, filename)
            if not filepath or not filepath.is_file():
                self._respond(404, "text/plain", b"Not found")
                return
            meta = get_metadata(str(filepath))
            if meta["cover"]:
                img_data = base64.b64decode(meta["cover"])
                mime = meta["cover_mime"] or "image/jpeg"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(img_data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(img_data)
            else:
                self._respond(404, "text/plain", b"No cover")

        elif path == "/api/releases":
            # Только отдаём накопленное. Проверка артистов запускается
            # исключительно кнопкой (/api/releases/refresh): ходить в чужие API
            # и сканировать библиотеку в фоне, когда пользователь об этом не
            # просил, — не то поведение, которого от плеера ждут.
            q = parse_qs(parsed.query)
            try:
                limit = int(q.get("limit", [RELEASES_LIMIT])[0])
            except Exception:
                limit = RELEASES_LIMIT
            limit = max(50, min(3000, limit))
            # Обе ленты растут из одного кэша артистов, поэтому это одна ручка
            # с переключателем, а не два раздельных конвейера.
            if q.get("mode", ["new"])[0] == "foryou":
                self._respond_json(build_foryou_feed(user, limit))
            else:
                self._respond_json(build_releases_feed(user, limit))

        elif path == "/api/releases/preview":
            src_url = parse_qs(parsed.query).get("u", [""])[0]
            mime = _preview_mime(src_url)
            if not mime:
                self._respond(400, "text/plain", b"Bad preview url")
                return
            try:
                client = _http()
                try:
                    resp = client.get(src_url)
                    if resp.status_code != 200:
                        self._respond(502, "text/plain", b"Preview unavailable")
                        return
                    body = resp.content
                finally:
                    try: client.close()
                    except Exception: pass
            except Exception:
                self._respond(502, "text/plain", b"Preview unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/releases/art":
            art_url = parse_qs(parsed.query).get("u", [""])[0]
            if not _art_allowed(art_url):
                self._respond(400, "text/plain", b"Bad art url")
                return
            body, mime = get_release_art(art_url)
            if not body:
                self._respond(404, "text/plain", b"No art")
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=2592000, immutable")
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/releases/tracks":
            params = parse_qs(parsed.query)
            rid = params.get("rid", [""])[0]
            src = params.get("source", ["itunes"])[0]
            if not rid:
                self._respond_json({"ok": False, "error": "no id"})
                return
            tracks = release_tracks(src, rid,
                                    params.get("artist", [""])[0],
                                    params.get("title", [""])[0])
            self._respond_json({"ok": True, "tracks": tracks})

        elif path == "/api/wan/status":
            active = _tunnel_proc is not None and _tunnel_proc.poll() is None
            self._respond_json({"active": active, "url": _tunnel_url})



        elif path == "/api/admin/download_catalog":
            # Admin-only: ZIP catalog to temp file, stream it
            if not udata or not udata.get("is_admin"):
                self._respond(403, "text/plain", b"Forbidden")
                return
            params = parse_qs(parsed.query)
            folder = params.get("path", [""])[0]
            if not folder or not Path(folder).is_dir():
                self._respond(404, "text/plain", b"Not found")
                return
            import zipfile, tempfile
            folder_path = Path(folder)
            folder_name = folder_path.name or "catalog"
            audio_files = sorted([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_FORMATS])
            if not audio_files:
                self._respond(404, "text/plain", b"No audio files")
                return
            # Write ZIP to temp file (avoids loading all into RAM)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            try:
                with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_STORED) as zf:
                    for f in audio_files:
                        zf.write(f, f.name)
                tmp.close()
                size = os.path.getsize(tmp.name)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", 'attachment; filename="{}.zip"'.format(folder_name))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                # Stream in 1MB chunks
                with open(tmp.name, 'rb') as zf:
                    while True:
                        chunk = zf.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            finally:
                os.unlink(tmp.name)

        elif path == "/api/browse":
            params = parse_qs(parsed.query)
            music_root = get_music_root()
            is_admin_user = udata.get("is_admin", False) if udata else False
            # Non-admins restricted to MUSIC_ROOT
            default_path = music_root if not is_admin_user else str(Path.home())
            browse_path = params.get("path", [""])[0] or default_path
            p = Path(browse_path)
            if not p.is_dir():
                p = Path(default_path)
            # Enforce boundary for non-admins
            if not is_admin_user and not is_path_within(str(p), music_root):
                p = Path(music_root)
            items = []
            parent = str(p.parent)
            # Allow going up only within allowed boundary
            can_go_up = parent != str(p)
            if not is_admin_user:
                can_go_up = can_go_up and is_path_within(parent, music_root)
            if can_go_up:
                items.append({"name": "..", "path": parent, "is_dir": True})
            try:
                for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                    if child.name.startswith('.'):
                        continue
                    if child.is_dir():
                        items.append({"name": child.name, "path": str(child), "is_dir": True})
                    elif child.suffix.lower() in SUPPORTED_FORMATS:
                        items.append({"name": child.name, "path": str(child), "is_dir": False})
            except PermissionError:
                pass
            # Count music files to show if folder has music
            music_count = sum(1 for c in items if not c["is_dir"])
            self._respond_json({"current": str(p), "items": items, "music_count": music_count})

        elif path == "/api/admin/users":
            if not udata or not udata.get("is_admin"):
                self._respond_json({"error": "forbidden"})
                return
            all_users = load_users()
            user_list = []
            for uname, ud in all_users.items():
                user_list.append({"username": uname, "is_admin": ud.get("is_admin", False), "role": ud.get("role", "user"), "folders": ud.get("folders", [])})
            self._respond_json({"users": user_list})

        elif path.startswith("/api/stream/"):
            filename = unquote(path[len("/api/stream/"):])
            user_folders = get_user_folders(user)
            udir = _user_music_dirs.get(user, "") or get_user_last_folder(user)
            if not udir:
                self._respond(404, "text/plain", b"Not found")
                return
            if udir not in user_folders:
                self._respond(403, "text/plain", b"Forbidden")
                return
            _user_music_dirs[user] = udir
            filepath = _safe_path(udir, filename)
            if not filepath or not filepath.is_file():
                self._respond(404, "text/plain", b"Not found")
                return
            mime = AUDIO_MIME.get(filepath.suffix.lower()) or mimetypes.guess_type(str(filepath))[0] or "audio/mpeg"
            size = filepath.stat().st_size
            range_header = self.headers.get("Range")
            if range_header:
                rm = re.match(r'bytes=(\d+)-(\d*)', range_header)
                if rm:
                    start = int(rm.group(1))
                    end = int(rm.group(2)) if rm.group(2) else size - 1
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    with open(filepath, "rb") as f:
                        f.seek(start)
                        self.wfile.write(f.read(length))
                    return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(filepath, "rb") as f:
                self.wfile.write(f.read())

        else:
            self._respond(404, "text/plain", b"Not found")

    def _handle_auth_get(self, path, parsed):
        if path == "/api/auth/check":
            users = load_users()
            user = self._get_user()
            self._respond_json({
                "needs_setup": len(users) == 0,
                "logged_in": user is not None,
                "default_music_root": str(Path.home() / "VinylMusic"),
            })
        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        # Desktop widget bridge (localhost only, no auth) — must run before the
        # auth gate so the browser tab and the widget can talk to each other.
        if path == "/api/widget/state":
            global _widget_state
            if not self._is_localhost():
                self._respond_json({"ok": False, "error": "forbidden"})
                return
            with _widget_lock:
                _widget_state = {
                    "playing": bool(data.get("playing")),
                    "title": str(data.get("title", ""))[:300],
                    "artist": str(data.get("artist", ""))[:300],
                    "album": str(data.get("album", ""))[:300],
                    "file": str(data.get("file", ""))[:600],
                    "position": data.get("position", 0),
                    "duration": data.get("duration", 0),
                    "ts": time.time(),
                }
            self._respond_json({"ok": True})
            return

        if path == "/api/widget/command":
            global _widget_command
            if not self._is_localhost():
                self._respond_json({"ok": False, "error": "forbidden"})
                return
            cmd = data.get("cmd")
            if cmd in ("play", "pause", "toggle", "next", "prev"):
                with _widget_lock:
                    _widget_command = cmd
                self._respond_json({"ok": True})
            else:
                self._respond_json({"ok": False, "error": "bad command"})
            return

        # Auth endpoints — no login required
        if path == "/api/auth/setup":
            users = load_users()
            if len(users) > 0:
                self._respond_json({"ok": False, "error": "Пользователи уже существуют."})
                return
            u, p = data.get("username", "").strip(), data.get("password", "")
            mr = data.get("music_root", "").strip()
            if not u or not p:
                self._respond_json({"ok": False, "error": "Заполните все поля."})
                return
            # Set MUSIC_ROOT before creating user (create_user uses it)
            if mr:
                set_music_root(mr)
            else:
                set_music_root(str(Path.home() / "VinylMusic"))
            create_user(u, p, is_admin=True, role="admin")
            token = create_session(u)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._set_cookie(token)
            body_bytes = json.dumps({"ok": True}).encode()
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        if path == "/api/auth/login":
            global _GLOBAL_FAIL_COUNT, _GLOBAL_FAIL_TIME
            client_ip = self.client_address[0]
            now = time.time()
            u, p = data.get("username", "").strip(), data.get("password", "")

            # Rate limit by IP
            ip_att = _login_attempts_ip.get(client_ip, (0, 0))
            if ip_att[0] >= _LOGIN_MAX_IP and (now - ip_att[1]) < _LOGIN_WINDOW:
                self._respond_json({"ok": False, "error": "Слишком много попыток с вашего IP. Подождите 5 минут."})
                return

            # Rate limit by username (password spraying protection)
            user_att = _login_attempts_user.get(u, (0, 0))
            if user_att[0] >= _LOGIN_MAX_USER and (now - user_att[1]) < _LOGIN_WINDOW:
                self._respond_json({"ok": False, "error": "Аккаунт временно заблокирован. Подождите 5 минут."})
                return

            # Global rate limit (distributed attack protection)
            if _GLOBAL_FAIL_COUNT >= _GLOBAL_MAX and (now - _GLOBAL_FAIL_TIME) < _LOGIN_WINDOW:
                self._respond_json({"ok": False, "error": "Слишком много неудачных попыток. Сервер приостановил вход."})
                return

            if not authenticate_user(u, p):
                # Increment all counters
                ip_c = ip_att[0] + 1 if (now - ip_att[1]) < _LOGIN_WINDOW else 1
                _login_attempts_ip[client_ip] = (ip_c, now)
                user_c = user_att[0] + 1 if (now - user_att[1]) < _LOGIN_WINDOW else 1
                _login_attempts_user[u] = (user_c, now)
                _GLOBAL_FAIL_COUNT = _GLOBAL_FAIL_COUNT + 1 if (now - _GLOBAL_FAIL_TIME) < _LOGIN_WINDOW else 1
                _GLOBAL_FAIL_TIME = now
                self._respond_json({"ok": False, "error": "Неверный логин или пароль."})
                return

            # Success — clear counters
            _login_attempts_ip.pop(client_ip, None)
            _login_attempts_user.pop(u, None)
            token = create_session(u)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._set_cookie(token)
            body_bytes = json.dumps({"ok": True}).encode()
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        if path == "/api/auth/logout":
            cookie_header = self.headers.get("Cookie", "")
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith("session="):
                    tok = part[len("session="):]
                    _sessions.pop(tok, None)
                    _save_sessions()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            body_bytes = json.dumps({"ok": True}).encode()
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return


        # All other POST endpoints require auth
        user = self._get_user()
        if not user:
            self._respond_json({"error": "unauthorized"})
            return
        udata = get_user_data(user)

        if path == "/api/meta/start":
            if self._deny_demo(udata): return
            ms = get_meta_state(user)
            if ms["running"]:
                self._respond_json({"ok": False, "already_running": True})
                return
            folder = data.get("path", _user_music_dirs.get(user, ""))
            if not folder or not Path(folder).is_dir():
                self._respond_json({"ok": False, "error": "Папка не найдена."})
                return
            user_folders = get_user_folders(user)
            if folder not in user_folders:
                self._respond_json({"ok": False, "error": "Нет доступа к каталогу."})
                return
            t = threading.Thread(target=metadata_worker, args=(folder, user), daemon=True)
            t.start()
            self._respond_json({"ok": True})

        elif path == "/api/meta/cancel":
            get_meta_state(user)["cancel"] = True
            self._respond_json({"ok": True})

        elif path == "/api/meta/proposals":
            ms = get_meta_state(user)
            # Return proposals without internal _meta
            proposals = []
            for p in ms.get("proposals", []):
                proposals.append({k: v for k, v in p.items() if not k.startswith('_')})
            self._respond_json({"ok": True, "proposals": proposals})

        elif path == "/api/meta/apply":
            if self._deny_demo(udata): return
            ms = get_meta_state(user)
            if ms["running"]:
                self._respond_json({"ok": False, "error": "Процесс уже идёт."})
                return
            selected_files = data.get("files", [])  # list of filenames to apply
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            if not selected_files or not folder:
                self._respond_json({"ok": False, "error": "Нет данных."})
                return
            # Filter proposals to only selected
            to_apply = [p for p in ms.get("proposals", []) if p["file"] in selected_files]
            if not to_apply:
                self._respond_json({"ok": False, "error": "Нет выбранных."})
                return
            t = threading.Thread(target=metadata_apply, args=(folder, to_apply, user), daemon=True)
            t.start()
            self._respond_json({"ok": True})

        elif path == "/api/meta/single":
            if self._deny_demo(udata): return
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            filename = data.get("file", "")
            if not folder or not filename:
                self._respond_json({"ok": False})
                return
            user_folders = get_user_folders(user)
            if folder not in user_folders:
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            filepath = _safe_path(folder, filename)
            if not filepath or not filepath.is_file():
                self._respond_json({"ok": False})
                return
            existing = get_metadata(str(filepath))
            if existing.get("artist") and existing.get("album") and existing.get("cover"):
                self._respond_json({"ok": True, "updated": False})
                return
            artist_q, title_q = parse_track_name(filename)
            found = search_metadata(artist_q, title_q)
            if not found:
                self._respond_json({"ok": True, "updated": False})
                return
            cover_data = fetch_cover_art(found)
            write_metadata_to_file(str(filepath), found, cover_data)
            done_set = _load_meta_done(folder)
            done_set.add(filename)
            _save_meta_done(folder, done_set)
            self._respond_json({"ok": True, "updated": True, "artist": found.get("artist", ""), "album": found.get("album", ""), "has_cover": cover_data is not None})

        elif path == "/api/public":
            if not udata or not udata.get("is_admin"):
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            enabled = data.get("enabled", False)
            global IS_PUBLIC, _use_https
            IS_PUBLIC = enabled
            local_ip = get_local_ip()
            s = load_settings()
            if enabled:
                # Auto-enable HTTPS for LAN (needed for SW on non-localhost)
                if not _use_https:
                    if _generate_self_signed_cert():
                        _use_https = True
                s["lan"] = True
                s["https"] = _use_https
                save_settings(s)
                all_ips = get_all_local_ips()
                proto = "https" if _use_https else "http"
                # Follow the toggle in the same window: switch to the LAN HTTPS port.
                # Safe now that this is a distinct port (7656 is always HTTPS; the
                # local 7666 stays plain HTTP and is never pinned by the browser).
                redirect_url = "{}://127.0.0.1:{}".format(proto, SERVER_PORT)
                lan_url = "{}://{}:{}".format(proto, local_ip, SERVER_PORT)
                all_urls = ["{}://{}:{}".format(proto, ip, SERVER_PORT) for ip in all_ips]
                self._respond_json({"ok": True, "public": True, "redirect_url": redirect_url, "lan_url": lan_url, "ip": local_ip, "all_urls": all_urls, "lan_host_url": get_lan_host_url()})
                try: self.wfile.flush()
                except Exception: pass
                def _restart_and_watchdog():
                    _start_server("0.0.0.0")  # LAN HTTPS on 7656 (local server keeps running)
                    if _use_https:
                        _start_cert_watchdog()
                threading.Timer(1.0, _restart_and_watchdog).start()
            else:
                _use_https = False
                s["lan"] = False
                s["https"] = False
                save_settings(s)
                # Redirect the window back to the always-on local HTTP port
                # (localhost host avoids the 127.0.0.1 HTTPS pin).
                local_url = "http://localhost:{}".format(LOCAL_PORT)
                self._respond_json({"ok": True, "public": False, "redirect_url": local_url})
                try: self.wfile.flush()
                except Exception: pass
                threading.Timer(1.0, _stop_server).start()  # stop LAN server; local stays up


        elif path == "/api/releases/star":
            key = str(data.get("key", ""))
            if not key:
                self._respond_json({"ok": False, "error": "no key"})
                return
            n = set_release_starred(user, key, bool(data.get("on")), data.get("item"))
            self._respond_json({"ok": True, "starred_count": n})

        elif path == "/api/releases/refresh":
            if _releases_state["running"]:
                self._respond_json({"ok": True, "already": True})
                return
            threading.Thread(target=refresh_releases, args=(user,), daemon=True).start()
            self._respond_json({"ok": True})

        elif path == "/api/cert/renew":
            if not udata or not udata.get("is_admin"):
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            if _renew_cert_and_restart():
                self._respond_json({"ok": True, "renewed": True})
            else:
                self._respond_json({"ok": False, "error": "Не удалось обновить сертификат"})

        elif path == "/api/remove_folder":
            if self._deny_demo(udata): return
            folder = data.get("path", "")
            remove_user_folder(user, folder)
            self._respond_json({"ok": True})

        elif path == "/api/vk/auth":
            if self._deny_demo(udata): return
            raw = data.get("url", "")
            m = re.search(r'access_token=([A-Za-z0-9._-]+)', raw)
            token = m.group(1) if m else raw.strip()
            if not token:
                self._respond_json({"ok": False, "error": "Не удалось извлечь токен."})
                return
            if not HAS_VK:
                self._respond_json({"ok": False, "error": "vkpymusic не установлен."})
                return
            if not vk_validate_token(token):
                self._respond_json({"ok": False, "error": "Токен невалиден."})
                return
            set_user_vk_token(user, token)
            get_vk_state(user)["service"] = VkService(VK_USER_AGENT, token)
            self._respond_json({"ok": True})

        elif path == "/api/vk/download":
            if self._deny_demo(udata): return
            vs = get_vk_state(user)
            if vs["running"]:
                self._respond_json({"ok": False, "already_running": True})
                return
            if not vs["service"]:
                self._respond_json({"ok": False, "error": "VK не авторизован."})
                return
            urls = data.get("urls", [])
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            user_folders = get_user_folders(user)
            if folder not in user_folders:
                is_admin_user = udata.get("is_admin", False) if udata else False
                if not is_admin_user:
                    self._respond_json({"ok": False, "error": "Нет доступа к каталогу."})
                    return
            order = data.get("order", "normal")
            mode = data.get("mode", "new")
            run_meta = data.get("run_meta", False)
            if not urls:
                self._respond_json({"ok": False, "error": "Нет ссылок."})
                return
            t = threading.Thread(target=vk_download_worker, args=(urls, folder, order, mode, run_meta, user), daemon=True)
            t.start()
            self._respond_json({"ok": True})

        elif path == "/api/vk/cancel":
            get_vk_state(user)["cancel"] = True
            self._respond_json({"ok": True})

        elif path == "/api/local/pick":
            # Open a native file picker on the server machine. Only the local
            # machine may do this — over LAN/WAN the dialog would pop up on the
            # server, not the remote device, so it's meaningless and forbidden.
            if not self._is_localhost():
                self._respond_json({"ok": False, "error": "Доступно только на локальном компьютере."})
                return
            if self._deny_demo(udata):
                return
            with _local_pick_lock:
                st = _local_pick.get(user)
                if st and st.get("picking"):
                    self._respond_json({"ok": True, "picking": True})
                    return
                _local_pick[user] = {"picking": True, "done": False, "files": []}

            def _run_pick(uname=user):
                files = _native_pick_files()
                with _local_pick_lock:
                    _local_pick[uname] = {"picking": False, "done": True, "files": files}

            threading.Thread(target=_run_pick, daemon=True).start()
            self._respond_json({"ok": True, "picking": True})

        elif path == "/api/local/import":
            if not self._is_localhost():
                self._respond_json({"ok": False, "error": "Доступно только на локальном компьютере."})
                return
            if self._deny_demo(udata):
                return
            vs = get_vk_state(user)
            if vs["running"]:
                self._respond_json({"ok": False, "already_running": True})
                return
            files = data.get("files", [])
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            user_folders = get_user_folders(user)
            if folder not in user_folders:
                is_admin_user = udata.get("is_admin", False) if udata else False
                if not is_admin_user:
                    self._respond_json({"ok": False, "error": "Нет доступа к каталогу."})
                    return
            mode = data.get("mode", "append")
            position = data.get("position", 1)
            run_meta = data.get("run_meta", False)
            if not files:
                self._respond_json({"ok": False, "error": "Файлы не выбраны."})
                return
            t = threading.Thread(target=local_import_worker,
                                 args=(files, folder, mode, position, run_meta, user), daemon=True)
            t.start()
            self._respond_json({"ok": True})

        elif path == "/api/import/parse":
            # Parse external playlist and match tracks with VK
            if self._deny_demo(udata): return
            vs = get_vk_state(user)
            if not vs["service"]:
                self._respond_json({"ok": False, "error": "VK не авторизован."})
                return
            ext_url = data.get("url", "").strip()
            if not ext_url:
                self._respond_json({"ok": False, "error": "Вставьте ссылку."})
                return
            tracks_list, platform = parse_external_playlist(ext_url)
            if not tracks_list:
                self._respond_json({"ok": False, "error": "Не удалось получить треки. Убедитесь что плейлист публичный."})
                return
            # Match each track with VK
            matches = []
            for t in tracks_list:  # no artificial limit
                query = "{} {}".format(t.get("artist", ""), t.get("title", "")).strip()
                if not query:
                    continue
                try:
                    results = vs["service"].search_songs_by_text(query, count=1)
                    if results:
                        s = results[0]
                        matches.append({
                            "original_artist": t.get("artist", ""),
                            "original_title": t.get("title", ""),
                            "vk_artist": s.artist,
                            "vk_title": s.title,
                            "vk_id": "{}_{}".format(s.owner_id, s.track_id),
                            "vk_duration": s.duration,
                            "has_url": bool(s.url and "index.m3u8" not in s.url),
                            "matched": True,
                        })
                    else:
                        matches.append({
                            "original_artist": t.get("artist", ""),
                            "original_title": t.get("title", ""),
                            "vk_artist": "", "vk_title": "", "vk_id": "",
                            "vk_duration": 0, "has_url": False, "matched": False,
                        })
                except Exception as e:
                    err_str = str(e)
                    if "captcha" in err_str.lower() or "Captcha" in err_str:
                        # Stop immediately, return what we have + error flag
                        self._respond_json({"ok": True, "platform": platform, "matches": matches, "total": len(tracks_list),
                            "warning": "VK включил captcha после {} треков. Подождите 1-2 часа.".format(len(matches))})
                        return
                    print("VK import match error:", err_str[:100])
                    matches.append({
                        "original_artist": t.get("artist", ""),
                        "original_title": t.get("title", ""),
                        "vk_artist": "", "vk_title": "", "vk_id": "",
                        "vk_duration": 0, "has_url": False, "matched": False,
                    })
                time.sleep(0.3)
            self._respond_json({"ok": True, "platform": platform, "matches": matches, "total": len(tracks_list)})

        elif path == "/api/import/retry":
            # Retry matching for unmatched tracks only
            if self._deny_demo(udata): return
            vs = get_vk_state(user)
            if not vs["service"]:
                self._respond_json({"ok": False, "error": "VK не авторизован."})
                return
            retry_tracks = data.get("tracks", [])
            if not retry_tracks:
                self._respond_json({"ok": False, "error": "Нет треков."})
                return
            matches = []
            warning = None
            for t in retry_tracks[:200]:
                query = "{} {}".format(t.get("artist", ""), t.get("title", "")).strip()
                if not query:
                    matches.append({"original_artist": t.get("artist",""), "original_title": t.get("title",""),
                        "vk_artist":"","vk_title":"","vk_id":"","vk_duration":0,"has_url":False,"matched":False})
                    continue
                try:
                    results = vs["service"].search_songs_by_text(query, count=1)
                    if results:
                        s = results[0]
                        matches.append({"original_artist": t.get("artist",""), "original_title": t.get("title",""),
                            "vk_artist": s.artist, "vk_title": s.title,
                            "vk_id": "{}_{}".format(s.owner_id, s.track_id),
                            "vk_duration": s.duration,
                            "has_url": bool(s.url and "index.m3u8" not in s.url), "matched": True})
                    else:
                        matches.append({"original_artist": t.get("artist",""), "original_title": t.get("title",""),
                            "vk_artist":"","vk_title":"","vk_id":"","vk_duration":0,"has_url":False,"matched":False})
                except Exception as e:
                    if "captcha" in str(e).lower():
                        warning = "VK снова включил captcha после {} треков.".format(len(matches))
                        break
                    matches.append({"original_artist": t.get("artist",""), "original_title": t.get("title",""),
                        "vk_artist":"","vk_title":"","vk_id":"","vk_duration":0,"has_url":False,"matched":False})
                time.sleep(0.3)
            resp = {"ok": True, "matches": matches}
            if warning:
                resp["warning"] = warning
            self._respond_json(resp)

        elif path == "/api/import/re_search":
            # Re-search single track in VK with custom query
            if self._deny_demo(udata): return
            vs = get_vk_state(user)
            if not vs["service"]:
                self._respond_json({"ok": False, "error": "VK не авторизован."})
                return
            query = data.get("query", "").strip()
            if not query:
                self._respond_json({"ok": False, "error": "Пустой запрос."})
                return
            try:
                results = vs["service"].search_songs_by_text(query, count=5)
                items = []
                for s in results:
                    items.append({
                        "vk_artist": s.artist, "vk_title": s.title,
                        "vk_id": "{}_{}".format(s.owner_id, s.track_id),
                        "vk_duration": s.duration,
                        "has_url": bool(s.url and "index.m3u8" not in s.url),
                    })
                self._respond_json({"ok": True, "results": items})
            except Exception:
                self._respond_json({"ok": False, "error": "Ошибка поиска."})

        elif path == "/api/vk/search":
            if self._deny_demo(udata): return
            vs = get_vk_state(user)
            if not vs["service"]:
                self._respond_json({"ok": False, "error": "VK не авторизован."})
                return
            query = data.get("query", "").strip()
            if not query:
                self._respond_json({"ok": False, "error": "Пустой запрос."})
                return
            try:
                results = vs["service"].search_songs_by_text(query, count=20)
                items = []
                for s in results:
                    items.append({
                        "title": s.title,
                        "artist": s.artist,
                        "duration": s.duration,
                        "track_id": s.track_id,
                        "owner_id": s.owner_id,
                        "has_url": bool(s.url and "index.m3u8" not in s.url),
                    })
                self._respond_json({"ok": True, "results": items})
            except Exception as e:
                err_msg = str(e)
                if "access_token" in err_msg or "authorization" in err_msg.lower():
                    vs["service"] = None
                    self._respond_json({"ok": False, "error": "Токен VK истёк. Переавторизуйтесь."})
                elif "captcha" in err_msg.lower():
                    self._respond_json({"ok": False, "error": "VK временно ограничил доступ (captcha). Подождите 1-2 часа и попробуйте снова. Это нормально при частых запросах."})
                else:
                    print("VK search error:", err_msg)
                    self._respond_json({"ok": False, "error": "Ошибка поиска VK: " + err_msg[:100]})

        elif path == "/api/vk/download_tracks":
            if self._deny_demo(udata): return
            vs = get_vk_state(user)
            if not vs["service"]:
                self._respond_json({"ok": False, "error": "VK не авторизован."})
                return
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            track_ids = data.get("track_ids", [])  # ["owner_id_track_id", ...]
            mode = data.get("mode", "append")
            run_meta = data.get("run_meta", False)
            if not folder or not track_ids:
                self._respond_json({"ok": False, "error": "Нет данных."})
                return
            user_folders = get_user_folders(user)
            if folder not in user_folders:
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            def dl_tracks():
                vst = get_vk_state(user)
                vst["running"] = True
                vst["done"] = False
                vst["log"] = []
                vst["progress"] = 0
                vst["total"] = len(track_ids)
                try:
                    songs = vs["service"].get_songs_by_id(track_ids)
                    save_dir = Path(folder)
                    save_dir.mkdir(parents=True, exist_ok=True)
                    existing = vk_get_existing_tracks(folder)
                    if mode == "prepend" and existing:
                        vk_renumber_tracks(folder, start_from=len(songs) + 1)
                        start_num = 1
                    elif mode == "append" and existing:
                        start_num = max(t[0] for t in existing) + 1
                    else:
                        start_num = 1
                    pad = len(str(start_num + len(songs) - 1))
                    for idx, song in enumerate(songs):
                        vst["progress"] = idx + 1
                        num_str = str(start_num + idx).zfill(pad)
                        artist = vk_safe_filename(song.artist)
                        title = vk_safe_filename(song.title)
                        filepath = save_dir / "{}. {} - {}.mp3".format(num_str, artist, title)
                        if vk_download_song(song, filepath):
                            vst["log"].append("OK: {} - {}".format(artist, title))
                        else:
                            vst["log"].append("FAIL: {} - {}".format(artist, title))
                        time.sleep(0.3)
                    vk_repad_tracks(folder)
                    if run_meta:
                        vst["log"].append("\nЗапускаю поиск мета-данных...")
                        metadata_worker(folder, user)
                    vst["log"].append("\nГотово!")
                except Exception:
                    vst["log"].append("Ошибка загрузки.")
                finally:
                    vst["running"] = False
                    vst["done"] = True
            t = threading.Thread(target=dl_tracks, daemon=True)
            t.start()
            self._respond_json({"ok": True})

        elif path == "/api/track/delete":
            if self._deny_demo(udata): return
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            filename = data.get("file", "")
            if not folder or not filename:
                self._respond_json({"ok": False, "error": "Нет данных."})
                return
            user_folders = get_user_folders(user)
            if folder not in user_folders:
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            filepath = _safe_path(folder, filename)
            if not filepath or not filepath.is_file():
                self._respond_json({"ok": False, "error": "Файл не найден."})
                return
            try:
                filepath.unlink()
                self._respond_json({"ok": True})
            except Exception as ex:
                self._respond_json({"ok": False, "error": str(ex)})

        elif path == "/api/track/delete_bulk":
            if self._deny_demo(udata): return
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            files = data.get("files", [])
            if not folder or not files:
                self._respond_json({"ok": False, "error": "Нет данных."})
                return
            user_folders = get_user_folders(user)
            if folder not in user_folders:
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            deleted = 0
            for fn in files:
                fp = _safe_path(folder, fn)
                if fp and fp.is_file():
                    try:
                        fp.unlink()
                        deleted += 1
                    except Exception:
                        pass
            self._respond_json({"ok": True, "deleted": deleted})

        elif path == "/api/meta/bulk":
            if self._deny_demo(udata): return
            ms = get_meta_state(user)
            if ms["running"]:
                self._respond_json({"ok": False, "already_running": True})
                return
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            files = data.get("files", [])
            if not folder or not files:
                self._respond_json({"ok": False, "error": "Нет данных."})
                return
            user_folders = get_user_folders(user)
            if folder not in user_folders:
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            t = threading.Thread(target=metadata_bulk_worker, args=(folder, files, user), daemon=True)
            t.start()
            self._respond_json({"ok": True})

        elif path == "/api/track/edit":
            if self._deny_demo(udata): return
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            old_file = data.get("file", "")
            new_title = data.get("title", "").strip()
            new_artist = data.get("artist", "").strip()
            new_order = data.get("order", 0)
            run_meta = data.get("run_meta", False)
            # Остальные теги и обложка приходят по желанию: пустое поле значит
            # «не трогать», а не «стереть» — стирать теги из окна правки нельзя,
            # это слишком легко сделать случайно.
            extra = {}
            for key in ("album", "genre", "albumartist"):
                val = (data.get(key) or "").strip()
                if val:
                    extra[key] = val
            try:
                if int(data.get("year") or 0):
                    extra["year"] = str(int(data["year"]))
            except (TypeError, ValueError):
                pass
            try:
                if int(data.get("track_no") or 0):
                    extra["track"] = int(data["track_no"])
            except (TypeError, ValueError):
                pass
            cover_bytes = None
            cover_b64 = data.get("cover") or ""
            if cover_b64:
                try:
                    raw = base64.b64decode(cover_b64.split(",", 1)[-1])
                    # Проверяем сигнатурой, а не доверяем присланному типу:
                    # записать в тег что угодно означало бы сломать файл.
                    if _art_sniff(raw):
                        cover_bytes = raw
                except Exception:
                    cover_bytes = None
            if not folder or not old_file or not new_title:
                self._respond_json({"ok": False, "error": "Заполните название."})
                return
            user_folders = get_user_folders(user)
            if folder not in user_folders:
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            old_path = _safe_path(folder, old_file)
            if not old_path or not old_path.exists():
                self._respond_json({"ok": False, "error": "Файл не найден."})
                return
            try:
                ext = old_path.suffix
                name_part = (vk_safe_filename(new_artist) + " - " + vk_safe_filename(new_title)) if new_artist else vk_safe_filename(new_title)
                old_match = re.match(r'^(\d+)\.\s+(.+)$', old_path.stem)
                has_numbering = old_match is not None
                wants_number = bool(new_order and int(new_order) > 0)

                if has_numbering or wants_number:
                    # Numbered catalog — reorder all tracks
                    old_num = int(old_match.group(1)) if old_match else 0
                    target_num = int(new_order) if new_order else old_num
                    if target_num < 1:
                        target_num = 1

                    all_tracks = vk_get_existing_tracks(folder)

                    # Step 1: rename all numbered tracks + our file to temp
                    temp_list = []
                    our_tmp = None
                    for num, tname, tpath in all_tracks:
                        tmp = tpath.parent / ("__tmp_te_{}_{}".format(num, tpath.name))
                        tpath.rename(tmp)
                        if tpath == old_path:
                            temp_list.append((num, name_part, tmp, True))
                            our_tmp = tmp
                        else:
                            temp_list.append((num, tname, tmp, False))

                    # If our file wasn't in numbered list (it was unnumbered), add it
                    if our_tmp is None:
                        our_tmp = old_path.parent / ("__tmp_te_new_" + old_path.name)
                        old_path.rename(our_tmp)
                        temp_list.append((0, name_part, our_tmp, True))

                    # Step 2: separate edited from others
                    edited = None
                    others = []
                    for num, tname, tmp, is_edited in temp_list:
                        if is_edited:
                            edited = (name_part, tmp)
                        else:
                            others.append((tname, tmp))

                    # Step 3: insert at target position
                    insert_pos = max(0, min(target_num - 1, len(others)))
                    others.insert(insert_pos, edited)

                    # Step 4: rename all with sequential numbers
                    pad = len(str(len(others)))
                    new_name = ""
                    for i, (tname, tmp) in enumerate(others):
                        final = "{}. {}{}".format(str(i + 1).zfill(pad), tname, tmp.suffix)
                        final_path = Path(folder) / final
                        tmp.rename(final_path)
                        if tmp == edited[1]:
                            new_name = final
                else:
                    # Non-numbered, no order requested: just rename
                    new_name = name_part + ext
                    new_path = old_path.parent / new_name
                    if new_path != old_path:
                        old_path.rename(new_path)
                # Always update title/artist tags in the file
                if new_name and HAS_MUTAGEN:
                    try:
                        fp = str(Path(folder) / new_name)
                        _update_tags(fp, new_title, new_artist)
                        # Остальное дописываем общим писателем: он умеет и
                        # обложку, и жанр с годом, и знает форматы.
                        if extra or cover_bytes:
                            write_metadata_to_file(str(fp), extra, cover_bytes, overwrite=True)
                    except Exception:
                        pass
                # Run meta search if requested
                if run_meta and new_name:
                    def do_meta():
                        fp = str(Path(folder) / new_name)
                        found = search_metadata(new_artist or '', new_title)
                        if found:
                            cover = fetch_cover_art(found)
                            # User explicitly asked to refresh — overwrite even when
                            # tags already exist (the default fill-only mode would do
                            # nothing for an already-tagged track). Keep the title/
                            # artist the user just set; only refresh album/year/cover.
                            refresh = dict(found)
                            refresh.pop('title', None)
                            refresh.pop('artist', None)
                            write_metadata_to_file(fp, refresh, cover, overwrite=True)
                    threading.Thread(target=do_meta, daemon=True).start()
                # Reordering renumbered other files — heal playlist references.
                repair_playlist_refs(folder)
                self._respond_json({"ok": True, "new_file": new_name})
            except Exception:
                self._respond_json({"ok": False, "error": "Ошибка переименования."})

        elif path == "/api/eras":
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            if not folder:
                self._respond_json({"ok": False, "error": "Нет каталога."})
                return
            if data.get("action") == "save":
                if self._deny_demo(udata): return
                self._respond_json({"ok": True, "eras": save_eras(folder, data)})
            else:
                self._respond_json({"ok": True, "eras": load_eras(folder)})

        elif path == "/api/plays":
            # POST и для чтения — как у /api/playlists: путь каталога в query
            # строке выглядел бы плохо и упирался бы в её длину.
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            if not folder:
                self._respond_json({"ok": False, "error": "Нет каталога."})
                return
            if data.get("action") == "record":
                if self._deny_demo(udata): return
                n = record_plays(user, folder, data.get("plays") or [])
                self._respond_json({"ok": True, "recorded": n})
            else:
                self._respond_json({"ok": True, "plays": get_plays(user, folder)})

        elif path == "/api/playlists":
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            action = data.get("action", "")
            if not folder:
                self._respond_json({"ok": False, "error": "Нет каталога."})
                return

            playlists = load_playlists(folder)

            if action == "list":
                playlists = repair_playlist_refs(folder, playlists)
                self._respond_json({"ok": True, "playlists": playlists})

            elif action in ("create", "update", "delete"):
                if self._deny_demo(udata): return

            if action == "create":
                name = data.get("name", "").strip() or "Новый плейлист"
                track_files = data.get("tracks", [])
                pl = {"id": secrets.token_hex(8), "name": name, "tracks": track_files}
                playlists.append(pl)
                save_playlists(folder, playlists)
                self._respond_json({"ok": True, "playlist": pl})

            elif action == "update":
                pl_id = data.get("id", "")
                for pl in playlists:
                    if pl["id"] == pl_id:
                        if "name" in data:
                            pl["name"] = data["name"]
                        if "tracks" in data:
                            pl["tracks"] = data["tracks"]
                        save_playlists(folder, playlists)
                        self._respond_json({"ok": True, "playlist": pl})
                        return
                self._respond_json({"ok": False, "error": "Плейлист не найден."})

            elif action == "delete":
                pl_id = data.get("id", "")
                playlists = [p for p in playlists if p["id"] != pl_id]
                save_playlists(folder, playlists)
                self._respond_json({"ok": True})

            elif action == "reorder":
                order = data.get("order", [])  # list of playlist IDs in new order
                if order:
                    by_id = {p["id"]: p for p in playlists}
                    reordered = [by_id[pid] for pid in order if pid in by_id]
                    # Append any playlists not in the order list
                    seen = set(order)
                    for p in playlists:
                        if p["id"] not in seen:
                            reordered.append(p)
                    save_playlists(folder, reordered)
                    self._respond_json({"ok": True})
                else:
                    self._respond_json({"ok": False, "error": "Пустой порядок."})

            else:
                self._respond_json({"ok": False, "error": "Неизвестное действие."})

        elif path == "/api/reorder":
            if self._deny_demo(udata): return
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            new_order = data.get("order", [])
            if not folder or not new_order:
                self._respond_json({"ok": False, "error": "Нет данных"})
                return
            # Check user has access to this folder
            user_folders = get_user_folders(user)
            if folder not in user_folders:
                self._respond_json({"ok": False, "error": "Нет доступа к каталогу."})
                return
            try:
                p = Path(folder)
                pad = len(str(len(new_order)))
                temp_map = {}
                for i, fname in enumerate(new_order):
                    # Sanitize filename — prevent path traversal
                    safe_name = Path(fname).name  # strips any ../
                    if safe_name != fname or '..' in fname:
                        continue
                    src = _safe_path(folder, safe_name)
                    if not src or not src.exists():
                        continue
                    tmp = p / ("__tmp_reorder_{}_{}".format(i, safe_name))
                    src.rename(tmp)
                    temp_map[i] = (tmp, safe_name)
                for i in sorted(temp_map.keys()):
                    tmp, fname = temp_map[i]
                    rm = re.match(r'^\d+\.\s+(.+)$', Path(fname).stem)
                    name_part = rm.group(1) if rm else Path(fname).stem
                    ext = Path(fname).suffix
                    new_name = "{}. {}{}".format(str(i+1).zfill(pad), name_part, ext)
                    tmp.rename(p / new_name)
                repair_playlist_refs(folder)
                self._respond_json({"ok": True})
            except Exception as e:
                self._respond_json({"ok": False, "error": "Ошибка переименования."})

        elif path == "/api/wan/start":
            if not udata or not udata.get("is_admin"):
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            mode = data.get("mode", "tunnel")
            if mode == "static":
                ip = data.get("ip", "")
                port = data.get("port", str(SERVER_PORT))
                if not ip:
                    self._respond_json({"ok": False, "error": "IP не указан"})
                    return
                wan_url = set_wan_static(ip, port)
                self._respond_json({"ok": True, "url": wan_url})
            else:
                t = threading.Thread(target=start_tunnel, daemon=True)
                self._respond_json({"ok": True, "status": "starting"})
                t.start()

        elif path == "/api/wan/stop":
            if not udata or not udata.get("is_admin"):
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            stop_tunnel()
            self._respond_json({"ok": True})

        # ── Admin endpoints ──
        elif path == "/api/admin/create_user":
            if not udata or not udata.get("is_admin"):
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            nu, np = data.get("username", "").strip(), data.get("password", "")
            role = data.get("role", "user")
            if role not in ("admin", "user", "demo"):
                role = "user"
            if not nu or not np:
                self._respond_json({"ok": False, "error": "Заполните все поля."})
                return
            if not create_user(nu, np, is_admin=(role == "admin"), role=role):
                self._respond_json({"ok": False, "error": "Пользователь уже существует."})
                return
            self._respond_json({"ok": True})

        elif path == "/api/admin/delete_user":
            if not udata or not udata.get("is_admin"):
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            target = data.get("username", "")
            if target == user:
                self._respond_json({"ok": False, "error": "Нельзя удалить себя."})
                return
            users_db = load_users()
            if target in users_db:
                del users_db[target]
                save_users(users_db)
            self._respond_json({"ok": True})

        elif path == "/api/admin/change_password":
            if not udata or not udata.get("is_admin"):
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            target = data.get("username", "")
            new_pw = data.get("password", "")
            if not target or not new_pw:
                self._respond_json({"ok": False, "error": "Заполните все поля."})
                return
            users_db = load_users()
            if target not in users_db:
                self._respond_json({"ok": False, "error": "Пользователь не найден."})
                return
            users_db[target]["password"] = _hash_pw(new_pw)
            save_users(users_db)
            self._respond_json({"ok": True})

        elif path == "/api/admin/set_folders":
            if not udata or not udata.get("is_admin"):
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            target = data.get("username", "")
            folders = data.get("folders", [])
            users_db = load_users()
            if target in users_db:
                users_db[target]["folders"] = folders
                save_users(users_db)
            self._respond_json({"ok": True})

        elif path == "/api/admin/set_music_root":
            if not udata or not udata.get("is_admin"):
                self._respond_json({"ok": False, "error": "Нет доступа."})
                return
            mr = data.get("music_root", "").strip()
            if not mr:
                self._respond_json({"ok": False, "error": "Путь не указан."})
                return
            set_music_root(mr)
            self._respond_json({"ok": True})

        elif path == "/api/profile/change_password":
            old_pw = data.get("old_password", "")
            new_pw = data.get("new_password", "")
            if not authenticate_user(user, old_pw):
                self._respond_json({"ok": False, "error": "Неверный текущий пароль."})
                return
            users_db = load_users()
            users_db[user]["password"] = _hash_pw(new_pw)
            save_users(users_db)
            self._respond_json({"ok": True})

        else:
            self._respond_json({"ok": False, "error": "Unknown endpoint"})

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-XSS-Protection", "1; mode=block")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._respond(200, "application/json", body)

    def log_message(self, format, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except ssl.SSLError:
            global _ssl_error_count
            _ssl_error_count += 1
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass


import signal

_tunnel_proc = None
_tunnel_url = None


def _find_cloudflared():
    """Ищет cloudflared: сначала рядом с бинарником (PyInstaller bundle), потом в PATH."""
    # PyInstaller bundle
    if getattr(sys, '_MEIPASS', None):
        bundled = os.path.join(sys._MEIPASS, 'cloudflared')
        if os.path.isfile(bundled):
            return bundled
        bundled_exe = bundled + '.exe'
        if os.path.isfile(bundled_exe):
            return bundled_exe
    # Same directory as script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for name in ['cloudflared', 'cloudflared.exe']:
        local = os.path.join(script_dir, name)
        if os.path.isfile(local):
            return local
    # System PATH
    return 'cloudflared'


def start_tunnel():
    """Запускает cloudflared tunnel и возвращает публичный URL."""
    global _tunnel_proc, _tunnel_url
    stop_tunnel()
    _tunnel_url = None
    cf_bin = _find_cloudflared()
    try:
        proc = subprocess.Popen(
            [cf_bin, "tunnel", "--url", "http://127.0.0.1:{}".format(LOCAL_PORT)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True
        )
        _tunnel_proc = proc
        # cloudflared prints URL to stderr/stdout, parse it
        import time as _t
        deadline = _t.time() + 30
        while _t.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            # URL looks like: https://xxx-xxx-xxx.trycloudflare.com
            m = re.search(r'(https://[a-zA-Z0-9-]+\.trycloudflare\.com)', line)
            if m:
                _tunnel_url = m.group(1)
                print("WAN tunnel: " + _tunnel_url)
                # Keep reading in background so pipe doesn't block
                def drain():
                    try:
                        while proc.poll() is None:
                            proc.stdout.readline()
                    except Exception:
                        pass
                threading.Thread(target=drain, daemon=True).start()
                return _tunnel_url
        print("cloudflared: не удалось получить URL")
        return None
    except FileNotFoundError:
        print("cloudflared не установлен. brew install cloudflared")
        return None
    except Exception as e:
        print("Ошибка tunnel: " + str(e))
        return None


def set_wan_static(ip, port):
    """Настраивает WAN в режиме статического IP — без туннеля."""
    global _tunnel_url, IS_PUBLIC, _tunnel_proc
    stop_tunnel()
    wan_url = "http://{}:{}".format(ip, port)
    _tunnel_url = wan_url
    # Persist to settings for auto-restore
    s = load_settings()
    s["wan_mode"] = "static"
    s["wan_ip"] = ip
    s["wan_port"] = port
    save_settings(s)
    if not IS_PUBLIC:
        IS_PUBLIC = True
        _restart_server("0.0.0.0")
        time.sleep(1)
    print("WAN static: " + wan_url)
    return wan_url


def stop_tunnel():
    global _tunnel_proc, _tunnel_url
    # Clear saved WAN config
    s = load_settings()
    s.pop("wan_mode", None)
    s.pop("wan_ip", None)
    s.pop("wan_port", None)
    save_settings(s)
    if _tunnel_proc:
        try:
            _tunnel_proc.terminate()
            _tunnel_proc.wait(timeout=5)
        except Exception:
            try:
                _tunnel_proc.kill()
            except Exception:
                pass
        _tunnel_proc = None
    _tunnel_url = None
    # Kill any orphan cloudflared processes
    try:
        subprocess.run(["pkill", "-f", "cloudflared tunnel"], timeout=3, capture_output=True)
    except Exception:
        pass


CERT_FILE = Path.home() / ".vinyl_cert.pem"
KEY_FILE = Path.home() / ".vinyl_key.pem"
_use_https = False


def _read_cert_san():
    """Возвращает SAN сертификата как (set(IP), set(DNS)) или None, если не прочитать.

    Читаем через cryptography, а если её нет — через `openssl x509 -text`:
    флаг `-ext` есть только в OpenSSL 1.1.1+, а на macOS системный openssl —
    это LibreSSL, где он отсутствует. Раньше проверка там всегда падала в
    except, сертификат перевыпускался при каждом запуске, и на iPhone каждый раз
    приходилось заново принимать самоподписанный сертификат.
    """
    if not CERT_FILE.exists():
        return None
    try:
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID
        cert = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        import ipaddress as _ipa
        return (set(str(i) for i in san.get_values_for_type(x509.IPAddress)),
                set(san.get_values_for_type(x509.DNSName)))
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["openssl", "x509", "-in", str(CERT_FILE), "-noout", "-text"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode()
        lines = out.split("\n")
        value = ""
        for i, line in enumerate(lines):
            if "Subject Alternative Name" in line:
                # значение идёт со следующей строки и может переноситься
                for cont in lines[i + 1:]:
                    if not cont.startswith("                "):
                        break
                    value += " " + cont.strip()
                break
        if not value:
            return None
        ips, dns = set(), set()
        for part in value.split(","):
            part = part.strip()
            if part.startswith("IP Address:"):
                ips.add(part[len("IP Address:"):].strip())
            elif part.startswith("DNS:"):
                dns.add(part[len("DNS:"):].strip())
        return (ips, dns)
    except Exception:
        return None


def _cert_covers_current_names():
    """Покрывает ли текущий сертификат все локальные IP и mDNS-имя машины."""
    san = _read_cert_san()
    if san is None:
        return False
    cert_ips, cert_dns = san
    for ip in set(get_all_local_ips()) | {"127.0.0.1"}:
        if ip not in cert_ips:
            return False
    host = get_mdns_hostname()
    if host and host not in cert_dns:
        return False
    return True


def _cert_expires_soon(days_threshold=30):
    """Check if cert expires within given number of days."""
    if not CERT_FILE.exists():
        return True
    try:
        result = subprocess.run(
            ["openssl", "x509", "-in", str(CERT_FILE), "-noout", "-checkend", str(days_threshold * 86400)],
            capture_output=True, timeout=5
        )
        # openssl returns 1 if cert expires within the period
        return result.returncode != 0
    except Exception:
        return True


_ssl_error_count = 0
_SSL_ERROR_THRESHOLD = 5


def _cert_needs_renewal():
    """Нужно ли перевыпускать сертификат: его нет, он истекает или не покрывает
    текущие адреса машины.

    Раньше сюда входил и счётчик TLS-ошибок, но это делало только хуже:
    ssl.SSLError в handle() — это почти всегда клиент, который ещё не принял
    самоподписанный сертификат (или оборвал соединение). Пять таких ошибок
    перевыпускали сертификат и перезапускали HTTPS-сервер, после чего телефону
    надо было принимать доверие заново — то есть ошибки порождали ещё больше
    ошибок, а все текущие загрузки рвались на середине.
    """
    global _ssl_error_count
    if not CERT_FILE.exists() or not KEY_FILE.exists():
        return True
    if _cert_expires_soon():
        return True
    if not _cert_covers_current_names():
        return True
    if _ssl_error_count >= _SSL_ERROR_THRESHOLD:
        # Диагностика, но не повод трогать рабочий сертификат.
        print("HTTPS: {} TLS-ошибок рукопожатия (клиент не принял сертификат?)".format(_ssl_error_count))
        _ssl_error_count = 0
    return False


def _renew_cert_and_restart():
    """Regenerate certificate and restart HTTPS server if needed."""
    global _use_https, _ssl_error_count
    if not IS_PUBLIC or not _use_https:
        return False
    print("HTTPS: автоматическая перегенерация сертификата...")
    if _generate_self_signed_cert(force=True):
        _ssl_error_count = 0
        s = load_settings()
        s["https"] = True
        save_settings(s)
        _restart_server("0.0.0.0")
        print("HTTPS: сертификат обновлён, сервер перезапущен")
        return True
    else:
        print("HTTPS: не удалось обновить сертификат")
        return False


_cert_watchdog_running = False


def _start_cert_watchdog():
    """Start certificate watchdog if not already running."""
    global _cert_watchdog_running
    if _cert_watchdog_running:
        return
    _cert_watchdog_running = True
    threading.Thread(target=_cert_watchdog, daemon=True).start()


def _cert_watchdog():
    """Background thread: periodically checks cert validity and auto-renews."""
    global _cert_watchdog_running
    while True:
        time.sleep(300)  # check every 5 minutes
        try:
            if not IS_PUBLIC or not _use_https:
                _cert_watchdog_running = False
                return  # stop watchdog if HTTPS/LAN disabled
            if _cert_needs_renewal():
                _renew_cert_and_restart()
        except Exception as ex:
            print(f"HTTPS watchdog error: {ex}")


def _generate_self_signed_cert(force=False):
    """Генерирует self-signed сертификат для HTTPS (LAN/offline)."""
    if not force and CERT_FILE.exists() and KEY_FILE.exists():
        if _cert_covers_current_names():
            return True
        print("HTTPS: адреса изменились, перегенерирую сертификат...")
    san_ips = list(set(["127.0.0.1"] + get_all_local_ips()))
    # mDNS-имя в SAN — чтобы PWA можно было поставить по стабильному адресу
    # https://<имя>.local:PORT, который переживает смену сети.
    san_dns = ["localhost"]
    host = get_mdns_hostname()
    if host:
        san_dns += [host, host[:-len(".local")]]
    # Try openssl CLI first, then Python fallback
    if _generate_cert_openssl(san_ips, san_dns):
        return True
    if _generate_cert_python(san_ips, san_dns):
        return True
    print("HTTPS: не удалось создать сертификат")
    return False


def _generate_cert_openssl(san_ips, san_dns=("localhost",)):
    """Генерация через openssl CLI."""
    try:
        san_entries = ",".join(["IP:" + ip for ip in san_ips] + ["DNS:" + d for d in san_dns])
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
            "-days", "3650", "-nodes",
            "-subj", "/CN=insideside-music",
            "-addext", "subjectAltName=" + san_entries
        ], capture_output=True, timeout=10, check=True)
        try: os.chmod(str(KEY_FILE), 0o600)
        except Exception: pass
        try: os.chmod(str(CERT_FILE), 0o600)
        except Exception: pass
        return True
    except Exception as ex:
        return False


def _generate_cert_python(san_ips, san_dns=("localhost",)):
    """Fallback: генерация через Python (без openssl CLI)."""
    try:
        from datetime import datetime, timedelta
        # Try cryptography library first
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            import ipaddress as _ipa

            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "insideside-music")])
            san_list = [x509.DNSName(d) for d in san_dns]
            for ip in san_ips:
                try: san_list.append(x509.IPAddress(_ipa.ip_address(ip)))
                except Exception: pass
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.utcnow())
                .not_valid_after(datetime.utcnow() + timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
                .sign(key, hashes.SHA256())
            )
            KEY_FILE.write_bytes(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
            CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            try: os.chmod(str(KEY_FILE), 0o600)
            except Exception: pass
            try: os.chmod(str(CERT_FILE), 0o600)
            except Exception: pass
            print("HTTPS: сертификат создан (cryptography)")
            return True
        except ImportError:
            pass

        # Minimal fallback: use ssl module to make a basic self-signed cert
        # Python 3.10+ has ssl._create_self_signed_cert, older versions don't
        # Generate via subprocess with python itself
        script = '''
import ssl, socket, struct, hashlib, os, sys, base64
from datetime import datetime, timedelta

# Minimal ASN.1 DER self-signed cert generator
import secrets

# Use built-in ssl to create a temp context — this won't work for generation
# Fall back to printing instructions
print("NEED_CRYPTOGRAPHY")
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
        if "NEED_CRYPTOGRAPHY" in result.stdout:
            print("HTTPS: установите пакет cryptography: python -m pip install cryptography")
            return False
        return False
    except Exception as ex:
        print(f"HTTPS: Python fallback не удался: {ex}")
        return False


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        super().server_bind()


_server = None          # LAN/WAN server (0.0.0.0:SERVER_PORT, HTTPS) — only when public
_server_thread = None
_server_lock = threading.Lock()
_local_server = None    # localhost server (127.0.0.1:LOCAL_PORT, plain HTTP) — always on


def get_all_local_ips():
    """Возвращает список всех локальных IP-адресов."""
    ips = []
    try:
        import subprocess
        out = subprocess.check_output(["ifconfig"], stderr=subprocess.DEVNULL).decode()
        for line in out.split('\n'):
            line = line.strip()
            if line.startswith('inet ') and '127.0.0.1' not in line:
                parts = line.split()
                if len(parts) >= 2:
                    ips.append(parts[1])
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips or ["127.0.0.1"]


def get_local_ip():
    ips = get_all_local_ips()
    # Prefer 192.168.x.x (WiFi) over 10.x.x.x (VPN/other)
    for ip in ips:
        if ip.startswith("192.168."):
            return ip
    return ips[0]


_mdns_name_cache = None      # (timestamp, "<имя>.local" | None)
_mdns_resolve_cache = None    # (timestamp, host, host если резолвится иначе None)
_MDNS_TTL = 300


def get_mdns_hostname():
    """Стабильное mDNS-имя ЭТОЙ машины (например MacBook-M4-Pro.local).

    Имя определяется на сервере в рантайме, ничего не захардкожено: на другой
    машине подставится её собственное имя, на Linux/Windows — из
    socket.gethostname().

    В отличие от LAN-IP оно не меняется при переходе в другую сеть, поэтому
    установленная по этому адресу PWA переживает смену Wi-Fi: origin остаётся
    прежним, а вместе с ним — Service Worker и весь офлайн-кэш треков.
    Возвращает None, если имя нельзя превратить в валидную DNS-метку.
    """
    global _mdns_name_cache
    now = time.time()
    if _mdns_name_cache and now - _mdns_name_cache[0] < _MDNS_TTL:
        return _mdns_name_cache[1]
    name = ""
    if sys.platform == "darwin":
        try:
            name = subprocess.check_output(
                ["scutil", "--get", "LocalHostName"],
                stderr=subprocess.DEVNULL, timeout=5
            ).decode().strip()
        except Exception:
            name = ""
    if not name:
        try:
            name = socket.gethostname().split(".")[0].strip()
        except Exception:
            name = ""
    host = name + ".local" if re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$", name or "") else None
    _mdns_name_cache = (now, host)
    return host


def get_lan_host_url():
    """URL по стабильному mDNS-имени — рекомендуемый адрес для установки PWA.

    Отдаём его только если сервер сам может разрезолвить это имя: суффикс
    .local работает лишь пока в системе крутится mDNS-ответчик (mDNSResponder
    в macOS — всегда, avahi в Linux и Bonjour в Windows — не всегда). Без него
    адрес никуда не ведёт, и рекомендовать его в UI было бы обманом: клиент
    просто получит NXDOMAIN. В сертификат имя при этом кладётся всегда, чтобы
    адрес заработал сразу, если ответчик появится позже.
    """
    global _mdns_resolve_cache
    host = get_mdns_hostname()
    if not host:
        return None
    now = time.time()
    if not (_mdns_resolve_cache and now - _mdns_resolve_cache[0] < _MDNS_TTL
            and _mdns_resolve_cache[1] == host):
        resolvable = None
        try:
            socket.getaddrinfo(host, None)
            resolvable = host
        except Exception:
            resolvable = None
        _mdns_resolve_cache = (now, host, resolvable)
    if _mdns_resolve_cache[2] != host:
        return None
    proto = "https" if _use_https else "http"
    return "{}://{}:{}".format(proto, host, SERVER_PORT)


def _start_local_server():
    """Always-on plain-HTTP server on 127.0.0.1:LOCAL_PORT. This is the Mac/widget
    entry point — its port and scheme never change, so toggling LAN/WAN can't
    break the local Dock app (and Safari can't pin it to HTTPS)."""
    global _local_server
    with _server_lock:
        if _local_server:
            return
        _local_server = ReusableHTTPServer(("127.0.0.1", LOCAL_PORT), Handler)
    threading.Thread(target=_local_server.serve_forever, daemon=True).start()


def _start_server(bind_addr):
    """Запускает LAN/WAN HTTP(S)-сервер на 0.0.0.0:SERVER_PORT в фоновом потоке."""
    global _server, _server_thread, _use_https
    with _server_lock:
        if _server:
            try:
                _server.shutdown()
                _server.server_close()
            except Exception:
                pass
            _server = None
        time.sleep(0.5)  # дать порту освободиться
        srv = ReusableHTTPServer((bind_addr, SERVER_PORT), Handler)
        # Wrap with SSL if HTTPS enabled, public mode, and cert exists
        if _use_https and bind_addr == "0.0.0.0" and CERT_FILE.exists() and KEY_FILE.exists():
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        _server = srv
    _server_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    _server_thread.start()


def _stop_server():
    """Stop the LAN/WAN server (called when LAN is turned off). The always-on
    local server keeps running, so the Mac app is never interrupted."""
    global _server
    with _server_lock:
        if _server:
            try:
                _server.shutdown()
                _server.server_close()
            except Exception:
                pass
            _server = None


def _restart_server(bind_addr):
    """Перезапускает LAN/WAN-сервер на новом адресе."""
    try:
        _start_server(bind_addr)
    except Exception as ex:
        print("ОШИБКА перезапуска сервера: {}".format(ex))
        # Fallback: try to start without HTTPS
        try:
            global _use_https
            _use_https = False
            _start_server(bind_addr)
            print("Сервер запущен без HTTPS (fallback)")
        except Exception as ex2:
            print("КРИТИЧЕСКАЯ ОШИБКА: сервер не запустился: {}".format(ex2))


_shutting_down = False

def _shutdown_cleanup():
    """Tear down the WAN tunnel before the process exits (a stale cloudflared
    must not linger). LAN is left enabled so it auto-restores next launch and
    phones reconnect without re-adding the icon / re-downloading their cache —
    the local Mac app is unaffected since it lives on its own always-HTTP port.
    Runs on Ctrl+C and on SIGTERM (how the desktop widget stops the app)."""
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    print("\nЗавершение: останавливаю WAN-туннель...")
    try:
        stop_tunnel()  # stops cloudflared + clears saved WAN config
    except Exception:
        pass


def _on_exit_signal(signum, frame):
    _shutdown_cleanup()
    os._exit(0)


def main():
    global IS_PUBLIC, _use_https
    _load_sessions()
    public = "--public" in sys.argv
    # --local forces a clean localhost-only HTTP instance (no LAN/WAN, no
    # self-signed cert). Used by the desktop widget so the player just works
    # on the same machine without certificate friction. Saved settings are
    # left untouched — LAN/WAN can still be toggled from the running app.
    local = "--local" in sys.argv
    # --no-browser starts the server without auto-opening a browser tab. Used by
    # the desktop widget toggle: the player is opened separately (e.g. from the
    # Safari "Add to Dock" web app), so the widget shouldn't pop a tab.
    no_browser = "--no-browser" in sys.argv
    IS_PUBLIC = public
    bind_addr = "0.0.0.0" if public else "127.0.0.1"

    s = load_settings()
    if not local:
        # Auto-restore saved LAN mode
        if s.get("lan"):
            IS_PUBLIC = True
            bind_addr = "0.0.0.0"
            print("Restoring LAN mode")

        # Auto-restore saved WAN static IP config
        if s.get("wan_mode") == "static" and s.get("wan_ip"):
            IS_PUBLIC = True
            bind_addr = "0.0.0.0"
            print("Restoring WAN static: http://{}:{}".format(s["wan_ip"], s.get("wan_port", SERVER_PORT)))
    else:
        IS_PUBLIC = False
        bind_addr = "127.0.0.1"
        print("Local mode: 127.0.0.1 HTTP only (LAN/WAN auto-restore skipped)")

    # Auto-enable HTTPS when public (LAN/WAN) — needed for SW on non-localhost
    if IS_PUBLIC:
        if _generate_self_signed_cert():
            _use_https = True
            s["https"] = True
            save_settings(s)
            print("HTTPS enabled")
        else:
            _use_https = False
            print("HTTPS: не удалось создать сертификат, работаю по HTTP")
    else:
        _use_https = False

    # Always-on local entry point (its own port + plain HTTP — never disrupted by LAN).
    _start_local_server()
    # LAN/WAN server (0.0.0.0:SERVER_PORT, HTTPS) only when public.
    if IS_PUBLIC:
        _start_server("0.0.0.0")

    # Start certificate watchdog for auto-renewal
    if IS_PUBLIC and _use_https:
        _start_cert_watchdog()

    # Apply saved WAN after server starts (skipped in --local mode)
    if not local and s.get("wan_mode") == "static" and s.get("wan_ip"):
        set_wan_static(s["wan_ip"], s.get("wan_port", str(SERVER_PORT)))

    # The local URL is always plain HTTP on LOCAL_PORT. Use the "localhost"
    # hostname (not 127.0.0.1): once LAN serves HTTPS on 127.0.0.1:7656, the
    # browser can pin HTTPS for the whole 127.0.0.1 host and upgrade even the
    # local http port. "localhost" is never served over HTTPS, so it stays HTTP.
    url = "http://localhost:{}".format(LOCAL_PORT)
    proto = "https" if _use_https else "http"
    import base64 as _b64
    _an = _b64.b64decode("aW5zaWRlc2lkZSBtdXNpYw==").decode()
    print(_an + ": " + url)
    if public or IS_PUBLIC:
        local_ip = get_local_ip()
        print("LAN: {}://{}:{}".format(proto, local_ip, SERVER_PORT))
        host_url = get_lan_host_url()
        if host_url:
            print("LAN (стабильный адрес для PWA): " + host_url)
    print("Ctrl+C для остановки")
    if not no_browser:
        webbrowser.open(url)

    # Clean shutdown: disable LAN/WAN first, then exit (catches the widget's
    # pkill/SIGTERM, not just Ctrl+C).
    signal.signal(signal.SIGTERM, _on_exit_signal)
    try:
        signal.signal(signal.SIGINT, _on_exit_signal)
    except Exception:
        pass

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown_cleanup()


if __name__ == "__main__":
    main()
