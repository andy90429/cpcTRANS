import json
import re
import stat
import time
from pathlib import Path


MARKER = ".fuelcore-print-batch"
BATCH_NAME = re.compile(r"報單_[A-Z0-9]{14}_[a-z0-9_]{8}")
OUTPUT_NAME = re.compile(r"報單\.(?:Rpx|pdf)|比對差異\.txt|page-[0-9]{3,}\.png")
SEVEN_DAYS = 7 * 24 * 60 * 60


def mark_batch(folder, *, now=None):
    created = time.time() if now is None else now
    (folder / MARKER).write_text(json.dumps({"version": 1, "batch": folder.name, "created": created}), encoding="utf-8")


def ordinary(path, kind):
    info = path.lstat()
    return kind(info.st_mode) and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT


def cleanup_batches(output, *, now=None):
    root = Path(output).resolve()
    if not root.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - SEVEN_DAYS
    removed = []
    for folder in root.iterdir():
        if not BATCH_NAME.fullmatch(folder.name):
            continue
        marker = folder / MARKER
        try:
            if not ordinary(folder, stat.S_ISDIR) or folder.resolve().parent != root or not ordinary(marker, stat.S_ISREG):
                continue
            ownership = json.loads(marker.read_text(encoding="utf-8"))
            if (not isinstance(ownership, dict) or ownership.get("version") != 1 or ownership.get("batch") != folder.name
                or type(ownership.get("created")) not in (int, float) or not 0 <= ownership["created"] < cutoff):
                continue
            outputs = [file for file in folder.iterdir() if file.name != MARKER]
            if not all(OUTPUT_NAME.fullmatch(file.name) and ordinary(file, stat.S_ISREG) for file in outputs):
                continue
            for file in outputs:
                file.unlink()
            marker.unlink()
            folder.rmdir()
            removed.append(folder)
        except (OSError, ValueError):
            continue
    return removed
