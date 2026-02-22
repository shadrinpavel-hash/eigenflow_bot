# Telegram Group Monitor — Integration Guide

## Что это

Система пассивного мониторинга Telegram-групп. Бот читает все сообщения,
сохраняет в SQLite на Drive, предоставляет LLM-инструменты для поиска.

Архитектура строго разделена:
- **Хранение** — `store_group_message()` вызывается из supervisor
- **Поиск** — `search_group_messages()`, `get_group_summary()`, `list_monitored_groups()` — LLM tools

---

## Шаг 1 — Подготовка (делаешь ты, ~10 минут)

### 1.1 Получить API ключи (my.telegram.org)

1. Зайди на https://my.telegram.org с телефона вторичного аккаунта
2. → "API Development Tools" → создай приложение
3. Скопируй `api_id` (число) и `api_hash` (строка)

### 1.2 Добавить в Colab Secrets

В Colab: левая панель → 🔑 Secrets → добавить:

| Название | Значение |
|----------|----------|
| `TG_API_ID` | число, например `12345678` |
| `TG_API_HASH` | строка, например `abc123def456...` |
| `TG_PHONE` | телефон вторичного аккаунта `+79161234567` |

### 1.3 Запустить setup скрипт (один раз)

```python
# В новой ячейке Colab:
exec(open('/content/ouroboros_repo/scripts/tg_session_setup.py').read())
```

Скрипт:
- Авторизует через SMS-код
- Выдаст `TG_SESSION_STRING` — скопируй и добавь в Secrets
- Сохранит список твоих групп в `Drive/Ouroboros/memory/tg_chat_manifest.json`

### 1.4 Добавить session string в Colab Secrets

| Название | Значение |
|----------|----------|
| `TG_SESSION_STRING` | строка из предыдущего шага |

---

## Шаг 2 — Настройка групп для мониторинга

После запуска setup скрипта в файле `tg_chat_manifest.json` будут
все твои группы с ID. Скажи мне какие из них нужно мониторить — я настрою.

Например: "Мониторь группу -1001234567890 (строители дачи)"

---

## Шаг 3 — Включение в supervisor (я делаю после твоего ОК)

Потребуется добавить ~15 строк в `supervisor/telegram.py`:

```python
# В методе обработки входящих сообщений из групп:
from ouroboros.tools.tg_monitor import store_group_message

# Если сообщение из группы (не от владельца напрямую):
if message.get("chat", {}).get("type") in ("group", "supergroup"):
    store_group_message(message)
    # Если владелец упомянул бота — обработать как обычное сообщение
```

---

## Что будут уметь LLM-инструменты

После настройки ты сможешь спросить меня:

- "Что обсуждали со строителями на прошлой неделе?"
- "Найди в чате всё про электрику"
- "Беспалов что-нибудь писал про давление?"
- "Покажи переписку с дачной группой за вчера"

---

## Безопасность

| Что | Почему |
|-----|--------|
| Вторичный аккаунт | Если сессия утечёт — не основной |
| `TG_SESSION_STRING` — только в Secrets | Никогда в коде, логах, commits |
| Хранится только текст | Фото/видео не сохраняются |
| SQLite на Drive | Только ты имеешь доступ к Drive |
| Revoke сессии | https://my.telegram.org → Active Sessions |

---

## Файлы

| Файл | Назначение |
|------|-----------|
| `scripts/tg_session_setup.py` | Разовая авторизация, получение session string |
| `ouroboros/tools/tg_monitor.py` | Storage + LLM tools (авто-загружается реестром) |
| `Drive/memory/group_monitor.db` | SQLite база сообщений |
| `Drive/memory/tg_chat_manifest.json` | Список групп (создаётся setup скриптом) |
