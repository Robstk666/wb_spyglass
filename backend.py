"""
backend.py — Главный файл FastAPI-приложения WB Spyglass.

Запуск:
    uvicorn backend:app --reload --host 0.0.0.0 --port 8000

Архитектура:
    config.py      → настройки (.env) + зависимость verify_api_key
    wb_client.py   → асинхронный парсер Wildberries API
    ai_analyzer.py → цепочка промптов Groq (Keyword Gap + Отчёт)
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Path, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ai_analyzer import AIAnalyzer
from config import Settings, get_settings, verify_api_key
from wb_client import WildberriesClient

# ─────────────────────────────────────────────────────────────────────────────
# Логирование
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("wb_spyglass")


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic-схемы ответа (строгий контракт для фронтенда)
# ─────────────────────────────────────────────────────────────────────────────

class TargetProductSchema(BaseModel):
    name: str
    price: str            # строка с форматом "1 234.56 ₽" для UI
    sku: str
    brand: str
    rating: float
    feedbacks: int
    category: str


class CompetitorSchema(BaseModel):
    sku: str
    name: str
    brand: str
    price: str
    rating: float
    feedbacks: int
    url: str


class SEOReportSchema(BaseModel):
    missing_keywords: list[str] = Field(default_factory=list)
    recommendations: str
    optimized_title: str


class ParseResponse(BaseModel):
    status: str = "success"
    processing_time_sec: float
    target_product: TargetProductSchema
    competitors: list[CompetitorSchema]
    algorithm_explanation: str


class AIReportRequest(BaseModel):
    target_product: TargetProductSchema
    competitors: list[CompetitorSchema]


class ErrorResponse(BaseModel):
    status: str = "error"
    detail: str

class AIErrorFallback(BaseModel):
    status: str
    cringe_message: str
    tech_message: str


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные форматтеры и Telegram-алерты
# ─────────────────────────────────────────────────────────────────────────────

def _format_price(price: float) -> str:
    """Форматирует цену → '1 234.56 ₽'."""
    return f"{price:,.2f} ₽".replace(",", " ")

async def send_telegram_alert(error_text: str) -> None:
    """Асинхронная отправка логов в Telegram админу."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.admin_chat_id:
        logger.warning("Telegram credentials not set. Cannot send alert: %s", error_text)
        return
        
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.admin_chat_id,
        "text": f"🚨 *WB Spyglass AI Error* 🚨\n\n`{error_text}`",
        "parse_mode": "Markdown"
    }
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession() as s:
            await s.post(url, json=payload, timeout=5)
    except Exception as exc:
        logger.error("Failed to send Telegram alert: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan: инициализация при старте, очистка при остановке
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("🚀 WB Spyglass starting up [env=%s]", settings.app_env)
    logger.info("CORS origins: %s", settings.cors_origins)
    yield
    logger.info("🛑 WB Spyglass shutting down")


# ─────────────────────────────────────────────────────────────────────────────
# Инициализация FastAPI
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="WB Spyglass — Competitor Analysis API",
    description=(
        "Бэкенд для глубокого анализа конкурентов на Wildberries. "
        "Использует публичные API WB и Groq LLM для генерации SEO-отчётов."
    ),
    version="1.0.0",
    docs_url="/docs",       # Swagger UI
    redoc_url="/redoc",     # ReDoc
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. CORS Middleware
# ─────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def configure_cors() -> None:
    """Динамически добавляем CORS-middleware после загрузки настроек."""
    pass  # CORS добавляется ниже синхронно


# CORS добавляется сразу при импорте модуля
_settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Разрешаем все домены (в т.ч. Vercel)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Middleware для логирования времени ответа
# ─────────────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = round(time.perf_counter() - start, 3)
    logger.info(
        "%s %s → %s (%.3fs)",
        request.method, request.url.path, response.status_code, elapsed,
    )
    return response


# ─────────────────────────────────────────────────────────────────────────────
# 3. Глобальный обработчик ошибок
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"status": "error", "detail": "Internal server error. Check server logs."},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Health Check — публичный, без авторизации
# ─────────────────────────────────────────────────────────────────────────────

@app.get(
    "/health",
    summary="Health Check",
    tags=["System"],
    response_model=dict,
)
async def health_check(settings: Settings = Depends(get_settings)):
    return {
        "status": "ok",
        "service": "wb-spyglass",
        "env": settings.app_env,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. Главный защищённый эндпоинт анализа
# ─────────────────────────────────────────────────────────────────────────────

@app.get(
    "/api/analyze/{sku}",
    summary="Быстрый парсинг конкурентов по артикулу WB",
    tags=["Analysis"],
    response_model=ParseResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Неверный или отсутствующий API-ключ"},
        404: {"model": ErrorResponse, "description": "Товар не найден на WB"},
        422: {"model": ErrorResponse, "description": "Некорректный артикул"},
        502: {"model": ErrorResponse, "description": "Ошибка при запросе к WB"},
    },
    dependencies=[Depends(verify_api_key)],
)
async def analyze_product(
    sku: int = Path(
        ...,
        title="Артикул товара WB",
        description="Числовой артикул (SKU) товара Wildberries",
        ge=1,
        example=174274145,
    ),
    settings: Settings = Depends(get_settings),
) -> ParseResponse:
    """
    Быстрый сбор данных с Wildberries (без вызова ИИ):
    1. Получает карточку товара с WB.
    2. Ищет топ-5 конкурентов.
    """
    start_time = time.perf_counter()
    logger.info("▶ Fast Parse request: SKU=%d", sku)

    try:
        async with WildberriesClient() as wb:
            wb_data = await wb.analyze(sku)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error("WB data fetch failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ошибка при получении данных с Wildberries: {exc}",
        ) from exc

    target = wb_data.target
    competitors = wb_data.competitors

    logger.info(
        "WB data collected: product='%s', competitors=%d",
        target.name, len(competitors),
    )

    elapsed = round(time.perf_counter() - start_time, 2)
    return ParseResponse(
        status="success",
        processing_time_sec=elapsed,
        target_product=TargetProductSchema(
            name=target.name,
            price=_format_price(target.price),
            sku=str(target.sku),
            brand=target.brand,
            rating=target.rating,
            feedbacks=target.feedbacks,
            category=target.subject_name,
        ),
        competitors=[
            CompetitorSchema(
                sku=str(c.sku),
                name=c.name,
                brand=c.brand,
                price=_format_price(c.price),
                rating=c.rating,
                feedbacks=c.feedbacks,
                url=c.url,
            )
            for c in competitors
        ],
        algorithm_explanation="Самые сильные конкуренты выбраны по алгоритму: 1) Точное совпадение предметной категории. 2) Максимальное количество отзывов. 3) Рейтинг ≥ 4.5."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Эндпоинт ИИ-отчёта с грациозной деградацией
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/api/ai-report",
    summary="Генерация умного SEO-отчета Гуру",
    tags=["Analysis"],
    dependencies=[Depends(verify_api_key)],
)
async def ai_report(
    payload: AIReportRequest,
    settings: Settings = Depends(get_settings),
):
    """
    Принимает спарсенные данные и делает запрос к OpenRouter API.
    В случае ошибки ИИ не падает, а возвращает креативный JSON (graceful degradation)
    и отправляет алерт в Telegram.
    """
    logger.info("▶ AI Report request for SKU=%s", payload.target_product.sku)
    
    if not settings.openrouter_api_key:
        err_msg = "OPENROUTER_API_KEY не настроен на сервере."
        await send_telegram_alert(err_msg)
        return JSONResponse(status_code=200, content={
            "status": "ai_vacation",
            "cringe_message": "Наш главный SEO-гуру улетел на Бали и забыл ноутбук 🌴",
            "tech_message": err_msg
        })

    # Конвертируем обратно в модели ai_analyzer (удаляя символ ₽ из цены для float)
    from wb_client import ProductInfo, CompetitorInfo
    
    def parse_price(p: str) -> float:
        try:
            return float(p.replace("₽", "").replace(" ", "").strip())
        except ValueError:
            return 0.0

    target = ProductInfo(
        sku=int(payload.target_product.sku),
        name=payload.target_product.name,
        brand=payload.target_product.brand,
        brand_id=0,
        price=parse_price(payload.target_product.price),
        rating=payload.target_product.rating,
        feedbacks=payload.target_product.feedbacks,
        subject_id=0,  # ИИ не использует это поле строго, кроме контекста
        subject_name=payload.target_product.category
    )
    
    comps = [
        CompetitorInfo(
            sku=int(c.sku),
            name=c.name,
            brand=c.brand,
            price=parse_price(c.price),
            rating=c.rating,
            feedbacks=c.feedbacks,
            url=c.url,
            subject_id=0
        )
        for c in payload.competitors
    ]

    try:
        analyzer = AIAnalyzer(api_key=settings.openrouter_api_key)
        seo_report = await analyzer.run_analysis(target, comps)
        return {
            "status": "success",
            "seo_report": {
                "missing_keywords": seo_report.missing_keywords,
                "recommendations": seo_report.recommendations,
                "optimized_title": seo_report.optimized_title,
            }
        }
    except Exception as exc:
        err_str = f"Ошибка OpenRouter API: {type(exc).__name__} - {str(exc)}"
        logger.error(err_str, exc_info=True)
        # Отправляем алерт админу в телеграм асинхронно
        import asyncio
        asyncio.create_task(send_telegram_alert(err_str))
        
        # Возвращаем статус success, чтобы фронтенд точно отрисовал ошибку в полях!
        return {
            "status": "success",
            "seo_report": {
                "missing_keywords": [f"Ошибка ИИ: {type(exc).__name__}"],
                "recommendations": f"Сбой генерации нейросети: {str(exc)}. Пожалуйста, проверьте баланс OpenRouter или правильность API ключа в Railway.",
                "optimized_title": "ОШИБКА ГЕНЕРАЦИИ",
            }
        }


# ─────────────────────────────────────────────────────────────────────────────
# Точка входа (для прямого запуска: python backend.py)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import uvicorn

    uvicorn.run(
        "backend:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=False,
        log_level="info",
    )
