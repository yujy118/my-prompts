import holidays
from datetime import date


def get_kr_holidays(year=None):
    if year is None:
        year = date.today().year
    return holidays.KR(years=year)


def is_korean_holiday(d):
    kr = get_kr_holidays(d.year)
    if d in kr:
        return True, kr[d]
    return False, ""


def is_business_day(d):
    if d.weekday() >= 5:
        return False
    if is_korean_holiday(d)[0]:
        return False
    return True
