# -*- coding: utf-8 -*-
"""
Извлечение данных лота (раздел 10.2 ТЗ) из ответов API ГИС «Торги» и
страниц резервного источника goszemli.ru. Здесь «сырые» данные превращаются
в единый словарь лота; окончательная нормализация выполняется в normalizer.
"""

import re

from src.normalizer import (clean_html, normalize_area, normalize_address,
                            normalize_cadastral, normalize_date,
                            normalize_lease_term)
from src.utils import keyword_pattern, normalize_russian_text

# Человекочитаемые названия статусов лота (справочник ГИС «Торги»)
LOT_STATUS_NAMES = {
    "PUBLISHED": "Опубликован",
    "APPLICATIONS_SUBMISSION": "Приём заявок",
    "DETERMINING_WINNER": "Определение победителя",
    "SUCCEED": "Состоялся",
    "FAILED": "Не состоялся",
    "CANCELED": "Отменён",
}


def _value_to_text(value):
    """Приводит значение характеристики к строке (значение может быть списком)."""
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("name") or item.get("code") or item))
            else:
                parts.append(str(item))
        return ", ".join(p for p in parts if p)
    if isinstance(value, dict):
        return str(value.get("name") or value.get("code") or value)
    return str(value)


def _characteristics_maps(characteristics):
    """Разбирает список характеристик лота на удобные словари.

    Возвращает (по_коду, по_имени): {code: текст}, {name: текст}.
    """
    by_code, by_name = {}, {}
    for char in characteristics or []:
        if not isinstance(char, dict):
            continue
        code = str(char.get("code") or "")
        name = str(char.get("name") or "")
        value_text = _value_to_text(char.get("characteristicValue"))
        unit = ((char.get("unit") or {}) or {}).get("symbol") or ""
        if value_text:
            if code:
                by_code[code] = {"text": value_text, "unit": unit}
            if name:
                by_name[name] = {"text": value_text, "unit": unit}
    return by_code, by_name


def _attributes_map(attributes):
    """Разбирает атрибуты извещения в словарь {имя: значение}."""
    result = {}
    if isinstance(attributes, dict):
        return {str(k): _value_to_text(v) for k, v in attributes.items()}
    for attr in attributes or []:
        if not isinstance(attr, dict):
            continue
        name = attr.get("name") or attr.get("attributeName") or attr.get("code")
        value = attr.get("value") if "value" in attr else attr.get("attributeValue")
        if name and value is not None:
            result[str(name)] = _value_to_text(value)
    return result


def _collect_documents(detail, file_base_url):
    """Собирает список документов лота со ссылками (раздел 7.2 ТЗ)."""
    documents = []
    seen = set()
    for key in ("lotAttachments", "noticeAttachments", "attachments"):
        for att in (detail or {}).get(key) or []:
            if not isinstance(att, dict):
                continue
            file_id = att.get("fileId") or att.get("id")
            file_name = att.get("fileName") or att.get("name") or "документ"
            if not file_id:
                continue
            url = f"{file_base_url}{file_id}"
            if url in seen:
                continue
            seen.add(url)
            documents.append({"name": clean_html(file_name), "url": url})
    return documents


def new_lot(lot_id, source) -> dict:
    """Создаёт пустой словарь лота с обязательными служебными полями."""
    return {
        "id": str(lot_id),
        "source": source,
        "name": "",
        "notification_number": "",
        "cadastral_number": "",
        "address": "",
        "area_m2": None,
        "area_ha": None,
        "price_raw": "",
        "price_period": "неизвестно",
        "price_year": None,
        "price_month": None,
        "is_rent_price": False,
        "vri_text": "",
        "land_category": "",
        "ownership": "",
        "lease_term_years": None,
        "deadline": None,
        "documents": [],
        "status_code": "",
        "status_name": "",
        "deal_type_text": "",
        "category_name": "",
        "subject_rf_code": "",
        "description": "",
        "coords": None,
        "coords_origin": "",
        "card_text": "",          # полный текст карточки для проверки рисков
        "card_url": "",
        "attrs": {},
        "extra": {},              # исходные ценовые поля для нормализации цены
    }


# ---------------------------------------------------------------------------
# ГИС «Торги»: строка выдачи поиска
# ---------------------------------------------------------------------------

def parse_torgi_search_row(row: dict, file_base_url: str) -> dict:
    """Разбирает элемент списка лотов из ответа /new/api/public/lotcards/search."""
    lot = new_lot(row.get("id"), "torgi.gov.ru")
    lot["name"] = clean_html(row.get("lotName") or "")
    lot["notification_number"] = str(row.get("noticeNumber") or "")
    lot["status_code"] = str(row.get("lotStatus") or "")
    lot["status_name"] = LOT_STATUS_NAMES.get(lot["status_code"], lot["status_code"])
    lot["deal_type_text"] = str(row.get("typeTransaction") or "")
    category = row.get("category") or {}
    lot["category_name"] = str(category.get("name") or "")
    lot["subject_rf_code"] = str(row.get("subjectRFCode") or "")
    lot["deadline"] = normalize_date(row.get("biddEndTime"))

    by_code, _ = _characteristics_maps(row.get("characteristics"))
    cadastral = by_code.get("CadastralNumber", {}).get("text")
    lot["cadastral_number"] = normalize_cadastral(cadastral) or ""

    square = by_code.get("SquareZU")
    if square:
        lot["area_m2"], lot["area_ha"] = normalize_area(
            square["text"], square.get("unit"), lot["name"])

    permitted = by_code.get("PermittedUse", {}).get("text")
    if permitted:
        lot["vri_text"] = permitted

    lot["address"] = normalize_address(row.get("estateAddress"))
    lot["description"] = clean_html(row.get("lotDescription") or "")

    # Веб-ссылка на карточку лота (раздел 10.2 ТЗ)
    lot["card_url"] = (f"https://torgi.gov.ru/new/public/lots/lot/"
                       f"{lot['id']}/(lotInfo:info)")
    lot["extra"]["price_min"] = row.get("priceMinExact", row.get("priceMin"))
    lot["extra"]["price_min_exact"] = row.get("priceMinExact")
    lot["extra"]["deposit"] = row.get("deposit")
    return lot


# ---------------------------------------------------------------------------
# ГИС «Торги»: детальная карточка лота
# ---------------------------------------------------------------------------

def merge_torgi_detail(lot: dict, detail: dict, file_base_url: str) -> dict:
    """Дополняет лот данными карточки /new/api/public/lotcards/{id}."""
    if not isinstance(detail, dict):
        return lot

    lot["name"] = lot["name"] or clean_html(detail.get("lotName") or "")
    detail_desc = clean_html(detail.get("lotDescription") or "")
    if detail_desc and detail_desc not in (lot["description"] or ""):
        lot["description"] = (lot["description"] + " " + detail_desc).strip()

    lot["status_code"] = str(detail.get("lotStatus") or lot["status_code"])
    lot["status_name"] = LOT_STATUS_NAMES.get(lot["status_code"], lot["status_code"])
    if detail.get("typeTransaction"):
        lot["deal_type_text"] = str(detail["typeTransaction"])
    bidd_form = detail.get("biddForm") or {}
    if isinstance(bidd_form, dict) and bidd_form.get("name"):
        lot["attrs"]["Форма торгов"] = str(bidd_form["name"])
    bidd_type = detail.get("biddType") or {}
    if isinstance(bidd_type, dict) and bidd_type.get("name"):
        lot["attrs"]["Вид торгов"] = str(bidd_type["name"])

    category = detail.get("category") or {}
    if isinstance(category, dict) and category.get("name"):
        lot["category_name"] = str(category["name"])

    lot["subject_rf_code"] = str(detail.get("subjectRFCode") or lot["subject_rf_code"])
    lot["address"] = lot["address"] or normalize_address(detail.get("estateAddress"))

    ownership = detail.get("ownershipForm") or {}
    if isinstance(ownership, dict) and ownership.get("name"):
        lot["ownership"] = str(ownership["name"])

    lot["deadline"] = normalize_date(detail.get("biddEndTime")) or lot["deadline"]
    lot["extra"]["price_min"] = detail.get("priceMin", lot["extra"].get("price_min"))
    lot["extra"]["price_min_exact"] = detail.get("priceMinExact")
    lot["extra"]["deposit"] = detail.get("deposit")
    if detail.get("priceStep") is not None:
        lot["attrs"]["Шаг аукциона"] = str(detail["priceStep"])

    by_code, by_name = _characteristics_maps(detail.get("characteristics"))
    lot["attrs"].update({name: data["text"] for name, data in by_name.items()})

    if not lot["cadastral_number"]:
        cadastral = by_code.get("CadastralNumber", {}).get("text") or \
            by_name.get("Кадастровый номер", {}).get("text")
        lot["cadastral_number"] = normalize_cadastral(cadastral) or ""

    square = by_code.get("SquareZU") or by_name.get("Площадь земельного участка")
    if square and lot["area_m2"] is None:
        lot["area_m2"], lot["area_ha"] = normalize_area(
            square["text"], square.get("unit"), lot["description"])

    permitted = by_code.get("PermittedUse", {}).get("text")
    if not permitted:
        permitted = by_name.get("Вид разрешённого использования", {}).get("text")
    if permitted and not lot["vri_text"]:
        lot["vri_text"] = permitted

    # Категория земель — в характеристиках или атрибутах
    if not lot["land_category"]:
        for name, data in by_name.items():
            if re.search(r"категор\w*\s*земел", name, re.I):
                lot["land_category"] = data["text"]
                break

    # Координаты участка из карточки (point.lat / point.lon)
    point = detail.get("point") or {}
    if isinstance(point, dict) and point.get("lat") and point.get("lon"):
        lot["coords"] = (float(point["lat"]), float(point["lon"]))
        lot["coords_origin"] = "карточка лота (ГИС Торги)"

    lot["documents"] = _collect_documents(detail, file_base_url)

    # Полный текст карточки: описание + атрибуты + характеристики + условия
    text_parts = [lot.get("description", ""), lot["name"], lot["address"]]
    for key, value in lot["attrs"].items():
        text_parts.append(f"{key}: {value}")
    conditions = detail.get("conditions") or detail.get("contractConditions")
    if conditions:
        text_parts.append(clean_html(_value_to_text(conditions)))
    lot["card_text"] = " ".join(str(p) for p in text_parts if p)

    # Срок аренды: сначала структурные характеристики, затем текст карточки
    if lot["lease_term_years"] is None:
        for name in ("Срок аренды", "Срок договора аренды"):
            if name in by_name:
                match = re.search(r"(\d{1,2})", by_name[name]["text"])
                if match:
                    lot["lease_term_years"] = float(match.group(1))
                    break
    if lot["lease_term_years"] is None:
        lot["lease_term_years"] = normalize_lease_term(lot["card_text"], lot["attrs"])
    return lot


# ---------------------------------------------------------------------------
# Резервный источник heveya.ru
# ---------------------------------------------------------------------------

def _page_visible_text(html_text: str) -> str:
    """Видимый текст страницы: теги заменяются на '|', лишние пробелы убраны."""
    import re as _re

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text("|")
    except Exception:
        text = _re.sub(r"<script.*?</script>", " ", str(html_text), flags=_re.S)
        text = _re.sub(r"<[^>]+>", "|", text)
    text = _re.sub(r"[ \t\u00a0]+", " ", text)
    return _re.sub(r"\s*\|\s*", "|", text).strip(" |")


def _label_value(text: str, label: str, max_len: int = 220):
    """Значение после подписи «Метка:» до следующей вертикальной черты."""
    match = re.search(re.escape(label) + r"\s*:?\s*\|?\s*([^|]{1," + str(max_len) + r"})",
                      text, re.I)
    return match.group(1).strip(" ,;.") if match else None


def parse_heveya_list_item(url: str, name: str) -> dict:
    """Создаёт базовый лот из элемента каталога heveya.ru."""
    lot_id = re.search(r"/lot/(\d+)", url)
    lot = new_lot(f"heveya-{lot_id.group(1)}" if lot_id else url, "heveya.ru")
    lot["name"] = clean_html(name)
    lot["card_url"] = url
    # площадь в названии («Земельный участок 939 кв.м, Хабаровский край»)
    if lot["area_m2"] is None:
        lot["area_m2"], lot["area_ha"] = normalize_area(None, None, name)
    # регион — часть названия после последней запятой
    region = name.rsplit(",", 1)[-1].strip() if "," in name else ""
    if region and keyword_pattern("Хабаровск").search(normalize_russian_text(region)):
        lot["attrs"]["Регион (источник)"] = region
    return lot


def merge_heveya_detail(lot: dict, html_text: str) -> dict:
    """Разбирает страницу карточки heveya.ru и дополняет лот данными."""
    lot["attrs"].setdefault("Источник", "heveya.ru (срез каталога)")

    title = ""
    match = re.search(r"<title>(.*?)</title>", html_text, re.S | re.I)
    if match:
        title = clean_html(match.group(1))
    if title and not lot["name"]:
        lot["name"] = title
    if title:
        # площадь из заголовка («Земельный участок 1200 кв.м, ...»)
        if lot["area_m2"] is None:
            lot["area_m2"], lot["area_ha"] = normalize_area(None, None, title)

    text = _page_visible_text(html_text)

    # ключевые поля карточки по подписям
    for label, attr in (("Кадастровый номер", "Кадастровый номер (heveya)"),
                        ("Кадастровый номер участка", "Кадастровый номер (heveya)")):
        value = _label_value(text, label, 60)
        if value:
            lot["attrs"][attr] = value
            if not lot["cadastral_number"]:
                lot["cadastral_number"] = normalize_cadastral(value) or ""
            break

    vri = _label_value(text, "вид разрешенного использования", 180) or \
        _label_value(text, "вид разрешённого использования", 180)
    if vri and not lot["vri_text"]:
        lot["vri_text"] = vri

    category = _label_value(text, "Категория земель", 80)
    if category:
        lot["land_category"] = category

    ownership = _label_value(text, "форма собственности", 120) or \
        _label_value(text, "Форма торгов", 120)
    if ownership:
        lot["ownership"] = ownership

    address = _label_value(text, "Адрес", 240) or _label_value(text, "Местоположение", 240)
    if address:
        lot["address"] = normalize_address(address)

    price = _label_value(text, "Начальная цена", 120) or \
        _label_value(text, "Арендная плата", 120)
    if price:
        lot["extra"]["heveya_price_raw"] = f"цена: {price}"

    deadline_raw = None
    match = re.search(r"окончания\s*(?:приема|приёма)\s*заявок\s*:?\s*\|?\s*([^|]{1,60})",
                      text, re.I)
    if match:
        deadline_raw = match.group(1)
    if deadline_raw:
        match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", deadline_raw)
        if match:
            from datetime import datetime
            lot["deadline"] = datetime(int(match.group(3)), int(match.group(2)),
                                       int(match.group(1)))

    # описание лота (первый крупный текстовый блок)
    description = _label_value(text, "Описание", 800)
    if description:
        lot["description"] = clean_html(description)[:1200]

    # тип сделки: «аренда» или «продажа/выкуп»
    deal_zone = " ".join([title, lot.get("description", ""), text[:4000]])
    if re.search(r"аренд\w*", deal_zone, re.I):
        lot["deal_type_text"] = "rent"
        lot["attrs"]["Тип сделки"] = "Аренда"
    elif re.search(r"прода\w*|выкуп\w*", deal_zone, re.I):
        lot["deal_type_text"] = "sale"
        lot["attrs"]["Тип сделки"] = "Продажа"

    # статус торгов
    if re.search(r"процедура\s*завершена|торги\s*завершены|отменен\w*", text, re.I):
        lot["status_code"] = "FINISHED"
        lot["status_name"] = "Процедура завершена"
    else:
        lot["status_code"] = "PUBLISHED"
        lot["status_name"] = "Опубликован"

    # ссылки на документы
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            label = clean_html(a.get_text())
            if (re.search(r"\.(pdf|docx?|xlsx?)", href, re.I)) and label:
                lot["documents"].append({"name": label[:120], "url": href})
    except Exception:
        pass

    lot["card_text"] = " ".join(filter(None, [
        title, lot.get("description", ""), lot.get("address", ""),
        lot.get("vri_text", ""), lot["land_category"],
        lot["attrs"].get("Кадастровый номер (heveya)", ""),
        lot["extra"].get("heveya_price_raw", ""), text[:6000],
    ]))
    if lot["lease_term_years"] is None:
        lot["lease_term_years"] = normalize_lease_term(lot["card_text"], lot["attrs"])
    return lot


# ---------------------------------------------------------------------------
# Резервный источник goszemli.ru
# ---------------------------------------------------------------------------

def parse_goszemli_list_item(url: str, name: str) -> dict:
    """Создаёт базовый лот из элемента JSON-LD каталога goszemli.ru."""
    lot = new_lot(url.rsplit("/", 1)[-1], "goszemli.ru")
    lot["name"] = clean_html(name)
    lot["card_url"] = url
    # признак сделки: /torgi/arenda → аренда; /torgi/prodazha → продажа
    if re.search(r"аренда|arenda|под (?:аренду|аренду участка)", name, re.I) or \
            "/torgi/arenda" in url or "-arenda-" in url:
        lot["deal_type_text"] = "rent"
        lot["attrs"]["Тип сделки"] = "Аренда"
    elif re.search(r"прода\w*|uchastok-\d+-sot", url) and "arenda" not in url:
        lot["deal_type_text"] = "sale"
        lot["attrs"]["Тип сделки"] = "Продажа"
    # площадь в названии («9,22 сот.», «14,31 сот.»)
    if lot["area_m2"] is None:
        lot["area_m2"], lot["area_ha"] = normalize_area(None, None, name)
    return lot


def merge_goszemli_detail(lot: dict, html_text: str) -> dict:
    """Разбирает страницу карточки goszemli.ru и дополняет лот данными."""
    import json as _json

    from bs4 import BeautifulSoup

    lot["attrs"].setdefault("Источник", "goszemli.ru (срез каталога)")
    soup = BeautifulSoup(html_text, "lxml")

    # 1) JSON-LD Product: описание, цена, идентификатор
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict) or item.get("@type") != "Product":
                continue
            lot["description"] = clean_html(item.get("description") or "")
            offers = item.get("offers") or {}
            price = offers.get("price")
            if price is not None:
                lot["extra"]["goszemli_price"] = price
            sku = item.get("sku")
            if sku:
                # sku содержит идентификатор извещения ГИС «Торги»
                lot["attrs"]["SKU источника"] = str(sku)

    # 2) Видимые подписи «Название: значение» на странице
    page_text = re.sub(r"\s+", " ", soup.get_text(" "))
    label_map = {
        "Кадастровый номер": "cadastral",
        "Площадь": "area",
        "Категория земель": "land_category",
        "Разрешённое использование": "vri",
        "Использование по ЕГРН": "vri_egn",
        "Форма собственности": "ownership",
        "Регион": "region",
        "Кадастровая стоимость": "cad_cost",
        "Статус в ЕГРН": "egrn_status",
        "Ограничения прав": "restrictions",
        "Территориальная зона": "zone",
        "Права на участок": "rights",
    }
    parsed = {}
    for label, key in label_map.items():
        match = re.search(re.escape(label) + r"\s*([^|]{0,200}?)(?=\s{2,}|Кадастровая|Адрес|Регион|Категория|Разрешённое|Использование|Форма собственности|Статус в ЕГРН|Ограничения|Территориальная|Права на участок|$)",
                          page_text)
        if match:
            parsed[key] = match.group(1).strip()

    if parsed.get("cadastral") and not lot["cadastral_number"]:
        lot["cadastral_number"] = normalize_cadastral(parsed["cadastral"]) or ""
    if parsed.get("area") and lot["area_m2"] is None:
        lot["area_m2"], lot["area_ha"] = normalize_area(None, None, parsed["area"])
    if parsed.get("land_category"):
        lot["land_category"] = parsed["land_category"]
    vri = parsed.get("vri") or parsed.get("vri_egn")
    if vri and not lot["vri_text"]:
        lot["vri_text"] = vri
    if parsed.get("ownership"):
        lot["ownership"] = parsed["ownership"]
    if parsed.get("region"):
        lot["attrs"]["Регион (источник)"] = parsed["region"]

    # 3) Координаты: «Координаты: 54.271505 , 56.525537»
    match = re.search(r"Координаты\s*:\s*(-?\d{1,2}[.,]\d+)\s*,\s*(-?\d{1,3}[.,]\d+)", page_text)
    if match and not lot["coords"]:
        lat, lon = float(match.group(1).replace(",", ".")), float(match.group(2).replace(",", "."))
        lot["coords"] = (lat, lon)
        lot["coords_origin"] = "goszemli.ru"

    # 4) Статус торгов («Процедура завершена», «Приём заявок» …)
    if re.search(r"процедура\s*завершена", page_text, re.I):
        lot["status_code"] = "FINISHED"
        lot["status_name"] = "Процедура завершена"
    else:
        lot["status_code"] = "PUBLISHED"
        lot["status_name"] = "Опубликован"

    # 5) Ссылки на документы лота (страницы-источники / загруженные файлы)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = clean_html(a.get_text())
        if ("docs.goszemli.ru" in href or re.search(r"\.(pdf|docx?|xlsx?)", href, re.I)) and text:
            lot["documents"].append({"name": text[:120], "url": href})

    lot["card_text"] = " ".join([
        lot.get("description", ""), lot["name"], parsed.get("region", ""),
        parsed.get("restrictions", ""), parsed.get("rights", ""),
        parsed.get("zone", ""), lot["ownership"], lot["vri_text"],
        lot["land_category"], lot["address"],
    ])
    if parsed.get("cadastral") and not lot["address"]:
        lot["address"] = lot["description"][:250]
    return lot
