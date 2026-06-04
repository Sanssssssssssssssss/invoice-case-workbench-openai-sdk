from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def parse_money(value: Any) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def values_match(left: Any, right: Any) -> bool:
    left_money = parse_money(left)
    right_money = parse_money(right)
    if left_money is not None and right_money is not None:
        return left_money == right_money
    return str(left or "").strip().lower() == str(right or "").strip().lower()
