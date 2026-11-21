# -*- coding: utf-8 -*-
"""
Локальный бот поиска земельных участков для ИЖС (Хабаровский край).

Запуск:  python main.py   (раздел 10.1 ТЗ)

Порядок работы:
  1. Загрузка config.yaml и настройка журнала search.log.
  2. Сбор лотов со ВСЕХ настроенных источников (раздел 5 ТЗ): основной
     ГИС «Торги» + резервные goszemli.ru и heveya.ru; результаты
     объединяются и дедуплицируются, а не берутся только от первого
     работающего источника.
  3. Первичная фильтрация списка (раздел 7.1 ТЗ). Лоты резервных
     источников уже пре-фильтрованы по региону ДО загрузки карточек.
  4. Проверка карточек лотов, документов, координат и рисков (разделы 7.2–7.3).
  5. Дедупликация записей между запусками (раздел 14 ТЗ).
  6. Формирование Excel-отчёта с листами «Подходящие», «Требуют проверки»,
     «Отклонённые», «Все лоты», «Сводка» (раздел 10.3 ТЗ).

Уведомления в Telegram или по email не предусмотрены (раздел 3 ТЗ).
"""

import copy
import re
import sys
from pathlib import Path

# Корректный вывод кириллицы в консоли Windows 11
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config, resolve_path  # noqa: E402
from src.excel_report import build_report  # noqa: E402
from src.filters import LotFilter, _contains_any  # noqa: E402
from src.logger import setup_logging  # noqa: E402
from src.torgi_client import (SourceUnavailableError, get_reserve_clients,  # noqa: E402
                              get_search_source)
from src.utils import LotStateStore, iso_now, parse_iso_datetime, rand_delay  # noqa: E402

# Листы отчёта (раздел 10.3 ТЗ)
SHEET_FIT = "Подходящие"
SHEET_REVIEW = "Требуют проверки"
SHEET_REJECT = "Отклонённые"

# Номер извещения ГИС «Торги» в идентификаторе лота резервных источников
# («26000003290000000024-1-arenda-uchastka-…» → 26000003290000000024)
NOTICE_IN_ID_RE = re.compile(r"(\d{18,})-\d")


def _lot_identity_keys(lot: dict) -> set:
    """Ключи дедупликации лота между источниками и запусками."""
    keys = set()
    lot_id = str(lot.get("id") or "")
    keys.add("id:" + lot_id)
    match = NOTICE_IN_ID_RE.match(lot_id)
    if match:
        keys.add("notice:" + match.group(1))
    if lot.get("notification_number"):
        keys.add("notice:" + str(lot["notification_number"]))
    if lot.get("cadastral_number"):
        keys.add("cadr:" + str(lot["cadastral_number"]))
    if lot.get("card_url"):
        keys.add("url:" + str(lot["card_url"]).split("?")[0])
    return keys


def _fetch_card(source_client, lot: dict, cfg: dict, logger) -> None:
    """Загружает карточку лота и (опционально) текст документов (раздел 7.2)."""
    source_client.get_lot_detail(lot)
    if cfg["output"].get("download_documents"):
        docs_dir = resolve_path("output/docs") / str(lot["id"])[:60]
        max_docs = int(cfg["output"].get("max_documents_per_lot", 5))
        for document in (lot.get("documents") or [])[:max_docs]:
            document["text"] = source_client.download_document_text(document, docs_dir)
            rand_delay(cfg["request"]["delay_min_seconds"],
                       cfg["request"]["delay_max_seconds"])


def _record_to_state(lot: dict) -> dict:
    """Подготовка записи лота к JSON-сохранению (дедупликация запусков)."""
    record = {k: v for k, v in lot.items() if not k.startswith("_")}
    record["coords"] = list(lot["coords"]) if lot.get("coords") else None
    record["deadline"] = lot["deadline"].isoformat() if lot.get("deadline") else None
    record["checked_at"] = lot.get("checked_at") or iso_now()
    return record


def _record_from_state(record: dict) -> dict:
    """Восстановление записи лота из хранилища состояния."""
    lot = copy.deepcopy(record)
    coords = lot.get("coords")
    lot["coords"] = (float(coords[0]), float(coords[1])) if coords else None
    lot["deadline"] = parse_iso_datetime(lot.get("deadline"))
    lot.setdefault("documents", [])
    lot.setdefault("attrs", {})
    lot.setdefault("extra", {})
    lot.setdefault("reject_reasons", [])
    lot.setdefault("review_reasons", [])
    lot.setdefault("sheet", SHEET_REVIEW)
    return lot


def _collect_from_all_sources(cfg, logger):
    """Сбор лотов со всех источников (раздел 5 ТЗ).

    :return: (per_source — список (клиент, лоты), source_used — строка-описание)
    """
    source_clients = [get_search_source(cfg, logger)] + get_reserve_clients(cfg, logger)
    per_source = []
    for client in source_clients:
        try:
            lots = client.search_lots()
            per_source.append((client, lots))
            logger.info("Источник %s: получено лотов: %d", client.name, len(lots))
        except SourceUnavailableError as exc:
            logger.warning("Источник %s недоступен: %s — продолжаю с другими "
                           "источниками", client.name, exc)
        except Exception as exc:
            # logger.exception пишет ПОЛНЫЙ стек ошибки в search.log —
            # по нему можно определить точное место сбоя, не воспроизводя его.
            logger.exception("Непредвиденная ошибка источника %s: %s", client.name, exc)
    return per_source


def _merge_sources(per_source, logger):
    """Объединяет лоты всех источников, исключая дубликаты.

    Приоритет у основного источника (ГИС «Торги») — первые в списке.

    :return: (raw_lots, lot_clients — {id лота: клиент-владелец},
              source_used — строка «источник (N) + …»)
    """
    raw_lots = []
    lot_clients = {}
    seen_keys = set()
    source_used_parts = []
    for client, lots in per_source:
        kept = 0
        for lot in lots:
            keys = _lot_identity_keys(lot)
            if keys & seen_keys:
                continue                      # дубликат из другого источника
            seen_keys |= keys
            raw_lots.append(lot)
            lot_clients[lot["id"]] = client
            kept += 1
        source_used_parts.append(f"{client.name} ({kept})")
        logger.info("Источник %s: лотов %d, уникальных после объединения: %d",
                    client.name, len(lots), kept)
    source_used = " + ".join(source_used_parts) if source_used_parts else "—"
    return raw_lots, lot_clients, source_used


def main() -> int:
    """Точка входа бота. Возвращает код завершения процесса."""
    cfg_path = None
    if len(sys.argv) > 2 and sys.argv[1] == "--config":
        cfg_path = sys.argv[2]
    cfg = load_config(cfg_path)
    logger = setup_logging(cfg)

    run_started_at = iso_now()
    logger.info("=== Запуск бота поиска участков ИЖС (%s) ===", run_started_at)
    logger.info("Регион: %s | Сделка: %s | Площадь от %s м² | Аренда до %s руб./год",
                cfg["region"], cfg["deal_type"], cfg["min_area_m2"],
                cfg["max_rent_per_year"])

    errors = 0
    store = LotStateStore()
    lot_filter = LotFilter(cfg, logger)

    # ------------------------------------------------------------------
    # 1. Сбор лотов со ВСЕХ источников и объединение (раздел 5 ТЗ)
    # ------------------------------------------------------------------
    per_source = _collect_from_all_sources(cfg, logger)
    if not per_source:
        logger.error("Все источники данных недоступны. Бот завершает работу.")
        _log_summary(logger, found_count=0, source_used="недоступны",
                     errors=errors, report_path=None)
        return 1

    raw_lots, lot_clients, source_used = _merge_sources(per_source, logger)
    found_count = len(raw_lots)
    logger.info("Найдено лотов всего: %d (источники: %s)", found_count, source_used)

    # ------------------------------------------------------------------
    # 2. Первичная фильтрация списка (раздел 7.1 ТЗ)
    # ------------------------------------------------------------------
    candidates = []
    for lot in raw_lots:
        try:
            if lot.get("source") == "torgi.gov.ru":
                # в строках выдачи уже есть регион, статус, тип сделки
                passed, reasons = lot_filter.primary_check(lot)
                if passed:
                    candidates.append(lot)
                else:
                    lot["reject_reasons"] = reasons
                    lot["sheet"] = SHEET_REJECT
                    logger.info("Лот %s отклонён на первичной фильтрации: %s",
                                lot["id"], "; ".join(reasons))
            else:
                # резервный источник: регион подтверждён пре-фильтром каталога,
                # статус и цена неизвестны до карточки — проверим после неё
                candidates.append(lot)
        except Exception as exc:
            errors += 1
            logger.error("Ошибка первичной фильтрации лота %s: %s",
                         lot.get("id"), exc)
            lot["review_reasons"] = [f"ошибка первичной фильтрации: {exc}"]
            lot["sheet"] = SHEET_REVIEW

    primary_passed = len(candidates)
    logger.info("Прошли первичную фильтрацию: %d", primary_passed)

    # Лимит карточек для резервных источников (после регионального
    # пре-фильтра: тратим лимит только на лоты Хабаровского края)
    reserve_max = int((cfg.get("sources") or {}).get("reserve_max_details", 30))
    by_source = {}
    for lot in candidates:
        client = lot_clients.get(lot["id"])
        if client is None:
            continue
        by_source.setdefault(client.name, (client, []))[1].append(lot)

    card_candidates = []
    for client_name, (client, lots_c) in by_source.items():
        if client_name.startswith("ГИС Торги"):
            card_candidates.extend(lots_c)     # основной источник без лимита
            continue

        def reserve_sort_key(lot):
            hit = _contains_any(" ".join([lot.get("name", ""),
                                          lot.get("description", ""),
                                          str((lot.get("attrs") or {}).get(
                                              "Регион (источник)", ""))]),
                                cfg["location_keywords"])
            return (0 if hit else 1, 0 if lot.get("area_m2") else 1)

        lots_c.sort(key=reserve_sort_key)
        if len(lots_c) > reserve_max:
            logger.info("Резервный источник %s: проверка карточек ограничена "
                        "%d лотами (sources.reserve_max_details)",
                        client_name, reserve_max)
            lots_c = lots_c[:reserve_max]
        card_candidates.extend(lots_c)

    # ------------------------------------------------------------------
    # 3. Проверка карточек лотов (раздел 7.2 ТЗ)
    # ------------------------------------------------------------------
    cards_checked = 0
    for lot in card_candidates:
        client = lot_clients.get(lot["id"])
        if client is None:                     # теоретически невозможно
            continue
        try:
            _fetch_card(client, lot, cfg, logger)
            cards_checked += 1

            # резервный источник: теперь регион и статус известны
            if lot.get("source") != "torgi.gov.ru":
                passed, reasons = lot_filter.primary_check(lot)
                if not passed:
                    lot["reject_reasons"] = reasons
                    lot["sheet"] = SHEET_REJECT
                    logger.info("Лот %s отклонён после карточки: %s",
                                lot["id"], "; ".join(reasons))
                    continue

            lot_filter.full_check(lot)
            lot["sheet"] = lot_filter.assign_sheet(lot)
            if lot["sheet"] == SHEET_FIT:
                logger.info("Лот %s — ПОДХОДИТ (%s)",
                            lot["id"], lot.get("address") or lot.get("name"))
        except SourceUnavailableError as exc:
            errors += 1
            logger.error("Карточка лота %s не загрузилась: %s", lot.get("id"), exc)
            lot.setdefault("reject_reasons", [])
            lot["review_reasons"] = ["карточка лота не загрузилась (источник недоступен)"]
            lot["sheet"] = SHEET_REVIEW
        except Exception as exc:
            errors += 1
            logger.error("Ошибка обработки карточки лота %s: %s", lot.get("id"), exc)
            lot.setdefault("reject_reasons", [])
            lot["review_reasons"] = [f"ошибка обработки карточки: {exc}"]
            lot["sheet"] = SHEET_REVIEW

    logger.info("Проверено карточек: %d", cards_checked)

    # ------------------------------------------------------------------
    # 4. Дедупликация: обновление существующих записей (раздел 14 ТЗ)
    # ------------------------------------------------------------------
    for lot in raw_lots:
        try:
            lot["checked_at"] = iso_now()
            if lot.get("deadline"):
                lot["deadline"] = parse_iso_datetime(lot["deadline"])
            if lot.get("sheet") is None:
                lot["sheet"] = SHEET_REJECT
            record = _record_to_state(lot)
            store.upsert(lot["id"], record)
        except Exception as exc:
            errors += 1
            logger.error("Не удалось сохранить состояние лота %s: %s",
                         lot.get("id"), exc)

    try:
        store.save()
    except OSError as exc:
        errors += 1
        logger.error("Ошибка сохранения файла состояния: %s", exc)

    # ------------------------------------------------------------------
    # 5. Excel-отчёт (раздел 10.3 ТЗ) — данные собираются из всего состояния
    # ------------------------------------------------------------------
    all_lots = []
    for record in store.all_records().values():
        try:
            all_lots.append(_record_from_state(record))
        except Exception as exc:
            errors += 1
            logger.error("Ошибка восстановления записи лота: %s", exc)

    fitting = sum(1 for lot in all_lots if lot.get("sheet") == SHEET_FIT)
    review = sum(1 for lot in all_lots if lot.get("sheet") == SHEET_REVIEW)
    rejected = sum(1 for lot in all_lots if lot.get("sheet") == SHEET_REJECT)

    stats = {
        "run_started_at": run_started_at,
        "source_used": source_used,
        "found": found_count,
        "primary_passed": primary_passed,
        "cards_checked": cards_checked,
        "fitting": fitting,
        "review": review,
        "rejected": rejected,
        "total_in_state": len(all_lots),
        "errors": errors,
        "min_area_m2": cfg["min_area_m2"],
        "max_rent_per_year": cfg["max_rent_per_year"],
        "max_distance_km": cfg["max_distance_km"],
    }

    report_path = None
    try:
        report_path = build_report(all_lots, stats, cfg)
        logger.info("Отчёт сохранён: %s", report_path)
    except PermissionError as exc:
        errors += 1
        logger.error("%s", exc)
    except Exception as exc:
        errors += 1
        logger.error("Ошибка сохранения Excel-отчёта: %s", exc)

    _log_summary(logger, found_count, primary_passed, cards_checked,
                 fitting, review, rejected, source_used, errors, report_path)
    return 0


def _log_summary(logger, found_count, primary_passed=0, cards_checked=0,
                 fitting=0, review=0, rejected=0, source_used="—", errors=0,
                 report_path=None):
    """Итоговые строки журнала (формат раздела 13 ТЗ)."""
    logger.info("Найдено лотов: %d", found_count)
    if primary_passed:
        logger.info("Прошли первичную фильтрацию: %d", primary_passed)
    if cards_checked:
        logger.info("Проверено карточек: %d", cards_checked)
    logger.info("Подходящих лотов: %d", fitting)
    logger.info("Требуют ручной проверки: %d", review)
    logger.info("Отклонено: %d", rejected)
    if errors:
        logger.warning("Ошибок в ходе работы: %d", errors)
    if report_path:
        logger.info("Отчёт сохранён: %s", report_path)


if __name__ == "__main__":
    sys.exit(main())
