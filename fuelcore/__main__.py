import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from fuelcore import Settings, convert_files
from fuelcore.schema import SCHEMA, init_sqlite, statements


def main():
    prog = "fuelcore.exe" if getattr(sys, "frozen", False) else "python -m fuelcore"
    parser = argparse.ArgumentParser(prog=prog)
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser("convert")
    convert.add_argument("txt", nargs="+", type=Path)
    convert.add_argument("--config", "--db", required=True)
    schema = commands.add_parser("schema", help="建立 SQLite 或輸出 MySQL 測試建表 SQL")
    schema.add_argument("--config", required=True)
    schema.add_argument("--csv", type=Path, default=SCHEMA)
    schema.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = Settings.load(args.config)
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
    results = convert_files(args.txt, settings)
    for result in results:
        print(json.dumps(asdict(result), ensure_ascii=False))
    failures = sum(not result.ok for result in results)
    print(f"成功 {len(results) - failures}，失敗 {failures}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
