from .ships import load_ships


RESTART = "CBS 已開著的話要整個關掉重開才看得到這艘船"
F2_PREFIX = "呼號（F2）："
SHIP_ADDED = F2_PREFIX + "新增船舶呼號 "
SHIP_BATCH_REMINDER = "這次有新增船舶呼號，CBS 開著的話請整個關掉重開"


def has_new_ship(notes):
    return any(note.startswith(SHIP_ADDED) for note in notes)


def buyer(db, number, name, code, write, notes, *, country, destination):
    table = db.table("mybuyer")
    lock = " FOR UPDATE" if db.mysql and write else ""
    rows = db.select(f"SELECT BY015 FROM {table} WHERE BY002 = ?" + lock, (number,))
    if rows:
        old = (rows[0][0] or "").strip()
        if old.upper() in ("", ".", "-", "NIL"):
            if write:
                db.execute(f"UPDATE {table} SET BY015 = ? WHERE BY002 = ?", (code, number))
        elif old != code:
            notes.append(f"買方 {number} 代碼表為 {old}，本次使用 {code}")
    elif write:
        db.insert("mybuyer", dict(BY002=number, BY003=name, BY012=country[0], BY013=country[1],
                                 BY015=code, BY017=destination[0], BY018=destination[1]))


def normalize_call(value):
    return "".join((value or "").split()).upper()


def ship_call(db, call, imo, name, notes, *, ships_path=None, raw_name=None):
    if call:
        return call
    if ships_path is not None:
        ships, warning = load_ships(ships_path)
        if warning:
            notes.append(warning)
        local_name = (raw_name if raw_name is not None else name).strip()
        for field, value, label in (("imo", imo.strip(), "IMO"), ("name", local_name, "船名")):
            match = next((ship for ship in ships if getattr(ship, field).casefold() == value.casefold()), None)
            if match is not None:
                resolved = normalize_call(match.call_sign)
                notes.append(f"自動填入呼號 {resolved}（依船舶資料 {label} {value}）")
                return resolved
    table = db.table("myshipname")
    for column, value, label in (("SN004", imo, "IMO"), ("SN003", name[:40], "船名")):
        if not value.strip():
            continue
        rows = db.select(f"SELECT SN002 FROM {table} WHERE UPPER(TRIM({column})) = ?",
                         (value.strip().upper(),))
        matches = {normalize_call(row[0]) for row in rows}
        matches.discard("")
        if len(matches) == 1:
            resolved = matches.pop()
            notes.append(f"自動填入呼號 {resolved}（依船舶代碼表 {label} {value.strip()}）")
            return resolved
    notes.append("請手動填呼號")
    return ""


def write_ship(db, call, imo, name, notes):
    table = db.table("myshipname")
    lock = " FOR UPDATE" if db.mysql else ""
    rows = db.select(f"SELECT SN001, SN003, SN004 FROM {table} WHERE UPPER(TRIM(SN002)) = ?" + lock,
                     (call,))
    same = [row for row in rows if (row[1] or "").strip().upper() == name.strip().upper()]
    unnamed = [row for row in rows if not (row[1] or "").strip()]
    if len(same) > 1 or (not same and len(unnamed) > 1):
        raise ValueError("船舶代碼表有多筆候選")
    if same or unnamed:
        key, existing_name, existing_imo = (same or unnamed)[0]
        changes = []
        if not (existing_imo or "").strip() and imo:
            db.execute(f"UPDATE {table} SET SN004 = ? WHERE SN001 = ?", (imo, key))
            changes.append(f"補 IMO {imo}")
        elif imo and (existing_imo or "").strip() != imo:
            notes.append(f"呼號 {call} 的代碼表 IMO 為 {existing_imo}，本次為 {imo}")
        if not same and name:
            db.execute(f"UPDATE {table} SET SN003 = ? WHERE SN001 = ?", (name, key))
            changes.extend((f"補船名 {name}", RESTART))
        if not changes:
            changes.append("CBS 代碼檔已有，不用新增")
        notes.append(f"{F2_PREFIX}呼號 {call}，" + "；".join(changes))
    else:
        existing_names = sorted({str(row[1]).strip() for row in rows if (row[1] or "").strip()}, key=str.casefold)
        key = db.insert("myshipname", dict(SN002=call, SN003=name, SN004=imo))
        rows = db.select(f"SELECT SN002, SN003, SN004 FROM {table} WHERE SN001 = ?", (key,))
        if list(rows) != [(call, name, imo)]:
            raise ValueError("新增後讀取確認失敗")
        previous = f"；代碼檔原有同呼號：{'、'.join(existing_names)}" if existing_names else ""
        notes.append(f"{SHIP_ADDED}{call} {name}，已確認寫入代碼檔（流水號 {key}）{previous}；{RESTART}")


def maintain_ship(db, raw_call, imo, name, write, notes, *, ships_path=None, raw_name=None):
    call = normalize_call(raw_call)
    try:
        call = ship_call(db, call, imo, name, notes, ships_path=ships_path, raw_name=raw_name)
    except Exception as error:
        notes.append(f"查詢船舶代碼表失敗：{error}，請手動填呼號")
    if not call:
        notes.append(f"{F2_PREFIX}找不到呼號，請手動填呼號")
    elif len(call) > 10:
        notes.append(f"{F2_PREFIX}呼號 {call} 超過 10 碼，略過船舶代碼表")
    elif not write:
        notes.append(f"{F2_PREFIX}呼號 {call}，代碼檔寫回已停用")
    else:
        pending = []
        if len(name) > 40:
            pending.append("myshipname.SN003 超過 40 字，已截斷")
        if len(imo) > 25:
            pending.append("myshipname.SN004 超過 25 字，已截斷")
        db.execute("SAVEPOINT ship")
        try:
            write_ship(db, call, imo[:25], name[:40], pending)
        except Exception as error:
            db.execute("ROLLBACK TO SAVEPOINT ship")
            notes.append(f"{F2_PREFIX}呼號 {call}，船舶代碼表處理失敗：{error}")
        else:
            notes.extend(pending)
        db.execute("RELEASE SAVEPOINT ship")
    return call
