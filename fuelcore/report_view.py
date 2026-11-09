"""Tk controls for previewing, editing and printing a read-only report."""

import re
import tkinter as tk
from contextlib import closing
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .report.calc import ItemInput, decimal_input
from .report.correction import export_correction
from .report.editing import EditableReport, report_ids
from .report.printing import plan_print, print_batch
from .report.rpx import RpxError
from .settings import Settings
from .workbench import config_source


class ReportPage:
    def __init__(self, workbench, page):
        self.workbench = workbench
        self.page = page
        self.output = workbench.config_path.parent / "列印輸出"
        self.report = None
        self.pdf = None
        self.image = None
        self.selection = tk.StringVar()
        self.number = tk.StringVar()
        self.status = tk.StringVar(value="請輸入報單號碼或開啟 Rpx 副本。")
        self.zoom = tk.BooleanVar()

        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)
        ttk.Label(page, text="報單列印", style="Title.TLabel").grid(sticky="w", pady=(0, 12))
        load = ttk.Frame(page, padding=10, style="Card.TFrame")
        load.grid(sticky="ew")
        load.columnconfigure(1, weight=1)
        ttk.Label(load, text="報單號碼", style="Card.TLabel").grid(row=0, column=0, padx=(0, 8))
        self.numbers = ttk.Combobox(load, textvariable=self.number)
        self.numbers.grid(row=0, column=1, sticky="ew")
        self.numbers.bind("<Return>", lambda _: self.load())
        ttk.Button(load, text="查詢清單", command=self.lookup).grid(row=0, column=2, padx=6)
        ttk.Button(load, text="從資料庫產生", style="Primary.TButton", command=self.load).grid(row=0, column=3)
        ttk.Button(load, text="開啟 Rpx…", command=self.open).grid(row=0, column=4, padx=(6, 0))
        actions = ttk.Frame(page)
        actions.grid(sticky="ew", pady=8)
        self.buttons = {}
        for index, (label, command) in enumerate((
            ("輸出 PDF", self.export), ("直接列印", self.print_report), ("試算", self.open_trial),
            ("更正申請書 Excel", self.export_excel), ("修改欄位", self.open_editor),
        )):
            button = ttk.Button(actions, text=label, command=command, state="disabled")
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=(0, 6), pady=2)
            self.buttons[label] = button
        pager = ttk.Frame(page)
        pager.grid(sticky="ew", pady=(0, 6))
        ttk.Label(pager, text="預覽頁面").pack(side="left", padx=(0, 8))
        self.pages = ttk.Combobox(pager, textvariable=self.selection, state="readonly", width=27)
        self.pages.pack(side="left")
        self.pages.bind("<<ComboboxSelected>>", lambda _: self.preview())
        ttk.Checkbutton(pager, text="放大", variable=self.zoom, command=self.preview).pack(side="left", padx=10)
        view = ttk.Frame(page, style="Card.TFrame")
        view.grid(sticky="nsew")
        view.rowconfigure(0, weight=1)
        view.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(view, background="#f3f5f8", highlightthickness=0, width=1, height=1)
        scroll_y = ttk.Scrollbar(view, orient="vertical", command=self.canvas.yview)
        scroll_x = ttk.Scrollbar(view, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))
        self.canvas.bind("<Configure>", self.position_preview)
        status = ttk.Label(page, textvariable=self.status)
        status.grid(sticky="ew", pady=(6, 0))
        status.bind("<Configure>", lambda event: status.configure(wraplength=max(200, event.width - 8)))

    def settings(self):
        return Settings.load(config_source(self.workbench.config_path))

    def show_error(self, error):
        message = str(error)
        self.status.set(message)
        messagebox.showerror("報單列印", message, parent=self.page)

    def task(self, title, work, finish):
        if self.workbench.busy:
            return
        self.status.set(title)
        self.workbench.run_task(title, work, finish, self.show_error)

    def lookup(self):
        prefix = self.number.get()

        def ready(ids):
            self.numbers.configure(values=ids)
            if ids:
                self.number.set(ids[0])
            self.status.set(f"找到 {len(ids)} 筆報單（最多 100 筆），可從下拉清單選取。")

        self.task("查詢測試資料庫…", lambda: report_ids(self.settings(), prefix), ready)

    def load(self):
        ids = re.split(r"[,，\s]+", self.number.get().strip())
        self.load_task(lambda: EditableReport.from_database(self.settings(), ids))

    def open(self):
        path = filedialog.askopenfilename(parent=self.page, filetypes=[("Rpx 報單", "*.rpx")])
        if path:
            self.load_task(lambda: EditableReport.open(Path(path)))

    def load_task(self, source):
        def work():
            report = source()
            return report, report.pdf()

        self.task("產生報單預覽…", work, lambda result: self.install(*result))

    def install(self, report, pdf, index=0, path=None):
        self.report, self.pdf = report, pdf
        choices = [f"{i + 1} · {number} · {side}" for i, (number, side) in enumerate(
            (number, side) for number in report.batch.ids for side in ("正面", "背面")
        )]
        self.pages.configure(values=choices)
        self.pages.current(index)
        self.status.set(f"已儲存 {path}" if path else f"已載入 {len(report.batch.ids)} 張報單。")
        for button in self.buttons.values():
            button.state(["!disabled"])
        self.preview()

    def preview(self):
        if self.pdf is None:
            return
        try:
            import pypdfium2
            from PIL import ImageTk

            with closing(pypdfium2.PdfDocument(self.pdf)) as pdf, closing(pdf[self.pages.current()]) as page:
                fit = min((self.canvas.winfo_width() - 24) / page.get_width(),
                          (self.canvas.winfo_height() - 24) / page.get_height())
                scale = 1.55 if self.zoom.get() else max(0.40, min(0.85, fit))
                with closing(page.render(scale=scale)) as bitmap:
                    self.image = ImageTk.PhotoImage(bitmap.to_pil(), master=self.page)
        except Exception as error:
            self.show_error(error)
            return
        self.canvas.delete("all")
        self.canvas.create_image(12, 12, anchor="nw", image=self.image, tags=("report",))
        self.position_preview()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def position_preview(self, _=None):
        if self.image is not None:
            width = max(self.canvas.winfo_width(), self.image.width() + 24)
            self.canvas.coords("report", (width - self.image.width()) // 2, 12)
            self.canvas.configure(scrollregion=(0, 0, width, self.image.height() + 24))

    def active_report(self):
        if self.report is None:
            raise RpxError("請先載入報單")
        return self.report, self.pages.current() // 2

    def saved(self, path):
        self.status.set(f"已儲存 {path}")

    def export(self):
        report, _ = self.active_report()
        self.task("輸出 PDF…", lambda: report.export_pdf(self.output), self.saved)

    def print_report(self):
        report, _ = self.active_report()

        def ask(plan):
            if messagebox.askokcancel("確認直接列印", plan.summary(report.batch), parent=self.page):
                self.task("送出列印…", lambda: print_batch(report.batch, plan, notify=lambda _: None),
                          lambda _: self.status.set("已送出列印。"))
            else:
                self.status.set("已取消列印。")

        self.task("讀取印表機設定…", lambda: plan_print(self.settings()), ask)

    def export_excel(self):
        try:
            report, index = self.active_report()
            self.save_excel(report, index)
        except RpxError as error:
            self.show_error(error)

    def save_excel(self, report, index, inputs=None, *, exchange=None):
        self.task("產生更正申請書…", lambda: export_correction(report, index, inputs, self.output,
                                                               exchange=exchange), self.saved)

    def change(self, index, operation):
        def work():
            report, path = operation()
            return report, report.pdf(), path

        def done(result):
            report, pdf, path = result
            self.install(report, pdf, index * 2, path)

        self.task("儲存 Rpx 副本…", work, done)

    def open_trial(self):
        if self.workbench.busy:
            return
        try:
            report, index = self.active_report()
            return TrialDialog(self, report, index)
        except RpxError as error:
            self.show_error(error)

    def open_editor(self):
        if self.workbench.busy:
            return
        report, index = self.active_report()
        fields = report.fields(index)
        if not fields:
            self.show_error("這張報單沒有可編輯欄位")
            return
        window = tk.Toplevel(self.page)
        window.title("修改欄位")
        window.transient(self.page.winfo_toplevel())
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        choices = [f"{position + 1} · {name} · {' '.join(value.split())[:38]}" for position, name, value in fields]
        picker = ttk.Combobox(window, values=choices, state="readonly", width=56)
        picker.grid(row=0, column=0, sticky="ew", padx=12, pady=10)
        editor = tk.Text(window, height=5, width=56, wrap="word")
        editor.grid(row=1, column=0, sticky="nsew", padx=12)

        def load_field(_=None):
            editor.delete("1.0", "end")
            editor.insert("1.0", fields[picker.current()][2])

        def save():
            if self.workbench.busy:
                return
            position = fields[picker.current()][0]
            value = editor.get("1.0", "end-1c")
            self.change(index, lambda: report.edit(index, position, value, self.output))
            window.destroy()

        ttk.Button(window, text="另存 Rpx 副本", command=save).grid(row=2, column=0, sticky="e", padx=12, pady=10)
        picker.bind("<<ComboboxSelected>>", load_field)
        picker.current(0)
        load_field()
        window.grab_set()


class TrialDialog:
    def __init__(self, view, report, index):
        self.view, self.report, self.index = view, report, index
        self.trial = report.trial(index)
        entries = report.inputs(index)
        self.window = tk.Toplevel(view.page)
        self.window.title("數量金額試算")
        self.window.transient(view.page.winfo_toplevel())
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)
        width = min(1040, self.window.winfo_screenwidth() - 100)
        height = min(270 + 42 * len(entries), 530, self.window.winfo_screenheight() - 120)
        self.window.geometry(f"{width}x{height}")
        ttk.Label(self.window, text="數量金額試算", style="Title.TLabel").grid(sticky="w", padx=14, pady=12)
        table = ttk.Frame(self.window)
        table.grid(sticky="nsew", padx=14)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        canvas = tk.Canvas(table, highlightthickness=0, background="#f3f5f8")
        grid = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=grid, anchor="nw")
        grid.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        scroll = ttk.Scrollbar(table, orient="vertical", command=canvas.yview)
        scroll_x = ttk.Scrollbar(table, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=scroll.set, xscrollcommand=scroll_x.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.values, self.results = [], []
        self.total = tk.StringVar()
        self.notice = tk.StringVar()
        item = self.trial.items[0]
        headings = ("項次", f"數量 ({item.unit})", "單價 (USD)", "密度（選填）", "金額 (USD)",
                    "淨重 (KGM)", f"統計數量 ({item.stat_unit})", "離岸價格 (TWD)")
        for col, name in enumerate(headings):
            ttk.Label(grid, text=name).grid(row=0, column=col, padx=5, pady=6, sticky="w")
        for row, (item, entry) in enumerate(zip(self.trial.items, entries), 1):
            ttk.Label(grid, text=item.number).grid(row=row, column=0, pady=3)
            variables = [tk.StringVar(value=str(value) if value is not None else "")
                         for value in (entry.quantity, entry.price, entry.density)]
            for col, var in enumerate(variables, 1):
                ttk.Entry(grid, width=11, textvariable=var).grid(row=row, column=col, padx=3, pady=4)
                var.trace_add("write", self.update)
            self.values.append(variables)
            result = [tk.StringVar() for _ in range(4)]
            self.results.append(result)
            for col, var in enumerate(result, 4):
                ttk.Label(grid, textvariable=var, width=13, anchor="e").grid(row=row, column=col, padx=3)
        ttk.Label(self.window, textvariable=self.total, wraplength=width - 40).grid(sticky="w", padx=14, pady=5)
        release = report.release(index) or "報單未載"
        details = ttk.Frame(self.window)
        details.grid(sticky="w", padx=14)
        self.exchange = tk.StringVar(value=str(report.exchange(index)))
        self.exchange.trace_add("write", self.update)
        ttk.Label(details, text="匯率").pack(side="left")
        ttk.Entry(details, width=11, textvariable=self.exchange).pack(side="left", padx=5)
        ttk.Label(details, text=f"放行日期 {release}").pack(side="left")
        ttk.Label(self.window, textvariable=self.notice, wraplength=760).grid(sticky="w", padx=14, pady=4)
        bar = ttk.Frame(self.window, padding=(14, 6))
        bar.grid(sticky="ew")
        self.save = ttk.Button(bar, text="套用並另存 Rpx", style="Primary.TButton", command=self.apply)
        self.excel = ttk.Button(bar, text="產生更正申請書", command=self.export)
        self.save.pack(side="left", padx=(0, 6))
        self.excel.pack(side="left")
        self.update()
        self.window.grab_set()

    def inputs(self):
        return tuple(ItemInput(decimal_input(q.get()), decimal_input(p.get()),
                               decimal_input(d.get()) if d.get().strip() else None)
                     for q, p, d in self.values)

    def update(self, *_):
        try:
            result = self.trial.calculate(self.inputs(), exchange=decimal_input(self.exchange.get()))
        except RpxError as error:
            self.total.set("")
            self.notice.set(str(error))
            for variables in self.results:
                for var in variables:
                    var.set("—")
            for button in (self.save, self.excel):
                button.state(["disabled"])
            return
        for variables, row in zip(self.results, result.rows):
            for var, value in zip(variables, (row.usd, row.net, row.liters, row.twd)):
                var.set(f"{value:,.2f}" if value.as_tuple().exponent < 0 else f"{value:,}")
        self.total.set(f"合計　數量 {result.quantity:,}　USD {result.usd:,}　淨重 {result.net:,}　"
                       f"統計數量 {result.liters:,}\n離岸價格 TWD {result.twd:,}　推廣費 {result.fee:,}")
        self.notice.set("部分項次無法從原值反推 API，目前以原比例換算。可輸入密度改用 Table 13。"
                        if any(row.needs_density for row in result.rows) else "")
        for button in (self.save, self.excel):
            button.state(["!disabled"])

    def apply(self):
        if self.view.workbench.busy:
            return
        inputs = self.inputs()
        exchange = decimal_input(self.exchange.get())
        self.view.change(self.index, lambda: self.report.apply(self.index, inputs, self.view.output,
                                                              exchange=exchange))
        self.window.destroy()

    def export(self):
        if self.view.workbench.busy:
            return
        self.view.save_excel(self.report, self.index, self.inputs(), exchange=decimal_input(self.exchange.get()))
        self.window.destroy()
