# CRUDE Driller v4.0

Автоматический бот для майнинга $CRUDE токенов на Base через [drillcrude.com](https://www.drillcrude.com).

## Что делает

Три параллельных async loop:

- **Drilling** — основной цикл: auth → выбор сайта → получение challenge → решение через LLM → submit → post receipt on-chain
- **Claiming** — каждые 30 мин проверяет завершённые эпохи и клеймит награды
- **Monitoring** — каждые 5 мин логирует статистику (solve rate, credits, gushers)

## Требования

- Python 3.10+
- [Bankr](https://bankr.bot) API key с write-доступом
- [OpenRouter](https://openrouter.ai) API key
- Застейканные $CRUDE токены (минимум 25M для Wildcat tier)
- ETH на Base для газа (~$0.01 за транзакцию)

## Установка

```bash
# Клонируй репо
cd E:\Bot\driller

# Создай venv и установи зависимости
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

pip install aiohttp openai zai-sdk
```

## Настройка

Скопируй `.env.example` в `.env` и заполни ключи:

```bash
copy .env.example .env
notepad .env
```

Основные параметры:

| Переменная | Описание |
|---|---|
| `BANKR_API_KEY` | API ключ от Bankr |
| `OPENROUTER_API_KEY` | API ключ от OpenRouter |
| `DRILLER_ADDRESS` | Твой Base wallet address |
| `LLM_MODEL` | Модель для решения challenges |
| `DRILLER_DEBUG` | `true` для подробных логов |

## Выбор модели

Модель задаётся через `LLM_MODEL` в `.env`:

| Модель | Hit rate | Стоимость | ID для .env |
|---|---|---|---|
| GPT-4o | ~70% | $$$ | `openai/gpt-4o` |
| GPT-4o-mini | ~40% | $ | `openai/gpt-4o-mini` |
| Claude Sonnet 4 | ~70% | $$$ | `anthropic/claude-sonnet-4` |
| Claude Haiku 4.5 | ~50% | $$ | `anthropic/claude-haiku-4.5` |
| DeepSeek R1 | ~50% | $ | `deepseek/deepseek-r1` |
| Grok 3 | ~?% | $$ | `x-ai/grok-3` |

## Запуск

```bash
# Windows PowerShell
python crude_driller.py

# С debug-логами
$env:DRILLER_DEBUG="true"; python crude_driller.py

# Linux
DRILLER_DEBUG=true python3 crude_driller.py
```

Остановка — `Ctrl+C` (чистое завершение с сохранением state).

## Файлы

| Файл | Описание |
|---|---|
| `crude_driller.py` | Основной скрипт |
| `.env` | Конфигурация (не коммитить!) |
| `crude_driller.log` | Основной лог |
| `crude_debug.log` | Debug: challenges + ответы LLM |
| `crude_driller_state.json` | Персистентное состояние (solves, credits, epochs) |
| `crude_driller.pid` | Lock-файл (single instance) |

## Архитектура solver

Two-pass подход:

1. **Pass 1** — LLM читает документ, отвечает на вопросы, извлекает данные (employees, founded year, revenue)
2. **Pass 2** — LLM получает документ + извлечённые данные + constraints и вычисляет артифакт

Pre-validation: проверка что компания из ответа есть в списке valid companies.

## Staking tiers

| Tier | Стейк | Credits/solve | Доступ |
|---|---|---|---|
| Wildcat | 25M+ $CRUDE | 1 | Shallow |
| Platform | 50M+ $CRUDE | 2 | Shallow + Medium |
| Deepwater | 100M+ $CRUDE | 3 | Все сайты |

## Troubleshooting

**"Miner already has an active drill request"** — предыдущий drill не завершён. Скрипт автоматически пробует закрыть его, ждать ~1-2 мин.

**"Artifact did not satisfy deterministic constraints"** — LLM ошиблась в вычислении. Нормально при ~40% hit rate. Переключи на более сильную модель.

**"Paragraph N does not contain..."** — trace указывает неправильный параграф. Обычно исправляется автоматически.

**Auth 502 errors** — координатор временно недоступен. Скрипт retry-ит с backoff.
