# -*- coding: utf-8 -*-
"""
Клиенты источников данных (разделы 5 и 11 ТЗ).

Основной источник — официальный JSON API ГИС «Торги»:
    GET https://torgi.gov.ru/new/api/public/lotcards/search  — список лотов;
    GET https://torgi.gov.ru/new/api/public/lotcards/{id}    — карточка лота;
    https://torgi.gov.ru/new/file-store/v1/{fileId}          — документы.

Резервный источник (при недоступности основного) — goszemli.ru:
    каталог /torgi/arenda и карточки лотов (JSON-LD schema.org).

Правила обращения к сайту: корректный User-Agent, случайные паузы между
запросами, повтор неудачных запросов, логирование HTTP-статусов.
Задача — надёжное получение официально опубликованных данных, а не обход
защитных механизмов.
"""

import random
import time

import httpx

from src.parser import merge_goszemli_detail, merge_torgi_detail, parse_torgi_search_row
from src.utils import rand_delay

# Справочные константы API ГИС «Торги»
TORGI_SEARCH_URL = "https://torgi.gov.ru/new/api/public/lotcards/search"
TORGI_LOT_URL = "https://torgi.gov.ru/new/api/public/lotcards/"
TORGI_FILE_URL = "https://torgi.gov.ru/new/file-store/v1/"

# Категории лотов ГИС «Торги», относящиеся к земельным участкам
# (справочник catCode): 2 — «Земельные участки», 301 — «Земли населённых
# пунктов», 3 — «категория не установлена», 307 — «не образованы».
LAND_CATEGORY_CODES = ["2", "301", "3", "307"]


class SourceUnavailableError(Exception):
    """Источник данных недоступен после всех повторов запроса."""


class BaseClient:
    """Общая логика HTTP-запросов: таймауты, повторы, паузы, журналирование."""

    name = "базовый источник"

    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.log = logger
        self.headers = {
            "User-Agent": cfg["user_agent"],
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        self.timeout = float(cfg["request"]["timeout_seconds"])

    def _get(self, url, params=None, expect="json"):
        """GET-запрос с повторами (разделы 10.1 и 11.3 ТЗ)."""
        retries = int(self.cfg["request"]["retries"])
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True,
                                  headers=self.headers) as client:
                    response = client.get(url, params=params)
                self.log.debug("%s: GET %s -> HTTP %s", self.name, url, response.status_code)
                if response.status_code == 200:
                    return response if expect == "raw" else response.json()
                if response.status_code in (403, 412, 429):
                    # явная блокировка/ограничение доступа
                    last_error = SourceUnavailableError(
                        f"HTTP {response.status_code} — доступ ограничен")
                    self.log.warning("%s: HTTP %s (попытка %d/%d)", self.name,
                                     response.status_code, attempt, retries)
                else:
                    last_error = SourceUnavailableError(
                        f"HTTP {response.status_code}")
                    self.log.warning("%s: HTTP %s (попытка %d/%d)", self.name,
                                     response.status_code, attempt, retries)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                self.log.warning("%s: ошибка запроса (%s), попытка %d/%d",
                                 self.name, exc, attempt, retries)
            if attempt < retries:
                delay = 2 ** attempt + random.uniform(0.5, 1.5)
                self.log.info("%s: повтор запроса через %.1f сек.",
                              self.name, delay)
                time.sleep(delay)
        raise SourceUnavailableError(f"{self.name}: {last_error}")


class TorgiClient(BaseClient):
    """Клиент официального API ГИС «Торги» (основной источник)."""

    name = "ГИС Торги (torgi.gov.ru)"

    def search_lots(self):
        """
        Получает список актуальных лотов: аренда земельных участков в
        Хабаровском крае со статусами «Опубликован»/«Приём заявок».
        Обрабатывает несколько страниц результата (раздел 10.1 ТЗ).
        """
        request = self.cfg["request"]
        params = {
            "dynSubjRF": self.cfg["region_code"],          # 31 — Хабаровский край
            "typeTransaction": self.cfg["type_transaction_code"],  # rent — аренда
            "lotStatus": ",".join(self.cfg["lot_statuses"]),
            "catCode": ",".join(LAND_CATEGORY_CODES),      # земельные категории
            "byFirstVersion": "true",
            "withFacets": "true",
            "size": int(request["page_size"]),
            "sort": "firstVersionPublicationDate,desc",
        }
        lots, seen_ids = [], set()
        max_pages = int(request["max_pages"])
        page = 0
        total_pages = None

        while page < max_pages:
            if page:
                params["page"] = page
                params["withFacets"] = "false"
            self.log.info("%s: запрос страницы %s", self.name, page)
            data = self._get(TORGI_SEARCH_URL, params=params)

            content = data.get("content") or []
            if data.get("number") != page:
                self.log.warning("%s: API вернул страницу %s вместо %s — остановка",
                                 self.name, data.get("number"), page)
                break
            if total_pages is None:
                total_pages = int(data.get("totalPages") or 0)
                self.log.info("%s: всего лотов по запросу: %s (страниц: %s)",
                              self.name, data.get("totalElements"), total_pages)

            for row in content:
                lot_id = str(row.get("id") or "")
                if not lot_id or lot_id in seen_ids:
                    continue                       # защита от дубликатов страниц
                seen_ids.add(lot_id)
                try:
                    lots.append(parse_torgi_search_row(row, TORGI_FILE_URL))
                except Exception as exc:           # ошибка разбора одного лота
                    self.log.error("%s: не удалось разобрать лот %s: %s",
                                   self.name, lot_id, exc)

            self.log.info("%s: страница %s — получено лотов: %d (всего собрано %d)",
                          self.name, page, len(content), len(lots))
            if data.get("last") or not content or page >= total_pages - 1:
                break
            page += 1
            rand_delay(request["delay_min_seconds"], request["delay_max_seconds"])

        return lots

    def get_lot_detail(self, lot: dict):
        """Загружает карточку лота (раздел 7.2 ТЗ) и дополняет запись."""
        rand_delay(self.cfg["request"]["delay_min_seconds"],
                   self.cfg["request"]["delay_max_seconds"])
        detail = self._get(TORGI_LOT_URL + str(lot["id"]))
        return merge_torgi_detail(lot, detail, TORGI_FILE_URL)

    def download_document_text(self, document, output_dir):
        """Скачивает PDF-документ и извлекает текст для проверки рисков.

        Включается параметром output.download_documents: true в config.yaml.
        Возвращает текст или None; ошибки не прерывают работу бота.
        """
        from pathlib import Path

        from src.utils import safe_filename

        url = document.get("url") or ""
        name = document.get("name") or "документ"
        if not url.startswith(TORGI_FILE_URL):
            return None
        if not name.lower().endswith(".pdf"):
            return None                        # текстовый разбор поддержан для PDF
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        local_name = safe_filename(name)
        if not Path(local_name).suffix:
            local_name += ".pdf"
        local_path = output_dir / local_name
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True,
                              headers=self.headers) as client:
                response = client.get(url)
            if response.status_code != 200:
                self.log.warning("%s: документ %s недоступен (HTTP %s)",
                                 self.name, name, response.status_code)
                return None
            max_bytes = float(self.cfg["output"].get("max_document_size_mb", 10)) * 1e6
            if len(response.content) > max_bytes:
                self.log.info("%s: документ %s пропущен (превышен лимит размера)",
                              self.name, name)
                return None
            local_path.write_bytes(response.content)
        except (httpx.HTTPError, OSError) as exc:
            self.log.warning("%s: не удалось скачать документ %s: %s",
                             self.name, name, exc)
            return None
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(local_path))
            text = " ".join((page.extract_text() or "") for page in reader.pages[:20])
            return text[:20000]
        except Exception as exc:
            self.log.debug("%s: не удалось извлечь текст из %s: %s",
                           self.name, name, exc)
            return None


class GoszemliClient(BaseClient):
    """Резервный источник: каталог земельных торгов goszemli.ru."""

    name = "goszemli.ru (резервный источник)"

    SEARCH_PAGES = ["https://goszemli.ru/torgi/arenda",
                    "https://goszemli.ru/torgi/naznachenie/izhs"]

    def search_lots(self):
        """Получает список лотов каталога (JSON-LD ItemList на страницах)."""
        import json as _json

        from bs4 import BeautifulSoup

        lots, seen = [], set()
        for page_url in self.SEARCH_PAGES:
            try:
                self.log.info("%s: загрузка каталога %s", self.name, page_url)
                response = self._get(page_url, expect="raw")
                soup = BeautifulSoup(response.text, "lxml")
            except SourceUnavailableError:
                raise
            except Exception as exc:
                self.log.warning("%s: не удалось разобрать %s: %s",
                                 self.name, page_url, exc)
                continue

            items = []
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = _json.loads(script.string or script.get_text() or "")
                except (ValueError, TypeError):
                    continue
                blocks = data if isinstance(data, list) else [data]
                for block in blocks:
                    if isinstance(block, dict) and block.get("@type") == "ItemList":
                        for elem in block.get("itemListElement") or []:
                            if isinstance(elem, dict) and elem.get("url"):
                                items.append((elem["url"], elem.get("name") or ""))
            if not items:
                # запасной путь: ссылки на лоты прямо из разметки
                for a in soup.find_all("a", href=True):
                    if "/torgi/lot/" in a["href"]:
                        items.append((a["href"], a.get_text(strip=True)))

            for url, name in items:
                if url in seen:
                    continue
                seen.add(url)
                if url.startswith("/"):
                    url = "https://goszemli.ru" + url
                from src.parser import parse_goszemli_list_item
                lots.append(parse_goszemli_list_item(url, name))
            rand_delay(self.cfg["request"]["delay_min_seconds"],
                       self.cfg["request"]["delay_max_seconds"])

        self.log.info("%s: получено лотов в каталоге: %d", self.name, len(lots))
        return lots

    def get_lot_detail(self, lot: dict):
        """Загружает карточку лота goszemli.ru и дополняет запись."""
        rand_delay(self.cfg["request"]["delay_min_seconds"],
                   self.cfg["request"]["delay_max_seconds"])
        response = self._get(lot["card_url"], expect="raw")
        return merge_goszemli_detail(lot, response.text)


# ---------------------------------------------------------------------------
# Выбор источника данных (раздел 5 ТЗ)
# ---------------------------------------------------------------------------

CLIENTS = {
    "torgi": TorgiClient,
    "goszemli": GoszemliClient,
}


def get_search_source(cfg, logger):
    """Возвращает клиент основного источника данных."""
    primary = (cfg.get("sources") or {}).get("primary", "torgi")
    client_cls = CLIENTS.get(primary)
    if not client_cls:
        raise ValueError(f"Неизвестный основной источник: {primary}")
    return client_cls(cfg, logger)


def get_reserve_clients(cfg, logger):
    """Возвращает список клиентов резервных источников (по порядку)."""
    reserve_names = (cfg.get("sources") or {}).get("reserve", []) or []
    clients = []
    for name in reserve_names:
        client_cls = CLIENTS.get(name)
        if client_cls:
            clients.append(client_cls(cfg, logger))
        else:
            logger.warning("Резервный источник «%s» не реализован — пропущен", name)
    return clients
