"""
Generates the static analytical source files for P1.

    sales.csv      - fact table: one row per order line
    customers.csv  - dimension table: one row per customer

Everything is deterministic (seeded) so the warehouse is reproducible.
Re-run this any time to regenerate the dataset:

    python data/generate_data.py
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 42
START_DATE = date(2024, 1, 1)
END_DATE = date(2026, 9, 30)
N_CUSTOMERS = 140
TARGET_ORDERS = 1500

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #

REGIONS = {
    "North America": [
        ("New York", "United States"),
        ("Chicago", "United States"),
        ("Austin", "United States"),
        ("San Francisco", "United States"),
        ("Toronto", "Canada"),
        ("Vancouver", "Canada"),
    ],
    "EMEA": [
        ("London", "United Kingdom"),
        ("Berlin", "Germany"),
        ("Paris", "France"),
        ("Amsterdam", "Netherlands"),
        ("Dublin", "Ireland"),
        ("Madrid", "Spain"),
    ],
    "APAC": [
        ("Singapore", "Singapore"),
        ("Tokyo", "Japan"),
        ("Sydney", "Australia"),
        ("Mumbai", "India"),
        ("Seoul", "South Korea"),
    ],
    "LATAM": [
        ("Sao Paulo", "Brazil"),
        ("Mexico City", "Mexico"),
        ("Buenos Aires", "Argentina"),
        ("Bogota", "Colombia"),
    ],
}
REGION_WEIGHTS = {
    "North America": 0.40,
    "EMEA": 0.28,
    "APAC": 0.22,
    "LATAM": 0.10,
}

CHANNELS = ["Web", "Mobile App", "Marketplace", "Retail Store", "Partner Reseller"]
CHANNEL_WEIGHTS = [0.38, 0.24, 0.18, 0.12, 0.08]
CHANNEL_DISCOUNT = {
    "Web": (0.00, 0.15),
    "Mobile App": (0.00, 0.12),
    "Marketplace": (0.08, 0.28),
    "Retail Store": (0.00, 0.20),
    "Partner Reseller": (0.05, 0.18),
}

# category -> [(product, base_price, cost_ratio, popularity_weight)]
PRODUCTS = {
    "Electronics": [
        ("Laptop Pro 14", 1299.00, 0.68, 1.4),
        ("Wireless Earbuds", 179.00, 0.52, 3.0),
        ("4K Monitor 27", 429.00, 0.71, 1.8),
        ("Smartphone X", 999.00, 0.74, 2.2),
        ("Mechanical Keyboard", 139.00, 0.48, 2.6),
    ],
    "Furniture": [
        ("Ergonomic Chair", 549.00, 0.58, 1.5),
        ("Standing Desk", 699.00, 0.63, 1.3),
        ("Bookshelf Unit", 219.00, 0.66, 1.7),
        ("Conference Table", 1290.00, 0.69, 0.8),
    ],
    "Apparel": [
        ("Cotton Hoodie", 79.00, 0.38, 4.0),
        ("Running Shoes", 139.00, 0.44, 3.4),
        ("Denim Jacket", 119.00, 0.41, 2.4),
        ("Wool Scarf", 49.00, 0.35, 2.0),
    ],
    "Office Supplies": [
        ("Notebook A5", 19.00, 0.30, 6.0),
        ("Gel Pen Pack", 12.00, 0.26, 7.5),
        ("Desk Organizer", 34.00, 0.36, 3.8),
        ("Sticky Notes Set", 9.00, 0.24, 6.5),
    ],
    "Home & Kitchen": [
        ("Coffee Grinder", 149.00, 0.49, 2.2),
        ("Air Purifier", 389.00, 0.61, 1.4),
        ("Chef Knife", 129.00, 0.42, 1.9),
        ("Ceramic Cookware Set", 279.00, 0.56, 1.6),
    ],
}

SEGMENTS = [("Enterprise", 0.15), ("SMB", 0.35), ("Consumer", 0.50)]
LOYALTY_BY_SEGMENT = {
    "Enterprise": ["Gold", "Gold", "Platinum", "Platinum", "Bronze"],
    "SMB": ["Silver", "Silver", "Gold", "Bronze", "Platinum"],
    "Consumer": ["Bronze", "Bronze", "Silver", "Silver", "Gold"],
}

FIRST_NAMES = [
    "Ava", "Liam", "Noah", "Emma", "Olivia", "Mason", "Isabella", "Ethan",
    "Mia", "Lucas", "Amelia", "Arjun", "Zara", "Hugo", "Leila", "Mateo",
    "Priya", "Kenji", "Nadia", "Oscar", "Fatima", "Diego", "Ingrid", "Rafael",
    "Chloe", "Omar", "Yuki", "Elena", "Tariq", "Freya", "Diego", "Anika",
]
LAST_NAMES = [
    "Sharma", "Nakamura", "Okonkwo", "Fernandez", "Novak", "Andersen", "Rossi",
    "Kowalski", "Haddad", "Lindqvist", "Moreau", "Silva", "Petrov", "Dubois",
    "Ahmed", "Okafor", "Tanaka", "Weber", "Costa", "Nielsen", "Rahman",
    "Johansson", "Bianchi", "Ferreira", "Kaur", "Vargas", "Schmidt",
]
# Header order for customers.csv
CUSTOMER_FIELDS = [
    "customer_id", "customer_name", "segment", "region", "country", "city",
    "signup_date", "loyalty_tier",
]
# Header order for sales.csv
SALES_FIELDS = [
    "order_id", "order_date", "customer_id", "region", "city", "channel",
    "category", "product", "units", "unit_price", "discount", "revenue",
    "cost", "profit", "is_returned",
]


def weighted_choice(rng: random.Random, options, weights):
    return rng.choices(options, weights=weights, k=1)[0]


def month_factor(month: int) -> float:
    """Retail seasonality: Q4 is strong, early year is soft."""
    return {
        1: 0.85, 2: 0.88, 3: 0.95, 4: 0.97, 5: 1.02, 6: 0.98,
        7: 0.95, 8: 0.96, 9: 1.05, 10: 1.10, 11: 1.25, 12: 1.35,
    }[month]


def year_factor(year: int) -> float:
    return {2024: 1.0, 2025: 1.16, 2026: 1.31}[year]


def build_customers(rng: random.Random):
    customers = []
    region_names = list(REGIONS.keys())
    region_weights = [REGION_WEIGHTS[r] for r in region_names]
    used_names = set()

    for i in range(1, N_CUSTOMERS + 1):
        region = weighted_choice(rng, region_names, region_weights)
        city, country = rng.choice(REGIONS[region])
        segment = weighted_choice(rng, [s for s, _ in SEGMENTS], [w for _, w in SEGMENTS])

        while True:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if name not in used_names:
                used_names.add(name)
                break

        signup = START_DATE - timedelta(days=rng.randint(30, 900))
        tier = rng.choice(LOYALTY_BY_SEGMENT[segment])

        customers.append({
            "customer_id": f"CUST-{i:04d}",
            "customer_name": name,
            "segment": segment,
            "region": region,
            "country": country,
            "city": city,
            "signup_date": signup.isoformat(),
            "loyalty_tier": tier,
        })
    return customers


def build_sales(rng: random.Random, customers):
    total_days = (END_DATE - START_DATE).days + 1
    category_names = list(PRODUCTS.keys())
    category_weights = [sum(p[3] for p in PRODUCTS[c]) for c in category_names]

    rows = []
    order_no = 10000

    for _ in range(TARGET_ORDERS):
        # Pick a date, biased by seasonality and growth.
        day_offset = rng.randint(0, total_days - 1)
        order_date = START_DATE + timedelta(days=day_offset)
        density = month_factor(order_date.month) * year_factor(order_date.year)
        # Rejection sampling on the seasonal weight.
        while rng.random() > min(density, 1.45) / 1.45:
            day_offset = rng.randint(0, total_days - 1)
            order_date = START_DATE + timedelta(days=day_offset)
            density = month_factor(order_date.month) * year_factor(order_date.year)

        customer = rng.choice(customers)
        category = weighted_choice(rng, category_names, category_weights)
        product, base_price, cost_ratio, _ = weighted_choice(
            rng, PRODUCTS[category], [p[3] for p in PRODUCTS[category]]
        )
        channel = weighted_choice(rng, CHANNELS, CHANNEL_WEIGHTS)

        units = rng.choices([1, 2, 3, 4, 5, 6, 8], weights=[48, 22, 12, 7, 5, 3, 3])[0]
        unit_price = round(base_price * rng.uniform(0.88, 1.18), 2)
        d_lo, d_hi = CHANNEL_DISCOUNT[channel]
        discount = round(rng.uniform(d_lo, d_hi), 3)

        revenue = round(units * unit_price * (1 - discount), 2)
        cost = round(units * unit_price * cost_ratio, 2)
        profit = round(revenue - cost, 2)

        is_returned = rng.random() < 0.055
        if is_returned:
            revenue, cost, profit = -revenue, -cost, -profit

        order_no += 1
        rows.append({
            "order_id": f"ORD-{order_no}",
            "order_date": order_date.isoformat(),
            "customer_id": customer["customer_id"],
            "region": customer["region"],
            "city": customer["city"],
            "channel": channel,
            "category": category,
            "product": product,
            "units": units,
            "unit_price": unit_price,
            "discount": discount,
            "revenue": revenue,
            "cost": cost,
            "profit": profit,
            "is_returned": "true" if is_returned else "false",
        })

    rows.sort(key=lambda r: (r["order_date"], r["order_id"]))
    return rows


def write_csv(path: Path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  wrote {path.name}: {len(rows):,} rows")


def main() -> None:
    rng = random.Random(SEED)
    print(f"Generating P1 sample data (seed={SEED}) ...")
    customers = build_customers(rng)
    sales = build_sales(rng, customers)
    print(f"Writing to {HERE} ...")
    write_csv(HERE / "customers.csv", CUSTOMER_FIELDS, customers)
    write_csv(HERE / "sales.csv", SALES_FIELDS, sales)

    gross = sum(r["revenue"] for r in sales if r["is_returned"] == "false")
    profit = sum(r["profit"] for r in sales if r["is_returned"] == "false")
    print(f"  gross revenue: ${gross:,.2f}")
    print(f"  gross profit : ${profit:,.2f}")
    print("Done.")


if __name__ == "__main__":
    main()
