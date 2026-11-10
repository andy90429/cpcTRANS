import json
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from .report.printing import list_printers
from .settings import OTHER_FIELDS, other_fields, read_config
from .settings_editor import (
    PRODUCT_FIELDS, lookup_note, save_countries, save_other, save_products, unsupported_cp950, valid_buyer,
    valid_product,
)
from .ship_view import ShipPanel
from .ships import SHIP_FILENAME
from .workbench import config_source, save_options


class SettingsPages:
    def __init__(self, app, notebook, make_table):
        self.app = app
        self.path = app.config_path
        self.forms = {}
        self.tables = []
        self.printers_requested = False
        for name, build in (
            ("轉檔", self.conversion), ("其他申報事項", self.other),
            ("油品表", self.products), ("買方國家", self.countries),
            ("列印", self.printing), ("海關回覆", self.notifications), ("船舶資料", self.ships),
        ):
            page = ttk.Frame(notebook, padding=12)
            notebook.add(page, text=name)
            build(page, make_table)
        notebook.bind("<<NotebookTabChanged>>", self.tab_changed)

    def tab_changed(self, event):
        notebook = event.widget
        tab = notebook.tab(notebook.select(), "text")
        if tab == "列印" and not self.printers_requested:
            self.reload_printers()
        elif tab == "船舶資料":
            self.ship_panel.refresh()

    def ships(self, page, make_table):
        self.ship_panel = ShipPanel(self.app, page, make_table, self.path.with_name(SHIP_FILENAME))

    def load(self):
        config = read_config(config_source(self.path))
        for (section, key), (control, default) in self.forms.items():
            if isinstance(control, tk.StringVar):
                if default in ("yes", "no"):
                    control.set("yes" if config.getboolean(section, key, fallback=default == "yes") else "no")
                else:
                    control.set(config.get(section, key, fallback=default))
            else:
                control.delete("1.0", "end")
                control.insert("1.0", config.get(section, key, fallback=default))
        legacy = config.get("conversion", "other", fallback="")
        text = json.loads(config["other"]["text_json"]) if config.has_option("other", "text_json") else legacy
        if not isinstance(text, str):
            raise ValueError("其他申報事項必須是文字。")
        self.other_text.insert("1.0", text)
        fields = other_fields(config.get("other", "fields", fallback=",".join(OTHER_FIELDS)))
        for key, variable in self.fields.items():
            variable.set(key in fields)
        if config.has_section("edi") and not config.has_option("edi", "enabled"):
            self.forms["edi", "enabled"][0].set("yes")
        for section, table, decode in self.tables:
            for code, data in config.items(section):
                table.insert(code.upper(), decode(data))

    def action(self, parent, text, command, *, primary=False):
        button = ttk.Button(parent, text=text, command=command, style="Primary.TButton" if primary else "TButton")
        self.app.setting_actions.append(button)
        return button

    def variable(self, section, key, default):
        variable = tk.StringVar(value=default)
        self.forms[section, key] = variable, default
        return variable

    def entry(self, parent, row, label, section, key, default=""):
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
        variable = self.variable(section, key, default)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=3)

    def checkbox(self, parent, row, label, section, key, default="yes"):
        variable = (self.app.values[key] if key == "check_after_convert" else self.variable(section, key, default))
        self.forms[section, key] = variable, default
        ttk.Checkbutton(parent, text=label, variable=variable, onvalue="yes", offvalue="no").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2)

    def text(self, parent, row, label, section, key):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=3)
        control = ScrolledText(parent, height=3, width=25, wrap="word")
        control.grid(row=row, column=1, sticky="ew", pady=3)
        self.forms[section, key] = control, ""

    def save_button(self, page, command, row):
        self.action(page, "儲存", command, primary=True).grid(row=row, column=0, sticky="w", pady=(10, 0))

    def run_save(self, task, *, refresh_watch=False):
        def finish(_):
            if refresh_watch:
                self.app.reply_page.restart_watch()
            self.app.show_feedback(f"已儲存 {self.path}")

        self.app.run_task("正在儲存設定…", task, finish)

    def save_form(self, *sections):
        updates = {section: {} for section in sections}
        for (section, key), (control, _) in self.forms.items():
            if section in updates:
                value = (control.get().strip() if isinstance(control, tk.StringVar)
                         else "\n".join(line.strip() for line in control.get("1.0", "end-1c").splitlines()
                                         if line.strip()))
                updates[section][key] = value
        self.run_save(lambda: save_options(self.path, updates), refresh_watch="notify" in sections)

    def conversion(self, page, _):
        for row, label, key, default in ((0, "使用者代碼", "user_code", "USER1"), (1, "報關箱號", "cusboxno", "")):
            self.entry(page, row, label, "conversion", key, default)
        self.checkbox(page, 2, "寫回買方／船舶代碼表", "conversion", "write_codes")
        self.checkbox(page, 3, "轉完自動檢查（跟 CBS 按 F6 一樣）", "workflow", "check_after_convert")
        self.checkbox(page, 4, "轉入成功後歸檔", "workflow", "archive_after_convert")
        ttk.Label(page, text="邏輯檢查外掛", style="Title.TLabel").grid(row=5, column=0, columnspan=2, sticky="w",
                                                                  pady=(8, 3))
        for row, label, key, default in (
            (6, "外掛目錄", "plugin_dir", ""), (7, "Python 執行檔", "python_exe", ""),
            (8, "逾時秒數", "timeout_seconds", "120"), (9, "規則測試庫", "rules_database", ""),
        ):
            self.entry(page, row, label, "logic_check", key, default)
        self.save_button(page, lambda: self.save_form("conversion", "workflow", "logic_check"), 10)

    def other(self, page, _):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)
        self.checkbox(page, 0, "啟用其他申報事項", "other", "enabled")
        fields = ttk.Frame(page)
        fields.grid(row=1, column=0, sticky="w", pady=3)
        ttk.Label(fields, text="寫入欄位").pack(side="left", padx=(0, 8))
        self.fields = {key: tk.BooleanVar(value=True) for key in OTHER_FIELDS}
        for key, variable in self.fields.items():
            ttk.Checkbutton(fields, text=key, variable=variable).pack(side="left")
        self.other_text = ScrolledText(page, height=7, width=42, wrap="word")
        self.other_text.grid(row=2, column=0, sticky="nsew", pady=6)
        note = ttk.Frame(page)
        note.grid(row=3, column=0, sticky="w")
        ttk.Label(note, text="片語代碼").pack(side="left")
        self.note_code = ttk.Entry(note, width=14)
        self.note_code.pack(side="left", padx=8)
        self.action(note, "帶入", self.insert_note).pack(side="left")
        self.save_button(page, self.save_other, 4)

    def insert_note(self):
        code = self.note_code.get().strip()
        if not code:
            self.app.show_feedback("請輸入片語代碼。", error=True)
            return

        def finish(value):
            name, text = value
            if self.other_text.get("1.0", "end-1c") and not messagebox.askyesno(
                    "帶入片語", f"要用片語「{name}」取代目前的文字嗎？", parent=self.app.root):
                return
            self.other_text.delete("1.0", "end")
            self.other_text.insert("1.0", text)
            self.app.show_feedback("已帶入片語。")

        self.app.run_task("正在查詢片語…", lambda: lookup_note(self.path, code), finish)

    def save_other(self):
        text = self.other_text.get("1.0", "end-1c")
        invalid = unsupported_cp950(text)
        if invalid and not messagebox.askyesno(
                "確認其他申報事項",
                "下列字元無法編成 Big5（cp950）：\n" + "、".join(repr(char) for char in invalid)
                + "\n\n仍要儲存嗎？", parent=self.app.root):
            return
        enabled = self.forms["other", "enabled"][0].get()
        fields = ",".join(key for key, selected in self.fields.items() if selected.get())
        self.run_save(lambda: save_other(self.path, enabled, fields, text))

    def products(self, page, make_table):
        table = EditableTable(self, page, make_table, "油品代碼",
                              tuple(zip(PRODUCT_FIELDS, ("稅則", "報單品名", "簽審文號", "許可證號", "許可證項次"))),
                              valid_product, "油品代碼須符合 999-X99999-999。")
        self.tables.append(("products", table, json.loads))
        self.save_button(page, lambda: self.save_table(table, save_products), 2)

    def countries(self, page, make_table):
        def decode(data):
            country, name = data.split(maxsplit=1)
            return {"country": country, "name": name}

        def persist(path, rows):
            save_countries(path, [(code, row["country"], row["name"]) for code, row in rows])

        table = EditableTable(self, page, make_table, "買方代號", (("country", "國家代碼"), ("name", "國家名稱")),
                              valid_buyer, "買方代號須為 6 碼英數。")
        self.tables.append(("buyer_countries", table, decode))
        self.save_button(page, lambda: self.save_table(table, persist), 2)

    def save_table(self, table, persist):
        rows = tuple((code, row.copy()) for code, row in table.data.values())
        self.run_save(lambda: persist(self.path, rows))

    def printing(self, page, _):
        page.columnconfigure(1, weight=1)
        ttk.Label(page, text="報單印表機").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.printer = ttk.Combobox(page, textvariable=self.variable("print", "printer", ""))
        self.printer.grid(row=0, column=1, sticky="ew")
        self.action(page, "重新讀取印表機", self.reload_printers).grid(row=0, column=2, padx=(10, 0))
        self.save_button(page, lambda: self.save_form("print"), 1)

    def reload_printers(self):
        if self.app.busy:
            return
        self.printers_requested = True
        def failed(_):
            self.app.show_feedback("無法讀取印表機清單，仍可手動輸入。", error=True)

        self.app.run_task("正在讀取印表機…", list_printers,
                          lambda names: self.printer.configure(values=names), failed)

    def notifications(self, page, _):
        page.columnconfigure(0, weight=1)
        page.columnconfigure(1, weight=1)
        left = ttk.Frame(page, padding=(0, 0, 10, 0))
        left.grid(row=0, column=0, sticky="nsew")
        for row, label, key, default in (
            (0, "啟用回覆監看", "enabled", "yes"), (1, "通知 C3", "c3", "yes"),
            (2, "通知 C1A18", "c1a18", "yes"),
        ):
            self.checkbox(left, row, label, "notify", key, default)
        self.entry(left, 3, "監看間隔（秒）", "notify", "poll_seconds", "60")
        self.text(left, 4, "C3 PDF", "notify", "c3_pdfs")
        self.text(left, 5, "C1A18 PDF", "notify", "c1a18_pdfs")
        self.entry(left, 6, "PDF 列印指令", "notify", "print_command")
        right = ttk.Frame(page, padding=(10, 0, 0, 0))
        right.grid(row=0, column=1, sticky="nsew")
        self.checkbox(right, 0, "啟用 EDI 測試設定", "edi", "enabled", "no")
        for row, label, key in (
            (1, "EDI 測試庫", "edi_database"), (2, "XML 設定測試庫", "xmlset_database"),
            (3, "公司代碼", "company_code"), (4, "使用者", "user"),
        ):
            self.entry(right, row, label, "edi", key)
        self.save_button(page, lambda: self.save_form("notify", "edi"), 1)


class EditableTable:
    def __init__(self, editor, page, make_table, label, fields, validate, message):
        self.editor, self.page = editor, page
        self.label, self.fields, self.validate, self.message = label, fields, validate, message
        self.data = {}
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        actions = ttk.Frame(page)
        actions.grid(row=0, column=0, sticky="w", pady=(0, 8))
        for title, command in (("新增", lambda: self.edit(None)), ("修改", self.edit_selected), ("刪除", self.delete)):
            editor.action(actions, title, command).pack(side="left", padx=(0, 8))
        panel, self.tree = make_table(page, (("code", label, 140, False), *(
            (key, title, 120, key in ("description", "name")) for key, title in fields
        )))
        panel.grid(row=1, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _: self.edit_selected())

    def values(self, code, data):
        return (code, *(" ".join(str(data.get(key) or "").splitlines()) for key, _ in self.fields))

    def insert(self, code, data):
        row = self.tree.insert("", "end", values=self.values(code, data))
        self.data[row] = code, data

    def edit_selected(self):
        selected = self.tree.selection()
        if selected:
            self.edit(selected[0])

    def edit(self, row):
        if self.editor.app.busy:
            return
        code, data = self.data.get(row, ("", {}))
        used = {other for key, (other, _) in self.data.items() if key != row}
        dialog = RowDialog(self.page, self, code, data, used)
        if dialog.result is None:
            return
        code, data = dialog.result
        if row is None:
            self.insert(code, data)
        else:
            self.data[row] = code, data
            self.tree.item(row, values=self.values(code, data))

    def delete(self):
        for row in self.tree.selection():
            del self.data[row]
            self.tree.delete(row)


class RowDialog(simpledialog.Dialog):
    def __init__(self, parent, table, code, data, used):
        self.table, self.code, self.data, self.used = table, code, data, used
        super().__init__(parent, title=table.label)

    def body(self, parent):
        parent.columnconfigure(1, weight=1)
        self.inputs = {}
        for row, (key, label) in enumerate((("code", self.table.label), *self.table.fields)):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=4)
            value = self.code if key == "code" else self.data.get(key, "")
            if key == "description":
                control = ScrolledText(parent, width=45, height=4, wrap="word")
                control.insert("1.0", value)
            else:
                control = ttk.Entry(parent, width=42)
                control.insert(0, value)
            control.grid(row=row, column=1, sticky="ew", pady=4)
            self.inputs[key] = control
        return self.inputs["code"]

    def validate(self):
        values = {
            key: control.get("1.0", "end-1c") if key == "description" else control.get().strip()
            for key, control in self.inputs.items()
        }
        code = values.pop("code").upper()
        if not self.table.validate(code):
            message = self.table.message
        elif code in self.used:
            message = f"{self.table.label}重複：{code}"
        elif "country" in values and (len(values["country"].split()) != 1 or not values["name"]):
            message = "請填寫國家代碼與國家名稱。"
        else:
            self.result = code, {**self.data, **values}
            return True
        messagebox.showerror(self.table.label, message, parent=self)
        return False
