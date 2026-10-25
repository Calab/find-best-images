#!/usr/bin/env python3
"""
find_best_images.py

Scan one or more directories for images that look the same (even if they differ
in resolution, format or compression), pick the best version of each group, and
copy the winners into a "search-result" directory that mirrors their original
paths.

By default the program opens a window. Any options given on the command line
(-s, -t, --no-dup, --no-copy-other, --no-bad-files, --allow-sleep, --dry-run,
--threshold, --lossless-bonus) pre-fill the matching fields, and nothing runs
until you press Start scan. The source list starts empty, and "Add folder..."
opens at ~/Pictures:

    python find_best_images.py
    python find_best_images.py -s ~/pics -t /mnt/backup --dry-run

Add --no-gui to run in the command line only (no window, no tkinter needed).
Without -s it then scans ~/Pictures, and without -t the results go to ~.
Example (current directory is ~):

    python find_best_images.py --no-gui -s ~/pics ~/downloads/photos

    ~/pics/family/mom.png  ->  ~/search-result/pics/family/mom.png

Use -t/--target to create "search-result" somewhere else:

    python find_best_images.py --no-gui -s ~/pics -t /mnt/backup

    ~/pics/family/mom.png  ->  /mnt/backup/search-result/pics/family/mom.png

Selection rules for a group of look-alike images:
  1. Highest resolution (pixel count), with lossless formats (PNG, BMP, GIF,
     lossless TIFF and WebP) getting a bonus so they are favoured over lossy
     ones (JPG, lossy WebP) unless the lossy image is clearly larger (see
     --lossless-bonus). The format is read from the file itself, not its name.
  2. If resolutions are roughly equal (within ~5%), the sharper image wins
     (edge-detail score measured at a common size).
  3. If still tied, the larger file size wins.
  4. If still tied, the file from the source listed first wins: earlier -s
     entries beat later ones (in the GUI, higher in the Sources list). Within
     a single source, the alphabetically first path wins.
Images with no look-alikes are simply copied as-is.
Every look-alike that was NOT chosen gets a small text placeholder at its own
mirrored path with ".dup" appended, e.g.
    ~/pics/old/mom.jpg  ->  ~/search-result/pics/old/mom.jpg.dup
containing the full path of where the kept file was copied to. A group of
three matches yields one real copy plus two .dup files. Use --no-dup to skip
the placeholders.
Also on by default: every non-image file found under the sources is copied into
"search-result" (same relative path; skip with --no-copy-other), and each image
that can't be read gets a "<name>.bad" file there holding its path and the
error (skip with --no-bad-files).
When the scan finishes, search-result/search-report.txt lists every group of
look-alikes and whether each file was kept or not.

While a scan runs, the program asks the operating system not to sleep
(Windows: SetThreadExecutionState, macOS: caffeinate, Linux: systemd-inhibit).
If that isn't possible, the scan carries on and says so. Use --allow-sleep to
turn this off.

Requires: Python 3.10+ and Pillow  (pip install pillow)
The window needs tkinter (bundled with most Python installs; on Debian/Ubuntu:
sudo apt install python3-tk). With --no-gui it isn't needed.
"""

from __future__ import annotations

import argparse
import math
import multiprocessing
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

try:  # tkinter is only needed for --gui; the command line works without it
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - depends on the Python install
    tk = filedialog = messagebox = ttk = None  # type: ignore[assignment]

Image.MAX_IMAGE_PIXELS = None  # your own files; don't choke on huge images

EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
HASH_SIZE = 16          # 16x16 difference hash -> 256 bits
DETAIL_SIZE = 512       # common size used to compare sharpness
RESOLUTION_BIN = 1.05   # resolutions within ~5% are treated as "equal"
PHASES = ["Discovering", "Analysing", "Comparing", "Detail", "Copying"]
DEFAULT_SOURCE = "~/Pictures"   # --no-gui fallback when no -s is given; also where
                                # the window's "Add folder..." dialog opens
DEFAULT_TARGET = "~"            # where 'search-result' is created when no -t is given


# --------------------------------------------------------------------------
# Image analysis (runs in worker processes)
# --------------------------------------------------------------------------
def _webp_is_lossless(path: str) -> bool:
    """WebP can be either; its image chunk says which ('VP8L' = lossless,
    'VP8 ' = lossy). Pillow doesn't expose this, so read the RIFF chunks."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(12)
            if header[:4] != b"RIFF" or header[8:12] != b"WEBP":
                return False
            while True:
                chunk = fh.read(8)
                if len(chunk) < 8:
                    return False
                tag, size = chunk[:4], int.from_bytes(chunk[4:], "little")
                if tag == b"VP8L":
                    return True
                if tag == b"VP8 ":
                    return False
                if tag == b"ANMF":    # animation frame: image chunk is nested inside
                    fh.seek(16, 1)    # skip the frame header, then read its chunks
                else:
                    fh.seek(size + (size & 1), 1)  # chunks are padded to even length
    except OSError:
        return False


def _is_lossless(im, path: str) -> bool:
    """True if the image's real format (not its filename) stores pixels exactly:
    PNG, BMP, GIF, TIFF (unless JPEG-compressed) and lossless WebP. JPEG and
    lossy WebP are not."""
    fmt = (im.format or "").upper()
    if fmt in ("PNG", "BMP", "GIF"):
        return True
    if fmt == "TIFF":
        return "jpeg" not in str(im.info.get("compression", "")).lower()
    if fmt == "WEBP":
        return _webp_is_lossless(path)
    return False


def _load_gray(path: str):
    """Return (width, height, grayscale image, lossless), honouring EXIF rotation
    and flattening transparency onto white so alpha doesn't skew comparisons."""
    with Image.open(path) as im:
        lossless = _is_lossless(im, path)
        im = ImageOps.exif_transpose(im)
        width, height = im.size
        if im.mode in ("RGBA", "LA", "PA") or (
            im.mode == "P" and "transparency" in im.info
        ):
            rgba = im.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im = Image.alpha_composite(background, rgba)
        gray = im.convert("L")
    return width, height, gray, lossless


def analyze(path: str):
    """Return (path, width, height, dhash, error, lossless)."""
    try:
        width, height, gray, lossless = _load_gray(path)
        small = gray.resize((HASH_SIZE + 1, HASH_SIZE), Image.Resampling.LANCZOS)
        data = small.tobytes()
        bits = 0
        stride = HASH_SIZE + 1
        for row in range(HASH_SIZE):
            base = row * stride
            for col in range(HASH_SIZE):
                bits = (bits << 1) | (data[base + col] > data[base + col + 1])
        return path, width, height, bits, None, lossless
    except Exception as exc:  # unreadable / corrupt / unsupported
        return path, 0, 0, 0, f"{type(exc).__name__}: {exc}", False


def measure_detail(path: str):
    """Edge-energy score at a fixed size, so images of different resolution
    can be compared fairly. Higher = more detail / sharper."""
    try:
        _, _, gray, _ = _load_gray(path)
        resized = gray.resize((DETAIL_SIZE, DETAIL_SIZE), Image.Resampling.LANCZOS)
        edges = resized.filter(ImageFilter.FIND_EDGES)
        return path, ImageStat.Stat(edges).mean[0]
    except Exception:
        return path, 0.0


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s" if hours else f"{minutes}m {secs}s"


# --------------------------------------------------------------------------
# Front-end interface. run_scan() reports everything through a ScanUI, so the
# same engine drives the console, the GUI, and tests.
# --------------------------------------------------------------------------
class NullProgress:
    def step(self) -> None: ...
    def clear(self) -> None: ...
    def done(self) -> None: ...


class ScanUI:
    """Receives events from run_scan(). The base class ignores everything."""

    def info(self, text: str) -> None: ...

    def warn(self, text: str) -> None: ...

    def progress(self, label: str, total: int):
        """Start a counted phase; returns an object with step()/clear()/done()."""
        return NullProgress()

    def discovery(self, folders: int, images: int, final: bool = False) -> None:
        """Running totals while folders are being walked (total unknown)."""


class ScanError(Exception):
    """Bad input (e.g. a source that isn't a directory)."""


# ---- console front end ----------------------------------------------------
class ConsoleProgress:
    def __init__(self, label: str, total: int, enabled: bool, ui: "ConsoleUI"):
        self.label, self.total, self.enabled, self.ui = label, total, enabled, ui
        self.count = 0
        self._last = 0.0

    def step(self) -> None:
        self.count += 1
        if not self.enabled or self.total <= 0:
            return
        now = time.monotonic()
        if now - self._last >= 0.1 or self.count >= self.total:
            self._last = now
            fraction = min(self.count / self.total, 1.0)
            filled = int(30 * fraction)
            bar = "#" * filled + "-" * (30 - filled)
            sys.stderr.write(
                f"\r{self.label:<10} [{bar}] {self.count}/{self.total} ({fraction:4.0%})"
            )
            sys.stderr.flush()

    def clear(self) -> None:
        """Erase the bar so a message can be printed on a clean line."""
        if self.enabled and self.count:
            sys.stderr.write("\r" + " " * 79 + "\r")

    def done(self) -> None:
        if self.enabled and self.total > 0:
            sys.stderr.write("\n")
            sys.stderr.flush()
        if self.ui._active is self:
            self.ui._active = None


class ConsoleUI(ScanUI):
    def __init__(self, show_progress: bool = False):
        self.show_progress = show_progress
        self._active: ConsoleProgress | None = None
        self._ticks = 0
        self._disc_last = 0.0

    def _clear(self) -> None:
        if self._active is not None:
            self._active.clear()

    def info(self, text: str) -> None:
        self._clear()
        print(text)

    def warn(self, text: str) -> None:
        self._clear()
        print(text, file=sys.stderr)

    def progress(self, label: str, total: int) -> ConsoleProgress:
        bar = ConsoleProgress(label, total, self.show_progress, self)
        self._active = bar
        return bar

    def discovery(self, folders: int, images: int, final: bool = False) -> None:
        if not self.show_progress:
            return
        now = time.monotonic()
        if not final and now - self._disc_last < 0.1:
            return
        self._disc_last = now
        mark = "done" if final else "|/-\\"[self._ticks % 4]
        self._ticks += 1
        sys.stderr.write(
            f"\rDiscovering [{mark:^4}] {folders} folder(s) scanned, "
            f"{images} image(s) found   "
        )
        if final:
            sys.stderr.write("\n")
        sys.stderr.flush()


# ---- queue front end (used by the GUI; thread-safe, no tkinter needed) ----
class QueueProgress:
    def __init__(self, q: "queue.Queue", label: str, total: int):
        self.q, self.label, self.total = q, label, total
        self.count = 0
        self._last = 0.0
        q.put(("progress", label, 0, total))

    def step(self) -> None:
        self.count += 1
        now = time.monotonic()
        if now - self._last >= 0.05 or self.count >= self.total:
            self._last = now
            self.q.put(("progress", self.label, self.count, self.total))

    def clear(self) -> None: ...

    def done(self) -> None:
        self.q.put(("progress_done", self.label, self.total))


class QueueUI(ScanUI):
    """Posts events to a queue so another thread (the Tk main loop) can draw them."""

    def __init__(self, q: "queue.Queue"):
        self.q = q
        self._disc_last = 0.0

    def info(self, text: str) -> None:
        self.q.put(("info", text))

    def warn(self, text: str) -> None:
        self.q.put(("warn", text))

    def progress(self, label: str, total: int) -> QueueProgress:
        return QueueProgress(self.q, label, total)

    def discovery(self, folders: int, images: int, final: bool = False) -> None:
        now = time.monotonic()
        if final or now - self._disc_last >= 0.05:
            self._disc_last = now
            self.q.put(("discovery", folders, images, final))


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Info:
    path: Path
    root: Path
    width: int
    height: int
    dhash: int
    file_size: int
    detail: float = 0.0
    order: int = 0  # position of its source in the list (0 = listed first)
    lossless: bool = False  # stored exactly (PNG, BMP, GIF, lossless TIFF/WebP)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def ratio(self) -> float:
        return self.width / self.height


@dataclass
class Options:
    """Everything run_scan() needs; filled from the CLI or from the GUI."""

    sources: list[str] = field(default_factory=list)
    target: str | None = DEFAULT_TARGET
    threshold: int = 5
    lossless_bonus: float = 1.10
    ratio_tol: float = 0.03
    jobs: int = os.cpu_count() or 1
    dup: bool = True
    copy_other: bool = True   # also copy non-image files
    bad: bool = True          # write <name>.bad for unreadable images
    dry_run: bool = False
    verbose: bool = False
    keep_awake: bool = True   # stop the computer sleeping while the scan runs
    spawn: bool = False  # use 'spawn' worker processes (safer next to a GUI)


# --------------------------------------------------------------------------
# File discovery and path mapping
# --------------------------------------------------------------------------
def gather_files(
    roots: list[Path],
    out_dir: Path,
    ui: ScanUI | None = None,
    collect_others: bool = False,
) -> tuple[dict[Path, Path], dict[Path, Path]]:
    """Return ({image: root}, {other_file: root}), skipping the output dir.

    A file counts as an image purely by its extension (see EXTENSIONS); whether
    it can really be read is decided later. Non-image files are only collected
    when `collect_others` is set. The total isn't known until the walk
    finishes, so the UI gets running counts rather than a percentage."""
    ui = ui or ScanUI()
    images: dict[Path, Path] = {}
    others: dict[Path, Path] = {}
    folders = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d
                for d in dirnames
                if Path(os.path.abspath(os.path.join(dirpath, d))) != out_dir
            ]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.splitext(name)[1].lower() in EXTENSIONS:
                    images.setdefault(Path(os.path.abspath(full)), root)
                elif collect_others and os.path.isfile(full):  # skips sockets, broken links
                    others.setdefault(Path(os.path.abspath(full)), root)
            folders += 1
            ui.discovery(folders, len(images))
    ui.discovery(folders, len(images), True)
    return images, others


def mirror_path(path: Path, root: Path, base: Path, out_dir: Path) -> Path:
    """~/pics/family/mom.png (cwd=~) -> ~/search-result/pics/family/mom.png.
    Files outside the cwd fall back to <root folder name>/<path inside root>."""
    try:
        rel = path.relative_to(base)
    except ValueError:
        rel = Path(root.name or "root") / path.relative_to(root)
    return out_dir / rel


def destination_for(info: Info, base: Path, out_dir: Path) -> Path:
    return mirror_path(info.path, info.root, base, out_dir)


# --------------------------------------------------------------------------
# Grouping look-alikes
# --------------------------------------------------------------------------
def cluster(
    infos: list[Info], threshold: int, ratio_tol: float, progress=None
) -> list[list[Info]]:
    """Union-find over images whose hashes are within `threshold` bits and whose
    aspect ratios agree within `ratio_tol`. Images are sorted by aspect ratio so
    each one is only compared against others with a similar shape."""
    n = len(infos)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    order = sorted(range(n), key=lambda i: infos[i].ratio)
    for a in range(n):
        i = order[a]
        limit = infos[i].ratio * (1 + ratio_tol)
        hi = infos[i].dhash
        for b in range(a + 1, n):
            j = order[b]
            if infos[j].ratio > limit:
                break
            if (hi ^ infos[j].dhash).bit_count() <= threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
        if progress:
            progress.step()
    if progress:
        progress.done()

    groups: dict[int, list[Info]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(infos[i])
    return list(groups.values())


def rank_key(info: Info, lossless_bonus: float):
    effective = info.pixels * (lossless_bonus if info.lossless else 1.0)
    resolution_bin = round(math.log(max(effective, 1)) / math.log(RESOLUTION_BIN))
    return (resolution_bin, info.detail, info.file_size, -info.order)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def write_report(
    report_path: Path,
    opts: Options,
    roots: list[Path],
    base: Path,
    out_dir: Path,
    scanned: int,
    unique: int,
    dup_groups: list[tuple[Info, list[Info]]],
    skipped_files: list[tuple[str, str]],
    elapsed: float,
    others_count: int = 0,
    others_failed: int = 0,
    bad_dests: dict[str, Path] | None = None,
) -> None:
    bad_dests = bad_dests or {}
    kept = len(dup_groups)
    dropped = sum(len(g) - 1 for _, g in dup_groups)
    lines = [
        "IMAGE SEARCH REPORT",
        "=" * 19,
        f"Generated:         {datetime.now():%Y-%m-%d %H:%M:%S}",
        "Sources:           " + ("\n                   ".join(str(r) for r in roots)),
        f"Result directory:  {out_dir}",
        f"Settings:          threshold={opts.threshold} bits, "
        f"lossless-bonus={opts.lossless_bonus}, ratio-tol={opts.ratio_tol}",
        f".dup placeholders: {'created' if opts.dup else 'disabled (--no-dup)'}",
        f".bad files:        {'created' if opts.bad else 'disabled'}",
        f"Non-image files:   {'copied' if opts.copy_other else 'not copied'}",
        f"Elapsed time:      {format_duration(elapsed)}",
        "",
        f"Images scanned:               {scanned}",
        f"Groups of look-alikes:        {len(dup_groups)}",
        f"  files kept (1 per group):   {kept}",
        f"  files not kept:             {dropped}",
        f"Unique images (copied):       {unique}",
        f"Unreadable files skipped:     {len(skipped_files)}",
    ]
    if opts.copy_other:
        failed = f" ({others_failed} failed)" if others_failed else ""
        lines.append(f"Non-image files copied:       {others_count}{failed}")
    lines.append("")

    if dup_groups:
        lines.append("DUPLICATE GROUPS")
        lines.append("-" * 16)
        for n, (best, ordered) in enumerate(
            sorted(dup_groups, key=lambda g: str(g[0].path)), start=1
        ):
            lines.append(f"\nGroup {n} ({len(ordered)} files)")
            for info in ordered:
                dest = destination_for(info, base, out_dir)
                if info is best:
                    status, note = "KEPT   ", f"copied to {dest}"
                else:
                    status = "NOT KEPT"
                    note = f"placeholder {dest}.dup" if opts.dup else "not copied"
                lines.append(
                    f"  [{status}] {info.path}\n"
                    f"             {info.width}x{info.height}, "
                    f"{info.file_size:,} bytes, "
                    f"{'lossless' if info.lossless else 'lossy'}; {note}"
                )
    else:
        lines.append("No groups of look-alike images were found.")

    if skipped_files:
        lines += ["", "UNREADABLE FILES (skipped)", "-" * 26]
        for path, reason in skipped_files:
            lines.append(f"  {path}: {reason}")
            if path in bad_dests:
                lines.append(f"      marker file: {bad_dests[path]}")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# The scan itself (shared by the command line and the GUI)
# --------------------------------------------------------------------------
def _run_scan(opts: Options, ui: ScanUI) -> int:
    started = time.monotonic()
    base = Path(os.path.abspath(os.getcwd()))
    target = Path(os.path.abspath(os.path.expanduser(opts.target or DEFAULT_TARGET)))
    if target.exists() and not target.is_dir():
        raise ScanError(f"target is not a directory: {opts.target}")
    out_dir = target / "search-result"

    roots: list[Path] = []
    for d in opts.sources:
        root = Path(os.path.abspath(os.path.expanduser(d)))
        if not root.is_dir():
            raise ScanError(f"source is not a directory: {d}")
        if root not in roots:  # a repeated source keeps its first position
            roots.append(root)
    if not roots:
        raise ScanError("no source directories were given")
    root_order = {root: n for n, root in enumerate(roots)}

    ui.info("Sources:")
    for root in roots:
        ui.info(f"  {root}")
    ui.info(f"Search results directory: {out_dir}\n")

    files, others = gather_files(roots, out_dir, ui, opts.copy_other)
    found = f"Found {len(files)} image file(s)"
    if opts.copy_other:
        found += f" and {len(others)} other file(s)"
    ui.info(found + ". Analysing...")
    if not files and not others:
        return 0

    paths = [str(p) for p in files]
    infos: list[Info] = []
    skipped = 0
    skipped_files: list[tuple[str, str]] = []
    context = multiprocessing.get_context("spawn") if opts.spawn else None
    with ProcessPoolExecutor(max_workers=opts.jobs, mp_context=context) as pool:
        analysing = ui.progress("Analysing", len(paths))
        for path, w, h, dhash, err, lossless in pool.map(analyze, paths, chunksize=16):
            analysing.step()
            if err or w == 0 or h == 0:
                skipped += 1
                skipped_files.append((path, err or "zero-sized image"))
                ui.warn(f"  skipped {path}: {err or 'zero-sized image'}")
                continue
            p = Path(path)
            infos.append(Info(p, files[p], w, h, dhash, p.stat().st_size,
                              order=root_order[files[p]], lossless=lossless))
        analysing.done()

        groups = cluster(
            infos, opts.threshold, opts.ratio_tol, ui.progress("Comparing", len(infos))
        )

        # Sharpness only matters when there is a real choice to make.
        by_path = {str(i.path): i for i in infos}
        contested = [str(i.path) for g in groups if len(g) > 1 for i in g]
        scoring = ui.progress("Detail", len(contested))
        for path, detail in pool.map(measure_detail, contested, chunksize=8):
            by_path[path].detail = detail
            scoring.step()
        scoring.done()

    winners: list[Info] = []
    losers: list[tuple[Info, Info]] = []  # (not kept, kept)
    dup_groups = 0
    dup_group_list: list[tuple[Info, list[Info]]] = []
    for group in groups:
        if len(group) == 1:
            winners.append(group[0])
            continue
        # Sorting by path first makes the last tie-break deterministic (the
        # second sort is stable, so fully tied files stay in path order).
        ordered = sorted(
            sorted(group, key=lambda i: str(i.path)),
            key=lambda i: rank_key(i, opts.lossless_bonus),
            reverse=True,
        )
        best = ordered[0]
        winners.append(best)
        dup_groups += 1
        losers.extend((i, best) for i in group if i is not best)
        dup_group_list.append((best, ordered))
        if opts.verbose:
            ui.info(f"\nGroup of {len(group)} look-alikes:")
            for i in ordered:
                mark = "KEEP" if i is best else "    "
                kind = "lossless" if i.lossless else "lossy"
                ui.info(f"  [{mark}] {i.path}  {i.width}x{i.height}  "
                        f"{i.file_size:,} B  {kind}  detail={i.detail:.2f}")

    bad_dests: dict[str, Path] = {}
    if opts.bad:
        for path, _reason in skipped_files:
            mirrored = mirror_path(Path(path), files[Path(path)], base, out_dir)
            bad_dests[path] = mirrored.with_name(mirrored.name + ".bad")

    saving = ui.progress(
        "Copying",
        len(winners)
        + (len(losers) if opts.dup else 0)
        + len(bad_dests)
        + (len(others) if opts.copy_other else 0),
    )
    copied = 0
    for w in sorted(winners, key=lambda i: str(i.path)):
        dest = destination_for(w, base, out_dir)
        if opts.dry_run:
            ui.info(f"[dry-run] {w.path} -> {dest}")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(w.path, dest)
        copied += 1
        saving.step()

    placeholders = 0
    if opts.dup:
        for loser, kept_info in sorted(losers, key=lambda t: str(t[0].path)):
            dest = destination_for(loser, base, out_dir)
            dest = dest.with_name(dest.name + ".dup")
            kept_copy = destination_for(kept_info, base, out_dir)
            if opts.dry_run:
                ui.info(f"[dry-run] {dest}  ->  {kept_copy}")
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(f"{kept_copy}\n", encoding="utf-8")
            placeholders += 1
            saving.step()

    bad_made = 0
    for path, reason in skipped_files:
        if not opts.bad:
            break
        dest = bad_dests[path]
        if opts.dry_run:
            ui.info(f"[dry-run] {dest}  ({reason})")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f"{path}\n{reason}\n", encoding="utf-8")
        bad_made += 1
        saving.step()

    others_copied = others_failed = 0
    if opts.copy_other:
        for src, root in sorted(others.items(), key=lambda kv: str(kv[0])):
            dest = mirror_path(src, root, base, out_dir)
            if opts.dry_run:
                ui.info(f"[dry-run] {src} -> {dest}")
                others_copied += 1
            else:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    others_copied += 1
                except OSError as exc:
                    others_failed += 1
                    ui.warn(f"  couldn't copy {src}: {exc}")
            saving.step()
    saving.done()

    elapsed = time.monotonic() - started
    report_path = out_dir / "search-report.txt"
    if not opts.dry_run:
        write_report(
            report_path, opts, roots, base, out_dir, len(infos),
            len(groups) - dup_groups, dup_group_list, skipped_files, elapsed,
            others_copied, others_failed, bad_dests,
        )

    ui.info(
        f"\nScanned {len(infos)} image(s) ({skipped} unreadable): "
        f"{dup_groups} group(s) of look-alikes, {len(groups) - dup_groups} unique."
    )
    verb = "Would copy" if opts.dry_run else "Copied"
    ui.info(f"{verb} {copied} file(s) to {out_dir}")
    if opts.dup:
        ui.info(f"{'Would create' if opts.dry_run else 'Created'} "
                f"{placeholders} .dup placeholder(s)")
    else:
        ui.info(".dup placeholders disabled (--no-dup)")
    if opts.bad:
        ui.info(f"{'Would create' if opts.dry_run else 'Created'} "
                f"{bad_made} .bad file(s)")
    if opts.copy_other:
        failed = f" ({others_failed} could not be copied)" if others_failed else ""
        ui.info(f"{'Would copy' if opts.dry_run else 'Copied'} "
                f"{others_copied} non-image file(s){failed}")
    ui.info(f"{'Would write' if opts.dry_run else 'Report written to'} {report_path}")
    ui.info(f"Scan completed in {format_duration(elapsed)}")
    return 0


class SleepInhibitor:
    """Context manager that asks the OS not to sleep while it is held.

    Uses each platform's own mechanism, so no extra packages are needed. If the
    request can't be made, `active` stays False and `error` says why; nothing
    is raised, because a scan is still worth running without it.

    Enter and exit must happen on the same thread (Windows ties the request to
    the calling thread); run_scan() guarantees that."""

    _WIN_CONTINUOUS = 0x80000000
    _WIN_SYSTEM_REQUIRED = 0x00000001

    def __init__(self, reason: str = "Scanning and copying images"):
        self.reason = reason
        self.active = False
        self.method = ""
        self.error = ""
        self._proc: subprocess.Popen | None = None
        self._windows = False

    def __enter__(self) -> "SleepInhibitor":
        try:
            if sys.platform == "win32":
                self._start_windows()
            elif sys.platform == "darwin":
                self._start_macos()
            else:
                self._start_linux()
        except Exception as exc:  # never let this stop the scan
            self.active = False
            self.error = str(exc) or type(exc).__name__
        return self

    def __exit__(self, *exc_info) -> bool:
        self.release()
        return False

    # ---- per-platform ----
    def _start_windows(self) -> None:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not kernel32.SetThreadExecutionState(
            self._WIN_CONTINUOUS | self._WIN_SYSTEM_REQUIRED
        ):
            raise OSError("SetThreadExecutionState failed")
        self._windows = True
        self.active, self.method = True, "Windows execution state"

    def _start_macos(self) -> None:
        # -i: no idle sleep, -s: no system sleep on AC power; -w: exit with us.
        self._spawn(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
        self.active, self.method = True, "caffeinate"

    def _start_linux(self) -> None:
        # The helper holds the lock until we release it, or until this process
        # disappears (so a crash can't leave the computer unable to sleep).
        watcher = f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 5; done"
        self._spawn([
            "systemd-inhibit", "--what=idle:sleep", "--who=find_best_images",
            f"--why={self.reason}", "--mode=block", "sh", "-c", watcher,
        ])
        self.active, self.method = True, "systemd-inhibit"

    def _spawn(self, command: list[str]) -> None:
        if shutil.which(command[0]) is None:
            raise FileNotFoundError(f"{command[0]} not found")
        self._proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:  # a helper that fails (e.g. no D-Bus) exits straight away
            code = self._proc.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            return
        self._proc = None
        raise RuntimeError(f"{command[0]} exited with code {code}")

    def release(self) -> None:
        if self._windows:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
                self._WIN_CONTINUOUS
            )
            self._windows = False
        if self._proc is not None:
            if self._proc.poll() is None:
                try:  # the helper leads its own process group
                    os.killpg(self._proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
        self.active = False


def run_scan(opts: Options, ui: ScanUI) -> int:
    """Run a scan, keeping the computer awake while it works (unless disabled)."""
    if not opts.keep_awake:
        return _run_scan(opts, ui)
    with SleepInhibitor() as awake:
        if awake.active:
            ui.info(f"Keeping the computer awake during the scan ({awake.method})")
        else:
            ui.warn(f"Note: couldn't prevent the computer from sleeping "
                    f"({awake.error}); the scan will run anyway.")
        return _run_scan(opts, ui)


# --------------------------------------------------------------------------
# GUI (tkinter)
# --------------------------------------------------------------------------
class SourceList:
    """The list of source folders the user builds up (GUI-independent)."""

    def __init__(self, initial=()):
        self.items: list[str] = []
        for path in initial:
            self.add(path)

    @staticmethod
    def normalize(path: str) -> str:
        return os.path.abspath(os.path.expanduser(path))

    def add(self, path: str) -> bool:
        """Add a folder; returns False if it was empty or already listed."""
        if not path:
            return False
        norm = self.normalize(path)
        if norm in self.items:
            return False
        self.items.append(norm)
        return True

    def remove(self, indices) -> None:
        for i in sorted(set(indices), reverse=True):
            if 0 <= i < len(self.items):
                del self.items[i]

    def move(self, indices, delta: int) -> list[int]:
        """Move the selected entries one place up (delta=-1) or down (+1).

        Entries that can't move (already at the edge, or blocked by another
        selected entry) stay put. Returns the selection's new positions."""
        count = len(self.items)
        selected = sorted({i for i in indices if 0 <= i < count})
        positions = set(selected)
        for i in (selected if delta < 0 else reversed(selected)):
            j = i + delta
            if 0 <= j < count and j not in positions:
                self.items[i], self.items[j] = self.items[j], self.items[i]
                positions.remove(i)
                positions.add(j)
        return sorted(positions)

    def clear(self) -> None:
        self.items.clear()


class App:
    """The main window. Scanning runs in a background thread and reports back
    through a queue that the Tk main loop polls, so the window stays live."""

    def __init__(self, root, opts: Options):
        self.root = root
        self.base = opts
        self.sources = SourceList(opts.sources)
        self.q: queue.Queue = queue.Queue()
        self.running = False
        self._disc_running = False
        self._last_dry = opts.dry_run
        self._ttk_controls: list = []
        self._tk_controls: list = []
        self.bars: dict[str, tuple] = {}
        self._last_dir = self._initial_dir()
        self._help_win = None

        root.title("Find Best Images")
        root.minsize(760, 910)
        root.protocol("WM_DELETE_WINDOW", self._on_quit)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        outer = ttk.Frame(root, padding=10)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)  # the log grows with the window

        self._build_sources(outer)
        self._build_target(outer)
        self._build_options(outer)
        self._build_progress(outer)
        self._build_log(outer)
        self._build_buttons(outer)
        self._refresh_sources()

    # ---- layout -----------------------------------------------------------
    def _build_sources(self, outer) -> None:
        frame = ttk.LabelFrame(
            outer, text="Sources (folders to scan, in priority order)", padding=8
        )
        frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(0, weight=1)

        self.listbox = tk.Listbox(
            frame, selectmode="extended", height=6, exportselection=False,
            activestyle="none",
        )
        self.listbox.grid(row=0, column=0, rowspan=5, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.listbox.yview)
        scroll.grid(row=0, column=1, rowspan=5, sticky="ns")
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.bind("<Delete>", lambda _e: self.remove_selected())
        self.listbox.bind("<BackSpace>", lambda _e: self.remove_selected())
        self._tk_controls.append(self.listbox)

        for row, (text, command) in enumerate([
            ("Add folder…", self.add_source),
            ("Move up", self.move_up),
            ("Move down", self.move_down),
            ("Remove selected", self.remove_selected),
            ("Clear all", self.clear_sources),
        ]):
            button = ttk.Button(frame, text=text, command=command)
            button.grid(row=row, column=2, sticky="ew", padx=(8, 0), pady=1)
            self._ttk_controls.append(button)

        ttk.Label(
            frame, foreground="#555555",
            text="Order matters: if look-alikes are otherwise tied, the file from "
                 "the source higher in the list is kept.",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_target(self, outer) -> None:
        frame = ttk.LabelFrame(outer, text="Target directory", padding=8)
        frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(0, weight=1)

        self.target_var = tk.StringVar(
            value=os.path.abspath(os.path.expanduser(self.base.target or DEFAULT_TARGET))
        )
        entry = ttk.Entry(frame, textvariable=self.target_var)
        entry.grid(row=0, column=0, sticky="ew")
        browse = ttk.Button(frame, text="Browse…", command=self.browse_target)
        browse.grid(row=0, column=1, padx=(8, 0))
        self._ttk_controls += [entry, browse]

        self.result_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.result_var, foreground="#555555").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        self.target_var.trace_add("write", lambda *_a: self._update_result_label())
        self._update_result_label()

    @staticmethod
    def _threshold_range(value) -> tuple[int, int]:
        """Slider range; widened if a command-line value lies outside it."""
        return 0, max(64, int(value))

    @staticmethod
    def _lossless_range(value) -> tuple[float, float]:
        return min(1.0, value), max(2.0, value)

    def _build_options(self, outer) -> None:
        frame = ttk.LabelFrame(outer, text="Options", padding=8)
        frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)

        self.dup_var = tk.BooleanVar(value=self.base.dup)
        self.other_var = tk.BooleanVar(value=self.base.copy_other)
        self.bad_var = tk.BooleanVar(value=self.base.bad)
        self.awake_var = tk.BooleanVar(value=self.base.keep_awake)
        self.dry_var = tk.BooleanVar(value=self.base.dry_run)
        checks = [
            ("Create .dup files for look-alikes that aren't kept", self.dup_var),
            ("Copy non-image files to the target as well", self.other_var),
            ("Create .bad files for images that can't be read", self.bad_var),
            ("Keep the computer awake while scanning", self.awake_var),
            ("Dry run (don't create any files)", self.dry_var),
        ]
        for row, (text, var) in enumerate(checks):
            box = ttk.Checkbutton(frame, text=text, variable=var)
            box.grid(row=row, column=0, columnspan=2, sticky="w")
            self._ttk_controls.append(box)

        slider_row = len(checks)
        self.threshold_var = tk.DoubleVar(value=self.base.threshold)
        self.lossless_var = tk.DoubleVar(value=self.base.lossless_bonus)
        low, high = self._threshold_range(self.base.threshold)
        ttk.Label(frame, text="Match threshold\n(bits of 256; lower = stricter)").grid(
            row=slider_row, column=0, sticky="w", pady=(8, 0))
        self.threshold_scale = tk.Scale(
            frame, from_=low, to=high, orient="horizontal", resolution=1,
            variable=self.threshold_var, highlightthickness=0,
        )
        self.threshold_scale.grid(
            row=slider_row, column=1, sticky="ew", padx=(12, 0), pady=(8, 0))
        low, high = self._lossless_range(self.base.lossless_bonus)
        ttk.Label(frame, text="Lossless bonus\n(1.00 = resolution only)").grid(
            row=slider_row + 1, column=0, sticky="w", pady=(4, 0))
        self.lossless_scale = tk.Scale(
            frame, from_=low, to=high, orient="horizontal", resolution=0.01,
            variable=self.lossless_var, highlightthickness=0,
        )
        self.lossless_scale.grid(
            row=slider_row + 1, column=1, sticky="ew", padx=(12, 0), pady=(4, 0))
        self._tk_controls += [self.threshold_scale, self.lossless_scale]

    def _build_progress(self, outer) -> None:
        frame = ttk.LabelFrame(outer, text="Progress", padding=8)
        frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)
        for row, name in enumerate(PHASES):
            ttk.Label(frame, text=name, width=12).grid(row=row, column=0, sticky="w")
            bar = ttk.Progressbar(frame, orient="horizontal", mode="determinate",
                                  maximum=100, value=0)
            bar.grid(row=row, column=1, sticky="ew", padx=6, pady=2)
            text = tk.StringVar(value="waiting")
            ttk.Label(frame, textvariable=text, width=30, anchor="w").grid(
                row=row, column=2, sticky="w")
            self.bars[name] = (bar, text)

    def _build_log(self, outer) -> None:
        frame = ttk.LabelFrame(outer, text="Log", padding=4)
        frame.grid(row=4, column=0, sticky="nsew", pady=(0, 8))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(frame, height=10, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.tag_configure("warn", foreground="#b00020")

    def _build_buttons(self, outer) -> None:
        frame = ttk.Frame(outer)
        frame.grid(row=5, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        # Help stays available during a scan, so it isn't in the disabled list.
        ttk.Button(frame, text="Help", command=self.show_help).grid(
            row=0, column=1, padx=(8, 0))
        self.defaults_btn = ttk.Button(frame, text="Defaults", command=self.reset_defaults)
        self.defaults_btn.grid(row=0, column=2, padx=(8, 0))
        self.start_btn = ttk.Button(frame, text="Start scan", command=self.start_scan)
        self.start_btn.grid(row=0, column=3, padx=(8, 0))
        ttk.Button(frame, text="Quit", command=self._on_quit).grid(
            row=0, column=4, padx=(8, 0))
        self._ttk_controls += [self.defaults_btn, self.start_btn]

    @staticmethod
    def _initial_dir() -> str:
        """Where 'Add folder…' opens: ~/Pictures if it exists, else home."""
        pictures = os.path.expanduser(DEFAULT_SOURCE)
        return pictures if os.path.isdir(pictures) else os.path.expanduser("~")

    # ---- source / target handling -----------------------------------------
    def _refresh_sources(self, select=()) -> None:
        """Redraw the list, optionally re-selecting the given positions."""
        self.listbox.delete(0, "end")
        for path in self.sources.items:
            self.listbox.insert("end", path)
        for index in select:
            self.listbox.selection_set(index)
        if select:
            self.listbox.see(select[0])

    def add_source(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root, title="Add a source folder", initialdir=self._last_dir,
            mustexist=True,
        )
        if chosen:  # '' (or an empty tuple) means the dialog was cancelled
            self._last_dir = chosen
            self.sources.add(chosen)
            self._refresh_sources()

    def remove_selected(self) -> None:
        self.sources.remove(self.listbox.curselection())
        self._refresh_sources()

    def clear_sources(self) -> None:
        self.sources.clear()
        self._refresh_sources()

    def _move(self, delta: int) -> None:
        moved = self.sources.move(self.listbox.curselection(), delta)
        self._refresh_sources(select=moved)

    def move_up(self) -> None:
        self._move(-1)

    def move_down(self) -> None:
        self._move(1)

    def browse_target(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root, title="Choose the target directory",
            initialdir=self.target_var.get() or self._last_dir,
        )
        if chosen:
            self.target_var.set(os.path.abspath(chosen))

    def _update_result_label(self) -> None:
        target = self.target_var.get().strip() or DEFAULT_TARGET
        out = os.path.join(os.path.abspath(os.path.expanduser(target)), "search-result")
        self.result_var.set(f"Results will go in: {out}")

    def reset_defaults(self) -> None:
        """Put every setting back to the program's defaults.

        This includes emptying the source list and the values that have no
        widget (--ratio-tol, --jobs, --verbose)."""
        if self.running:
            return
        defaults = Options()
        self.base = defaults
        self.sources = SourceList(defaults.sources)
        self._refresh_sources()
        self._last_dir = self._initial_dir()
        self.target_var.set(os.path.abspath(os.path.expanduser(defaults.target)))
        self.dup_var.set(defaults.dup)
        self.other_var.set(defaults.copy_other)
        self.bad_var.set(defaults.bad)
        self.awake_var.set(defaults.keep_awake)
        self.dry_var.set(defaults.dry_run)
        low, high = self._threshold_range(defaults.threshold)
        self.threshold_scale.configure(from_=low, to=high)
        self.threshold_var.set(defaults.threshold)
        low, high = self._lossless_range(defaults.lossless_bonus)
        self.lossless_scale.configure(from_=low, to=high)
        self.lossless_var.set(defaults.lossless_bonus)
        self.status_var.set("Defaults restored")

    def show_help(self) -> None:
        """Open (or raise) a read-only window with the option usage."""
        if self._help_win is not None and self._help_win.winfo_exists():
            self._help_win.deiconify()
            self._help_win.lift()
            self._help_win.focus_set()
            return
        win = tk.Toplevel(self.root)
        win.title("Find Best Images - Help")
        win.transient(self.root)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)
        frame = ttk.Frame(win, padding=8)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        text = tk.Text(frame, width=92, height=30, wrap="none", font="TkFixedFont")
        text.grid(row=0, column=0, sticky="nsew")
        down = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        down.grid(row=0, column=1, sticky="ns")
        across = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        across.grid(row=1, column=0, sticky="ew")
        text.configure(yscrollcommand=down.set, xscrollcommand=across.set)
        text.insert("1.0", help_text())
        text.configure(state="disabled")  # read-only, but still selectable/copyable

        ttk.Button(frame, text="Close", command=win.destroy).grid(
            row=2, column=0, columnspan=2, sticky="e", pady=(8, 0))
        win.bind("<Escape>", lambda _e: win.destroy())
        self._help_win = win

    # ---- running a scan ---------------------------------------------------
    def current_options(self) -> Options:
        return Options(
            sources=list(self.sources.items),
            target=self.target_var.get().strip() or None,
            threshold=int(round(self.threshold_var.get())),
            lossless_bonus=round(self.lossless_var.get(), 2),
            ratio_tol=self.base.ratio_tol,   # no widget: carried over from the CLI
            jobs=self.base.jobs,
            dup=bool(self.dup_var.get()),
            copy_other=bool(self.other_var.get()),
            bad=bool(self.bad_var.get()),
            keep_awake=bool(self.awake_var.get()),
            dry_run=bool(self.dry_var.get()),
            verbose=self.base.verbose,
            spawn=True,
        )

    def start_scan(self) -> None:
        if self.running:
            return
        if not self.sources.items:
            messagebox.showwarning("No sources", "Add at least one source folder "
                                   "before starting.", parent=self.root)
            return
        opts = self.current_options()
        self._last_dry = opts.dry_run
        self._clear_log()
        self._reset_progress()
        self._set_running(True)
        self.status_var.set("Scanning…")
        threading.Thread(target=self._worker, args=(opts,), daemon=True).start()
        self.root.after(50, self._poll)

    def _worker(self, opts: Options) -> None:
        try:
            code = run_scan(opts, QueueUI(self.q))
            self.q.put(("finished", code, None))
        except ScanError as exc:
            self.q.put(("finished", 2, str(exc)))
        except Exception as exc:  # keep the window alive and show what happened
            self.q.put(("log_error", traceback.format_exc()))
            self.q.put(("finished", 1, f"{type(exc).__name__}: {exc}"))

    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        if self.running or not self.q.empty():
            self.root.after(50, self._poll)

    def _handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "info":
            self.log(event[1])
        elif kind == "warn":
            self.log(event[1], "warn")
        elif kind == "log_error":
            self.log(event[1], "warn")
        elif kind == "discovery":
            self._discovery(*event[1:])
        elif kind == "progress":
            self._progress(*event[1:])
        elif kind == "progress_done":
            self._progress_done(*event[1:])
        elif kind == "finished":
            _, _code, error = event
            self._stop_discovery_bar()
            self._set_running(False)
            if error:
                self.log(f"error: {error}", "warn")
                self.status_var.set("Failed")
                messagebox.showerror("Scan failed", error, parent=self.root)
            else:
                self.status_var.set("Finished (dry run)" if self._last_dry else "Finished")

    # ---- progress display -------------------------------------------------
    def _reset_progress(self) -> None:
        self._stop_discovery_bar()
        for bar, text in self.bars.values():
            bar.configure(mode="determinate", maximum=100)
            bar["value"] = 0
            text.set("waiting")

    def _stop_discovery_bar(self) -> None:
        if self._disc_running:
            self.bars["Discovering"][0].stop()
            self._disc_running = False

    def _discovery(self, folders: int, images: int, final: bool) -> None:
        bar, text = self.bars["Discovering"]
        if final:
            self._stop_discovery_bar()
            bar.configure(mode="determinate", maximum=1)
            bar["value"] = 1
            text.set(f"done: {folders:,} folders, {images:,} images")
            return
        if not self._disc_running:
            bar.configure(mode="indeterminate")
            bar.start(12)
            self._disc_running = True
        text.set(f"{folders:,} folders, {images:,} images…")

    def _progress(self, label: str, count: int, total: int) -> None:
        bar, text = self.bars[label]
        if total <= 0:
            bar.configure(mode="determinate", maximum=1)
            bar["value"] = 0
            text.set("nothing to do")
            return
        bar.configure(mode="determinate", maximum=total)
        bar["value"] = count
        text.set(f"{count:,} / {total:,}  ({count * 100 // total}%)")

    def _progress_done(self, label: str, total: int) -> None:
        if total > 0:
            self._progress(label, total, total)

    # ---- misc -------------------------------------------------------------
    def _set_running(self, running: bool) -> None:
        self.running = running
        for widget in self._ttk_controls:
            widget.state(["disabled"] if running else ["!disabled"])
        for widget in self._tk_controls:
            widget.configure(state="disabled" if running else "normal")

    def log(self, text: str, tag: str | None = None) -> None:
        self.log_text.configure(state="normal")
        if tag:
            self.log_text.insert("end", text + "\n", tag)
        else:
            self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_quit(self) -> None:
        if self.running and not messagebox.askyesno(
            "Scan in progress",
            "A scan is still running. Quit anyway?\nFiles copied so far will be kept.",
            parent=self.root,
        ):
            return
        was_running = self.running
        self.root.destroy()
        if was_running:  # don't wait for the worker pool to drain
            os._exit(1)


def launch_gui(opts: Options) -> int:
    if tk is None:
        print(
            "error: the GUI needs Python's tkinter module, which isn't installed.\n"
            "Install it (Debian/Ubuntu: sudo apt install python3-tk) or add "
            "--no-gui to use the command line.",
            file=sys.stderr,
        )
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"error: couldn't open a window ({exc}).\n"
              "Add --no-gui to use the command line.", file=sys.stderr)
        return 1
    App(root, opts)
    root.mainloop()
    return 0


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="find_best_images.py",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="%(prog)s [options]                       (opens the window)\n"
              "       %(prog)s --no-gui [-s DIR [DIR ...]] [-t DIR] [options]",
        description=(
            "Scan directories for images that look the same, keep the best version of\n"
            "each (highest resolution, lossless favoured, then sharpest), and copy the\n"
            "winners into a 'search-result' directory that mirrors their original\n"
            "paths. Unchosen look-alikes get a '<name>.dup' text file holding the path\n"
            "the kept file was copied to (disable with --no-dup)."
        ),
        epilog=(
            "examples:\n"
            "  %(prog)s                          open the window\n"
            "  %(prog)s -s ~/pics -t /mnt/backup open the window, pre-filled\n"
            "  %(prog)s --no-gui                 command line: scan ~/Pictures, results in ~\n"
            "  %(prog)s --no-gui -s ~/pics\n"
            "  %(prog)s --no-gui -s ~/pics ~/downloads/photos -t /mnt/backup --progress\n"
            "  %(prog)s --no-gui -s ~/pics --dry-run -v\n"
            "  %(prog)s --no-gui -s ~/pics --no-copy-other --no-bad-files\n"
        ),
    )

    optional = p.add_argument_group("options")
    optional.add_argument(
        "-s", "--source", nargs="+", action="extend", metavar="DIR",
        help="directory (or directories) to scan recursively; may be given "
        "several times or with several values. With --no-gui the default is "
        "~/Pictures; the window starts with an empty list. Order matters: when "
        "look-alikes are otherwise tied, the file from the earlier source is kept",
    )
    optional.add_argument(
        "-g", "--gui", action=argparse.BooleanOptionalAction, default=True,
        help="open the graphical window (the default); the other options pre-fill "
        "its fields and nothing runs until you press Start scan. Use --no-gui to "
        "run in the command line only",
    )
    optional.add_argument(
        "-t", "--target", default=DEFAULT_TARGET, metavar="DIR",
        help="directory in which 'search-result' is created "
        "(default: ~, your home directory)",
    )
    optional.add_argument(
        "-T", "--threshold", type=int, default=Options.threshold, metavar="BITS",
        help="max differing bits (of 256) for two images to count as the same "
        "(default 5; lower = stricter)",
    )
    optional.add_argument(
        "--lossless-bonus", "--png-bonus", dest="lossless_bonus", type=float,
        default=Options.lossless_bonus, metavar="X",
        help="pixel-count multiplier for lossless formats (PNG, BMP, GIF, lossless "
        "TIFF/WebP) when ranking, to favour them over lossy ones such as JPG "
        "(default 1.10; use 1.0 for pure resolution, higher to favour lossless more)",
    )
    optional.add_argument(
        "--ratio-tol", type=float, default=0.03, metavar="X",
        help="allowed aspect-ratio difference between matches (default 0.03)",
    )
    optional.add_argument(
        "-j", "--jobs", type=int, default=os.cpu_count() or 1, metavar="N",
        help="worker processes (default: CPU count)",
    )
    optional.add_argument(
        "--no-dup", dest="dup", action="store_false",
        help="don't create .dup placeholder files for look-alikes that weren't kept",
    )
    optional.add_argument(
        "-c", "--copy-other", action=argparse.BooleanOptionalAction,
        default=Options.copy_other,
        help="also copy every non-image file found under the sources into "
        "'search-result', keeping its relative path (on by default; "
        "--no-copy-other turns it off)",
    )
    optional.add_argument(
        "-b", "--bad-files", dest="bad", action=argparse.BooleanOptionalAction,
        default=Options.bad,
        help="create a '<name>.bad' file in 'search-result' for each image that "
        "can't be read; it holds the file's path and the error (on by default; "
        "--no-bad-files turns it off)",
    )
    optional.add_argument(
        "--allow-sleep", dest="keep_awake", action="store_false",
        help="don't stop the computer from sleeping while the scan runs "
        "(by default sleep is prevented until the scan finishes)",
    )
    optional.add_argument(
        "-p", "--progress", action="store_true",
        help="show a progress bar for each phase of the scan (with --no-gui "
        "only; the window always shows progress)",
    )
    optional.add_argument(
        "-n", "--dry-run", action="store_true",
        help="report what would be copied without copying anything",
    )
    optional.add_argument(
        "-v", "--verbose", action="store_true",
        help="list each group of look-alikes and the chosen file",
    )
    optional.add_argument(
        "-h", "--help", action="help", help="show this help message and exit"
    )
    return p


GUI_CONTROL_HELP = """\
WINDOW CONTROLS AND THEIR COMMAND-LINE OPTIONS
  Sources list ............... -s / --source
  Target directory ........... -t / --target
  Create .dup files .......... --no-dup (turns it off)
  Copy non-image files ....... -c / --copy-other, --no-copy-other
  Create .bad files .......... -b / --bad-files, --no-bad-files
  Keep the computer awake .... --allow-sleep (turns it off)
  Dry run .................... -n / --dry-run
  Match threshold ............ -T / --threshold
  Lossless bonus ............. --lossless-bonus
  --ratio-tol, --jobs and --verbose have no control in the window; they can
  only be set on the command line; values given there still apply to scans
  started from the window.

COMMAND-LINE USAGE
"""


def help_text(width: int = 90) -> str:
    """The option usage shown by the window's Help button: a key to the
    controls, followed by the same text as --help."""
    parser = build_parser()
    parser.formatter_class = lambda prog: argparse.RawDescriptionHelpFormatter(
        prog, width=width
    )
    return GUI_CONTROL_HELP + "\n" + parser.format_help()


def options_from_args(args: argparse.Namespace) -> Options:
    return Options(
        sources=list(args.source or []),
        target=args.target,
        threshold=args.threshold,
        lossless_bonus=args.lossless_bonus,
        ratio_tol=args.ratio_tol,
        jobs=args.jobs,
        dup=args.dup,
        copy_other=args.copy_other,
        bad=args.bad,
        dry_run=args.dry_run,
        verbose=args.verbose,
        keep_awake=args.keep_awake,
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    opts = options_from_args(args)
    if args.gui:
        return launch_gui(opts)
    if not opts.sources:
        opts.sources = [DEFAULT_SOURCE]
    try:
        return run_scan(opts, ConsoleUI(args.progress))
    except ScanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if not args.source:
            print(f"(no -s was given, so the default source {DEFAULT_SOURCE} was used)",
                  file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
