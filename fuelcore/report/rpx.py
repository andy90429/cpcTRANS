import math
import struct
from dataclasses import dataclass, field
from decimal import Decimal


MAGIC = b"TPF0"
INTEGER = {2: "<b", 3: "<h", 4: "<i", 19: "<q"}
FLOAT = {15: "<f", 16: "<q", 17: "<d", 21: "<d"}
STRING_ENCODING = {6: "cp950", 7: "cp950", 12: "cp950", 18: "utf-16le", 20: "utf-8"}
EMPTY = {0, 8, 9, 13}


class RpxError(ValueError):
    pass


def extended(raw):
    mantissa, flags = struct.unpack("<QH", raw)
    exponent = flags & 0x7FFF
    sign = -1 if flags & 0x8000 else 1
    if exponent == 0x7FFF:
        return math.copysign(math.inf, sign) if mantissa == (1 << 63) else math.nan
    if not mantissa:
        return math.copysign(0.0, sign)
    try:
        return math.ldexp(sign * mantissa, max(exponent, 1) - 16383 - 63)
    except OverflowError:
        return math.copysign(math.inf, sign)


@dataclass(frozen=True)
class Value:
    kind: int
    data: object = None

    @classmethod
    def string(cls, text, previous=None):
        if previous and previous.kind == 20:
            return cls(20, text.encode("utf-8"))
        if (previous is None or previous.kind == 6) and text.isascii() and len(text) <= 255:
            return cls(6, text.encode("ascii"))
        return cls(18, text.encode("utf-16le"))

    @classmethod
    def integer(cls, number):
        for kind in (2, 3, 4, 19):
            try:
                struct.pack(INTEGER[kind], number)
            except struct.error:
                continue
            return cls(kind, number)
        raise RpxError("整數超出 TPF0 範圍")

    def text(self):
        codec = STRING_ENCODING.get(self.kind)
        return self.data.decode(codec, errors="replace") if codec else ""

    def number(self, fallback=0):
        if self.kind in INTEGER:
            return self.data
        if self.kind == 5:
            return extended(self.data)
        if self.kind in FLOAT:
            decoded = struct.unpack(FLOAT[self.kind], self.data)[0]
            return Decimal(decoded).scaleb(-4) if self.kind == 16 else decoded
        return fallback


@dataclass
class CollectionItem:
    properties: list
    order: Value = None
    framed: bool = True


@dataclass
class Component:
    class_name: str
    name: str = ""
    properties: list = field(default_factory=list)
    children: list = field(default_factory=list)
    flags: int = None
    position: Value = None

    def get(self, key):
        return next((value for name, value in self.properties if name.casefold() == key.casefold()), None)

    def set(self, key, value):
        for index, (name, _) in enumerate(self.properties):
            if name.casefold() == key.casefold():
                self.properties[index] = (name, value)
                return
        self.properties.append((key, value))

    def text(self, key):
        value = self.get(key)
        return value.text() if value else ""

    def number(self, key, fallback=0):
        value = self.get(key)
        return value.number(fallback) if value else fallback

    def boolean(self, key, fallback=False):
        value = self.get(key)
        return value.kind == 9 if value and value.kind in (8, 9) else fallback

    def set_text(self, key, text):
        self.set(key, Value.string(text, self.get(key)))

    def set_number(self, key, number):
        self.set(key, Value.integer(round(number)))


class Reader:
    def __init__(self, data, offset):
        self.data = data
        self.offset = offset

    def take(self, count):
        end = self.offset + count
        if count < 0 or end > len(self.data):
            raise RpxError(f"TPF0 長度無效（位移 {self.offset}）")
        data = self.data[self.offset:end]
        self.offset = end
        return data

    def unpack(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def peek(self):
        if self.offset >= len(self.data):
            raise RpxError("TPF0 串流未完整結束")
        return self.data[self.offset]

    def short(self):
        return self.take(self.unpack("<B"))

    def until_end(self, read):
        values = []
        while self.peek():
            values.append(read())
        self.take(1)
        return values

    def properties(self):
        return self.until_end(lambda: (self.short().decode("latin-1"), self.value()))

    def collection_item(self):
        order = self.value() if self.peek() in INTEGER else None
        framed = self.peek() == 1
        if framed:
            self.take(1)
        return CollectionItem(self.properties(), order, framed)

    def value(self):
        kind = self.unpack("<B")
        if kind in EMPTY:
            data = None
        elif kind in INTEGER:
            data = self.unpack(INTEGER[kind])
        elif kind in FLOAT or kind == 5:
            data = self.take(10 if kind == 5 else struct.calcsize(FLOAT[kind]))
        elif kind in (6, 7):
            data = self.short()
        elif kind in (10, 12, 18, 20):
            data = self.take(self.unpack("<i") * (2 if kind == 18 else 1))
        elif kind == 1:
            data = self.until_end(self.value)
        elif kind == 11:
            data = self.until_end(lambda: self.short().decode("latin-1"))
        elif kind == 14:
            data = self.until_end(self.collection_item)
        else:
            raise RpxError(f"未知 TPF0 屬性型別 {kind}（位移 {self.offset - 1}）")
        return Value(kind, data)

    def component(self):
        flags = self.unpack("<B") if self.peek() & 0xF0 == 0xF0 else None
        position = self.value() if flags is not None and flags & 2 else None
        class_name, name = (self.short().decode("latin-1") for _ in range(2))
        if not class_name:
            raise RpxError("TPF0 元件缺少類別名稱")
        return Component(class_name, name, self.properties(), self.until_end(self.component), flags, position)


def short(data):
    return bytes((len(data),)) + data


def write_properties(properties):
    return b"".join(short(key.encode("latin-1")) + write_value(value) for key, value in properties) + b"\0"


def write_value(value):
    kind, data = value.kind, value.data
    if kind in EMPTY:
        payload = b""
    elif kind in INTEGER:
        payload = struct.pack(INTEGER[kind], data)
    elif kind in FLOAT or kind == 5:
        expected = 10 if kind == 5 else struct.calcsize(FLOAT[kind])
        if not isinstance(data, bytes) or len(data) != expected:
            raise RpxError(f"型別 {kind} 需要 {expected} 位元組的浮點表示")
        payload = data
    elif kind in (6, 7):
        payload = short(data)
    elif kind in (10, 12, 18, 20):
        if kind == 18 and len(data) % 2:
            raise RpxError("WideString 長度必須為偶數")
        payload = struct.pack("<i", len(data) // (2 if kind == 18 else 1)) + data
    elif kind == 1:
        payload = b"".join(map(write_value, data)) + b"\0"
    elif kind == 11:
        payload = b"".join(short(item.encode("latin-1")) for item in data) + b"\0"
    elif kind == 14:
        payload = b"".join((write_value(item.order) if item.order else b"") +
                           (b"\1" if item.framed else b"") + write_properties(item.properties)
                           for item in data) + b"\0"
    else:
        raise RpxError(f"未知 TPF0 屬性型別 {kind}")
    return bytes((kind,)) + payload


def write_component(component):
    prefix = b"" if component.flags is None else bytes((component.flags,))
    if component.flags is not None and component.flags & 2:
        prefix += write_value(component.position)
    return (prefix + short(component.class_name.encode("latin-1")) + short(component.name.encode("latin-1"))
            + write_properties(component.properties) + b"".join(map(write_component, component.children)) + b"\0")


@dataclass
class RpxDocument:
    header: bytes
    segments: list
    title: str = ""

    @property
    def roots(self):
        return [segment for segment in self.segments if isinstance(segment, Component)]

    @classmethod
    def create(cls, title, roots):
        encoded = title.encode("cp950")
        return cls(b"EDI1.1" + struct.pack("<i", len(encoded)) + encoded, list(roots), title)

    @classmethod
    def read(cls, data):
        title, start = "", 0
        if data.startswith(b"EDI1."):
            if len(data) < 10:
                raise RpxError("EDI 報表表頭不完整")
            length = struct.unpack_from("<i", data, 6)[0]
            start = 10 + length
            if length < 0 or start > len(data):
                raise RpxError("EDI 報表標題長度無效")
            title = data[10:start].decode("cp950", errors="replace")
        marker = data.find(MAGIC, start)
        if marker < 0:
            raise RpxError("報表缺少 TPF0 串流")
        header = data[:marker]
        segments = []
        while marker >= 0:
            reader = Reader(data, marker + len(MAGIC))
            try:
                segments.append(reader.component())
            except RecursionError as error:
                raise RpxError("TPF0 巢狀層級過深") from error
            marker = data.find(MAGIC, reader.offset)
            end = len(data) if marker < 0 else marker
            if end > reader.offset:
                segments.append(data[reader.offset:end])
        return cls(header, segments, title)

    def to_bytes(self):
        return self.header + b"".join(MAGIC + write_component(part) if isinstance(part, Component) else part
                                     for part in self.segments)
