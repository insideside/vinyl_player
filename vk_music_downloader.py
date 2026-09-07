#!/usr/bin/env python3
"""
VK Music Playlist Downloader
Веб-интерфейс для скачивания треков из плейлистов VK Music.
Работает на macOS и Windows — открывается в браузере.
"""

import json
import html
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from httpx import Client as HttpClient
from vkpymusic import Service

os.environ["TK_SILENCE_DEPRECATION"] = "1"

VK_APP_ID = 2685278
TOKEN_FILE = Path.home() / ".vk_music_token.json"
USER_AGENT = "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)"
SERVER_PORT = 7655

# ──────────────────── Глобальное состояние ────────────────────

state = {
    "service": None,
    "log": [],
    "progress": 0,
    "total": 0,
    "running": False,
    "done": False,
}


# ──────────────────── Core logic ────────────────────

def parse_playlist_url(url):
    pattern = r"music/playlist/(-?\d+)_(\d+)_([a-f0-9]+)"
    match = re.search(pattern, url)
    if not match:
        raise ValueError("Неверный формат ссылки: " + url)
    return match.group(1), int(match.group(2)), match.group(3)


def get_all_playlist_songs(service, owner_id, playlist_id, access_key):
    all_songs = []
    offset = 0
    while True:
        songs = service.get_songs_by_playlist_id(
            user_id=owner_id, playlist_id=playlist_id,
            access_key=access_key, count=100, offset=offset,
        )
        if not songs:
            break
        all_songs.extend(songs)
        if len(songs) < 100:
            break
        offset += 100
        time.sleep(0.3)
    return all_songs


def safe_filename(s):
    s = re.sub(r'[<>:"/\\|?*]', '', s)
    s = s.strip('. ')
    return s if s else 'unknown'


def download_song(song, filepath):
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


def search_and_download_fallback(service, artist, title, filepath):
    try:
        results = service.search_songs_by_text(artist + " " + title, count=5)
    except Exception:
        return False
    for r in results:
        if r.url and "index.m3u8" not in r.url:
            if download_song(r, filepath):
                return True
    return False


# ──────────────────── Library helpers ────────────────────

def get_existing_tracks(folder):
    tracks = []
    for f in Path(folder).glob("*.mp3"):
        match = re.match(r'^(\d+)\.\s+(.+)$', f.stem)
        if match:
            tracks.append((int(match.group(1)), match.group(2), f))
    tracks.sort(key=lambda x: x[0])
    return tracks


def renumber_tracks(folder, start_from):
    tracks = get_existing_tracks(folder)
    if not tracks:
        return
    total = start_from + len(tracks) - 1
    pad = len(str(total))
    for i in reversed(range(len(tracks))):
        _, name, old_path = tracks[i]
        new_num = str(start_from + i).zfill(pad)
        new_name = new_num + ". " + name + ".mp3"
        new_path = old_path.parent / new_name
        if old_path != new_path:
            old_path.rename(new_path)


def repad_tracks(folder):
    tracks = get_existing_tracks(folder)
    if not tracks:
        return
    max_num = max(t[0] for t in tracks)
    pad = len(str(max_num))
    for num, name, old_path in tracks:
        new_num = str(num).zfill(pad)
        new_name = new_num + ". " + name + ".mp3"
        new_path = old_path.parent / new_name
        if old_path != new_path:
            old_path.rename(new_path)


# ──────────────────── Token ────────────────────

def save_token(token):
    TOKEN_FILE.write_text(json.dumps({"token": token}))


def load_token():
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text()).get("token")
        except Exception:
            pass
    return None


def make_service(token):
    return Service(USER_AGENT, token)


def validate_token(token):
    try:
        svc = make_service(token)
        svc.get_popular(count=1)
        return True
    except Exception:
        return False


def log(msg):
    state["log"].append(msg)


# ──────────────────── Download worker ────────────────────

def download_worker(urls, folder, order, mode):
    state["running"] = True
    state["done"] = False
    state["log"] = []
    state["progress"] = 0
    state["total"] = 0

    try:
        service = state["service"]
        save_dir = Path(folder)
        save_dir.mkdir(parents=True, exist_ok=True)

        all_tracks = []
        total_playlists = len(urls)

        for i, url in enumerate(reversed(urls)):
            pl_num = total_playlists - i
            owner_id, playlist_id, access_key = parse_playlist_url(url)
            log("[{}/{}] Загружаю список треков...".format(pl_num, total_playlists))
            songs = get_all_playlist_songs(service, owner_id, playlist_id, access_key)
            log("  Найдено: {} треков".format(len(songs)))
            if order == "reverse":
                songs = list(reversed(songs))
            all_tracks.extend(songs)

        new_count = len(all_tracks)
        if new_count == 0:
            log("Треков не найдено.")
            return

        existing = get_existing_tracks(save_dir)
        if mode == "prepend" and existing:
            shift = new_count
            log("Сдвигаю {} существующих треков...".format(len(existing)))
            renumber_tracks(save_dir, start_from=shift + 1)
            start_num = 1
        elif mode == "append" and existing:
            start_num = max(t[0] for t in existing) + 1
        else:
            start_num = 1

        total = new_count
        state["total"] = total
        max_num = start_num + total - 1
        if mode in ("prepend", "append") and existing:
            refreshed = get_existing_tracks(save_dir)
            if refreshed:
                max_num = max(max_num, max(t[0] for t in refreshed))
        pad = len(str(max_num))

        log("\nСкачиваю {} треков...".format(total))
        downloaded = 0
        failed = []

        for idx, song in enumerate(all_tracks):
            track_num = start_num + idx
            num_str = str(track_num).zfill(pad)
            artist = safe_filename(song.artist)
            title = safe_filename(song.title)
            filename = "{}. {} - {}.mp3".format(num_str, artist, title)
            filepath = save_dir / filename
            display = "{} - {}".format(artist, title)

            state["progress"] = idx + 1

            if filepath.exists():
                log("  Уже есть: " + display)
                downloaded += 1
                continue

            ok = download_song(song, filepath)
            if not ok:
                ok = search_and_download_fallback(service, song.artist, song.title, filepath)
                if ok:
                    log("  " + display + " (найден через поиск)")

            if ok:
                downloaded += 1
                log("  OK: " + display)
            else:
                failed.append(display)
                log("  НЕ НАЙДЕН: " + display)

            time.sleep(0.3)

        repad_tracks(save_dir)

        log("\n========================================")
        log("Готово! Скачано: {}/{}".format(downloaded, total))
        log("Папка: {}".format(save_dir.resolve()))
        if failed:
            log("\nНе удалось скачать ({}):" .format(len(failed)))
            for f in failed:
                log("  - " + f)
    except Exception as e:
        log("ОШИБКА: " + str(e))
    finally:
        state["running"] = False
        state["done"] = True


# ──────────────────── HTML ────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VK Music Downloader</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #1a1a2e; color: #eee; padding: 20px; min-height: 100vh; }
.container { max-width: 700px; margin: 0 auto; }
h1 { text-align: center; color: #e94560; margin-bottom: 20px; font-size: 24px; }
.card { background: #16213e; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
.card h2 { font-size: 16px; color: #e94560; margin-bottom: 12px; }
label { display: block; font-size: 14px; color: #aaa; margin-bottom: 4px; }
input[type=text], input[type=url] {
    width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #333;
    background: #0f3460; color: #eee; font-size: 14px; outline: none; }
input:focus { border-color: #e94560; }
button { padding: 10px 20px; border-radius: 8px; border: none; cursor: pointer;
         font-size: 14px; font-weight: 600; transition: background 0.2s; }
.btn-primary { background: #e94560; color: #fff; }
.btn-primary:hover { background: #c73650; }
.btn-primary:disabled { background: #555; cursor: not-allowed; }
.btn-secondary { background: #0f3460; color: #eee; border: 1px solid #333; }
.btn-secondary:hover { background: #1a4a7a; }
.btn-small { padding: 6px 14px; font-size: 13px; }
.btn-danger { background: #333; color: #e94560; }
.btn-danger:hover { background: #442; }
.row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.row input { flex: 1; }
.url-list { list-style: none; margin: 8px 0; }
.url-list li { background: #0f3460; padding: 8px 12px; border-radius: 6px; margin-bottom: 4px;
               display: flex; justify-content: space-between; align-items: center; font-size: 13px;
               word-break: break-all; }
.url-list li .num { color: #e94560; margin-right: 8px; font-weight: bold; }
.radio-group { display: flex; gap: 16px; margin: 8px 0; }
.radio-group label { display: flex; align-items: center; gap: 6px; color: #eee; cursor: pointer; }
.radio-group input { accent-color: #e94560; }
.status { display: inline-block; padding: 4px 10px; border-radius: 20px; font-size: 13px; font-weight: 600; }
.status-ok { background: #1b4332; color: #52b788; }
.status-no { background: #442; color: #e94560; }
#log { background: #0a0a1a; border-radius: 8px; padding: 12px; font-family: 'SF Mono', Menlo, monospace;
       font-size: 12px; max-height: 300px; overflow-y: auto; white-space: pre-wrap; color: #aaa;
       margin-top: 10px; min-height: 60px; }
.progress-bar { width: 100%; height: 8px; background: #0f3460; border-radius: 4px; overflow: hidden; margin: 8px 0; }
.progress-fill { height: 100%; background: #e94560; transition: width 0.3s; border-radius: 4px; }
.progress-text { font-size: 13px; color: #aaa; text-align: center; }
.flex-right { display: flex; justify-content: flex-end; gap: 8px; }
.mt { margin-top: 12px; }
</style>
</head>
<body>
<div class="container">
<h1>VK Music Playlist Downloader</h1>

<!-- Авторизация -->
<div class="card">
    <h2>Авторизация VK</h2>
    <div class="row">
        <span>Статус: <span id="authStatus" class="status status-no">Не авторизован</span></span>
        <span style="flex:1"></span>
        <button class="btn-primary btn-small" onclick="doAuth()">Войти через браузер</button>
    </div>
    <div id="tokenInput" style="display:none; margin-top:12px;">
        <label>Вставьте URL из адресной строки после авторизации:</label>
        <div class="row">
            <input type="text" id="tokenUrl" placeholder="https://oauth.vk.com/blank.html#access_token=...">
            <button class="btn-primary btn-small" onclick="submitToken()">OK</button>
        </div>
    </div>
</div>

<!-- Плейлисты -->
<div class="card">
    <h2>Плейлисты <span style="font-weight:normal;color:#aaa;font-size:13px">(первый — внизу, последний — вверху)</span></h2>
    <div class="row">
        <input type="url" id="urlInput" placeholder="https://vk.ru/music/playlist/..." onkeydown="if(event.key==='Enter')addUrl()">
        <button class="btn-primary btn-small" onclick="addUrl()">Добавить</button>
    </div>
    <ul class="url-list" id="urlList"></ul>
</div>

<!-- Настройки -->
<div class="card">
    <h2>Настройки</h2>
    <label>Порядок треков в плейлисте:</label>
    <div class="radio-group">
        <label><input type="radio" name="order" value="normal" checked> Как в плейлисте</label>
        <label><input type="radio" name="order" value="reverse"> В обратном порядке</label>
    </div>
    <label class="mt">Режим:</label>
    <div class="radio-group">
        <label><input type="radio" name="mode" value="new" checked> Новая загрузка</label>
        <label><input type="radio" name="mode" value="prepend"> Добавить в начало</label>
        <label><input type="radio" name="mode" value="append"> Добавить в конец</label>
    </div>
    <label class="mt">Папка для сохранения:</label>
    <input type="text" id="folder" value="FOLDER_DEFAULT">
</div>

<!-- Прогресс -->
<div class="card">
    <h2>Прогресс</h2>
    <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
    <div class="progress-text" id="progressText"></div>
    <div id="log"></div>
</div>

<div style="text-align:center; margin-top:16px">
    <button class="btn-primary" id="downloadBtn" onclick="startDownload()" style="padding:14px 50px;font-size:16px">
        Скачать
    </button>
</div>
</div>

<script>
var urls = [];

function doAuth() {
    window.open('AUTH_URL', '_blank');
    document.getElementById('tokenInput').style.display = 'block';
    document.getElementById('tokenUrl').focus();
}

function submitToken() {
    var raw = document.getElementById('tokenUrl').value.trim();
    if (!raw) return;
    fetch('/api/auth', { method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({url: raw}) })
    .then(r => r.json()).then(d => {
        if (d.ok) {
            document.getElementById('authStatus').className = 'status status-ok';
            document.getElementById('authStatus').textContent = 'Авторизован';
            document.getElementById('tokenInput').style.display = 'none';
        } else {
            alert(d.error || 'Ошибка авторизации');
        }
    });
}

function addUrl() {
    var input = document.getElementById('urlInput');
    var url = input.value.trim();
    if (!url) return;
    if (!/music\\/playlist\\/-?\\d+_\\d+_[a-f0-9]+/.test(url)) {
        alert('Неверный формат ссылки на плейлист VK Music');
        return;
    }
    urls.push(url);
    input.value = '';
    renderUrls();
}

function removeUrl(i) {
    urls.splice(i, 1);
    renderUrls();
}

function renderUrls() {
    var list = document.getElementById('urlList');
    list.innerHTML = '';
    for (var i = 0; i < urls.length; i++) {
        var li = document.createElement('li');
        li.innerHTML = '<span><span class="num">' + (i+1) + '</span>' + escapeHtml(urls[i]) + '</span>' +
            '<button class="btn-danger btn-small" onclick="removeUrl(' + i + ')">x</button>';
        list.appendChild(li);
    }
}

function escapeHtml(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function getRadio(name) {
    var r = document.querySelectorAll('input[name="'+name+'"]');
    for (var i = 0; i < r.length; i++) if (r[i].checked) return r[i].value;
    return '';
}

function startDownload() {
    if (urls.length === 0) { alert('Добавьте хотя бы один плейлист.'); return; }
    var folder = document.getElementById('folder').value.trim();
    if (!folder) { alert('Укажите папку.'); return; }
    document.getElementById('downloadBtn').disabled = true;
    document.getElementById('log').textContent = '';
    fetch('/api/download', { method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ urls: urls, folder: folder, order: getRadio('order'), mode: getRadio('mode') })
    }).then(r => r.json()).then(d => {
        if (!d.ok) { alert(d.error || 'Ошибка'); document.getElementById('downloadBtn').disabled = false; }
        else pollProgress();
    });
}

function pollProgress() {
    fetch('/api/status').then(r => r.json()).then(d => {
        var pct = d.total > 0 ? Math.round(d.progress / d.total * 100) : 0;
        document.getElementById('progressFill').style.width = pct + '%';
        document.getElementById('progressText').textContent =
            d.total > 0 ? d.progress + '/' + d.total + ' (' + pct + '%)' : '';
        document.getElementById('log').textContent = d.log.join('\\n');
        document.getElementById('log').scrollTop = document.getElementById('log').scrollHeight;
        if (d.running) setTimeout(pollProgress, 500);
        else {
            document.getElementById('downloadBtn').disabled = false;
            if (d.done) document.getElementById('progressText').textContent = 'Готово!';
        }
    });
}

// Проверяем авторизацию при загрузке
fetch('/api/status').then(r => r.json()).then(d => {
    if (d.authenticated) {
        document.getElementById('authStatus').className = 'status status-ok';
        document.getElementById('authStatus').textContent = 'Авторизован';
    }
});
</script>
</body>
</html>"""


# ──────────────────── HTTP Server ────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            auth_url = (
                "https://oauth.vk.com/authorize?"
                "client_id={}&scope=audio&"
                "redirect_uri=https://oauth.vk.com/blank.html&"
                "response_type=token&v=5.131"
            ).format(VK_APP_ID)
            default_folder = str(Path.cwd() / "VK_Music")
            page = HTML_PAGE.replace("AUTH_URL", auth_url).replace("FOLDER_DEFAULT", html.escape(default_folder))
            self._respond(200, "text/html", page.encode("utf-8"))
        elif self.path == "/api/status":
            data = {
                "authenticated": state["service"] is not None,
                "running": state["running"],
                "done": state["done"],
                "progress": state["progress"],
                "total": state["total"],
                "log": state["log"][-200:],
            }
            self._respond_json(data)
        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body) if body else {}
        except Exception:
            self._respond_json({"ok": False, "error": "Bad JSON"})
            return

        if self.path == "/api/auth":
            raw = data.get("url", "")
            m = re.search(r'access_token=([a-f0-9]+)', raw)
            token = m.group(1) if m else (raw if re.match(r'^[a-f0-9]{50,}$', raw) else None)
            if not token:
                self._respond_json({"ok": False, "error": "Не удалось извлечь токен из URL."})
                return
            if not validate_token(token):
                self._respond_json({"ok": False, "error": "Токен невалиден или нет доступа к audio API."})
                return
            save_token(token)
            state["service"] = make_service(token)
            self._respond_json({"ok": True})

        elif self.path == "/api/download":
            if state["running"]:
                self._respond_json({"ok": False, "error": "Загрузка уже идёт."})
                return
            if not state["service"]:
                self._respond_json({"ok": False, "error": "Сначала авторизуйтесь."})
                return
            urls = data.get("urls", [])
            folder = data.get("folder", "")
            order = data.get("order", "normal")
            mode = data.get("mode", "new")
            if not urls:
                self._respond_json({"ok": False, "error": "Нет плейлистов."})
                return
            t = threading.Thread(target=download_worker, args=(urls, folder, order, mode), daemon=True)
            t.start()
            self._respond_json({"ok": True})
        else:
            self._respond_json({"ok": False, "error": "Unknown endpoint"})

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._respond(200, "application/json", body)

    def log_message(self, format, *args):
        pass  # тихий сервер


def main():
    # Проверяем сохранённый токен
    token = load_token()
    if token and validate_token(token):
        state["service"] = make_service(token)
        print("[OK] Токен авторизации загружен.")

    server = HTTPServer(("127.0.0.1", SERVER_PORT), Handler)
    url = "http://127.0.0.1:{}".format(SERVER_PORT)
    print("Сервер запущен: " + url)
    print("Открываю браузер...")
    webbrowser.open(url)
    print("Для остановки нажмите Ctrl+C")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        server.server_close()


if __name__ == "__main__":
    main()
