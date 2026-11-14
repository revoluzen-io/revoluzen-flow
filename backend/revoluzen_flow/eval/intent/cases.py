"""Request-to-pipeline cases: a request, small input files, and the rows a
correct pipeline returns (2026-11-11).

Each case is scored by running the drafted pipeline on these files and
comparing what it returns with ``expected``, not by reading the draft. Rows
compare in any order; numbers with a small tolerance; a column the request
does not name may have any name in the result as long as its values match.

Write a case in the words a person would use, and work the expected rows out
by hand from the files. Cases the planners cannot do yet stay in: they are
what the score is for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ORDERS = """order_id,customer_id,status,amount,order_date
1,c1,paid,100,2026-11-01
2,c1,paid,50,2026-11-01
3,c2,refunded,30,2026-11-02
4,c2,paid,70,2026-11-02
5,c3,pending,20,2026-11-03
"""

CUSTOMERS = """customer_id,name,region
c1,Asha,north
c2,Ravi,south
c3,Meena,north
"""

DUPES = """order_id,customer_id,amount
1,c1,100
1,c1,100
2,c2,50
"""


@dataclass
class IntentCase:
    id: str
    request: str
    files: dict[str, str]
    expected: list[dict[str, Any]]
    note: str = ""
    tags: list[str] = field(default_factory=list)


CASES: list[IntentCase] = [
    IntentCase(
        "filter_paid", "Load orders.csv and keep only status = 'paid'",
        {"orders.csv": ORDERS},
        [{"order_id": 1}, {"order_id": 2}, {"order_id": 4}],
        tags=["filter"],
    ),
    IntentCase(
        "total_by_customer", "Load orders.csv and total the amount by customer_id",
        {"orders.csv": ORDERS},
        [{"customer_id": "c1", "total": 150}, {"customer_id": "c2", "total": 100}, {"customer_id": "c3", "total": 20}],
        tags=["aggregate"],
    ),
    IntentCase(
        "count_by_status", "Load orders.csv and count the orders by status",
        {"orders.csv": ORDERS},
        [{"status": "paid", "orders": 3}, {"status": "refunded", "orders": 1}, {"status": "pending", "orders": 1}],
        tags=["aggregate"],
    ),
    IntentCase(
        "dedupe_orders", "Load dupes.csv and remove duplicates on order_id",
        {"dupes.csv": DUPES},
        [{"order_id": 1, "amount": 100}, {"order_id": 2, "amount": 50}],
        tags=["deduplicate"],
    ),
    IntentCase(
        "join_customers", "Join orders.csv with customers.csv on customer_id",
        {"orders.csv": ORDERS, "customers.csv": CUSTOMERS},
        [{"order_id": 1, "name": "Asha"}, {"order_id": 2, "name": "Asha"}, {"order_id": 3, "name": "Ravi"},
         {"order_id": 4, "name": "Ravi"}, {"order_id": 5, "name": "Meena"}],
        tags=["join"],
    ),
    IntentCase(
        "paid_total_by_customer", "Load orders.csv, keep only status = 'paid', then total the amount by customer_id",
        {"orders.csv": ORDERS},
        [{"customer_id": "c1", "total": 150}, {"customer_id": "c2", "total": 70}],
        tags=["filter", "aggregate"],
    ),
    IntentCase(
        "daily_revenue", "Load orders.csv and calculate the daily revenue by order_date",
        {"orders.csv": ORDERS},
        [{"order_date": "2026-11-01", "revenue": 150}, {"order_date": "2026-11-02", "revenue": 100},
         {"order_date": "2026-11-03", "revenue": 20}],
        tags=["aggregate"],
    ),
    IntentCase(
        "paid_revenue_by_region",
        "Join orders.csv with customers.csv on customer_id, keep only status = 'paid', and total the amount by region",
        {"orders.csv": ORDERS, "customers.csv": CUSTOMERS},
        [{"region": "north", "total": 150}, {"region": "south", "total": 70}],
        tags=["join", "filter", "aggregate"],
    ),
    IntentCase(
        "top_two_orders", "Load orders.csv, sort by amount from highest, and keep the top 2 orders",
        {"orders.csv": ORDERS},
        [{"order_id": 1, "amount": 100}, {"order_id": 4, "amount": 70}],
        tags=["sort", "limit"],
    ),
    IntentCase(
        "average_by_status", "Load orders.csv and work out the average amount for each status",
        {"orders.csv": ORDERS},
        [{"status": "paid", "average": 220 / 3}, {"status": "refunded", "average": 30}, {"status": "pending", "average": 20}],
        tags=["aggregate"],
    ),
]


__all__ = ["CASES", "IntentCase"]
