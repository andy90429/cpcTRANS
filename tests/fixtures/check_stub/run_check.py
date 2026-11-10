import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--sersno", nargs="+", required=True)
parser.add_argument("--db-json", type=Path, required=True)
parser.add_argument("--save", action="store_true")
args = parser.parse_args()
with args.db_json.open(encoding="utf-8") as stream:
    config = json.load(stream)
    assert config["test_only"] is True
Path("started.json").write_text(json.dumps({"path": str(args.db_json), "save": args.save,
                                            "ids": args.sersno}), encoding="utf-8")
results = []
for number in reversed(args.sersno):
    case = number[-1]
    if case == "4":
        time.sleep(30)
    if case == "5":
        sys.stdout.write("{")
        sys.exit(0)
    if case == "6":
        number = "TEST00A10E9999"
    if args.save:
        path = Path(config["path"]).resolve()
        assert config["driver"] == "sqlite" and path.stem == "fuelcore_test"
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db, db:
            passed = case not in ("2", "3")
            db.execute("UPDATE cusdec SET `原狀態` = STATUS, STATUS = ?, `是否已邏輯` = ? "
                       "WHERE MSGCODE = ? AND SERSNO = ?",
                       ("Y" if passed else "0", int(passed), "5203S", number))
    results.append(dict(sersno=number, status="failed" if case == "2" else "passed",
                        rules_total=87, rules_unparsed=int(case == "3"),
                        basic_passed=True, item_passed=case != "2", saved=args.save,
                        messages=[dict(level="error", rule_id="stub", field="數量", item=1,
                                       text="測試結果")] if case == "2" else []))
sys.stdout.buffer.write(json.dumps(dict(plugin_version="fake", results=results), ensure_ascii=False).encode("utf-8"))
