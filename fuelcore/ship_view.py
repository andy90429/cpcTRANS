from tkinter import messagebox, simpledialog, ttk

from .agent_lookup import lookup_ship
from .ships import Ship, change_ship, load_ships, validate_ship


FIELDS = (
    ("name", "船名"), ("imo", "IMO"), ("call_sign", "呼號"),
    ("buyer_id", "買方編號"), ("agent", "代理行"),
)


class ShipDialog(simpledialog.Dialog):
    def __init__(self, app, parent, ship):
        self.app, self.ship = app, ship
        super().__init__(parent, title="船舶資料")

    def body(self, parent):
        parent.columnconfigure(1, weight=1)
        self.inputs = {}
        for row, (key, label) in enumerate(FIELDS):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            entry = ttk.Entry(parent, width=36)
            entry.insert(0, getattr(self.ship, key))
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            self.inputs[key] = entry
        ttk.Button(parent, text="查詢代理行", command=self.query_agent).grid(row=4, column=2, padx=(8, 0))
        return self.inputs["name"]

    def query_agent(self):
        if self.app.busy:
            return
        name = self.inputs["name"].get().strip()
        if not name:
            messagebox.showinfo("查詢代理行", "請先輸入船名。", parent=self)
            return

        def finish(result):
            if not self.winfo_exists() or self.inputs["name"].get().strip() != name:
                return
            if result.status in ("found", "no_call_sign") and result.agent_full:
                entry = self.inputs["agent"]
                entry.delete(0, "end")
                entry.insert(0, result.agent_full)
            else:
                message = "找到多艘同名船，請手動填寫代理行。" if result.status == "ambiguous" else "找不到代理行。"
                messagebox.showinfo("查詢代理行", message, parent=self)

        def failed(message):
            if self.winfo_exists():
                messagebox.showinfo("查詢代理行", message, parent=self)

        self.app.run_task("正在查詢代理行…", lambda: lookup_ship(name), finish, failed)

    def validate(self):
        if self.app.busy:
            messagebox.showinfo("船舶資料", "請等候目前的作業完成。", parent=self)
            return False
        try:
            self.result = validate_ship(Ship(**{key: entry.get() for key, entry in self.inputs.items()}))
        except ValueError as error:
            messagebox.showerror("船舶資料", str(error), parent=self)
            return False
        return True


def save_ship(app, parent, path, finish, replacement=None, *, original=None):
    def failed(message):
        if parent.winfo_exists():
            messagebox.showerror("船舶資料", message, parent=parent)

    app.run_task("正在儲存船舶清單…", lambda: change_ship(path, replacement, original=original), finish, failed)


class ShipPanel:
    def __init__(self, app, page, make_table, path):
        self.app, self.page, self.path = app, page, path
        self.rows = {}
        self.warned = False
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        bar = ttk.Frame(page)
        bar.grid(row=0, column=0, sticky="w", pady=(0, 8))
        for text, command in (("新增", lambda: self.edit()), ("修改", self.edit_selected), ("刪除", self.delete)):
            button = ttk.Button(bar, text=text, command=command)
            button.pack(side="left", padx=(0, 8))
            app.setting_actions.append(button)
        panel, self.table = make_table(page, tuple(
            (key, label, width, key in ("name", "agent"))
            for (key, label), width in zip(FIELDS, (180, 90, 90, 100, 160))
        ))
        panel.grid(row=1, column=0, sticky="nsew")
        self.table.bind("<Double-1>", lambda _: self.edit_selected())

    def refresh(self):
        ships, warning = load_ships(self.path)
        self.render(ships)
        if warning and not self.warned:
            self.app.add_log("提醒", warning)
        self.warned = bool(warning)

    def render(self, ships):
        self.table.delete(*self.table.get_children())
        self.rows.clear()
        for ship in ships:
            row = self.table.insert("", "end", values=tuple(getattr(ship, key) for key, _ in FIELDS))
            self.rows[row] = ship

    def selected(self):
        selection = self.table.selection()
        return self.rows.get(selection[0]) if selection else None

    def edit_selected(self):
        ship = self.selected()
        if ship is not None:
            self.edit(ship)

    def edit(self, original=None):
        if self.app.busy:
            return
        dialog = ShipDialog(self.app, self.page, original or Ship("", "", ""))
        if dialog.result is not None:
            save_ship(self.app, self.page, self.path, self.render, dialog.result, original=original)

    def delete(self):
        ship = self.selected()
        if not self.app.busy and ship is not None and messagebox.askokcancel(
                "刪除船舶", f"要刪除「{ship.name}」嗎？", parent=self.page):
            save_ship(self.app, self.page, self.path, self.render, original=ship)
