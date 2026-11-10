from pathlib import Path
from dataclasses import dataclass

from ..runtime import record
from . import prepare_report
from .rpx import RpxError


def windows_backend():
    try:
        from .gdi import GdiBackend
    except ImportError as error:
        raise RpxError("請先安裝列印相依套件：python -m pip install .[report]") from error
    return GdiBackend()


def list_printers(backend=None):
    return (backend if backend is not None else windows_backend()).printers()


@dataclass(frozen=True)
class PrintPlan:
    backend: object
    printer: str
    duplex: bool

    def summary(self, batch):
        mode = "長邊翻頁" if self.duplex else "單面（驅動程式未回報支援雙面）"
        return (f"報單：{'、'.join(batch.ids)}\n頁數：{len(batch.pages)}\n印表機：{self.printer}\n"
                f"雙面：{mode}")


def plan_print(settings, backend=None):
    backend = backend if backend is not None else windows_backend()
    names = list_printers(backend)
    configured = settings.printer
    printer = next((name for name in names if configured and name.casefold() == configured.casefold()), None)
    if printer is None:
        reason = f"找不到印表機 {configured}" if configured else "請設定 fuelcore.ini 的 [print] printer"
        raise RpxError(f"{reason}。本機印表機：{'、'.join(names) or '（無）'}")
    return PrintPlan(backend, printer, backend.duplex(printer))


def print_report(settings, sersnos, *, yes=False, reports=None, cache=None, connection=None,
                 backend=None, confirm=None, notify=print, retention_dir=Path("列印輸出")):
    plan = plan_print(settings, backend)
    batch = prepare_report(settings, sersnos, reports=reports, cache=cache, connection=connection)
    notify(plan.summary(batch))
    if not yes:
        try:
            response = (confirm or input)("輸入 y 才送出列印：")
        except (EOFError, KeyboardInterrupt):
            response = ""
        if response.strip().casefold() != "y":
            notify("已取消列印。")
            return False
    return print_batch(batch, plan, notify)


def print_batch(batch, plan, notify=print):
    try:
        result = send_batch(batch, plan, notify)
    except Exception:
        record("print_report", "failed")
        raise
    record("print_report", "submitted", count=len(batch.ids))
    return result


def send_batch(batch, plan, notify):
    try:
        from .drawing import draw_fields, prepare_pages
    except ImportError as error:
        raise RpxError("請先安裝列印相依套件：python -m pip install .[report]") from error
    pages = prepare_pages(batch.pages, batch.assets, batch.cache)
    points = pages[0].points
    if any(any(abs(a - b) > 0.1 for a, b in zip(points, page.points)) for page in pages):
        raise RpxError("同一個 GDI 列印工作需要相同紙張尺寸")
    with plan.backend.open(plan.printer, plan.duplex, points) as job:
        job.start(f"fuelcore 報單 {batch.ids[0]}")
        for page in pages:
            job.start_page(page)
            draw_fields(job, page)
            job.end_page()
        job.finish()
        for warning in dict.fromkeys([*batch.warnings, *job.warnings]):
            notify(f"提醒：{warning}")
    return True
