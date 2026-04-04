#!/usr/bin/env python3
"""
Vinyl Record Music Player
Веб-плеер с визуализацией виниловой пластинки.
Запускается как localhost в браузере.
"""

import base64
import json
import mimetypes
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote, quote

try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, APIC, ID3NoHeaderError
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

from httpx import Client as HttpClient

try:
    from vkpymusic import Service as VkService
    HAS_VK = True
except ImportError:
    HAS_VK = False

import hashlib
import hmac
import secrets
import http.cookies

SERVER_PORT = 7656
_user_music_dirs = {}  # username -> current MUSIC_DIR
USERS_FILE = Path.home() / ".vinyl_users.json"
SETTINGS_FILE = Path.home() / ".vinyl_settings.json"
VK_APP_ID = 2685278
VK_USER_AGENT = "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)"
IS_PUBLIC = False

SUPPORTED_FORMATS = {'.mp3', '.flac', '.m4a', '.ogg', '.wav', '.aac', '.opus'}

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
    return _sessions.get(token)


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
    tracks = []
    for f in Path(folder).glob("*.mp3"):
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
        new_path = old_path.parent / (new_num + ". " + name + ".mp3")
        if old_path != new_path:
            old_path.rename(new_path)


def vk_repad_tracks(folder):
    tracks = vk_get_existing_tracks(folder)
    if not tracks:
        return
    mx = max(t[0] for t in tracks)
    pad = len(str(mx))
    for num, name, old_path in tracks:
        new_path = old_path.parent / (str(num).zfill(pad) + ". " + name + ".mp3")
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


def load_config():
    # Backward compat — now per-user
    return {"folders": [], "last_folder": ""}


def save_config(config):
    pass


def add_folder_to_config(folder):
    pass


def get_metadata(filepath):
    """Извлекает метаданные трека: title, artist, album, cover (base64)."""
    p = Path(filepath)
    meta = {
        "title": p.stem,
        "artist": "",
        "album": "",
        "cover": None,
        "cover_mime": None,
    }
    if not HAS_MUTAGEN:
        return meta

    try:
        ext = p.suffix.lower()
        if ext == '.mp3':
            audio = MP3(filepath)
            tags = audio.tags
            if tags:
                meta["title"] = str(tags.get("TIT2", p.stem))
                meta["artist"] = str(tags.get("TPE1", ""))
                meta["album"] = str(tags.get("TALB", ""))
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
            covr = tags.get("covr")
            if covr:
                meta["cover"] = base64.b64encode(bytes(covr[0])).decode()
                meta["cover_mime"] = "image/jpeg"
        elif ext == '.ogg':
            audio = OggVorbis(filepath)
            meta["title"] = audio.get("title", [p.stem])[0]
            meta["artist"] = audio.get("artist", [""])[0]
            meta["album"] = audio.get("album", [""])[0]
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
            tracks.append({
                "id": len(tracks),
                "file": f.name,
                "title": meta["title"],
                "artist": meta["artist"],
                "album": meta["album"],
                "has_cover": meta["cover"] is not None,
            })
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
    """Записывает метаданные в файл. НЕ перезаписывает существующие поля (если overwrite=False)."""
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
            if meta.get('album'):
                audio.tags['\xa9alb'] = [meta['album']]
            if meta.get('year') and (overwrite or not audio.tags.get('\xa9day')):
                audio.tags['\xa9day'] = [meta['year']]
            if cover_data and (overwrite or not audio.tags.get('covr')):
                audio.tags['covr'] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            return True

    except Exception:
        pass
    return False


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
        if existing.get("artist") and existing.get("album") and existing.get("cover"):
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
      return Promise.all(names.filter(function(n) {
        return n.startsWith('app-') && n !== CACHE_APP;
      }).map(function(n) { return caches.delete(n); }));
    }).then(function() { return self.clients.claim(); })
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

var OFFLINE_FALLBACK = '<html><body style="background:#111;color:#eee;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0"><div style="text-align:center"><h2>Offline</h2><p>Server unavailable, no cached page.</p></div></body></html>';

self.addEventListener('fetch', function(e) {
  var url = new URL(e.request.url);

  // Audio streams and covers — do NOT intercept, let browser handle directly.
  // Offline playback is handled client-side via IndexedDB blob URLs.
  // Intercepting audio breaks iOS PWA standalone mode (Range request issues).
  if (url.pathname.startsWith('/api/stream/') || url.pathname.startsWith('/api/cover/')) {
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
<link rel="apple-touch-icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxODAgMTgwIj4KPHJlY3Qgd2lkdGg9IjE4MCIgaGVpZ2h0PSIxODAiIHJ4PSI0MCIgZmlsbD0iIzFhMWEyZSIvPgo8Y2lyY2xlIGN4PSI5MCIgY3k9IjkwIiByPSI2OCIgZmlsbD0iIzExMSIgc3Ryb2tlPSIjMzMzIiBzdHJva2Utd2lkdGg9IjEuNSIvPgo8Y2lyY2xlIGN4PSI5MCIgY3k9IjkwIiByPSI1NSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMjIyIiBzdHJva2Utd2lkdGg9IjAuNSIvPgo8Y2lyY2xlIGN4PSI5MCIgY3k9IjkwIiByPSI0MiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMjIyIiBzdHJva2Utd2lkdGg9IjAuNSIvPgo8Y2lyY2xlIGN4PSI5MCIgY3k9IjkwIiByPSIyMiIgZmlsbD0iI2U5NDU2MCIvPgo8Y2lyY2xlIGN4PSI5MCIgY3k9IjkwIiByPSI0IiBmaWxsPSIjMWExYTJlIi8+Cjwvc3ZnPg==">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxODAgMTgwIj4KPHJlY3Qgd2lkdGg9IjE4MCIgaGVpZ2h0PSIxODAiIHJ4PSI0MCIgZmlsbD0iIzFhMWEyZSIvPgo8Y2lyY2xlIGN4PSI5MCIgY3k9IjkwIiByPSI2OCIgZmlsbD0iIzExMSIgc3Ryb2tlPSIjMzMzIiBzdHJva2Utd2lkdGg9IjEuNSIvPgo8Y2lyY2xlIGN4PSI5MCIgY3k9IjkwIiByPSI1NSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMjIyIiBzdHJva2Utd2lkdGg9IjAuNSIvPgo8Y2lyY2xlIGN4PSI5MCIgY3k9IjkwIiByPSI0MiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMjIyIiBzdHJva2Utd2lkdGg9IjAuNSIvPgo8Y2lyY2xlIGN4PSI5MCIgY3k9IjkwIiByPSIyMiIgZmlsbD0iI2U5NDU2MCIvPgo8Y2lyY2xlIGN4PSI5MCIgY3k9IjkwIiByPSI0IiBmaWxsPSIjMWExYTJlIi8+Cjwvc3ZnPg==">
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
  background: rgba(0,0,0,0.35); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  border-radius: 10px; padding: 3px;
  border: 1px solid rgba(255,255,255,0.06);
}
.player-mode-btn {
  width: 32px; height: 32px; border: none; border-radius: 6px; background: none;
  color: rgba(255,255,255,0.3); cursor: pointer; display: flex; align-items: center; justify-content: center;
  transition: all 0.2s;
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
  position: absolute; top: 4%; left: 5%; right: 5%; height: 28%;
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
}
.progress-bg {
  width: 100%; height: 4px; background: rgba(255,255,255,0.15);
  border-radius: 2px; position: relative;
}
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
.playlist-item .info .name { font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.playlist-item .info .artist { font-size: 12px; color: rgba(255,255,255,0.4); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
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
  position: fixed; top: 20px; left: 50%; transform: translateX(-50%) translateY(-80px);
  background: rgba(30,30,50,0.95); color: #eee; padding: 12px 24px; border-radius: 12px;
  font-size: 14px; z-index: 200; transition: transform 0.3s ease; pointer-events: none;
  backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,0.1);
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
  .ipod-body { width: min(65vw, 45vh); min-width: 180px; }
  .cassette-body { --cw: min(90vw, 50vh); min-width: 260px; }
  .player-mode-toggle { top: 8px; right: 8px; }
  .player-mode-btn { width: 28px; height: 28px; }
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
.loading-spinner { width:28px;height:28px;border:3px solid rgba(255,255,255,0.1);border-top-color:#e94560;border-radius:50%;animation:lspin .7s linear infinite; }
@keyframes lspin { to { transform:rotate(360deg); } }
</style>
</head>
<body>
<div class="bg-canvas" id="bgCanvas"><canvas id="bgC"></canvas></div>
<div class="app">
  <!-- Left: Vinyl -->
  <div class="vinyl-side">
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
      <div class="track-title" id="trackTitle" style="opacity:0.3" data-idle="1"></div>
      <div class="track-artist"><span class="artist-link" id="trackArtist" onclick="searchArtist(this.textContent)" style="opacity:0.3">Выберите трек</span></div>
    </div>

    <div class="controls">
      <button class="ctrl-btn" onclick="prevTrack()"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zm12 0v12l-8.5-6z"/></svg></button>
      <button class="ctrl-btn play-btn" id="playBtn" onclick="togglePlay()"><svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor" id="playIcon"><path d="M8 5v14l11-7z"/></svg></button>
      <button class="ctrl-btn" onclick="nextTrack()"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg></button>
    </div>

    <div class="progress-wrap" onclick="seek(event)">
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
        <button class="folder-btn folder-btn-secondary" style="flex:1" onclick="startMetaSearch()" data-tip="Поиск обложек, артистов и альбомов">Meta</button>
        <button class="folder-btn folder-btn-secondary" style="flex:1" onclick="openVkModal()" data-tip="Импорт из VK, Яндекс, Spotify, Apple Music, SoundCloud">Загрузить</button>
        <div id="networkToggles" style="display:none;align-items:center;gap:4px;flex-shrink:0">
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

      <div id="lanInfo" style="font-size:11px;color:rgba(255,255,255,0.4);display:none"></div>

      <div class="search-wrap" style="position:relative">
        <input type="text" id="searchInput" class="folder-path-input" style="width:100%" placeholder="Поиск по трекам..." oninput="onSearchInput(this.value)">
        <button class="search-clear" id="searchClear" onclick="clearSearch()">&times;</button>
      </div>
    </div>

    <div class="playlist-tabs">
      <button class="playlist-tab active" id="tabTracks" onclick="showTab('tracks')">Треки</button>
      <button class="playlist-tab" id="tabAlbums" onclick="showTab('albums')">Альбомы</button>
      <button class="playlist-tab" id="tabPlaylists" onclick="showTab('playlists')">Плейлисты</button>
    </div>

    <div class="playlist-header" style="display:flex;align-items:center;gap:8px">
      <button class="shuffle-btn" id="downloadCatalogBtn" onclick="downloadCatalog()" data-tip="Скачать ZIP-архив" style="display:none"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M20 6h-8l-2-2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm-2 10h-3v3h-2v-3H9l5-5 5 5z"/></svg></button>
      <button class="shuffle-btn" id="cacheBtn" onclick="startCacheAll()" data-tip="Кэшировать для офлайн"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg></button>
      <button class="shuffle-btn" id="cachedOnlyBtn" onclick="toggleCachedOnly()" data-tip="Только кэш"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg></button>
      <span id="playlistHeader" style="flex:1;cursor:pointer" onclick="scrollTracklistTop()">0 треков</span>
      <button class="shuffle-btn" id="shuffleListBtn" onclick="toggleShuffleFromList()" data-tip="Перемешать"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/></svg></button>
      <button class="shuffle-btn" id="editBtn" onclick="startEdit()" data-tip="Редактировать порядок" style="display:none"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 000-1.41l-2.34-2.34a1 1 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg></button>
      <div id="editControls" style="display:none;gap:4px">
        <button class="shuffle-btn" onclick="saveEdit()" data-tip="Сохранить" style="color:#52b788"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg></button>
        <button class="shuffle-btn" onclick="cancelEdit()" data-tip="Отмена" style="color:#e94560"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg></button>
      </div>
    </div>

    <div class="tab-slider">
      <div class="tab-slider-inner" id="tabSlider">
        <div class="playlist-list tab-panel-visible" id="trackList"></div>
        <div class="coverflow-wrap tab-panel-hidden" id="albumList"></div>
        <div class="coverflow-wrap tab-panel-hidden" id="playlistsList"></div>
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
      <button class="folder-btn folder-btn-primary" style="flex:1" onclick="confirmPlAdd('end')">В конец</button>
    </div>
  </div>
</div>

<!-- Track edit modal -->
<div class="meta-overlay" id="trackEditOverlay" onmousedown="this._mdt=event.target" onclick="if(event.target===this&&this._mdt===this)this.classList.remove('show')">
  <div class="meta-modal" style="width:min(400px,90vw)">
    <h3>Редактирование трека</h3>
    <div style="font-size:11px;color:rgba(255,255,255,0.3);margin-bottom:10px" id="trackEditFile"></div>
    <label style="font-size:12px;color:rgba(255,255,255,0.4);display:block;margin-bottom:4px">Название</label>
    <input type="text" id="trackEditTitle" class="folder-path-input" style="margin-bottom:8px">
    <label style="font-size:12px;color:rgba(255,255,255,0.4);display:block;margin-bottom:4px">Артист</label>
    <input type="text" id="trackEditArtist" class="folder-path-input" style="margin-bottom:8px">
    <div id="trackEditOrderRow" style="margin-bottom:8px">
      <label style="font-size:12px;color:rgba(255,255,255,0.4);display:block;margin-bottom:4px">Позиция в каталоге <span id="trackEditOrderHint" style="color:rgba(255,255,255,0.2)"></span></label>
      <input type="number" id="trackEditOrder" class="folder-path-input" min="1" style="width:120px" placeholder="—">
    </div>
    <label style="display:flex;align-items:center;gap:6px;color:rgba(255,255,255,0.5);cursor:pointer;font-size:12px;margin-bottom:12px">
      <input type="checkbox" id="trackEditMeta" style="accent-color:#e94560"> Найти Meta-данные после сохранения
    </label>
    <div id="trackEditCacheRow" style="display:none;align-items:center;gap:8px;margin-bottom:12px;padding:8px 10px;background:rgba(255,255,255,0.04);border-radius:8px">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="#52b788"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
      <span style="font-size:12px;color:rgba(255,255,255,0.5);flex:1">Закэшировано</span>
      <button class="folder-btn folder-btn-secondary" style="padding:4px 10px;font-size:11px" onclick="uncacheEditTrack()">Удалить из кэша</button>
    </div>
    <div style="display:flex;gap:6px">
      <button class="folder-btn folder-btn-primary" style="flex:1" onclick="saveTrackEdit()">Сохранить</button>
      <button class="folder-btn folder-btn-secondary" style="flex:1" onclick="document.getElementById('trackEditOverlay').classList.remove('show')">Отмена</button>
      <button class="folder-btn folder-btn-secondary" style="color:#e94560;flex-shrink:0;padding:8px 12px" onclick="deleteEditTrack()" data-tip="Удалить трек">&#10005;</button>
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
    <div style="font-size:12px;color:rgba(255,255,255,0.4);margin-bottom:8px">Сменить пароль</div>
    <div class="pw-field"><input type="password" id="profOldPw" placeholder="Текущий пароль"><button class="pw-eye" onclick="togglePwVis('profOldPw',this)"><img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIGZpbGw9IiM4ODgiIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyIDQuNUM3IDQuNSAyLjczIDcuNjEgMSAxMmMxLjczIDQuMzkgNiA3LjUgMTEgNy41czkuMjctMy4xMSAxMS03LjVjLTEuNzMtNC4zOS02LTcuNS0xMS03LjV6TTEyIDE3Yy0yLjc2IDAtNS0yLjI0LTUtNXMyLjI0LTUgNS01IDUgMi4yNCA1IDUtMi4yNCA1LTUgNXptMC04Yy0xLjY2IDAtMyAxLjM0LTMgM3MxLjM0IDMgMyAzIDMtMS4zNCAzLTMtMS4zNC0zLTMtM3oiLz48L3N2Zz4="></button></div>
    <div class="pw-field" style="margin-top:6px"><input type="password" id="profNewPw" placeholder="Новый пароль"><button class="pw-eye" onclick="togglePwVis('profNewPw',this)"><img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIGZpbGw9IiM4ODgiIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyIDQuNUM3IDQuNSAyLjczIDcuNjEgMSAxMmMxLjczIDQuMzkgNiA3LjUgMTEgNy41czkuMjctMy4xMSAxMS03LjVjLTEuNzMtNC4zOS02LTcuNS0xMS03LjV6TTEyIDE3Yy0yLjc2IDAtNS0yLjI0LTUtNXMyLjI0LTUgNS01IDUgMi4yNCA1IDUtMi4yNCA1LTUgNXptMC04Yy0xLjY2IDAtMyAxLjM0LTMgM3MxLjM0IDMgMyAzIDMtMS4zNCAzLTMtMS4zNC0zLTMtM3oiLz48L3N2Zz4="></button></div>
    <button class="folder-btn folder-btn-primary" style="width:100%;margin-top:12px" onclick="changeMyPassword()">Сменить пароль</button>
    <div style="margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.06)">
      <div style="font-size:12px;color:rgba(255,255,255,0.4);margin-bottom:8px">Офлайн-кэш</div>
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:12px;color:rgba(255,255,255,0.5);flex:1" id="profileCacheInfo"></span>
        <button class="folder-btn folder-btn-secondary" style="padding:6px 12px;font-size:12px;color:#e94560" onclick="clearAllCache()">Очистить кэш</button>
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

<!-- Track context menu -->
<div class="ctx-menu" id="ctxMenu">
  <div class="ctx-item" onclick="ctxPlayNext()"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"/></svg> Играть следующим</div>
  <div class="ctx-item" id="ctxCacheItem" onclick="ctxToggleCache()"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg> <span id="ctxCacheLabel">Кэшировать</span></div>
  <div class="ctx-sep"></div>
  <div class="ctx-sub-header">Добавить в плейлист</div>
  <div class="ctx-sub" id="ctxPlaylists"></div>
  <div class="ctx-sep"></div>
  <div class="ctx-item danger" onclick="ctxDelete()"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg> Удалить</div>
</div>

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
  if (t.has_cover) {
    ic.src = '/api/cover/' + encodeURIComponent(t.file);
    ic.style.display = ''; icp.style.display = 'none';
  } else { ic.style.display = 'none'; icp.style.display = ''; }
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
var scratchGain = null;
var scratchNoise = null;
var scratchFilter = null;
var isScratchPlaying = false;

function initScratchSound() {
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
}

function startScratch(speed) {
  if (!audioCtx) initScratchSound();
  var vol = Math.min(Math.abs(speed) * 0.15, 0.35);
  scratchFilter.frequency.value = 600 + Math.abs(speed) * 200;
  scratchGain.gain.setTargetAtTime(vol, audioCtx.currentTime, 0.02);
  isScratchPlaying = true;
}

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
function animationLoop(ts) {
  var dt = lastTime ? (ts - lastTime) / 1000 : 0;
  lastTime = ts;

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

  // Tonearm follows track progress with smooth lerp
  var targetArm = ARM_REST;
  if (isPlaying && (!audio.duration || isNaN(audio.duration))) {
    // Track started but duration not loaded yet — move to start position
    targetArm = ARM_START;
  } else if (audio.duration && !isNaN(audio.duration) && (isPlaying || audio.currentTime > 0)) {
    var pct = audio.currentTime / audio.duration;
    targetArm = ARM_START + (ARM_END - ARM_START) * pct;
  }
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
  if (audio.duration && !isDragging) {
    var pctBar = audio.currentTime / audio.duration * 100;
    document.getElementById('progressFill').style.width = pctBar + '%';
    document.getElementById('timeCurrent').textContent = formatTime(audio.currentTime);
    // iPod progress sync
    if (_playerMode === 'ipod') {
      document.getElementById('ipodProgress').style.width = pctBar + '%';
      document.getElementById('ipodTimeCur').textContent = formatTime(audio.currentTime);
      document.getElementById('ipodTimeDur').textContent = formatTime(audio.duration);
    }
  }

  requestAnimationFrame(animationLoop);
}
requestAnimationFrame(animationLoop);

audio.addEventListener('loadedmetadata', function() {
  document.getElementById('timeDuration').textContent = formatTime(audio.duration);
});
var _trackSrcGen = 0; // incremented on each src change to detect stale ended events

audio.addEventListener('ended', function() {
  var gen = _trackSrcGen;
  // Ignore stale 'ended' from previous src (race when switching near end of track)
  setTimeout(function() {
    if (_trackSrcGen !== gen) return; // src changed since ended fired — stale event
    nextTrack();
  }, 0);
});

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
  var newTime = Math.max(0, Math.min(audio.currentTime + timeDelta, audio.duration - 0.1));
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
  var newTime = Math.max(0, Math.min(audio.currentTime + timeDelta, audio.duration - 0.1));
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
  newTime = Math.max(0, Math.min(newTime, audio.duration - 0.1));
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
    gain.gain.setValueAtTime(0.03, t);
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
      // Volume control in Now Playing
      var volDelta = delta / 360; // full rotation = full volume range
      audio.volume = Math.max(0, Math.min(1, audio.volume + volDelta));
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
        ipodClick();
        if (_ipodListMode) {
          if (_ipodSelectedIdx >= 0 && _ipodSelectedIdx < tracks.length) {
            playFromList(_ipodSelectedIdx);
            _ipodShowNp();
          }
        } else { togglePlay(); }
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
        } else { togglePlay(); }
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
        } else { togglePlay(); }
      }
    });
  }

  if (center) {
    center.addEventListener('click', function() {
      if (_playerMode !== 'ipod') return;
      if (_ipodWheelMoved) { _ipodWheelMoved = false; return; }
      ipodClick();
      if (_ipodListMode) {
        if (_ipodSelectedIdx >= 0 && _ipodSelectedIdx < tracks.length) {
          playFromList(_ipodSelectedIdx);
          _ipodShowNp();
        }
      } else { togglePlay(); }
    });
  }
})();

function formatTime(s) {
  var m = Math.floor(s / 60);
  var sec = Math.floor(s % 60);
  return m + ':' + (sec < 10 ? '0' : '') + sec;
}

function renderTracks() {
  var html = '';
  var indices = getVisibleIndices();
  for (var ii = 0; ii < indices.length; ii++) {
    var i = indices[ii];
    var t = tracks[i];
    var coverHtml = t.has_cover
      ? '<img src="/api/cover/' + encodeURIComponent(t.file) + '" loading="lazy" onerror="loadCachedImg(this,\'' + encodeURIComponent(t.file).replace(/'/g,"\\'") + '\')">'
      : '';
    if (isEditMode) {
      html += '<div class="playlist-item' + (i === currentIdx ? ' active' : '') + '" data-idx="' + i + '"'
        + ' draggable="true" ondragstart="onDragStart(event,' + i + ')" ondragend="onDragEnd(event)"'
        + ' ondragover="onDragOver(event,' + i + ')" ondrop="onDrop(event,' + i + ')"'
        + ' ontouchstart="onTouchDragStart(event,' + i + ')">'
        + '<span class="drag-handle">&#9776;</span>'
        + '<div class="cover-thumb">' + coverHtml + '</div>'
        + '<div class="info"><div class="name">' + esc(t.title) + '</div>'
        + '<div class="artist">' + esc(t.artist) + '</div></div></div>';
    } else {
      var offDisabled = _isOffline && !isTrackCached(t.file);
      html += '<div class="playlist-item' + (i === currentIdx ? ' active' : '') + (offDisabled ? ' disabled' : '') + '"'
        + (offDisabled ? '' : ' onclick="playFromList(' + i + ')"')
        + (offDisabled ? ' style="opacity:0.3;pointer-events:none"' : '')
        + ' oncontextmenu="event.preventDefault();showCtxMenu(event,' + i + ')"'
        + ' data-longpress="' + i + '">'
        + '<div class="cover-thumb">' + coverHtml + '</div>'
        + '<div class="info"><div class="name">' + esc(t.title) + '</div>'
        + '<div class="artist">' + esc(t.artist) + '</div></div>'
        + (isTrackCached(t.file)
          ? '<span style="width:6px;height:6px;border-radius:50%;background:#52b788;flex-shrink:0" data-tip="В кэше"></span>'
          : (!offDisabled ? '<button class="track-edit-btn" onclick="event.stopPropagation();cacheTrack(\'' + esc(t.file).replace(/'/g,"\\'") + '\',function(ok){if(ok)renderTracks()})" data-tip="Кэшировать"><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg></button>' : ''))
        + (!offDisabled && userRole !== 'demo' ? '<button class="track-edit-btn" onclick="event.stopPropagation();openTrackEdit(' + i + ')" data-tip="Редактировать"><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 000-1.41l-2.34-2.34a1 1 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg></button>' : '')
        + '</div>';
    }
  }
  document.getElementById('trackList').innerHTML = html;
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
      ? '<img src="/api/cover/' + encodeURIComponent(alb.cover_file) + '" loading="lazy" onerror="loadCachedImg(this,\'' + encodeURIComponent(alb.cover_file).replace(/'/g,"\\'") + '\')">'
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

function showTab(tab) {
  activeTab = tab;
  var tabs = ['tracks', 'albums', 'playlists'];
  var panels = {tracks: 'trackList', albums: 'albumList', playlists: 'playlistsList'};
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
  if (tab === 'albums') {
    document.getElementById('playlistHeader').textContent = (filteredAlbums ? filteredAlbums.length + ' / ' : '') + albums.length + ' альбомов';
    document.getElementById('editBtn').style.display = 'none';
    if (isEditMode) cancelEdit();
  } else if (tab === 'playlists') {
    document.getElementById('editBtn').style.display = 'none';
    if (isEditMode) cancelEdit();
    loadUserPlaylists();
  } else {
    document.getElementById('playlistHeader').textContent = (filteredTracks ? filteredTracks.length + ' / ' : '') + tracks.length + ' треков';
    document.getElementById('editBtn').style.display = (isNumberedCatalog && userRole !== 'demo') ? '' : 'none';
  }
}

// ── Blob URL pre-cache (keeps ready-to-use blob URLs in memory for instant playback) ──
var _blobUrlCache = {}; // file -> blob URL

function makeBlobUrl(buf, file) {
  var ext = file.split('.').pop().toLowerCase();
  var mimeMap = {mp3:'audio/mpeg',flac:'audio/flac',m4a:'audio/mp4',ogg:'audio/ogg',wav:'audio/wav',aac:'audio/aac',opus:'audio/ogg'};
  return URL.createObjectURL(new Blob([buf], {type: mimeMap[ext] || 'audio/mpeg'}));
}

function prepareBlobUrl(file) {
  if (_blobUrlCache[file] || !isTrackCached(file)) return;
  getCachedAudio(file, function(buf) {
    if (!buf) { delete cachedFiles[file]; return; }
    _blobUrlCache[file] = makeBlobUrl(buf, file);
  });
}

function prepareNearbyBlobs() {
  if (playQueue.length === 0) return;
  var startPos = playQueuePos >= 0 ? playQueuePos : 0;
  // Prepare current + 5 next + 2 prev
  for (var d = 0; d <= 7; d++) {
    var pos = startPos + (d <= 5 ? d : -(d - 5));
    if (pos < 0) pos += playQueue.length;
    if (pos >= playQueue.length) pos -= playQueue.length;
    var idx = playQueue[pos];
    if (idx >= 0 && idx < tracks.length) prepareBlobUrl(tracks[idx].file);
  }
}

function selectTrack(i, autoplay) {
  if (i < 0 || i >= tracks.length) return;
  currentIdx = i;
  _trackSrcGen++;
  var t = tracks[i];

  vinylAngle = 0;
  vinylSpeed = 0;

  audio.pause();
  var streamUrl = '/api/stream/' + encodeURIComponent(t.file);
  if (_blobUrlCache[t.file]) {
    audio.src = _blobUrlCache[t.file];
  } else {
    audio.src = streamUrl;
  }
  if (autoplay) {
    var p = audio.play();
    if (p && p.then) p.then(function() {
      // iOS PWA: detect stuck audio (plays but no sound, time=0)
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
    }).catch(function(err) { console.error('play() failed:', err); showToast('Ошибка воспроизведения: ' + err.message); });
    setPlayState(true);
  }
  if (isTrackCached(t.file)) prepareBlobUrl(t.file);
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
    // Cassette label + cover
    document.getElementById('cassetteTitle').textContent = t.title;
    document.getElementById('cassetteArtist').textContent = t.artist;
    var ccov = document.getElementById('cassetteCover');
    var ccph = document.getElementById('cassetteCoverPh');
    if (t.has_cover) {
      ccov.src = '/api/cover/' + encodeURIComponent(t.file);
      ccov.style.display = ''; ccph.style.display = 'none';
    } else {
      ccov.style.display = 'none'; ccph.style.display = '';
    }
    // iPod sync
    _ipodSyncTrack(t);
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

function togglePlay() {
  if (currentIdx < 0 && tracks.length > 0) {
    playFromList(0);
    return;
  }
  if (isPlaying) {
    audio.pause();
    setPlayState(false);
  } else {
    audio.play();
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
  // Build queue from current visible list respecting cachedOnly filter
  var indices = getVisibleIndices();
  playQueue = indices.slice();
  playQueuePos = playQueue.indexOf(trackIdx);
  if (playQueuePos < 0) playQueuePos = 0;
  selectTrack(trackIdx, true);
}

function playFromAlbum(albumIdx, trackIdx) {
  // Build queue from album tracks
  var alb = albums[albumIdx];
  if (!alb) return;
  playQueue = alb.tracks.slice();
  playQueuePos = playQueue.indexOf(trackIdx);
  if (playQueuePos < 0) playQueuePos = 0;
  selectTrack(trackIdx, true);
}

function prevTrack() {
  if (audio.currentTime > 3) { audio.currentTime = 0; return; }
  if (playQueue.length > 0) {
    playQueuePos--;
    if (playQueuePos < 0) playQueuePos = playQueue.length - 1;
    selectTrack(playQueue[playQueuePos], isPlaying);
  }
}

function nextTrack() {
  if (_forceNextIdx >= 0) {
    var idx = _forceNextIdx;
    _forceNextIdx = -1;
    // Move queue position to this track so next continues from there
    var qPos = playQueue.indexOf(idx);
    if (qPos >= 0) playQueuePos = qPos;
    selectTrack(idx, isPlaying);
    return;
  }
  if (playQueue.length > 0) {
    playQueuePos++;
    if (playQueuePos >= playQueue.length) playQueuePos = 0;
    selectTrack(playQueue[playQueuePos], isPlaying);
  }
}

function seek(e) {
  if (!audio.duration) return;
  var rect = e.currentTarget.getBoundingClientRect();
  var pct = (e.clientX - rect.left) / rect.width;
  audio.currentTime = pct * audio.duration;
}

function setVolume(v) { audio.volume = v; }

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
  bgCvs.width = Math.floor(window.innerWidth / 2);
  bgCvs.height = Math.floor(window.innerHeight / 2);
}

var _bgLastFrame = 0;

function drawBg(t) {
  requestAnimationFrame(drawBg);
  if (!bgCtx) return;
  // Throttle to ~30fps (33ms) and skip when page is hidden
  if (t - _bgLastFrame < 33 || document.hidden) return;
  _bgLastFrame = t;

  var w = bgCvs.width, h = bgCvs.height;
  var s = t * 0.0002;

  // Lerp base color
  bgBaseR += (bgTargetR - bgBaseR) * 0.04;
  bgBaseG += (bgTargetG - bgBaseG) * 0.04;
  bgBaseB += (bgTargetB - bgBaseB) * 0.04;

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
function updateMediaSession(t) {
  if (!('mediaSession' in navigator)) return;
  var artwork = [];
  if (t.has_cover) {
    artwork.push({
      src: '/api/cover/' + encodeURIComponent(t.file),
      sizes: '512x512',
      type: 'image/jpeg'
    });
  }
  navigator.mediaSession.metadata = new MediaMetadata({
    title: t.title || '',
    artist: t.artist || '',
    album: t.album || '',
    artwork: artwork
  });
}

function initMediaSession() {
  if (!('mediaSession' in navigator)) return;
  navigator.mediaSession.setActionHandler('play', function() {
    if (currentIdx < 0 && tracks.length > 0) selectTrack(0, true);
    else { audio.play(); setPlayState(true); }
  });
  navigator.mediaSession.setActionHandler('pause', function() {
    audio.pause(); setPlayState(false);
  });
  navigator.mediaSession.setActionHandler('previoustrack', function() { prevTrack(); });
  navigator.mediaSession.setActionHandler('nexttrack', function() { nextTrack(); });
  navigator.mediaSession.setActionHandler('seekto', function(d) {
    if (d.seekTime !== undefined && audio.duration) audio.currentTime = d.seekTime;
  });
  // iOS: override seek buttons to act as prev/next
  try { navigator.mediaSession.setActionHandler('seekbackward', function() { prevTrack(); }); } catch(e) {}
  try { navigator.mediaSession.setActionHandler('seekforward', function() { nextTrack(); }); } catch(e) {}
}

// Update position state for lock screen progress bar
function onTimeUpdate() {
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
audio.addEventListener('timeupdate', onTimeUpdate);

// ── Config / Folders ──
var currentUser = '';
var isAdmin = false;
var userRole = 'user';

var _isOffline = false;

function applyConfig(cfg) {
  currentUser = cfg.username || '';
  isAdmin = cfg.is_admin || false;
  userRole = cfg.role || 'user';
  savedFolders = cfg.folders || [];
  renderFolderSelect();
  if (!_isOffline) syncNetworkState();
  var isDemo = userRole === 'demo';
  var isLocal = cfg.is_local !== false; // true if server says client is local, default true for cached
  document.getElementById('adminBtn').style.display = isAdmin ? '' : 'none';
  document.getElementById('networkToggles').style.display = (isAdmin && !_isOffline && isLocal) ? 'flex' : 'none';
  document.getElementById('downloadCatalogBtn').style.display = (isAdmin && !_isOffline) ? '' : 'none';
  document.getElementById('metaVkRow').style.display = (isDemo || _isOffline) ? 'none' : '';
  document.getElementById('addFolderBtn').style.display = (isDemo || _isOffline) ? 'none' : '';
  document.getElementById('removeFolderBtn').style.display = (isDemo || _isOffline) ? 'none' : '';
}

function showOfflineBanner(show) {
  var banner = document.getElementById('offlineBanner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'offlineBanner';
    banner.style.cssText = 'position:fixed;bottom:72px;left:50%;transform:translateX(-50%);z-index:9999;background:rgba(30,30,30,0.85);color:rgba(255,255,255,0.6);text-align:center;padding:6px 16px;font-size:11px;border-radius:20px;pointer-events:none;transition:opacity .3s;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,0.08);';
    banner.textContent = 'Офлайн — только кэшированные треки';
    document.body.appendChild(banner);
  }
  banner.style.opacity = show ? '1' : '0';
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
      if ('caches' in window) caches.keys().then(function(n){n.forEach(function(k){caches.delete(k)})});
      window.location.reload();
      return;
    }
    if (cfg.error === 'offline') { if (!hadCache) enterOfflineMode(); return; }
    _isOffline = false;
    showOfflineBanner(false);
    try { localStorage.setItem('_vc_config', JSON.stringify(cfg)); } catch(e){}
    applyConfig(cfg);
    if (cfg.last_folder) {
      document.getElementById('folderSelect').value = cfg.last_folder;
      loadFolder(cfg.last_folder);
    }
  }).catch(function() {
    if (!hadCache) enterOfflineMode();
  });
}

function showLoadingIndicator() {
  document.getElementById('trackList').innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 20px;color:rgba(255,255,255,0.3)">'
    + '<div class="loading-spinner"></div><div style="margin-top:12px;font-size:13px">Загрузка...</div></div>';
}

function enterOfflineMode() {
  _isOffline = true;
  showOfflineBanner(true);
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
  renderTracks();
  renderAlbums();
  checkIfNumbered();
  buildDefaultQueue();
  // Prefetch playlists for offline (even if not on playlists tab)
  if (!_isOffline) {
    var folder = document.getElementById('folderSelect').value;
    if (folder) {
      fetch('/api/playlists', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({folder: folder, action: 'list'})})
      .then(function(r){return r.json()}).then(function(d) {
        if (d.playlists) try { localStorage.setItem('_vc_playlists_' + folder, JSON.stringify(d.playlists)); } catch(e){}
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

function loadFolder(path, retries) {
  if (!path) return;
  if (_isOffline) { loadFolderOffline(path); return; }
  if (retries === undefined) retries = 2;
  // Show loading only if no cached data visible
  if (!tracks.length) showLoadingIndicator();
  fetch('/api/scan?path=' + encodeURIComponent(path))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        if (data.error === 'offline') { if (!tracks.length) enterOfflineMode(); return; }
        if (retries > 0) { setTimeout(function(){ loadFolder(path, retries - 1); }, 1000); return; }
        showToast(data.error); return;
      }
      try { localStorage.setItem('_vc_folder_' + path, JSON.stringify(data)); } catch(e){}
      applyFolderData(data);
    })
    .catch(function() {
      if (retries > 0) { setTimeout(function(){ loadFolder(path, retries - 1); }, 1000); }
      else if (!tracks.length) { enterOfflineMode(); }
    });
}

function loadFolderOffline(path) {
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

function syncNetworkState() {
  if (!isAdmin) return;
  Promise.all([
    fetch('/api/config').then(function(r){return r.json()}),
    fetch('/api/wan/status').then(function(r){return r.json()})
  ]).then(function(results) {
    var cfg = results[0];
    var wan = results[1];
    var info = document.getElementById('lanInfo');
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
      }
    }

    if (parts.length) {
      info.style.display = '';
      info.innerHTML = parts.join('<br>');
    } else {
      info.style.display = 'none';
    }
  });
}

function togglePublic(enabled) {
  setToggle('publicToggle', 'publicDot', enabled);
  var info = document.getElementById('lanInfo');
  info.style.display = '';
  info.textContent = enabled ? 'Подключаю LAN...' : 'Отключаю LAN...';

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
    if (d.public) {
      info.textContent = 'LAN включён. Перезапуск...';
    } else {
      info.textContent = 'LAN выключен. Перезапуск...';
    }
    // Server restarts on new bind address + possibly HTTPS; redirect accordingly
    var url = d.redirect_url || ('http://127.0.0.1:' + location.port);
    setTimeout(function() { window.location.href = url; }, 2500);
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

  var info = document.getElementById('lanInfo');
  info.style.display = '';

  if (mode === 'tunnel') {
    info.innerHTML = '<span style="color:#e9a545">&#9679;</span> Запускаю туннель...';
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
        var info = document.getElementById('lanInfo');
        info.innerHTML = '<span style="color:#e9a545">&#9679;</span> Запускаю туннель... (' + wanPollCount + 'с)';
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

function onSearchInput(q) {
  var btn = document.getElementById('searchClear');
  btn.classList.toggle('show', q.length > 0);
  clearTimeout(searchTimer);
  q = q.trim().toLowerCase();
  if (!q) {
    filteredTracks = null;
    filteredAlbums = null;
    renderTracks();
    renderAlbums();
    document.getElementById('playlistHeader').textContent =
      activeTab === 'albums' ? albums.length + ' альбомов' : tracks.length + ' треков';
    return;
  }
  searchTimer = setTimeout(function() {
    // Filter tracks
    filteredTracks = [];
    for (var i = 0; i < tracks.length; i++) {
      var t = tracks[i];
      var hay = (t.title + ' ' + t.artist + ' ' + t.album).toLowerCase();
      if (hay.indexOf(q) >= 0) filteredTracks.push(i);
    }
    // Filter albums
    filteredAlbums = [];
    for (var a = 0; a < albums.length; a++) {
      var alb = albums[a];
      var hay = (alb.name + ' ' + alb.artist).toLowerCase();
      if (hay.indexOf(q) >= 0) {
        filteredAlbums.push(a);
      } else {
        // Check if any track in album matches
        for (var ti = 0; ti < alb.tracks.length; ti++) {
          var t = tracks[alb.tracks[ti]];
          if (t && (t.title + ' ' + t.artist).toLowerCase().indexOf(q) >= 0) {
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
  var t = document.getElementById('toast');
  if (_toastTimer) clearTimeout(_toastTimer);
  t.textContent = msg;
  t.classList.add('show');
  _toastTimer = setTimeout(function(){ t.classList.remove('show'); _toastTimer = null; }, 2500);
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
        if (d.artist) tracks[currentIdx].artist = d.artist;
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
  if (vkPolling) pollVk();
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
function openProfile() {
  document.getElementById('profileUser').textContent = 'Пользователь: ' + currentUser;
  document.getElementById('profOldPw').value = '';
  document.getElementById('profNewPw').value = '';
  // Show cache stats
  var count = Object.keys(cachedFiles).length;
  var infoEl = document.getElementById('profileCacheInfo');
  infoEl.textContent = count ? count + ' треков в кэше' : 'Кэш пуст';
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
    // Clear SW cache so login page is fetched fresh (not cached app)
    if ('caches' in window) {
      caches.keys().then(function(names) {
        return Promise.all(names.map(function(n) { return caches.delete(n); }));
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

// ── Playlists ──
var userPlaylists = [];
var plEditId = null;
var plEditTracks = [];

function loadUserPlaylists() {
  var folder = document.getElementById('folderSelect').value;
  if (!folder) return;
  if (_isOffline) {
    try {
      var saved = localStorage.getItem('_vc_playlists_' + folder);
      if (saved) { userPlaylists = JSON.parse(saved); }
    } catch(e){}
    renderPlaylists();
    document.getElementById('playlistHeader').textContent = userPlaylists.length + ' плейлистов';
    return;
  }
  fetch('/api/playlists', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder: folder, action: 'list'})})
  .then(function(r){return r.json()}).then(function(d) {
    userPlaylists = d.playlists || [];
    try { localStorage.setItem('_vc_playlists_' + folder, JSON.stringify(userPlaylists)); } catch(e){}
    renderPlaylists();
    document.getElementById('playlistHeader').textContent = userPlaylists.length + ' плейлистов';
  });
}

var expandedPlaylist = null;

function renderPlaylists() {
  var html = '';
  if (userRole !== 'demo') {
    html += '<div style="padding:8px 12px"><button class="folder-btn folder-btn-secondary" style="width:100%;font-size:12px" onclick="createPlaylist()">+ Создать плейлист</button></div>';
  }
  for (var i = 0; i < userPlaylists.length; i++) {
    var pl = userPlaylists[i];
    var isExp = expandedPlaylist === pl.id;
    var coverHtml = buildPlCover(pl);
    html += '<div class="album-card pl-drag-card' + (isExp ? ' active' : '') + '" data-plid="' + pl.id + '" data-plidx="' + i + '" draggable="true" onclick="togglePlaylistExpand(\'' + pl.id + '\')" oncontextmenu="showPlCtxMenu(event,\'' + pl.id + '\')" data-longpress-pl="' + pl.id + '">'
      + '<div class="album-cover" style="position:relative;overflow:hidden">' + coverHtml + '</div>'
      + '<div class="album-info"><div class="album-name">' + esc(pl.name) + '</div>'
      + '<div class="album-count">' + pl.tracks.length + ' треков</div></div>'
      + '<button class="shuffle-btn" style="width:32px;height:32px;flex-shrink:0" onclick="event.stopPropagation();cachePlaylist(\'' + pl.id + '\')" data-tip="Кэшировать"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg></button>'
      + (userRole !== 'demo' ? '<button class="shuffle-btn" style="width:32px;height:32px;flex-shrink:0" onclick="event.stopPropagation();editPlaylist(\'' + pl.id + '\')" data-tip="Редактировать"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 000-1.41l-2.34-2.34a1 1 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg></button>' : '')
      + '</div>';
    // Expanded track list
    html += '<div class="album-tracks' + (isExp ? ' open' : '') + '">';
    if (isExp) {
      html += '<div style="padding:6px 12px"><button class="folder-btn folder-btn-primary" style="width:100%;font-size:11px;padding:6px" onclick="event.stopPropagation();playPlaylist(\'' + pl.id + '\')">&#9654; Воспроизвести</button></div>';
      for (var ti = 0; ti < pl.tracks.length; ti++) {
        var file = pl.tracks[ti];
        var t = tracks.find(function(tr){return tr.file===file});
        if (!t) continue;
        var trackIdx = tracks.indexOf(t);
        var plCachedDot = isTrackCached(file) ? '<span style="width:5px;height:5px;border-radius:50%;background:#52b788;flex-shrink:0;margin-left:auto"></span>' : '';
        var plOffDis = _isOffline && !isTrackCached(file);
        html += '<div class="playlist-item' + (trackIdx === currentIdx ? ' active' : '') + '"'
          + (plOffDis ? ' style="padding-left:20px;opacity:0.3;pointer-events:none"' : ' style="padding-left:20px"')
          + (plOffDis ? '' : ' onclick="event.stopPropagation();playFromPlaylist(\'' + pl.id + '\',' + ti + ')"') + '>'
          + '<div class="info"><div class="name" style="font-size:12px">' + esc(t.title) + '</div>'
          + '<div class="artist" style="font-size:11px">' + esc(t.artist) + '</div></div>' + plCachedDot + '</div>';
      }
    }
    html += '</div>';
  }
  document.getElementById('playlistsList').innerHTML = html;
  initPlDrag();
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
  fetch('/api/playlists', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder: folder, action: 'reorder', order: order})});
}

function playFromPlaylist(plId, trackIndex) {
  var pl = userPlaylists.find(function(p){return p.id===plId});
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

function buildPlCover(pl) {
  // 4 covers from last 4 tracks
  var covers = [];
  for (var i = pl.tracks.length - 1; i >= 0 && covers.length < 4; i--) {
    var file = pl.tracks[i];
    var t = tracks.find(function(tr){return tr.file === file});
    if (t && t.has_cover) covers.push('/api/cover/' + encodeURIComponent(t.file));
  }
  if (covers.length === 0) return '<div style="width:100%;height:100%;background:#333;display:flex;align-items:center;justify-content:center;color:rgba(255,255,255,0.2);font-size:20px">&#9835;</div>';
  if (covers.length < 4) return '<img src="' + covers[0] + '" style="width:100%;height:100%;object-fit:cover">';
  return '<div style="display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;width:100%;height:100%">'
    + covers.map(function(c){return '<img src="'+c+'" style="width:100%;height:100%;object-fit:cover">'}).join('')
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
      if (d.ok) {
        showToast('Плейлист удалён');
        document.getElementById('plEditOverlay').classList.remove('show');
        loadUserPlaylists();
      } else showToast(d.error);
    });
  }, 'Удалить');
}

function savePlEdit() {
  var folder = document.getElementById('folderSelect').value;
  var name = document.getElementById('plEditName').value.trim() || 'Плейлист';
  var action = plEditId ? 'update' : 'create';
  var body = {folder: folder, action: action, name: name, tracks: plEditTracks};
  if (plEditId) body.id = plEditId;
  fetch('/api/playlists', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok) {
      showToast(plEditId ? 'Плейлист обновлён' : 'Плейлист создан');
      document.getElementById('plEditOverlay').classList.remove('show');
      loadUserPlaylists();
    } else showToast(d.error);
  });
}

function playPlaylist(id) {
  var pl = userPlaylists.find(function(p){return p.id===id});
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

function showCtxMenu(e, idx) {
  _ctxIdx = idx;
  var menu = document.getElementById('ctxMenu');
  // Update cache/uncache label
  var isCached = idx >= 0 && idx < tracks.length && isTrackCached(tracks[idx].file);
  document.getElementById('ctxCacheLabel').textContent = isCached ? 'Удалить из кэша' : 'Кэшировать';
  // Build playlist submenu
  var plHtml = '';
  if (userPlaylists.length === 0) {
    plHtml = '<div class="ctx-item" style="color:rgba(255,255,255,0.3);pointer-events:none">Нет плейлистов</div>';
  } else {
    for (var p = 0; p < userPlaylists.length; p++) {
      var pl = userPlaylists[p];
      plHtml += '<div class="ctx-item" onclick="ctxAddToPlaylist(\'' + pl.id + '\',\'start\')">' + esc(pl.name) + ' <span style="color:rgba(255,255,255,0.25);margin-left:auto;font-size:10px">в начало</span></div>';
      plHtml += '<div class="ctx-item" onclick="ctxAddToPlaylist(\'' + pl.id + '\',\'end\')">' + esc(pl.name) + ' <span style="color:rgba(255,255,255,0.25);margin-left:auto;font-size:10px">в конец</span></div>';
    }
  }
  document.getElementById('ctxPlaylists').innerHTML = plHtml;
  // Position
  var x = e.clientX || (e.touches && e.touches[0] ? e.touches[0].clientX : 100);
  var y = e.clientY || (e.touches && e.touches[0] ? e.touches[0].clientY : 100);
  menu.style.left = Math.min(x, window.innerWidth - 200) + 'px';
  menu.style.top = Math.min(y, window.innerHeight - 300) + 'px';
  menu.classList.add('show');
  // Close on any outside click
  setTimeout(function() {
    document.addEventListener('click', hideCtxMenu, {once: true});
    document.addEventListener('touchstart', hideCtxMenu, {once: true});
  }, 50);
}

function hideCtxMenu() {
  document.getElementById('ctxMenu').classList.remove('show');
  _ctxIdx = -1;
}

var _forceNextIdx = -1;

function ctxPlayNext() {
  var idx = _ctxIdx;
  hideCtxMenu();
  if (idx < 0 || idx >= tracks.length) return;
  _forceNextIdx = idx;
  showToast(tracks[idx].title + ' — следующий');
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
  fetch('/api/playlists', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder: folder, action: 'update', id: plId, tracks: newTracks})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok) {
      pl.tracks = newTracks;
      showToast('Добавлено в «' + pl.name + '»');
    }
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
    document.addEventListener('click', hidePlCtxMenu, {once: true});
    document.addEventListener('touchstart', hidePlCtxMenu, {once: true});
  }, 50);
}
function hidePlCtxMenu() {
  document.getElementById('plCtxMenu').classList.remove('show');
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
      if (d.ok) { showToast('Плейлист удалён'); loadUserPlaylists(); }
    });
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

function openTrackEdit(idx) {
  if (idx < 0 || idx >= tracks.length) return;
  editingTrackIdx = idx;
  var t = tracks[idx];
  document.getElementById('trackEditFile').textContent = t.file;
  document.getElementById('trackEditTitle').value = t.title || '';
  document.getElementById('trackEditArtist').value = t.artist || '';
  document.getElementById('trackEditMeta').checked = false;
  var m = t.file.match(/^(\d+)\.\s/);
  var hint = document.getElementById('trackEditOrderHint');
  if (m) {
    document.getElementById('trackEditOrder').value = parseInt(m[1]);
    hint.textContent = '(сейчас: ' + parseInt(m[1]) + ')';
  } else {
    document.getElementById('trackEditOrder').value = '';
    hint.textContent = '(нет номера — введите чтобы назначить)';
  }
  // Show cache status
  var cacheRow = document.getElementById('trackEditCacheRow');
  cacheRow.style.display = isTrackCached(t.file) ? 'flex' : 'none';
  document.getElementById('trackEditOverlay').classList.add('show');
  document.getElementById('trackEditTitle').focus();
}

function uncacheEditTrack() {
  if (editingTrackIdx < 0 || editingTrackIdx >= tracks.length) return;
  var file = tracks[editingTrackIdx].file;
  // Remove audio and cover from cache
  openCacheDB(function(db) {
    var tx = db.transaction('audio', 'readwrite');
    var store = tx.objectStore('audio');
    store.delete(file);
    store.delete('cover:' + file);
    tx.oncomplete = function() {
      delete cachedFiles[file];
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
    body: JSON.stringify({folder: folder, file: t.file, title: title, artist: artist, order: order, run_meta: runMeta})})
  .then(function(r){return r.json()}).then(function(d) {
    if (d.ok) {
      showToast('Трек обновлён');
      document.getElementById('trackEditOverlay').classList.remove('show');
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
              audio.src = '/api/stream/' + encodeURIComponent(d.new_file);
              audio.currentTime = playPos;
              audio.play();
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
  var tl = document.getElementById('trackList');
  if (tl) tl.scrollTo({top: 0, behavior: 'smooth'});
  var al = document.getElementById('albumList');
  if (al) al.scrollTo({top: 0, behavior: 'smooth'});
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
    var left = r.left + r.width / 2 - tw / 2;
    if (left < 4) left = 4;
    if (left + tw > window.innerWidth - 4) left = window.innerWidth - tw - 4;
    tip.style.left = left + 'px';
    tip.style.top = (r.bottom + 6) + 'px';
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

function refreshCachedList() {
  openCacheDB(function(db) {
    var tx = db.transaction('audio', 'readonly');
    var store = tx.objectStore('audio');
    var req = store.getAllKeys();
    req.onsuccess = function() {
      cachedFiles = {};
      for (var i = 0; i < req.result.length; i++) if (req.result[i].indexOf('cover:') !== 0) cachedFiles[req.result[i]] = true;
      if (typeof renderTracks === 'function') renderTracks();
      prepareNearbyBlobs();
    };
  });
}

function isTrackCached(file) { return !!cachedFiles[file]; }

function cacheTrack(file, onDone) {
  var url = '/api/stream/' + encodeURIComponent(file);
  fetch(url).then(function(r) {
    if (!r.ok) throw new Error('fetch failed');
    return r.arrayBuffer();
  }).then(function(buf) {
    openCacheDB(function(db) {
      var tx = db.transaction('audio', 'readwrite');
      tx.objectStore('audio').put(buf, file);
      tx.oncomplete = function() {
        cachedFiles[file] = true;
        // Also cache cover art if available
        cacheCover(file);
        if (onDone) onDone(true);
      };
      tx.onerror = function() { if (onDone) onDone(false); };
    });
  }).catch(function() { if (onDone) onDone(false); });
}

function cacheCover(file) {
  var url = '/api/cover/' + encodeURIComponent(file);
  fetch(url).then(function(r) {
    if (!r.ok) return;
    return r.arrayBuffer();
  }).then(function(buf) {
    if (!buf) return;
    openCacheDB(function(db) {
      var tx = db.transaction('audio', 'readwrite');
      tx.objectStore('audio').put(buf, 'cover:' + file);
    });
  }).catch(function() {});
}

function getCachedCover(file, cb) {
  openCacheDB(function(db) {
    var tx = db.transaction('audio', 'readonly');
    var req = tx.objectStore('audio').get('cover:' + file);
    req.onsuccess = function() { cb(req.result || null); };
    req.onerror = function() { cb(null); };
  });
}

// Fallback for broken cover images: try IndexedDB cache, else remove img
function loadCachedImg(img, encodedFile) {
  var file = decodeURIComponent(encodedFile);
  img.onerror = null; // prevent loop
  getCachedCover(file, function(buf) {
    if (buf) {
      var blob = new Blob([buf]);
      img.src = URL.createObjectURL(blob);
    } else {
      img.remove();
    }
  });
}

function uncacheTrack(file) {
  openCacheDB(function(db) {
    var tx = db.transaction('audio', 'readwrite');
    var store = tx.objectStore('audio');
    store.delete(file);
    store.delete('cover:' + file);
    tx.oncomplete = function() { delete cachedFiles[file]; renderTracks(); renderAlbums(); showToast('Удалено из кэша'); };
  });
}

function getCachedAudio(file, cb) {
  openCacheDB(function(db) {
    var tx = db.transaction('audio', 'readonly');
    var req = tx.objectStore('audio').get(file);
    req.onsuccess = function() { cb(req.result || null); };
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

function beginCaching(files) {
  cacheQueue = files.slice();
  cacheTotalCount = files.length;
  cachingActive = true;
  showToast('Кэширование: 0/' + cacheTotalCount);
  updateCacheBtn();
  cacheNextInQueue();
}

function cacheNextInQueue() {
  if (!cachingActive || !cacheQueue.length) {
    var was = cachingActive;
    cachingActive = false;
    updateCacheBtn();
    if (was) { showToast('Кэширование завершено'); refreshCachedList(); }
    return;
  }
  var done = cacheTotalCount - cacheQueue.length;
  showToast('Кэширование: ' + done + '/' + cacheTotalCount);
  var file = cacheQueue.shift();
  updateCacheBtn();
  cacheTrack(file, function() { cacheNextInQueue(); });
}

function stopCacheAll() {
  cacheQueue = [];
  cachingActive = false;
  updateCacheBtn();
  showToast('Кэширование остановлено');
  refreshCachedList();
}

function updateCacheBtn() {
  var btn = document.getElementById('cacheBtn');
  if (btn) btn.classList.toggle('active', cachingActive);
}

function cachePlaylist(plId) {
  var pl = userPlaylists.find(function(p){return p.id===plId});
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

initBgCanvas();
// Hide volume slider on iOS (audio.volume is read-only)
if(_isIOS){var vw=document.querySelector('.volume-wrap input[type=range]');if(vw)vw.style.display='none';var vs=document.querySelector('.volume-wrap span');if(vs)vs.style.display='none';}
initMediaSession();

// Detect online/offline transitions
window.addEventListener('online', function() {
  if (_isOffline) {
    _isOffline = false;
    showOfflineBanner(false);
    showCachedOnly = false;
    var btn = document.getElementById('cachedOnlyBtn');
    if (btn) btn.classList.remove('active');
    loadConfig();
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
  var u=document.getElementById('lu').value,p=document.getElementById('lp').value,mr=document.getElementById('mr').value;
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

    def _needs_auth(self, path):
        return not path.startswith("/api/auth/")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

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
            sw_code = SW_JS.replace("BUILD_HASH", build_hash)
            self._respond(200, "application/javascript", sw_code.encode("utf-8"))
            return

        if not user:
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
                "username": user,
                "is_admin": udata.get("is_admin", False) if udata else False,
                "role": udata.get("role", "user") if udata else "user",
                "music_root": get_music_root(),
                "is_local": is_local,
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
            mime = mimetypes.guess_type(str(filepath))[0] or "audio/mpeg"
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
                redirect_url = "{}://127.0.0.1:{}".format(proto, SERVER_PORT)
                lan_url = "{}://{}:{}".format(proto, local_ip, SERVER_PORT)
                all_urls = ["{}://{}:{}".format(proto, ip, SERVER_PORT) for ip in all_ips]
                self._respond_json({"ok": True, "public": True, "redirect_url": redirect_url, "lan_url": lan_url, "ip": local_ip, "all_urls": all_urls})
                try: self.wfile.flush()
                except Exception: pass
                threading.Timer(1.0, _restart_server, args=["0.0.0.0"]).start()
            else:
                _use_https = False
                s["lan"] = False
                s["https"] = False
                save_settings(s)
                self._respond_json({"ok": True, "public": False})
                try: self.wfile.flush()
                except Exception: pass
                threading.Timer(1.0, _restart_server, args=["127.0.0.1"]).start()


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

        elif path == "/api/track/edit":
            if self._deny_demo(udata): return
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            old_file = data.get("file", "")
            new_title = data.get("title", "").strip()
            new_artist = data.get("artist", "").strip()
            new_order = data.get("order", 0)
            run_meta = data.get("run_meta", False)
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
                    except Exception:
                        pass
                # Run meta search if requested
                if run_meta and new_name:
                    def do_meta():
                        fp = str(Path(folder) / new_name)
                        found = search_metadata(new_artist or '', new_title)
                        if found:
                            cover = fetch_cover_art(found)
                            write_metadata_to_file(fp, found, cover)
                    threading.Thread(target=do_meta, daemon=True).start()
                self._respond_json({"ok": True, "new_file": new_name})
            except Exception:
                self._respond_json({"ok": False, "error": "Ошибка переименования."})

        elif path == "/api/playlists":
            folder = data.get("folder", _user_music_dirs.get(user, ""))
            action = data.get("action", "")
            if not folder:
                self._respond_json({"ok": False, "error": "Нет каталога."})
                return

            playlists = load_playlists(folder)

            if action == "list":
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
        except (ConnectionResetError, BrokenPipeError, ssl.SSLError, OSError):
            pass  # Client disconnected during SSL handshake or request


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
            [cf_bin, "tunnel", "--url", "http://127.0.0.1:{}".format(SERVER_PORT)],
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


def _cert_covers_current_ips():
    """Check if existing cert SAN covers all current local IPs."""
    if not CERT_FILE.exists():
        return False
    try:
        out = subprocess.check_output(
            ["openssl", "x509", "-in", str(CERT_FILE), "-noout", "-ext", "subjectAltName"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode()
        current_ips = set(get_all_local_ips()) | {"127.0.0.1"}
        for ip in current_ips:
            if "IP Address:" + ip not in out:
                return False
        return True
    except Exception:
        return False


def _generate_self_signed_cert(force=False):
    """Генерирует self-signed сертификат для HTTPS (LAN/offline)."""
    if not force and CERT_FILE.exists() and KEY_FILE.exists():
        # Check if cert covers current IPs
        if _cert_covers_current_ips():
            return True
        print("HTTPS: IP изменился, перегенерирую сертификат...")
    try:
        san_ips = list(set(["127.0.0.1"] + get_all_local_ips()))
        # Add common private ranges to avoid regeneration on IP changes
        san_entries = ",".join("IP:" + ip for ip in san_ips) + ",DNS:localhost"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
            "-days", "3650", "-nodes",
            "-subj", "/CN=insideside-music",
            "-addext", "subjectAltName=" + san_entries
        ], capture_output=True, timeout=10, check=True)
        os.chmod(str(KEY_FILE), 0o600)
        os.chmod(str(CERT_FILE), 0o600)
        return True
    except Exception as ex:
        print(f"HTTPS: не удалось создать сертификат: {ex}")
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


_server = None
_server_thread = None
_server_lock = threading.Lock()


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


def _start_server(bind_addr):
    """Запускает HTTP/HTTPS-сервер в фоновом потоке."""
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


def _restart_server(bind_addr):
    """Перезапускает HTTP-сервер на новом адресе."""
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


def main():
    global IS_PUBLIC, _use_https
    _load_sessions()
    public = "--public" in sys.argv
    IS_PUBLIC = public
    bind_addr = "0.0.0.0" if public else "127.0.0.1"

    # Auto-restore saved LAN mode
    s = load_settings()
    if s.get("lan"):
        IS_PUBLIC = True
        bind_addr = "0.0.0.0"
        print("Restoring LAN mode")

    # Auto-restore saved WAN static IP config
    if s.get("wan_mode") == "static" and s.get("wan_ip"):
        IS_PUBLIC = True
        bind_addr = "0.0.0.0"
        print("Restoring WAN static: http://{}:{}".format(s["wan_ip"], s.get("wan_port", SERVER_PORT)))

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

    _start_server(bind_addr)

    # Apply saved WAN after server starts
    if s.get("wan_mode") == "static" and s.get("wan_ip"):
        set_wan_static(s["wan_ip"], s.get("wan_port", str(SERVER_PORT)))

    proto = "https" if _use_https else "http"
    url = "{}://127.0.0.1:{}".format(proto, SERVER_PORT)
    import base64 as _b64
    _an = _b64.b64decode("aW5zaWRlc2lkZSBtdXNpYw==").decode()
    print(_an + ": " + url)
    if public or IS_PUBLIC:
        local_ip = get_local_ip()
        print("LAN: {}://{}:{}".format(proto, local_ip, SERVER_PORT))
    print("Ctrl+C для остановки")
    webbrowser.open(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nОстановлено.")
        stop_tunnel()
        with _server_lock:
            if _server:
                _server.shutdown()
                _server.server_close()


if __name__ == "__main__":
    main()
