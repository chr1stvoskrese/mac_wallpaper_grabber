#!/usr/bin/env python3
"""
mac_wallpaper_grabber.py — скачивает ВСЁ, что видно в Настройках macOS
в разделе «Wallpaper», в выбранную папку.

Секции — те же, что в Настройках, с теми же названиями:

    Dynamic Wallpapers   динамические .heic (меняются от времени суток)
    Landscape            видео-обои Aerial
    Cityscape            видео-обои Aerial
    Underwater           видео-обои Aerial
    Earth                видео-обои Aerial
    Mac                  встроенные обои текущей macOS
    Pictures             статичные картинки

Откуда берётся контент:
  • видео и их названия — системный манифест entries.json + уже скачанные
    системой .mov (Sonoma/Sequoia/Tahoe пути) + прямые ссылки Apple
  • Dynamic Wallpapers / Pictures — локальные /System/Library/Desktop
    Pictures + кэши AssetsV2 + полный каталог Apple (mesu.apple.com),
    т.е. скачать можно даже то, что вы ещё не нажимали в Настройках
  • Mac — /System/Library/Wallpapers (включая скрытую .default)

Умеет: докачку после обрыва (HTTP Range + .part), ретраи, параллельные
загрузки, пропуск уже скачанного, распаковку архивов Apple с картинками.
Только стандартная библиотека Python.

Запуск:  python3 mac_wallpaper_grabber.py
Без вопросов: python3 mac_wallpaper_grabber.py --all --dest ~/Pictures/wp
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()

# порядок и названия секций — как в Настройках → Wallpaper
SECTIONS = ["Dynamic Wallpapers", "Landscape", "Cityscape", "Underwater",
            "Earth", "Mac", "Pictures"]

# ── видео-обои (Aerial) ─────────────────────────────────────────────────
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
    HOME / "Library/Containers/com.apple.wallpaper.agent"
         / "Data/Library/Caches/com.apple.wallpaper.caches",
    CUSTOMER_DIR / "4KSDR240FPS",                        # Sonoma/Sequoia
]
STRINGS_BUNDLE = CUSTOMER_DIR / "TVIdleScreenStrings.bundle"
VIDEO_URL_KEYS = {
    "sdr": ["url-4K-SDR-240FPS", "url-4K-SDR", "url-1080-SDR"],
    "hdr": ["url-4K-HDR", "url-4K-SDR-240FPS", "url-4K-SDR"],
}

# ── картинки ────────────────────────────────────────────────────────────
DESKTOP_PICTURES = Path("/System/Library/Desktop Pictures")
MAC_WALLPAPERS_DIR = Path("/System/Library/Wallpapers")   # секция Mac (Tahoe)
ASSETSV2_DIRS = [                                          # кэш уже скачанного Настройками
    Path("/System/Library/AssetsV2/com_apple_MobileAsset_DesktopPicture"),
    Path("/Library/AssetsV2/com_apple_MobileAsset_DesktopPicture"),
    Path("/private/var/db/AssetsV2/com_apple_MobileAsset_DesktopPicture"),
    HOME / "Library/Application Support/com.apple.mobileAssetDesktop",
]
MESU_CATALOG = ("https://mesu.apple.com/assets/macos/"
                "com_apple_MobileAsset_DesktopPicture/"
                "com_apple_MobileAsset_DesktopPicture.xml")

IMAGE_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
MIN_IMAGE_SIZE = 300 * 1024          # отсекаем миниатюры и превью
DYNAMIC_MARKERS = (b"apple_desktop:solar", b"apple_desktop:h24")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) WallpaperGrabber/5.0"}
CHUNK = 512 * 1024
BOLD, DIM, GREEN, CYAN, YELLOW, RED, RESET = ("\033[1m", "\033[2m", "\033[32m",
                                              "\033[36m", "\033[33m", "\033[31m", "\033[0m")


@dataclass
class Wallpaper:
    section: str
    name: str
    kind: str                    # video / image / archive (zip Apple с картинками)
    local: Path | None = None    # файл уже на диске — просто копируем
    url: str = ""                # иначе качаем у Apple
    size: int = 0

    @property
    def status(self) -> str:
        return f"{GREEN}на диске{RESET}" if self.local else f"{CYAN}из сети{RESET}"

    @property
    def safe_name(self) -> str:
        return re.sub(r'[\\/:*?"<>|]+', "_", self.name).strip()[:120] or "wallpaper"

    def target(self, root: Path) -> Path:
        folder = root / self.section
        if self.kind == "archive":      # архив → папка с извлечёнными картинками
            return folder / self.safe_name
        ext = self.local.suffix if self.local else (".mov" if self.kind == "video" else ".heic")
        return folder / f"{self.safe_name}{ext}"


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


def yes(prompt: str, default: bool = True) -> bool:
    v = ask(f"{prompt} ({'Y/n' if default else 'y/N'})", "y" if default else "n").lower()
    return v in ("y", "yes", "д", "да", "1")


def parse_selection(s: str, n: int) -> list[int]:
    """'1,3,5-8' → [1,3,5,6,7,8];  'a'/'все'/пусто → всё."""
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


def norm(name: str) -> str:
    """Ключ для дедупликации: 'Sonoma River Day 4131' ≈ 'SonomaRiverDay'."""
    return re.sub(r"[^a-z]", "", name.lower())


def prettify(name: str) -> str:
    """SonomaRiver → Sonoma River; убираем номера ассетов в хвосте."""
    name = re.sub(r"[-_]?\d{3,}$", "", name)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return name.replace("_", " ").replace("-", " ").strip() or name


def is_dynamic_heic(p: Path) -> bool:
    """Динамические .heic несут XMP-метки solar/h24 в начале файла."""
    try:
        with open(p, "rb") as f:
            head = f.read(4 * 1024 * 1024)
        return any(m in head for m in DYNAMIC_MARKERS)
    except OSError:
        return False


def fetch(url: str, timeout: int = 30) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=timeout) as r:
        return r.read()


def head_size(url: str) -> int:
    try:
        req = urllib.request.Request(url, method="HEAD", headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return int(r.headers.get("Content-Length", 0) or 0)
    except Exception:
        return 0


# ── секции Landscape / Cityscape / Underwater / Earth (видео Aerial) ───

def _localized_names() -> dict[str, str]:
    names: dict[str, str] = {}
    if not STRINGS_BUNDLE.exists():
        return names
    for f in list(STRINGS_BUNDLE.rglob("*.strings")) + list(STRINGS_BUNDLE.rglob("*.loctable")):
        try:
            with open(f, "rb") as fh:
                data = plistlib.load(fh)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, str):
                        names[k] = v
                    elif isinstance(v, dict):
                        names.update({kk: vv for kk, vv in v.items() if isinstance(vv, str)})
        except Exception:
            continue
    return names


def _aerial_section(category: str) -> str:
    s = category.lower()
    if "city" in s:
        return "Cityscape"
    if "under" in s or "sea" in s:
        return "Underwater"
    if "earth" in s or "space" in s:
        return "Earth"
    return "Landscape"


def find_aerials(video_mode: str) -> list[Wallpaper]:
    names = _localized_names()

    # видео, которые система уже скачала (ключ — UUID из имени файла)
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

    for ej in ENTRIES_JSON_CANDIDATES:
        if not ej.exists():
            continue
        try:
            data = json.loads(ej.read_text())
        except Exception:
            continue
        cat_by_id = {}
        for cat in data.get("categories", []):
            key = cat.get("localizedNameKey", "")
            cat_by_id[cat.get("id", "")] = names.get(key, key)
        for a in data.get("assets", []):
            aid = str(a.get("id", "")).upper()
            url = next((a[k] for k in VIDEO_URL_KEYS[video_mode] if a.get(k)), "")
            nk = a.get("localizedNameKey", "")
            name = names.get(nk, nk) or a.get("accessibilityLabel", "") or aid
            cats = a.get("categories") or []
            section = _aerial_section(cat_by_id.get(cats[0], "") if cats else "")
            lp = local.get(aid)
            items.append(Wallpaper(section, name, "video", lp, url,
                                   file_size(lp) if lp else 0))
            seen.add(aid)
        break

    # видео на диске, которых нет в манифесте (встроенные в Tahoe)
    for key, path in sorted(local.items()):
        if key not in seen:
            name = names.get(path.stem) or f"Aerial {key[:8]}"
            items.append(Wallpaper("Landscape", name, "video", path, "",
                                   file_size(path)))
    return items


# ── секции Dynamic Wallpapers / Mac / Pictures (картинки) ──────────────

def find_local_images() -> list[Wallpaper]:
    items: list[Wallpaper] = []

    # секция Mac: встроенные обои текущей macOS (в Tahoe — внутри .default)
    if MAC_WALLPAPERS_DIR.exists():
        for f in MAC_WALLPAPERS_DIR.rglob("*"):
            if f.is_file() and f.suffix.lower() in IMAGE_EXT and file_size(f) >= MIN_IMAGE_SIZE:
                items.append(Wallpaper("Mac", f.stem, "image", f, "", file_size(f)))

    # /System/Library/Desktop Pictures: динамические и статичные .heic
    if DESKTOP_PICTURES.exists():
        for f in DESKTOP_PICTURES.rglob("*"):
            if (not f.is_file() or f.suffix.lower() not in IMAGE_EXT
                    or file_size(f) < MIN_IMAGE_SIZE
                    or ".wallpapers" in f.parts):   # там живут видео, не картинки
                continue
            section = "Dynamic Wallpapers" if is_dynamic_heic(f) else "Pictures"
            items.append(Wallpaper(section, f.stem, "image", f, "", file_size(f)))

    # кэши AssetsV2: то, что Настройки уже скачали с mesu
    for base in ASSETSV2_DIRS:
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.is_file() and f.suffix.lower() in IMAGE_EXT and file_size(f) >= MIN_IMAGE_SIZE:
                section = "Dynamic Wallpapers" if is_dynamic_heic(f) else "Pictures"
                items.append(Wallpaper(section, prettify(f.stem), "image", f, "",
                                       file_size(f)))
    return items


def _catalog_name(a: dict) -> str:
    for k in ("WallpaperName", "WallpaperIdentifier", "SUDocumentationID",
              "AssetSpecifier", "ArtworkName", "Slice"):
        v = a.get(k)
        if isinstance(v, str) and v:
            return prettify(v)
    return prettify(Path(a.get("__RelativePath", "wallpaper")).stem)


def _catalog_is_dynamic(a: dict, name: str) -> bool:
    """В каталоге нет явного флага — смотрим метаданные и размер.
    Динамические обои весят на порядок больше статичных."""
    blob = name + " " + " ".join(str(v) for v in a.values() if isinstance(v, str))
    if re.search(r"dynamic|solar", blob, re.I):
        return True
    unpacked = int(a.get("_UnarchivedSize", 0) or 0)
    return unpacked > 60 * 1024 * 1024


def fetch_catalog() -> list[Wallpaper]:
    """Полный каталог обоев Apple — включая те, что вы ещё не скачивали."""
    try:
        data = plistlib.loads(fetch(MESU_CATALOG))
    except Exception as e:
        print(f"{YELLOW}⚠️  Каталог Apple (mesu.apple.com) недоступен: {e}\n"
              f"   Покажу только то, что уже есть на диске.{RESET}")
        return []
    items: list[Wallpaper] = []
    for a in data.get("Assets", []):
        base, rel = a.get("__BaseURL", ""), a.get("__RelativePath", "")
        if not base or not rel:
            continue
        name = _catalog_name(a)
        section = "Dynamic Wallpapers" if _catalog_is_dynamic(a, name) else "Pictures"
        items.append(Wallpaper(section, name, "archive", None, base + rel,
                               int(a.get("_DownloadSize", 0) or 0)))
    return items


# ── общий поиск ─────────────────────────────────────────────────────────

def discover(video_mode: str, use_network: bool) -> dict[str, list[Wallpaper]]:
    inv: dict[str, list[Wallpaper]] = {s: [] for s in SECTIONS}

    def put(w: Wallpaper) -> None:
        inv.setdefault(w.section, []).append(w)

    for w in find_aerials(video_mode):
        put(w)
    for w in find_local_images():
        put(w)

    if use_network:
        have = {norm(w.name) for sec in ("Dynamic Wallpapers", "Pictures", "Mac")
                for w in inv[sec]}
        for w in fetch_catalog():
            if norm(w.name) not in have:
                put(w)

    for sec, arr in inv.items():     # дедуп внутри секции + сортировка
        seen: set[str] = set()
        uniq = []
        for w in sorted(arr, key=lambda w: (not w.local, w.name)):
            if norm(w.name) not in seen:
                seen.add(norm(w.name))
                uniq.append(w)
        inv[sec] = sorted(uniq, key=lambda w: w.name)
    return inv


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

class Progress:
    """Общая строка прогресса для параллельных загрузок."""

    def __init__(self, total_files: int):
        self.lock = threading.Lock()
        self.total = total_files
        self.done = 0
        self.bytes = 0
        self.start = time.time()

    def chunk(self, n: int) -> None:
        with self.lock:
            self.bytes += n
            self._draw()

    def finish(self, name: str, ok: bool, err: str = "") -> None:
        with self.lock:
            self.done += 1
            sys.stdout.write("\r\033[K")
            if ok:
                print(f"  {GREEN}✓{RESET} {name}")
            else:
                print(f"  {RED}✗ {name}: {err}{RESET}")
            self._draw()

    def _draw(self) -> None:
        speed = self.bytes / max(time.time() - self.start, 0.1)
        sys.stdout.write(f"\r  ⬇ {self.done}/{self.total} · {human(self.bytes)}"
                         f" · {human(speed)}/s ")
        sys.stdout.flush()


def download(url: str, dest: Path, progress: Progress) -> None:
    """Скачивание с докачкой после обрыва (Range + .part)."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    got = file_size(tmp)
    req = urllib.request.Request(url, headers=UA)
    if got:
        req.add_header("Range", f"bytes={got}-")
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:            # весь файл уже в .part
            tmp.rename(dest)
            return
        raise
    mode = "ab" if got and resp.status == 206 else "wb"
    with resp, open(tmp, mode) as out:
        while chunk := resp.read(CHUNK):
            out.write(chunk)
            progress.chunk(len(chunk))
    tmp.rename(dest)


def extract_images(archive: Path, into: Path) -> int:
    """Вынимает картинки из zip-архива Apple."""
    n = 0
    with zipfile.ZipFile(archive) as z:
        for zi in z.infolist():
            if not zi.is_dir() and Path(zi.filename).suffix.lower() in IMAGE_EXT:
                out = into / Path(zi.filename).name
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(z.read(zi))
                n += 1
    return n


def collect(picked: list[Wallpaper], root: Path, workers: int) -> None:
    copied = skipped = 0
    locals_ = [w for w in picked if w.local]
    nets = [w for w in picked if not w.local and w.url]

    if locals_:
        print(f"\n💾 Копирую с диска ({len(locals_)})…")
    for w in locals_:
        target = w.target(root)
        if target.exists() and file_size(target) == w.size:
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(w.local, target)
            copied += 1
        except PermissionError:
            print(f"  {RED}✗ {w.name}: нет доступа{RESET}")
    if locals_:
        print(f"   ✓ скопировано {copied}, пропущено (уже было) {skipped}")

    failed: list[str] = []
    downloaded = net_skipped = 0
    if nets:
        print(f"\n⬇️  Скачиваю из сети ({len(nets)}, потоков: {workers})…")
        progress = Progress(len(nets))
        counter_lock = threading.Lock()

        def worker(w: Wallpaper) -> None:
            nonlocal downloaded, net_skipped
            target = w.target(root)
            if (w.kind == "archive" and target.is_dir() and any(target.iterdir())) or \
               (w.kind != "archive" and target.exists()
                    and (not w.size or file_size(target) == w.size)):
                with counter_lock:
                    net_skipped += 1
                progress.finish(f"{w.name} {DIM}(уже есть){RESET}", True)
                return
            for attempt in range(3):
                try:
                    if w.kind == "archive":
                        with tempfile.TemporaryDirectory() as td:
                            tmp_zip = Path(td) / "a.zip"
                            download(w.url, tmp_zip, progress)
                            if extract_images(tmp_zip, target) == 0:
                                target.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(tmp_zip, target / f"{w.safe_name}.bin")
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        download(w.url, target, progress)
                    with counter_lock:
                        downloaded += 1
                    progress.finish(w.name, True)
                    return
                except Exception as e:
                    if attempt == 2:
                        failed.append(f"{w.name}: {e}")
                        progress.finish(w.name, False, str(e))
                    else:
                        time.sleep(2 * (attempt + 1))

        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(worker, nets))
        sys.stdout.write("\r\033[K")

    print(f"\n{BOLD}────────── ИТОГО ──────────{RESET}")
    print(f"  Скопировано с диска  : {copied}")
    print(f"  Скачано из сети      : {downloaded}")
    print(f"  Пропущено (уже было) : {skipped + net_skipped}")
    if failed:
        print(f"  {RED}Ошибок               : {len(failed)}{RESET}")
        for f in failed[:10]:
            print(f"    {RED}✗{RESET} {f}")
    print(f"  Папка                : {root}")
    if copied or downloaded:
        subprocess.run(["open", str(root)], check=False)


# ── интерактив ──────────────────────────────────────────────────────────

def choose_items(inv: dict[str, list[Wallpaper]]) -> list[Wallpaper]:
    sections = [s for s in inv if inv[s]]
    print(f"\n{BOLD}Секции (как в Настройках → Wallpaper):{RESET}")
    for i, s in enumerate(sections, 1):
        arr = inv[s]
        on_disk = sum(1 for w in arr if w.local)
        extra = f" · на диске {on_disk}" if on_disk else ""
        print(f"  {i}. {s} ({len(arr)}){DIM}{extra}{RESET}")

    raw = ask(f"\nКакие секции? {DIM}(например 1,3-5; Enter — все){RESET}", "все")
    chosen = [sections[i - 1] for i in parse_selection(raw, len(sections))]
    if not chosen:
        sys.exit("Ничего не выбрано.")

    take_all = yes("Взять секции целиком? (n — выбирать обои по одному)")
    picked: list[Wallpaper] = []
    for s in chosen:
        arr = inv[s]
        if take_all:
            picked += arr
            continue
        print(f"\n{BOLD}── {s} ({len(arr)}) ──{RESET}")
        for i, w in enumerate(arr, 1):
            sz = f" {human(w.size)}" if w.size else ""
            print(f"  {i:3}. {w.name:<44} {w.status}{DIM}{sz}{RESET}")
        raw = ask(f"Какие из «{s}»? {DIM}(Enter — все){RESET}", "все")
        picked += [arr[i - 1] for i in parse_selection(raw, len(arr))]
    return picked


def estimate(picked: list[Wallpaper]) -> None:
    nets = [w for w in picked if not w.local and w.url]
    unknown = [w for w in nets if not w.size]
    if unknown:
        print(f"{DIM}   … спрашиваю у сервера размеры ({len(unknown)} шт.){RESET}")
        with cf.ThreadPoolExecutor(max_workers=8) as pool:
            for w, sz in zip(unknown, pool.map(lambda w: head_size(w.url), unknown)):
                w.size = sz
    local_n = len(picked) - len(nets)
    total = sum(w.size for w in nets)
    print(f"\n{BOLD}── План ──{RESET}")
    print(f"  Копировать с диска : {local_n}")
    print(f"  Скачать из сети    : {len(nets)}" +
          (f"  (~{human(total)})" if total else ""))


# ── main ────────────────────────────────────────────────────────────────

def main() -> None:
    if sys.platform != "darwin":
        sys.exit("Этот скрипт нужно запускать на macOS.")

    p = argparse.ArgumentParser(description="Скачивает всё из Настроек → Wallpaper")
    p.add_argument("--dest", type=Path, help="папка (без вопросов)")
    p.add_argument("--all", action="store_true", help="взять все секции без вопросов")
    p.add_argument("--sections", default="", help="фильтр секций через запятую")
    p.add_argument("--video", choices=["sdr", "hdr"], default="sdr",
                   help="качество видео-обоев (по умолчанию 4K SDR 240fps)")
    p.add_argument("--local-only", action="store_true", help="без сети")
    p.add_argument("--workers", type=int, default=3, help="параллельных загрузок")
    args = p.parse_args()
    interactive = not args.all and sys.stdin.isatty()

    print(f"{BOLD}{CYAN}═══ macOS Wallpaper Grabber ═══{RESET}")
    print(f"{DIM}Сканирую систему и каталог Apple…{RESET}")
    inv = discover(args.video, use_network=not args.local_only)
    if not any(inv.values()):
        sys.exit("Ничего не нашлось — похоже, нет ни кэша, ни доступа к сети.")

    if args.sections:
        want = {norm(s) for s in args.sections.split(",")}
        inv = {s: arr for s, arr in inv.items() if norm(s) in want}

    if interactive:
        picked = choose_items(inv)
        estimate(picked)
        folder = args.dest or pick_folder()
        if not yes("Поехали?"):
            sys.exit(0)
    else:
        picked = [w for arr in inv.values() for w in arr]
        folder = args.dest or HOME / "Downloads" / "macOS Wallpapers"

    collect(picked, folder, args.workers)


if __name__ == "__main__":
    main()
