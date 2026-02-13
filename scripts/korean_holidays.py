"""2026 Korean public holidays + substitute holidays"""
from datetime import date

KOREAN_HOLIDAYS = {
    date(2026, 1, 1): "sinjeong",
    date(2026, 1, 27): "seollal-eve",
    date(2026, 1, 28): "seollal",
    date(2026, 1, 29): "seollal+1",
    date(2026, 3, 1): "samiljeol",
    date(2026, 3, 2): "samiljeol-sub",
    date(2026, 5, 5): "children-day",
    date(2026, 5, 24): "buddha-birthday",
    date(2026, 5, 25): "buddha-sub",
    date(2026, 6, 6): "memorial-day",
    date(2026, 8, 15): "liberation-day",
    date(2026, 8, 17): "liberation-sub",
    date(2026, 9, 14): "chuseok-eve",
    date(2026, 9, 15): "chuseok",
    date(2026, 9, 16): "chuseok+1",
    date(2026, 10, 3): "gaecheonjeol",
    date(2026, 10, 5): "gaecheonjeol-sub",
    date(2026, 10, 9): "hangul-day",
    date(2026, 12, 25): "christmas",
}


def is_korean_holiday(d):
    if d in KOREAN_HOLIDAYS:
        return True, KOREAN_HOLIDAYS[d]
    return False, ""


def is_business_day(d):
    if d.weekday() >= 5:
        return False
    if d in KOREAN_HOLIDAYS:
        return False
    return True
