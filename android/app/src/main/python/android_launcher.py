"""
Android launcher for Vinyl Player.
Patches incompatible stdlib calls before importing the main module.
"""
import os
import sys

# ── 1. Set HOME to Android internal storage ──
# Must happen before `import vinyl_player` because module-level
# Path.home() calls resolve USERS_FILE, SETTINGS_FILE, etc.
android_home = os.environ.get("VINYL_HOME", "")
if android_home:
    os.environ["HOME"] = android_home
    from pathlib import Path
    Path(android_home).mkdir(parents=True, exist_ok=True)

# ── 2. Patch webbrowser (no browser on Android) ──
import webbrowser
webbrowser.open = lambda *a, **kw: None

# ── 3. Patch subprocess — block Android-incompatible binaries ──
import subprocess

_blocked_cmds = {"ifconfig", "openssl", "pkill", "cloudflared", "ip"}
_orig_check_output = subprocess.check_output
_orig_run = subprocess.run
_orig_popen = subprocess.Popen


def _get_cmd_name(cmd):
    if isinstance(cmd, (list, tuple)) and cmd:
        return os.path.basename(str(cmd[0]))
    if isinstance(cmd, str):
        return os.path.basename(cmd.split()[0]) if cmd.strip() else ""
    return ""


def _patched_check_output(cmd, **kwargs):
    if _get_cmd_name(cmd) in _blocked_cmds:
        raise FileNotFoundError(f"{_get_cmd_name(cmd)}: not available on Android")
    return _orig_check_output(cmd, **kwargs)


def _patched_run(cmd, **kwargs):
    if _get_cmd_name(cmd) in _blocked_cmds:
        raise FileNotFoundError(f"{_get_cmd_name(cmd)}: not available on Android")
    return _orig_run(cmd, **kwargs)


class _PatchedPopen(_orig_popen):
    def __init__(self, cmd, *args, **kwargs):
        if _get_cmd_name(cmd) in _blocked_cmds:
            raise FileNotFoundError(f"{_get_cmd_name(cmd)}: not available on Android")
        super().__init__(cmd, *args, **kwargs)


subprocess.check_output = _patched_check_output
subprocess.run = _patched_run
subprocess.Popen = _PatchedPopen

# ── 4. Patch signal.SIGHUP (not available on Android/Windows) ──
import signal
if not hasattr(signal, 'SIGHUP'):
    signal.SIGHUP = None

# ── 5. Import the app ──
import vinyl_player

# ── 6. Post-import patches ──

# Redirect music root to Android external storage
_music_dir = os.environ.get("VINYL_MUSIC_DIR", "")
if _music_dir:
    _orig_get_music_root = vinyl_player.get_music_root
    def _android_get_music_root():
        root = _orig_get_music_root()
        home_music = str(vinyl_player.Path.home() / "VinylMusic")
        if root == home_music:
            return _music_dir
        return root
    vinyl_player.get_music_root = _android_get_music_root


def start_server():
    """Called from Kotlin. Starts the HTTP server and returns port."""
    vinyl_player._load_sessions()
    vinyl_player.IS_PUBLIC = False
    vinyl_player._use_https = False

    s = vinyl_player.load_settings()
    s["lan"] = False
    s["https"] = False
    vinyl_player.save_settings(s)

    vinyl_player._start_server("127.0.0.1")
    return vinyl_player.SERVER_PORT
