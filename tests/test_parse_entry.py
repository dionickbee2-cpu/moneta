from datetime import date

import pytest

from bot import parse_entry, BadDate, tx_line

TODAY = date(2026, 9, 27)


@pytest.mark.parametrize("text,amount,desc,income,when", [
    ("coffee 4.5", 4.5, "coffee", False, None),
    ("4.5 coffee", 4.5, "coffee", False, None),
    ("кофе 12,50", 12.5, "кофе", False, None),
    ("coffee 3.10", 3.1, "coffee", False, None),
    ("salary +3411", 3411, "salary", True, None),
    ("47", 47, "—", False, None),
    ("12.05", 12.05, "—", False, None),
    ("x 31.02", 31.02, "x", False, None),
    ("pizza 2 slices 7.5", 7.5, "pizza 2 slices", False, None),
    ("coffee 4.5 25.05", 4.5, "coffee", False, date(2026, 5, 25)),
    ("coffee 4.5 25/05", 4.5, "coffee", False, date(2026, 5, 25)),
    ("coffee 4.5 01.12.2025", 4.5, "coffee", False, date(2025, 12, 1)),
    ("coffee 4.5 1.12.25", 4.5, "coffee", False, date(2025, 12, 1)),
    ("coffee 4.5 15.10", 4.5, "coffee", False, date(2025, 10, 15)),  # future -> last year
])
def test_parse(text, amount, desc, income, when):
    e = parse_entry(text, TODAY)
    assert e == {"amount": amount, "description": desc, "income": income, "date": when}


@pytest.mark.parametrize("text", ["coffee", "hello world", "0", "coffee 12.05.2026"])
def test_no_amount(text):
    assert parse_entry(text, TODAY) is None


def test_impossible_date():
    with pytest.raises(BadDate):
        parse_entry("coffee 5 31.02", TODAY)


def test_html_escaped():
    line = tx_line({"Date": "01.09.2026", "Amount": 5.0, "Category": "☕ Cafe",
                    "Description": "a_b *c* <b>x</b> & y", "Type": "expense"})
    assert "<b>x</b>" not in line
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in line
    assert "☕ Кафе" in line
