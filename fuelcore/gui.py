import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

from .convert import convert_file
from .logic_check import run_logic_check
from .report_view import ReportPage
from .settings import Settings
from .workbench import (
    DB_FIELDS, DEFAULTS, FAILED, IMPORTED, PATH_FIELDS, PENDING, UNREADABLE,
    config_source, edited_config, load_fields, prepare_database, resolve_folder,
    save_fields, scan_inputs, test_database,
)


BACKGROUND = "#f3f5f8"
WHITE = "#ffffff"
FRAME = "#dfe4ea"
TEXT = "#1f2937"
MUTED = "#6b7280"
NAV = "#0f2a4a"
BLUE = "#0b5cad"
SELECTED = "#d6e4f5"
SUCCESS = "#067647"
WARNING = "#b54708"
ERROR = "#b42318"
FONT = ("Microsoft JhengHei UI", 12)
BOLD = (*FONT, "bold")
TITLE = (FONT[0], 17, "bold")


def configure_styles(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    for widget in ("Text", "TEntry", "TCombobox", "TCombobox*Listbox"):
        root.option_add(f"*{widget}.font", FONT)
    style.configure(".", font=FONT, foreground=TEXT, background=BACKGROUND)
    style.configure("Card.TFrame", background=WHITE, bordercolor=FRAME, borderwidth=1, relief="solid")
    style.configure("Card.TLabel", background=WHITE)
    style.configure("Hint.TLabel", background=WHITE, foreground=MUTED)
    style.configure("Error.TLabel", background=WHITE, foreground=ERROR)
    style.configure("Good.TLabel", background=WHITE, foreground=SUCCESS)
    style.configure("Title.TLabel", font=TITLE)
    style.configure("CardTitle.TLabel", font=BOLD, background=WHITE)
    style.configure("Nav.TFrame", background=NAV)
    style.configure("Accent.TFrame", background="#4ea1ff")
    style.configure("Brand.TLabel", background=NAV, foreground=WHITE, font=TITLE)
    style.configure("Nav.TButton", background=NAV, foreground="#c7d2e0", anchor="w",
                    bordercolor=NAV, lightcolor=NAV, darkcolor=NAV, padding=(18, 10))
    style.map("Nav.TButton", background=[("active", "#17395e")])
    style.configure("Current.Nav.TButton", foreground=WHITE, font=BOLD,
                    background="#17395e", bordercolor="#17395e")
    style.configure("TButton", background=WHITE, bordercolor="#cfd6df", lightcolor=WHITE,
                    darkcolor=WHITE, padding=(12, 7))
    style.map("TButton", background=[("disabled", BACKGROUND), ("active", "#eef2f7")],
              foreground=[("disabled", MUTED)])
    style.configure("Primary.TButton", background=BLUE, foreground=WHITE, bordercolor=BLUE,
                    lightcolor=BLUE, darkcolor=BLUE, font=BOLD)
    style.map("Primary.TButton", background=[("disabled", "#e2e8f0"), ("active", "#094b8e")],
              foreground=[("disabled", MUTED), ("!disabled", WHITE)])
    style.configure("TEntry", padding=6, fieldbackground=WHITE, bordercolor="#cfd6df")
    style.configure("TCombobox", padding=5, fieldbackground=WHITE, bordercolor="#cfd6df")
    row_height = max(34, font.Font(root=root, font=FONT).metrics("linespace") + 12)
    style.configure("Treeview", background=WHITE, fieldbackground=WHITE, bordercolor=FRAME, rowheight=row_height)
    style.configure("Treeview.Heading", background="#f1f4f8", font=BOLD, padding=(8, 7),
                    bordercolor=FRAME, lightcolor="#f1f4f8", darkcolor="#f1f4f8")
    style.map("Treeview", background=[("selected", SELECTED)], foreground=[("selected", TEXT)])


def make_table(parent, columns, selectmode="browse"):
    panel = ttk.Frame(parent, style="Card.TFrame")
    panel.columnconfigure(0, weight=1)
    panel.rowconfigure(0, weight=1)
    tree = ttk.Treeview(panel, columns=[column[0] for column in columns], show="headings",
                        selectmode=selectmode, height=5)
    for key, heading, width, stretch in columns:
        tree.heading(key, text=heading, anchor="w")
        tree.column(key, width=width, minwidth=width, stretch=stretch)
    yscroll = ttk.Scrollbar(panel, orient="vertical", command=tree.yview)
    xscroll = ttk.Scrollbar(panel, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    yscroll.grid(row=0, column=1, sticky="ns")
    xscroll.grid(row=1, column=0, sticky="ew")
    for tag, color in ((IMPORTED, SUCCESS), (FAILED, ERROR), (UNREADABLE, ERROR), ("成功", SUCCESS),
                       ("提醒", WARNING), ("失敗", ERROR)):
        tree.tag_configure(tag, foreground=color)
    return panel, tree


class Workbench:
    def __init__(self, root, config_path):
        self.root = root
        self.config_path = config_path.resolve()
        self.events = queue.Queue()
        self.busy = False
        self.extra = set()
        self.failures = {}
        self.checks = {}
        self.rows = {}
        self.nav = {}
        self.pages = {}
        self.setting_actions = []
        self.values = {key: tk.StringVar(value=DEFAULTS.get(key, "")) for key in (*PATH_FIELDS, *DB_FIELDS)}
        self.source_text = tk.StringVar(value="尚未指定 TXT 來源目錄")
        self.summary = tk.StringVar()
        self.show_all = tk.BooleanVar(value=False)
        self.cbs_status = tk.StringVar()
        self.feedback = tk.StringVar()
        self.task_status = tk.StringVar(value="就緒")
        self.log_detail = tk.StringVar()

        configure_styles(root)
        root.title("fuelcore | 轉檔工作台")
        root.configure(background=BACKGROUND)
        root.minsize(900, 640)
        if root.tk.call("tk", "windowingsystem") == "win32":
            root.state("zoomed")
        else:
            root.geometry(f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(root, style="Nav.TFrame", width=176)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="fuelcore", style="Brand.TLabel").pack(anchor="w", padx=21, pady=(26, 25))
        body = ttk.Frame(root, padding=(24, 18))
        body.grid(row=0, column=1, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        for name, build in (("轉檔", self.build_convert_page), ("報單列印", self.build_report_page),
                            ("設定", self.build_settings_page)):
            item = ttk.Frame(sidebar, style="Nav.TFrame")
            item.pack(fill="x", pady=2)
            indicator = ttk.Frame(item, style="Nav.TFrame", width=4)
            indicator.pack(side="left", fill="y")
            button = ttk.Button(item, text=name, style="Nav.TButton", command=lambda key=name: self.show(key))
            button.pack(side="left", fill="x", expand=True)
            page = ttk.Frame(body)
            page.grid(row=0, column=0, sticky="nsew")
            self.nav[name] = indicator, button
            self.pages[name] = page
            build(page)

        self.show("轉檔")
        try:
            for key, value in load_fields(self.config_path).items():
                self.values[key].set(value)
        except (OSError, ValueError) as error:
            self.add_log("失敗", str(error))
        self.update_driver()
        self.update_cbs_status()
        self.root.after(60, self.poll)
        self.root.after_idle(self.refresh)

    def show(self, name):
        self.pages[name].tkraise()
        for key, (indicator, button) in self.nav.items():
            indicator.configure(style="Accent.TFrame" if key == name else "Nav.TFrame")
            button.configure(style="Current.Nav.TButton" if key == name else "Nav.TButton")

    def build_report_page(self, page):
        self.report_page = ReportPage(self, page)

    def build_convert_page(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=5)
        page.rowconfigure(2, weight=1)
        ttk.Label(page, text="轉檔", style="Title.TLabel").grid(sticky="w", pady=(0, 12))
        source = ttk.Frame(page, style="Card.TFrame", padding=14)
        source.grid(row=1, column=0, sticky="nsew", pady=(0, 12))
        source.columnconfigure(0, weight=1)
        source.rowconfigure(3, weight=1)
        ttk.Label(source, text="TXT 來源目錄", style="CardTitle.TLabel").grid(sticky="w")
        ttk.Label(source, textvariable=self.source_text, style="Hint.TLabel").grid(sticky="ew", pady=(2, 9))
        bar = ttk.Frame(source, style="Card.TFrame", borderwidth=0)
        bar.grid(sticky="ew", pady=(0, 9))
        self.start_button = ttk.Button(bar, text="開始轉檔", style="Primary.TButton", command=self.convert)
        self.start_button.pack(side="left", padx=(0, 8))
        self.add_button = ttk.Button(bar, text="加入其他檔案…", command=self.add_files)
        self.add_button.pack(side="left", padx=(0, 8))
        self.refresh_button = ttk.Button(bar, text="重新整理", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.check_button = ttk.Button(bar, text="邏輯檢查", command=self.check_selected, state="disabled")
        self.check_button.pack(side="left", padx=(8, 0))
        self.show_all_button = ttk.Checkbutton(bar, text="顯示全部", variable=self.show_all,
                                               command=self.refresh)
        self.show_all_button.pack(side="left", padx=(12, 0))
        ttk.Label(bar, textvariable=self.summary, style="Hint.TLabel").pack(side="right", padx=8)
        panel, self.files = make_table(source, (
            ("name", "檔名", 240, True), ("folder", "日期資料夾", 116, False),
            ("sersno", "報單號碼", 165, False), ("status", "狀態", 103, False),
            ("logic", "邏輯檢查", 125, False),
        ), "extended")
        panel.grid(sticky="nsew")
        self.files.configure(height=7)
        self.files.bind("<<TreeviewSelect>>", lambda _: self.update_buttons())
        self.files.bind("<Double-1>", self.show_check_detail)

        log = ttk.Frame(page, style="Card.TFrame", padding=14)
        log.grid(row=2, column=0, sticky="nsew")
        log.columnconfigure(0, weight=1)
        log.rowconfigure(1, weight=1)
        ttk.Label(log, text="作業紀錄", style="CardTitle.TLabel").grid(sticky="w", pady=(0, 8))
        panel, self.logs = make_table(log, (
            ("time", "時間", 95, False), ("kind", "類別", 80, False), ("message", "內容", 420, True),
        ))
        panel.grid(sticky="nsew")
        self.logs.configure(height=2)
        details = ttk.Label(log, textvariable=self.log_detail, style="Hint.TLabel")
        details.grid(sticky="ew", pady=(4, 0))
        log.bind("<Configure>", lambda event: details.configure(wraplength=max(event.width - 32, 100)))
        self.logs.bind("<<TreeviewSelect>>", self.show_log_detail)
        ttk.Label(page, textvariable=self.task_status).grid(sticky="w", pady=(7, 0))

    def build_settings_page(self, page):
        page.columnconfigure(0, weight=1)
        ttk.Label(page, text="設定", style="Title.TLabel").grid(sticky="w", pady=(0, 12))
        paths = ttk.Frame(page, style="Card.TFrame", padding=14)
        paths.grid(sticky="ew", pady=(0, 12))
        paths.columnconfigure(1, weight=1)
        ttk.Label(paths, text="路徑設定", style="CardTitle.TLabel").grid(columnspan=3, sticky="w", pady=(0, 7))
        for row, field, label in ((1, "txt_path", "TXT 來源目錄"), (2, "cbsapp_path", "CBSAPP 目錄")):
            self.setting_entry(paths, row, field, label)
            button = ttk.Button(paths, text="選資料夾…", command=lambda key=field: self.choose_folder(key))
            button.grid(row=row, column=2, sticky="e", padx=(9, 0))
            self.setting_actions.append(button)
        self.cbs_label = ttk.Label(paths, textvariable=self.cbs_status, style="Hint.TLabel", wraplength=700)
        self.cbs_label.grid(row=3, column=1, columnspan=2, sticky="w", pady=(2, 0))
        self.values["cbsapp_path"].trace_add("write", lambda *_: self.update_cbs_status())

        db = ttk.Frame(page, style="Card.TFrame", padding=14)
        db.grid(sticky="ew")
        db.columnconfigure(1, weight=1)
        db.columnconfigure(3, weight=1)
        ttk.Label(db, text="測試資料庫", style="CardTitle.TLabel").grid(columnspan=4, sticky="w", pady=(0, 7))
        ttk.Label(db, text="類型", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 12))
        driver = ttk.Combobox(db, textvariable=self.values["driver"], values=("sqlite", "mysql"), state="readonly")
        driver.grid(row=1, column=1, sticky="ew", pady=3)
        driver.bind("<<ComboboxSelected>>", lambda _: self.update_driver())
        self.mysql_entries = []
        for row, column, field, label in (
            (2, 0, "host", "主機"), (2, 2, "port", "埠"), (3, 0, "user", "帳號"),
            (3, 2, "password", "密碼"), (4, 0, "database", "報單資料庫"),
            (4, 2, "code_database", "代碼資料庫"),
        ):
            self.mysql_entries.append(self.setting_entry(db, row, field, label, column))
        ttk.Label(db, text="MySQL 報單與代碼資料庫名稱需符合 test 命名規則。",
                  style="Hint.TLabel").grid(row=5, columnspan=4, sticky="w", pady=(5, 0))
        actions = ttk.Frame(page)
        actions.grid(sticky="ew", pady=(12, 6))
        for title, action, style in (("儲存設定", self.save, "Primary.TButton"),
                                     ("測試連線", self.test_connection, "TButton")):
            button = ttk.Button(actions, text=title, command=action, style=style)
            button.pack(side="left", padx=(0, 8))
            self.setting_actions.append(button)
        ttk.Label(actions, textvariable=self.task_status).pack(side="right")
        self.feedback_label = ttk.Label(page, textvariable=self.feedback, wraplength=780)
        self.feedback_label.grid(sticky="w")

    def setting_entry(self, parent, row, field, label, column=0):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=column, sticky="w", padx=(14 if column else 0, 12))
        entry = ttk.Entry(parent, textvariable=self.values[field], show="●" if field == "password" else "")
        entry.grid(row=row, column=column + 1, sticky="ew", pady=3)
        return entry

    def update_driver(self):
        state = "!disabled" if self.values["driver"].get() == "mysql" else "disabled"
        for entry in self.mysql_entries:
            entry.state([state])

    def update_cbs_status(self):
        try:
            path = resolve_folder(self.config_path.parent, "cbsapp_path", self.values["cbsapp_path"].get())
            self.cbs_status.set(f"已找到 {path / 'CBSAPP.exe'}" if path else "尚未指定 CBSAPP 目錄")
            self.cbs_label.configure(style="Good.TLabel" if path else "Hint.TLabel")
        except (OSError, ValueError) as error:
            self.cbs_status.set(str(error))
            self.cbs_label.configure(style="Error.TLabel")

    def choose_folder(self, field):
        selected = filedialog.askdirectory(parent=self.root, title="選擇資料夾", mustexist=True)
        if selected:
            self.values[field].set(selected)

    def add_files(self):
        selected = filedialog.askopenfilenames(parent=self.root, title="選擇 TXT 檔案",
                                               filetypes=[("TXT 檔案", "*.txt"), ("所有檔案", "*.*")])
        self.extra.update(Path(path) for path in selected if Path(path).suffix.lower() == ".txt")
        if selected:
            self.refresh()

    def refresh(self):
        extra, failures = tuple(self.extra), self.failures.copy()
        show_all = self.show_all.get()
        self.run_task("正在掃描 TXT…", lambda: scan_inputs(self.config_path, extra, failures,
                                                          show_all=show_all), self.show_files)

    def show_files(self, scan):
        folder, files, warnings = scan
        self.busy = True
        self.task_status.set("正在更新檔案清單…")
        self.source_text.set(str(folder) if folder else "尚未指定有效的 TXT 來源目錄，可在設定指定或加入檔案。")
        self.summary.set(f"找到 {len(files)} 個檔（其中未轉入 {sum(file.status == PENDING for file in files)} 個）")
        self.files.delete(*self.files.get_children())
        self.rows.clear()
        pending = []

        def show_batch(start):
            end = start + 100
            for file in files[start:end]:
                row = self.files.insert("", "end", values=(file.path.name, file.folder, file.sersno, file.status,
                                                           self.check_label(file)),
                                        tags=(file.status,))
                self.rows[row] = file
                if file.status == PENDING:
                    pending.append(row)
                elif file.status == UNREADABLE:
                    self.add_log("失敗", f"無法讀取 {file.path}：{file.error}")
            if end < len(files):
                self.root.after(1, show_batch, end)
                return
            self.files.selection_set(pending)
            for warning in warnings:
                self.add_log("提醒", warning)
            self.busy = False
            self.task_status.set("就緒")
            self.update_buttons()

        self.root.after(1, show_batch, 0)

    def convert(self):
        selected = set(self.files.selection())
        paths = [file.path for row, file in self.rows.items()
                 if row in selected and file.status not in (IMPORTED, UNREADABLE)]
        if not paths:
            return
        extra, failures = tuple(self.extra), self.failures.copy()
        show_all = self.show_all.get()

        def work():
            settings = Settings.load(config_source(self.config_path))
            prepare_database(settings)
            for path in paths:
                result = convert_file(path, settings)
                if result.ok:
                    failures.pop(path, None)
                else:
                    failures[path] = result.message or "轉檔失敗"
                self.events.put(("conversion", (path, result)))
            return scan_inputs(self.config_path, extra, failures, show_all=show_all), failures

        def finish(value):
            scan, self.failures = value
            self.show_files(scan)

        self.run_task("正在轉檔…", work, finish)

    def show_result(self, result_event):
        path, result = result_event
        if result.ok:
            self.checks.pop(result.sersno, None)
        label = f"{path.name}（{result.sersno}）" if result.sersno else path.name
        for note in reversed(result.notes):
            self.add_log("提醒", note)
        message = result.message or ("已轉入" if result.ok else "轉檔失敗")
        self.add_log("成功" if result.ok else "失敗", f"{label}：{message}")

    def check_label(self, file):
        result = self.checks.get(file.sersno)
        return (result.label if result else "未檢查") if file.status == IMPORTED else ""

    def check_selected(self):
        ids = list(dict.fromkeys(self.rows[row].sersno for row in self.files.selection()
                                 if row in self.rows and self.rows[row].status == IMPORTED))
        if not ids:
            return

        def work():
            try:
                settings = Settings.load(config_source(self.config_path))
            except (ValueError, OSError, KeyError):
                raise ValueError("無法讀取 fuelcore 測試庫設定。請核對 test_only 與資料庫名稱。") from None
            return run_logic_check(ids, settings)

        def finish(run):
            for result in run.results:
                self.checks[result.sersno] = result
                self.add_log("成功" if result.passed else "提醒", f"{result.sersno} {result.label}")
            for row, file in self.rows.items():
                if file.sersno in ids:
                    self.files.set(row, "logic", self.check_label(file))

        self.run_task("正在執行邏輯檢查…", work, finish,
                      lambda message: messagebox.showerror("邏輯檢查", message, parent=self.root))

    def show_check_detail(self, event):
        row = self.files.identify_row(event.y)
        file = self.rows.get(row)
        result = self.checks.get(file.sersno) if file and file.status == IMPORTED else None
        if result is None:
            return
        window = tk.Toplevel(self.root)
        window.title(f"{result.sersno} · {result.label}")
        window.transient(self.root)
        body = tk.Text(window, width=72, height=18, wrap="word")
        scrollbar = ttk.Scrollbar(window, orient="vertical", command=body.yview)
        body.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        body.pack(side="left", fill="both", expand=True)
        body.insert("end", f"{result.label}　執行 {result.rules_total} 條，無法檢查 {result.rules_unparsed} 條\n")
        for message in result.messages:
            item = f"第 {message.item} 項 " if message.item else ""
            body.insert("end", f"\n[{message.level}] {item}{message.rule_id} {message.field}\n{message.text}\n")
        body.configure(state="disabled")

    def current_fields(self):
        return {key: value.get() for key, value in self.values.items()}

    def show_feedback(self, text, error=False):
        self.feedback.set(text)
        self.feedback_label.configure(foreground=ERROR if error else SUCCESS)

    def save(self):
        values = self.current_fields()
        extra = tuple(self.extra)
        show_all = self.show_all.get()

        def work():
            save_fields(self.config_path, values)
            return scan_inputs(self.config_path, extra, show_all=show_all)

        def finish(scan):
            self.failures.clear()
            self.checks.clear()
            self.show_feedback(f"已儲存 {self.config_path}")
            self.show_files(scan)

        self.run_task("正在儲存設定並掃描 TXT…", work, finish)

    def test_connection(self):
        values = self.current_fields()

        def work():
            _, settings = edited_config(self.config_path, values, check_paths=False)
            return test_database(settings)

        self.run_task("正在測試連線…", work, self.show_feedback)

    def add_log(self, kind, message):
        self.logs.insert("", 0, values=(datetime.now().strftime("%H:%M:%S"), kind, message), tags=(kind,))

    def show_log_detail(self, _):
        selection = self.logs.selection()
        self.log_detail.set(self.logs.set(selection[0], "message") if selection else "")

    def update_buttons(self):
        can_start = not self.busy and any(self.rows[row].status not in (IMPORTED, UNREADABLE)
                                          for row in self.files.selection() if row in self.rows)
        self.start_button.state(["!disabled" if can_start else "disabled"])
        can_check = not self.busy and any(self.rows[row].status == IMPORTED
                                          for row in self.files.selection() if row in self.rows)
        self.check_button.state(["!disabled" if can_check else "disabled"])
        for button in [self.add_button, self.refresh_button, self.show_all_button, *self.setting_actions]:
            button.state(["disabled" if self.busy else "!disabled"])

    def run_task(self, title, work, finish, on_error=None):
        if self.busy:
            return
        self.busy = True
        self.task_status.set(title)
        self.update_buttons()

        def worker():
            try:
                result = work()
            except Exception as error:
                self.events.put(("error", (str(error), on_error)))
            else:
                self.events.put(("done", (finish, result)))

        threading.Thread(target=worker).start()

    def poll(self):
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "conversion":
                self.show_result(value)
                continue
            self.busy = False
            self.task_status.set("就緒")
            if kind == "error":
                message, on_error = value
                self.add_log("失敗", message)
                self.show_feedback(message, error=True)
                self.task_status.set("作業失敗，請查看紀錄或設定頁。")
                if on_error is not None:
                    on_error(message)
            else:
                finish, result = value
                finish(result)
            self.update_buttons()
        self.root.after(60, self.poll)

    def close(self):
        if self.busy:
            messagebox.showinfo("fuelcore", "作業進行中，請等候完成後再關閉。", parent=self.root)
        else:
            self.report_page.confirm_leave(lambda: self.root.after_idle(self.root.destroy))


def run(config_path):
    root = tk.Tk()
    Workbench(root, config_path)
    root.mainloop()
    return 0
