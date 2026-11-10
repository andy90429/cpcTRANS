"""Local workbench logs and state. Never log exceptions or connection settings."""

import faulthandler
import json
import logging
import re
import sys
from contextlib import closing, contextmanager
from importlib.metadata import PackageNotFoundError, version
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import NamedTemporaryFile

from .db import Database
from .settings import config_path as check_config_path


LOGGER = logging.getLogger("fuelcore.runtime")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)
PROJECT = Path(__file__).resolve().parents[1]
UI_FILE = "fuelcore_ui.json"


def app_version():
    try:
        text = (PROJECT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        if match:
            return match[1]
    except (OSError, UnicodeError):
        pass
    try:
        return version("fuelcore")
    except PackageNotFoundError:
        return "未知版本"


def app_folder():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT


@contextmanager
def runtime_logging(config_path):
    try:
        folder = check_config_path(config_path).parent
    except ValueError:
        yield
        return
    handler = crash = None
    previously_enabled = faulthandler.is_enabled()
    crash_enabled = False
    try:
        try:
            handler = RotatingFileHandler(folder / "fuelcore_run.log", maxBytes=2 * 1024 * 1024,
                                          backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            LOGGER.addHandler(handler)
        except OSError:
            pass
        try:
            crash = (folder / "fuelcore_crash.log").open("ab", buffering=0)
            faulthandler.enable(file=crash)
            crash_enabled = True
        except (OSError, RuntimeError):
            pass
        yield
    finally:
        if handler is not None:
            LOGGER.removeHandler(handler)
            handler.close()
        if crash_enabled:
            faulthandler.disable()
        if crash is not None:
            crash.close()
        if crash_enabled and previously_enabled:
            try:
                faulthandler.enable(file=sys.__stderr__)
            except (OSError, RuntimeError, ValueError):
                pass


def record(operation, status, *, filename=None, count=None, seconds=None):
    fields = [operation, status]
    if filename is not None:
        try:
            fields.append("file=" + repr(Path(filename).name))
        except TypeError:
            pass
    if count is not None:
        fields.append(f"count={count}")
    if seconds is not None:
        fields.append(f"seconds={seconds:.3f}")
    LOGGER.info(" ".join(fields))


def recent_path(config_path):
    return Path(config_path).with_name(UI_FILE)


def valid_numbers(values):
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(value for value in values
                              if isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{14}", value)))


def save_recent(config_path, numbers):
    numbers = valid_numbers(numbers)
    if not numbers:
        return
    path = recent_path(config_path)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"last_imported": numbers}, stream)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def eligible_recent(config_path, settings):
    try:
        state = json.loads(recent_path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return []
    numbers = valid_numbers(state.get("last_imported")) if isinstance(state, dict) else []
    if not numbers:
        return []
    eligible = set()
    with closing(Database(settings, read_only=True)) as db:
        for start in range(0, len(numbers), 300):
            batch = numbers[start:start + 300]
            markers = ", ".join("?" for _ in batch)
            rows = db.select(
                f"SELECT SERSNO FROM cusdec WHERE MSGCODE = ? AND SERSNO IN ({markers}) "
                "AND COALESCE(UPPER(TRIM(STATUS)), '') NOT IN (?, ?) AND COALESCE(TRIM(`UPDATE`), '') <> ?",
                ("5203S", *batch, "S", "P", "*"),
            )
            eligible.update(row[0] for row in rows)
    return [number for number in numbers if number in eligible]
