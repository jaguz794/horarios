from __future__ import annotations

from datetime import date, timedelta


def calculate_easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def move_to_next_monday(day_value: date) -> date:
    if day_value.weekday() == 0:
        return day_value
    return day_value + timedelta(days=(7 - day_value.weekday()))


def get_colombian_holidays(year: int) -> set[date]:
    easter = calculate_easter_sunday(year)
    holidays = {
        date(year, 1, 1),
        date(year, 5, 1),
        date(year, 7, 20),
        date(year, 8, 7),
        date(year, 12, 8),
        date(year, 12, 25),
        easter - timedelta(days=3),  # Jueves Santo
        easter - timedelta(days=2),  # Viernes Santo
        move_to_next_monday(date(year, 1, 6)),
        move_to_next_monday(date(year, 3, 19)),
        move_to_next_monday(date(year, 6, 29)),
        move_to_next_monday(date(year, 8, 15)),
        move_to_next_monday(date(year, 10, 12)),
        move_to_next_monday(date(year, 11, 1)),
        move_to_next_monday(date(year, 11, 11)),
        move_to_next_monday(easter + timedelta(days=39)),  # Ascension
        move_to_next_monday(easter + timedelta(days=60)),  # Corpus Christi
        move_to_next_monday(easter + timedelta(days=68)),  # Sagrado Corazon
    }
    return holidays


def is_colombian_holiday(day_value: date) -> bool:
    return day_value in get_colombian_holidays(day_value.year)


def get_special_day_label(day_value: date) -> str:
    is_sunday = day_value.weekday() == 6
    is_holiday = is_colombian_holiday(day_value)
    if is_sunday and is_holiday:
        return "Domingo y festivo"
    if is_sunday:
        return "Domingo"
    if is_holiday:
        return "Festivo"
    return ""
