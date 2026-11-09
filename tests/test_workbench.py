import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from fuelcore.schema import init_sqlite
from fuelcore.settings import Settings, read_config
from fuelcore.workbench import (
    FAILED, IMPORTED, PENDING, UNKNOWN, UNREADABLE, file_statuses, find_inputs, imported_numbers,
    load_fields, resolve_folder, save_fields, scan_inputs, test_database as verify_database,
)


EXAMPLE = Path(__file__).resolve().parents[1] / "fuelcore.ini.example"


def sample(path, number, modified):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["X0000", number] + [""] * 36
    path.write_text("|".join(header) + "\n" + "|" * 16, encoding="utf-8")
    os.utime(path, (modified, modified))
    return path


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.config = self.folder / "fuelcore.ini"
        self.config.with_suffix(".ini.example").write_bytes(EXAMPLE.read_bytes())

    def test_config_defaults_and_round_trip(self):
        txt = self.folder / "測試 檔案"
        txt.mkdir()
        cbs = self.folder / "CBS APP"
        cbs.mkdir()
        (cbs / "CBSAPP.exe").touch()
        fields = load_fields(self.config)
        self.assertEqual((fields["txt_path"], fields["driver"]), ("", "sqlite"))
        fields.update(txt_path=txt.name, cbsapp_path=str(cbs), driver="mysql",
                      host="127.0.0.1", port="3307", user="tester", password="example%value",
                      database="fuelcore_test", code_database="fuelcore_test_code")
        save_fields(self.config, fields)
        self.assertEqual(load_fields(self.config), fields)
        self.assertEqual(Settings.load(self.config).mysql["port"], 3307)
        original = read_config(EXAMPLE)
        updated = read_config(self.config)
        for section in ("conversion", "buyer_countries", "products"):
            self.assertEqual(dict(updated[section]), dict(original[section]))

    def test_legacy_fuelcore_config_can_add_paths(self):
        config = read_config(EXAMPLE)
        config.remove_section("paths")
        with self.config.open("w", encoding="utf-8") as stream:
            config.write(stream)
        fields = load_fields(self.config)
        save_fields(self.config, fields)
        self.assertEqual(load_fields(self.config), fields)

    def test_invalid_database_name_leaves_existing_config_intact(self):
        fields = load_fields(self.config)
        save_fields(self.config, fields)
        previous = self.config.read_bytes()
        fields.update(driver="mysql", host="127.0.0.1", user="tester",
                      database="cbsdata4", code_database="fuelcore_test_code")
        with self.assertRaisesRegex(ValueError, "test"):
            save_fields(self.config, fields)
        self.assertEqual(self.config.read_bytes(), previous)

    def test_rejects_invalid_port_and_does_not_read_legacy_ini(self):
        fields = load_fields(self.config)
        fields.update(driver="mysql", port="65536", host="127.0.0.1", user="tester",
                      database="fuelcore_test", code_database="fuelcore_test_code")
        with self.assertRaisesRegex(ValueError, "port"):
            save_fields(self.config, fields)
        self.assertFalse(self.config.exists())
        for name in ("TxTrans.ini", "CPCtrans.INI"):
            with self.assertRaisesRegex(ValueError, name):
                Settings.load(self.folder / name)

    def test_sqlite_connection_check_leaves_missing_database_uncreated(self):
        settings = Settings.load(self.config.with_suffix(".ini.example"))
        self.assertIn("SQLite", verify_database(settings))
        self.assertFalse(settings.path.exists())

    def test_resolve_paths_and_report_missing_executable(self):
        self.assertIsNone(resolve_folder(self.folder, "txt_path", ""))
        self.assertEqual(resolve_folder(self.folder, "txt_path", "."), self.folder.resolve())
        with self.assertRaises(ValueError) as missing:
            resolve_folder(self.folder, "txt_path", "missing")
        self.assertIn(str(self.folder / "missing"), str(missing.exception))
        with self.assertRaises(ValueError) as missing:
            resolve_folder(self.folder, "cbsapp_path", ".")
        self.assertIn(str(self.folder / "CBSAPP.exe"), str(missing.exception))
        (self.folder / "CBSAPP.exe").touch()
        self.assertEqual(resolve_folder(self.folder, "cbsapp_path", "."), self.folder.resolve())

    def test_recursive_list_sorted_and_deduplicated(self):
        root = self.folder / "TXT"
        first = sample(root / "20260102" / "old.txt", "TEST00A10E0001", 100)
        second = sample(root / "20260103" / "new.TXT", "TEST00A10E0002", 200)
        extra = sample(self.folder / "outside" / "added.txt", "TEST00A10E0003", 300)
        (root / "skip.ini").touch()
        bad = root / "invalid.txt"
        bad.write_text("invalid", encoding="utf-8")
        files = find_inputs(root.resolve(), [first, extra], show_all=True)
        self.assertEqual([file.path for file in files], [bad, extra, second, first])
        self.assertEqual(files[2].folder, "20260103")
        self.assertEqual(files[2].sersno, "TEST00A10E0002")
        self.assertEqual(files[0].status, UNREADABLE)

    def test_scan_200_date_folders_with_1000_mixed_txt_files(self):
        today = date(2026, 11, 9)
        root = self.folder / "TXT"
        for offset in range(200):
            day = today - timedelta(days=offset)
            for sequence in range(5):
                suffix = ".TXT" if sequence == 4 else ".txt"
                path = root / day.strftime("%Y%m%d") / f"D4122_{day:%m%d}_{sequence:04d}{suffix}"
                sample(path, f"TEST00A10E{offset * 5 + sequence:04d}", 2000000000 - offset)
                if offset in (0, 199) and sequence in (0, 1):
                    path.write_bytes(b"" if sequence == 0 else b"\xff\xfe")
        fields = load_fields(self.config)
        fields["txt_path"] = "TXT"
        save_fields(self.config, fields)
        folder, recent, warnings = scan_inputs(self.config, today=today)
        self.assertEqual(folder, root.resolve())
        self.assertEqual(warnings, [])
        self.assertEqual(len(recent), 300)
        self.assertEqual(sum(file.status == PENDING for file in recent), 298)
        self.assertEqual(sum(file.path.suffix == ".TXT" for file in recent), 60)
        self.assertEqual({file.folder for file in recent},
                         {(today - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(60)})

        _, all_files, warnings = scan_inputs(self.config, show_all=True, today=today)
        self.assertEqual(warnings, [])
        self.assertEqual(len(all_files), 1000)
        self.assertEqual(len({file.folder for file in all_files}), 200)
        self.assertEqual(sum(file.status == PENDING for file in all_files), 996)
        self.assertEqual(sum(file.path.suffix == ".TXT" for file in all_files), 200)
        errors = [file.error for file in all_files if file.status == UNREADABLE]
        self.assertEqual(len(errors), 4)
        self.assertTrue(all(errors))

    def test_refresh_reads_changed_ini_and_logs_empty_scan_path(self):
        original = self.folder / "原始 TXT"
        path = sample(original / "report.txt", "TEST00A10E0001", 100)
        empty = self.folder / "空目錄"
        empty.mkdir()
        fields = load_fields(self.config)
        fields["txt_path"] = original.name
        save_fields(self.config, fields)
        self.assertEqual([file.path for file in scan_inputs(self.config)[1]], [path])
        fields["txt_path"] = empty.name
        save_fields(self.config, fields)
        folder, files, warnings = scan_inputs(self.config)
        self.assertEqual(folder, empty.resolve())
        self.assertEqual(files, [])
        self.assertIn(f"未找到 TXT，掃描目錄：{empty.resolve()}", warnings)

    def test_status_queries_test_db_and_prefers_committed_row(self):
        root = self.folder / "TXT"
        first = sample(root / "first.txt", "TEST00A10E0001", 200)
        second = sample(root / "second.txt", "TEST00A10E0002", 100)
        settings = Settings.load(self.config.with_suffix(".ini.example"))
        self.assertEqual(imported_numbers(settings, ["TEST00A10E0001"]), set())
        init_sqlite(settings)
        with closing(sqlite3.connect(settings.path)) as db:
            db.execute('DROP TABLE cusdec')
            db.execute('CREATE TABLE cusdec (MSGCODE TEXT, SERSNO TEXT)')
            db.executemany('INSERT INTO cusdec VALUES (?, ?)', [
                ('5203S', 'TEST00A10E0001'), ('other', 'TEST00A10E0002'),
            ])
            db.commit()
        files = find_inputs(root.resolve())
        imported = imported_numbers(settings, (file.sersno for file in files))
        self.assertEqual(imported, {"TEST00A10E0001"})
        self.assertEqual([file.status for file in file_statuses(files, imported, {})], [IMPORTED, PENDING])
        failures = {first: "舊錯誤", second: "轉檔失敗"}
        updated = list(file_statuses(files, imported, failures))
        self.assertEqual([file.status for file in updated], [IMPORTED, FAILED])
        self.assertEqual(updated[1].error, "轉檔失敗")
        self.assertEqual([file.status for file in file_statuses(files, None, {})], [UNKNOWN, UNKNOWN])


if __name__ == "__main__":
    unittest.main()
