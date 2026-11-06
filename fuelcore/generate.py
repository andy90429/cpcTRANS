import argparse
import calendar
import random
from decimal import Decimal
from pathlib import Path

from .convert import read_txt
from .rules import rounded


def generate(samples, output, count=12, seed=None):
    if count < 12:
        raise ValueError("至少產生 12 張以涵蓋必要情境")
    templates = [read_txt(path) for path in sorted(Path(samples).rglob("*.txt"))]
    if not templates:
        raise ValueError("找不到 TXT 樣板")
    items_by_product = {}
    buyers = sorted({(header[23], header[24]) for header, _ in templates})
    for _, items in templates:
        for item in items:
            items_by_product.setdefault(item[3], []).append(item)
    if len(items_by_product) < 2:
        raise ValueError("樣板需包含至少兩種油品")
    single_item_templates = [(header, items) for header, items in templates if len(items) == 1]
    if not single_item_templates:
        raise ValueError("樣板需包含單項次報單")
    rng = random.Random(seed)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # Keep the range below the platform-dependent size limit of range().
    ids = [rng.randrange(10 ** 4) * 10 ** 6 + suffix
           for suffix in rng.sample(range(10 ** 6), count)]
    month = rng.randrange(1, 13)
    last = calendar.monthrange(2026, month)[1]
    days = (10, 11, 20, 21, last)
    first_ship = None
    generated = []
    for index, serial in enumerate(ids):
        base, items = rng.choice(single_item_templates)
        header = base.copy()
        header[1] = f"TEST{serial:010d}"
        header[11] = f"115/{month:02d}/{days[index % len(days)]:02d}"
        header[20] = f"TEST VESSEL {rng.randrange(10 ** 8):08d}"
        header[22] = f"{rng.randrange(10 ** 6, 10 ** 7):07d}"
        header[27] = f"ZZ{rng.randrange(10 ** 6):06d}"
        header[23], header[24] = rng.choice(buyers)
        if index == 0:
            first_ship = header[20], header[22]
        elif index == 1:
            header[20], header[22], header[27] = "", first_ship[1], ""
        elif index == 2:
            header[20], header[22], header[27] = first_ship[0], "", ""
        elif index == 3:
            header[20] += " LONG TEST VESSEL NAME FOR COLUMN LENGTH"
        elif index in (4, 5):
            header[20] += " & VESSEL" if index == 4 else " &AMP; VESSEL"
        elif index == 6:
            header[21] = ""
        if index == 7:
            items = [rng.choice(items_by_product[key]) for key in sorted(items_by_product)[:2]]
        rate = Decimal(rng.randrange(2900000, 3400000)).scaleb(-5)
        header[28] = format(rate, ".5f")
        totals = dict.fromkeys((7, 9, 11, 12, 13), Decimal(0))
        new_items = []
        for template in items:
            item = template.copy()
            quantity = Decimal(rng.randrange(1000, 700000)).scaleb(-3)
            if index == 7:
                # Whole-dollar amounts divisible by 100000 keep both TWD rounding paths equal.
                quantity = Decimal(100)
                price = Decimal(rng.randrange(1, 4) * 1000)
            else:
                price = Decimal(rng.randrange(10000, 150000)).scaleb(-2)
            if index == 8:
                quantity = Decimal(0)
            elif index == 9:
                quantity = Decimal("50000.000")
            elif index == 10:
                quantity, price = Decimal("33.123456789"), Decimal("321.987654321")
            usd = rounded(quantity * price, 2)
            sample_quantity = Decimal(template[9])
            litres = rounded(quantity * Decimal(template[11]) / sample_quantity, 0)
            values = {7: rounded(quantity * 1000, 0), 9: quantity, 11: litres,
                      12: rounded(usd * rate, 0), 13: usd}
            for field, value in values.items():
                item[field] = format(value, "f")
                totals[field] += value
            item[14] = format(price, "f")
            new_items.append(item)
        for source, target in ((7, 13), (9, 15), (11, 17), (12, 25), (13, 26)):
            header[target] = format(totals[source], "f")
        header[31] = format(rounded(totals[12] * Decimal(header[6]), 0), "f")
        path = output / f"TEST_{index + 1:04d}_{header[1]}.txt"
        with path.open("x", encoding="utf-8", newline="") as stream:
            stream.write("\r\n".join("|".join(line) for line in [header, *new_items]) + "\r\n")
        generated.append(path)
    return generated


def main():
    parser = argparse.ArgumentParser(description="由樣板產生以 TEST 開頭的 TXT")
    parser.add_argument("samples", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    print(f"已產生 {len(generate(args.samples, args.output, args.count, args.seed))} 個 TXT")


if __name__ == "__main__":
    main()
