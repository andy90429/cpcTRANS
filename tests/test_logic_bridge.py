import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from fuelcore import Settings, convert_file
from fuelcore.logic_check import run_logic_check
from fuelcore.schema import identifier, init_sqlite
from test_fuelcore import EXAMPLE
import test_fuelcore as transfer_tests


ROOT = Path(__file__).resolve().parents[1]
NUMBERS = tuple(f"TEST00A10E000{n}" for n in range(1, 7))
STUB = Path(__file__).with_name("fixtures") / "check_stub" / "run_check.py"


@pytest.fixture
def fake_plugin(tmp_path, monkeypatch):
    folder = tmp_path / "plugin"
    folder.mkdir()
    shutil.copyfile(STUB, folder / STUB.name)
    monkeypatch.setenv("FUELCORE_LOGIC_PLUGIN", str(folder))
    cfg = Settings.load(EXAMPLE)
    cfg.path = tmp_path / "fuelcore_test.sqlite3"
    return cfg, folder


def started(folder):
    value = json.loads((folder / "started.json").read_text(encoding="utf-8"))
    assert not Path(value["path"]).exists()
    return value


def test_pass_failed_and_unparsed_in_request_order(fake_plugin):
    cfg, folder = fake_plugin
    run = run_logic_check(NUMBERS[:3], cfg)
    assert run.plugin_version == "fake"
    assert [result.sersno for result in run.results] == list(NUMBERS[:3])
    assert [result.status for result in run.results] == ["passed", "failed", "failed"]
    assert [result.passed for result in run.results] == [True, False, False]
    assert run.results[1].messages[0].field == "數量"
    assert run.results[2].rules_unparsed == 1
    assert started(folder)["save"] is False


@pytest.mark.parametrize("index,expected", [(3, "逾時"), (4, "JSON"), (5, "報單號碼")])
def test_errors_and_temporary_file_cleanup(fake_plugin, index, expected):
    cfg, folder = fake_plugin
    cfg.logic_check = replace(cfg.logic_check, timeout_seconds=2)
    result, = run_logic_check([NUMBERS[index]], cfg).results
    assert result.status == "error"
    assert expected in result.messages[0].text
    started(folder)


def test_missing_plugin(fake_plugin):
    cfg, folder = fake_plugin
    (folder / STUB.name).unlink()
    result, = run_logic_check([NUMBERS[0]], cfg).results
    assert result.label == "未檢查"
    assert "找不到" in result.messages[0].text


def test_save_requires_test_database_and_opt_in(fake_plugin):
    cfg, folder = fake_plugin
    cfg.path = cfg.path.with_name("production.sqlite3")
    assert run_logic_check([NUMBERS[0]], cfg, save=True).results[0].status == "error"
    assert not (folder / "started.json").exists()
    cfg.path = cfg.path.with_name("fuelcore_test.sqlite3")
    cfg.test_only = False
    assert run_logic_check([NUMBERS[0]], cfg, save=True).results[0].status == "error"
    assert not (folder / "started.json").exists()
    cfg.test_only = True
    assert run_logic_check([NUMBERS[0]], cfg, save=True).results[0].saved
    assert started(folder)["save"] is True


def test_save_rejects_non_test_mysql_rules_schema_before_launch(fake_plugin):
    cfg, folder = fake_plugin
    cfg.driver = "mysql"
    cfg.database, cfg.code_database = "test_declarations", "test_codes"
    cfg.logic_check = replace(cfg.logic_check, rules_database="cbssetcontent")
    assert run_logic_check([NUMBERS[0]], cfg, save=True).results[0].status == "error"
    assert not (folder / "started.json").exists()


def test_cli_conclusions_and_json_output(fake_plugin, tmp_path):
    cfg, folder = fake_plugin
    output = tmp_path / "result.json"
    child = subprocess.run([sys.executable, "-m", "fuelcore", "check", *NUMBERS[:2], "--config",
                            str(EXAMPLE), "--output", str(output)], cwd=ROOT,
                           capture_output=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
    assert child.returncode == 1
    assert "✓ 通過" in child.stdout and "✗ 未通過" in child.stdout
    assert "數量" in child.stdout
    assert json.loads(output.read_text(encoding="utf-8"))["plugin_version"] == "fake"
    assert started(folder)["save"] is False


def test_tracked_files_exclude_legacy_modules_and_local_assets():
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    old_names = {"logiccheck.py", "netcompat.py", "custom_functions.py", "mysql_compat.py",
                 "check_db.py", "sqlite_backend.py"}
    forbidden_parts = {"txtrans", "expressions", "搬移包_邏輯檢查", "只放本機_邏輯檢查外掛"}
    assert paths and not any(
        Path(path).name.casefold() in old_names
        or forbidden_parts.intersection(part.casefold() for part in Path(path).parts)
        or Path(path).suffix.casefold() in {".ini", ".sqlite", ".sqlite3", ".db"}
        or (Path(path).name.casefold() == "run_check.py" and Path(path) != STUB.relative_to(ROOT))
        for path in paths if path
    )


def test_workbench_checks_imported_selection_in_background(fake_plugin):
    _, folder = fake_plugin
    command = ("import sys; sys.path.insert(0, 'tests'); "
               "from test_logic_bridge import exercise_workbench; exercise_workbench(sys.argv[1])")
    child = subprocess.run([sys.executable, "-X", "utf8", "-c", command, str(folder)], cwd=ROOT,
                           capture_output=True, encoding="utf-8", timeout=15)
    if child.returncode == 77:
        pytest.skip(child.stdout.strip())
    assert child.returncode == 0, child.stderr
    assert started(folder)["save"] is False


def exercise_workbench(folder):
    import tkinter as tk

    from fuelcore.gui import Workbench
    from fuelcore.workbench import IMPORTED, InputFile

    folder = Path(folder)
    try:
        root = tk.Tk()
    except tk.TclError as error:
        print(error)
        raise SystemExit(77)
    root.withdraw()
    try:
        workbench = Workbench(root, EXAMPLE)
        workbench.refresh = lambda: None

        def wait():
            limit = time.monotonic() + 8
            while workbench.busy and time.monotonic() < limit:
                root.update()
                time.sleep(0.01)
            assert not workbench.busy

        workbench.show_files((folder, [InputFile(folder / "fixture.txt", "", NUMBERS[0], IMPORTED)], []))
        wait()
        row, = workbench.files.get_children()
        assert workbench.files.set(row, "logic") == "未檢查"
        workbench.files.selection_set(row)
        workbench.update_buttons()
        workbench.check_button.invoke()
        assert workbench.busy
        wait()
        assert workbench.files.set(row, "logic") == "✓ 通過"
    finally:
        root.destroy()


def import_cbs_tables(db, folder):
    required = {"cbssetcontent." + name for name in (
        "tb_setifform", "cbsformtype", "cbsfielddata", "tb_setifvar", "tb_setiftype",
        "tb_setiftype_client", "tb_setiftype_item", "tb_setiflayer", "tb_errormsg"
    )} | {"code4.mydeclarerate"}
    paths = {path.stem.casefold(): path for path in folder.glob("*.csv")
             if path.stem.casefold().startswith(("cbssetcontent.", "code4."))}
    if not required <= paths.keys():
        pytest.skip("FUELCORE_CBS_CONFIG 缺少正式邏輯規則或匯率 CSV")
    for key, path in paths.items():
        table = identifier(key.split(".", 1)[1])
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = csv.reader(stream)
            columns = next(rows)
            integer = {"porder", "piflayer", "piftype", "pfieldindex", "dr004", "plogictype"}
            fields = ", ".join(f"{identifier(name)} {'INTEGER' if name.casefold() in integer else 'TEXT'}"
                               for name in columns)
            db.execute(f"CREATE TABLE IF NOT EXISTS {table} ({fields})")
            markers = ", ".join("?" for _ in columns)
            db.executemany(f"INSERT INTO {table} VALUES ({markers})", rows)


# The rule tables use the CBS layout, so the made-up test rules live beside the plug-in, outside Git.
SAMPLE_RULES = Path("test_rules") / "sample_rules.sql"
BROKEN_RULE = Path("test_rules") / "broken_rule.sql"
SAMPLE_DEFECTS = (
    ("數量", "UPDATE invitem SET `數量` = '0.0000' WHERE SERSNO = ?"),
    ("報單品名", "UPDATE invitem SET `報單品名` = '' WHERE SERSNO = ?"),
    ("MARKS", "UPDATE cusdec SET MARKS = '' WHERE SERSNO = ?"),
    ("FOREX", "UPDATE cusdec SET FOREX = '30.00000' WHERE SERSNO = ?"),
)


@pytest.fixture
def local_plugin(tmp_path):
    """The real plug-in, a database from the transfer core, and made-up rules."""
    folder = os.getenv("FUELCORE_LOGIC_PLUGIN")
    if not folder:
        pytest.skip("需要 FUELCORE_LOGIC_PLUGIN 指向本機邏輯檢查外掛")
    folder = Path(folder)
    assert (folder / "run_check.py").is_file(), "FUELCORE_LOGIC_PLUGIN 裡找不到 run_check.py"
    if not (folder / SAMPLE_RULES).is_file() or not (folder / BROKEN_RULE).is_file():
        pytest.skip("本機外掛資料夾缺少 test_rules 的自編測試規則")
    cfg = Settings.load(EXAMPLE)
    cfg.path = tmp_path / "fuelcore_test.sqlite3"
    # Optional: run the plug-in with the interpreter used for packaging, for example Python 3.7 32-bit.
    cfg.logic_check = replace(cfg.logic_check, python_exe=os.getenv("FUELCORE_LOGIC_PYTHON", ""))
    init_sqlite(cfg)
    numbers = NUMBERS[:len(SAMPLE_DEFECTS) + 1]
    for number in numbers:
        path = transfer_tests.DatabaseTest.sample(SimpleNamespace(folder=tmp_path), number[-4:])
        assert convert_file(path, cfg).ok
    with closing(sqlite3.connect(cfg.path)) as db, db:
        db.executescript((folder / SAMPLE_RULES).read_text(encoding="utf-8"))
        for number, (_, sql) in zip(numbers[1:], SAMPLE_DEFECTS):
            assert db.execute(sql, (number,)).rowcount
    return cfg, numbers


def declarations(cfg):
    with closing(sqlite3.connect(cfg.path)) as db:
        return {number: tuple(rest) for number, *rest in db.execute(
            "SELECT SERSNO, STATUS, `原狀態`, `是否已邏輯` FROM cusdec")}


def test_local_plugin_reads_transfer_output_without_writing(local_plugin):
    cfg, numbers = local_plugin
    baseline = cfg.path.read_bytes()
    correct, *invalid = run_logic_check(numbers, cfg).results
    assert correct.passed and correct.basic_passed and correct.item_passed, correct
    assert correct.rules_total > 0 and not correct.messages and not correct.saved
    for result, (field, _) in zip(invalid, SAMPLE_DEFECTS):
        assert result.status == "failed" and result.rules_unparsed == 0, result
        assert [message.field for message in result.messages] == [field], result
    assert invalid[0].basic_passed and not invalid[0].item_passed
    assert not invalid[1].basic_passed and invalid[1].item_passed
    assert [message.item for message in invalid[0].messages + invalid[1].messages] == [1, 1]
    assert cfg.path.read_bytes() == baseline


def test_local_plugin_save_writes_status_and_error_list(local_plugin):
    cfg, numbers = local_plugin
    results = run_logic_check(numbers, cfg, save=True).results
    assert all(result.saved for result in results)
    states = declarations(cfg)
    assert states[numbers[0]] == ("Y", "0", 1)
    assert all(states[number] == ("0", "0", 0) for number in numbers[1:])
    with closing(sqlite3.connect(cfg.path)) as db:
        saved = sorted(db.execute("SELECT SERSNO, PFIELD, PLOGICTYPE FROM tb_errormsg"))
    assert saved == sorted([(numbers[1], "數量", 2), (numbers[2], "報單品名", 1),
                            (numbers[3], "MARKS", 1), (numbers[4], "FOREX", 1)])
    # A corrected declaration passes on the next run, and its old errors are removed.
    with closing(sqlite3.connect(cfg.path)) as db, db:
        db.execute("UPDATE cusdec SET MARKS = 'N/M' WHERE SERSNO = ?", (numbers[3],))
    assert run_logic_check([numbers[3]], cfg, save=True).results[0].passed
    assert declarations(cfg)[numbers[3]] == ("Y", "0", 1)
    with closing(sqlite3.connect(cfg.path)) as db:
        assert not db.execute("SELECT 1 FROM tb_errormsg WHERE SERSNO = ?", (numbers[3],)).fetchall()


def test_local_plugin_never_passes_what_it_cannot_check(local_plugin):
    cfg, numbers = local_plugin
    missing, = run_logic_check(["TEST00A10E9999"], cfg, save=True).results
    assert missing.status == "error" and not missing.saved
    broken_rule = Path(os.environ["FUELCORE_LOGIC_PLUGIN"]) / BROKEN_RULE
    with closing(sqlite3.connect(cfg.path)) as db, db:
        db.executescript(broken_rule.read_text(encoding="utf-8"))
    broken, = run_logic_check(numbers[:1], cfg, save=True).results
    assert broken.status == "failed" and broken.rules_unparsed == 1 and not broken.passed
    assert declarations(cfg)[numbers[0]] == ("0", "0", 0)
    with closing(sqlite3.connect(cfg.path)) as db, db:
        db.execute("DROP TABLE tb_setifform")
    unusable, = run_logic_check(numbers[:1], cfg).results
    assert unusable.status == "error" and not unusable.passed


def test_local_plugin_with_cbs_rules(tmp_path):
    if not os.getenv("FUELCORE_LOGIC_PLUGIN") or not os.getenv("FUELCORE_CBS_CONFIG"):
        pytest.skip("需要 FUELCORE_LOGIC_PLUGIN 與 FUELCORE_CBS_CONFIG")
    cfg = Settings.load(EXAMPLE)
    cfg.path = tmp_path / "fuelcore_test.sqlite3"
    init_sqlite(cfg)
    with closing(sqlite3.connect(cfg.path)) as db, db:
        import_cbs_tables(db, Path(os.environ["FUELCORE_CBS_CONFIG"]))
    for index in range(1, 5):
        path = transfer_tests.DatabaseTest.sample(SimpleNamespace(folder=tmp_path), f"{index:04d}")
        assert convert_file(path, cfg).ok
    changes = (("數量", 0), ("報單品名", ""), ("DUTYAMT", 1))
    with closing(sqlite3.connect(cfg.path)) as db, db:
        for number, (column, value) in zip(NUMBERS[1:4], changes):
            db.execute(f"UPDATE invitem SET {identifier(column)} = ? WHERE SERSNO = ?", (value, number))
    baseline = cfg.path.read_bytes()
    correct, *invalid = run_logic_check(NUMBERS[:4], cfg).results
    assert correct.passed, correct
    for result, (column, _) in zip(invalid, changes):
        assert result.status == "failed"
        assert any(message.field.casefold() == column.casefold() for message in result.messages), result
    assert cfg.path.read_bytes() == baseline
