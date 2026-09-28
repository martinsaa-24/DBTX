"""Generate the jaffle_shop seed CSVs deterministically.

    python scripts/generate_seeds.py

The output is committed; re-running produces byte-identical files (fixed RNG seed),
so tests can assert on exact row counts and aggregates.
"""

from __future__ import annotations

import csv
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

SEEDS_DIR = Path(__file__).resolve().parents[1] / "examples" / "jaffle_shop" / "seeds"

RNG_SEED = 42
N_CUSTOMERS = 1_000
N_ORDERS = 8_000
START = datetime(2024, 9, 1)
END = datetime(2026, 9, 1)

STORES = [
    # name, opened_at, tax_rate
    ("Philadelphia", datetime(2016, 9, 1), 0.06),
    ("Brooklyn", datetime(2017, 3, 12), 0.04),
    ("Chicago", datetime(2018, 4, 29), 0.0625),
    ("San Francisco", datetime(2018, 5, 9), 0.075),
    ("New Orleans", datetime(2019, 3, 10), 0.04),
    ("Los Angeles", datetime(2025, 3, 1), 0.08),  # opens mid-window
]

PRODUCTS = [
    # sku, name, type, price (cents)
    ("JAF-001", "nutellaphone who dis?", "jaffle", 1100),
    ("JAF-002", "doctor stew", "jaffle", 1100),
    ("JAF-003", "the krautback", "jaffle", 1200),
    ("JAF-004", "flame impala", "jaffle", 1400),
    ("JAF-005", "mel-bun", "jaffle", 1200),
    ("BEV-001", "tangaroo", "beverage", 600),
    ("BEV-002", "chai and mighty", "beverage", 500),
    ("BEV-003", "vanilla ice", "beverage", 600),
    ("BEV-004", "for richer or pourover", "beverage", 700),
    ("BEV-005", "adele-ade", "beverage", 400),
]
PRODUCT_WEIGHTS = [14, 12, 9, 6, 10, 11, 9, 8, 7, 14]

FIRST_NAMES = """James Mary Robert Patricia John Jennifer Michael Linda David Elizabeth William
Barbara Richard Susan Joseph Jessica Thomas Sarah Charles Karen Daniel Nancy Matthew Lisa
Anthony Betty Mark Sandra Donald Ashley Steven Kimberly Andrew Emily Paul Donna Joshua Michelle
Kenneth Carol Kevin Amanda Brian Melissa George Deborah Timothy Stephanie""".split()
LAST_NAMES = """Smith Johnson Williams Brown Jones Garcia Miller Davis Rodriguez Martinez Hernandez
Lopez Gonzalez Wilson Anderson Thomas Taylor Moore Jackson Martin Lee Perez Thompson White Harris
Sanchez Clark Ramirez Lewis Robinson Walker Young Allen King Wright Scott Torres Nguyen Hill
Flores""".split()


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _write(name: str, header: list[str], rows: list[list]) -> None:
    path = SEEDS_DIR / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    print(f"{path.relative_to(SEEDS_DIR.parents[2])}: {len(rows):,} rows")


def main() -> None:
    rng = random.Random(RNG_SEED)
    SEEDS_DIR.mkdir(parents=True, exist_ok=True)

    stores = [(_uuid(rng), *s) for s in STORES]
    _write(
        "raw_stores",
        ["id", "name", "opened_at", "tax_rate"],
        [[sid, name, opened.isoformat(sep=" "), rate] for sid, name, opened, rate in stores],
    )

    _write(
        "raw_products",
        ["sku", "name", "type", "price"],
        [list(p) for p in PRODUCTS],
    )

    customers = [_uuid(rng) for _ in range(N_CUSTOMERS)]
    _write(
        "raw_customers",
        ["id", "name"],
        [[cid, f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"] for cid in customers],
    )

    # A long tail of customers: a few regulars place most of the orders.
    customer_weights = [rng.paretovariate(1.2) for _ in customers]
    span = int((END - START).total_seconds())
    order_rows: list[list] = []
    item_rows: list[list] = []
    for _ in range(N_ORDERS):
        order_id = _uuid(rng)
        ordered_at = START + timedelta(seconds=rng.randrange(span))
        # Stores only take orders once they are open.
        open_stores = [s for s in stores if s[2] <= ordered_at]
        store_id, _, _, tax_rate = rng.choice(open_stores)
        n_items = rng.choices([1, 2, 3, 4, 5], weights=[30, 35, 20, 10, 5])[0]
        products = rng.choices(PRODUCTS, weights=PRODUCT_WEIGHTS, k=n_items)
        subtotal = sum(p[3] for p in products)
        tax_paid = round(subtotal * tax_rate)
        order_rows.append(
            [
                order_id,
                rng.choices(customers, weights=customer_weights)[0],
                ordered_at.isoformat(sep=" "),
                store_id,
                subtotal,
                tax_paid,
                subtotal + tax_paid,
            ]
        )
        item_rows.extend([_uuid(rng), order_id, p[0]] for p in products)

    order_rows.sort(key=lambda r: r[2])
    _write(
        "raw_orders",
        ["id", "customer", "ordered_at", "store_id", "subtotal", "tax_paid", "order_total"],
        order_rows,
    )
    _write("raw_items", ["id", "order_id", "sku"], item_rows)


if __name__ == "__main__":
    main()
