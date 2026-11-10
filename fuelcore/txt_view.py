import subprocess
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from .ship_view import ShipDialog, save_ship
from .ships import SHIP_FILENAME, Ship, load_ships
from .txt_edit import HEADER_FIELDS, ITEM_FIELDS


def display(value):
    return "" if value is None else format(value, "f")


class TxtEditWindow:
    def __init__(self, app, editor, make_table):
        self.app, self.editor = app, editor
        self.saving = False
        self.window = window = tk.Toplevel(app.root)
        window.title(f"編輯 TXT — {editor.path.name}")
        window.transient(app.root)
        window.geometry("1120x750")
        window.minsize(740, 480)
        window.protocol("WM_DELETE_WINDOW", self.close)

        page = ttk.Frame(window, padding=12)
        page.pack(fill="both", expand=True)
        page.columnconfigure(0, weight=1)
        for row, weight in ((1, 1), (3, 2), (5, 1)):
            page.rowconfigure(row, weight=weight)

        ttk.Label(page, text="油品換算表（雙擊密度可修改）").grid(row=0, column=0, sticky="w")
        panel, self.oils = make_table(page, (
            ("name", "品名", 190, True), ("tons", "噸數", 75, False), ("density", "密度", 80, False),
            ("range", "TXT 回推範圍", 145, False), ("api", "API", 65, False), ("factor", "係數", 75, False),
            ("calc_ltr", "計算 LTR", 95, False), ("txt_ltr", "TXT 的 LTR", 95, False),
        ))
        self.oils.configure(height=3)
        panel.grid(row=1, column=0, sticky="nsew")
        self.oils.bind("<Double-1>", self.edit_density)

        header_bar = ttk.Frame(page)
        header_bar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(header_bar, text="主檔欄位清單").pack(side="left")
        self.used_only = tk.BooleanVar(master=window, value=True)
        ttk.Checkbutton(header_bar, text="只顯示有資料或有用到的欄位", variable=self.used_only,
                        command=self.render).pack(side="left", padx=12)
        self.ships_path = app.config_path.with_name(SHIP_FILENAME)
        ttk.Label(header_bar, text="選船").pack(side="left")
        self.ship_combo = ttk.Combobox(header_bar, state="readonly", width=22)
        self.ship_combo.pack(side="left", padx=(6, 8))
        self.ship_combo.bind("<<ComboboxSelected>>", self.select_ship)
        self.add_ship_button = ttk.Button(header_bar, text="加入船舶清單", command=self.add_ship)
        self.add_ship_button.pack(side="left")
        ships, warning = load_ships(self.ships_path)
        self.show_ships(ships)
        if warning:
            app.add_log("提醒", warning)
        panel, self.headers = make_table(page, (
            ("index", "欄號", 55, False), ("label", "說明", 175, False), ("value", "內容", 220, True),
            ("original", "原值", 220, True),
        ))
        panel.grid(row=3, column=0, sticky="nsew")
        self.headers.bind("<Double-1>", self.edit_header)

        ttk.Label(page, text="項次表格（雙擊儲存格可修改）").grid(row=4, column=0, sticky="w", pady=(6, 0))
        panel, self.items = make_table(page, (
            ("item", "項次", 55, False),
            *((f"i{index}", f"I{index} {ITEM_FIELDS.get(index, '')}", 130 if index in ITEM_FIELDS else 60, False)
              for index in range(17)),
        ))
        self.items.configure(height=3)
        panel.grid(row=5, column=0, sticky="nsew")
        self.items.bind("<Double-1>", self.edit_item)
        for table in (self.oils, self.headers, self.items):
            table.tag_configure("changed", background="#fff4c2")

        ttk.Label(page, text="檢核訊息（供確認）").grid(row=6, column=0, sticky="w", pady=(6, 0))
        self.messages = ScrolledText(page, height=4, width=52, state="disabled", wrap="word")
        self.messages.grid(row=7, column=0, sticky="ew")
        buttons = ttk.Frame(page)
        buttons.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        self.actions = [self.add_ship_button]
        for label, action in (("還原", self.restore), ("存檔", self.save), ("取消", self.close),
                              ("用記事本開啟", self.open_notepad)):
            button = ttk.Button(buttons, text=label, command=action,
                                style="Primary.TButton" if label == "存檔" else "TButton")
            button.pack(side="left", padx=(0, 8))
            self.actions.append(button)
        self.render()
        window.grab_set()

    @staticmethod
    def populate(table, rows):
        selection = table.selection()
        x, y = table.xview()[0], table.yview()[0]
        table.delete(*table.get_children())
        for row_id, values, changed in rows:
            table.insert("", "end", iid=str(row_id), values=values, tags=("changed",) if changed else ())
        table.selection_set([row for row in selection if table.exists(row)])
        table.xview_moveto(x)
        table.yview_moveto(y)

    def render(self):
        editor = self.editor
        self.populate(self.headers, (
            (column, (f"H{column}", HEADER_FIELDS.get(column, ""), text, old), text != old)
            for column, (text, old) in enumerate(zip(editor.header, editor.original_header))
            if not self.used_only.get() or column in HEADER_FIELDS or text or old
        ))
        self.populate(self.items, (
            (index, (index + 1, *item), tuple(item) != editor.original_items[index])
            for index, item in enumerate(editor.items)
        ))
        oils = []
        for index, (item, density) in enumerate(zip(editor.items, editor.densities)):
            api, factor, liters = editor.calculation(index)
            original, low, high = editor.inferred[index]
            span = f"{low:f} ～ {high:f}" if low is not None else ""
            oils.append((index, (item[4], item[9], display(density), span, display(api), display(factor),
                                 display(liters), item[11]),
                         tuple(item) != editor.original_items[index] or density != original))
        self.populate(self.oils, oils)
        warnings = editor.warnings()
        self.messages.configure(state="normal")
        self.messages.delete("1.0", "end")
        self.messages.insert("1.0", "\n".join(warnings) if warnings else "沒有檢核提醒。")
        self.messages.configure(state="disabled")

    def cell(self, table, event):
        if self.saving or table.identify_region(event.x, event.y) != "cell":
            return None, None
        row = table.identify_row(event.y)
        return (int(row), table.identify_column(event.x)) if row else (None, None)

    def ask(self, label, current, apply):
        text = simpledialog.askstring("修改 TXT", label, initialvalue=current, parent=self.window)
        if text is None:
            return
        try:
            apply(text)
        except ValueError as error:
            messagebox.showerror("修改 TXT", str(error), parent=self.window)
            return
        self.render()

    def edit_density(self, event):
        index, column = self.cell(self.oils, event)
        if column == "#3":
            self.ask(f"第 {index + 1} 項密度（可留空）", display(self.editor.densities[index]),
                     lambda value: self.editor.edit_density(index, value))

    def edit_header(self, event):
        index, column = self.cell(self.headers, event)
        if column == "#3":
            self.ask(f"H{index} {HEADER_FIELDS.get(index, '')}", self.editor.header[index],
                     lambda value: self.editor.edit_header(index, value))

    def edit_item(self, event):
        index, selected = self.cell(self.items, event)
        if selected is None:
            return
        column = int(selected[1:]) - 2
        if column > 0:
            self.ask(f"第 {index + 1} 項 I{column} {ITEM_FIELDS.get(column, '')}", self.editor.items[index][column],
                     lambda value: self.editor.edit_item(index, column, value))

    def restore(self):
        self.editor.restore()
        self.ship_combo.set("")
        self.render()

    def show_ships(self, ships):
        self.ships = ships
        self.ship_combo.configure(values=tuple(ship.name for ship in ships))

    def select_ship(self, _=None):
        index = self.ship_combo.current()
        if self.saving or index < 0:
            return
        ship = self.ships[index]
        for column, value in ((20, ship.name), (22, ship.imo), (27, ship.call_sign)):
            self.editor.edit_header(column, value)
        if ship.buyer_id:
            self.editor.edit_header(23, ship.buyer_id)
        self.render()

    def add_ship(self):
        if self.app.busy or self.saving:
            return
        header = self.editor.header
        dialog = ShipDialog(self.app, self.window, Ship(header[20], header[22], header[27], header[23]))
        if dialog.result is not None:
            def finish(ships):
                if self.window.winfo_exists():
                    self.show_ships(ships)
                self.app.add_log("成功", f"已加入船舶清單：{dialog.result.name}")

            save_ship(self.app, self.window, self.ships_path, finish, dialog.result)

    def close(self):
        if self.saving:
            return
        if self.editor.dirty and not messagebox.askokcancel("取消 TXT 編輯", "放棄未存檔修改？", parent=self.window):
            return
        self.window.destroy()

    def save(self):
        if self.app.busy:
            messagebox.showinfo("TXT 編輯", "請等候工作台目前的作業完成。", parent=self.window)
            return
        self.saving = True
        for button in self.actions:
            button.state(["disabled"])

        def finish(backup):
            if backup:
                self.app.add_log("成功", f"已儲存 {self.editor.path.name}，原檔備份：{backup}")
            self.window.destroy()
            self.app.refresh()

        def failed(message):
            self.saving = False
            for button in self.actions:
                button.state(["!disabled"])
            messagebox.showerror("無法儲存 TXT", message, parent=self.window)

        self.app.run_task("正在儲存 TXT…", self.editor.save, finish, failed)

    def open_notepad(self):
        try:
            subprocess.Popen(["notepad.exe", str(self.editor.path)])
        except OSError:
            messagebox.showerror("TXT 編輯", "無法開啟 Windows 記事本。", parent=self.window)
