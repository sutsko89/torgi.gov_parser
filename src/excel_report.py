# -*- coding: utf-8 -*-
"""
Формирование Excel-отчёта (раздел 10.3 ТЗ).

Файл output/land_lots_report.xlsx содержит листы:
  «Подходящие»        — лоты, прошедшие все обязательные фильтры;
  «Требуют проверки»  — лоты с потенциальными ограничениями или неполными данными;
  «Отклонённые»       — лоты, не прошедшие фильтрацию, с указанием причины;
  «Все лоты»          — полная выгрузка всех обработанных лотов;
  «Сводка»            — количества и дата запуска.

Числа (площадь, цены) сохраняются как числа; ссылки на лоты и документы —
гиперссылки; кириллица корректно отображается (openpyxl использует UTF-8).
При повторном запуске существующий файл обновляется; дополнительно создаётся
копия с датой в имени (land_lots_report_2026-11-18.xlsx).
"""

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from src.config import resolve_path

# Колонки листа «Подходящие» — раздел 10.4 ТЗ
BASE_COLUMNS = [
    ("ID лота", 22),
    ("Название лота", 46),
    ("Номер извещения", 20),
    ("Кадастровый номер", 22),
    ("Адрес / местоположение", 46),
    ("Площадь, м²", 12),
    ("Площадь, га", 12),
    ("Цена, руб./год", 14),
    ("Цена, руб./мес.", 14),
    ("Исходная цена", 28),
    ("Период цены", 14),
    ("ВРИ", 34),
    ("Категория земель", 20),
    ("Форма собственности", 20),
    ("Срок аренды, лет", 14),
    ("Дата окончания приёма заявок", 16),
    ("Расстояние, км", 13),
    ("Статус локации", 13),
    ("Уровень риска", 14),
    ("Найденные ограничения", 44),
    ("Ссылка на лот", 40),
    ("Документы", 44),
    ("Источник", 16),
    ("Дата проверки", 18),
]

SHEET_COLUMNS = {
    "Подходящие": BASE_COLUMNS,
    "Требуют проверки": BASE_COLUMNS + [("Причина проверки", 52)],
    "Отклонённые": BASE_COLUMNS + [("Причина отклонения", 60)],
    "Все лоты": BASE_COLUMNS + [("Причина проверки", 48), ("Причина отклонения", 56),
                                ("Лист", 15)],
}

SHEET_ORDER = ["Подходящие", "Требуют проверки", "Отклонённые", "Все лоты", "Сводка"]

_HEADER_FILL = PatternFill("solid", fgColor="2F5496")
_HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
_THIN_GRAY = Side(style="thin", color="D9D9D9")
_ROW_BORDER = Border(left=_THIN_GRAY, right=_THIN_GRAY, top=_THIN_GRAY, bottom=_THIN_GRAY)
_LINK_FONT = Font(name="Calibri", color="0563C1", underline="single", size=10)
_BODY_FONT = Font(name="Calibri", size=10)

_STATUS_FILLS = {
    "Подходящие": PatternFill("solid", fgColor="E2EFDA"),     # зелёный
    "Требуют проверки": PatternFill("solid", fgColor="FFF2CC"),  # жёлтый
    "Отклонённые": PatternFill("solid", fgColor="FCE4EC"),    # розовый
}


def _write_lot_row(ws, lot: dict, sheet_name: str, row_idx: int):
    """Записывает одну строку лота в рабочий лист."""
    values = [
        lot.get("id"),
        lot.get("name"),
        lot.get("notification_number"),
        lot.get("cadastral_number"),
        lot.get("address"),
        lot.get("area_m2"),
        lot.get("area_ha"),
        lot.get("price_year"),
        lot.get("price_month"),
        lot.get("price_raw"),
        lot.get("price_period"),
        lot.get("vri_text"),
        lot.get("land_category"),
        lot.get("ownership"),
        lot.get("lease_term_years"),
        lot.get("deadline"),
        lot.get("distance_km"),
        lot.get("location_status"),
        lot.get("risk_level"),
        lot.get("risks_summary") or "; ".join(lot.get("review_reasons") or [])[:200] or
        "; ".join(lot.get("reject_reasons") or [])[:200],
        lot.get("card_url"),
        "",                                   # документы — гиперссылки ниже
        lot.get("source"),
        lot.get("checked_at"),
    ]
    if sheet_name == "Требуют проверки":
        values.append("; ".join(lot.get("review_reasons") or []))
    elif sheet_name == "Отклонённые":
        values.append("; ".join(lot.get("reject_reasons") or []))
    elif sheet_name == "Все лоты":
        values.append("; ".join(lot.get("review_reasons") or []))
        values.append("; ".join(lot.get("reject_reasons") or []))
        values.append(lot.get("sheet"))

    for col_idx, value in enumerate(values, start=1):
        cell = ws.cell(row=row_idx, column=col_idx, value=value)
        cell.font = _BODY_FONT
        cell.border = _ROW_BORDER
        cell.alignment = Alignment(vertical="top", wrap_text=col_idx in (2, 5, 10, 12, 20, 22)
                                   or col_idx > len(BASE_COLUMNS))
        # Числовые форматы
        if col_idx == 6:
            cell.number_format = "#,##0.00"
        elif col_idx == 7:
            cell.number_format = "0.000000"
        elif col_idx in (8, 9):
            cell.number_format = "#,##0.00"
        elif col_idx == 15:
            cell.number_format = "0.#"
        elif col_idx == 16 and value:
            cell.number_format = "DD.MM.YYYY"
        elif col_idx == 17:
            cell.number_format = "0.0"
        elif col_idx == 24 and value:
            cell.number_format = "DD.MM.YYYY HH:MM"

    # Гиперссылка на карточку лота (колонка 21)
    url = lot.get("card_url")
    if url:
        cell = ws.cell(row=row_idx, column=21)
        cell.hyperlink = url
        cell.value = url if len(url) <= 60 else url[:57] + "..."
        cell.font = _LINK_FONT

    # Гиперссылки на документы (колонка 22): имя первого документа + список
    documents = lot.get("documents") or []
    if documents:
        names = "; ".join(d["name"][:40] for d in documents[:3])
        if len(documents) > 3:
            names += f"; +{len(documents) - 3}"
        cell = ws.cell(row=row_idx, column=22, value=names)
        cell.hyperlink = documents[0]["url"]
        cell.font = _LINK_FONT

    # Подсветка строки цветом итогового статуса
    fill = _STATUS_FILLS.get(sheet_name if sheet_name != "Все лоты" else lot.get("sheet"))
    if fill:
        ws.cell(row=row_idx, column=19).fill = fill


def _create_lot_sheet(wb: Workbook, sheet_name: str):
    """Создаёт лист с заголовками колонок и базовым форматированием."""
    ws = wb.create_sheet(sheet_name)
    columns = SHEET_COLUMNS[sheet_name]
    for col_idx, (title, width) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col_idx, value=title)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}1"
    ws.row_dimensions[1].height = 30
    return ws


# Соответствие колонок итоговых листов колонкам листа «Все лоты»
# (на «Все лоты»: 25 — «Причина проверки», 26 — «Причина отклонения», 27 — «Лист»)
_SOURCE_COLUMN_MAP = {
    "Подходящие": {},
    "Требуют проверки": {25: 25},
    "Отклонённые": {25: 26},
}


def _fill_sheet_from(wb: Workbook, target_name: str, lots: list, all_ws=None):
    """Переносит строки лотов из листа «Все лоты» на итоговый лист (без дублей)."""
    ws = wb[target_name]
    columns = SHEET_COLUMNS[target_name]
    column_map = _SOURCE_COLUMN_MAP.get(target_name, {})
    wrap_columns = (2, 5, 10, 12, 20, 22)
    next_row = ws.max_row + 1
    for lot in lots:
        for col_idx in range(1, len(columns) + 1):
            src_idx = column_map.get(col_idx, col_idx)
            src_cell = all_ws.cell(row=lot["_row"], column=src_idx)
            dst_cell = ws.cell(row=next_row, column=col_idx, value=src_cell.value)
            dst_cell.font = _BODY_FONT
            dst_cell.border = _ROW_BORDER
            dst_cell.number_format = src_cell.number_format
            dst_cell.alignment = Alignment(vertical="top", wrap_text=col_idx in wrap_columns
                                           or col_idx > len(BASE_COLUMNS))
            if src_cell.hyperlink and src_cell.hyperlink.target:
                dst_cell.hyperlink = src_cell.hyperlink.target
                dst_cell.font = _LINK_FONT
        fill = _STATUS_FILLS.get(target_name)
        if fill:
            ws.cell(row=next_row, column=19).fill = fill
        next_row += 1


def _write_summary(wb: Workbook, stats: dict, report_path: Path):
    """Лист «Сводка»: количества лотов и дата запуска (раздел 10.3 ТЗ)."""
    ws = wb.create_sheet("Сводка")
    ws.column_dimensions["A"].width = 44
    ws.column_dimensions["B"].width = 46
    title = ws.cell(row=1, column=1, value="Сводка по поиску земельных участков для ИЖС")
    title.font = Font(bold=True, size=13, color="2F5496")
    ws.merge_cells("A1:B1")

    max_rent = stats.get("max_rent_per_year")
    price_text = (f"аренда ≤ {float(max_rent):,.0f} руб./год".replace(",", " ")
                  if max_rent else "без ограничения по цене (снято — цена справочная)")
    rows = [
        ("Дата и время запуска", stats.get("run_started_at")),
        ("Источник данных", stats.get("source_used")),
        ("Найдено лотов (по списку)", stats.get("found")),
        ("Прошли первичную фильтрацию", stats.get("primary_passed")),
        ("Проверено карточек (детально)", stats.get("cards_checked")),
        ("Подходящих лотов", stats.get("fitting")),
        ("Требуют ручной проверки", stats.get("review")),
        ("Отклонено лотов", stats.get("rejected")),
        ("Всего записей в базе (вкл. прошлые запуски)", stats.get("total_in_state")),
        ("Ошибок в ходе работы", stats.get("errors")),
        ("Файл отчёта", report_path.name),
        ("Пороговые критерии",
         f"площадь ≥ {stats.get('min_area_m2')} м²; арендная плата {price_text}; "
         f"расстояние ≤ {stats.get('max_distance_km')} км от Хабаровска"),
    ]
    row_idx = 3
    for label, value in rows:
        cell_a = ws.cell(row=row_idx, column=1, value=label)
        cell_a.font = Font(bold=True, size=10)
        cell_a.border = _ROW_BORDER
        cell_b = ws.cell(row=row_idx, column=2, value=value)
        cell_b.font = _BODY_FONT
        cell_b.border = _ROW_BORDER
        row_idx += 1
    return ws


def build_report(all_lots: list, stats: dict, cfg: dict) -> Path:
    """
    Формирует Excel-файл отчёта.

    :param all_lots: список словарей лотов (все записи базы, без дубликатов).
    :param stats: сводка запуска.
    :param cfg: конфигурация.
    :return: путь к основному файлу отчёта.
    """
    excel_path: Path = resolve_path(cfg["output"]["excel_file"])
    excel_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    wb.remove(wb.active)  # убираем лист по умолчанию

    # 1. Лист «Все лоты» — полная выгрузка (источник строк для остальных листов)
    all_ws = _create_lot_sheet(wb, "Все лоты")
    ordered_lots = sorted(
        all_lots,
        key=lambda lot: (lot.get("distance_km") is None,
                         lot.get("distance_km") if lot.get("distance_km") is not None else 0,
                         str(lot.get("deadline") or "")))
    row_idx = 2
    for lot in ordered_lots:
        lot["_row"] = row_idx
        _write_lot_row(all_ws, lot, "Все лоты", row_idx)
        row_idx += 1

    # 2. Итоговые листы с копированием строк (без повторного вычисления)
    for sheet_name in ("Подходящие", "Требуют проверки", "Отклонённые"):
        _create_lot_sheet(wb, sheet_name)
        subset = [lot for lot in ordered_lots if lot.get("sheet") == sheet_name]
        _fill_sheet_from(wb, sheet_name, subset, all_ws)

    # 3. Сводка
    _write_summary(wb, stats, excel_path)

    # Порядок листов: Подходящие, Требуют проверки, Отклонённые, Все лоты, Сводка
    wb._sheets = [wb[name] for name in SHEET_ORDER]

    try:
        wb.save(excel_path)
    except PermissionError as exc:
        raise PermissionError(
            f"Не удалось сохранить {excel_path}: файл открыт в Microsoft Excel. "
            f"Закройте его и повторите запуск. ({exc})")

    # Дополнительная копия с датой в имени (раздел 10.3 ТЗ)
    if cfg["output"].get("dated_copy", True):
        dated_path = excel_path.with_name(
            f"{excel_path.stem}_{datetime.now().strftime('%Y-%m-%d')}{excel_path.suffix}")
        wb.save(dated_path)

    return excel_path


def verify_report(path: Path) -> dict:
    """Служебная проверка файла: имена листов и количество строк."""
    wb = load_workbook(path, read_only=True)
    result = {name: wb[name].max_row - 1 for name in wb.sheetnames}
    wb.close()
    return result
