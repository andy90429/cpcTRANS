RESTART = "CBS 已開著的話要整個關掉重開才看得到這艘船"


def buyer(db, number, name, code, write, notes):
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
        db.insert("mybuyer", dict(BY002=number, BY003=name, BY015=code))


def normalize_call(value):
    return "".join((value or "").split()).upper()


def ship_call(db, call, imo, name, notes):
    if call:
        return call
    table = db.table("myshipname")
    for column, value in (("SN004", imo), ("SN003", name[:40])):
        if not value.strip():
            continue
        rows = db.select(f"SELECT SN002 FROM {table} WHERE UPPER(TRIM({column})) = ?",
                         (value.strip().upper(),))
        matches = {normalize_call(row[0]) for row in rows}
        matches.discard("")
        if len(matches) == 1:
            return matches.pop()
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
        if not (existing_imo or "").strip() and imo:
            db.execute(f"UPDATE {table} SET SN004 = ? WHERE SN001 = ?", (imo, key))
        elif imo and (existing_imo or "").strip() != imo:
            notes.append(f"呼號 {call} 的代碼表 IMO 為 {existing_imo}，本次為 {imo}")
        if not same and name:
            db.execute(f"UPDATE {table} SET SN003 = ? WHERE SN001 = ?", (name, key))
            notes.append(f"呼號 {call} 補船名 {name}；{RESTART}")
    else:
        key = db.insert("myshipname", dict(SN002=call, SN003=name, SN004=imo))
        rows = db.select(f"SELECT SN002, SN003, SN004 FROM {table} WHERE SN001 = ?", (key,))
        if list(rows) != [(call, name, imo)]:
            raise ValueError("新增後讀取確認失敗")
        notes.append(f"呼號（F2）：新增船舶呼號 {call} {name}，已確認寫入代碼檔（流水號 {key}）；{RESTART}")


def maintain_ship(db, raw_call, imo, name, write, notes):
    call = normalize_call(raw_call)
    try:
        call = ship_call(db, call, imo, name, notes)
    except Exception as error:
        notes.append(f"查詢船舶代碼表失敗：{error}，請手動填呼號")
    if len(call) > 10:
        notes.append(f"呼號 {call} 超過 10 碼，略過船舶代碼表")
    elif call and write:
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
            notes.append(f"船舶代碼表處理失敗：{error}")
        else:
            notes.extend(pending)
        db.execute("RELEASE SAVEPOINT ship")
    return call
