# -*- coding: utf-8 -*-
"""
Настройка журналирования (раздел 13 ТЗ).

Бот ведёт журнал в файле search.log (с временными метками) и одновременно
выводит понятные сообщения в консоль в формате: [INFO] Найдено лотов: 128.
"""

import logging
from pathlib import Path

from src.config import resolve_path

_FORMAT_CONSOLE = "[%(levelname)s] %(message)s"
_FORMAT_FILE = "%(asctime)s [%(levelname)s] %(message)s"


def setup_logging(cfg: dict) -> logging.Logger:
    """
    Создаёт logger «torgi_bot» с двумя обработчиками:
      * файл (output-параметр log_file, кодировка UTF-8);
      * консоль (компактный формат, как в примере ТЗ).

    Повторный вызов не добавляет дублирующие обработчики.
    """
    logger = logging.getLogger("torgi_bot")

    if logger.handlers:                      # уже настроен — не дублируем
        return logger

    level_name = (cfg.get("logging") or {}).get("level", "INFO")
    logger.setLevel(getattr(logging, str(level_name).upper(), logging.INFO))

    log_path: Path = resolve_path(cfg["output"]["log_file"])

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FORMAT_FILE))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, str(level_name).upper(), logging.INFO))
    console_handler.setFormatter(logging.Formatter(_FORMAT_CONSOLE))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False
    return logger
