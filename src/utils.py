# -*- coding: utf-8 -*-
"""
Вспомогательные функции: математика расстояний, геокодирование,
задержки между запросами, хранилище состояния для дедупликации запусков.
"""

import json
import math
import random
import re
import time
from datetime import datetime
from pathlib import Path

from src.config import resolve_path

# Регулярное выражение для кадастрового номера участка, например 27:05:0000001:123
CADASTRAL_RE = re.compile(r"\b\d{1,2}:\d{1,2}:\d{1,10}:\d{1,10}\b")


# ---------------------------------------------------------------------------
# Математика: расстояние по формуле гаверсинусов (раздел 8 ТЗ)
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками Земли в километрах (гаверсинусы)."""
    radius = 6371.0  # средний радиус Земли, км
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Время и задержки
# ---------------------------------------------------------------------------

def rand_delay(min_seconds: float, max_seconds: float) -> None:
    """Случайная пауза между запросами (вежливость к сайту, раздел 11.3 ТЗ)."""
    time.sleep(random.uniform(float(min_seconds), float(max_seconds)))


def iso_now() -> str:
    """Текущие дата и время в ISO-формате для поля «Дата проверки»."""
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def parse_iso_datetime(value):
    """Преобразует строку даты в datetime, возвращая None при неудаче."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:len(pattern) + 2].replace("Z", ""), pattern)
        except ValueError:
            continue
    # пробуем универсальный разбор ISO (например, '2026-07-03T15:00:00+10:00')
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Геокодирование (раздел 8 ТЗ, этап 2)
# ---------------------------------------------------------------------------

def epsg3857_to_wgs84(x: float, y: float):
    """Перевод координат Web Mercator (EPSG:3857) в широту/долготу WGS84."""
    a = 6378137.0
    lon = (x / a) * 180.0 / math.pi
    lat = (math.pi / 2 - 2 * math.atan(math.exp(-y / a))) * 180.0 / math.pi
    return lat, lon


def _flatten_coords(coords):
    """Приводит список координат полигона к плоскому виду [[x, y], ...]."""
    flat = []
    def walk(node):
        if isinstance(node, (list, tuple)) and node and isinstance(node[0], (int, float)):
            flat.append(node)
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
    walk(coords)
    return flat


def _center_of_geometry(geometry):
    """Вычисляет центр геометрии участка (полигон/мультиполигон/точка)."""
    if not isinstance(geometry, dict):
        return None
    gtype = (geometry.get("type") or "").lower()
    coords = geometry.get("coordinates")
    if gtype == "point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        return float(coords[1]), float(coords[0])          # (lat, lon)
    flat = _flatten_coords(coords)
    if not flat:
        return None
    xs = [float(p[0]) for p in flat]
    ys = [float(p[1]) for p in flat]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    if gtype and "3857" not in gtype and abs(cx) <= 180 and abs(cy) <= 90:
        return cy, cx                                       # уже WGS84
    return epsg3857_to_wgs84(cx, cy)


def geocode_by_nspd(cadastral_number: str, cfg: dict):
    """
    Поиск координат участка по кадастровому номеру через НСПД (nspd.gov.ru).
    Возвращает (lat, lon) или None. Ошибки не прерывают работу бота.
    """
    if not cadastral_number:
        return None
    if not (cfg.get("geocoding") or {}).get("use_nspd", True):
        return None
    try:
        import httpx
        url = "https://nspd.gov.ru/api/geoportal/v2/search/geoportal"
        params = {"thematicSearchId": 1, "query": cadastral_number}
        headers = {"User-Agent": cfg.get("user_agent")}
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
        features = ((data.get("data") or {}).get("features")) or []
        if not features:
            return None
        geometry = features[0].get("geometry")
        return _center_of_geometry(geometry)
    except Exception:
        # НСПД может быть недоступна — это не ошибка бота, молча переходим к
        # следующему способу геокодирования (основной ход фиксируется в логе).
        return None


def geocode_by_nominatim(address: str, cfg: dict):
    """
    Геокодирование по адресу через Nominatim (OpenStreetMap).
    Соблюдается лимит: не чаще одного запроса в секунду.
    """
    if not address:
        return None
    geo_cfg = cfg.get("geocoding") or {}
    if not geo_cfg.get("use_nominatim", True):
        return None
    try:
        from geopy.geocoders import Nominatim
        geolocator = Nominatim(
            user_agent=geo_cfg.get("nominatim_user_agent", "torgi-land-bot/1.0"),
            timeout=15,
        )
        time.sleep(float(geo_cfg.get("nominatim_delay_seconds", 1.1)))
        location = geolocator.geocode(address, country_codes="ru")
        if location:
            return location.latitude, location.longitude
    except Exception:
        return None
    return None


def distance_from_center(lat: float, lon: float, cfg: dict):
    """Расстояние в км от центра Хабаровска (задаётся в config.yaml)."""
    center = cfg["center"]
    return haversine_km(lat, lon, center["lat"], center["lon"])


# ---------------------------------------------------------------------------
# Хранилище состояния (дедупликация повторных запусков, разделы 10.1/14 ТЗ)
# ---------------------------------------------------------------------------

class LotStateStore:
    """
    Хранит записи обо всех ранее обработанных лотах в JSON-файле
    (output/state_lots.json). Ключ — идентификатор лота.

    Повторный запуск обновляет существующие записи, а не создаёт дубликаты.
    """

    def __init__(self, path=None):
        self.path: Path = path or resolve_path("output/state_lots.json")
        self.records: dict = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self.records = data
        except (json.JSONDecodeError, OSError):
            # Повреждённый файл состояния не должен останавливать бота
            self.records = {}

    def upsert(self, lot_id: str, record: dict) -> bool:
        """Добавляет новую запись или обновляет существующую.

        :return: True, если лот встречен впервые.
        """
        is_new = lot_id not in self.records
        old = self.records.get(lot_id) or {}
        record.setdefault("first_seen", old.get("first_seen", iso_now()))
        self.records[lot_id] = record
        return is_new

    def all_records(self) -> dict:
        """Все записи (для построения Excel-отчёта без дубликатов)."""
        return self.records

    def save(self) -> None:
        """Сохраняет состояние на диск; ошибки сохранения фиксируются в логе."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(self.records, fh, ensure_ascii=False, indent=1)
        tmp_path.replace(self.path)


def safe_filename(name: str, max_len: int = 80) -> str:
    """Превращает произвольную строку в безопасное имя файла."""
    cleaned = re.sub(r"[^\wА-Яа-яЁё\-. ]+", "_", str(name)).strip()
    return (cleaned[:max_len] or "file").rstrip(" ._")


# ---------------------------------------------------------------------------
# Поиск ключевых слов в русских текстах (падежи, ё/е, неразрывные пробелы)
# ---------------------------------------------------------------------------

def normalize_russian_text(text: str) -> str:
    """Приводит текст к виду, удобному для поиска: строчные буквы,
    ё → е, неразрывные и «умные» пробелы → обычные."""
    low = str(text or "").lower().replace("ё", "е")
    for ch in ("\u00a0", "\u202f", "\u2009", "\t", "\n", "\r"):
        low = low.replace(ch, " ")
    return re.sub(r"\s+", " ", low)


def keyword_pattern(keyword: str):
    """Регулярное выражение для ключевого слова/фразы с учётом падежей.

    Каждое слово сравнивается по основе (первые символы слова): слово
    «индивидуальное» из ключа совпадает с «индивидуального» в тексте.
    Короткие слова (до 4 символов, например «для», «ижс») сравниваются целиком.
    """
    words = normalize_russian_text(keyword).split(" ")
    parts = []
    for word in words:
        word = word.replace("ё", "е")
        if len(word) <= 4 or not word.isalpha():
            parts.append(re.escape(word))
        else:
            stem = word[:max(4, len(word) - 2)]
            parts.append(re.escape(stem) + r"\w*")
    return re.compile(r"[\s\-–—]*".join(parts), re.IGNORECASE)
