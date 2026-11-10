import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fuelcore.settings import Settings, read_config
from fuelcore.workbench import IMPORTED, convert_batch


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "fuelcore.ini.example"
SAMPLES = ROOT / "資料" / "txt樣本"


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.source = self.folder / "TXT"
        self.source.mkdir()
        self.ini = self.folder / "fuelcore.ini"
        self.config = read_config(EXAMPLE)
        self.config["paths"] = {"txt_path": "TXT"}
        self.config["workflow"]["check_after_convert"] = "no"
        self.config["workflow"].pop("archive_after_convert", None)
        self.settings = Settings(self.config, self.folder)
        self.notify = Mock()

    def sample(self, index, folder=None, name=None):
        original = SAMPLES / f"D4122_TEST_{index:04d}.txt"
        target = (folder or self.source) / (name or original.name)
        shutil.copyfile(original, target)
        return target

    def test_only_successful_root_txt_is_archived(self):
        self.assertTrue(self.settings.archive_after_convert)
        root_file = self.sample(1, name="TEST.TXT")
        failed = self.source / "INVALID.txt"
        failed.write_text("TEST INVALID", encoding="utf-8")
        nested = self.source / "20260101"
        nested.mkdir()
        dated = self.sample(2, nested)
        outside = self.sample(3, self.folder)
        before = {path: path.read_bytes() for path in (root_file, failed, dated, outside)}
        archived = Mock()
        now = datetime(2026, 11, 10, 14, 30)
        with patch("fuelcore.workbench.datetime") as clock:
            clock.now.return_value = now
            ids, _ = convert_batch([root_file, failed, dated, outside], self.settings, self.notify,
                                   on_archive=archived)
        self.assertEqual(ids, ["TEST00A10E0001", "TEST00A10E0002", "TEST00A10E0003"])
        self.assertEqual([call.args[1].ok for call in self.notify.call_args_list], [True, False, True, True])
        target = self.source / "20261110" / root_file.name
        self.assertFalse(root_file.exists())
        self.assertEqual(target.read_bytes(), before[root_file])
        archived.assert_called_once_with(root_file, target)
        for path in (failed, dated, outside):
            self.assertEqual(path.read_bytes(), before[path])

    def test_collision_adds_timestamp_and_preserves_existing_files(self):
        original = self.sample(1, name="TEST.txt")
        data = original.read_bytes()
        now = datetime(2026, 11, 10, 14, 30, 1)
        dest = self.source / "20261110"
        dest.mkdir()
        first = dest / original.name
        second = dest / "TEST.143001_000000.txt"
        first.write_bytes(b"TEST EXISTING 1")
        second.write_bytes(b"TEST EXISTING 2")
        with patch("fuelcore.workbench.datetime") as clock:
            clock.now.return_value = now
            convert_batch([original], self.settings, self.notify)
        self.assertEqual(first.read_bytes(), b"TEST EXISTING 1")
        self.assertEqual(second.read_bytes(), b"TEST EXISTING 2")
        self.assertEqual((dest / "TEST.143001_000000_2.txt").read_bytes(), data)
        self.assertFalse(original.exists())

    def test_switch_off_keeps_successfully_converted_file(self):
        self.config["workflow"]["archive_after_convert"] = "no"
        settings = Settings(self.config, self.folder)
        original = self.sample(1)
        with patch("fuelcore.workbench.archive_txt") as archive:
            ids, _ = convert_batch([original], settings, self.notify)
        self.assertEqual(ids, ["TEST00A10E0001"])
        self.assertTrue(original.exists())
        archive.assert_not_called()

    def test_move_failure_keeps_commit_and_runs_logic_check(self):
        self.settings.check_after_convert = True
        original = self.sample(1)
        data = original.read_bytes()
        with patch("fuelcore.workbench.move_without_overwriting", side_effect=PermissionError("TEST DENIED")), \
                patch("fuelcore.workbench.run_logic_check") as check:
            ids, result = convert_batch([original], self.settings, self.notify)
        self.assertEqual(ids, ["TEST00A10E0001"])
        check.assert_called_once_with(ids, self.settings, save=True)
        self.assertIs(result, check.return_value)
        _, conversion = self.notify.call_args.args
        self.assertTrue(conversion.ok)
        notes = [note for note in conversion.notes if note.startswith("歸檔失敗")]
        self.assertEqual(notes, [f"歸檔失敗 {original.name}：TEST DENIED"])
        self.assertEqual(original.read_bytes(), data)
        with closing(sqlite3.connect(self.settings.path)) as db:
            self.assertEqual(db.execute("SELECT SERSNO FROM cusdec").fetchall(), [(ids[0],)])

    def test_workbench_updates_a_manually_added_root_path(self):
        import tkinter as tk

        from fuelcore.gui import Workbench

        original = self.sample(1)
        with self.ini.open("w", encoding="utf-8") as stream:
            self.config.write(stream)
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))

        def destroy():
            for callback in root.tk.call("after", "info"):
                root.after_cancel(callback)
            root.destroy()

        self.addCleanup(destroy)
        root.withdraw()
        app = Workbench(root, self.ini)

        def wait():
            deadline = time.monotonic() + 8
            while app.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertFalse(app.busy)

        app.extra.add(original)
        root.update()
        wait()
        with patch("fuelcore.gui.messagebox.showinfo"):
            app.start_button.invoke()
            self.assertTrue(app.busy)
            wait()
        target = self.source / f"{date.today():%Y%m%d}" / original.name
        self.assertEqual(app.extra, {target})
        self.assertFalse(original.exists())
        self.assertTrue(target.exists())
        item, = app.rows.values()
        self.assertEqual((item.path, item.status), (target, IMPORTED))
