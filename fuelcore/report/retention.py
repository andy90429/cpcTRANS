import json
import re
import stat
import time
from datetime import date
from pathlib import Path


MARKER = ".fuelcore-print-batch"
BATCH_NAME = re.compile(r"報單_[A-Z0-9]{14}_[a-z0-9_]{8}")
OUTPUT_NAME = re.compile(r"報單\.(?:Rpx|pdf)|比對差異\.txt|page-[0-9]{3,}\.png")
SEVEN_DAYS = 7 * 24 * 60 * 60
CATEGORIES = {"print": 30, "diag": 30, "generated": 7, "correction": 90}
OWNED_NAME = re.compile(r"(?:報單_[A-Z0-9]{14}|列印|更正|產生|診斷|模擬[送收]信_\d{8}_\d{6})_[a-z0-9_]{8}")
SUFFIXES = {"print": {".pdf", ".png"}, "diag": {".txt", ".xml"},
            "generated": {".rpx"}, "correction": {".xlsx"}}
DIAG_NAME = re.compile(r"每旬匯率模擬_\d{8}_\d{6}(?:_[a-z0-9_]{8})?\.txt")
FILE_MARKER = ".fuelcore-owned"


def mark_batch(folder, *, now=None):
    created = time.time() if now is None else now
    (folder / MARKER).write_text(json.dumps({"version": 1, "batch": folder.name, "created": created}), encoding="utf-8")


def mark_outputs(folder, files, *, now=None):
    created = time.time() if now is None else now
    data = {"version": 2, "batch": folder.name, "created": created, "files": files}
    (folder / MARKER).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def mark_diagnostic(path, *, now=None):
    created = time.time() if now is None else now
    marker = path.with_name("." + path.name + FILE_MARKER)
    with marker.open("x", encoding="utf-8") as stream:
        json.dump({"version": 1, "file": path.name, "created": created}, stream, ensure_ascii=False)


def ordinary(path, kind):
    info = path.lstat()
    return kind(info.st_mode) and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT


def cleanup_batches(output, *, now=None, days=None):
    root = Path(output).resolve()
    if not root.is_dir():
        return []
    now = time.time() if now is None else now
    limits = {key: 7 for key in CATEGORIES} if days is None else days
    removed = []
    for folder in root.iterdir():
        if folder.name.startswith(".") and folder.name.endswith(FILE_MARKER):
            file = root / folder.name[1:-len(FILE_MARKER)]
            if not DIAG_NAME.fullmatch(file.name) or not limits["diag"]:
                continue
            try:
                if not ordinary(folder, stat.S_ISREG):
                    continue
                marker = json.loads(folder.read_text(encoding="utf-8"))
                if (not isinstance(marker, dict) or marker.get("version") != 1 or marker.get("file") != file.name
                    or type(marker.get("created")) not in (int, float)
                    or not 0 <= marker["created"] < now - limits["diag"] * 86400):
                    continue
                if file.exists() or file.is_symlink():
                    if not ordinary(file, stat.S_ISREG):
                        continue
                    file.unlink()
                    removed.append(file)
                folder.unlink()
            except (OSError, ValueError):
                pass
            continue
        if not OWNED_NAME.fullmatch(folder.name):
            continue
        marker = folder / MARKER
        try:
            if not ordinary(folder, stat.S_ISDIR) or folder.resolve().parent != root or not ordinary(marker, stat.S_ISREG):
                continue
            ownership = json.loads(marker.read_text(encoding="utf-8"))
            if (not isinstance(ownership, dict) or ownership.get("batch") != folder.name
                or type(ownership.get("created")) not in (int, float) or not 0 <= ownership["created"] <= now):
                continue
            if not any(limit and now - ownership["created"] > limit * 86400 for limit in limits.values()):
                continue
            outputs = [file for file in folder.iterdir() if file.name != MARKER]
            if not all(ordinary(file, stat.S_ISREG) for file in outputs):
                continue
            if ownership.get("version") == 1 and BATCH_NAME.fullmatch(folder.name):
                if not all(OUTPUT_NAME.fullmatch(file.name) for file in outputs):
                    continue
                kinds = {file.name: {".rpx": "generated", ".txt": "diag"}.get(file.suffix.lower(), "print")
                         for file in outputs}
            elif ownership.get("version") == 2:
                kinds = ownership.get("files")
                if (not isinstance(kinds, dict) or not all(
                    isinstance(kind, str) and kind in SUFFIXES and Path(name).name == name
                    and Path(name).suffix.lower() in SUFFIXES[kind] for name, kind in kinds.items()
                ) or any(file.name not in kinds for file in outputs)):
                    continue
            else:
                continue
            for file in outputs:
                limit = limits[kinds[file.name]]
                if limit and now - ownership["created"] > limit * 86400:
                    try:
                        file.unlink()
                    except OSError:
                        pass
            if not any(file.name != MARKER for file in folder.iterdir()):
                marker.unlink()
                folder.rmdir()
                removed.append(folder)
        except (OSError, ValueError):
            continue
    return removed


def daily_cleanup(base, days, *, today=None):
    base = Path(base)
    state = base / ".fuelcore-cleanup"
    state.mkdir(exist_ok=True)
    if not ordinary(state, stat.S_ISDIR):
        return []
    day = (today or date.today()).strftime("%Y%m%d")
    try:
        with (state / day).open("x"):
            pass
    except FileExistsError:
        return []
    removed = []
    for root in (base / "列印輸出", base / "診斷"):
        if root.exists() and ordinary(root, stat.S_ISDIR):
            removed.extend(cleanup_batches(root, days=days))
    return removed
