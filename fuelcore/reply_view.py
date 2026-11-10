import os
import shlex
import subprocess
import tkinter as tk
from datetime import date, datetime, time, timedelta
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .replies import NAMES, ReplyError, ReplyList, poll, today_replies
from .report import prepare_report
from .report.printing import plan_print, print_batch
from .settings import Settings
from .runtime import record
from .workbench import config_source


NO_EDI = "未設定 [edi] 測試庫，海關回覆監看未啟動。"


def command_arguments(command):
    if os.name != "nt":
        return shlex.split(command)
    import ctypes
    from ctypes import wintypes

    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = (ctypes.c_void_p,)
    free.restype = ctypes.c_void_p
    count = ctypes.c_int()
    argv = parse(command, ctypes.byref(count))
    if not argv:
        raise ReplyError("無法解析 [notify] print_command。")
    try:
        return argv[:count.value]
    finally:
        free(argv)


def print_attachment(path, command):
    if not path.is_file():
        raise ReplyError(f"找不到檔案：{path}")
    if command:
        args = command_arguments(command)
        if not args or not any("{file}" in arg for arg in args[1:]):
            raise ReplyError("[notify] print_command 的參數須包含 {file}。")
        subprocess.run([arg.replace("{file}", str(path)) for arg in args], check=True, timeout=60,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    elif os.name == "nt":
        os.startfile(str(path), "print")
    else:
        raise ReplyError("PDF 預設列印需要 Windows，或請設定 [notify] print_command。")


class ReplyPage:
    def __init__(self, app, page, make_table):
        self.app, self.page = app, page
        self.root = app.root
        self.rows = {}
        self.note_messages = set()
        self.day = date.today()
        self.loaded = False
        self.timer = None
        self.interval = None
        self.watch_running = False
        self.watch_generation = 0
        self.state_path = app.config_path.with_suffix(".notify-state.json")

        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=3)
        page.rowconfigure(3, weight=1)
        header = ttk.Frame(page)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="今日海關回覆", style="Title.TLabel").pack(side="left")
        self.status = tk.StringVar(value="尚未更新")
        ttk.Label(header, textvariable=self.status).pack(side="left", padx=10)
        self.refresh_button = ttk.Button(header, text="重新整理", command=self.refresh)
        self.refresh_button.pack(side="right")
        table, self.table = make_table(page, (
            ("received", "接收時間", 145, False), ("sersno", "報單號碼", 148, False),
            ("ship", "船名", 110, True), ("name", "訊息", 135, False), ("content", "內容", 260, True),
        ))
        table.grid(row=1, column=0, sticky="nsew")
        self.table.bind("<Double-1>", self.print_row)
        self.table.bind("<Button-3>", self.context_menu)
        self.menu = tk.Menu(page, tearoff=False)
        self.menu.add_command(label="列印這張報單…", command=self.confirm_print_row)

        ttk.Label(page, text="作業紀錄", style="Title.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 6))
        logs, self.logs = make_table(page, (
            ("time", "時間", 95, False), ("text", "內容", 440, True),
        ))
        logs.grid(row=3, column=0, sticky="nsew")

    def settings(self):
        return Settings.load(config_source(self.app.config_path))

    def keep_today(self):
        today = date.today()
        if today == self.day:
            return
        self.day = today
        self.loaded = False
        self.rows.clear()
        self.note_messages.clear()
        self.table.delete(*self.table.get_children())
        self.logs.delete(*self.logs.get_children())
        self.status.set("尚未更新")

    def activate(self):
        self.keep_today()
        if not self.loaded:
            self.refresh()

    def log(self, text, *, note=False):
        self.keep_today()
        if note:
            if text in self.note_messages:
                return
            self.note_messages.add(text)
        self.logs.insert("", 0, values=(datetime.now().strftime("%H:%M:%S"), text))

    def failure(self, message):
        record("reply_watch", "failed")
        self.status.set("更新失敗，請查看作業紀錄")
        self.log(message, note=True)

    def show_results(self, results):
        self.keep_today()
        self.loaded = True
        self.rows.clear()
        self.table.delete(*self.table.get_children())
        releases = set()
        for reply in results.replies:
            if reply.received.date() != self.day:
                continue
            tag = {"NX903": "失敗", "NX5106": "失敗", "N5109": "提醒", "N5204": "成功"}.get(reply.code, "")
            row = self.table.insert("", "end", values=(reply.received.strftime("%H:%M:%S"), reply.sersno,
                                    reply.ship_name, NAMES[reply.code], reply.content), tags=(tag,))
            self.rows[row] = reply
            if reply.code == "N5204" and reply.sersno:
                releases.add(reply.sersno)
        self.status.set(f"{datetime.now():%H:%M:%S} 更新：{len(self.rows)} 則回覆，放行 {len(releases)} 張")
        for note in results.notes:
            self.log(note, note=True)

    def refresh(self):
        def work():
            settings = self.settings()
            return today_replies(settings) if settings.edi else ReplyList((), (NO_EDI,))

        self.app.run_task("正在讀取今日海關回覆…", work,
                          self.show_results, self.failure)

    def restart_watch(self):
        self.watch_generation += 1
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.interval = None
        try:
            settings = self.settings()
        except (OSError, ValueError, KeyError):
            settings = None
        if settings and settings.notify.enabled:
            if settings.edi:
                self.interval = settings.notify.poll_seconds * 1000
            else:
                self.log(NO_EDI, note=True)
        self.schedule(1000 if self.interval else None)

    def schedule(self, delay=None):
        now = datetime.now()
        midnight = datetime.combine(now.date() + timedelta(days=1), time.min)
        until_midnight = max(1, int((midnight - now).total_seconds() * 1000) + 1)
        delay = delay if delay is not None else self.interval
        self.timer = self.root.after(min(delay, until_midnight) if delay else until_midnight, self.watch)

    def watch(self):
        self.timer = None
        self.keep_today()
        if self.interval is None:
            self.schedule()
            return
        if self.watch_running:
            self.schedule(1000)
            return
        self.watch_running = True
        generation = self.watch_generation

        def work():
            settings = self.settings()
            if not settings.edi or not settings.notify.enabled:
                return None, None, ""
            results = today_replies(settings)
            try:
                monitored = poll(settings, self.state_path)
            except ReplyError as error:
                return results, None, str(error)
            return results, monitored, ""

        def finish(value):
            self.watch_running = False
            if generation != self.watch_generation:
                return
            results, monitored, warning = value
            if results is None:
                self.restart_watch()
                return
            self.schedule()
            self.show_results(results)
            if warning:
                record("reply_watch", "failed")
                self.log(warning, note=True)
            if monitored:
                for text in monitored.logs:
                    self.log(text)
                for note in monitored.notes:
                    self.log(note, note=True)
                if monitored.notes:
                    record("reply_watch", "read_warning", count=len(monitored.notes))
                if monitored.notifications:
                    self.show_notifications(monitored.notifications)

        def failed(message):
            self.watch_running = False
            if generation != self.watch_generation:
                return
            self.schedule()
            record("reply_watch", "failed")
            self.log(message, note=True)

        self.app.run_background(work, finish, failed)

    def row_at(self, event):
        if self.table.identify_region(event.x, event.y) != "cell":
            return False
        row = self.table.identify_row(event.y)
        if row not in self.rows:
            return False
        self.table.selection_set(row)
        return True

    def print_row(self, event):
        if self.row_at(event):
            self.confirm_print_row()

    def context_menu(self, event):
        if self.row_at(event):
            self.menu.tk_popup(event.x_root, event.y_root)
            self.menu.grab_release()

    def confirm_print_row(self):
        selected = self.table.selection()
        if self.app.busy or not selected:
            return
        reply = self.rows[selected[0]]
        if not reply.sersno:
            messagebox.showinfo("補印報單", "這筆回覆沒有報單號碼。", parent=self.page)
            return
        if messagebox.askokcancel("補印報單", f"列印 {reply.sersno}？", parent=self.page):
            self.start_print((reply.sersno,))

    def show_notifications(self, notifications):
        window = tk.Toplevel(self.root)
        window.title("海關回覆通知")
        window.attributes("-topmost", True)
        window.transient(self.root)
        body = ScrolledText(window, width=74, height=min(17, max(6, len(notifications) * 3)), wrap="word")
        body.pack(fill="both", expand=True, padx=12, pady=12)
        for entry in notifications:
            reply = entry.reply
            body.insert("end", f"{reply.sersno}　{reply.ship_name or '—'}　{reply.buyer or '—'}　{entry.kind}\n")
            for missing in entry.missing:
                body.insert("end", f"找不到檔案：{missing}\n")
        body.configure(state="disabled")
        actions = ttk.Frame(window, padding=8)
        actions.pack(fill="x")
        numbers = tuple(dict.fromkeys(entry.reply.sersno for entry in notifications))
        attachments = tuple(dict.fromkeys(path for entry in notifications for path in entry.pdfs))
        ttk.Button(actions, text="列印", command=lambda: self.start_print(numbers, attachments, window)).pack(side="left")
        ttk.Button(actions, text="知道了", command=window.destroy).pack(side="right")

    def start_print(self, numbers, attachments=(), window=None):
        if self.app.busy:
            return

        def prepare():
            settings = self.settings()
            if not settings.printer:
                raise ReplyError("請先設定 [print] printer，才能列印報單。")
            return settings, plan_print(settings)

        def work(settings, plan):
            logs = []
            for number in numbers:
                try:
                    batch = prepare_report(settings, (number,))
                    print_batch(batch, plan, notify=logs.append)
                except Exception as error:
                    logs.append(f"{number}：列印失敗（{error}）")
                else:
                    mode = "雙面" if plan.duplex else "單面"
                    logs.append(f"{number}：已送出{mode}列印。")
            for path in attachments:
                try:
                    print_attachment(path, settings.notify.print_command)
                except ReplyError as error:
                    record("print_attachment", "failed")
                    logs.append(str(error))
                except (OSError, ValueError, subprocess.SubprocessError):
                    record("print_attachment", "failed")
                    logs.append(f"PDF 列印失敗：{path}")
                else:
                    record("print_attachment", "submitted")
                    logs.append(f"PDF 已送出列印：{path}")
            return logs

        def finish(logs):
            for text in logs:
                self.log(text)
            if window is not None and window.winfo_exists():
                window.destroy()

        def failed(message):
            self.log(message)
            messagebox.showerror("列印報單", message, parent=self.page)

        def ready(prepared):
            settings, plan = prepared
            if not plan.duplex and not messagebox.askyesno(
                    "確認單面列印", "這台印表機未回報支援雙面，要單面列印嗎？", parent=window or self.page):
                return
            self.app.run_task("正在列印海關回覆報單…", lambda: work(settings, plan), finish, failed)

        self.app.run_task("正在讀取印表機設定…", prepare, ready, failed)
