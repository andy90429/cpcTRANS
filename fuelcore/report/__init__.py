import os
import re
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

from ..db import Database
from .declaration import DeclarationReader, ReportAssets, build_page
from .rpx import RpxDocument, RpxError


@dataclass(frozen=True)
class ReportResult:
    rpx: Path
    pdf: Path
    previews: list
    warnings: list
    comparison: Path = None


def safe_destination(path, assets):
    path = Path(path).resolve()
    if path == assets.root or assets.root in path.parents:
        raise RpxError("輸出和快取必須放在 CBS 報表資料夾外")
    return path


def display_values(page):
    values = {}
    for field in sorted(page.children, key=lambda node: (node.number("Top"), node.number("Left"))):
        if not field.boolean("Visible", True) or not field.boolean("isVisible", True) or field.get("Caption") is None:
            continue
        expression = field.text("Field") or field.text("PrtConditions")
        key = re.sub(r"\s+", "", expression).upper()
        direct = re.fullmatch(r"\[(?:CUSDEC|INVITEM|項次_INV主檔|PERMITNO[12]?|SUMSTC|SUMEXP)\.([^\]]+)\]", key)
        if direct:
            key = direct[1]
        if not key:
            key = f"固定文字@{field.number('Left')},{field.number('Top')}"
        values.setdefault(key, []).append(field.text("Caption"))
    return values


def reference_comparison(assets, reports, warnings):
    refs = assets.root / "參考輸出"
    if not refs.is_dir() or assets.root not in refs.resolve().parents:
        return []
    lines = []
    for path in sorted(refs.rglob("*")):
        if path.suffix.lower() != ".rpx" or not path.is_file() or assets.root not in path.resolve().parents:
            continue
        try:
            document = RpxDocument.read(path.read_bytes())
        except (OSError, RpxError) as error:
            warnings.append(f"無法讀取參考 Rpx {path.name}：{error}")
            continue
        for reference in (page for page in document.roots if page.class_name == "TGPage"):
            title = reference.text("PageTitle")
            candidates = set(re.findall(r"\b[A-Z0-9]{14}\b", title, re.I))
            for field in reference.children:
                formula = (field.text("Field") or field.text("PrtConditions")).upper()
                if formula in {"SERSNO", "[CUSDEC.SERSNO]"}:
                    candidates.add(re.sub(r"[^A-Za-z0-9]", "", field.text("Caption")))
            for sersno in reports.keys() & {candidate.upper() for candidate in candidates}:
                old, new = display_values(reference), display_values(reports[sersno])
                lines.append(f"{sersno} · {path.name}")
                changes = [key for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)]
                lines.extend(f"{key} | 舊 {old.get(key, [])!r} | 新 {new.get(key, [])!r}" for key in changes)
                lines.append(f"差異欄位組數 {len(changes)}")
    return lines


def render_report(settings, sersnos, output, *, reports=None, cache=None, connection=None):
    try:
        import pypdfium2  # noqa: F401  Check the PDF preview runtime before creating any outputs.
        from .pdf import preview_pngs, render_pdf
    except ImportError as error:
        raise RpxError("請先安裝列印相依套件：python -m pip install .[report]") from error

    reports = reports or os.environ.get("FUELCORE_CBS_REPORTS")
    if not reports:
        raise RpxError("請將 FUELCORE_CBS_REPORTS 指向本機 CBS 報表資料夾")
    assets = ReportAssets(reports)
    ids = [sersno.strip().upper() for sersno in sersnos]
    if not ids or any(not re.fullmatch(r"[A-Z0-9]{14}", sersno) for sersno in ids):
        raise RpxError("請提供 14 碼英數報單號碼")
    output = safe_destination(output, assets)
    cache = cache or os.environ.get("FUELCORE_REPORT_CACHE") or (
        Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "fuelcore" / "report-backgrounds")
    cache = safe_destination(cache, assets)
    front, back = assets.page("N5203.Rpt"), assets.page("N5203CUS.Rpt")
    pages, fronts, warnings = [], [], []
    with closing(Database(settings, connection, read_only=True)) as db:
        reader = DeclarationReader(db)
        for sersno in ids:
            tables = reader.read(sersno)
            front_page = build_page(front, tables, warnings)
            front_page.set_text("PageTitle", f"報單編號: {sersno}")
            back_page = build_page(back, tables, warnings)
            pages.extend(((front_page, False), (back_page, True)))
            fronts.append(front_page)
    pdf_bytes = render_pdf(pages, assets, cache, warnings)
    rpx_bytes = RpxDocument.create(f"出口報單 {ids[0]}", fronts).to_bytes()
    differences = reference_comparison(assets, dict(zip(ids, fronts)), warnings)

    output.mkdir(parents=True, exist_ok=True)
    folder = Path(mkdtemp(prefix=f"報單_{ids[0]}_", dir=output))
    rpx, pdf = folder / "報單.Rpx", folder / "報單.pdf"
    rpx.write_bytes(rpx_bytes)
    pdf.write_bytes(pdf_bytes)
    previews = preview_pngs(pdf_bytes, folder)
    comparison = None
    if differences:
        comparison = folder / "比對差異.txt"
        comparison.write_bytes(("\r\n".join(differences) + "\r\n").encode("utf-8-sig"))
    return ReportResult(rpx, pdf, previews, list(dict.fromkeys(warnings)), comparison)
