# WB Spyglass — Backend

Отказоустойчивый FastAPI-бэкенд для анализа конкурентов на Wildberries с AI SEO-отчётом.

## Структура файлов

```
wb_spyglass/
├── .env              ← секреты (не коммитить в git!)
├── requirements.txt  ← зависимости Python
├── config.py         ← настройки pydantic-settings + зависимость verify_api_key
├── wb_client.py      ← асинхронный парсер WB API (httpx)
├── ai_analyzer.py    ← цепочка промптов Groq (Keyword Gap → SEO-отчёт)
└── backend.py        ← FastAPI app: CORS, middleware, эндпоинты
```

## Быстрый старт

### 1. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 2. Настройка `.env`

Открой `.env` и заполни:

```env
SECRET_API_KEY=my_secret_key_123       # любой длинный ключ
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx  # из https://console.groq.com
ALLOWED_ORIGINS=http://localhost:3000,https://your-tg-mini-app.com
APP_ENV=development
```

### 3. Запуск сервера

```bash
uvicorn backend:app --reload --host 0.0.0.0 --port 8000
```

Или напрямую:

```bash
python backend.py
```

### 4. Swagger UI

После запуска открой: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## API Reference

### `GET /health` — публичный, без авторизации

```json
{ "status": "ok", "service": "wb-spyglass", "env": "development" }
```

### `GET /api/analyze/{sku}` — защищённый эндпоинт

**Требует заголовок:** `X-API-Key: <SECRET_API_KEY из .env>`

**Пример запроса (curl):**

```bash
curl -X GET "http://localhost:8000/api/analyze/174274145" \
  -H "X-API-Key: my_secret_key_123"
```

**Пример ответа:**

```json
{
  "status": "success",
  "processing_time_sec": 8.42,
  "target_product": {
    "name": "Кроссовки мужские беговые",
    "price": "2 990.00 ₽",
    "sku": "174274145",
    "brand": "NIKE",
    "rating": 4.3,
    "feedbacks": 1200,
    "category": "Кроссовки"
  },
  "competitors": [
    {
      "sku": "123456789",
      "name": "Кроссовки мужские спортивные легкие дышащие",
      "brand": "Adidas",
      "price": "3 190.00 ₽",
      "rating": 4.8,
      "feedbacks": 8500,
      "url": "https://www.wildberries.ru/catalog/123456789/detail.aspx"
    }
  ],
  "seo_report": {
    "missing_keywords": ["спортивные", "легкие", "дышащие", "для бега"],
    "recommendations": "В названии товара отсутствуют ключевые слова...",
    "optimized_title": "Кроссовки мужские беговые спортивные легкие дышащие"
  }
}
```

---

## Архитектурные решения

| Аспект | Решение |
|--------|---------|
| **Безопасность** | `X-API-Key` dependency + CORS whitelist |
| **Парсинг WB** | `httpx.AsyncClient` с browser User-Agent + timeout |
| **AI** | Groq `llama3-70b-8192`, температура 0.3 (минимум галлюцинаций) |
| **Prompt Chaining** | Шаг 1 → Gap Analysis → Шаг 2 использует результат шага 1 |
| **Валидация** | Pydantic v2 на входе и выходе, строгий JSON-парсинг из LLM |
| **Логирование** | Структурированные логи с временем выполнения каждого запроса |
| **Ошибки** | 401/404/422/502 с чёткими JSON-сообщениями |

## Требования к `.env` для продакшена

```env
SECRET_API_KEY=<минимум 32 символа случайных>
GROQ_API_KEY=gsk_...
ALLOWED_ORIGINS=https://your-tg-mini-app.vercel.app
APP_ENV=production
```
