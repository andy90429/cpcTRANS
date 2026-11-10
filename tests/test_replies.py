import json
import sqlite3
import tempfile
import time
from threading import Event
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fuelcore.replies import ReplyError, expand_pdf_paths, poll, today_replies
from fuelcore.reply_view import NO_EDI, print_attachment
from fuelcore.settings import Settings, read_config
from test_edi import CONFIG, REPORTS, load_fixture_tables


FIRST, SECOND = REPORTS


class RepliesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.config = self.folder / "fuelcore.ini"
        ini = read_config(CONFIG)
        ini["edi"] = dict(edi_database="test_edi", xmlset_database="test_xmlset",
                          company_code="00000000", user="TEST")
        ini["notify"]["enabled"] = "yes"
        ini["notify"]["c3_pdfs"] = "{SERSNO}-{BAID}-*.pdf\n找不到-{BYNAME}.pdf"
        with self.config.open("w", encoding="utf-8") as stream:
            ini.write(stream)
        self.settings = Settings.load(self.config)
        self.state = self.config.with_suffix(".notify-state.json")
        with closing(sqlite3.connect(self.settings.path)) as db:
            load_fixture_tables(db)
        self.today = date.today()

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.settings.path)) as db, db:
            db.execute(sql, params)

    def notice(self, main_id, number, method, requirement, *, day=None):
        day = day or self.today
        self.execute("INSERT INTO n5107 (MainID,`報單號碼`,`通關方式`,`接收日期時間`) VALUES (?,?,?,?)",
                     (main_id, number, method, day.isoformat() + " 08:15:00"))
        self.execute("INSERT INTO n5107_11 (MainID,`應補辦事項`) VALUES (?,?)", (main_id, requirement))

    def release(self, main_id, number, method):
        self.execute("INSERT INTO n5204 (MainID,`報單號碼`,`通關方式`,`接收日期時間`,`放行日期時間`) VALUES (?,?,?,?,?)",
                     (main_id, number, method, self.today.strftime("%Y/%m/%d") + " 08:35:00", "08:30:00"))

    def watch(self):
        return poll(self.settings, self.state)

    def test_daily_fixture_rows_sorted_missing_tables_and_ship_fallback(self):
        self.execute("UPDATE cusdec SET BANAME = ? WHERE SERSNO = ?", ("TEST FIXTURE SHIP", FIRST))
        self.execute("UPDATE n5204 SET `接收日期時間` = ? WHERE `報單號碼` = ?", ("2026/10/22 12:00:00", FIRST))
        before = self.settings.path.read_bytes()
        result = today_replies(self.settings, date(2026, 10, 22))
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assertEqual(len(result.replies), 3)
        self.assertEqual(result.replies[0].sersno, FIRST)
        self.assertEqual(result.replies[0].ship_name, "TEST FIXTURE SHIP")
        self.assertIn("放行件數 105 TNE", result.replies[0].content)
        notice, = (row for row in result.replies if row.code == "N5107")
        self.assertIn("應補辦 G01", notice.content)
        self.assertIn("補辦期限 2026-10-23", notice.content)
        self.assertEqual(len(result.notes), 4)
        self.assertTrue(all("已略過" in note for note in result.notes))

    def test_daily_other_messages_use_documented_fields_and_child_rows(self):
        for code, columns, values in (
            ("N5109", "`派驗日期時間`,`查驗區`,`驗貨關員`", ("2026-01-09 10:30:59", "X", "Y")),
            ("NX5106", "`回應代碼`,`備註`", ("TEST01", "TEST &lt;內容&gt; &amp; 原因")),
            ("NX903", "`原訊息類別`,`原訊息編號`", ("TEST03", "TEST0001")),
            ("N5110", "`稅費合計`,`繳款截止日`", ("100", "2026-11-30")),
        ):
            self.execute(f"CREATE TABLE {code} (MainID INTEGER, `接收日期時間` TEXT, {columns})")
            markers = ", ".join("?" for _ in values)
            self.execute(f"INSERT INTO {code} VALUES (1, ?, {markers})", (self.today.isoformat(), *values))
        self.execute("CREATE TABLE NX903_1 (MainID INTEGER, `錯誤說明` TEXT)")
        self.execute("INSERT INTO NX903_1 VALUES (1, 'TEST ERROR'), (2, 'OTHER')")
        self.execute("INSERT INTO N5109 VALUES (2, '2000-01-01', '', '', '')")
        result = today_replies(self.settings)
        rows = {entry.code: entry.content for entry in result.replies}
        self.assertEqual(rows["N5109"], "查驗；派驗 2026-01-09 10:30；查驗區 X；關員 Y")
        self.assertEqual(rows["NX5106"], "回應代碼 TEST01；備註 TEST <內容> & 原因")
        self.assertEqual(rows["NX903"], "原訊息類別 TEST03；原訊息編號 TEST0001；錯誤說明 TEST ERROR")
        self.assertEqual(rows["N5110"], "稅費合計 100；繳款截止日 2026-11-30")

    def test_unreadable_table_does_not_hide_other_replies(self):
        self.execute("CREATE TABLE NX903 (MainID INTEGER)")
        result = today_replies(self.settings, date(2026, 10, 22))
        self.assertIn("無法讀取 NX903，已略過。", result.notes)
        self.assertTrue(any(entry.code == "N5204" for entry in result.replies))

    def test_first_run_saves_positions_without_existing_today_notifications(self):
        self.notice(10000, FIRST, "C3M", "A01")
        self.release(22000, FIRST, "C3M")
        result = self.watch()
        self.assertEqual((result.logs, result.notifications), ((), ()))
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8")), {"N5107": 10000, "N5204": 22000})
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_poll_classifies_c3_c1a18_and_ignores_c2_for_notifications(self):
        self.watch()
        self.notice(10000, FIRST, "C3M", "A01")
        self.notice(10001, SECOND, "C1", "A18")
        self.release(22000, FIRST, "C2")
        result = self.watch()
        self.assertEqual([entry.kind for entry in result.notifications], ["C3", "C1A18"])
        self.assertEqual(result.logs, (
            f"{FIRST}：通關方式 C3M（應補辦 A01）", f"{SECOND}：通關方式 C1（應補辦 A18）",
            f"{FIRST}：放行（C3M 改 C2） 08:30:00",
        ))
        self.assertTrue(result.notifications[0].missing)
        self.assertEqual(self.watch().logs, ())

    def test_release_c1_and_old_rows_do_not_notify(self):
        self.watch()
        self.notice(10000, FIRST, "C1", "A18", day=self.today - timedelta(days=1))
        self.release(22000, FIRST, "C1")
        result = self.watch()
        self.assertEqual(result.logs, (f"{FIRST}：放行（C1） 08:30:00",))
        self.assertFalse(result.notifications)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["N5107"], 10000)

    def test_notification_switches_are_independent(self):
        self.watch()
        config = read_config(self.config)
        config["notify"]["c3"] = "no"
        self.settings = Settings(config, self.folder)
        self.notice(10000, FIRST, "C3M", "A01")
        self.notice(10001, SECOND, "C1", "A18")
        self.assertEqual([entry.kind for entry in self.watch().notifications], ["C1A18"])

    def test_c3_release_logs_but_only_c3_notice_notifies(self):
        self.watch()
        self.notice(10000, FIRST, "C3", "A01")
        self.notice(10001, SECOND, "C1", "XA18")
        self.release(22000, FIRST, "C3")
        result = self.watch()
        self.assertEqual([entry.reply.sersno for entry in result.notifications], [FIRST])
        self.assertEqual([line for line in result.logs if line.startswith(FIRST)], [
            f"{FIRST}：通關方式 C3（應補辦 A01）", f"{FIRST}：放行（C3） 08:30:00",
        ])
        self.notice(10002, SECOND, "C1", " a18 ")
        self.assertEqual([entry.kind for entry in self.watch().notifications], ["C1A18"])

    def test_missing_watch_table_keeps_its_existing_position(self):
        self.watch()
        self.execute("DROP TABLE n5107")
        self.release(22000, FIRST, "C3")
        result = self.watch()
        self.assertIn("測試庫缺少 N5107，已略過。", result.notes)
        self.assertEqual(result.logs, (f"{FIRST}：放行（C3） 08:30:00",))
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8")), {"N5107": 9847, "N5204": 22000})

    def test_corrupt_state_is_not_silently_replaced(self):
        self.state.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ReplyError, "監看位置"):
            self.watch()
        self.assertEqual(self.state.read_text(encoding="utf-8"), "{")

    def test_pdf_patterns_expand_placeholders_glob_and_report_missing(self):
        fields = dict(SERSNO=FIRST, BANAME="TEST [SHIP]", BAID="TEST1", BYNAME="TEST BUYER")
        first = self.folder / f"{FIRST}-TEST [SHIP]-TEST1-TEST BUYER-a.pdf"
        second = self.folder / f"{FIRST}-TEST [SHIP]-TEST1-TEST BUYER-b.PDF"
        for path in (first, second, first.with_suffix(".txt")):
            path.touch()
        found, missing = expand_pdf_paths(
            ("{SERSNO}-{BANAME}-{BAID}-{BYNAME}-*", str(first.parent / "{SERSNO}-*"),
             "不存在-{SERSNO}.pdf", "{BYNAME}/不存在.pdf"), self.folder, fields,
        )
        self.assertEqual(found, (first.resolve(), second.resolve()))
        self.assertEqual(missing, (f"不存在-{FIRST}.pdf", "TEST BUYER/不存在.pdf"))

    def test_attachment_command_receives_the_path_as_one_argument(self):
        path = self.folder / "TEST FILE.pdf"
        path.touch()
        with patch("fuelcore.reply_view.subprocess.run") as run:
            print_attachment(path, '"Test PDF.exe" /print "{file}"')
        self.assertEqual(run.call_args.args[0], ["Test PDF.exe", "/print", str(path)])
        with self.assertRaisesRegex(ReplyError, "找不到檔案"):
            print_attachment(self.folder / "missing.pdf", '"Test PDF.exe" "{file}"')

    def test_workbench_prompts_before_print_and_uses_duplex_plan(self):
        import tkinter as tk

        from fuelcore.gui import Workbench

        self.release(22000, FIRST, "C3")
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
        app = Workbench(root, self.config)
        page = app.reply_page
        root.after_cancel(page.timer)
        page.timer = None
        app.refresh = lambda: None
        app.show("送收信")
        self.assertEqual([app.edi_tabs.tab(tab, "text") for tab in app.edi_tabs.tabs()],
                         ["送出前確認", "今日回覆"])
        self.assertEqual(app.edi_tabs.select(), str(app.send_tab))
        app.send_numbers.insert(0, FIRST)
        app.edi_tabs.select(app.reply_tab)

        def wait():
            deadline = time.monotonic() + 8
            while (app.busy or page.watch_running or app.cleanup_running) and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertFalse(app.busy)
            self.assertFalse(page.watch_running)
            self.assertFalse(app.cleanup_running)

        deadline = time.monotonic() + 8
        while not page.loaded and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        wait()
        self.assertIn("1 則回覆，放行 1 張", page.status.get())
        app.edi_tabs.select(app.send_tab)
        self.assertEqual(app.send_numbers.get(), FIRST)
        app.edi_tabs.select(app.reply_tab)
        row, = page.table.get_children()
        self.assertEqual(page.table.item(row, "tags"), ("成功",))
        page.table.selection_set(row)
        with patch("fuelcore.reply_view.messagebox.askokcancel", return_value=False), \
                patch("fuelcore.reply_view.print_batch") as send:
            page.confirm_print_row()
            send.assert_not_called()
        config = read_config(self.config)
        config["print"]["printer"] = "TEST PRINTER"
        with self.config.open("w", encoding="utf-8") as stream:
            config.write(stream)
        fake_plan = SimpleNamespace(duplex=True)
        with patch("fuelcore.reply_view.messagebox.askokcancel", return_value=True), \
                patch("fuelcore.reply_view.plan_print", return_value=fake_plan), \
                patch("fuelcore.reply_view.prepare_report", return_value="TEST BATCH") as prepare, \
                patch("fuelcore.reply_view.print_batch") as send:
            page.confirm_print_row()
            wait()
        self.assertEqual(prepare.call_args.args[1], (FIRST,))
        self.assertEqual(send.call_args.args, ("TEST BATCH", fake_plan))
        self.assertTrue(any("已送出雙面列印" in page.logs.set(row, "text") for row in page.logs.get_children()))

        simplex = SimpleNamespace(duplex=False)
        with patch("fuelcore.reply_view.messagebox.askyesno", side_effect=[False, True]) as ask, \
                patch("fuelcore.reply_view.plan_print", return_value=simplex), \
                patch("fuelcore.reply_view.prepare_report", return_value="TEST BATCH"), \
                patch("fuelcore.reply_view.print_batch") as send:
            page.start_print((FIRST,))
            wait()
            send.assert_not_called()
            page.start_print((FIRST,))
            wait()
            send.assert_called_once()
        self.assertIn("要單面列印嗎？", ask.call_args.args[1])

        self.watch()
        self.notice(10000, FIRST, "C3M", "A01")
        with patch.object(page, "start_print") as start_print:
            page.watch()
            wait()
            start_print.assert_not_called()
            window, = (child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
            self.assertEqual(window.title(), "海關回覆通知")
            body, actions = window.winfo_children()
            text, = (child for child in body.winfo_children() if isinstance(child, tk.Text))
            self.assertIn(FIRST, text.get("1.0", "end"))
            self.assertIn("找不到檔案：", text.get("1.0", "end"))
            print_button, acknowledge = actions.winfo_children()
            print_button.invoke()
            self.assertEqual(start_print.call_args.args, ((FIRST,), (), window))
            acknowledge.invoke()

        root.after_cancel(page.timer)
        entered, release = Event(), Event()
        foreground = []

        def failing_read(_):
            entered.set()
            release.wait(5)
            raise ReplyError("無法開啟 EDI 測試庫。")

        conversion_log = app.logs.get_children()
        reply_status = page.status.get()
        with patch("fuelcore.reply_view.today_replies", side_effect=failing_read), \
                patch("fuelcore.reply_view.messagebox.showerror") as show_error:
            page.watch()
            self.assertTrue(entered.wait(2))
            self.assertFalse(app.busy)
            app.run_task("TEST FOREGROUND", lambda: "done", foreground.append)
            try:
                deadline = time.monotonic() + 3
                while not foreground and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertEqual(foreground, ["done"])
                self.assertTrue(page.watch_running)
                app.task_status.set("TEST KEEP")
            finally:
                release.set()
            wait()
            show_error.assert_not_called()
        self.assertEqual(app.logs.get_children(), conversion_log)
        self.assertEqual(app.task_status.get(), "TEST KEEP")
        self.assertEqual(page.status.get(), reply_status)
        self.assertTrue(any("無法開啟 EDI 測試庫" in page.logs.set(row, "text") for row in page.logs.get_children()))

        with patch("fuelcore.gui.daily_cleanup", side_effect=OSError("TEST CLEANUP")), \
                patch("fuelcore.gui.record") as run_log:
            app.cleanup_outputs()
            self.assertFalse(app.busy)
            wait()
        run_log.assert_called_once_with("cleanup", "failed")
        self.assertEqual(app.logs.get_children(), conversion_log)
        self.assertEqual(app.task_status.get(), "TEST KEEP")
        self.assertTrue(any("無法清理過期輸出" in page.logs.set(row, "text") for row in page.logs.get_children()))

        config.remove_section("edi")
        config.remove_section("notify")
        with self.config.open("w", encoding="utf-8") as stream:
            config.write(stream)
        self.assertTrue(Settings.load(self.config).notify.enabled)
        with patch("fuelcore.reply_view.today_replies") as read, patch("fuelcore.reply_view.messagebox.showerror") as error:
            page.restart_watch()
            self.assertIsNone(page.interval)
            page.refresh()
            wait()
        read.assert_not_called()
        error.assert_not_called()
        self.assertEqual(sum(page.logs.set(row, "text") == NO_EDI for row in page.logs.get_children()), 1)
