# SimBridge

Мост между Telegram и GSM-телефонией (Asterisk + chan_dongle).

SMS, голосовые сообщения и живые голосовые звонки — управляются через команды Telegram с личного аккаунта пользователя (MTProto, не Bot API). Построено по стандартам финансового класса: сбои могут стоить реальных денег, поэтому корректность важнее скорости.

**Требования:** Asterisk 18+ на EL9/Ubuntu, `chan_dongle` + USB GSM-модем (проверен на Huawei E173), аккаунт Telegram пользователя (Bot API не может совершать голосовые звонки), Tailscale для распределённых развёртываний.

**⚠️  Риск для аккаунта:** Используется личный аккаунт Telegram. Условия использования Telegram могут ограничивать автоматизацию. Вы несёте ответственность за последствия блокировки аккаунта.

## Архитектура

```
                        ┌────────────────── TELEGRAM NODE ───────────────────┐
   Telegram ◄──────────►│  userbot (Telethon)    tg-bridge (ntgcalls↔SIP)   │
   (MTProto+WebRTC)     │       │ управление               │ SIP 5062       │
                        └───────┼──────────────────────────┼────────────────┘
                                │  аутентифицированный HTTP │  SIP + RTP
                                │  (control plane)          │  (media plane)
                           ─────┼─────── TAILSCALE ─────────┼─────
                                │                           │
                        ┌───────┼────────────────────────────┼── GSM NODE ──┐
                        │  simbridge-agent           Asterisk 18 (5060)     │
                        │                                     │ chan_dongle │
                        └─────────────────────────────────────┼─────────────┘
                                                      GSM-модем / SIM-карта
```

- **userbot** — аккаунт Telegram (Telethon). Принимает команды, пересылает SMS/голосовые сообщения в Telegram.
- **simbridge-agent** — HTTP+JSON API на GSM-узле. Заменяет старый путь через SSH+shell-interpolation.
- **tg-bridge** — мост голосовой медиа (Telegram WebRTC ↔ SIP). Stage 04.
- **core** — общие компоненты: конфигурация, ACL, аудит, ограничение частоты запросов, обнаружение секретов.

## Быстрый старт

```bash
# Клонирование
git clone git@github.com:alexolvin/SimBridge.git
cd SimBridge

# Установка на один узел (Asterisk + agent + userbot на одной машине)
sudo deploy/install.sh all-in-one

# Конфигурация
sudo cp config/simbridge.example.yaml /etc/simbridge/simbridge.yaml
sudo vim /etc/simbridge/simbridge.yaml

# Установка секретов (НИКОГДА не коммитьте их)
sudo tee /etc/simbridge/env <<'EOF'
SIMBRIDGE_TG_API_ID=12345
SIMBRIDGE_TG_API_HASH=...
SIMBRIDGE_AGENT_TOKEN=...
SIMBRIDGE_HTTP_SECRET=...
EOF
sudo chmod 0600 /etc/simbridge/env

# Запуск
sudo systemctl restart simbridge-agent simbridge-userbot
```

Подробные руководства: `docs/install-single-node.md` или `docs/install-distributed.md`.

## Команды

| Команда | Право | Описание |
|---|---|---|
| `/sms <номер> <сообщение>` | `out_sms` | Отправить SMS |
| `/broadcast <сообщение>` | `out_sms` | Отправить всем пользователям |
| `/help` | — | Показать доступные команды |

Входящие SMS и голосовые сообщения пересылаются автоматически пользователям с правами `in_sms` / `in_call`.

## Удаление

### Единый узел (all-in-one)

```bash
# 1. Остановить сервисы
sudo systemctl stop simbridge-userbot simbridge-agent

# 2. Отключить автозапуск
sudo systemctl disable simbridge-userbot simbridge-agent

# 3. Удалить файлы systemd
sudo rm -f /etc/systemd/system/simbridge-userbot.service
sudo rm -f /etc/systemd/system/simbridge-agent.service
sudo systemctl daemon-reload

# 4. Удалить конфигурацию
sudo rm -rf /etc/simbridge/

# 5. Удалить данные (записи, сессии, кэш)
sudo rm -rf /var/lib/simbridge/

# 6. Удалить логи
sudo rm -rf /var/log/simbridge/

# 7. Очистить Asterisk chan_dongle (если устанавливался отдельно)
#    Раскомментировать в /etc/asterisk/modules.conf:
#    noload => chan_dongle.so
#    Затем: sudo systemctl restart asterisk

# 8. Удалить Tailscale (если устанавливался только для SimBridge)
sudo tailscale down
sudo systemctl stop tailscaled
sudo systemctl disable tailscaled
sudo apt remove tailscale   # Ubuntu
# ИЛИ
sudo yum remove tailscale   # EL9

# 9. Удалить директорию проекта
cd /home/user/myhub
rm -rf SimBridge
```

### Распределённая установка (две ноды)

**GSM Node (Asterisk + agent):**
```bash
# 1. Остановить сервис
sudo systemctl stop simbridge-agent
sudo systemctl disable simbridge-agent

# 2. Удалить файл systemd
sudo rm -f /etc/systemd/system/simbridge-agent.service
sudo systemctl daemon-reload

# 3. Удалить конфигурацию
sudo rm -rf /etc/simbridge/

# 4. Удалить данные
sudo rm -rf /var/lib/simbridge/

# 5. Удалить логи
sudo rm -rf /var/log/simbridge/

# 6. Очистить Asterisk chan_dongle (см. единый узел выше)
```

**Telegram Node (userbot + tg-bridge):**
```bash
# 1. Остановить сервисы
sudo systemctl stop simbridge-userbot
# Если tg-bridge через Docker:
sudo docker compose -f /home/user/myhub/SimBridge/deploy/docker-compose.yml down --remove-orphans

# 2. Отключить автозапуск
sudo systemctl disable simbridge-userbot

# 3. Удалить файл systemd
sudo rm -f /etc/systemd/system/simbridge-userbot.service
sudo systemctl daemon-reload

# 4. Удалить конфигурацию
sudo rm -rf /etc/simbridge/

# 5. Удалить данные
sudo rm -rf /var/lib/simbridge/

# 6. Удалить логи
sudo rm -rf /var/log/simbridge/

# 7. Удалить директорию проекта
cd /home/user/myhub
rm -rf SimBridge
```

> **⚠️ Внимание:** Удаление необратимо. Убедитесь, что никто не пользуется системой. Если планируете повторную установку — сохраните `/etc/simbridge/env` (в нём API-ключи и токены).

## Структура проекта

```
simbridge/
├── agent/              # GSM-узел: HTTP API + клиент Asterisk AMI
├── userbot/            # Telegram-узел: клиент Telethon
├── bridge/             # Telegram-узел: мост голосовой медиа (stage 04)
├── core/               # Общие: конфигурация, ACL, аудит, ограничение частоты
├── config/
│   ├── simbridge.example.yaml
│   └── blacklist.example.txt
├── deploy/
│   ├── systemd/
│   ├── docker-compose.yml
│   └── install.sh
├── docs/
│   ├── install-single-node.md
│   ├── install-distributed.md
│   ├── voice-bridge.md
│   └── troubleshooting.md
├── scripts/            # Скрипты-хуки Asterisk
└── tests/
```

## Безопасность

- Секреты не попадают в git (pre-commit хук + проверка в CI)
- Все секреты через переменные окружения, ссылки по имени в конфигурации
- Agent API: bearer-токен + белый список IP (оба обязательны)
- Защита от replay-атак: дублирующиеся `correlation_id` отклоняются в пределах временного окна
- Текст SMS передаётся как структурированные поля AMI — никогда не подставляется через shell
- Сравнение секретов с защитой от тайминг-атак (`hmac.compare_digest`)
- Валидация адреса привязки: отказ от `0.0.0.0` при запуске

## Наблюдаемость (Observability)

- Структурированные JSON-логи с метками UTC и correlation ID
- Эндпоинт здоровья: `/v1/health` — статус компонентов (asterisk, модем, peer, bridge) + метрики
- Метрики: SMS входящие/исходящие, процент доставки, исходы звонков, состояние регистрации модема
- Оповещения: уведомления Telegram о критических событиях (модем офлайн, сессия недействительна и т.д.)
- Автоматическое восстановление: переподключение AMI с экспоненциальным backoff, watchdog модема

## Ограничения

- **Голосовой мост требует `foobar26/tg2sip`** — сторонний Docker-сервис. Требуется отдельная установка.
- **Одна SIM на GSM-узел** — пулы многомодемных конфигураций реализованы, но тестировались с одним участником.
- **Нет Bot API для голосовых звонков** — используется личный аккаунт Telegram (см. риск для аккаунта выше).
- **Требуется подтверждение на реальном оборудовании** — критерии приёмки SMS/голоса требуют доступа к физическому модему.
- **Нет gRPC** — для межвузлового общения используется HTTP+JSON.

## Этапы

| Этап | Статус | Описание |
|---|---|---|
| 01 — Фундамент | ✅ Готов | Репозиторий, конфигурация, секреты, агент API, ACL |
| 02 — SMS | ✅ Готов | Контакты, чёрный список, корреляция, маршрутизация ответов |
| 03 — Голосовые сообщения | ✅ Готов | Усиление, ранний сброс, очистка временных файлов |
| 04 — Голосовой мост | ✅ Готов | форк tg2sip, автоматы состояний вызовов, распределённость |
| 05 — Многомодемный режим | ⚠️ Частично | Абстракция модемов + пулы (S05.1+S05.2). S05.3 отложен — нет второго узла. |
| 06 — Релиз | 🔄 В работе | Аудит безопасности, наблюдаемость, устойчивость, документация |

## Лицензия

SimBridge распространяется под [лицензией MIT](LICENSE). Подробности — в файле `LICENSE`.
