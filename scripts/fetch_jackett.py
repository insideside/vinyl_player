#!/usr/bin/env python3
"""Архивы Jackett для установки из приложения: докачать недостающие и проверить суммы.

Архивы (~50 МБ на платформу) лежат в vendor/jackett, но в git не хранятся - там только
manifest.json (версия, адреса, sha256). Сборки зовут этот скрипт перед PyInstaller:

    python3 scripts/fetch_jackett.py            # все платформы
    python3 scripts/fetch_jackett.py --current  # только эта платформа; печатает путь к архиву
"""
import hashlib
import json
import platform
import sys
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "jackett"


def current_name():
    m = platform.machine().lower()
    arm = m in ("arm64", "aarch64")
    if sys.platform == "darwin":
        return "Jackett.Binaries.macOSARM64.tar.gz" if arm else "Jackett.Binaries.macOS.tar.gz"
    if sys.platform == "win32":
        return "Jackett.Binaries.Windows.zip"
    return "Jackett.Binaries.LinuxARM64.tar.gz" if arm else "Jackett.Binaries.LinuxAMDx64.tar.gz"


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure(name, info):
    p = VENDOR / name
    if p.is_file() and sha256(p) == info["sha256"]:
        return p
    sys.stderr.write("Скачиваю {} ...\n".format(name))
    tmp = p.with_suffix(p.suffix + ".part")
    urllib.request.urlretrieve(info["url"], str(tmp))
    if sha256(tmp) != info["sha256"]:
        tmp.unlink()
        raise SystemExit("Контрольная сумма {} не совпала - архив не принят".format(name))
    tmp.replace(p)
    return p


def main():
    man = json.loads((VENDOR / "manifest.json").read_text())
    files = man["files"]
    if "--current" in sys.argv:
        name = current_name()
        print(ensure(name, files[name]).as_posix())
        return
    for name, info in files.items():
        ensure(name, info)
    sys.stderr.write("Jackett {}: все архивы на месте\n".format(man.get("version")))


if __name__ == "__main__":
    main()
