"""
ai_analyzer.py — Интеграция с OpenRouter API (Prompt Chaining).

OpenRouter — агрегатор LLM с бесплатными моделями и OpenAI-совместимым API.
Используем meta-llama/llama-3.1-8b-instruct:free — полностью бесплатная.

Ключ получить на: https://openrouter.ai/keys
(не баниться автоматически в отличие от Groq)

Паттерн двухшаговой цепочки промптов:
  Шаг 1 → Keyword Gap Analysis
  Шаг 2 → Финальный SEO-отчёт
"""

import json
import logging
import re
from dataclasses import dataclass

from openai import AsyncOpenAI

from wb_client import CompetitorInfo, ProductInfo

logger = logging.getLogger(__name__)

# Бесплатная модель на OpenRouter (поддерживает русский, надёжная)
DEFAULT_MODEL = "google/gemini-2.0-flash-lite-001"

# OpenRouter — OpenAI-совместимый эндпоинт
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# OpenRouter требует заголовок HTTP-Referer (можно любой URL твоего проекта)
OPENROUTER_REFERER = "https://wb-spyglass.app"

_SYSTEM_ROLE = (
    "Ты — эксперт по SEO-оптимизации маркетплейсов, специализирующийся на "
    "Wildberries. Анализируешь данные строго и объективно. "
    "Никогда не выдумываешь ключевые слова — только то, что есть в данных. "
    "Отвечаешь исключительно на русском языке. "
    "Формат ответа — строго валидный JSON без markdown-обёртки и без ```json."
)


# ─────────────────────────────────────────────────────────────────────────────
# Датаклассы
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SEOReport:
    missing_keywords: list[str]
    recommendations: str
    optimized_title: str


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def _build_target_context(product: ProductInfo) -> str:
    return (
        f"SKU: {product.sku}\n"
        f"Название: {product.name}\n"
        f"Бренд: {product.brand}\n"
        f"Цена: {product.price} ₽\n"
        f"Рейтинг: {product.rating}\n"
        f"Отзывы: {product.feedbacks}\n"
        f"Категория: {product.subject_id} — {product.subject_name}"
    )


def _build_competitors_context(competitors: list[CompetitorInfo]) -> str:
    if not competitors:
        return "Конкуренты не найдены."
    lines = []
    for i, c in enumerate(competitors, 1):
        lines.append(
            f"{i}. [{c.brand}] {c.name}\n"
            f"   SKU: {c.sku} | Цена: {c.price} ₽ | "
            f"Рейтинг: {c.rating} | Отзывы: {c.feedbacks}"
        )
    return "\n\n".join(lines)


def _extract_json(text: str) -> dict:
    """Извлекает первый JSON-объект из текста, убирая markdown-fence."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"Модель не вернула валидный JSON. Ответ: {text[:400]}")
    return json.loads(match.group())


# ─────────────────────────────────────────────────────────────────────────────
# Основной анализатор
# ─────────────────────────────────────────────────────────────────────────────

class AIAnalyzer:
    """
    Двухшаговая цепочка промптов через Gemini OpenAI-compatible API.

    Использует тот же ключ от aistudio.google.com, но через
    OpenAI-совместимый эндпоинт — стабильно работает с free tier.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            default_headers={
                "HTTP-Referer": OPENROUTER_REFERER,   # обязателен для OpenRouter
                "X-Title": "WB Spyglass",             # отображается в дашборде
            },
        )
        self._model = model

    async def _chat(self, user_prompt: str) -> str:
        """Запрос к Gemini через OpenAI-совместимый API."""
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM_ROLE},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=2048,
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("Модель вернула пустой ответ (None). Возможно, сработал фильтр или сервер OpenRouter перегружен.")
        return content.strip()

    # ── Шаг 1: Keyword Gap Analysis ──────────────────────────────────────────

    async def _step1_keyword_gap(
        self,
        target: ProductInfo,
        competitors: list[CompetitorInfo],
    ) -> dict:
        """
        Находит SEO-пробелы: ключи конкурентов, отсутствующие у нас.
        Возвращает dict с 'missing_keywords' и 'analysis_notes'.
        """
        prompt = f"""
Ты проводишь Keyword Gap Analysis для товара на Wildberries.

## НАШИ ДАННЫЕ (целевой товар):
{_build_target_context(target)}

## ДАННЫЕ КОНКУРЕНТОВ (топ-5 по отзывам в категории):
{_build_competitors_context(competitors)}

## ЗАДАЧА:
Выдели поисковые ключевые слова, которые есть в названиях конкурентов,
но ОТСУТСТВУЮТ в нашем названии товара. Это — наши SEO-пробелы.

## ПРАВИЛА:
- Только реальные слова/фразы из названий конкурентов выше. Ничего не выдумывай.
- Верни ТОЛЬКО валидный JSON без markdown-обёртки:

{{"missing_keywords": ["ключ1", "ключ2", "ключ3"], "analysis_notes": "Краткие заметки (1-2 предложения)"}}
""".strip()

        logger.info("[STEP 1] Keyword Gap Analysis via Gemini OpenAI-compat")
        raw    = await self._chat(prompt)
        result = _extract_json(raw)

        if "missing_keywords" not in result:
            raise ValueError("Модель не вернула 'missing_keywords' на шаге 1.")
        if not isinstance(result["missing_keywords"], list):
            result["missing_keywords"] = []

        logger.info("[STEP 1] Found %d missing keywords", len(result["missing_keywords"]))
        return result

    # ── Шаг 2: Финальный отчёт ───────────────────────────────────────────────

    async def _step2_final_report(
        self,
        target: ProductInfo,
        competitors: list[CompetitorInfo],
        gap_analysis: dict,
    ) -> SEOReport:
        """
        Генерирует рекомендации и оптимизированный заголовок.
        Принимает результат шага 1 как контекст (prompt chaining).
        """
        missing = ", ".join(gap_analysis.get("missing_keywords", []))
        notes   = gap_analysis.get("analysis_notes", "")

        prompt = f"""
Ты — SEO-копирайтер для Wildberries. Тебе передан результат Keyword Gap Analysis.

## ЦЕЛЕВОЙ ТОВАР:
{_build_target_context(target)}

## КОНКУРЕНТЫ:
{_build_competitors_context(competitors)}

## РЕЗУЛЬТАТ ШАГА 1 (Keyword Gap Analysis):
Отсутствующие ключи: {missing}
Заметки: {notes}

## ЗАДАЧА — сформируй финальный SEO-отчёт:
1. recommendations — конкретные рекомендации по улучшению карточки (2-4 абзаца).
   Что именно добавить в название, как переструктурировать, какие слова убрать.
2. optimized_title — один вариант идеального заголовка (60-100 символов).
   Включи найденные ключи. Только реальные данные из предоставленных названий.

Верни ТОЛЬКО валидный JSON без markdown-обёртки:
{{"recommendations": "Текстовые рекомендации...", "optimized_title": "Оптимизированный заголовок"}}
""".strip()

        logger.info("[STEP 2] Final SEO report via Gemini OpenAI-compat")
        raw    = await self._chat(prompt)
        result = _extract_json(raw)

        return SEOReport(
            missing_keywords=gap_analysis.get("missing_keywords", []),
            recommendations=result.get("recommendations", "Рекомендации не сформированы."),
            optimized_title=result.get("optimized_title", target.name),
        )

    # ── Публичный метод ───────────────────────────────────────────────────────

    async def run_analysis(
        self,
        target: ProductInfo,
        competitors: list[CompetitorInfo],
    ) -> SEOReport:
        """Полная двухшаговая цепочка: Gap Analysis → SEO-отчёт."""
        gap    = await self._step1_keyword_gap(target, competitors)
        report = await self._step2_final_report(target, competitors, gap)
        return report
