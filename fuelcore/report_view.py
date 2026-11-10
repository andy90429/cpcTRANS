"""Tk controls for previewing, editing and printing a read-only report."""

import os
import re
import stat
from math import sqrt
import tkinter as tk
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .report.calc import ItemInput, decimal_input
from .report.comparison import compare_report
from .report.correction import export_correction
from .report.editing import EditableReport, report_ids
from .report.printing import plan_print, print_batch
from .report.retention import BATCH_NAME, MARKER, ordinary
from .report.rpx import RpxError
from .settings import Settings
from .runtime import record
from .workbench import config_source


ZOOM_LEVELS = ("符合寬度", "整頁", "100%", "125%", "150%", "200%", "300%")
PREVIEW_MARGIN = 12
MAX_PREVIEW_PIXELS = 24_000_000


def preview_scale(mode, viewport, paper, dpi):
    width, height = paper
    if mode in ZOOM_LEVELS[:2]:
        fit_width = max(1, viewport[0] - 2 * PREVIEW_MARGIN) / width
        fit_height = max(1, viewport[1] - 2 * PREVIEW_MARGIN) / height
        scale = min(fit_width, fit_height) if mode == "整頁" else fit_width
    else:
        scale = int(mode.rstrip("%")) / 100 * dpi / 72
    return min(scale, sqrt(MAX_PREVIEW_PIXELS / (width * height)))


def windows_open(path, action="open"):
    try:
        if not hasattr(os, "startfile"):
            raise RpxError("此動作需要 Windows。")
        os.startfile(str(path), action)
    except (OSError, RpxError):
        if action == "print":
            record("print_correction", "failed")
        raise
    if action == "print":
        record("print_correction", "submitted")


def saved_rpx(output):
    if not output.is_dir():
        return []
    folders = [(output, "修改副本"), (output / "原檔備份", "原檔備份")]
    folders.extend((folder, "資料庫產生") for folder in output.iterdir()
                   if (BATCH_NAME.fullmatch(folder.name) or re.fullmatch(r"產生_[a-z0-9_]{8}", folder.name))
                   and (folder / MARKER).is_file())
    files = []
    for folder, kind in folders:
        if not folder.exists() or not ordinary(folder, stat.S_ISDIR):
            continue
        for path in folder.iterdir():
            try:
                if path.suffix.casefold() == ".rpx" and ordinary(path, stat.S_ISREG):
                    files.append((path, kind, datetime.fromtimestamp(path.stat().st_mtime)))
            except OSError:
                continue
    return sorted(files, key=lambda entry: (entry[2], entry[0].name), reverse=True)


class ReportPage:
    def __init__(self, workbench, page):
        self.workbench = workbench
        self.page = page
        self.output = workbench.config_path.parent / "列印輸出"
        self.report = None
        self.pdf = None
        self.image = None
        self.selected_page = 0
        self.saved_report = self.saved_pdf = None
        self.differences = None
        self.saved_differences = None
        self.correction_path = None
        self.dirty_notice = tk.StringVar()
        self.selection = tk.StringVar()
        self.number = tk.StringVar()
        self.status = tk.StringVar(value="請輸入報單號碼或開啟 Rpx 副本。")
        self.zoom = tk.StringVar(value=ZOOM_LEVELS[0])
        self.preview_timer = None
        self.preview_key = None

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
        ttk.Button(load, text="我存過的檔案", command=self.saved_files).grid(row=1, column=1, sticky="w", pady=(4, 0))
        actions = ttk.Frame(page)
        actions.grid(sticky="ew", pady=8)
        self.buttons = {}
        for index, (label, command) in enumerate((
            ("輸出 PDF", self.export), ("直接列印", self.print_report), ("試算", self.open_trial),
            ("更正申請書 Excel", self.export_excel), ("修改欄位", self.open_editor),
            ("另存 Rpx", self.save_rpx), ("列印更正申請書", self.print_correction),
        )):
            button = ttk.Button(actions, text=label, command=command, state="disabled")
            button.grid(row=index // 4, column=index % 4, sticky="ew", padx=(0, 6), pady=2)
            self.buttons[label] = button
        pager = ttk.Frame(page)
        pager.grid(sticky="ew", pady=(0, 6))
        ttk.Label(pager, text="預覽頁面").pack(side="left", padx=(0, 8))
        self.pages = ttk.Combobox(pager, textvariable=self.selection, state="readonly", width=27)
        self.pages.pack(side="left")
        self.pages.bind("<<ComboboxSelected>>", self.select_page)
        ttk.Label(pager, text="縮放").pack(side="left", padx=(12, 5))
        zoom = ttk.Combobox(pager, textvariable=self.zoom, values=ZOOM_LEVELS, state="readonly", width=9)
        zoom.pack(side="left", padx=(0, 10))
        zoom.bind("<<ComboboxSelected>>", lambda _: self.preview())
        ttk.Label(pager, textvariable=self.dirty_notice, foreground="#b54708").pack(side="left")
        view = ttk.Frame(page, style="Card.TFrame")
        view.grid(sticky="nsew")
        view.rowconfigure(0, weight=1)
        view.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(view, background="#dce5ee", highlightthickness=0, width=1, height=1)
        scroll_y = ttk.Scrollbar(view, orient="vertical", command=self.canvas.yview)
        scroll_x = ttk.Scrollbar(view, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))
        self.canvas.bind("<Control-MouseWheel>", self.wheel_zoom)
        self.canvas.bind("<Configure>", self.resize_preview)
        self.diff_panel = ttk.Frame(view, padding=5)
        self.diff_panel.grid(row=0, column=2, sticky="nsew")
        ttk.Label(self.diff_panel, text="資料庫比對差異").pack(anchor="w")
        self.diff_table = ttk.Treeview(self.diff_panel, columns=("name", "rpx", "database"), show="headings")
        for key, title in (("name", "欄位"), ("rpx", "Rpx 值"), ("database", "資料庫值")):
            self.diff_table.heading(key, text=title)
            self.diff_table.column(key, width=112)
        self.diff_table.pack(side="left", fill="both", expand=True)
        diff_scroll = ttk.Scrollbar(self.diff_panel, command=self.diff_table.yview)
        diff_scroll.pack(side="right", fill="y")
        self.diff_table.configure(yscrollcommand=diff_scroll.set)
        self.diff_panel.grid_remove()
        footer = ttk.Frame(page)
        footer.grid(sticky="ew", pady=(6, 0))
        ttk.Label(footer, textvariable=self.dirty_notice, foreground="#b54708").pack(side="right", padx=(10, 0))
        status = ttk.Label(footer, textvariable=self.status)
        status.pack(side="left", fill="x", expand=True)
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
        self.load_task(lambda: EditableReport.from_database(self.settings(), ids), generated=True)

    def open(self):
        if self.workbench.busy:
            return
        path = filedialog.askopenfilename(parent=self.page, filetypes=[("Rpx 報單", "*.rpx")])
        if path:
            self.open_path(path)

    def open_path(self, path):
        self.load_task(lambda: EditableReport.open(Path(path), backup=self.output), compare=True)

    def compare(self, report):
        try:
            return compare_report(report, self.settings())
        except (OSError, ValueError, KeyError):
            return [()] * len(report.batch.ids), ["無法讀取測試庫設定，已開啟 Rpx 預覽。"]

    def load_task(self, source, *, compare=False, generated=False):
        def work():
            report = source()
            pdf = report.pdf()
            differences, warnings = self.compare(report) if compare else (None, [])
            if generated:
                report.save_generated(self.output)
            return report, pdf, differences, warnings

        def ready(result):
            report, pdf, self.differences, warnings = result
            self.install(report, pdf)
            if warnings:
                self.status.set(" ".join(warnings))

        self.confirm_leave(lambda: self.task("產生報單預覽…", work, ready))

    def install(self, report, pdf, index=0, *, edited=False):
        self.report, self.pdf = report, pdf
        self.preview_key = None
        if not edited:
            self.saved_report, self.saved_pdf = report, pdf
            self.saved_differences = self.differences
            self.correction_path = None
        choices = [f"{i + 1} · {number} · {side}" for i, (number, side) in enumerate(
            (number, side) for number in report.batch.ids for side in ("正面", "背面")
        )]
        self.pages.configure(values=choices)
        self.status.set("已更新預覽。" if edited else f"已載入 {len(report.batch.ids)} 張報單。")
        for button in self.buttons.values():
            button.state(["!disabled"])
        self.buttons["列印更正申請書"].state(["!disabled" if self.correction_path else "disabled"])
        self.update_dirty()
        self.show_page(index)

    def update_dirty(self):
        changed = self.report.document.to_bytes() != self.saved_report.document.to_bytes()
        self.dirty_notice.set("已修改，未存檔" if changed else "")
        self.buttons["另存 Rpx"].state(["!disabled" if changed else "disabled"])

    def show_page(self, index):
        self.selected_page = index
        self.pages.current(index)
        self.show_differences()
        self.preview()

    def page_differences(self):
        if self.differences is None or self.selected_page % 2:
            return ()
        return self.differences[self.selected_page // 2]

    def show_differences(self):
        self.diff_table.delete(*self.diff_table.get_children())
        differences = self.page_differences()
        if differences:
            self.diff_panel.grid()
            for diff in differences:
                self.diff_table.insert("", "end", values=(diff.label, diff.rpx, diff.database))
        else:
            self.diff_panel.grid_remove()

    def select_page(self, _=None):
        target = self.pages.current()
        self.pages.current(self.selected_page)
        if self.workbench.busy or target < 0:
            return
        if target // 2 == self.selected_page // 2:
            self.show_page(target)
        else:
            def discard():
                self.differences = self.saved_differences
                self.install(self.saved_report, self.saved_pdf, target)

            self.confirm_leave(lambda: self.show_page(target),
                               discard)

    def confirm_leave(self, proceed, discard=None):
        if self.workbench.busy:
            return
        if not self.dirty_notice.get():
            proceed()
            return
        answer = messagebox.askyesnocancel(
            "要不要另存 Rpx？",
            "目前報單已修改，未存檔。\n\n選「是」另存 Rpx，「否」不存檔，「取消」留在目前報單。",
            parent=self.page,
        )
        if answer is True:
            self.save_rpx(after=proceed)
        elif answer is False:
            (discard or proceed)()

    def wheel_zoom(self, event):
        current = ZOOM_LEVELS.index(self.zoom.get())
        if current < 2:
            if self.preview_key is None:
                return "break"
            current_percent = self.preview_key[1] * 72 * 100 / self.canvas.winfo_fpixels("1i")
            choices = [(index, int(level.rstrip("%"))) for index, level in enumerate(ZOOM_LEVELS[2:], 2)]
            if event.delta > 0:
                current = next((index for index, percent in choices if percent > current_percent + 1), current)
            else:
                current = next((index for index, percent in reversed(choices) if percent < current_percent - 1), 1)
        else:
            current = max(1, min(len(ZOOM_LEVELS) - 1, current + (1 if event.delta > 0 else -1)))
        self.zoom.set(ZOOM_LEVELS[current])
        self.preview()
        return "break"

    def resize_preview(self, _=None):
        self.position_preview()
        if self.pdf is None or (self.preview_key is not None and self.zoom.get() not in ZOOM_LEVELS[:2]):
            return
        if self.preview_timer is not None:
            self.canvas.after_cancel(self.preview_timer)
        self.preview_timer = self.canvas.after(100, lambda: self.preview(reset_scroll=False))

    def preview(self, *, reset_scroll=True):
        if self.preview_timer is not None:
            self.canvas.after_cancel(self.preview_timer)
            self.preview_timer = None
        viewport = self.canvas.winfo_width(), self.canvas.winfo_height()
        if self.pdf is None or min(viewport) <= 2 * PREVIEW_MARGIN:
            return
        index = self.selected_page
        try:
            import pypdfium2
            from PIL import ImageTk

            with closing(pypdfium2.PdfDocument(self.pdf)) as pdf, closing(pdf[index]) as page:
                scale = preview_scale(self.zoom.get(), viewport, (page.get_width(), page.get_height()),
                                      self.canvas.winfo_fpixels("1i"))
                key = index, round(scale, 3)
                if key == self.preview_key:
                    return
                with closing(page.render(scale=scale)) as bitmap:
                    self.image = ImageTk.PhotoImage(bitmap.to_pil(), master=self.page)
                self.preview_key = key
        except Exception as error:
            self.show_error(error)
            return
        x, y = self.canvas.xview()[0], self.canvas.yview()[0]
        self.canvas.delete("all")
        self.canvas.create_image(PREVIEW_MARGIN, PREVIEW_MARGIN, anchor="nw", image=self.image, tags=("report",))
        self.position_preview()
        self.canvas.xview_moveto(0 if reset_scroll else x)
        self.canvas.yview_moveto(0 if reset_scroll else y)

    def position_preview(self, _=None):
        if self.image is not None:
            width = max(self.canvas.winfo_width(), self.image.width() + 2 * PREVIEW_MARGIN)
            x = (width - self.image.width()) // 2
            self.canvas.coords("report", x, PREVIEW_MARGIN)
            self.canvas.configure(scrollregion=(0, 0, width, self.image.height() + 2 * PREVIEW_MARGIN))
            self.canvas.delete("difference")
            differences = self.page_differences()
            if differences:
                front = self.report.batch.fronts[self.selected_page // 2]
                sx = self.image.width() / front.number("Width", 794)
                sy = self.image.height() / front.number("Height", 1123)
                for diff in differences:
                    field = front.children[diff.position]
                    left = x + field.number("Left") * sx
                    top = PREVIEW_MARGIN + field.number("Top") * sy
                    self.canvas.create_rectangle(left, top, left + field.number("Width", 1) * sx,
                                                 top + field.number("Height", 1) * sy,
                                                 outline="#b42318", width=2, tags="difference")

    def active_report(self):
        if self.report is None:
            raise RpxError("請先載入報單")
        return self.report, self.pages.current() // 2

    def saved(self, path):
        self.status.set(f"已儲存 {path}")

    def save_rpx(self, *, after=None):
        if self.workbench.busy:
            return
        report, index = self.active_report()

        def done(path):
            self.saved_report, self.saved_pdf = report, self.pdf
            self.saved_differences = self.differences
            self.update_dirty()
            self.saved(path)
            if after:
                after()

        self.task("另存 Rpx 副本…", lambda: report.save(self.output, index), done)

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
        def ready(path):
            self.correction_path = path
            self.buttons["列印更正申請書"].state(["!disabled"])
            self.saved(path)

        self.task("產生更正申請書…", lambda: export_correction(report, index, inputs, self.output,
                                                               exchange=exchange), ready)

    def print_correction(self):
        path = self.correction_path
        if not path or self.workbench.busy:
            return
        if not messagebox.askokcancel("列印更正申請書", f"用 Windows 預設程式列印單頁更正申請書？\n{path.name}",
                                      parent=self.page):
            return
        self.task("送出更正申請書列印…", lambda: windows_open(path, "print"),
                  lambda _: self.status.set("已交給 Windows 預設程式列印。"))

    def saved_files(self):
        self.task("讀取已儲存的 Rpx…", lambda: saved_rpx(self.output), self.show_saved_files)

    def show_saved_files(self, files):
        window = tk.Toplevel(self.page)
        window.title("我存過的檔案")
        window.transient(self.workbench.root)
        tree = ttk.Treeview(window, columns=("name", "kind", "time"), show="headings", selectmode="browse")
        for key, label, width in (("name", "檔名", 340), ("kind", "類型", 130), ("time", "時間", 155)):
            tree.heading(key, text=label)
            tree.column(key, width=width)
        scroll = ttk.Scrollbar(window, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        for index, (path, kind, stamp) in enumerate(files):
            tree.insert("", "end", iid=str(index), values=(path.name, kind, f"{stamp:%Y-%m-%d %H:%M:%S}"))

        def selected():
            selection = tree.selection()
            return files[int(selection[0])][0] if selection else None

        def open_selected(_=None):
            path = selected()
            if path and not self.workbench.busy:
                self.open_path(path)

        def open_folder():
            path = selected()
            try:
                windows_open(path.parent if path else self.output)
            except (OSError, RpxError) as error:
                self.show_error(error)

        actions = ttk.Frame(window)
        actions.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(actions, text="開啟", command=open_selected).pack(side="left")
        ttk.Button(actions, text="開啟資料夾", command=open_folder).pack(side="left", padx=6)
        tree.bind("<Double-1>", open_selected)

    def change(self, index, operation, *, save=False):
        def work():
            report = operation()
            differences = self.compare(report)[0] if self.differences is not None else None
            return report, report.pdf(cache_backgrounds=False), differences

        def done(result):
            report, pdf, self.differences = result
            self.install(report, pdf, index * 2, edited=True)
            if save:
                self.save_rpx()

        self.task("更新報單預覽…", work, done)

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

        def apply():
            if self.workbench.busy:
                return
            position = fields[picker.current()][0]
            value = editor.get("1.0", "end-1c")
            self.change(index, lambda: report.edit(index, position, value))
            window.destroy()

        ttk.Button(window, text="套用", command=apply).grid(row=2, column=0, sticky="e", padx=12, pady=10)
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
        height = min(650, self.window.winfo_screenheight() - 80)
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
        self.values, self.results, self.price_entries = [], [], []
        self.total = tk.StringVar()
        self.notice = tk.StringVar()
        item = self.trial.items[0]
        headings = ("項次", f"數量 ({item.unit})", "單價 (USD)", "金額 (USD)",
                    "淨重 (KGM)", f"統計數量 ({item.stat_unit})", "離岸價格 (TWD)", "密度（選填）")
        for col, name in enumerate(headings):
            ttk.Label(grid, text=name).grid(row=0, column=col, padx=5, pady=6, sticky="w")
        for row, (item, entry) in enumerate(zip(self.trial.items, entries), 1):
            ttk.Label(grid, text=item.number).grid(row=row, column=0, pady=3)
            quantity = tk.StringVar(value=str(entry.quantity))
            density = tk.StringVar(value="" if entry.density is None else str(entry.density))
            ttk.Entry(grid, width=11, textvariable=quantity).grid(row=row, column=1, padx=3, pady=4)
            price_entry = ttk.Entry(grid, width=11)
            price_entry.insert(0, str(item.price))
            price_entry.state(["readonly"])
            price_entry.grid(row=row, column=2, padx=3, pady=4)
            self.price_entries.append(price_entry)
            ttk.Entry(grid, width=7, textvariable=density).grid(row=row, column=7, padx=3, pady=4)
            for variable in (quantity, density):
                variable.trace_add("write", self.update)
            self.values.append((quantity, density))
            result = [tk.StringVar() for _ in range(4)]
            self.results.append(result)
            for col, var in enumerate(result, 3):
                ttk.Label(grid, textvariable=var, width=13, anchor="e").grid(row=row, column=col, padx=3)
        ttk.Label(self.window, textvariable=self.total, wraplength=width - 40).grid(sticky="w", padx=14, pady=5)
        release = report.release(index) or "報單未載"
        details = ttk.Frame(self.window)
        details.grid(sticky="w", padx=14)
        self.exchange = tk.StringVar(value=str(self.trial.exchange))
        ttk.Label(details, text="匯率").pack(side="left")
        self.exchange_entry = ttk.Entry(details, width=11, textvariable=self.exchange, state="readonly")
        self.exchange_entry.pack(side="left", padx=5)
        ttk.Label(details, text=f"放行日期 {release}").pack(side="left")
        ttk.Label(self.window, textvariable=self.notice, wraplength=760).grid(sticky="w", padx=14, pady=4)
        bar = ttk.Frame(self.window, padding=(14, 6))
        bar.grid(sticky="ew")
        self.apply_button = ttk.Button(bar, text="套用", style="Primary.TButton", command=self.apply)
        self.save = ttk.Button(bar, text="另存 Rpx", command=lambda: self.apply(save=True))
        self.excel = ttk.Button(bar, text="產生更正申請書", command=self.export)
        self.apply_button.pack(side="left", padx=(0, 6))
        self.save.pack(side="left", padx=(0, 6))
        self.excel.pack(side="left")
        self.update()
        self.window.grab_set()

    def inputs(self):
        return tuple(ItemInput(decimal_input(q.get()), item.price,
                               decimal_input(d.get()) if d.get().strip() else None)
                     for item, (q, d) in zip(self.trial.items, self.values))

    def update(self, *_):
        try:
            result = self.trial.calculate(self.inputs())
        except RpxError as error:
            self.total.set("")
            self.notice.set(str(error))
            for variables in self.results:
                for var in variables:
                    var.set("—")
            for button in (self.apply_button, self.save, self.excel):
                button.state(["disabled"])
            return
        for variables, row in zip(self.results, result.rows):
            for var, value in zip(variables, (row.usd, row.net, row.liters, row.twd)):
                var.set(f"{value:,.2f}" if value.as_tuple().exponent < 0 else f"{value:,}")
        self.total.set(f"合計　數量 {result.quantity:,}　USD {result.usd:,}　淨重 {result.net:,}　"
                       f"統計數量 {result.liters:,}\n離岸價格 TWD {result.twd:,}　推廣費 {result.fee:,}")
        self.notice.set("部分項次無法從原值反推 API，目前以原比例換算。可輸入密度改用 Table 13。"
                        if any(row.needs_density for row in result.rows) else "")
        for button in (self.apply_button, self.save, self.excel):
            button.state(["!disabled"])

    def apply(self, *, save=False):
        if self.view.workbench.busy:
            return
        inputs = self.inputs()
        self.view.change(self.index, lambda: self.report.apply(self.index, inputs), save=save)
        self.window.destroy()

    def export(self):
        if self.view.workbench.busy:
            return
        self.view.save_excel(self.report, self.index, self.inputs())
        self.window.destroy()
