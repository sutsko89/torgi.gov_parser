# -*- coding: utf-8 -*-
"""
Проверка SSL-сертификатов российских государственных сайтов (раздел 11 ТЗ).

ГИС «Торги» (torgi.gov.ru) использует TLS-сертификаты национального УЦ
Минцифры (Russian Trusted Root CA / Russian Trusted Sub CA). Эти сертификаты
не входят в стандартный набор certifi, который использует Python, поэтому
без дополнительных настроек запросы к torgi.gov.ru падают с ошибкой
«CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate».

Решение (по возрастанию «мягкости»):
  1. certifi + официальные сертификаты Минцифры из каталога certs/ проекта
     (файлы russian_trusted_root_ca.pem / russian_trusted_sub_ca.pem);
  2. если каталога certs/ нет — автоматическая загрузка сертификатов с
     официальной страницы gu-st.ru (кэшируются в certs/);
  3. на Windows дополнительно подключается системное хранилище сертификатов
     (если пользователь установил сертификаты Минцифры в Windows);
  4. последний резерв (request.ssl_fallback_insecure: true в config.yaml) —
     запрос без проверки сертификата с явным предупреждением в журнале.
     Данные публичные и только для чтения, но безопасность лучше: установите
     сертификаты Минцифры (https://gu-st.ru) в систему.
"""

import ssl
from pathlib import Path

import certifi

from src.config import PROJECT_ROOT

CERTS_DIR = Path(PROJECT_ROOT) / "certs"

# Официальные источники сертификатов Минцифры (публичные корневые сертификаты)
RUSSIAN_CA_URLS = [
    "https://gu-st.ru/content/lending/russian_trusted_sub_ca.cer",
    "https://gu-st.ru/content/lending/russian_trusted_root_ca.cer",
]


def _load_pem_dir(context: ssl.SSLContext, directory: Path) -> int:
    """Загружает все .pem файлы каталога в SSL-контекст. Возвращает число файлов."""
    loaded = 0
    if not directory.exists():
        return 0
    for pem in sorted(directory.glob("*.pem")):
        try:
            context.load_verify_locations(cafile=str(pem))
            loaded += 1
        except (ssl.SSLError, OSError):
            continue
    return loaded


def _add_windows_system_store(context: ssl.SSLContext) -> int:
    """Добавляет сертификаты системного хранилища Windows (только Windows).

    Помогает, когда пользователь установил сертификаты Минцифры в Windows.
    На других ОС возвращает 0 (ничего не делает).
    """
    if not hasattr(ssl, "enum_certificates"):
        return 0  # не Windows
    added = 0
    for store_name in ("CA", "ROOT"):
        try:
            for flags, der_data, _enc_type in ssl.enum_certificates(store_name):
                try:
                    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(der_data))
                    added += 1
                except (ssl.SSLError, OSError):
                    continue
        except (ssl.SSLError, OSError):
            continue
    return added


def download_russian_certs(logger=None) -> int:
    """Загружает сертификаты Минцифры с gu-st.ru и кэширует их в certs/.

    :return: число успешно сохранённых файлов.
    """
    saved = 0
    try:
        import httpx
    except ImportError:  # httpx всегда есть в requirements
        return 0
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    for url in RUSSIAN_CA_URLS:
        filename = url.rsplit("/", 1)[-1]
        if not filename.endswith(".pem"):
            filename += ".pem"
        target = CERTS_DIR / filename
        if target.exists() and target.stat().st_size > 200:
            saved += 1
            continue
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                response = client.get(url)
            body = response.content
            if response.status_code != 200 or len(body) < 200:
                continue
            # сервер отдаёт PEM; на всякий случай конвертируем DER
            if b"BEGIN CERTIFICATE" not in body:
                try:
                    body = ssl.DER_cert_to_PEM_cert(body).encode("ascii")
                except Exception:
                    continue
            target.write_bytes(body)
            saved += 1
            if logger:
                logger.info("SSL: сохранён сертификат Минцифры %s", target.name)
        except Exception as exc:
            if logger:
                logger.debug("SSL: сертификат %s не загружен: %s", url, exc)
    return saved


def build_ssl_context(logger=None, include_windows_store: bool = True):
    """
    Строит SSL-контекст с максимально полным набором доверенных сертификатов:
    certifi + сертификаты Минцифры (каталог certs/, при отсутствии — загрузка)
    + системное хранилище Windows. При ошибке проверки сертификата
    BaseClient сам переключится на незащищённый режим (если разрешено).
    """
    context = ssl.create_default_context(cafile=certifi.where())
    loaded = _load_pem_dir(context, CERTS_DIR)
    if loaded == 0:
        # каталога certs/ нет или он пуст — пробуем скачать сертификаты
        downloaded = download_russian_certs(logger)
        if downloaded:
            loaded = _load_pem_dir(context, CERTS_DIR)
    if logger:
        logger.debug("SSL: загружено дополнительных сертификатов Минцифры: %d", loaded)
    if include_windows_store:
        _add_windows_system_store(context)
    return context


def build_insecure_context():
    """Контекст без проверки сертификата (последний резерв, см. модуль докстринг)."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context
