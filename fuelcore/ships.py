"""Local ship directory. Loading never connects to a database or the network."""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile


SHIP_FILENAME = "fuelcore_ships.json"
CORRUPT_WARNING = "船舶清單無法讀取，已使用空清單。"


@dataclass(frozen=True)
class Ship:
    name: str
    imo: str = ""
    call_sign: str = ""
    buyer_id: str = ""
    agent: str = ""


def valid_imo(imo):
    return (isinstance(imo, str) and re.fullmatch(r"[0-9]{7}", imo) is not None
            and sum(int(digit) * weight for digit, weight in zip(imo[:6], range(7, 1, -1))) % 10 == int(imo[-1]))


def validate_ship(ship):
    values = asdict(ship)
    if any(not isinstance(value, str) for value in values.values()):
        raise ValueError("船舶清單的欄位須為文字。")
    ship = Ship(**{key: value.strip() for key, value in values.items()})
    if not ship.name:
        raise ValueError("船名不可空白。")
    if ship.imo and not valid_imo(ship.imo):
        raise ValueError("IMO 須為 7 位數字，且檢查碼須正確。")
    if any("|" in value or (value and value.splitlines() != [value])
           for value in (ship.name, ship.call_sign, ship.buyer_id)):
        raise ValueError("船名、呼號及買方編號不可含 TXT 分隔符號或換行。")
    return ship


def validate_ships(ships):
    result, names, imos = [], set(), set()
    for entry in ships:
        ship = validate_ship(entry)
        name = ship.name.casefold()
        if name in names:
            raise ValueError(f"船名重複：{ship.name}")
        if ship.imo and ship.imo in imos:
            raise ValueError(f"IMO 重複：{ship.imo}")
        result.append(ship)
        names.add(name)
        if ship.imo:
            imos.add(ship.imo)
    return tuple(result)


def read_bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def decode_ships(data):
    if data is None:
        return ()
    rows = json.loads(data.decode("utf-8-sig"))
    if not isinstance(rows, list):
        raise ValueError("船舶清單須為 JSON 陣列。")
    return validate_ships(Ship(**row) for row in rows)


def load_ships(path):
    try:
        return decode_ships(read_bytes(path)), ""
    except (OSError, TypeError, ValueError, RecursionError):
        return (), CORRUPT_WARNING


def change_ship(path, replacement=None, *, original=None):
    """Insert, replace or delete a row, validating the latest local file."""
    before = read_bytes(path)
    try:
        ships = list(decode_ships(before))
    except (TypeError, ValueError, RecursionError):
        raise ValueError("船舶清單已損壞。請先備份並移開原檔，再新增資料。") from None
    if original is None:
        ships.append(replacement)
    else:
        if original not in ships:
            raise ValueError("船舶清單已更新，請重新選取船舶。")
        index = ships.index(original)
        if replacement is None:
            del ships[index]
        else:
            ships[index] = replacement
    updated = validate_ships(ships)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
                                suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump([asdict(ship) for ship in updated], stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        if read_bytes(path) != before:
            raise ValueError("船舶清單已更新，請重新載入再修改。")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return updated
