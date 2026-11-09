import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from fuelcore import Settings, convert_files
from fuelcore.schema import SCHEMA, init_sqlite, statements


def main():
    if sys._xoptions.get("fuelcore_gui"):
        folder = Path(sys.executable).parent
        if len(sys.argv) > 1:
            return forward_cli(folder)
        from fuelcore.gui import run
        return run(folder / "fuelcore.ini")
    prog = Path(sys.executable).name if getattr(sys, "frozen", False) else "python -m fuelcore"
    parser = argparse.ArgumentParser(prog=prog)
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser("convert")
    convert.add_argument("txt", nargs="+", type=Path)
    convert.add_argument("--config", "--db", required=True)
    schema = commands.add_parser("schema", help="建立 SQLite 或輸出 MySQL 測試建表 SQL")
    schema.add_argument("--config", required=True)
    schema.add_argument("--csv", type=Path, default=SCHEMA)
    schema.add_argument("--output", type=Path)
    simulate = commands.add_parser("simulate-send", help="在測試庫以唯讀方式規劃送信")
    simulate.add_argument("sersno", nargs="+")
    simulate.add_argument("--config", required=True)
    simulate.add_argument("--output", type=Path, required=True)
    for command, help_text in (("simulate-receive", "以唯讀方式規劃收信"),
                               ("import-replies", "將回覆匯入測試庫")):
        receive = commands.add_parser(command, help=help_text)
        receive.add_argument("source", nargs="+", type=Path)
        receive.add_argument("--config", default="fuelcore.ini")
        if command == "simulate-receive":
            receive.add_argument("--output", type=Path, default=Path("."))
    report = commands.add_parser("render-report", help="產生唯讀報單 Rpx、PDF 與逐頁 PNG")
    report.add_argument("sersno", nargs="+")
    report.add_argument("--config", required=True)
    report.add_argument("--output", type=Path, required=True)
    print_cmd = commands.add_parser("print-report", help="以 Windows GDI 直接列印報單")
    print_cmd.add_argument("sersno", nargs="+")
    print_cmd.add_argument("--config", required=True)
    print_cmd.add_argument("--yes", action="store_true", help="略過送印確認")
    commands.add_parser("list-printers", help="列出 Windows 已安裝的印表機")
    args = parser.parse_args()
    if args.command == "list-printers":
        from fuelcore.report.printing import list_printers
        from fuelcore.report.rpx import RpxError
        try:
            for name in list_printers():
                print(name)
        except (RpxError, OSError) as error:
            print(error, file=sys.stderr)
            return 1
        return 0
    settings = Settings.load(args.config)
    if args.command == "print-report":
        from fuelcore.report.printing import print_report
        from fuelcore.report.rpx import RpxError
        try:
            if print_report(settings, args.sersno, yes=args.yes):
                print("已送出列印工作。")
        except (RpxError, OSError) as error:
            print(error, file=sys.stderr)
            return 1
        return 0
    if args.command == "render-report":
        from fuelcore.report import render_report
        from fuelcore.report.rpx import RpxError
        try:
            result = render_report(settings, args.sersno, args.output)
        except (RpxError, OSError) as error:
            print(error, file=sys.stderr)
            return 1
        for path in [result.rpx, result.pdf, *result.previews, result.comparison]:
            if path:
                print(path)
        for warning in result.warnings:
            print(f"提醒：{warning}", file=sys.stderr)
        return 0
    if args.command == "simulate-send":
        from fuelcore.edi import simulate_send
        result = simulate_send(settings, args.sersno, args.output)
        print(result.directory / "模擬結果.txt")
        return int(bool(result.rejected))
    if args.command in ("simulate-receive", "import-replies"):
        from fuelcore.edi import import_replies, simulate_receive
        if args.command == "simulate-receive":
            report, results = simulate_receive(settings, args.source, args.output)
            print(report)
        else:
            results = import_replies(settings, args.source)
        for result in results:
            print(f"{result.status} {result.file.name} {result.reference or result.message}".rstrip())
        return int(any(result.status == "失敗" for result in results))
    if args.command == "schema":
        if settings.driver == "sqlite":
            init_sqlite(settings, args.csv)
        else:
            sql = ";\n".join(statements(settings, args.csv)) + ";\n"
            if args.output:
                args.output.write_text(sql, encoding="utf-8")
            else:
                print(sql, end="")
        return 0
    return run_conversion(args.txt, settings)


def forward_cli(folder):
    import ctypes
    import subprocess

    # The windowed entry point has no console. Reuse the caller's console or pipes.
    ctypes.windll.kernel32.AttachConsole(-1)
    command = [str(folder / "fuelcore_cli.exe"), *sys.argv[1:]]
    return subprocess.call(command, close_fds=False)


def run_conversion(paths, settings):
    results = convert_files(paths, settings)
    for result in results:
        print(json.dumps(asdict(result), ensure_ascii=False))
    failures = sum(not result.ok for result in results)
    print(f"成功 {len(results) - failures}，失敗 {failures}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
