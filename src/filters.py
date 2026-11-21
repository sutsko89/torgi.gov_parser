# -*- coding: utf-8 -*-
"""
Фильтрация лотов (разделы 6, 7 и 8 ТЗ).

Первичная фильтрация (по данным списка лотов): регион, тип сделки, статус,
площадь, ВРИ, упоминание местоположения.
Проверка карточки: полный текст, документы, координаты и расстояние,
риски и ограничения, отсутствие сведений о подъезде/коммуникациях.
Цена арендной платы нормализуется только для колонок отчёта — ограничения
по цене сняты (max_rent_per_year: null в config.yaml).

Лот всегда получает ровно один итоговый статус:
  * «Подходящие»       — прошёл все обязательные фильтры;
  * «Требуют проверки» — есть потенциальные ограничения или неполные данные;
  * «Отклонённые»      — есть основания для исключения (с указанием причины).
"""

import re

from src.normalizer import normalize_price
from src.risk_check import RiskChecker
from src.utils import (distance_from_center, geocode_by_nominatim,
                       geocode_by_nspd, keyword_pattern, normalize_russian_text,
                       rand_delay)


def _contains_any(text, keywords):
    """Возвращает найденное ключевое слово (без учёта регистра, падежей
    и неразрывных пробелов) или None, если ничего не найдено."""
    if not text:
        return None
    low = normalize_russian_text(text)
    for keyword in keywords:
        if keyword_pattern(keyword).search(low):
            return keyword
    return None


class LotFilter:
    """Выполняет первичную и полную фильтрацию лотов."""

    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.log = logger
        self.risk_checker = RiskChecker(cfg)

    # ------------------------------------------------------------------
    # Первичная фильтрация (раздел 7.1 ТЗ) — по данным списка лотов
    # ------------------------------------------------------------------

    def primary_check(self, lot: dict):
        """
        Проверяет обязательные критерии по данным списка.

        :return: (прошёл_ли, причины_отклонения)
        """
        reasons = []
        text_blob = " ".join([lot.get("name", ""), lot.get("address", ""),
                              lot.get("description", ""),
                              str((lot.get("attrs") or {}).get("Регион (источник)", ""))])

        # 1. Регион
        region_ok = False
        if lot.get("source") == "torgi.gov.ru":
            # список уже запрошен с фильтром dynSubjRF=31, но проверим ответ
            if lot.get("subject_rf_code") in self.cfg["region_subject_codes"]:
                region_ok = True
            elif _contains_any(text_blob, [self.cfg["region"]]):
                region_ok = True
        else:
            region_ok = bool(_contains_any(text_blob, [self.cfg["region"],
                                                       "Хабаровск"]))
        if not region_ok:
            reasons.append("регион не подтверждён (не Хабаровский край)")

        # 2. Тип сделки — аренда
        deal = str(lot.get("deal_type_text") or "").lower()
        is_rent = deal == self.cfg["type_transaction_code"] or "аренда" in deal or \
            str((lot.get("attrs") or {}).get("Тип сделки", "")).lower().startswith("аренда")
        if not is_rent:
            reasons.append("тип сделки не «аренда»")

        # 3. Статус — опубликован / приём заявок
        status = str(lot.get("status_code") or "").upper()
        status_ok = status in [s.upper() for s in self.cfg["lot_statuses"]]
        if not status_ok:
            reasons.append(f"статус лота неактивен ({lot.get('status_name') or status or 'не определён'})")

        # 4. Площадь — если уже известна из списка (раздел 7.1 ТЗ)
        if lot.get("area_m2") is not None:
            if lot["area_m2"] < float(self.cfg["min_area_m2"]):
                reasons.append(f"площадь {lot['area_m2']:g} м² меньше минимальной "
                               f"{self.cfg['min_area_m2']:g} м²")

        # 5. ВРИ — если уже известен из списка: не ИЖС → отсев без запроса карточки
        vri_known = bool(lot.get("vri_text"))
        vri_is_izhs = bool(_contains_any(lot.get("vri_text", ""), self.cfg["vri_keywords"]) or
                           _contains_any(lot.get("description", ""), self.cfg["vri_keywords"]))
        if vri_known and not vri_is_izhs:
            reasons.append(f"ВРИ не соответствует ИЖС ({lot['vri_text'][:80]})")

        # 6. Местоположение (раздел 8 ТЗ, этап 1 — поиск по тексту)
        location_hit = _contains_any(text_blob, self.cfg["location_keywords"])
        lot["location_keyword"] = location_hit
        has_cadastral = bool(lot.get("cadastral_number"))
        if not location_hit and not has_cadastral and lot.get("source") != "goszemli.ru":
            # ни упоминания, ни кадастрового номера для геокодирования — отсев
            reasons.append("местоположение не указывает на Хабаровск/пригороды "
                           "(нет упоминания и кадастрового номера)")
        return len(reasons) == 0, reasons

    # ------------------------------------------------------------------
    # Проверка карточки лота (разделы 7.2–7.3, 8–9 ТЗ)
    # ------------------------------------------------------------------

    def full_check(self, lot: dict):
        """
        Полная проверка после загрузки карточки: цена, площадь, ВРИ,
        расстояние, риски и ограничения, сведения о подъезде/коммуникациях.
        Заполняет reject_reasons / review_reasons / статус локации.
        """
        reject = list(lot.get("reject_reasons") or [])
        review = list(lot.get("review_reasons") or [])
        card_text = lot.get("card_text") or ""

        # --- Площадь (раздел 14 ТЗ: неизвестная площадь не попадает в подходящие)
        if lot.get("area_m2") is None:
            review.append("площадь не определена в карточке лота")
        elif lot["area_m2"] < float(self.cfg["min_area_m2"]):
            reject.append(f"площадь {lot['area_m2']:g} м² меньше минимальной "
                          f"{self.cfg['min_area_m2']:g} м²")

        # --- ВРИ (обязательный критерий ИЖС)
        vri_hit = _contains_any(lot.get("vri_text", ""), self.cfg["vri_keywords"]) or \
            _contains_any(card_text, self.cfg["vri_keywords"]) or \
            _contains_any(lot.get("description", ""), self.cfg["vri_keywords"])
        if vri_hit:
            if not lot.get("vri_text"):
                lot["vri_text"] = f"ИЖС найдено по тексту («{vri_hit}»)"
        elif lot.get("vri_text"):
            reject.append(f"ВРИ не соответствует ИЖС ({str(lot['vri_text'])[:80]})")
        else:
            review.append("ВРИ не определён (нет данных в карточке)")

        # --- Цена (раздел 9 ТЗ): СПРАВОЧНАЯ информация в отчёте.
        # Ограничения по цене СНЯТЫ (изменение логики поиска по решению
        # пользователя): арендная плата больше НЕ отклоняет лоты и НЕ
        # переносит их на «Требуют проверки». Нормализация (в год / в месяц
        # / в квартал) сохранена для колонок «Цена, руб./год», «Цена,
        # руб./мес.», «Исходная цена», «Период цены».
        price_fields = []
        extra = lot.get("extra") or {}
        if extra.get("price_min") is not None:
            price_fields.append(f"начальная цена {extra['price_min']}")
        if extra.get("goszemli_price") is not None:
            price_fields.append(f"цена {extra['goszemli_price']}")
        price = normalize_price(price_fields, card_text)
        lot["price_raw"] = price["raw"] or "не указана"
        lot["price_period"] = price["period"]
        lot["price_year"] = price["year"]
        lot["price_month"] = price["month"]
        lot["is_rent_price"] = price["is_rent"]

        # Порог цены работает ТОЛЬКО если он задан числом в config.yaml
        # (max_rent_per_year: null — без ограничения; проверка отключена).
        max_rent_year = self.cfg.get("max_rent_per_year")
        if max_rent_year and price["year"] is not None and \
                price["year"] > float(max_rent_year):
            reject.append(f"арендная плата {price['year']:,.0f} руб./год выше порога "
                          f"{float(max_rent_year):,.0f} руб./год".replace(",", " "))

        # --- Расстояние от Хабаровска (раздел 8 ТЗ, этапы 2–3)
        self._resolve_distance(lot, reject, review)

        # --- Риски и ограничения (раздел 7.3 ТЗ)
        text_sources = [("карточка лота", card_text)]
        for doc in lot.get("documents") or []:
            text_sources.append((f"документ: {doc['name']}", doc.get("text") or doc["name"]))
        hard_hits, review_hits = self.risk_checker.check(text_sources)
        lot["risk_hard"] = hard_hits
        lot["risk_review"] = review_hits
        if hard_hits:
            reject.append("критические ограничения: "
                          + RiskChecker.format_hits(hard_hits))
        if review_hits:
            review.append("потенциальные ограничения: "
                          + RiskChecker.format_hits(review_hits))

        # --- Подъезд и коммуникации (раздел 7.3 ТЗ)
        if card_text:
            has_access = bool(_contains_any(card_text, self.cfg.get("access_keywords", [])))
            has_utilities = bool(_contains_any(card_text, self.cfg.get("utilities_keywords", [])))
            lot["has_access_info"] = has_access
            lot["has_utilities_info"] = has_utilities
            if not has_access and not has_utilities:
                review.append("нет сведений о подъезде или коммуникациях")

        lot["reject_reasons"] = reject
        lot["review_reasons"] = review
        lot["risk_level"] = "отклонён" if reject else ("требует проверки" if review else "нет")
        lot["risks_summary"] = "; ".join(filter(None, [
            RiskChecker.format_hits(hard_hits, 3),
            RiskChecker.format_hits(review_hits, 3),
        ]))
        return lot

    def _resolve_distance(self, lot: dict, reject: list, review: list) -> None:
        """Определяет расстояние от центра Хабаровска (раздел 8 ТЗ)."""
        geo_cfg = self.cfg.get("geocoding") or {}
        coords = lot.get("coords")

        # Этап 2. Геокодирование: карточка → НСПД по кадастровому номеру → адрес
        if coords is None and geo_cfg.get("enabled", True):
            if lot.get("cadastral_number") and geo_cfg.get("use_nspd", True):
                coords = geocode_by_nspd(lot["cadastral_number"], self.cfg)
                if coords:
                    lot["coords_origin"] = "НСПД (по кадастровому номеру)"
                    rand_delay(1.0, 2.0)
            if coords is None and lot.get("address") and geo_cfg.get("use_nominatim", True):
                coords = geocode_by_nominatim(lot["address"], self.cfg)
                if coords:
                    lot["coords_origin"] = "Nominatim (по адресу)"
        if coords:
            lot["coords"] = coords

        if coords is None:
            # Этап 3. Локация неизвестна — лот не удаляется автоматически
            lot["location_status"] = "неизвестно"
            lot["distance_km"] = None
            if lot.get("location_keyword"):
                # найдено упоминание пригорода, но координат нет
                review.append("distance_unknown: координаты не определены "
                              f"(местоположение по ключевому слову «{lot['location_keyword']}»)")
            else:
                review.append("distance_unknown: координаты не определены")
            return

        distance = distance_from_center(coords[0], coords[1], self.cfg)
        lot["distance_km"] = round(distance, 1)
        if distance <= float(self.cfg["max_distance_km"]):
            lot["location_status"] = "в радиусе"
        else:
            lot["location_status"] = "вне радиуса"
            reject.append(f"расстояние от Хабаровска {lot['distance_km']:g} км "
                          f"превышает {self.cfg['max_distance_km']:g} км")

    # ------------------------------------------------------------------
    # Итоговое распределение по листам Excel (раздел 10.3 ТЗ)
    # ------------------------------------------------------------------

    def assign_sheet(self, lot: dict) -> str:
        """Назначает лист отчёта по накопленным причинам."""
        reject = lot.get("reject_reasons") or []
        review = lot.get("review_reasons") or []
        if reject:
            return "Отклонённые"
        if review:
            return "Требуют проверки"
        return "Подходящие"
