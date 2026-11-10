import json
import re
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from tempfile import mkdtemp

from ..db import Database
from ..report.retention import mark_outputs
from .xml import CODE, EdiError, SendXmlBuilder, XmlSettings, row_text, upper_keys


@dataclass(frozen=True)
class SendPlan:
    filename: str
    data: bytes
    send_record: dict
    cusdec_changes: dict


@dataclass(frozen=True)
class SimulationResult:
    directory: Path
    planned: int
    rejected: int


def day_sequence(row, today, direction="Output"):
    previous = row_text(row, f"{direction.upper()}DATE").strip()
    if not previous:
        return 0
    match = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:$|[ T])", previous)
    if not match:
        raise EdiError(f"sequence.{direction}Date 格式不正確")
    if date(*(int(part) for part in match.groups())) != today:
        return 0
    current = int(row_text(row, f"{direction.upper()}SEQUENCE") or "0")
    if not 0 <= current <= 9999:
        raise EdiError(f"sequence.{direction}Sequence 超出 4 碼日序號範圍")
    return current


def planned_send(document, history, settings, now, sequence, packet):
    edi = settings.edi
    today = now.strftime("%Y%m%d")
    reference = f"{today}{sequence:04d}"
    filename = f"{CODE}.{edi.company_code}{reference}.xml"
    header = document.declaration
    sender = row_text(history, "傳送方代號") or row_text(header, "SENDID")
    receiver = row_text(history, "接收方代號") or row_text(header, "RECVID")
    sent_at = now.isoformat(sep=" ", timespec="milliseconds")
    record = {
        "傳送時間": sent_at, "報單號碼": row_text(header, "SERSNO"),
        "訊息簡稱": row_text(header, "MSGCODE"), "郵箱號碼": row_text(history, "郵箱號碼"),
        "交換控制碼": "", "訊息參考號碼": reference, "傳送方代號": sender, "接收方代號": receiver,
        "備份檔名": f"SendBak\\{today}\\{now:%H}\\{filename}", "檔案位元數": len(document.data),
        "封包號碼": packet, "User": edi.user, "ItemCount": document.item_count,
        "彙總報單": "", "彙總項次": "",
    }
    changes = {
        "STATUS": "S", "ICTLNO": packet, "MCTLNO": reference, "SENDDATE": sent_at,
        "SENDID": sender, "RECVID": receiver,
        "CUSBOXNO": row_text(header, "CUSBOXNO") or settings.cusboxno,
        "CUSMANNO": row_text(header, "CUSMANNO"),
    }
    return SendPlan(filename, document.data, record, changes)


def simulate_send(settings, sersnos, output, *, now=None, connection=None):
    if settings.edi is None:
        raise EdiError("fuelcore.ini 缺少 [edi] 設定")
    ids = list(dict.fromkeys(sersno.strip() for sersno in sersnos if sersno.strip()))
    if not ids:
        raise EdiError("請提供至少一個報單號碼")
    now = now or datetime.now()
    packet = f"{CODE}.{now:%Y%m%d%H%M%S}{now.microsecond // 1000:03d}"
    schema = settings.edi.edi_database
    plans, rejections = [], []
    with closing(Database(settings, connection, read_only=True)) as db:
        builder = SendXmlBuilder(XmlSettings.load(db), db)
        names = db.table_names(schema)
        if not {"sequence", "sendmsgrcds"} <= names.keys():
            raise EdiError(f"{schema} 測試庫須有 sequence 和 SendMsgRcds")
        sequence_table = db.table(names["sequence"], schema)
        record_table = db.table(names["sendmsgrcds"], schema)
        entries = db.select_dicts(f"SELECT * FROM {sequence_table} LIMIT 2")
        if len(entries) > 1:
            raise EdiError(f"{schema}.sequence 有多筆日序號資料")
        sequence = day_sequence(upper_keys(entries[0]) if entries else {}, now.date())
        original_sequence = sequence
        last = db.select_dicts(f"SELECT * FROM {record_table} ORDER BY `識別碼` DESC LIMIT 1")
        history = upper_keys(last[0]) if last else {}
        for sersno in ids:
            try:
                document = builder.document(sersno, ready_only=True)
                if sequence >= 9999:
                    raise EdiError("已用完當日 4 碼送信序號")
                plan = planned_send(document, history, settings, now, sequence + 1, packet)
            except ValueError as error:
                rejections.append((sersno, str(error)))
            else:
                plans.append(plan)
                sequence += 1

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    folder = Path(mkdtemp(prefix=f"模擬送信_{now:%Y%m%d_%H%M%S}_", dir=output))
    lines = ["fuelcore 送信模擬（只讀）", f"時間：{now.isoformat(sep=' ', timespec='milliseconds')}",
             f"封包號碼：{packet}"]
    if plans:
        lines.append(f"sequence：OutputDate={now:%Y-%m-%d}、OutputSequence={original_sequence} → {sequence}")
    for plan in plans:
        (folder / plan.filename).write_bytes(plan.data)
        lines.extend(("", f"XML：{plan.filename}",
                      "SendMsgRcds 擬新增：" + json.dumps(plan.send_record, ensure_ascii=False),
                      "CUSDEC 擬變更（UPDATE 維持原值）：" + json.dumps(plan.cusdec_changes, ensure_ascii=False)))
    for sersno, reason in rejections:
        lines.append(f"略過 {sersno}：{reason}")
    lines.extend(("", f"規劃 {len(plans)} 張，略過 {len(rejections)} 張。"))
    (folder / "模擬結果.txt").write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8-sig"))
    mark_outputs(folder, {name: "diag" for name in ("模擬結果.txt", *(plan.filename for plan in plans))})
    return SimulationResult(folder, len(plans), len(rejections))
