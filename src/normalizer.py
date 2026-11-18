# -*- coding: utf-8 -*-
"""
Нормализация полей лота (разделы 3 и 9 ТЗ): площадь, цена и период оплаты,
срок аренды, кадастровый номер, даты, адрес.
"""

import re
from decimal import Decimal, InvalidOperation

from src.utils import CADASTRAL_RE, parse_iso_datetime

# Деньги, записанные цифрами: 15 000, 15000.50, «2 193,44»
_MONEY_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:\s\d{3})+(?:[.,]\d{1,2})?|\d{2,9}(?:[.,]\d{1,2})?)"
)

# Ключевые слова периодов оплаты (раздел 9 ТЗ)
_PERIOD_PATTERNS = {
    "год": re.compile(r"(в\s*год|годовая|годовых|руб\s*\.\s*/\s*год|за\s*год|в\s*год\b)", re.I),
    "месяц": re.compile(r"(в\s*месяц|руб\s*\.\s*/\s*мес|ежемесячн\w*|месячная|за\s*месяц)", re.I),
    "квартал": re.compile(r"(в\s*квартал|руб\s*\.\s*/\s*квартал|квартальная)", re.I),
}

# Формулировки, которые НЕ являются арендной платой (раздел 9 ТЗ)
_NON_RENT_HINTS = re.compile(r"(задаток|цена\s*лота|обеспечител)", re.I)


def to_float(value):
    """Безопасное преобразование строки/числа к float. Возвращает None при неудаче."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return None
    # «1 234,56» → 1234.56; «1,234.56» → 1234.56
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return float(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def normalize_cadastral(text):
    """Извлекает кадастровый номер вида 12:34:5678901:123 из произвольного текста."""
    if not text:
        return None
    match = CADASTRAL_RE.search(str(text))
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Площадь
# ---------------------------------------------------------------------------

_AREA_RE = re.compile(
    r"(\d{1,3}(?:\s\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*"
    r"(кв\s*\.\s*м|кв\.\s*м2|м2|м²|кв\.?м|сотк\w*|сот\s*\.?|гектар\w*|га\b)",
    re.I,
)


def area_to_m2(value, unit=None):
    """Приводит площадь к м². unit — 'м2' | 'га' | 'сот' | None (если уже м²)."""
    number = to_float(value)
    if number is None:
        return None
    unit_low = (str(unit or "") or "").lower().replace(" ", "")
    if unit_low in ("га", "гектар", "гектаров", "га²", "гa"):
        return round(number * 10000.0, 2)
    if unit_low.startswith("сот"):
        return round(number * 100.0, 2)
    return round(number, 2)  # м², «м2», «кв.м» и т.п.


def parse_area_text(text):
    """Ищет площадь в тексте («922 кв.м», «0,06 га», «6 сот.») и возвращает м²."""
    if not text:
        return None
    match = _AREA_RE.search(str(text))
    if not match:
        return None
    return area_to_m2(match.group(1), match.group(2))


def normalize_area(raw_value, unit=None, source_text=None):
    """Итоговая нормализация площади: сначала структурные поля, затем текст.

    :return: (площадь_м2, площадь_га) — значения могут быть None.
    """
    m2 = area_to_m2(raw_value, unit)
    if m2 is None and source_text:
        m2 = parse_area_text(source_text)
    if m2 is None:
        return None, None
    return round(m2, 2), round(m2 / 10000.0, 6)


# ---------------------------------------------------------------------------
# Цена (раздел 9 ТЗ)
# ---------------------------------------------------------------------------

def _money_values_near(text, pattern):
    """Находит денежные суммы рядом с формулировкой периода."""
    results = []
    for match in pattern.finditer(text):
        window = text[max(0, match.start() - 120): match.end() + 120]
        for money in _MONEY_RE.finditer(window):
            value = to_float(money.group(1))
            if value and value >= 1:
                results.append((value, window.strip()))
    return results


def normalize_price(price_fields, card_text):
    """
    Определяет арендную плату и период оплаты по правилам раздела 9 ТЗ.

    :param price_fields: список строк с исходными ценовыми формулировками
                         («начальная цена 15 000 руб.», «задаток 5 000 руб.» ...).
    :param card_text: полный текст карточки лота (описание, условия, атрибуты).
    :return: словарь:
             raw        — исходная формулировка цены (для отчёта);
             period     — 'год' | 'месяц' | 'квартал' | 'неизвестно';
             year       — цена в рублях за год или None;
             month      — цена в рублях за месяц или None;
             is_rent    — True, если цена распознана как арендная плата.
    """
    price_fields = [str(p) for p in (price_fields or []) if p]
    combined = " ".join(price_fields) + " " + (card_text or "")
    combined = combined.replace("\u00a0", " ")

    def rent_candidates(period_pattern):
        """Суммы рядом с формулировкой периода; «задаток/цена лота» отфильтрованы.
        Если в окне есть слово «арендн…», приоритет у суммы, стоящей после него."""
        results = []
        for value, window in _money_values_near(combined, period_pattern):
            if _NON_RENT_HINTS.search(window):
                continue
            rent_mention = re.search(r"арендн\w*", window, re.I)
            if rent_mention:
                tail_value = _first_money(window[rent_mention.start():])
                if tail_value:
                    results.append((tail_value, window))
                    continue
            results.append((value, window))
        return results

    def in_price_fields(window):
        return any(price_field and window[:120] in price_field
                   for price_field in price_fields)

    # 1–2. Ищем периодическую плату: сначала суммы, связанные со словом
    # «арендн…» или ценовыми полями, затем любые суммы рядом с периодом.
    for strict in (True, False):
        for period_name, pattern in _PERIOD_PATTERNS.items():
            for value, window in rent_candidates(pattern):
                related = bool(re.search(r"арендн\w*", window, re.I)) or \
                    in_price_fields(window)
                if strict and not related:
                    continue
                year, month = _to_year_month(value, period_name)
                return {
                    "raw": (price_fields[0] if price_fields else window)[:200],
                    "period": period_name,
                    "year": year,
                    "month": month,
                    "is_rent": True,
                }

    # 3. «Начальная цена» / «цена предмета контракта» без периода.
    #    По ТЗ НЕ считается автоматически годовой арендой.
    initial = [f for f in price_fields
               if re.search(r"(начальн\w*\s*цен|цена\s*предмет\w*|цена\s*контракт)", f, re.I)]
    if initial:
        value = _first_money(initial[0])
        return {
            "raw": initial[0][:200],
            "period": "неизвестно",
            "year": None,
            "month": None,
            "is_rent": False,
        }

    # 4. Задаток / цена лота — арендной платой не считаются (раздел 9 ТЗ)
    non_rent = [f for f in price_fields if _NON_RENT_HINTS.search(f)]
    if non_rent:
        return {
            "raw": non_rent[0][:200],
            "period": "неизвестно",
            "year": None,
            "month": None,
            "is_rent": False,
        }

    # 5. Период оплаты не определён вовсе
    raw = (price_fields[0] if price_fields else "")
    return {
        "raw": raw[:200],
        "period": "неизвестно",
        "year": None,
        "month": None,
        "is_rent": False,
    }


def _first_money(text):
    """Первая денежная сумма в строке."""
    match = _MONEY_RE.search(str(text))
    return to_float(match.group(1)) if match else None


def _to_year_month(value, period):
    """Переводит цену к годовой и месячной величине (раздел 9 ТЗ)."""
    if period == "год":
        return value, round(value / 12.0, 2)
    if period == "месяц":
        return round(value * 12.0, 2), value
    if period == "квартал":
        year = round(value * 4.0, 2)
        return year, round(year / 12.0, 2)
    return None, None


# ---------------------------------------------------------------------------
# Срок аренды, даты, адрес
# ---------------------------------------------------------------------------

def normalize_lease_term(card_text, attributes=None):
    """Ищет срок аренды в годах в тексте карточки и структурных атрибутах.

    Понимает формулировки: «срок аренды 20 лет», «Аренда на 20 лет»,
    «арендная плата ... на 10 лет».

    :return: срок в годах (float) или None.
    """
    for key, value in (attributes or {}).items():
        if re.search(r"срок\s*(аренды|договор)", str(key), re.I):
            number = to_float(value)
            if number:
                return number
    if not card_text:
        return None
    text = str(card_text)
    for pattern in (
        r"срок\w*\s*(?:аренды|договор\w*)?\D{0,40}?(\d{1,2})\s*(?:лет|год|года)\b",
        r"\bаренд\w*\D{0,25}?(\d{1,2})\s*(?:лет|год|года)\b",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            years = to_float(match.group(1))
            if years and years <= 49:  # 49 лет — максимум аренды земли
                return years
    return None


def normalize_date(value):
    """Дата в формате datetime (для Excel) или None."""
    return parse_iso_datetime(value)


def clean_html(text):
    """Убирает HTML-разметку из описания лота, оставляя читаемый текст."""
    if not text:
        return ""
    text = str(text)
    try:
        from bs4 import BeautifulSoup
        return re.sub(r"\s+", " ", BeautifulSoup(text, "lxml").get_text(" ")).strip()
    except Exception:
        # запасной путь: простое удаление тегов
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def normalize_address(address, description=None):
    """Чистит адрес/местоположение и убирает лишние пробелы."""
    text = clean_html(address) if address else ""
    if not text and description:
        # адрес часто встречается в описании после слова «адрес» или «расположен»
        match = re.search(r"(расположен\w*|адрес)\D{0,20}?(Российская\s*Федерация.{0,200})",
                          str(description), re.I)
        if match:
            text = match.group(2)
    return re.sub(r"\s+", " ", text or "").strip()
