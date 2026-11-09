import json
import re
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp

from ..db import Database
from ..schema import identifier
from .reply import REPLY_CODES, ReplyRow, apply_defaults, convert_reply, reply_code
from .simulation import day_sequence
from .xml import EdiError, XmlSettings, row_text, upper_keys


@dataclass(frozen=True)
class ReceiveResult:
    file: Path
    status: str
    message: str = ""
    reference: str = ""


@dataclass(frozen=True)
class ReceivePlan:
    reply: ReplyRow
    sequence: int
    reference: str
    message_code: str
    receive_record: dict
    cusdec_changes: dict


def input_files(sources):
    if isinstance(sources, (Path, str)):
        sources = (sources,)
    paths = set()
    for source in sources:
        path = Path(source).resolve()
        if path.is_dir():
            paths.update(child for child in path.iterdir() if child.is_file())
        else:
            paths.add(path)
    return sorted(paths, key=lambda path: (path.name.casefold(), str(path)))


def all_rows(row):
    yield row
    yield from row.children
    for child in row.children:
        yield from child.children


def sql_fields(fields):
    return {key: value if value != "" else None for key, value in fields.items()}


def validate_reply_rows(reply):
    for row in all_rows(reply):
        if not re.fullmatch(fr"{reply.table}(?:_[0-9]+)?", row.table):
            raise EdiError(f"回覆設定指向不支援的資料表 {row.table}")
        if {key.upper() for key in row.fields} & {"MAINID", "SUBID", "PID"}:
            raise EdiError("回覆欄位不可直接指定關聯識別碼")
    if any(grandchild.children for child in reply.children for grandchild in child.children):
        raise EdiError("回覆只支援兩層子表")


class Receiver:
    def __init__(self, db, now):
        self.db = db
        self.now = now
        self.settings = db.settings
        self.edi_schema = self.settings.edi.edi_database
        self.data_schema = getattr(self.settings, "database", None)
        self.tables = db.table_names(self.edi_schema)
        self.data_tables = db.table_names(self.data_schema) if db.mysql else self.tables
        self.config = {}

    def name(self, table, *, declaration=False):
        names = self.data_tables if declaration else self.tables
        if table.casefold() not in names:
            schema = self.data_schema if declaration else self.edi_schema
            raise EdiError(f"測試庫缺少資料表 {schema + '.' if schema else ''}{table}")
        return names[table.casefold()]

    def table(self, table, *, declaration=False):
        schema = self.data_schema if declaration else self.edi_schema
        return self.db.table(self.name(table, declaration=declaration), schema)

    def parse(self, code, data, filename):
        if code not in self.config:
            self.config[code] = XmlSettings.load(self.db, code=code)
        return convert_reply(self.config[code], data, filename)

    def sequence(self, *, lock=False):
        suffix = " FOR UPDATE" if lock and self.db.mysql else ""
        rows = self.db.select_dicts(f"SELECT InputDate, InputSequence FROM {self.table('sequence')} LIMIT 2{suffix}")
        if len(rows) > 1 or (self.db.mysql and not rows):
            raise EdiError(f"{self.edi_schema}.sequence 必須有唯一一筆日序號資料")
        current = day_sequence(upper_keys(rows[0]) if rows else {}, self.now.date(), "Input")
        return bool(rows), current

    def already_imported(self, code, filename):
        return bool(self.db.select(f"SELECT 1 FROM {self.table(code)} WHERE LOWER(`接收訊息來源`) = LOWER(?) LIMIT 1",
                                   (filename,)))

    def plan(self, reply, path, size, sequence):
        if sequence > 9999:
            raise EdiError("已用完當日 4 碼收信序號")
        apply_defaults(reply, self.config[reply.table].defaults, path.name, self.now, sequence)
        validate_reply_rows(reply)
        fields = reply.fields
        reference = f"{self.now:%Y%m%d}{sequence:04d}"
        received = self.now.strftime("%Y-%m-%d %H:%M:%S")
        required = {"接收訊息來源": path.name, "訊息參考號碼": reference, "接收日期時間": received}
        if any(fields.get(key) != value for key, value in required.items()):
            raise EdiError("回覆設定缺少或未正確產生檔名、參考號碼或接收日期時間")
        sersno = fields["報單號碼"]
        transport = {"1": "S", "4": "A"}.get(fields.get("海空運別"), "")
        message_code = "5203" + transport if transport else ""
        changes, declaration = {}, {}
        if reply.table == "N5204":
            if not transport:
                raise EdiError("N5204 缺少可辨識的海空運別")
            decl_table = self.table("cusdec", declaration=True)
            lock = " FOR UPDATE" if self.db.mysql and not self.db.read_only else ""
            matches = self.db.select_dicts(f"SELECT SENDID, RECVID FROM {decl_table} WHERE SERSNO = ? AND MSGCODE = ?{lock}",
                                           (sersno, message_code))
            if len(matches) != 1:
                raise EdiError(f"找不到唯一的對應報單 {sersno}（{message_code}）")
            declaration = upper_keys(matches[0])
            release = fields.get("放行日期時間")
            changes = {"STATUS": "P", "UPDATE": "*", "CUSISCODE": fields.get("通關方式") or None,
                       "CUSISDATE": datetime.fromisoformat(release).strftime("%Y-%m-%d %H:%M:%S") if release else None,
                       "RECVDATE": received, "RECVMSGCODE": "N5204", "MCTLNO": reference}
        for row in all_rows(reply):
            self.name(row.table)
        self.name("RcvMsgRcds")
        history = {}
        if message_code and "sendmsgrcds" in self.tables:
            records = self.db.select_dicts(f"SELECT * FROM {self.table('SendMsgRcds')} "
                                           "WHERE `報單號碼` = ? AND `訊息簡稱` = ? ORDER BY `識別碼` DESC LIMIT 1",
                                           (sersno, message_code))
            history = records[0] if records else {}
        receive_record = {
            "郵箱號碼": row_text(history, "郵箱號碼"), "報單號碼": sersno, "訊息簡稱": reply.table,
            "接收時間": received, "訊息參考號碼": reference,
            "傳送方代號": row_text(history, "傳送方代號") or row_text(declaration, "SENDID"),
            "接收方代號": row_text(history, "接收方代號") or row_text(declaration, "RECVID"),
            "備份檔名": str(path), "檔案位元數": size, "User": self.settings.edi.user,
        }
        return ReceivePlan(reply, sequence, reference, message_code, receive_record, changes)

    def write(self, plan, sequence_exists):
        reply = plan.reply
        tables = {(self.edi_schema, self.name(row.table)) for row in all_rows(reply)}
        tables.update((self.edi_schema, self.name(table)) for table in ("sequence", "RcvMsgRcds"))
        if plan.cusdec_changes:
            tables.add((self.data_schema, self.name("cusdec", declaration=True)))
        self.db.require_transactional_tables(tables)

        def insert(row, fields):
            return self.db.insert(self.name(row), sql_fields(fields), schema=self.edi_schema)

        main_id = insert(reply.table, reply.fields)
        if not main_id:
            raise EdiError(f"{reply.table} 未取得 MainID")
        first_level = []
        for row in reply.children:
            sub_id = insert(row.table, {**row.fields, "MainID": main_id})
            if not sub_id:
                raise EdiError(f"{row.table} 未取得 SubID")
            first_level.append((row, sub_id))
        for row, sub_id in first_level:
            for child in row.children:
                insert(child.table, {**child.fields, "MainID": main_id, "SubID": sub_id})
        insert("RcvMsgRcds", plan.receive_record)
        if plan.cusdec_changes:
            columns = ", ".join(f"{identifier(key)} = ?" for key in plan.cusdec_changes)
            self.db.execute(f"UPDATE {self.table('cusdec', declaration=True)} SET {columns} WHERE SERSNO = ? AND MSGCODE = ?",
                            (*plan.cusdec_changes.values(), reply.fields["報單號碼"], plan.message_code))
        sequence = {"InputDate": self.now.strftime("%Y-%m-%d 00:00:00"), "InputSequence": plan.sequence}
        if sequence_exists:
            self.db.execute(f"UPDATE {self.table('sequence')} SET InputDate = ?, InputSequence = ?", tuple(sequence.values()))
        else:
            insert("sequence", sequence)


def receive_batch(settings, sources, now, connection, *, simulate):
    if settings.edi is None:
        raise EdiError("fuelcore.ini 缺少 [edi] 設定")
    results, plans = [], []
    sequence = None
    seen = set()
    with closing(Database(settings, connection, read_only=simulate)) as db:
        receiver = Receiver(db, now)
        for path in input_files(sources):
            code = reply_code(path.name)
            if code not in REPLY_CODES:
                results.append(ReceiveResult(path, "略過", f"不支援的訊息格式 {code or path.name}"))
                continue
            try:
                data = path.read_bytes()
                reply = receiver.parse(code, data, path.name)
                if not simulate:
                    db.begin()
                try:
                    if not simulate or sequence is None:
                        exists, sequence = receiver.sequence(lock=not simulate)
                    duplicate = (code, path.name.casefold()) in seen or receiver.already_imported(code, path.name)
                    if duplicate:
                        result = ReceiveResult(path, "略過", "相同回覆檔名已匯入")
                    else:
                        plan = receiver.plan(reply, path, len(data), sequence + 1)
                        if not simulate:
                            receiver.write(plan, exists)
                        result = ReceiveResult(path, "規劃" if simulate else "已匯入", reference=plan.reference)
                    if not simulate:
                        db.commit()
                except BaseException:
                    if not simulate:
                        db.rollback()
                    raise
            except Exception as error:
                results.append(ReceiveResult(path, "失敗", str(error)))
            else:
                results.append(result)
                if not duplicate:
                    seen.add((code, path.name.casefold()))
                    sequence = plan.sequence
                    if simulate:
                        plans.append((path, plan))
    return results, plans


def import_replies(settings, sources, *, now=None, connection=None):
    results, _ = receive_batch(settings, sources, now or datetime.now(), connection, simulate=False)
    return results


def simulate_receive(settings, sources, output, *, now=None, connection=None):
    now = now or datetime.now()
    results, plans = receive_batch(settings, sources, now, connection, simulate=True)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    folder = Path(mkdtemp(prefix=f"模擬收信_{now:%Y%m%d_%H%M%S}_", dir=output))
    lines = ["fuelcore 收信模擬（只讀）"]

    def row_text_line(table, fields):
        return f"{table}：" + json.dumps(fields, ensure_ascii=False)

    for path, plan in plans:
        reply = plan.reply
        lines.extend(("", f"{path.name}（{plan.reference}）", row_text_line(reply.table, reply.fields)))
        for index, row in enumerate(reply.children, 1):
            lines.append(row_text_line(row.table, {**row.fields, "MainID": "<主表新 ID>"}))
            for child in row.children:
                lines.append(row_text_line(child.table, {**child.fields, "MainID": "<主表新 ID>",
                                                          "SubID": f"<第 {index} 筆第一層新 ID>"}))
        lines.append(row_text_line("RcvMsgRcds 擬新增", plan.receive_record))
        if plan.cusdec_changes:
            lines.append(row_text_line(f"CUSDEC {reply.fields['報單號碼']} 擬變更", plan.cusdec_changes))
        lines.append(f"sequence：InputDate={now:%Y-%m-%d}、InputSequence={plan.sequence}")
    for result in results:
        if result.status != "規劃":
            lines.append(f"{result.status} {result.file.name}：{result.message}")
    report = folder / "模擬結果.txt"
    report.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8-sig"))
    return report, results
