"""Recalculate a declaration using the quantities on its original front page."""

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP

from .rpx import RpxError
from .table13 import FACTORS


ZERO = Decimal(0)
FEE_RATE = Decimal("0.0004")
BARREL_KL = Decimal("0.158987")
GRAVITY_NUMERATOR = Decimal("141.5")
GRAVITY_OFFSET = Decimal("131.5")


def decimal_input(value):
    try:
        number = Decimal(str(value).strip().replace(",", ""))
        if not number.is_finite() or not 0 <= number < 10**12:
            raise InvalidOperation
        return number
    except InvalidOperation as error:
        raise RpxError(f"請輸入有效的非負數值（小於 1 兆）：{value}") from error


def rounded(value, places=0):
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def down(value):
    return value.to_integral_value(rounding=ROUND_DOWN)


def table_factor(api):
    index = int(rounded(api * 100)) - 900
    if index < 0:
        raise RpxError("密度換算的 API 低於 ASTM Table 13 下限 9.00")
    return Decimal(FACTORS[min(index, len(FACTORS) - 1)]).scaleb(-5)


def density_api(density):
    if not Decimal("0.5") < density < Decimal("1.1"):
        raise RpxError("密度須大於 0.5 且小於 1.1")
    return rounded(GRAVITY_NUMERATOR / density - GRAVITY_OFFSET, 1) - Decimal("0.1")


def density_from_api(api):
    """Return the representative density and inclusive four-decimal bounds."""
    step = Decimal("0.0001")
    lower = rounded(GRAVITY_NUMERATOR / (GRAVITY_OFFSET + api + Decimal("0.15")), 4)
    upper = rounded(GRAVITY_NUMERATOR / (GRAVITY_OFFSET + api + Decimal("0.05")), 4)
    if density_api(lower) != api:
        lower += step
    if density_api(upper) != api:
        upper -= step
    density = rounded(GRAVITY_NUMERATOR / (GRAVITY_OFFSET + api + Decimal("0.1")), 4)
    return density, lower, upper


def ton_to_liters(quantity, factor):
    return rounded(quantity / factor * BARREL_KL, 3) * 1000


def derive_api(quantity, liters):
    hits = []
    for tenth in range(90, 451):
        api = Decimal(tenth).scaleb(-1)
        if ton_to_liters(quantity, table_factor(api)) == liters:
            hits.append(api)
    return hits[len(hits) // 2] if hits else None


@dataclass(frozen=True)
class OriginalItem:
    number: str
    quantity: Decimal
    price: Decimal
    net: Decimal
    liters: Decimal
    twd: Decimal
    unit: str = "TNE"
    stat_unit: str = "LTR"


@dataclass(frozen=True)
class ItemInput:
    quantity: Decimal
    price: Decimal
    density: Decimal = None


@dataclass(frozen=True)
class ItemResult:
    quantity: Decimal
    price: Decimal
    usd: Decimal
    twd: Decimal
    net: Decimal
    liters: Decimal
    net_ratio: Decimal
    stat_ratio: Decimal
    stat_places: int
    factor: Decimal = None
    api: Decimal = None
    needs_density: bool = False


@dataclass(frozen=True)
class Calculation:
    rows: tuple
    quantity: Decimal
    usd: Decimal
    twd: Decimal
    net: Decimal
    liters: Decimal
    gross: Decimal
    packages: Decimal
    fee: Decimal
    tax_total: Decimal
    exchange: Decimal


@dataclass(frozen=True)
class Trial:
    items: tuple
    exchange: Decimal
    gross_ratio: Decimal
    package_ratio: Decimal
    fee_offset: Decimal
    other_tax: Decimal
    apis: tuple
    filed_totals: dict

    @classmethod
    def from_original(cls, items, exchange, gross, packages, fee, tax_total, *, filed_totals=None):
        if not items:
            raise RpxError("報單沒有可試算的項次")
        quantity = sum(item.quantity for item in items)
        net = sum(item.net for item in items)
        twd = sum(item.twd for item in items)
        apis = tuple(derive_api(item.quantity, item.liters) if uses_table(item) else None for item in items)
        totals = dict(filed_totals or {})
        totals.update(gross=gross, packages=packages, fee=fee, tax_total=tax_total)
        return cls(tuple(items), exchange, gross / net if net else ZERO,
                   (Decimal(1) if packages == down(quantity) else packages / quantity) if quantity else ZERO,
                   down(twd * FEE_RATE) - fee, tax_total - fee, apis, totals)

    def inputs(self):
        return tuple(ItemInput(item.quantity, item.price) for item in self.items)

    def calculate(self, inputs=None, *, exchange=None):
        original = inputs is None and exchange is None
        exchange = self.exchange if exchange is None else decimal_input(exchange)
        inputs = self.inputs() if inputs is None else tuple(inputs)
        if len(inputs) != len(self.items):
            raise RpxError("試算輸入的項次數量與原報單不同")
        rows = tuple(self.calculate_item(item, entry, api, exchange)
                     for item, entry, api in zip(self.items, inputs, self.apis))
        if original:
            rows = tuple(replace(row, net=item.net, twd=item.twd, liters=item.liters)
                         for row, item in zip(rows, self.items))
        quantity = rounded(sum(row.quantity for row in rows), 2)
        usd = sum(row.usd for row in rows)
        twd = sum(row.twd for row in rows)
        net = sum(row.net for row in rows)
        liters = rounded(sum(row.liters for row in rows), 2)
        fee = max(ZERO, down(twd * FEE_RATE) - self.fee_offset)
        result = Calculation(rows, quantity, usd, twd, net, liters, rounded(net * self.gross_ratio, 2),
                             down(quantity * self.package_ratio), fee, self.other_tax + fee, exchange)
        return replace(result, **self.filed_totals) if original else result

    def calculate_item(self, item, entry, api, exchange):
        quantity, price = decimal_input(entry.quantity), decimal_input(entry.price)
        net_ratio = item.net / item.quantity if item.quantity else ZERO
        stat_ratio = item.liters / item.quantity if item.quantity else ZERO
        stat_places = 0 if item.liters == down(item.liters) else 2
        table = uses_table(item)
        if entry.density is not None:
            if not table:
                raise RpxError(f"項次 {item.number} 的單位不是 TNE／LTR")
            api = density_api(decimal_input(entry.density))
        factor = table_factor(api) if api is not None else None
        liters = ton_to_liters(quantity, factor) if factor else rounded(quantity * stat_ratio, stat_places)
        usd = rounded(quantity * price, 2)
        twd = rounded(usd * exchange)
        return ItemResult(quantity, price, usd, twd, rounded(quantity * net_ratio, 2),
                          liters, net_ratio, stat_ratio, stat_places, factor, api, table and factor is None)


def uses_table(item):
    return item.unit == "TNE" and "LTR" in item.stat_unit and item.quantity > 0
