# -*- coding: utf-8 -*-
"""
Проверка рисков и ограничений по тексту карточки лота и документам
(раздел 7.3 ТЗ).

Два уровня:
  * критические формулировки (hard_reject_keywords) → лот исключается;
  * потенциальные ограничения (review_keywords) → лот на «Требуют проверки».

Для каждой находки сохраняются: фраза, окружающий контекст и название
документа/поля, где она найдена. Слово «ограничение» само по себе не является
основанием для исключения — оно отсутствует в списках ключевых слов.
"""

import re

from src.utils import keyword_pattern


def _same_sentence_span(text, start, end):
    """Возвращает границы предложения, в котором встречена формулировка."""
    boundaries = ".!?;"
    sent_start = max(start - 200, 0)
    for i in range(start - 1, sent_start - 1, -1):
        if text[i] in boundaries:
            sent_start = i + 1
            break
    sent_end = min(end + 200, len(text))
    for i in range(end, sent_end):
        if text[i] in boundaries:
            sent_end = i
            break
    return sent_start, sent_end


def _is_negated(text, match_start, match_end, negation_keywords):
    """Проверяет, находится ли отрицание в том же предложении, что и находка.

    «Арест не наложен» → отрицание признаётся; «наложен арест. Зона затопления
    отсутствует» → предложения разные, находка остаётся риском.
    """
    s_start, s_end = _same_sentence_span(text, match_start, match_end)
    sentence = text[s_start:s_end]
    for neg in negation_keywords or []:
        if keyword_pattern(neg).search(sentence):
            return True
    return False


def scan_text(text, keywords, negation_keywords=None, source_name="карточка лота"):
    """
    Ищет ключевые формулировки в тексте.

    Поиск не зависит от регистра, падежей и различий ё/е (utils.keyword_pattern).

    :return: список находок вида {фраза, контекст, документ}.
    """
    hits = []
    if not text:
        return hits
    text = str(text)
    seen_spans = []
    for keyword in keywords:
        pattern = keyword_pattern(keyword)
        for match in pattern.finditer(text):
            start, end = match.start(), match.end()
            # не дублируем одну и ту же находку (перекрывающиеся фразы)
            if any(s <= start and end <= e for s, e in seen_spans):
                continue
            context_start = max(0, start - 100)
            context_end = min(len(text), end + 100)
            context = text[context_start:context_end].replace("\n", " ").strip()
            if negation_keywords and _is_negated(text, start, end, negation_keywords):
                # «арест не наложен», «ограничения отсутствуют» — риск исключаем,
                # но отрицание фиксируем в журнале вызывающего кода через возврат.
                hits.append({
                    "фраза": match.group(0),
                    "контекст": context[:220],
                    "документ": source_name,
                    "negated": True,
                })
                seen_spans.append((start, end))
                continue
            hits.append({
                "фраза": match.group(0),
                "контекст": context[:220],
                "документ": source_name,
                "negated": False,
            })
            seen_spans.append((start, end))
    return hits


class RiskChecker:
    """Проверяет лот по критическим и потенциальным формулировкам."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.hard_keywords = [k for k in cfg["hard_reject_keywords"]]
        self.review_keywords = [k for k in cfg["review_keywords"]]
        self.negations = [k for k in cfg.get("negation_keywords", [])]

    def check(self, text_sources):
        """
        Проверяет все текстовые источники лота.

        :param text_sources: список пар (название_источника, текст) —
                             описание лота, условия договора, атрибуты,
                             названия документов и (опционально) текст PDF.
        :return: (критические_находки, потенциальные_находки)
        """
        hard_hits, review_hits = [], []
        for source_name, text in text_sources:
            if not text:
                continue
            hard_hits += scan_text(text, self.hard_keywords, self.negations, source_name)
            review_hits += scan_text(text, self.review_keywords, self.negations, source_name)

        hard_hits = [h for h in hard_hits if not h.get("negated")]
        # потенциальные находки с отрицанием также не считаются риском
        review_hits = [h for h in review_hits if not h.get("negated")]

        # убираем точные дубли находок в разных источниках
        hard_hits = self._dedup(hard_hits)
        review_hits = self._dedup(review_hits)
        return hard_hits, review_hits

    @staticmethod
    def _dedup(hits):
        seen, result = set(), []
        for hit in hits:
            key = (hit["фраза"].lower(), hit["документ"])
            if key in seen:
                continue
            seen.add(key)
            result.append(hit)
        return result

    @staticmethod
    def format_hits(hits, max_items=5):
        """Краткое описание рисков для колонки «Найденные ограничения»."""
        parts = []
        for hit in hits[:max_items]:
            phrase = hit["фраза"]
            doc = hit["документ"]
            parts.append(f"«{phrase}» ({doc})")
        if len(hits) > max_items:
            parts.append(f"и ещё {len(hits) - max_items}")
        return "; ".join(parts)

    @staticmethod
    def format_hits_full(hits, max_items=10):
        """Подробное описание для листов «Требуют проверки»/«Отклонённые»."""
        lines = []
        for hit in hits[:max_items]:
            lines.append(f"«{hit['контекст']}» [{hit['документ']}]")
        return " | ".join(lines)
