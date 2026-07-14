#!/usr/bin/env python3
"""
mac_wallpaper_grabber.py — скачивает обои macOS в выбранную папку.

Всё как в Настройках: выбираете тип — видео-обои (Aerial) или обычные
обои, отмечаете нужные, указываете папку — готово.

Поддержка: macOS Sonoma 14, Sequoia 15, Tahoe 26.
Только стандартная библиотека Python.

Запуск:  python3 mac_wallpaper_grabber.py
"""

from __future__ import annotations

import json
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()

# ── где искать видео-обои (Aerial) ──────────────────────────────────────
CUSTOMER_DIR = Path("/Library/Application Support/com.apple.idleassetsd/Customer")
WALLPAPER_APP_SUPPORT = HOME / "Library/Application Support/com.apple.wallpaper"

ENTRIES_JSON_CANDIDATES = [
    CUSTOMER_DIR / "entries.json",                       # Sonoma/Sequoia/Tahoe
    WALLPAPER_APP_SUPPORT / "aerials" / "entries.json",  # Tahoe
    WALLPAPER_APP_SUPPORT / "entries.json",
]
LOCAL_VIDEO_DIRS = [
    WALLPAPER_APP_SUPPORT / "aerials" / "videos",        # Tahoe: скачанные
    WALLPAPER_APP_SUPPORT / "aerials",
    Path("/System/Library/Desktop Pictures/.wallpapers/videos"),  # Tahoe: встроенные
    Path("/System/Library/Desktop Pictures/.wallpapers"),
    CUSTOMER_DIR / "4KSDR240FPS",                        # Sonoma/Sequoia
]
STRINGS_BUNDLE = CUSTOMER_DIR / "TVIdleScreenStrings.bundle"
VIDEO_URL_KEYS = ["url-4K-SDR-240FPS", "url-4K-SDR", "url-1080-SDR"]

# ── где искать обычные обои ─────────────────────────────────────────────
STATIC_ROOTS = [
    Path("/System/Library/Desktop Pictures"),
    Path("/System/Library/Wallpapers"),                  # Tahoe/Sequoia
]
IMAGE_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) WallpaperGrabber/4.0"}
BOLD, DIM, GREEN, CYAN, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[36m", "\033[31m", "\033[0m"


@dataclass
class Wallpaper:
    name: str
    local: Path | None = None   # файл уже есть на диске
    url: str = ""               # или его можно скачать у Apple
    size: int = 0

    @property
    def status(self) -> str:
        return f"{GREEN}на диске{RESET}" if self.local else f"{CYAN}из сети{RESET}"

    @property
    def filename(self) -> str:
        safe = re.sub(r'[\\/:*?"<>|]+', "_", self.name).strip() or "wallpaper"
        ext = self.local.suffix if self.local else ".mov"
        return safe + ext


# ── мелкие помощники ────────────────────────────────────────────────────

def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def ask(prompt: str, default: str = "") -> str:
    hint = f" {DIM}[{default}]{RESET}" if default else ""
    try:
        return input(f"{prompt}{hint}: ").strip() or default
    except (EOFError, KeyboardInterrupt):
        print("\nОтмена.")
        sys.exit(0)


def parse_selection(s: str, n: int) -> list[int]:
    """'1,3,5-8' → [1,3,5,6,7,8];  'a'/'все' → всё."""
    s = s.strip().lower()
    if s in ("a", "all", "все", "*", ""):
        return list(range(1, n + 1))
    out: set[int] = set()
    for part in re.split(r"[,\s]+", s):
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                out.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(i for i in out if 1 <= i <= n)


def file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


# ── поиск видео-обоев ───────────────────────────────────────────────────

def _localized_names() -> dict[str, str]:
    """Человеческие имена аэриалов из системного бандла локализации."""
    names: dict[str, str] = {}
    if not STRINGS_BUNDLE.exists():
        return names
    for f in STRINGS_BUNDLE.rglob("*.strings"):
        try:
            data = plistlib.load(open(f, "rb"))
            names.update({k: v for k, v in data.items() if isinstance(v, str)})
        except Exception:
            continue
    return names


def find_video_wallpapers() -> list[Wallpaper]:
    names = _localized_names()

    # видео, которые уже лежат на диске (ключ — UUID из имени файла)
    local: dict[str, Path] = {}
    for d in LOCAL_VIDEO_DIRS:
        if not d.exists():
            continue
        for f in d.rglob("*.mov"):
            m = re.search(r"([0-9A-Fa-f-]{36})", f.name)
            key = (m.group(1) if m else f.stem).upper()
            if key not in local or file_size(f) > file_size(local[key]):
                local[key] = f

    items: list[Wallpaper] = []
    seen: set[str] = set()

    # манифест Apple: имена + ссылки на скачивание
    for ej in ENTRIES_JSON_CANDIDATES:
        if not ej.exists():
            continue
        try:
            data = json.loads(ej.read_text())
        except Exception:
            continue
        for a in data.get("assets", []):
            aid = str(a.get("id", "")).upper()
            url = next((a[k] for k in VIDEO_URL_KEYS if a.get(k)), "")
            key = a.get("localizedNameKey", "")
            name = names.get(key, key) or a.get("accessibilityLabel", "") or aid
            lp = local.get(aid)
            items.append(Wallpaper(name, lp, url, file_size(lp) if lp else 0))
            seen.add(aid)
        break

    # видео на диске, которых нет в манифесте (встроенные в Tahoe)
    for key, path in sorted(local.items()):
        if key not in seen:
            name = names.get(path.stem) or f"Aerial {key[:8]}"
            items.append(Wallpaper(name, path, "", file_size(path)))

    return sorted(items, key=lambda w: w.name)


# ── поиск обычных обоев ─────────────────────────────────────────────────

def find_static_wallpapers() -> list[Wallpaper]:
    items: list[Wallpaper] = []
    seen: set[tuple[str, int]] = set()
    for root in STATIC_ROOTS:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in IMAGE_EXT:
                continue
            key = (f.stem, file_size(f))
            if key not in seen:
                seen.add(key)
                items.append(Wallpaper(f.stem, f, "", file_size(f)))
    return sorted(items, key=lambda w: w.name)


# ── выбор папки ─────────────────────────────────────────────────────────

def pick_folder() -> Path:
    print(f"\n{BOLD}Куда сохранить?{RESET}")
    options = [
        ("Загрузки", HOME / "Downloads" / "macOS Wallpapers"),
        ("Рабочий стол", HOME / "Desktop" / "macOS Wallpapers"),
        ("Картинки", HOME / "Pictures" / "macOS Wallpapers"),
    ]
    for i, (label, p) in enumerate(options, 1):
        print(f"  {i}. {label:<13} {DIM}{p}{RESET}")
    print("  4. Выбрать папку в Finder…")
    while True:
        c = ask("Вариант", "1")
        if c in ("1", "2", "3"):
            return options[int(c) - 1][1]
        if c == "4":
            r = subprocess.run(
                ["osascript", "-e",
                 'POSIX path of (choose folder with prompt "Куда сохранить обои?")'],
                capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return Path(r.stdout.strip())
            print("Диалог отменён, попробуйте ещё раз.")
        else:
            print("Введите число 1–4.")


# ── скачивание ──────────────────────────────────────────────────────────

def download(url: str, dest: Path, label: str) -> None:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length", 0) or 0)
        got, start = 0, time.time()
        while chunk := resp.read(512 * 1024):
            out.write(chunk)
            got += len(chunk)
            pct = f"{got / total * 100:5.1f}%" if total else "  ...  "
            speed = got / max(time.time() - start, 0.1)
            sys.stdout.write(f"\r  {label[:40]:<40} {pct}  {human(speed)}/s   ")
            sys.stdout.flush()
    sys.stdout.write("\n")


def save_all(picked: list[Wallpaper], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    done = failed = skipped = 0
    for w in picked:
        target = folder / w.filename
        if target.exists() and (not w.size or file_size(target) == w.size):
            skipped += 1
            continue
        try:
            if w.local:
                shutil.copy2(w.local, target)
                print(f"  {w.name[:40]:<40} скопировано")
            else:
                download(w.url, target, w.name)
            done += 1
        except Exception as e:
            target.unlink(missing_ok=True)
            failed += 1
            print(f"\n  {RED}✗ {w.name}: {e}{RESET}")

    print(f"\n{BOLD}Готово:{RESET} сохранено {done}, пропущено (уже было) {skipped}"
          + (f", {RED}ошибок {failed}{RESET}" if failed else ""))
    print(f"Папка: {folder}")
    if done:
        subprocess.run(["open", str(folder)], check=False)


# ── main ────────────────────────────────────────────────────────────────

def main() -> None:
    if sys.platform != "darwin":
        sys.exit("Этот скрипт нужно запускать на macOS.")

    print(f"{BOLD}{CYAN}═══ macOS Wallpaper Grabber ═══{RESET}")
    print(f"\n{BOLD}Что скачать?{RESET}")
    print("  1. 🎬 Видео-обои (Aerial)")
    print("  2. 🖼  Обычные обои")
    kind = ask("Вариант", "1")

    print(f"\n{DIM}Ищу обои…{RESET}")
    items = find_video_wallpapers() if kind == "1" else find_static_wallpapers()
    if not items:
        sys.exit("Ничего не нашлось. Для видео-обоев откройте Настройки → Заставка,\n"
                 "скачайте хотя бы одну — и запустите скрипт снова.")

    print(f"\n{BOLD}Найдено {len(items)}:{RESET}")
    for i, w in enumerate(items, 1):
        sz = f" {human(w.size)}" if w.size else ""
        print(f"  {i:3}. {w.name:<42} {w.status}{DIM}{sz}{RESET}")

    raw = ask(f"\nКакие скачать? {DIM}(например 1,3,5-8; Enter — все){RESET}", "все")
    picked = [items[i - 1] for i in parse_selection(raw, len(items))]
    if not picked:
        sys.exit("Ничего не выбрано.")

    folder = pick_folder()
    print()
    save_all(picked, folder)


if __name__ == "__main__":
    main()
