"""Generate the local sample data used by the offline sample pipelines.

Run from anywhere::

    python samples/free-api-pipelines/data/generate_sample_data.py

The script is deterministic (fixed seed, fixed date anchor), so re-running
it reproduces byte-identical files. It uses only the standard library.

Files written next to this script and the pipelines that read them:

    orders.csv          13-local-csv-materialize, 14-customer-360-enrichment
    customers.csv       14-customer-360-enrichment (load into dbo.customers)
    daily_sales.csv     15-sales-pivot-trend
    baseline_sales.csv  15-sales-pivot-trend
    signups.csv         16-conditional-routing
    products.xml        18-xml-ingest-foreach-archive

Sample 18 moves products.xml into data/archive/ when it finishes; re-run
this script to put it back.
"""
from __future__ import annotations

import csv
import datetime as dt
import random
from pathlib import Path
from xml.sax.saxutils import escape

SEED = 20260526
HERE = Path(__file__).resolve().parent

# Last day of the daily_sales window. Everything else is dated relative
# to this so the files stay stable no matter when the script is run.
ANCHOR = dt.date(2026, 3, 31)
DAILY_DAYS = 60
BASELINE_DAYS = 14

REGIONS = ["EMEA", "AMER", "APAC", "LATAM"]
CHANNELS = ["online", "retail", "partner"]

# Weighted country mix. Codes are ISO 3166-1 alpha-2 so they line up with
# sample 16's conditional_split lists and REST Countries' `cca2`.
COUNTRY_WEIGHTS = {
    "US": 18, "GB": 9, "DE": 8, "FR": 6, "CA": 5, "IN": 6, "JP": 5, "AU": 4,
    "BR": 4, "NL": 3, "ES": 3, "IT": 3, "SE": 2, "MX": 3, "SG": 2, "KR": 2,
    "CN": 3, "ZA": 2, "AE": 2, "PL": 2, "IE": 2, "AR": 1, "NG": 1, "EG": 1,
}

PRODUCTS = [
    # (sku, name, category, unit_price)
    ("TL-1001", "Cordless Drill 18V", "tools", 129.00),
    ("TL-1002", "Impact Driver Kit", "tools", 159.00),
    ("TL-1003", "Socket Set 120pc", "tools", 89.50),
    ("TL-1004", "Laser Level", "tools", 74.99),
    ("TL-1005", "Torque Wrench", "tools", 64.00),
    ("TL-1006", "Angle Grinder", "tools", 98.00),
    ("TL-1007", "Workbench Vise", "tools", 112.00),
    ("TL-1008", "Oscillating Multi-Tool", "tools", 139.00),
    ("EL-2001", "USB-C Dock 12-in-1", "electronics", 189.00),
    ("EL-2002", "27in 4K Monitor", "electronics", 349.00),
    ("EL-2003", "Mechanical Keyboard", "electronics", 119.00),
    ("EL-2004", "Wireless Mouse", "electronics", 49.00),
    ("EL-2005", "Noise-Cancelling Headset", "electronics", 229.00),
    ("EL-2006", "1080p Webcam", "electronics", 79.00),
    ("EL-2007", "Portable SSD 2TB", "electronics", 179.00),
    ("EL-2008", "Smart Power Strip", "electronics", 39.00),
    ("EL-2009", "Label Printer", "electronics", 95.00),
    ("EL-2010", "Thermal Receipt Printer", "electronics", 215.00),
    ("BN-3001", "Home Office Starter Bundle", "bundles", 399.00),
    ("BN-3002", "Workshop Essentials Bundle", "bundles", 449.00),
    ("BN-3003", "Streaming Kit Bundle", "bundles", 329.00),
    ("BN-3004", "Field Technician Bundle", "bundles", 519.00),
    ("OF-4001", "Ergonomic Chair", "office", 279.00),
    ("OF-4002", "Sit-Stand Desk", "office", 459.00),
    ("OF-4003", "Monitor Arm Dual", "office", 89.00),
    ("OF-4004", "Desk Lamp LED", "office", 34.00),
    ("OF-4005", "Whiteboard 120x90", "office", 69.00),
    ("OF-4006", "Filing Cabinet 3-drawer", "office", 149.00),
    ("OF-4007", "Paper Shredder", "office", 84.00),
    ("OF-4008", "Acoustic Panel 6-pack", "office", 59.00),
]

FIRST_NAMES = [
    "Aarav", "Ada", "Amara", "Anika", "Arjun", "Beatriz", "Carlos", "Chloe",
    "Daniel", "Diego", "Elena", "Emeka", "Farah", "Felix", "Gabriel", "Hana",
    "Ibrahim", "Ines", "Jonas", "Julia", "Kai", "Kavya", "Lena", "Liam",
    "Lucia", "Maya", "Mateo", "Mei", "Nadia", "Noah", "Olivia", "Omar",
    "Priya", "Rafael", "Ravi", "Rosa", "Sanjay", "Sara", "Sofia", "Tariq",
    "Thomas", "Tomoko", "Valentina", "Wei", "Yara", "Yusuf", "Zara", "Zoe",
]
LAST_NAMES = [
    "Adeyemi", "Almeida", "Andersson", "Bauer", "Chen", "Costa", "Dubois",
    "Fischer", "Garcia", "Haddad", "Hoffmann", "Ivanova", "Jensen", "Kim",
    "Kowalski", "Kumar", "Lopez", "Martin", "Mensah", "Moreau", "Nakamura",
    "Nguyen", "Novak", "Okafor", "Oliveira", "Patel", "Rahman", "Rossi",
    "Santos", "Schmidt", "Silva", "Singh", "Tanaka", "Varga", "Walker",
    "Wang", "Yamamoto", "Zhang",
]
EMAIL_DOMAINS = ["example.com", "example.org", "example.net", "mail.example.com"]

PLANS = ["free", "starter", "team", "business"]
SOURCES = ["organic", "referral", "paid_search", "event", "partner"]


def _weighted_country(rng: random.Random) -> str:
    codes = list(COUNTRY_WEIGHTS)
    weights = [COUNTRY_WEIGHTS[c] for c in codes]
    return rng.choices(codes, weights=weights, k=1)[0]


def _person(rng: random.Random) -> tuple[str, str]:
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    return first, last


def _email(first: str, last: str, n: int, rng: random.Random) -> str:
    return f"{first}.{last}{n}@{rng.choice(EMAIL_DOMAINS)}".lower()


def _write_csv(name: str, header: list[str], rows: list[list]) -> Path:
    path = HERE / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


# ---------------------------------------------------------------------------
# customers.csv + orders.csv  (samples 13, 14)
# ---------------------------------------------------------------------------

def gen_customers(rng: random.Random, n: int = 120) -> list[list]:
    rows = []
    for cid in range(1, n + 1):
        first, last = _person(rng)
        signup = ANCHOR - dt.timedelta(days=rng.randint(120, 900))
        tier = rng.choices(
            ["bronze", "silver", "gold", "platinum"], weights=[45, 30, 18, 7], k=1
        )[0]
        rows.append([
            cid,
            f"{first} {last}",
            _email(first, last, cid, rng),
            _weighted_country(rng),
            signup.isoformat(),
            tier,
        ])
    return rows


def gen_orders(rng: random.Random, customers: list[list], n: int = 400) -> list[list]:
    rows = []
    by_id = {c[0]: c for c in customers}
    for i in range(n):
        order_id = 1001 + i
        customer = rng.choice(customers)
        sku, name, _category, price = rng.choice(PRODUCTS)
        qty = rng.choices(range(1, 13), weights=[30, 20, 14, 10, 7, 5, 4, 3, 2, 2, 2, 1], k=1)[0]
        order_date = ANCHOR - dt.timedelta(days=rng.randint(0, DAILY_DAYS - 1))
        # Most orders ship to the customer's own country; some do not.
        country = by_id[customer[0]][3] if rng.random() < 0.85 else _weighted_country(rng)
        status = rng.choices(
            ["delivered", "shipped", "processing", "cancelled"], weights=[70, 15, 10, 5], k=1
        )[0]
        rows.append([
            order_id,
            customer[0],
            order_date.isoformat(),
            country,
            sku,
            name,
            qty,
            f"{price:.2f}",
            status,
        ])
    rows.sort(key=lambda r: (r[2], r[0]))
    return rows


# ---------------------------------------------------------------------------
# daily_sales.csv + baseline_sales.csv  (sample 15)
# ---------------------------------------------------------------------------

_REGION_BASE = {"EMEA": 9000.0, "AMER": 12000.0, "APAC": 7500.0, "LATAM": 3800.0}
_REGION_UNIT_PRICE = {"EMEA": 48.0, "AMER": 52.0, "APAC": 41.0, "LATAM": 36.0}
_CHANNEL_SHARE = {"online": 0.55, "retail": 0.30, "partner": 0.15}


def _sales_rows(rng: random.Random, first_day: dt.date, days: int, trend: float) -> list[list]:
    """One row per region x channel x day, with a mild upward trend,
    a weekly cycle and noise. `trend` is the total growth over the window."""
    rows = []
    for d in range(days):
        day = first_day + dt.timedelta(days=d)
        growth = 1.0 + trend * (d / max(days - 1, 1))
        weekday = day.weekday()
        weekly = 0.8 if weekday >= 5 else 1.0 + 0.04 * (2 - abs(weekday - 2))
        for region in REGIONS:
            for channel in CHANNELS:
                base = _REGION_BASE[region] * _CHANNEL_SHARE[channel]
                revenue = base * growth * weekly * rng.uniform(0.85, 1.15)
                units = max(1, int(round(revenue / _REGION_UNIT_PRICE[region] * rng.uniform(0.9, 1.1))))
                rows.append([region, day.isoformat(), channel, units, f"{revenue:.2f}"])
    return rows


def gen_daily_sales(rng: random.Random) -> list[list]:
    first = ANCHOR - dt.timedelta(days=DAILY_DAYS - 1)
    return _sales_rows(rng, first, DAILY_DAYS, trend=0.18)


def gen_baseline_sales(rng: random.Random) -> list[list]:
    # The two weeks immediately before the daily window.
    first = ANCHOR - dt.timedelta(days=DAILY_DAYS - 1 + BASELINE_DAYS)
    return _sales_rows(rng, first, BASELINE_DAYS, trend=0.03)


# ---------------------------------------------------------------------------
# signups.csv  (sample 16)
# ---------------------------------------------------------------------------

def gen_signups(rng: random.Random, n: int = 300) -> list[list]:
    rows = []
    for i in range(n):
        signup_id = 50001 + i
        first, last = _person(rng)
        email = _email(first, last, signup_id, rng)
        country = _weighted_country(rng)
        roll = rng.random()
        # A few deliberately broken rows so the validate step has
        # something to quarantine.
        if roll < 0.03:
            email = email.replace("@", "_at_")          # fails regex
        elif roll < 0.05:
            email = ""                                  # fails email_required
        elif roll < 0.07:
            country = ""                                # fails country_required
        elif roll < 0.08:
            country = "USA"                             # fails LENGTH = 2
        signup_date = ANCHOR - dt.timedelta(days=rng.randint(0, 45))
        rows.append([
            signup_id,
            f"{first} {last}",
            email,
            country,
            signup_date.isoformat(),
            rng.choices(PLANS, weights=[50, 25, 18, 7], k=1)[0],
            rng.choice(SOURCES),
        ])
    return rows


# ---------------------------------------------------------------------------
# products.xml  (sample 18)
# ---------------------------------------------------------------------------

def gen_products_xml(rng: random.Random) -> str:
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<products>"]
    for pid, (sku, name, category, price) in enumerate(PRODUCTS, start=1):
        stock = rng.randint(0, 400)
        lines.append("  <product>")
        lines.append(f"    <id>{pid}</id>")
        lines.append(f"    <sku>{escape(sku)}</sku>")
        lines.append(f"    <name>{escape(name)}</name>")
        lines.append(f"    <category>{category}</category>")
        lines.append(f"    <price>{price:.2f}</price>")
        lines.append(f"    <stock>{stock}</stock>")
        lines.append("  </product>")
    # Two rows the data_quality step should drop: a negative stock count
    # and a missing SKU.
    lines += [
        "  <product>",
        f"    <id>{len(PRODUCTS) + 1}</id>",
        "    <sku>EL-2099</sku>",
        "    <name>Returned Monitor (damaged)</name>",
        "    <category>electronics</category>",
        "    <price>99.00</price>",
        "    <stock>-3</stock>",
        "  </product>",
        "  <product>",
        f"    <id>{len(PRODUCTS) + 2}</id>",
        "    <sku></sku>",
        "    <name>Unlabelled Pallet</name>",
        "    <category>bundles</category>",
        "    <price>250.00</price>",
        "    <stock>4</stock>",
        "  </product>",
        "</products>",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def main() -> None:
    rng = random.Random(SEED)

    customers = gen_customers(rng)
    _write_csv("customers.csv",
               ["customer_id", "name", "email", "country_code", "signup_date", "tier"],
               customers)

    orders = gen_orders(rng, customers)
    _write_csv("orders.csv",
               ["order_id", "customer_id", "order_date", "country", "sku", "product",
                "quantity", "unit_price", "status"],
               orders)

    _write_csv("daily_sales.csv",
               ["region", "sale_date", "channel", "units", "revenue"],
               gen_daily_sales(rng))
    _write_csv("baseline_sales.csv",
               ["region", "sale_date", "channel", "units", "revenue"],
               gen_baseline_sales(rng))

    _write_csv("signups.csv",
               ["signup_id", "name", "email", "country_code", "signup_date", "plan", "source"],
               gen_signups(rng))

    (HERE / "products.xml").write_text(gen_products_xml(rng), encoding="utf-8")

    for name in ("customers.csv", "orders.csv", "daily_sales.csv", "baseline_sales.csv",
                 "signups.csv", "products.xml"):
        p = HERE / name
        with p.open("rb") as fh:
            n_lines = sum(1 for _ in fh)
        print(f"wrote {p.name:20s} {p.stat().st_size:>8,d} bytes  {n_lines:>5d} lines")


if __name__ == "__main__":
    main()
