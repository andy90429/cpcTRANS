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
    for name, help_text in (("rate-simulate", "比對每旬匯率並產生唯讀報告"),
                            ("rate-import", "確認後寫入每旬匯率測試庫")):
        rate = commands.add_parser(name, help=help_text)
        rate.add_argument("--config", default="fuelcore.ini")
        rate.add_argument("--from-files", nargs="+", type=Path, metavar="FILE")
        if name == "rate-simulate":
            rate.add_argument("--output", type=Path, required=True)
        else:
            rate.add_argument("--yes", action="store_true", help="確認寫入測試庫")
    cert = commands.add_parser("cert-check", help="在 Windows 本機檢查 PFX 憑證")
    cert.add_argument("pfx")
    cert.add_argument("--recorded-expiry", metavar="YYYY-MM-DD")
    check = commands.add_parser("check", help="用本機外掛執行 CBS 邏輯檢查")
    check.add_argument("sersno", nargs="+")
    check.add_argument("--config", default="fuelcore.ini")
    check.add_argument("--save", action="store_true", help="將結果寫入測試庫")
    check.add_argument("--output", type=Path, help="另存 JSON 結果")
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
    agent = commands.add_parser("agent", help="依船名查詢臺中港的呼號及代理行")
    agent.add_argument("--name", required=True)
    agent.add_argument("--mapping", type=Path)
    agent.add_argument("--shorts", type=Path)
    args = parser.parse_args()
    if args.command in ("rate-simulate", "rate-import"):
        from fuelcore.declare_rate import run_rate_command
        return run_rate_command(args)
    if args.command == "cert-check":
        from fuelcore.cert_check import run_cert_command
        return run_cert_command(args)
    if args.command == "agent":
        return run_agent(args)
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
    if args.command == "check":
        return run_check_command(args)
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


def run_check_command(args):
    from fuelcore.logic_check import run_logic_check

    if args.output and args.output.suffix.casefold() != ".json":
        print("--output 請指定 .json 檔案", file=sys.stderr)
        return 2
    try:
        settings = Settings.load(args.config)
    except (ValueError, OSError, KeyError):
        print("無法讀取 fuelcore 測試庫設定。請核對 test_only 與資料庫名稱。", file=sys.stderr)
        return 2
    run = run_logic_check(args.sersno, settings, save=args.save)
    for result in run.results:
        saved = "，已寫回測試庫" if result.saved else ""
        print(f"{result.sersno} {result.label}（執行 {result.rules_total} 條，無法檢查 {result.rules_unparsed} 條{saved}）")
        for message in result.messages:
            location = " ".join(part for part in (message.rule_id, message.field,
                                                  f"第 {message.item} 項" if message.item else "") if part)
            print(f"  [{message.level}] {location + ' · ' if location else ''}{message.text}")
    if args.output:
        try:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(run.to_json() + "\n")
        except OSError:
            print("無法建立 JSON 結果檔，請指定可寫入的新檔名。", file=sys.stderr)
            return 2
    return int(not run.results or any(not result.passed for result in run.results))


def run_conversion(paths, settings):
    results = convert_files(paths, settings)
    for result in results:
        print(json.dumps(asdict(result), ensure_ascii=False))
    failures = sum(not result.ok for result in results)
    print(f"成功 {len(results) - failures}，失敗 {failures}")
    return int(bool(failures))


def run_agent(args):
    from fuelcore.agent_lookup import DEFAULT_DIR, DEFAULT_MAPPING, AgentLookupError, lookup_ship, resolve_short

    try:
        result = lookup_ship(args.name)
        print(f"狀態：{result.status}")
        if result.status == "not_found":
            print("找不到")
            return 1
        if result.status == "ambiguous":
            print("有多筆同名船")
            for ship in result.candidates:
                print(f"{ship.ship_name} {ship.ship_name_zh}｜{ship.agent_full or '無代理行資料'}"
                      f"｜碼頭 {ship.berth or '—'}｜ETA {ship.eta or '—'}")
            return 1
        print(f"{result.ship_name}｜呼號 {result.call_sign or '—'}｜代理行 {result.agent_full or '—'}"
              f"｜碼頭 {result.berth or '—'}｜ETA {result.eta or '—'}")
        if args.mapping is None and args.shorts is None:
            return int(result.status != "found")
        shorts_path = args.shorts or DEFAULT_DIR / "shorts"
        if shorts_path.is_dir():
            shorts = sorted(path.stem for path in shorts_path.iterdir()
                            if path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp"))
        elif shorts_path.is_file():
            shorts = shorts_path.read_text(encoding="utf-8-sig").splitlines()
        else:
            shorts = []
        short = resolve_short(result.agent_full, shorts, args.mapping or DEFAULT_MAPPING)
        if short.short:
            print(f"簡稱：{short.short}")
        else:
            print("需要使用者選" + ("：" + "、".join(short.candidates) if short.candidates else ""))
        return int(result.status != "found")
    except (AgentLookupError, OSError, ValueError) as error:
        print(f"代理行查詢失敗：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
