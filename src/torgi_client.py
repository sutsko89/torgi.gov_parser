# -*- coding: utf-8 -*-
"""
Клиенты источников данных (разделы 5 и 11 ТЗ).

Основной источник — официальный JSON API ГИС «Торги»:
    GET https://torgi.gov.ru/new/api/public/lotcards/search  — список лотов;
    GET https://torgi.gov.ru/new/api/public/lotcards/{id}    — карточка лота;
    https://torgi.gov.ru/new/file-store/v1/{fileId}          — документы.

Резервные источники (раздел 5 ТЗ), используются ВСЕ, а не только первый:
    * goszemli.ru — региональный каталог /torgi/region/habarovskii-krai
      (только лоты Хабаровского края) + общие каталоги с пре-фильтром региона;
    * heveya.ru   — раздел «Аренда и продажа земельных участков», выборка
      только лотов с Хабаровским краем в названии.

SSL: torgi.gov.ru использует сертификаты национального УЦ Минцифры,
которых нет в стандартном наборе certifi (см. src/ssl_utils.py). Клиент
подключает сертификаты Минцифры и системное хранилище Windows; при
неудаче проверки сертификата — явно залогированный резервный режим без
проверки (request.ssl_fallback_insecure).

Правила обращения к сайтам: корректный User-Agent, случайные паузы между
запросами, повтор неудачных запросов, логирование HTTP-статусов.
"""

import random
import re
import ssl
import time

import httpx

from src.parser import (merge_goszemli_detail, merge_heveya_detail,
                        merge_torgi_detail, parse_goszemli_list_item,
                        parse_heveya_list_item, parse_torgi_search_row)
from src.ssl_utils import build_insecure_context, build_ssl_context
from src.utils import keyword_pattern, normalize_russian_text, rand_delay

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


def _is_ssl_verify_error(exc: BaseException) -> bool:
    """Определяет, вызвана ли ошибка неудачной проверкой SSL-сертификата."""
    seen = set()
    err = exc
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        if isinstance(err, ssl.SSLCertVerificationError):
            return True
        if "certificate verify failed" in str(err).lower():
            return True
        err = err.__cause__ or err.__context__
    return False


class BaseClient:
    """Общая логика HTTP-запросов: таймауты, повторы, паузы, журналирование,
    проверка SSL-сертификатов с многоступенчатым резервом."""

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
        self._ssl_context = None
        self._ssl_fallback_used = False

    # ------------------------------------------------------------------
    # SSL-контексты
    # ------------------------------------------------------------------

    def _get_ssl_context(self):
        """SSL-контекст: certifi + сертификаты Минцифры + хранилище Windows."""
        if self._ssl_context is None:
            self._ssl_context = build_ssl_context(self.log)
        return self._ssl_context

    def _insecure_allowed(self) -> bool:
        return bool((self.cfg.get("request") or {}).get("ssl_fallback_insecure", True))

    def _switch_to_insecure(self, exc) -> None:
        """Однократное переключение на запрос без проверки сертификата."""
        if self._ssl_fallback_used:
            return False
        if not self._insecure_allowed():
            return False
        self._ssl_fallback_used = True
        self.log.warning("%s: не удалось проверить SSL-сертификат (%s)", self.name, exc)
        self.log.warning("%s: продолжаю БЕЗ проверки сертификата "
                         "(request.ssl_fallback_insecure=true). Для защищённой работы "
                         "установите сертификаты Минцифры с https://gu-st.ru",
                         self.name)
        self._ssl_context = build_insecure_context()
        return True

    def _open(self, url, params=None):
        """GET-запрос с текущим SSL-контекстом. Возвращает httpx.Response."""
        with httpx.Client(timeout=self.timeout, follow_redirects=True,
                          headers=self.headers, verify=self._get_ssl_context()) as client:
            return client.get(url, params=params)

    # ------------------------------------------------------------------
    # Запросы с повторами
    # ------------------------------------------------------------------

    def _get(self, url, params=None, expect="json"):
        """GET-запрос с повторами (разделы 10.1 и 11.3 ТЗ).

        При ошибке проверки сертификата один раз переключается на контекст
        без проверки (если разрешено конфигурацией) без расхода попыток.
        """
        retries = int(self.cfg["request"]["retries"])
        last_error = None
        attempt = 0
        while attempt < retries:
            try:
                response = self._open(url, params)
                self.log.debug("%s: GET %s -> HTTP %s", self.name, url,
                               response.status_code)
                if response.status_code == 200:
                    return response if expect == "raw" else response.json()
                last_error = SourceUnavailableError(f"HTTP {response.status_code}")
                self.log.warning("%s: HTTP %s (попытка %d/%d)", self.name,
                                 response.status_code, attempt + 1, retries)
            except (httpx.HTTPError, ValueError) as exc:
                if _is_ssl_verify_error(exc) and self._switch_to_insecure(exc):
                    continue                       # попытка не расходуется
                last_error = exc
                self.log.warning("%s: ошибка запроса (%s), попытка %d/%d",
                                 self.name, exc, attempt + 1, retries)
            except Exception as exc:
                # Непредвиденная ошибка кода (TypeError и т.п.) — повторять
                # бессмысленно, она не «сетевая». Полный стек идёт в search.log,
                # чтобы причину можно было определить по журналу.
                self.log.exception("%s: непредвиденная ошибка запроса к %s",
                                   self.name, url)
                raise SourceUnavailableError(
                    f"{self.name}: {type(exc).__name__}: {exc}") from exc
            attempt += 1
            if attempt < retries:
                delay = 2 ** attempt + random.uniform(0.5, 1.5)
                self.log.info("%s: повтор запроса через %.1f сек.", self.name, delay)
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
                              headers=self.headers, verify=self._get_ssl_context()) as client:
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


# ---------------------------------------------------------------------------
# Резервный источник goszemli.ru
# ---------------------------------------------------------------------------

class GoszemliClient(BaseClient):
    """Резервный источник: каталог земельных торгов goszemli.ru.

    Загружает РЕГИОНАЛЬНЫЙ каталог (только лоты Хабаровского края) и
    общие каталоги с пре-фильтром по региону: лоты других субъектов
    отсекаются до загрузки карточек — не тратятся запросы и время.
    """

    name = "goszemli.ru (резервный источник)"

    GENERAL_PAGES = ["https://goszemli.ru/torgi/arenda",
                     "https://goszemli.ru/torgi/naznachenie/izhs"]

    def region_page_url(self) -> str:
        slug = ((self.cfg.get("sources") or {}).get("goszemli_region_slug")
                or "habarovskii-krai")
        return f"https://goszemli.ru/torgi/region/{slug}"

    def search_lots(self):
        """Получает лоты Хабаровского края: региональная страница каталога
        (JSON-LD ItemList) + общие каталоги с пре-фильтром по региону."""
        lots: dict = {}

        # --- 1. Региональный каталог (все лоты уже относятся к региону) ----
        region_url = self.region_page_url()
        try:
            self.log.info("%s: загрузка регионального каталога %s", self.name, region_url)
            response = self._get(region_url, expect="raw")
            items = self._extract_items(response.text)
            added = 0
            for url, name in items:
                lot = parse_goszemli_list_item(url, name)
                lot["attrs"]["Регион (источник)"] = \
                    f"{self.cfg['region']} (региональный каталог goszemli.ru)"
                lots[lot["card_url"]] = lot
                added += 1
            self.log.info("%s: региональный каталог — лотов Хабаровского края: %d",
                          self.name, added)
        except SourceUnavailableError as exc:
            self.log.warning("%s: региональный каталог недоступен (%s) — "
                             "использую общие каталоги", self.name, exc)

        # --- 2. Общие каталоги с пре-фильтром по региону -------------------
        general_added = self._search_general_catalogs(lots)
        self.log.info("%s: всего лотов Хабаровского края: %d (из общих каталогов: %d)",
                      self.name, len(lots), general_added)
        return list(lots.values())

    def _extract_items(self, html_text: str):
        """JSON-LD ItemList со страницы каталога (запасной путь — <a href>)."""
        import json as _json

        from bs4 import BeautifulSoup

        items = []
        soup = BeautifulSoup(html_text, "lxml")
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
        return items

    def _slug_matches_region(self, url: str, name: str) -> bool:
        """Пре-фильтр региона по латинскому slug и названию до запроса карточки.

        goszemli.ru транслитерирует регион в адрес лота
        («...-habarovskii-krai»), поэтому проверяем латинские ключи
        (region_slug_keywords) и русские названия региона в заголовке.
        """
        keywords = (self.cfg.get("region_slug_keywords")
                    or ["khabarovsk", "habarovsk"])
        url_low = str(url).lower()
        name_low = normalize_russian_text(name)
        for keyword in keywords:
            if keyword.lower() in url_low:
                return True
        if keyword_pattern("Хабаровск").search(name_low):
            return True
        return False

    def _search_general_catalogs(self, lots: dict) -> int:
        """Общие каталоги: берём только лоты с Хабаровском в slug/названии."""
        added = 0
        for page_url in self.GENERAL_PAGES:
            try:
                self.log.info("%s: загрузка общего каталога %s", self.name, page_url)
                response = self._get(page_url, expect="raw")
            except SourceUnavailableError as exc:
                self.log.warning("%s: каталог %s недоступен: %s",
                                 self.name, page_url, exc)
                continue
            try:
                items = self._extract_items(response.text)
            except Exception as exc:
                self.log.warning("%s: не удалось разобрать %s: %s",
                                 self.name, page_url, exc)
                items = []
            matched = 0
            for url, name in items:
                if url in lots:                 # уже есть из регионального каталога
                    continue
                if not self._slug_matches_region(url, name):
                    continue                    # чужой регион — карточку не загружаем
                lot = parse_goszemli_list_item(url, name)
                lots[lot["card_url"]] = lot
                added += 1
                matched += 1
            self.log.info("%s: каталог %s — лотов %d, из них Хабаровский край: %d",
                          self.name, page_url, len(items), matched)
            rand_delay(self.cfg["request"]["delay_min_seconds"],
                       self.cfg["request"]["delay_max_seconds"])
        return added

    def get_lot_detail(self, lot: dict):
        """Загружает карточку лота goszemli.ru и дополняет запись."""
        rand_delay(self.cfg["request"]["delay_min_seconds"],
                   self.cfg["request"]["delay_max_seconds"])
        response = self._get(lot["card_url"], expect="raw")
        return merge_goszemli_detail(lot, response.text)


# ---------------------------------------------------------------------------
# Резервный источник heveya.ru
# ---------------------------------------------------------------------------

class HeveyaClient(BaseClient):
    """Резервный источник: раздел «Аренда и продажа земельных участков»
    heveya.ru. Сервер отдаёт каталог с названиями лотов, регион виден в
    названии («Земельный участок 939 кв.м, Хабаровский край») — пре-фильтр
    по названию выполняется ДО загрузки карточек."""

    name = "heveya.ru (резервный источник)"

    CATALOG_URL = "https://heveya.ru/arenda-i-prodazha-zemelnykh-uchastkov"
    CATALOG_PAGE_URL = CATALOG_URL + "?direction%5B0%5D=lease_and_sale_of_land&page={page}"
    _LOT_LINK_RE = re.compile(
        r'<a[^>]+href="(https?://heveya\.ru/lot/\d+)"[^>]*>(.*?)</a>', re.S | re.I)

    def _catalog_page(self, page: int) -> str:
        if page <= 1:
            return self.CATALOG_URL
        return self.CATALOG_PAGE_URL.format(page=page)

    def search_lots(self):
        """Каталог раздела (несколько страниц) → ссылки и названия →
        пре-фильтр по региону ДО загрузки карточек."""
        from src.normalizer import clean_html

        max_pages = int((self.cfg.get("sources") or {}).get("heveya_max_catalog_pages", 6))
        items, seen = [], set()
        for page in range(1, max_pages + 1):
            page_url = self._catalog_page(page)
            self.log.info("%s: загрузка каталога (страница %d/%d)",
                          self.name, page, max_pages)
            response = self._get(page_url, expect="raw")
            page_items = 0
            for match in self._LOT_LINK_RE.finditer(response.text):
                url, title = match.group(1), clean_html(match.group(2))
                url = url.rstrip("/").split("?")[0]
                if url in seen:
                    continue
                seen.add(url)
                items.append((url, title))
                page_items += 1
            self.log.debug("%s: страница %d — лотов %d", self.name, page, page_items)
            if page_items == 0:
                break                            # дальше лотов нет
            if page < max_pages:
                rand_delay(self.cfg["request"]["delay_min_seconds"],
                           self.cfg["request"]["delay_max_seconds"])
        self.log.info("%s: лотов в каталоге: %d", self.name, len(items))

        lots = []
        for url, title in items:
            if not keyword_pattern("Хабаровск").search(normalize_russian_text(title)):
                continue                        # чужой регион — карточку не загружаем
            lot = parse_heveya_list_item(url, title)
            lots.append(lot)
        self.log.info("%s: лотов Хабаровского края: %d", self.name, len(lots))
        return lots

    def get_lot_detail(self, lot: dict):
        """Загружает карточку лота heveya.ru и дополняет запись."""
        rand_delay(self.cfg["request"]["delay_min_seconds"],
                   self.cfg["request"]["delay_max_seconds"])
        response = self._get(lot["card_url"], expect="raw")
        return merge_heveya_detail(lot, response.text)


# ---------------------------------------------------------------------------
# Выбор источников данных (раздел 5 ТЗ)
# ---------------------------------------------------------------------------

CLIENTS = {
    "torgi": TorgiClient,
    "goszemli": GoszemliClient,
    "heveya": HeveyaClient,
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
