import tkinter as tk
from collections import Counter
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from .cert_check import CertificateError, check_pfx, expiry_date
from .declare_rate import plan_rates, report_text, save_rates
from .report.retention import mark_diagnostic
from .settings import Settings
from .workbench import config_source


class ToolsPage:
    def __init__(self, app, page):
        self.app, self.page = app, page
        page.columnconfigure(0, weight=1)
        ttk.Label(page, text="工具", style="Title.TLabel").grid(sticky="w", pady=(0, 12))
        actions = ttk.Frame(page)
        actions.grid(sticky="w")
        self.buttons = []
        for row, (label, action) in enumerate((
            ("每旬匯率（先模擬再更新）…", self.simulate_rates),
            ("從離線檔案模擬匯率…", self.select_rate_files),
            ("憑證檢查…", self.inspect_certificate),
        )):
            button = ttk.Button(actions, text=label, command=action)
            button.grid(row=row, column=0, sticky="w", pady=(0, 8))
            self.buttons.append(button)
        ttk.Label(page, textvariable=app.task_status).grid(sticky="w", pady=(6, 0))

    def select_rate_files(self):
        if self.app.busy:
            return
        current = filedialog.askopenfilename(title="選擇本旬匯率 JSON", parent=self.page,
                                             filetypes=[("JSON", "*.json")])
        if current:
            history = filedialog.askopenfilename(title="選擇歷史匯率 TXT（可取消）", parent=self.page,
                                                 filetypes=[("TXT", "*.txt")])
            self.simulate_rates(tuple(Path(path) for path in (current, history) if path))

    def simulate_rates(self, files=()):
        if self.app.busy:
            messagebox.showinfo("每旬匯率", "請等候目前的作業完成，再執行模擬。", parent=self.page)
            return
        output = self.app.report_page.output.parent / "診斷"

        def work():
            settings = Settings.load(config_source(self.app.config_path))
            plan = plan_rates(settings, files=files)
            text = report_text(plan)
            output.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf-8-sig", dir=output, suffix=".txt",
                                    prefix=f"每旬匯率模擬_{datetime.now():%Y%m%d_%H%M%S}_",
                                    delete=False) as report:
                report.write(text)
                path = Path(report.name)
            mark_diagnostic(path)
            return settings, plan, path, text

        self.app.run_task("正在模擬每旬匯率…", work, self.review_rates, self.rate_error)

    def review_rates(self, preview):
        settings, plan, path, text = preview
        window = self.open_report(path, text)
        counts = Counter(change.action for change in plan.changes)
        added, changed = counts["新增"], counts["更新"]
        if not (added or changed):
            messagebox.showinfo("每旬匯率", "本旬匯率皆相同，無須更新。", parent=window)
            return
        # Keep the reply watcher from starting a background job inside the modal confirmation.
        self.app.busy = True
        try:
            approved = messagebox.askyesno(
                "確認更新每旬匯率", f"新增 {added}、更新 {changed}，要寫入測試庫嗎？", parent=window,
            )
        finally:
            self.app.busy = False
        if approved:
            self.app.run_task("正在更新匯率並讀回核對…", lambda: save_rates(settings, plan),
                              self.show_rate_result, self.rate_error)

    def open_report(self, path, text):
        window = tk.Toplevel(self.app.root)
        window.title("每旬匯率模擬報告")
        window.transient(self.app.root)
        ttk.Label(window, text=str(path), wraplength=700).pack(anchor="w", padx=12, pady=8)
        body = ScrolledText(window, width=88, height=20, wrap="word")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        body.insert("1.0", text)
        body.configure(state="disabled")
        ttk.Button(window, text="關閉", command=window.destroy).pack(pady=(0, 8))
        return window

    def show_rate_result(self, count):
        messagebox.showinfo("每旬匯率", f"已寫入測試庫並讀回核對 {count} 筆匯率。", parent=self.page)

    def rate_error(self, message):
        messagebox.showerror("每旬匯率", message, parent=self.page)

    def inspect_certificate(self):
        if self.app.busy:
            return
        path = filedialog.askopenfilename(title="選擇 PFX 檔案", parent=self.page,
                                          filetypes=[("PFX / PKCS#12", "*.pfx *.p12")])
        if not path:
            return
        value = simpledialog.askstring("憑證檢查", "登錄到期日（YYYY-MM-DD，可留白）", parent=self.page)
        if value is None:
            return
        try:
            recorded = expiry_date(value.strip()) if value.strip() else None
        except CertificateError as error:
            messagebox.showerror("憑證檢查", str(error), parent=self.page)
            return
        password = simpledialog.askstring("憑證檢查", "請輸入 PFX 密碼", show="●", parent=self.page)
        if password is None:
            return
        if self.app.busy:
            messagebox.showinfo("憑證檢查", "請等候目前的作業完成，再檢查憑證。", parent=self.page)
            return

        def work():
            try:
                return check_pfx(path, password, recorded_expiry=recorded), ""
            except CertificateError as error:
                message = str(error)
                return None, message.replace(password, "〈已遮蔽〉") if password else message
            except Exception:
                return None, "無法檢查 PFX 憑證。"

        self.app.run_task("正在檢查 PFX 憑證…", work, lambda result: self.show_certificate(result, recorded))

    def show_certificate(self, value, recorded):
        result, error = value
        if error:
            messagebox.showerror("憑證檢查", error, parent=self.page)
            return
        signing = result.signing_certificate
        lines = [f"檔名：{result.filename}", f"主體：{signing.subject or '未提供'}",
                 f"到期日：{signing.not_after.date()}", f"剩餘天數：{result.days_left}",
                 f"狀態：{result.status}"]
        if recorded is not None:
            comparison = "不一致" if result.recorded_mismatch else "一致"
            lines.append(f"登錄到期日：{recorded}（{comparison}）")
        messagebox.showinfo("憑證檢查", "\n".join(lines), parent=self.page)
