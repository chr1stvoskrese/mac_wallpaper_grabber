#!/usr/bin/env python3
"""
mac_wallpaper_grabber.py — находит и забирает ВСЁ, что вы видите в Настройках
macOS в разделах «Обои» и «Заставка».

Поддержка: macOS 26 Tahoe (включая новые пути 26.2+), Sequoia 15, Sonoma 14.
В Tahoe хранилище переехало — скрипт знает все варианты:
  • скачанные аэриалы:  ~/Library/Application Support/com.apple.wallpaper/aerials[/videos]
  • встроенные ролики:  /System/Library/Desktop Pictures/.wallpapers/videos  (скрытая папка)
  • дефолтные обои:     /System/Library/Wallpapers  (включая скрытую .default)
  • кэш агента обоев:   ~/Library/Containers/com.apple.wallpaper.agent/…/com.apple.wallpaper.caches
  • старый кэш Sonoma:  /Library/Application Support/com.apple.idleassetsd/Customer/4KSDR240FPS

ИСТОЧНИКИ:

  🎬 aerial     Видео-обои/заставки Aerial:
                  манифест  /Library/Application Support/com.apple.idleassetsd/Customer/entries.json
                  кэш       .../Customer/4KSDR240FPS  (то, что система уже скачала)
                  сеть      прямые 4K SDR/HDR ссылки на sylvan.apple.com
  🖼  ondemand   Статичные обои «по требованию» (Macintosh, Sequoia, Sonoma и т.д.):
                  кэш       /System|/Library/AssetsV2/com_apple_MobileAsset_DesktopPicture
                  сеть      официальный каталог MobileAsset на mesu.apple.com
                            → можно скачать ВСЕ, даже те, что ещё не нажимали в Настройках
  🏞  static     Классические обои /System/Library/Desktop Pictures
                  (включая динамические .heic со сменой времени суток)
  🌌 collections Коллекции заставок «Default Collections»
                  (National Geographic, Aerial, Cosmos, Nature Patterns — старые macOS)
  👤 user        Обои, настроенные лично вами сейчас
                  (com.apple.wallpaper/Store/Index.plist + старая база Dock desktoppicture.db)
  📸 snapshots   Превью-кадры аэриалов (маленькие jpg/heic для миниатюр в Настройках)
  🧩 savers      Инвентаризация установленных заставок (.saver / .appex) — отчёт в манифест

УМНОСТИ:
  • интерактивный мастер: источники → категории → отдельные ролики → папка → план
  • нативный выбор папки через Finder (osascript)
  • оценка объёма сетевых загрузок (HEAD-запросы) и проверка свободного места
  • докачка после обрыва (HTTP Range + .part), ретраи, параллельность
  • пропуск уже существующих файлов, дедупликация по asset-id
  • манифест _manifest.json со всем найденным (пути, URL, размеры, источники)
  • полностью на стандартной библиотеке

НЕИНТЕРАКТИВНО:
  python3 mac_wallpaper_grabber.py --no-interactive --sources aerial,static --local-only
  python3 mac_wallpaper_grabber.py --no-interactive --sources ondemand --dest ~/Pictures/wp
  python3 mac_wallpaper_grabber.py --list            # только показать инвентарь
  python3 mac_wallpaper_grabber.py --export inv.json # выгрузить инвентарь в JSON
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import platform
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ════════════════════════════════════════════════════════════════ constants

HOME = Path.home()

IDLE_DIR = Path("/Library/Application Support/com.apple.idleassetsd")
CUSTOMER_DIR = IDLE_DIR / "Customer"
ENTRIES_JSON = CUSTOMER_DIR / "entries.json"
WALLPAPER_APP_SUPPORT = HOME / "Library/Application Support/com.apple.wallpaper"
WALLPAPER_AGENT_CACHE = (HOME / "Library/Containers/com.apple.wallpaper.agent"
                         / "Data/Library/Caches/com.apple.wallpaper.caches")

# где могут лежать .mov аэриалов (порядок = приоритет; сканируются все существующие)
LOCAL_VIDEO_DIRS = [
    WALLPAPER_APP_SUPPORT / "aerials" / "videos",   # Tahoe 26.x: скачанные пользователем
    WALLPAPER_APP_SUPPORT / "aerials",              # Tahoe: корень (бывают и тут)
    Path("/System/Library/Desktop Pictures/.wallpapers/videos"),  # Tahoe: встроенные
    Path("/System/Library/Desktop Pictures/.wallpapers"),
    WALLPAPER_AGENT_CACHE,                          # Tahoe: кэш агента обоев
    CUSTOMER_DIR / "4KSDR240FPS",                   # Sonoma/Sequoia + остатки после апгрейда
]
SNAPSHOT_DIRS = [
    IDLE_DIR / "snapshots", CUSTOMER_DIR / "snapshots",
    WALLPAPER_APP_SUPPORT / "aerials" / "snapshots",
    WALLPAPER_APP_SUPPORT / "snapshots",
]
STRINGS_BUNDLE = CUSTOMER_DIR / "TVIdleScreenStrings.bundle"

DESKTOP_PICTURES = Path("/System/Library/Desktop Pictures")
STATIC_ROOTS = [
    DESKTOP_PICTURES,                               # классика + скрытая .wallpapers внутри
    Path("/System/Library/Wallpapers"),             # Tahoe/Sequoia: дефолтные (внутри .default)
]
DEFAULT_COLLECTIONS = [
    Path("/System/Library/Screen Savers/Default Collections"),
    Path("/Library/Screen Savers/Default Collections"),
]
ASSETSV2_DIRS = [
    Path("/System/Library/AssetsV2/com_apple_MobileAsset_DesktopPicture"),
    Path("/Library/AssetsV2/com_apple_MobileAsset_DesktopPicture"),
    Path("/private/var/db/AssetsV2/com_apple_MobileAsset_DesktopPicture"),
    HOME / "Library/Application Support/com.apple.mobileAssetDesktop",  # Sequoia
    WALLPAPER_APP_SUPPORT,   # Tahoe: скачанные статичные тоже оседают здесь
]
MESU_CATALOG = ("https://mesu.apple.com/assets/macos/"
                "com_apple_MobileAsset_DesktopPicture/"
                "com_apple_MobileAsset_DesktopPicture.xml")

SAVER_DIRS = [
    Path("/System/Library/Screen Savers"),
    Path("/Library/Screen Savers"),
    Path.home() / "Library/Screen Savers",
    Path("/System/Library/ExtensionKit/Extensions"),
]

WALLPAPER_STORE = Path.home() / "Library/Application Support/com.apple.wallpaper/Store/Index.plist"
DOCK_DB = Path.home() / "Library/Application Support/Dock/desktoppicture.db"

IMAGE_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".webp"}
VIDEO_EXT = {".mov", ".mp4", ".m4v"}

URL_KEYS = {
    "sdr": ["url-4K-SDR-240FPS", "url-4K-SDR", "url-1080-SDR"],
    "hdr": ["url-4K-HDR", "url-4K-SDR-240FPS", "url-4K-SDR"],
    "all": ["url-4K-SDR-240FPS", "url-4K-HDR", "url-4K-SDR",
            "url-1080-SDR", "url-1080-HDR", "url-1080-H264"],
}

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) WallpaperGrabber/3.0"}
CHUNK = 1024 * 512

BOLD, DIM, GREEN, YELLOW, CYAN, RED, RESET = ("\033[1m", "\033[2m", "\033[32m",
                                              "\033[33m", "\033[36m", "\033[31m", "\033[0m")

SOURCES_META = {
    "aerial":      ("🎬", "Видео-обои Aerial (кэш + сеть Apple)"),
    "ondemand":    ("🖼 ", "Обои «по требованию» из Настроек (кэш + каталог Apple)"),
    "static":      ("🏞 ", "Классические обои /System/Library/Desktop Pictures"),
    "collections": ("🌌", "Коллекции заставок Default Collections"),
    "user":        ("👤", "Ваши текущие обои (настроенные вручную)"),
    "snapshots":   ("📸", "Превью-кадры аэриалов (миниатюры)"),
    "savers":      ("🧩", "Список установленных заставок (только отчёт)"),
}


# ════════════════════════════════════════════════════════════════ model

@dataclass
class Item:
    """Единица контента: локальный файл и/или URL для скачивания."""
    source: str                 # aerial / ondemand / static / ...
    kind: str                   # video / image / archive / bundle
    name: str
    category: str = ""
    local: str | None = None    # путь к локальному файлу (str для JSON-манифеста)
    url: str = ""
    size: int = 0               # известный размер (локальный или из каталога)
    asset_id: str = ""

    @property
    def local_path(self) -> Path | None:
        return Path(self.local) if self.local else None

    @property
    def safe_name(self) -> str:
        s = re.sub(r'[\\/:*?"<>|]+', "_", self.name).strip().strip(".")
        return s[:120] or (self.asset_id or "item")

    def target(self, root: Path) -> Path:
        sub = {
            "aerial": "Video Wallpapers",
            "ondemand": "Apple Wallpapers (on-demand)",
            "static": "Static Wallpapers",
            "collections": "Screen Saver Collections",
            "user": "My Current Wallpapers",
            "snapshots": "Aerial Previews",
        }.get(self.source, self.source)
        ext = (Path(self.local).suffix if self.local
               else (".mov" if self.kind == "video" else Path(self.url).suffix or ".bin"))
        folder = root / sub / self.category if self.category else root / sub
        return folder / f"{self.safe_name}{ext}"

    @property
    def status(self) -> str:
        if self.local:
            return f"{GREEN}💾 кэш{RESET}"
        return f"{CYAN}🌐 сеть{RESET}" if self.url else f"{RED}⛔ недоступно{RESET}"


@dataclass
class Report:
    copied: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    bytes_written: int = 0


# ════════════════════════════════════════════════════════════════ helpers

def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def ask(prompt: str, default: str = "") -> str:
    hint = f" {DIM}[{default}]{RESET}" if default else ""
    try:
        val = input(f"{prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nОтмена.")
        sys.exit(0)
    return val or default


def yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    v = ask(f"{prompt} ({d})", "y" if default else "n").lower()
    return v in ("y", "yes", "д", "да", "1")


def parse_selection(s: str, n: int) -> set[int]:
    """'1,3,5-8' → {1,3,5..8};  a/все → всё;  пусто/n → ничего."""
    s = s.strip().lower()
    if s in ("a", "all", "все", "*"):
        return set(range(1, n + 1))
    if s in ("", "n", "none", "нет", "0"):
        return set()
    out: set[int] = set()
    for part in re.split(r"[,\s]+", s):
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                out.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return {i for i in out if 1 <= i <= n}


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def head_size(url: str) -> int:
    try:
        req = urllib.request.Request(url, method="HEAD", headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return int(r.headers.get("Content-Length", 0) or 0)
    except Exception:
        return 0


def file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


# ════════════════════════════════════════════════════════ source: aerial

def _load_localized_names() -> dict[str, str]:
    names: dict[str, str] = {}
    if not STRINGS_BUNDLE.exists():
        return names
    for f in list(STRINGS_BUNDLE.rglob("*.strings")) + list(STRINGS_BUNDLE.rglob("*.loctable")):
        try:
            with open(f, "rb") as fh:
                data = plistlib.load(fh)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, dict):
                        names.update({kk: vv for kk, vv in v.items() if isinstance(vv, str)})
                    elif isinstance(v, str):
                        names[k] = v
        except Exception:
            continue
    return names


def _index_local_videos() -> dict[str, Path]:
    idx: dict[str, Path] = {}
    for d in LOCAL_VIDEO_DIRS:
        if not d.exists():
            continue
        for f in d.rglob("*.mov"):
            m = re.search(r"([0-9A-Fa-f-]{36})", f.name)
            key = (m.group(1) if m else f.stem).upper()
            if key not in idx or file_size(f) > file_size(idx[key]):
                idx[key] = f
    return idx


def _find_entries_json() -> Path | None:
    """entries.json может жить в разных местах в зависимости от версии macOS."""
    candidates = [
        ENTRIES_JSON,                                        # Sonoma/Sequoia/Tahoe
        WALLPAPER_APP_SUPPORT / "aerials" / "entries.json",  # возможный Tahoe-вариант
        WALLPAPER_APP_SUPPORT / "entries.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def discover_aerial(video_mode: str) -> list[Item]:
    names = _load_localized_names()
    local_idx = _index_local_videos()
    items: list[Item] = []
    matched: set[str] = set()

    ej = _find_entries_json()
    if ej:
        try:
            data = json.loads(ej.read_text())
        except Exception:
            data = {}
        cat_by_id = {}
        for cat in data.get("categories", []):
            key = cat.get("localizedNameKey", "")
            cat_by_id[cat.get("id", "")] = names.get(key, key or "Other")
        for a in data.get("assets", []):
            aid = str(a.get("id", "")).upper()
            url = next((a[k] for k in URL_KEYS[video_mode] if a.get(k)), "")
            nk = a.get("localizedNameKey", "")
            name = names.get(nk, nk) or a.get("accessibilityLabel", "") or aid
            cats = a.get("categories") or []
            category = cat_by_id.get(cats[0], "Other") if cats else "Other"
            lp = local_idx.get(aid)
            if lp:
                matched.add(aid)
            items.append(Item("aerial", "video", name, category,
                              str(lp) if lp else None, url,
                              file_size(lp) if lp else 0, aid))

    # «Сироты»: ролики на диске, которых нет в манифесте (типично для Tahoe,
    # где встроенные и скачанные .mov лежат в новых папках без entries.json)
    for key, path in sorted(local_idx.items()):
        if key in matched:
            continue
        p = str(path)
        if p.startswith("/System/"):
            category = "Built-in (Tahoe)"
        elif "wallpaper.agent" in p:
            category = "Agent cache"
        else:
            category = "Downloaded (local)"
        name = names.get(path.stem, "") or (path.stem if not re.fullmatch(
            r"[0-9A-Fa-f-]{36}.*", path.stem) else f"Aerial {key[:8]}")
        items.append(Item("aerial", "video", name, category,
                          p, "", file_size(path), key))
    return items


# ════════════════════════════════════════ source: on-demand (MobileAsset)

def discover_ondemand_local() -> list[Item]:
    """Уже скачанные Настройками статичные обои (кэш AssetsV2)."""
    items: list[Item] = []
    seen: set[str] = set()
    for base in ASSETSV2_DIRS:
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.suffix.lower() in IMAGE_EXT and f.is_file():
                low = str(f).lower()
                if "/aerials/" in low or "/snapshots/" in low or "/store/" in low:
                    continue  # это территория источников aerial/snapshots/user
                name = f.stem
                key = f"{name}|{file_size(f)}"
                if key in seen:
                    continue
                seen.add(key)
                items.append(Item("ondemand", "image", name, "",
                                  str(f), "", file_size(f), name))
    return items


def fetch_ondemand_catalog() -> list[Item]:
    """Каталог ВСЕХ обоев «по требованию» с mesu.apple.com (плюс к локальным)."""
    try:
        raw = fetch_bytes(MESU_CATALOG, timeout=30)
        data = plistlib.loads(raw)
    except Exception as e:
        print(f"{YELLOW}   ⚠️  Каталог mesu.apple.com недоступен: {e}{RESET}")
        return []
    items: list[Item] = []
    for a in data.get("Assets", []):
        base, rel = a.get("__BaseURL", ""), a.get("__RelativePath", "")
        if not base or not rel:
            continue
        # имя: ищем самый человеческий ключ
        name = ""
        for k in ("WallpaperIdentifier", "WallpaperName", "AssetSpecifier", "Slice"):
            if isinstance(a.get(k), str):
                name = a[k]
                break
        if not name:
            name = Path(rel).stem
        items.append(Item("ondemand", "archive", name, "",
                          None, base + rel,
                          int(a.get("_DownloadSize", 0) or 0), name))
    return items


# ════════════════════════════════════════════ sources: локальные папки

def _folder_items(source: str, roots: list[Path], exts: set[str],
                  keep_rel_category: bool = True) -> list[Item]:
    items: list[Item] = []
    for root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in exts:
                continue
            rel = f.relative_to(root)
            category = str(rel.parent) if keep_rel_category and str(rel.parent) != "." else ""
            items.append(Item(source, "image", f.stem, category,
                              str(f), "", file_size(f), f.stem))
    return items


def discover_static() -> list[Item]:
    items = _folder_items("static", STATIC_ROOTS, IMAGE_EXT)
    # дедуп между корнями (один файл может быть виден дважды через симлинки)
    seen: set[tuple[str, int]] = set()
    out = []
    for i in items:
        key = (i.name, i.size)
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out


def discover_collections() -> list[Item]:
    return _folder_items("collections", DEFAULT_COLLECTIONS, IMAGE_EXT)


def discover_snapshots() -> list[Item]:
    return _folder_items("snapshots", SNAPSHOT_DIRS, IMAGE_EXT | VIDEO_EXT,
                         keep_rel_category=False)


# ═══════════════════════════════════ source: пользовательские обои

def _walk_plist_for_paths(obj, out: set[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _walk_plist_for_paths(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_plist_for_paths(v, out)
    elif isinstance(obj, bytes):
        try:  # Sonoma прячет вложенные бинарные plist внутри data-блобов
            _walk_plist_for_paths(plistlib.loads(obj), out)
        except Exception:
            pass
    elif isinstance(obj, str):
        s = obj
        if s.startswith("file://"):
            s = urllib.request.url2pathname(s[7:])
        if any(s.lower().endswith(e) for e in IMAGE_EXT | VIDEO_EXT):
            out.add(s)


def discover_user() -> list[Item]:
    paths: set[str] = set()
    # Sonoma+: единое хранилище настроек обоев
    if WALLPAPER_STORE.exists():
        try:
            with open(WALLPAPER_STORE, "rb") as fh:
                _walk_plist_for_paths(plistlib.load(fh), paths)
        except Exception:
            pass
    # Pre-Ventura: sqlite-база Dock
    if DOCK_DB.exists():
        try:
            con = sqlite3.connect(f"file:{DOCK_DB}?mode=ro", uri=True)
            for (v,) in con.execute("SELECT value FROM data"):
                if isinstance(v, str) and any(v.lower().endswith(e) for e in IMAGE_EXT):
                    paths.add(v)
            con.close()
        except Exception:
            pass
    items = []
    for p in sorted(paths):
        f = Path(p).expanduser()
        if f.exists() and not str(f).startswith(("/System/", "/Library/Application Support/com.apple.idleassetsd")):
            items.append(Item("user", "image", f.stem, "", str(f), "", file_size(f), f.stem))
    return items


# ═══════════════════════════════════ source: заставки (инвентаризация)

def discover_savers() -> list[Item]:
    items: list[Item] = []
    for d in SAVER_DIRS:
        if not d.exists():
            continue
        for b in d.iterdir():
            if b.suffix in (".saver", ".appex") and b.is_dir():
                items.append(Item("savers", "bundle", b.stem,
                                  str(d), str(b), "", 0, b.stem))
    return items


# ════════════════════════════════════════════════════════════ discovery

def discover_all(video_mode: str, with_catalog: bool = False) -> dict[str, list[Item]]:
    inv: dict[str, list[Item]] = {
        "aerial": discover_aerial(video_mode),
        "ondemand": discover_ondemand_local(),
        "static": discover_static(),
        "collections": discover_collections(),
        "user": discover_user(),
        "snapshots": discover_snapshots(),
        "savers": discover_savers(),
    }
    if with_catalog:
        local_names = {i.name for i in inv["ondemand"]}
        for it in fetch_ondemand_catalog():
            if it.name not in local_names:
                inv["ondemand"].append(it)
    return inv


def print_inventory(inv: dict[str, list[Item]]) -> None:
    print(f"\n{BOLD}📋 Инвентарь найденного:{RESET}")
    for key, (icon, label) in SOURCES_META.items():
        items = inv.get(key, [])
        n = len(items)
        cached = sum(1 for i in items if i.local)
        size = sum(i.size for i in items if i.local)
        net = sum(1 for i in items if not i.local and i.url)
        parts = []
        if cached:
            parts.append(f"локально {cached} ({human(size)})")
        if net:
            parts.append(f"в сети {net}")
        detail = ", ".join(parts) if parts else ("ничего" if n == 0 else f"{n} шт.")
        mark = GREEN + "●" + RESET if n else DIM + "○" + RESET
        print(f"  {mark} {icon} {label:<55} {DIM}{detail}{RESET}")


# ════════════════════════════════════════════════════ interactive wizard

def choose_folder_dialog() -> Path | None:
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose folder with prompt "Куда сохранить обои macOS?")'],
            capture_output=True, text=True, timeout=600)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except Exception:
        pass
    return None


def pick_destination() -> Path:
    print(f"\n{BOLD}📂 Куда складывать?{RESET}")
    options = [
        ("Загрузки", Path.home() / "Downloads" / "macOS Wallpapers"),
        ("Рабочий стол", Path.home() / "Desktop" / "macOS Wallpapers"),
        ("Картинки", Path.home() / "Pictures" / "macOS Wallpapers"),
    ]
    for i, (label, p) in enumerate(options, 1):
        print(f"  {i}. {label:<14} {DIM}{p}{RESET}")
    print("  4. Выбрать папку в Finder…")
    print("  5. Ввести путь вручную")
    while True:
        c = ask("Вариант", "1")
        if c in ("1", "2", "3"):
            return options[int(c) - 1][1]
        if c == "4":
            p = choose_folder_dialog()
            if p:
                return p / "macOS Wallpapers"
            print(f"{YELLOW}Диалог отменён, попробуйте другой вариант.{RESET}")
        elif c == "5":
            raw = ask("Путь")
            if raw:
                return Path(raw).expanduser()
        else:
            print("Введите число 1–5.")


def refine_aerial(items: list[Item]) -> list[Item]:
    by_cat: dict[str, list[Item]] = {}
    for a in items:
        by_cat.setdefault(a.category, []).append(a)
    by_cat = {k: sorted(v, key=lambda x: x.name) for k, v in sorted(by_cat.items())}
    cats = list(by_cat)

    print(f"\n{BOLD}🎬 Категории видео-обоев:{RESET}")
    for i, cat in enumerate(cats, 1):
        arr = by_cat[cat]
        cached = sum(1 for a in arr if a.local)
        csize = sum(a.size for a in arr if a.local)
        extra = f", в кэше {cached} ({human(csize)})" if cached else ""
        print(f"  {i:2}. {cat:<28} {DIM}{len(arr)} шт.{extra}{RESET}")
    raw = ask(f"Какие категории? {DIM}(1,3,5-8; a — все; n — пропустить){RESET}", "a")
    chosen = [cats[i - 1] for i in sorted(parse_selection(raw, len(cats)))]
    if not chosen:
        return []

    selected: list[Item] = []
    drill = yes("Выбирать отдельные ролики внутри категорий?", default=False)
    for cat in chosen:
        arr = by_cat[cat]
        if not drill:
            selected.extend(arr)
            continue
        print(f"\n  {BOLD}── {cat} ──{RESET}")
        for i, a in enumerate(arr, 1):
            sz = f" {human(a.size)}" if a.size else ""
            print(f"    {i:2}. {a.name:<40} {a.status}{DIM}{sz}{RESET}")
        raw = ask(f"  Какие из «{cat}»? {DIM}(a — все){RESET}", "a")
        selected.extend(arr[i - 1] for i in sorted(parse_selection(raw, len(arr))))
    return selected


def refine_generic(items: list[Item], title: str) -> list[Item]:
    """Общая выбиралка для плоских списков (обои on-demand, user и т.п.)."""
    if len(items) <= 1 or not yes(f"{title}: выбирать по одному? (иначе возьмём все {len(items)})",
                                  default=False):
        return items
    arr = sorted(items, key=lambda x: (x.category, x.name))
    for i, a in enumerate(arr, 1):
        sz = f" {human(a.size)}" if a.size else ""
        cat = f"{DIM}[{a.category}]{RESET} " if a.category else ""
        print(f"  {i:3}. {cat}{a.name:<45} {a.status}{DIM}{sz}{RESET}")
    raw = ask(f"Какие? {DIM}(1,3,5-8; a — все){RESET}", "a")
    return [arr[i - 1] for i in sorted(parse_selection(raw, len(arr)))]


def estimate_network(items: list[Item], workers: int = 8) -> int:
    """Оценивает объём сетевых загрузок HEAD-запросами (для неизвестных размеров)."""
    unknown = [i for i in items if not i.local and i.url and not i.size]
    known = sum(i.size for i in items if not i.local and i.url and i.size)
    if unknown:
        print(f"{DIM}   … опрашиваю сервер о размерах ({len(unknown)} шт.){RESET}")
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for it, sz in zip(unknown, pool.map(lambda i: head_size(i.url), unknown)):
                it.size = sz
        known += sum(i.size for i in unknown)
    return known


def wizard(args: argparse.Namespace) -> tuple[list[Item], argparse.Namespace]:
    ver = platform.mac_ver()[0] or "?"
    print(f"{BOLD}{CYAN}═══ macOS Wallpaper Grabber · режим «забрать всё» ═══{RESET}")
    print(f"{DIM}macOS {ver} · сканирую пути Tahoe (26.x) + Sequoia/Sonoma одновременно{RESET}")
    if ver.split(".")[0].isdigit() and int(ver.split(".")[0]) >= 26 and not _find_entries_json():
        print(f"{YELLOW}ℹ️  Манифест entries.json не найден — на Tahoe возьмём всё, что уже\n"
              f"   лежит на диске (встроенные + скачанные вами ролики). Чтобы получить\n"
              f"   больше аэриалов, откройте Настройки → Заставка и нажмите ↓ на нужных —\n"
              f"   потом просто запустите скрипт ещё раз, он их подхватит.{RESET}")

    # качество видео влияет на URL — спрашиваем до сканирования
    q = ask(f"{BOLD}Качество видео-обоев{RESET}: 1 — 4K SDR 240fps, 2 — 4K HDR, 3 — все версии", "1")
    args.video = {"1": "sdr", "2": "hdr", "3": "all"}.get(q, "sdr")

    print(f"\n{DIM}Сканирую систему…{RESET}")
    inv = discover_all(args.video, with_catalog=False)

    # спросить про сетевой каталог on-demand обоев
    if yes("Запросить у Apple полный каталог обоев «по требованию»?\n"
           f"  {DIM}(даст скачать даже те, что вы не нажимали в Настройках; 1 запрос к mesu.apple.com){RESET}",
           default=True):
        local_names = {i.name for i in inv["ondemand"]}
        extra = [i for i in fetch_ondemand_catalog() if i.name not in local_names]
        if extra:
            print(f"   {GREEN}+ каталог дал ещё {len(extra)} обоев "
                  f"(~{human(sum(i.size for i in extra))} архивами){RESET}")
        inv["ondemand"].extend(extra)

    print_inventory(inv)

    # выбор источников
    keys = list(SOURCES_META)
    print(f"\n{BOLD}Что забираем?{RESET}")
    for i, k in enumerate(keys, 1):
        icon, label = SOURCES_META[k]
        n = len(inv[k])
        print(f"  {i}. {icon} {label} {DIM}({n}){RESET}")
    raw = ask(f"Источники {DIM}(1,2,5 или a — всё){RESET}", "a")
    chosen_keys = [keys[i - 1] for i in sorted(parse_selection(raw, len(keys)))]
    chosen_keys = [k for k in chosen_keys if inv[k]]

    # уточнение внутри источников
    picked: list[Item] = []
    for k in chosen_keys:
        if k == "aerial":
            picked += refine_aerial(inv[k])
        elif k == "ondemand":
            picked += refine_generic(inv[k], "🖼  Обои «по требованию»")
        elif k == "user":
            picked += refine_generic(inv[k], "👤 Ваши обои")
        elif k == "savers":
            picked += inv[k]  # только в манифест
        else:
            picked.append(None)  # placeholder не нужен
            picked.pop()
            picked += inv[k]

    net_items = [i for i in picked if not i.local and i.url]
    if net_items:
        if yes(f"\n{len(net_items)} элементов надо качать из сети. Оценить точный объём?",
               default=True):
            total = estimate_network(net_items)
            print(f"   Сетевые загрузки: {BOLD}{human(total)}{RESET}")
        if not yes("Качать из сети?", default=True):
            picked = [i for i in picked if i.local or not i.url]
            args.local_only = True

    # папка
    args.dest = pick_destination()

    # проверка места
    dl = [i for i in picked if i.kind != "bundle"]
    need = sum(i.size for i in dl)
    try:
        free = shutil.disk_usage(args.dest.anchor or "/").free
        if need and need > free * 0.95:
            print(f"{RED}⚠️  Нужно ~{human(need)}, свободно {human(free)} — может не хватить!{RESET}")
            if not yes("Продолжить всё равно?", default=False):
                sys.exit(0)
    except Exception:
        free = 0

    # план
    cached = sum(1 for i in dl if i.local)
    net = sum(1 for i in dl if not i.local and i.url)
    print(f"\n{BOLD}── План ──{RESET}")
    print(f"  Копировать локально : {cached}")
    print(f"  Скачать из сети     : {net}")
    print(f"  Ожидаемый объём     : ~{human(need)}" + (f"  (свободно {human(free)})" if free else ""))
    print(f"  Папка               : {args.dest}")
    if not yes("Поехали?"):
        sys.exit(0)
    return picked, args


# ════════════════════════════════════════════════════════════ collecting

def download(url: str, dest: Path, label: str, pos: int, total: int) -> int:
    tmp = dest.with_suffix(dest.suffix + ".part")
    got = file_size(tmp)
    req = urllib.request.Request(url, headers=UA)
    if got:
        req.add_header("Range", f"bytes={got}-")
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            tmp.rename(dest)
            return file_size(dest)
        raise
    total_size = got + int(resp.headers.get("Content-Length", 0) or 0)
    mode = "ab" if got and resp.status == 206 else "wb"
    if mode == "wb":
        got = 0
    start = time.time()
    with open(tmp, mode) as out:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            speed = got / max(time.time() - start, 0.1)
            pct = f"{got / total_size * 100:5.1f}%" if total_size else "   ?  "
            sys.stdout.write(f"\r  [{pos}/{total}] {label[:36]:<36} {pct}  "
                             f"{human(got):>9}/{human(total_size):>9}  {human(speed)}/s   ")
            sys.stdout.flush()
    sys.stdout.write("\n")
    tmp.rename(dest)
    return got


def extract_archive(archive: Path, into: Path) -> int:
    """Достаёт картинки из скачанного zip MobileAsset."""
    n = 0
    try:
        with zipfile.ZipFile(archive) as z:
            for zi in z.infolist():
                if zi.is_dir():
                    continue
                if Path(zi.filename).suffix.lower() in IMAGE_EXT:
                    data = z.read(zi)
                    out = into / Path(zi.filename).name
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(data)
                    n += 1
    except zipfile.BadZipFile:
        pass
    return n


def collect(items: list[Item], root: Path, report: Report, workers: int) -> None:
    work = [i for i in items if i.kind != "bundle"]
    locals_ = [i for i in work if i.local]
    nets = [i for i in work if not i.local and i.url]

    if locals_:
        print(f"\n💾 Копирую локальные файлы ({len(locals_)})…")
    for it in locals_:
        target = it.target(root)
        if target.exists() and file_size(target) == it.size:
            report.skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(it.local, target)
            report.copied += 1
            report.bytes_written += it.size
        except PermissionError:
            report.failed.append(f"{it.name}: нет доступа")
    if locals_:
        print(f"   ✓ скопировано {report.copied}, пропущено (уже было) {report.skipped}")

    if not nets:
        return
    print(f"\n⬇️  Сетевые загрузки: {len(nets)}, потоков: {workers}")
    total = len(nets)

    def worker(i_it: tuple[int, Item]) -> None:
        i, it = i_it
        target = it.target(root)
        if target.exists() and it.size and file_size(target) == it.size:
            report.skipped += 1
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            try:
                if it.kind == "archive":
                    with tempfile.TemporaryDirectory() as td:
                        tmp_zip = Path(td) / "a.zip"
                        got = download(it.url, tmp_zip, it.name, i, total)
                        n = extract_archive(tmp_zip, target.parent / it.safe_name)
                        if n == 0:  # не zip или без картинок — сохраняем как есть
                            shutil.copy2(tmp_zip, target)
                        report.bytes_written += got
                else:
                    report.bytes_written += download(it.url, target, it.name, i, total)
                report.downloaded += 1
                return
            except Exception as e:
                if attempt == 2:
                    report.failed.append(f"{it.name}: {e}")
                    print(f"\n   ✗ не удалось: {it.name} ({e})")
                else:
                    time.sleep(2 * (attempt + 1))

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, enumerate(nets, 1)))


def write_manifest(items: list[Item], root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "_manifest.json").write_text(json.dumps(
            [asdict(i) for i in items], ensure_ascii=False, indent=2))
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════ main

def main() -> None:
    if sys.platform != "darwin":
        sys.exit("Этот скрипт нужно запускать на macOS.")

    p = argparse.ArgumentParser(
        description="Находит и скачивает всё из «Обои» и «Заставка» macOS")
    p.add_argument("--dest", type=Path, default=Path.home() / "Downloads" / "macOS Wallpapers")
    p.add_argument("--no-interactive", action="store_true")
    p.add_argument("--sources", default="aerial,ondemand,static,collections,user,snapshots,savers",
                   help="через запятую: " + ",".join(SOURCES_META))
    p.add_argument("--video", choices=["sdr", "hdr", "all"], default="sdr")
    p.add_argument("--local-only", action="store_true", help="без сети")
    p.add_argument("--with-catalog", action="store_true",
                   help="запросить полный каталог on-demand обоев у Apple")
    p.add_argument("--category", default="", help="фильтр по подстроке (без интерактива)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--list", action="store_true", help="показать инвентарь и выйти")
    p.add_argument("--export", type=Path, help="выгрузить инвентарь в JSON и выйти")
    args = p.parse_args()

    if args.list or args.export:
        inv = discover_all(args.video, with_catalog=args.with_catalog)
        print_inventory(inv)
        if args.export:
            flat = [asdict(i) for arr in inv.values() for i in arr]
            args.export.write_text(json.dumps(flat, ensure_ascii=False, indent=2))
            print(f"\n💾 Инвентарь сохранён: {args.export} ({len(flat)} записей)")
        return

    if not args.no_interactive and sys.stdin.isatty():
        items, args = wizard(args)
    else:
        inv = discover_all(args.video, with_catalog=args.with_catalog and not args.local_only)
        wanted = [s.strip() for s in args.sources.split(",") if s.strip() in SOURCES_META]
        items = [i for k in wanted for i in inv[k]]
        if args.category:
            q = args.category.lower()
            items = [i for i in items if q in i.name.lower() or q in i.category.lower()]
        if args.local_only:
            items = [i for i in items if i.local or i.kind == "bundle"]

    report = Report()
    collect(items, args.dest, report, args.workers)
    write_manifest(items, args.dest)

    total_size = sum(file_size(f) for f in args.dest.rglob("*") if f.is_file()) \
        if args.dest.exists() else 0
    print(f"\n{BOLD}────────── ИТОГО ──────────{RESET}")
    print(f"  Скопировано локально : {report.copied}")
    print(f"  Скачано из сети      : {report.downloaded}")
    print(f"  Пропущено (уже есть) : {report.skipped}")
    print(f"  Ошибок               : {len(report.failed)}")
    print(f"  Записано за сессию   : {human(report.bytes_written)}")
    print(f"  Размер папки итого   : {human(total_size)}")
    print(f"  Манифест             : {args.dest / '_manifest.json'}")
    for f in report.failed[:10]:
        print(f"    {RED}✗{RESET} {f}")
    if report.copied or report.downloaded:
        subprocess.run(["open", str(args.dest)], check=False)


if __name__ == "__main__":
    main()
