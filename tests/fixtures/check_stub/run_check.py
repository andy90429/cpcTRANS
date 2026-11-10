import argparse
import json
import sys
import time
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--sersno", nargs="+", required=True)
parser.add_argument("--db-json", type=Path, required=True)
parser.add_argument("--save", action="store_true")
args = parser.parse_args()
with args.db_json.open(encoding="utf-8") as stream:
    assert json.load(stream)["test_only"] is True
Path("started.json").write_text(json.dumps({"path": str(args.db_json), "save": args.save}), encoding="utf-8")
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
    results.append(dict(sersno=number, status="failed" if case == "2" else "passed",
                        rules_total=87, rules_unparsed=int(case == "3"),
                        basic_passed=True, item_passed=case != "2", saved=args.save,
                        messages=[dict(level="error", rule_id="stub", field="數量", item=1,
                                       text="測試結果")] if case == "2" else []))
sys.stdout.buffer.write(json.dumps(dict(plugin_version="fake", results=results), ensure_ascii=False).encode("utf-8"))
