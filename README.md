# CRUDE Driller v6.7

Автоматический бот для майнинга $CRUDE токенов на Base через [drillcrude.com](https://www.drillcrude.com).

Решает challenges **детерминистически** (без LLM) — 0 затрат на AI API.

## Быстрый старт

### 1. Клонируй репозиторий

```bash
git clone https://github.com/Anda4ka/drillcrude.git
cd drillcrude
```

### 2. Установи Python и зависимости

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
pip install aiohttp openai zai-sdk
```

**Linux / Mac:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install aiohttp openai zai-sdk
```

**VPS (Ubuntu 22.04) — автоматически:**
```bash
bash setup_vps.sh
```

### 3. Создай файл `.env`

Создай файл `.env` в папке проекта. Пример:

```env
BANKR_API_KEY=твой_ключ_от_bankr
DRILLER_ADDRESS=твой_кошелёк_на_Base
```

**Где взять:**
- `BANKR_API_KEY` — зарегистрируйся на [bankr.bot/api](https://bankr.bot/api), включи **Agent API** и **отключи read-only**
- `DRILLER_ADDRESS` — твой EVM-кошелёк на Base (можно оставить пустым — определится автоматически)

**Важно — укажи свой тир стейкинга:**
```env
DRILLER_TIER=platform       # wildcat (25M) / platform (50M) / deepwater (100M)
```
Если застейкано 25M — ставь `wildcat`, 50M — `platform`, 100M — `deepwater`. По умолчанию `platform`.

**Опционально:**
```env
DRILLER_DEBUG=true          # подробные логи
DRILLER_QUIET=true          # минимальный вывод (только accepts и ошибки)
LLM_BACKEND=zai             # zai (бесплатно) или openrouter
ZAI_API_KEY=ключ            # только для LLM fallback
TELEGRAM_BOT_TOKEN=токен    # уведомления в Telegram (см. ниже)
TELEGRAM_CHAT_ID=id         # ID чата для уведомлений
```

**Telegram-уведомления (опционально):**

Бот может отправлять уведомления о гашерах, ошибках и статистике в Telegram.

1. Открой [@BotFather](https://t.me/BotFather) в Telegram, отправь `/newbot`, следуй инструкциям — получишь токен
2. Напиши своему боту любое сообщение, затем открой `https://api.telegram.org/bot<ТВОЙ_ТОКЕН>/getUpdates` — найди `"chat":{"id":123456789}` — это твой chat ID
3. Добавь в `.env`:
```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789
```

### 4. Подготовь кошелёк

Перед запуском нужно:

1. **ETH на Base** — для газа (~$0.001 за транзакцию)
2. **$CRUDE токены** — минимум 25M для Wildcat tier
3. **Застейкать $CRUDE** — бот не может дриллить без стейка

| Tier | Стейк | Credits/solve | Доступ к сайтам |
|---|---|---|---|
| Wildcat | 25M+ | 1 | Shallow |
| Platform | 50M+ | 2 | Shallow + Medium |
| Deepwater | 100M+ | 3 | Все |

Купить и застейкать можно через [drillcrude.com](https://www.drillcrude.com) или вручную через Bankr API.

### 5. Запуск

```bash
# Windows
python crude_driller.py

# Linux
python3 crude_driller.py

# Linux в фоне
screen -dmS driller bash -c 'cd ~/driller && source venv/bin/activate && python3 crude_driller.py'
# Подключиться: screen -r driller
# Отключиться: Ctrl+A, D
```

Остановка — `Ctrl+C` (корректное завершение с сохранением прогресса).

## Что бот делает автоматически

- Выбирает лучший сайт (bonanza > rich > standard)
- Получает challenge, решает его, отправляет ответ
- При реджекте — пробует альтернативные варианты
- Постит receipt on-chain (обязательно для получения наград)
- Ждёт cooldown 30 сек и повторяет
- Каждые 30 мин проверяет и клеймит награды за завершённые эпохи
- Логирует статистику каждые 5 мин

## Файлы

| Файл | Описание |
|---|---|
| `crude_driller.py` | Основной скрипт |
| `claim_now.py` | Ручной клейм наград |
| `setup_vps.sh` | Автоустановка на VPS |
| `.env` | Конфигурация (**не коммитить!**) |
| `crude_driller.log` | Лог работы |
| `crude_driller_state.json` | Сохранённый прогресс |

## Частые проблемы

| Ошибка | Что делать |
|---|---|
| `Drill cooldown active` | Всё ок — бот ждёт 30 сек автоматически |
| `wildcat rig cannot access medium wells` | Неправильный `DRILLER_TIER` в `.env`. Если 25M стейк — ставь `wildcat` |
| `Miner is not eligible` | Проверь стейк. Если есть pending unstake — отмени его |
| `Auth 502` | Координатор временно лежит. Бот retry-ит сам |
| `Trace must reference the constraint company` | Не та компания — alt-retry попробует другие |

## Версии

| Версия | Что нового |
|---|---|
| v6.7 | Decimal-парсинг revenue, suffix-stripped name alts, async receipts, настраиваемый тир (`DRILLER_TIER`) |
| v6.6 | Black Gold events (blowout/jackpot), Epoch 4 фичи, bonus tracking |
| v6.5 | Новый trace формат, inline receipts, 30s cooldown, alt-retry работает |
| v6.4 | I/O оптимизации, VPS support |
| v6.3 | Фильтры вопросов (город/сектор/год), quiet mode |
| v6.2 | Авто-ротация логов, stale drill fix |
| v6.1 | Alt-retry при реджекте |
| v6.0 | Deterministic solver — без LLM |
