# CRUDE Driller v6.2

Автоматический бот для майнинга $CRUDE токенов на Base через [drillcrude.com](https://www.drillcrude.com).

## Что делает

Параллельные async loop:

- **Drilling** — основной цикл: auth → выбор лучшего сайта → получение challenge → **детерминистическое решение** → submit → repeat (~1 сек/цикл)
- **Receipt poster** — фоновый постинг receipt-ов на чейн, throttled (1 per 10 sec) для избежания in-flight лимитов
- **Claiming** — каждые 30 мин проверяет завершённые эпохи и клеймит награды
- **Monitoring** — каждые 5 мин логирует статистику (solve rate, credits, gushers)

## Ключевая фича: Deterministic Solver

Бот **не использует LLM** для решения challenges. Вместо этого:

1. Парсит документ регулярками → извлекает данные каждой компании (employees, founded, revenue, margin)
2. Классифицирует вопрос → определяет поле и направление (highest revenue, fewest employees, etc.)
3. Выбирает компанию алгоритмически → `max()`/`min()` по нужному полю
4. Вычисляет артифакт в Python → `mod`, `letter_positions`, `first_n_reversed`, etc.
5. При реджекте — пробует альтернативные варианты (другой кандидат, с/без пробелов)

**Результат:** ~73% hit rate, ~1 сек/цикл, 0 затрат на API.

LLM используется только как fallback для неизвестных типов вопросов (GLM-5 бесплатно).

## Требования

- Python 3.10+
- [Bankr](https://bankr.bot) API key с write-доступом
- Застейканные $CRUDE токены (минимум 25M для Wildcat tier)
- ETH на Base для газа (~$0.01 за транзакцию)
- *(Опционально)* [OpenRouter](https://openrouter.ai) или ZAI API key — только для LLM fallback

## Установка

```bash
git clone https://github.com/Anda4ka/drillcrude.git
cd drillcrude

python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

pip install -r requirements.txt
```

## Настройка

```bash
copy .env.example .env       # Windows
# cp .env.example .env       # Linux/Mac
```

Заполни `.env`:

| Переменная | Обязательно | Описание |
|---|---|---|
| `BANKR_API_KEY` | ✅ | API ключ от Bankr |
| `DRILLER_ADDRESS` | ✅ | Твой Base wallet address |
| `DRILLER_DEBUG` | | `true` для подробных логов |
| `LLM_BACKEND` | | `openrouter` или `zai` (для fallback) |
| `LLM_MODEL` | | Модель для LLM fallback |
| `OPENROUTER_API_KEY` | | Нужен только если LLM_BACKEND=openrouter |
| `ZAI_API_KEY` | | Нужен только если LLM_BACKEND=zai |

## Запуск

```bash
# Windows PowerShell
python crude_driller.py

# С debug-логами
$env:DRILLER_DEBUG="true"; python crude_driller.py

# Linux
DRILLER_DEBUG=true python3 crude_driller.py

# Linux (фон через screen)
DRILLER_DEBUG=true screen -dmS driller python3 crude_driller.py
```

Остановка — `Ctrl+C` (чистое завершение с сохранением state).

## Архитектура solver

```
Challenge → deterministic_pass1() → compute_artifact_locally() → submit
                   ↓ (fail)                                        ↓ (rejected)
              LLM Pass 1 → local compute → submit              try alternates
                               ↓ (fail)
                          LLM Pass 2 → submit
```

**Deterministic solver** (основной путь, ~95% challenges):
- `parse_companies()` — regex-парсинг документа
- `parse_question()` — классификация типа вопроса
- `compute_artifact_locally()` — вычисление артифакта в Python
- **Alt-retry** — при реджекте пробует альтернативы (дубликаты, пробелы/без пробелов)

**LLM fallback** (для неизвестных типов):
- Pass 1: LLM извлекает компанию + данные
- Pass 2: LLM вычисляет артифакт

### Поддерживаемые constraint-ы (local compute)

| Тип | Пример | Метод |
|---|---|---|
| employees mod N | `employees mod 7` → `5874 % 7 = 1` | `employees_mod` |
| founding_year mod N | `founding_year mod 19` → `1995 % 19 = 0` | `founding_mod` |
| revenue mod N | `revenue_millions mod 13` → `8500 % 13 = 5` | `revenue_mod` |
| margin mod/mul/sub/add | `margin × 3` → `18 * 3 = 54` | `margin_mul` |
| first letters | `Blue Mesa Logistics` → `BML` | `first_letters` |
| first N reversed | `Summit` (4 chars) → `mmuS` | `first_n_reversed` |
| letter positions | `positions 2,5,8` → extract chars | `letter_positions` |
| every Nth letter | `every 3rd letter` → extract | `every_nth` |

## Типы сайтов

| Тип | Множитель кредитов | Описание |
|---|---|---|
| standard | 1× | Базовые кредиты |
| rich | 4× | Повышенная награда |
| bonanza | 5× | Максимальная награда |

Плюс случайные бонусы: gusher (3×), mega-gusher (10×).

Сайты истощаются со временем и регенерируют. Бот автоматически выбирает лучший доступный.

## Staking tiers

| Tier | Стейк | Credits/solve | Доступ |
|---|---|---|---|
| Wildcat | 25M+ $CRUDE | 1 | Shallow |
| Platform | 50M+ $CRUDE | 2 | Shallow + Medium |
| Deepwater | 100M+ $CRUDE | 3 | Все сайты |

## Файлы

| Файл | Описание |
|---|---|
| `crude_driller.py` | Основной скрипт (~1600 строк) |
| `claim_now.py` | Ручной клейм наград |
| `.env` | Конфигурация (не коммитить!) |
| `crude_driller.log` | Основной лог (accepts, gushers, warnings) |
| `crude_debug.log` | Debug-лог (receipt fails, парсинг, stale drills) |
| `crude_state.json` | Персистентное состояние |

Логи авто-ротируются при >10 МБ (остаётся последние 5 МБ).

## Troubleshooting

**"Miner already has an active drill request"** — предыдущий drill не завершён. v6.2 автоматически решает stale challenge или закрывает его dummy-сабмитом.

**"Artifact did not satisfy deterministic constraints"** — парсер извлёк неправильное число или выбрал не ту компанию. Включи `DRILLER_DEBUG=true` и проверь `crude_debug.log`.

**Auth 502 errors** — координатор временно недоступен. Скрипт retry-ит с backoff до 10 попыток.

**"DET_QUESTION_UNKNOWN"** в debug логе — встретился новый тип вопроса. Бот упадёт в LLM fallback. Добавь паттерн в `_QUESTION_MAP`.

## Версии

| Версия | Что нового |
|---|---|
| v6.0 | Deterministic solver — без LLM для 95% challenges |
| v6.1 | Alt-retry при реджекте + фиксы пробелов |
| v6.2 | Throttled receipts, stale drill fix, авто-ротация логов, чистые логи |
