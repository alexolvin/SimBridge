# Руководство пользователя SimBridge

**Версия:** 0.1 | **Дата:** 2026-08-14

> **Для кого:** для администратора, который хочет понять, как настроить, запустить и
> поддерживать систему — от установки до устранения неполадок.

---

## Быстрая карта — что куда отправлять

Все команды SimBridge отправляются **в Telegram, вашему собственному аккаунту** (тому, через который зарегистрирован SimBridge).

| Что сделать | Что написать в Telegram | Нужное право |
|---|---|---|
| Отправить SMS | `/sms +79261234567 Текст сообщения` | `out_sms` |
| Отправить SMS ответом | Ответьте на входящее SMS → `/sms ответ` | `out_sms` |
| Отправить в явном формате | `+79261234567: Текст сообщения` | `out_sms` |
| Заблокировать номер | `/block +79261234567` | `out_sms` |
| Разблокировать номер | `/unblock +79261234567` | `out_sms` |
| Рассылка всем | `/broadcast Текст` | `out_sms` |
| Список команд | `/help` | любое |

**Входящие SMS** пересылаются автоматически всем пользователям с правом `in_sms`.

> **Важно:** SimBridge использует **личный аккаунт Telegram** (userbot), а не Bot API.
> Только личный аккаунт может совершать голосовые звонки.
> Telegram может ограничить автоматизацию — вы несёте ответственность за последствия.

---

## 1. Что такое SimBridge

SimBridge — мост между Telegram и GSM-модемом (SIM-картой).

**Что работает:**
- Входящие SMS пересылаются в Telegram с именем контакта (если есть)
- Исходящие SMS отправляются через модем
- Голосовые сообщения записываются и пересылаются как голосовые заметки
- Чёрный список блокирует нежелательные номера
- Ограничение скорости: 30 SMS/час, 3 звонка/минуту

**Что НЕ реализовано (по состоянию на август 2026):**
- **Голосовые звонки в реальном времени (live voice)** — архитектура спроектирована (call_control, API-эндпоинты), но программный мост Telegram WebRTC <-> SIP (tg-bridge) ещё не выбран и не интегрирован. См. раздел «Голосовые звонки».
- **Рассылка (/broadcast)** — команда принимает текст, но реальная рассылка всем пользователям ещё не реализована (TODO в коде).
- **Автоответчик при отправке голосового сообщения** — приём голосовых заметок от пользователя Telegram ещё не реализован.
- **Выходящие звонки из Telegram** — ввод номера телефона без `/sms` распознаётся как запрос звонка, но сама маршрутизация звонка (tg-bridge context) пока маршрутизирует на автоответчик.

---

## 2. Установка

### 2.1 Что нужно (обязательно)

1. **Сервер** (Linux):
   - AlmaLinux 9 **или** Ubuntu 22.04/24.04
   - Python 3.9+ (установится автоматически)
   - Доступ в интернет (порт 443/tcp outward — для Telegram)

2. **GSM-модем** (USB):
   - Huawei E173 (проверен)
   - SIM-карта с поддержкой SMS

3. **Asterisk 18+** (установится установщиком)

4. **chan_dongle** — **устанавливается вручную**:
   - Вариант A: от wiringSoft (https://wiringSoft.com/)
   - Вариант B (Ubuntu): `sudo add-apt-repository ppa:dongle-project/ppa`

5. **Telegram**:
   - Личный аккаунт (не бот!)
   - MTProto-креденциалы: зайдите на https://my.telegram.org/apps, создайте приложение, запишите `api_id` и `api_hash`

### 2.2 Опционально

- **Tailscale** — нужен только для распределённой установки (два узла). Для single-node (всё на одном сервере) не требуется.

### 2.3 Запуск установщика

```bash
# Вариант A: из репозитория
git clone https://github.com/alexolvin/SimBridge.git && cd SimBridge
sudo python3 deploy/install.py

# Вариант B: скачать один файл
curl -L https://raw.githubusercontent.com/alexolvin/SimBridge/main/deploy/install.py -o install.py
sudo python3 install.py
```

### 2.4 Что спрашивает установщик

Установщик интерактивный. Шаг за шагом:

| Вопрос | Что вводить |
|---|---|
| Тип установки | `single-node` (всё на одном сервере) или `distributed` (два узла) |
| Node ID | Имя узла (по умолчанию — hostname, можно Enter) |
| Модель модема | Например `Huawei E173` (установщик может определить автоматически) |
| Номер SIM-карты | В формате `+79991234567` |
| Имя устройства chan_dongle | `gsm` (по умолчанию) |
| AMI-пароль | Пароль для доступа к Asterisk (установщик может найти существующий в `manager.conf`) |
| Telegram API_ID | Число из my.telegram.org |
| Telegram API_HASH | Строка из my.telegram.org |
| Telegram username | Ваш username без `@` |
| Agent token | Оставьте пустым — сгенерируется автоматически |
| HTTP secret | Оставьте пустым — сгенерируется автоматически |
| Telegram user IDs | Числовые ID пользователей Telegram через пробел (для ACL) |

### 2.5 Что делает установщик (под капотом)

1. **Диагностика:** определяет ОС, Python, Asterisk, chan_dongle, USB-модемы
2. **Установка:** клонирует репозиторий, устанавливает зависимости, создаёт virtualenv
3. **Конфигурация:**
   - Создаёт `/etc/simbridge/simbridge.yaml` — основной конфиг
   - Создаёт `/etc/simbridge/env` — секреты (API-ключи, токены), права 0600
   - Создаёт `/etc/simbridge/acl.conf` — права пользователей
   - Создаёт `/etc/simbridge/blacklist.txt` — чёрный список (пустой)
4. **Asterisk:** генерирует `asterisk-globals.conf` из YAML-конфига
5. **systemd:** устанавливает юниты `simbridge-agent.service` и `simbridge-userbot.service`
6. **Telegram:** запускает первый вход (phone number + код подтверждения)
7. **Запуск:** стартует сервисы, проверяет здоровье

### 2.6 Проверка после установки

```bash
# Статус сервисов
systemctl status simbridge-agent
systemctl status simbridge-userbot

# Логи
journalctl -u simbridge-agent -f
journalctl -u simbridge-userbot -f
```

Оба сервиса должны быть `active (running)`.

---

## 3. Конфигурация

### 3.1 Файлы конфигурации

| Файл | Зачем | Права |
|---|---|---|
| `/etc/simbridge/simbridge.yaml` | Основной конфиг (тайминги, порты, пути) | 0640 |
| `/etc/simbridge/env` | Секреты (API_ID, API_HASH, токены) | 0600 |
| `/etc/simbridge/acl.conf` | Права пользователей Telegram | 0640 |
| `/etc/simbridge/blacklist.txt` | Чёрный список номеров | 0644 |
| `/var/lib/simbridge/contacts.csv` | База контактов (номер → имя) | 0644 |
| `/var/lib/simbridge/sim_session` | Сессия Telegram (автоматически) | 0600 |

### 3.2 simbridge.yaml — основной конфиг

Копируете `config/simbridge.example.yaml` в `/etc/simbridge/simbridge.yaml` и заполняете:

```yaml
# Роль узла: all-in-one | gsm | telegram
node:
  role: all-in-one
  id: gsm-01

# Telegram userbot
telegram:
  master_username: "ваш_username"     # без @, ID определяется при запуске
  session_path: /var/lib/simbridge/sim_session
  acl_file: /etc/simbridge/acl.conf
  # Секреты — только ИМЕНА переменных окружения:
  api_id_env: SIMBRIDGE_TG_API_ID
  api_hash_env: SIMBRIDGE_TG_API_HASH

# Agent (HTTP API на GSM-узле)
agent:
  listen: "127.0.0.1:8090"       # single-node: 127.0.0.1
  # distributed: IP Tailscale
  token_env: SIMBRIDGE_AGENT_TOKEN
  allowed_peers:
    - "127.0.0.1"                 # single-node

# HTTP-сервер userbot (принимает события от Asterisk)
userbot_http:
  listen: "127.0.0.1:8088"
  secret_env: SIMBRIDGE_HTTP_SECRET
  allowed_peers:
    - "127.0.0.1"

# Asterisk
asterisk:
  ami_host: 127.0.0.1
  ami_port: 5038
  ami_username: simbridge
  ami_password_env: SIMBRIDGE_AMI_PASSWORD
  dongle: gsm
  ring_wait_seconds: 15           # секунд звонка перед автоответчиком
  max_record_seconds: 90          # макс. длительность голосового
  prompt: /var/lib/asterisk/sounds/custom/vm-prompt

# Голосовые звонки (архитектура, см. раздел «Голосовые звонки»)
voice:
  bridge_endpoint: tg-bridge
  bridge_host: 127.0.0.1
  bridge_port: 5062
  srtp: false
  outbound_answer_timeout: 30

# Ограничения
limits:
  sms_per_hour: 30
  calls_per_minute: 3
  max_call_seconds: 3600

# Пути
paths:
  blacklist: /etc/simbridge/blacklist.txt
  contacts_cache: /var/lib/simbridge/contacts.csv
  audit_log: /var/log/simbridge/audit.jsonl
  recordings_dir: /var/lib/simbridge/recordings
```

**Правила:**
- Секреты (API-ключи, токены) **НИКОГДА** не записываются в YAML. Они хранятся в файле `/etc/simbridge/env` и подхватываются через переменные окружения.
- В `agent.listen` и `userbot_http.listen` ставьте `127.0.0.1` для single-node или IP Tailscale для distributed.
- Адрес `0.0.0.0` запрещён — система откажется запускаться.

### 3.3 env — файл секретов

Формат: `ИМЯ_ПЕРЕМЕННОЙ=значение`, по одной на строку:

```
SIMBRIDGE_TG_API_ID=123456
SIMBRIDGE_TG_API_HASH=abcdef1234567890abcdef1234567890
SIMBRIDGE_AGENT_TOKEN=сгенерированный-токен
SIMBRIDGE_HTTP_SECRET=сгенерированный-секрет
SIMBRIDGE_AMI_PASSWORD=ваш-ami-пароль
```

**Права файла:** `chmod 600 /etc/simbridge/env` (только владелец может читать).

### 3.4 acl.conf — права пользователей

Формат: `<telegram_user_id> <право1> <право2> ...`

```
# Telegram User ID  права
# в один ID
12345678 in_sms in_call out_sms out_call
87654321 in_sms in_call
```

**Доступные права:**
| Право | Что позволяет |
|---|---|
| `in_sms` | Получать входящие SMS в Telegram |
| `in_call` | Получать входящие звонки и голосовые |
| `out_sms` | Отправлять SMS, управлять чёрным списком |
| `out_call` | Совершать исходящие звонки |

**Важно:**
- Если user ID нет в файле — все права отклонены (default deny).
- После изменения файла права подхватываются автоматически (hot-reload без перезагрузки).

**Как узнать свой Telegram User ID:**
1. Зайдите в Telegram, найдите бота @userinfobot
2. Отправьте ему `/my_id` — он ответит вашим числовым ID

### 3.5 blacklist.txt — чёрный список

Формат: один номер E.164 на строку. Комментарии начинаются с `#`.

```
# Чёрный список SimBridge
# Один номер на строку, формат E.164
+79161112233
+79034455667
```

**Управление через Telegram:**
- `/block +79161112233` — добавить в чёрный список
- `/unblock +79161112233` — убрать из чёрного списка

**Управление вручную:** редактируйте файл, перезагрузка не нужна — Asterisk читает файл при каждом входящем событии.

### 3.6 contacts.csv — база контактов

Формат: `номер,имя` (CSV, первая строка — заголовок):

```csv
number,name
+79261234555,Иванов Иван
+79161112233,Петров Петр
```

**Как работает:**
- При входящем SMS номер ищется в этом файле
- Если найдено — SMS приходит с именем: `SMS +79261234555 (Иванов Иван): текст`
- Если не найдено — только номер: `SMS +79261234555: текст`
- Файл читается локально, никаких сетевых запросов — SMS не задерживается

---

## 4. Ежедневная работа

### 4.1 Отправка SMS

**Вариант A — явный номер:**
```
/sms +79261234567 Привет, как дела?
```

**Вариант B — ответ на входящее SMS:**
1. Пришло SMS от кого-то
2. Ответьте на это сообщение (Reply в Telegram)
3. Напишите: `/sms Привет, получил!`

Номер подтянется автоматически из исходного сообщения.

**Вариант C — явный формат (без /sms):**
```
+79261234567: Текст сообщения
```

Если сообщение содержит номер и текст после двоеточия — это SMS.
Если только номер — это запрос звонка (пока не работает, см. раздел «Голосовые звонки»).

**Важно:** текст SMS может содержать запятые, кириллицу, эмодзи — всё передаётся корректно. Текст передаётся как параметр API, а не через shell, поэтому специальные символы безопасны.

### 4.2 Входящие SMS

Приходят автоматически. Формат:

```
SMS +79261234555 (Иванов Иван):
Добрый день, это напоминание...
```

Или без имени (если номер не в базе контактов):

```
SMS +79261234555:
Добрый день...
```

Входящие SMS доставляются всем пользователям с правом `in_sms`.

### 4.3 Голосовые сообщения (voicemail)

Когда кто-то звонит на ваш SIM-номер:

1. Звонящий слышит гудки
2. Через 15 секунд (или раньше, если вы не отвечает) — подключается автоответчик
3. Звонящий слышит приветственное сообщение
4. Звонящий оставляет голосовое сообщение
5. После нажатия `#` (или окончания записи) — голосовое пересылается в Telegram

**В Telegram вы получите:**
- Голосовую заметку (voice note) от вашего аккаунта
- С именем звонящего (если есть в контактах)

**Типы уведомлений:**

| Ситуация | Что приходит в Telegram |
|---|---|
| Нормальное голосовое | Голосовая заметка с именем |
| Звонящий сбросил до приветствия | Уведомление «📞 Звонок (брёл)» |
| Запись не удалась | Уведомление «⚠️ Нет записи — номер» |

---

## 5. Автоответчик — настройка

### 5.1 Куда класть аудиофайл приветствия

Файл приветствия указывается в конфиге:

```yaml
asterisk:
  prompt: /var/lib/asterisk/sounds/custom/vm-prompt
```

**Расположение:** `/var/lib/asterisk/sounds/custom/`

### 5.2 В каком формате

**Поддерживаемые форматы:**
- **ulaw** (G.711 μ-law) — `.ulaw` — рекомендуемый, нативный для Asterisk
- **WAV** (8 kHz, mono, PCM) — `.wav` — тоже работает

**Параметры записи:**
- Частота дискретизации: **8000 Гц** (8 kHz)
- Каналы: **моно** (1 канал)
- Формат: ulaw или PCM

### 5.3 Как создать файл приветствия

```bash
# Из любого аудиофайла конвертируем в ulaw для Asterisk:
ffmpeg -i your_greeting.mp3 -ar 8000 -ac 1 -c:a ulaw /var/lib/asterisk/sounds/custom/vm-prompt.ulaw

# Или в WAV (8kHz, mono, PCM):
ffmpeg -i your_greeting.mp3 -ar 8000 -ac 1 -c:a pcm_s16le /var/lib/asterisk/sounds/custom/vm-prompt.wav
```

**Важно:** в конфиге указывайте путь **без расширения**:
```yaml
prompt: /var/lib/asterisk/sounds/custom/vm-prompt
```
Asterisk сам выберет подходящий формат (.ulaw, .wav и т.д.).

### 5.4 Тайминги автоответчика

| Параметр | Конфиг | По умолчанию | Описание |
|---|---|---|---|
| Длительность гудков | `asterisk.ring_wait_seconds` | 15 сек | Сколько гудеть перед автоответчиком |
| Макс. запись | `asterisk.max_record_seconds` | 90 сек | Сколько ждать после приветствия |

После изменения — запустите генератор Asterisk-конфига:

```bash
python3 scripts/generate_asterisk_config.py /etc/simbridge/simbridge.yaml
```

---

## 6. Голосовые звонки в реальном времени (live voice)

### 6.1 Текущий статус

**Control plane реализован (Stage 04); медиа-мост — внешний процесс,
требует сборки и live-проверки (MANUAL_VERIFY).**

Что есть:
- State machine для управления звонками (CallState, CallMachine, CallRegistry)
- API-эндпоинты: `/v1/call/incoming`, `/v1/call/outgoing`,
  `/v1/call/outgoing/accepted`, `/v1/call/{id}/complete`,
  `/v1/call/{id}/accept|reject|hangup`, `/v1/call/check-timeouts`
- `scripts/notify-agent-agi.py` — AGI-мост dialplan ⇄ agent
  (incoming / outgoing-accepted / complete, DIALSTATUS-карта)
- Dialplan: входящие — `Dial(SIP/${BRIDGE_ENDPOINT},${RING_WAIT_SECONDS})`
  + voicemail на NOANSWER; исходящие — контекст `[tg-bridge]` с
  nocal-гейтом и `Dial(Dongle/${MODEM_ID}/${EXTEN})`
- PJSIP-эндпоинт `tg-bridge` **генерируется** из `simbridge.yaml`
  (`scripts/generate_asterisk_config.py -p`; ручной `pjsip.conf.example`
  убран — один механизм, Rule 1)
- Userbot: обработчик «голого номера» (звонок из Telegram), клиент
  bridge control API (loopback), эндпоинт `/events/call` для
  локализованных сообщений об исходе исходящего звонка
- Тайм-драйвер `simbridge-timeouts` (systemd timer, 5 s) — единственный
  исполнитель out-of-band Telegram-ринга + reaper по max_call_seconds
- Документация архитектуры в `docs/voice-bridge.md`

**Чего нет (MANUAL_VERIFY):**
- Собранного бинарного моста: кандидат `blitss/sip-tg-bridge` — POC;
  его control API нужно адаптировать/форкнуть под loopback-контракт
  из `docs/voice-bridge.md` (§ Bridge Control API)
- Live E2E на реальном модеме и Telegram-аккаунте (оба направления,
  link-drop, двухузловой режим)

### 6.2 Выбранный кандидат для моста

Исследование показывает:
- `Infactum/tg2sip` — отклонён (использует устаревший `libtgvoip`)
- `foobar26/tg2sip` — репозиторий не найден (404 на GitHub)
- `blitss/sip-tg-bridge` — перспективный кандидат (Go, ntgcalls, LiveKit SIP), но требует тестирования

Подробности: `docs/voice-bridge.md`

---

## 7. Управление пользователями (ACL)

### 7.1 Как добавить нового пользователя

1. Узнайте его Telegram User ID (через бота @userinfobot → `/my_id`)
2. Отредактируйте `/etc/simbridge/acl.conf`:
   ```
   # Telegram ID  права
   12345678 in_sms in_call
   ```
3. Сохраните файл — права подхватятся автоматически (hot-reload)

### 7.2 Права

| Право | Что делает |
|---|---|
| `in_sms` | Пользователь получает входящие SMS |
| `in_call` | Пользователь получает уведомления о звонках и голосовых |
| `out_sms` | Пользователь может отправлять SMS и управлять чёрным списком |
| `out_call` | Пользователь может инициировать исходящие звонки |

### 7.3 Default Deny

Если user ID **нет** в `acl.conf` — все действия отклонены, и попытка записывается в audit-log.

---

## 8. Чёрный список

### 8.1 Через Telegram

```
/block +79161112233     # заблокировать
/unblock +79161112233   # разблокировать
```

Нужно право `out_sms`.

### 8.2 Вручную

Отредактируйте `/etc/simbridge/blacklist.txt`:
```
# Заблокированные номера
+79161112233
+79034455667
```

Сохраните — изменения вступят в силу немедленно (Asterisk читает файл при каждом событии).

### 8.3 Что блокируется

Чёрный список применяется к:
- **Входящим SMS** — заблокированный номер не доставит SMS
- **Исходящим SMS** — система откажется отправить SMS на заблокированный номер
- **Входящим звонкам** — Asterisk проверяет файл при каждом входящем звонке

---

## 9. Мониторинг и диагностика

### 9.1 Быстрые команды

```bash
# Статус сервисов
systemctl status simbridge-agent simbridge-userbot

# Логи agent (GSM-узел)
journalctl -u simbridge-agent --since '5 min ago'

# Логи userbot (Telegram-узел)
journalctl -u simbridge-userbot --since '5 min ago'

# Live-лог (оба сервиса)
journalctl -u simbridge-agent -u simbridge-userbot -f

# Health-check agent'а
curl http://127.0.0.1:8090/v1/health

# Health-check userbot'а
curl http://127.0.0.1:8088/health
```

### 9.2 Asterisk — диагностика модема

```bash
# Модем подключен?
asterisk -rx "dongle status"

# Модуль chan_dongle загружен?
asterisk -rx "module show like dongle"

# Активные каналы
asterisk -rx "core show channels"
```

### 9.3 Audit-лог

Файл: `/var/log/simbridge/audit.jsonl`

Формат: JSON, одна строка на событие. Поля:
- `timestamp` — UTC, ISO 8601
- `event` — тип события (SMS_SEND_REQUESTED, USER_DENIED и т.д.)
- `telegram_user_id` — ID пользователя Telegram
- `correlation_id` — идентификатор цепочки событий
- `modem_id` — идентификатор модема
- `outcome` — результат (ok, denied, error и т.д.)

Просмотр:
```bash
# Последние события
tail -20 /var/log/simbridge/audit.jsonl

# Только ошибки
grep '"outcome": "error"' /var/log/simbridge/audit.jsonl
```

### 9.4 Health-эндпоинт

```bash
curl -s http://127.0.0.1:8090/v1/health | python3 -m json.tool
```

Ответ:
```json
{
  "status": "ok",
  "asterisk_reachable": true,
  "dongle_registered": true,
  "timestamp": "2026-08-14T12:00:00+00:00",
  "components": {
    "asterisk": {"healthy": true, "detail": "AMI connected"},
    "modem": {"healthy": true, "detail": "registered, signal=75%"},
    "agent_process": {"healthy": true, "detail": "running"}
  }
}
```

**Статусы:**
- `ok` — все компоненты работают
- `degraded` — часть компонентов не работает
- `critical` — Asterisk или модем недоступны

---

## 10. Потеря связи — как система восстанавливается

### 10.1 Что происходит при потере связи

SimBridge имеет встроенные механизмы восстановления:

| Что отвалилось | Что происходит | Как восстанавливается |
|---|---|---|
| **Asterisk перезапустился** | AMI-соединение обрывается | `BackoffReconnector` переподключается с экспоненциальной задержкой (2s → 4s → 8s → ... → 60s) |
| **Модем отключился** | SMS не отправляются, звонки не принимаются | `ModemWatchdog` проверяет каждые 30 сек, пытается сбросить модем |
| **Сеть Tailscale обрывается** | Userbot не может связаться с agent'ом | HTTP-запросы возвращают ошибку, SMS не доставляются |
| **Telegram отваливается** | Userbot не может отправить/получить сообщения | Telethon переподключается автоматически, session сохраняется на диск |

### 10.2 BackoffReconnector — автоматическая переподключение

Работает при потере AMI-соединения с Asterisk:

1. Первая попытка через 2 секунды
2. Задержка удваивается: 2s, 4s, 8s, 16s, 32s, 60s
3. Максимум 10 попыток
4. Если не удалось 10 раз — логирование критической ошибки

### 10.3 ModemWatchdog — мониторинг модема

1. Проверяет модем каждые 30 секунд
2. Если модем в сломанном состоянии (OFFLINE/ERROR) — инкрементирует счётчик. Занятые состояния (активный вызов, отправка SMS) и период до первого опроса (старт) неудачей не считаются: «сброс» посреди вызова оборвал бы разговор
3. После 3 неудачных проверок — пытается сбросить модем (переподключение AMI)
4. Результат (восстановился / не восстановился) логируется и присылается мастеру алертом

### 10.4 systemd — автоматический перезапуск сервисов

Оба systemd-юнита имеют:

```ini
Restart=on-failure
RestartSec=5
WatchdogSec=120
```

- Если процесс падает — перезапускается через 5 секунд
- Watchdog: если процесс не «пингуется» 120 секунд — systemd убивает и перезапускает

### 10.5 Что делать при проблемах

```bash
# 1. Проверить статус
systemctl status simbridge-agent simbridge-userbot

# 2. Если упал — посмотреть логи
journalctl -u simbridge-agent --since '10 min ago'

# 3. Перезапустить
systemctl restart simbridge-agent
systemctl restart simbridge-userbot

# 4. Проверить модем
asterisk -rx "dongle status"

# 5. Если модем не отвечает — переподключить USB
#    (физически отсоединить и вставить обратно)
```

---

## 11. Распространённые проблемы

### «Номер не указан» / «Некорректный формат номера»

Причина: номер не распознан или не в формате E.164.
Решение: используйте формат `+7XXXXXXXXXX` (начинается с плюса).

Примеры корректных форматов:
- `+79261234555` — OK
- `89261234555` — OK (система преобразует в `+79261234555`)
- `79261234555` — OK (система добавит `+`)
- `+7 (926) 123-45-55` — OK (система очистит)

### «Модем недоступен»

```bash
# Проверить
asterisk -rx "dongle status"
systemctl status asterisk

# Перезапустить Asterisk
systemctl restart asterisk
```

### «Модем не зарегистрирован в сети»

SIM-карта не получила регистрацию в сети оператора. Проверьте:
1. SIM-карта вставлена правильно
2. Антенна модема подключена
3. Покрытие сети в вашем месте

```bash
# Посмотреть статус
asterisk -rx "dongle status"
# Ищите: registered=yes, signal_percent=XX
```

### SMS не приходит в Telegram

1. Проверьте, что userbot запущен: `systemctl status simbridge-userbot`
2. Проверьте, что ваш ID в `acl.conf` с правом `in_sms`
3. Проверьте health-эндпоинт: `curl http://127.0.0.1:8088/health`

### «Номер в чёрном списке»

Номер заблокирован. Чтобы разблокировать:
```
/unblock +7XXXXXXXXXX
```

### «Слишком много SMS»

Превышен лимит (по умолчанию 30 SMS/час). Подождите или увеличьте лимит в конфиге:
```yaml
limits:
  sms_per_hour: 60
```

### Голосовое сообщение тише чем надо

Это решается автоматически: `tg-voice-forward.sh` применяет нормализацию громкости через `ffmpeg loudnorm`. Если всё равно тихо — проверьте, что `ffmpeg` установлен и работает.

### Сервис не стартует

```bash
# 1. Проверить логи
journalctl -u simbridge-agent -n 50

# 2. Проверить конфиг
python3 -c "from core.config import load_config; print(load_config('/etc/simbridge/simbridge.yaml'))"

# 3. Проверить секреты
cat /etc/simbridge/env  # убедитесь, что переменные есть и не пустые

# 4. Проверить права на сессию
ls -la /var/lib/simbridge/sim_session*
# Должно быть 600, владелец — simbridge
```

---

## 12. Распределённая установка (два узла)

### 12.1 Когда нужна

Когда GSM-модем физически находится в одном месте, а Telegram-соединение должно идти из другого.

### 12.2 Топология

```
Узел 1 (GSM):          Узел 2 (Telegram):
- Asterisk             - userbot
- simbridge-agent      - tg-bridge (в будущем)
- USB-модем
- Tailscale            - Tailscale
         \             /
          \           /
          Tailscale (mesh network)
```

### 12.3 Настройка

1. Установите Tailscale на оба узла (один tailnet)
2. На GSM-узле: `node.role: gsm`, `agent.listen: "<Tailscale-IP-GSM>:8090"` (агент слушает здесь)
3. На Telegram-узле: `node.role: telegram`, `agent.listen: "<Tailscale-IP-GSM>:8090"`, `agent.allowed_peers: ["<Tailscale-IP-TG>", "<Tailscale-IP-GSM>"]`. На TG-узле `agent.listen` указывает на GSM-узел: userbot читает его, чтобы достучаться до агента (сам агент на TG-узле не работает)
4. В `userbot_http.listen` — IP Tailscale этого узла, НЕ 127.0.0.1

**Важно:** используйте либо полный FQDN MagicDNS, либо сырой IP-адрес Tailscale. Короткие имена хостов могут не разрешаться.

---

## 13. Удаление системы

```bash
# Остановить и отключить сервисы
sudo systemctl stop simbridge-agent simbridge-userbot
sudo systemctl disable simbridge-agent simbridge-userbot

# Удалить юниты
sudo rm -f /etc/systemd/system/simbridge-*.service
sudo systemctl daemon-reload

# Удалить конфиг и данные
sudo rm -rf /etc/simbridge/
sudo rm -rf /var/lib/simbridge/
sudo rm -rf /var/log/simbridge/
```

> **Внимание:** удаление необратимо. Перед удалением сделайте бэкап `/etc/simbridge/env` (секреты) и `/var/lib/simbridge/sim_session*` (сессия Telegram), если планируете переустановить.

---

## 14. Справочник

### 14.1 Структура файлов на сервере

```
/opt/simbridge/                 # Рабочая копия проекта
/opt/simbridge-venv/            # Python virtualenv
/etc/simbridge/                 # Конфигурация
  simbridge.yaml                # Основной конфиг
  env                           # Секреты (права 600)
  acl.conf                      # Права пользователей
  blacklist.txt                 # Чёрный список
/var/lib/simbridge/             # Данные
  sim_session*                  # Сессия Telegram (права 600)
  contacts.csv                  # База контактов
  recordings/                   # Записи голосовых (удаляются после отправки)
/var/log/simbridge/             # Логи
  audit.jsonl                   # Audit-лог

# Asterisk
/etc/asterisk/                  # Конфигурация Asterisk
  extensions_custom.conf        # Dialplan SimBridge
  pjsip.conf                    # PJSIP (для voice bridge)
  dongle.conf                   # chan_dongle
  asterisk-globals.conf         # Сгенерированные переменные (НЕ редактируйте)
/var/lib/asterisk/sounds/custom/  # Аудиофайлы
  vm-prompt.ulaw                # Приветствие автоответчика
```

### 14.2 systemd-сервисы

| Сервис | Описание | Порт |
|---|---|---|
| `simbridge-agent` | HTTP API для управления GSM (SMS, звонки, health) | 8090 |
| `simbridge-userbot` | Telegram-клиент + HTTP-сервер для событий от Asterisk | 8088 |

### 14.3 API Agent'а (HTTP)

Все эндпоинты требуют авторизации: Bearer-токен + IP в allowed_peers.

| Метод | Путь | Описание |
|---|---|---|
| POST | `/v1/sms` | Отправить SMS |
| GET | `/v1/modems` | Состояние модемов |
| GET | `/v1/health` | Health-check |
| POST | `/v1/blacklist` | Заблокировать номер |
| POST | `/v1/unblock` | Разблокировать номер |
| GET | `/v1/calls` | Список активных звонков |

### 14.4 Команды Asterisk

```bash
# Статус модема
asterisk -rx "dongle status"

# Список каналов
asterisk -rx "core show channels"

# Перезагрузка dialplan'а (без перезапуска Asterisk)
asterisk -rx "dialplan reload"
```

### 14.5 Формат телефонных номеров

Система нормализует номера к E.164. Поддерживаемые входные форматы:

| Ввод | Результат |
|---|---|
| `+79261234555` | `+79261234555` |
| `89261234555` | `+79261234555` |
| `79261234555` | `+79261234555` |
| `+7 (926) 123-45-55` | `+79261234555` |

В чёрном списке, контактах и audit-логе — всегда E.164 (`+7...`).

### 14.6 Типы audit-событий

| Событие | Когда |
|---|---|
| `USER_DENIED` | Пользователь без прав попытался выполнить действие |
| `SMS_SEND_REQUESTED` | Запрос на отправку SMS |
| `SMS_SUBMITTED` | SMS отправлена на модем |
| `CALL_REQUESTED` | Запрос на звонок |
| `BLACKLIST_CHANGED` | Изменение чёрного списка |
| `CONFIG_RELOADED` | Перезагрузка конфигурации |

---

## 15. ОТЧЁТ О ЧЕСТНОСТИ

### Что реализовано и работает

1. **SMS отправляет через agent API + AMI** — текст передаётся как параметр, без shell-интерполяции
2. **Входящие SMS** — Asterisk → tg-sms-forward.sh → HTTP → userbot → Telegram
3. **Чёрный список** — атомарная запись, работает через `/block` и вручную
4. **Контакты** — CSV-кэш, локальный поиск, без сетевых запросов
5. **ACL** — 4 права, default deny, hot-reload
6. **Audit-лог** — JSONL, UTC, все значимые события
7. **Rate limiting** — 30 SMS/час, 3 звонка/мин
8. **Config validation** — строгая валидация, без молчаливых дефолтов для секретов
9. **Health-check** — компонентные проверки (Asterisk, модем, сеть, процесс)
10. **Восстановление** — BackoffReconnector для AMI, ModemWatchdog для модема
11. **Voicemail** — MixMonitor до Playback (ловит ранний сброс), ffmpeg loudnorm, early hangup detection
12. **Генератор Asterisk-глобалей** — из YAML в [globals] формат
13. **Многомодемная абстракция (S05)** — ModemProvider, ModemPool,
    роутинг (first_available / round_robin), poller состояния устройства
    (DongleShowDevices через AMI каждые `watchdog.modem_check_seconds`),
    аудит выбора модема (MODEM_SELECTED), modem_id на всех записях
14. **Secret detection** — pre-commit хук, проверка при комите
15. **Тесты** — 500 passed / 6 skipped, все проходят

### Что НЕ реализовано

1. **Медиа-мост live voice** — control plane (state machine, API,
   dialplan, AGI, userbot-обработчики, тайм-драйвер) реализован, но
   бинарный мост между Telegram WebRTC и Asterisk SIP — внешний
   процесс: кандидат `blitss/sip-tg-bridge` (POC) нужно собрать и
   адаптировать его control API под loopback-контракт
   (`docs/voice-bridge.md`, § Bridge Control API). Live E2E — MANUAL_VERIFY.
2. **Приём голосовых заметок от пользователя** — обработчик
   регистрирован, но логика пустая (TODO в `userbot/userbot.py`)

### Допущения и упрощения

1. **Один модем** — система спроектирована для нескольких, но проверена с одним
2. **Один узел** — распределённая установка спроектирована, но не проверена end-to-end
3. **Telegram session** — хранится в файле, права 0600 enforced installer'ом

### Что требует ручной проверки

1. **Все интеграционные тесты** (6 skipped) требуют физического модема и работающего Asterisk — автоматизировать невозможно
2. **Голосовые звонки** — требуют интеграции tg-bridge и тестирования на реальном оборудовании
3. **Распределённая установка** — требует двух серверов с Tailscale
4. **Отключение/подключение модема (TS05-2)** — poller покрыт unit-тестами
   на фэйковом AMI; live-прогон (выдернуть/вставить USB) — MANUAL_VERIFY
