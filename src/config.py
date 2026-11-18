# -*- coding: utf-8 -*-
"""
Загрузка конфигурации из config.yaml.

Все критерии поиска хранятся в конфигурации (раздел 12 ТЗ): изменение
критериев не должно требовать правки программного кода. Здесь выполняются
чтение файла, подстановка значений по умолчанию и простейшие проверки.
"""

from pathlib import Path

import yaml

# Корень проекта: каталог на уровень выше папки src/
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Значения по умолчанию — применяются, если параметра нет в config.yaml.
# Пользовательский config.yaml всегда главнее значений по умолчанию.
DEFAULTS = {
    "region": "Хабаровский край",
    "region_code": "31",
    "region_subject_codes": ["27", "31"],
    "deal_type": "аренда",
    "type_transaction_code": "rent",
    "status": "приём заявок",
    "lot_statuses": ["PUBLISHED", "APPLICATIONS_SUBMISSION"],
    "min_area_m2": 600,
    "max_rent_per_year": 180000,
    "max_rent_per_month": 15000,
    "max_distance_km": 50,
    "center": {"lat": 48.4814, "lon": 135.0721},
    "vri_keywords": ["индивидуальное жилищное строительство", "ижс"],
    "location_keywords": ["Хабаровск", "Хабаровский район"],
    "hard_reject_keywords": ["арест", "залог", "изъятие для государственных нужд",
                             "изъятие для муниципальных нужд"],
    "review_keywords": ["сервитут", "охранная зона", "водоохранная зона"],
    "negation_keywords": ["не наложен", "отсутствует", "отсутствуют", "отсутствие",
                          "не установлено", "не установлены"],
    "access_keywords": ["подъезд", "дорога", "проезд"],
    "utilities_keywords": ["электроснабжение", "водоснабжение", "вода",
                           "коммуникации", "технические условия"],
    "output": {
        "excel_file": "output/land_lots_report.xlsx",
        "log_file": "search.log",
        "dated_copy": True,
        "download_documents": False,
        "max_documents_per_lot": 5,
        "max_document_size_mb": 10,
    },
    "request": {
        "delay_min_seconds": 2,
        "delay_max_seconds": 5,
        "retries": 3,
        "timeout_seconds": 30,
        "page_size": 50,
        "max_pages": 40,
    },
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) torgi-land-bot/1.0",
    "geocoding": {
        "enabled": True,
        "use_nspd": True,
        "use_nominatim": True,
        "nominatim_user_agent": "torgi-land-bot/1.0",
        "nominatim_delay_seconds": 1.1,
    },
    "sources": {"primary": "torgi", "reserve": ["goszemli"],
                "reserve_max_details": 40},
    "logging": {"level": "INFO"},
}


def _deep_merge(base: dict, extra: dict) -> dict:
    """Рекурсивно объединяет словари: значения extra главнее значений base."""
    result = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path=None) -> dict:
    """
    Загружает конфигурацию бота.

    :param config_path: путь к YAML-файлу (относительно корня проекта или
                        абсолютный). По умолчанию — config.yaml в корне.
    :return: словарь настроек с подставленными значениями по умолчанию.
    """
    if config_path is None:
        path = PROJECT_ROOT / "config.yaml"
    else:
        path = Path(config_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path

    user_cfg = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            user_cfg = yaml.safe_load(fh) or {}
    else:
        raise FileNotFoundError(
            f"Конфигурационный файл не найден: {path}. "
            f"Создайте config.yaml в корне проекта (см. README.md)."
        )

    cfg = _deep_merge(DEFAULTS, user_cfg)

    # Базовые проверки корректности значений
    center = cfg.get("center") or {}
    if not center.get("lat") or not center.get("lon"):
        raise ValueError("В config.yaml не заданы координаты центра (center.lat / center.lon).")

    req = cfg["request"]
    if int(req["retries"]) < 1:
        raise ValueError("request.retries должен быть не меньше 1.")
    if float(req["delay_max_seconds"]) < float(req["delay_min_seconds"]):
        raise ValueError("request.delay_max_seconds не может быть меньше delay_min_seconds.")

    return cfg


def resolve_path(rel_path, base_dir=None) -> Path:
    """Преобразует относительный путь в абсолютный от корня проекта.

    Кроссплатформенный способ работы с путями (раздел 11.1 ТЗ):
    жёстко прописанные Windows-пути не используются.
    """
    p = Path(rel_path)
    if p.is_absolute():
        return p
    root = base_dir or PROJECT_ROOT
    return root / p
