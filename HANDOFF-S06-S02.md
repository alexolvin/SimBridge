# HANDOFF — S06.2 Observability & Recovery

**Commit:** `cdb2726 feat: S06.2 — observability and recovery`
**Date:** 2026-08-12
**Tests:** 264 passed, 6 skipped (25 new S06.2 tests)

---

## Что реализовано (6 новых файлов + 3 изменённых)

### Новые модули

1. **`core/logging_config.py`** — structured JSON logging
   - `JSONFormatter`: one-line JSON, UTC timestamps, correlation IDs
   - `set_correlation()` / `get_correlation()`: contextvars-based propagation
   - `StructuredAdapter`: auto-injects correlation_id into every log line
   - `setup_logging()`: root logger config (JSON or plain text)

2. **`core/metrics.py`** — SMS/call counters, component state
   - `SMSCounters`: sent, delivered, failed, incoming, delivery_rate
   - `CallCounters`: incoming/outgoing by outcome, total_answered, total_missed
   - `MetricsCollector`: thread-safe, `get_all()` exports flat dict

3. **`core/health.py`** — comprehensive health checks
   - `HealthChecker`: asterisk, modem, peer_node, bridge, agent_process
   - `HealthStatus`: ok/degraded/critical aggregation
   - `check_all()`: concurrent async checks, sorted by severity

4. **`core/alerting.py`** — Telegram alerting with per-rule cooldowns
   - `AlertManager`: sends formatted alerts to master account
   - `AlertRule`: per-rule cooldown (default 300s) — no flooding
   - Pre-registered: dongle_offline, gsm_registration_lost, telegram_session_invalid, peer_unreachable, repeated_call_failures, modem_recovery, peer_recovery

5. **`core/recovery.py`** — automatic recovery
   - `BackoffReconnector`: exponential backoff (1s → 60s), max_retries, on_give_up callback
   - `ModemWatchdog`: periodic check, reset on consecutive failures, recovery alerting

### Изменённые файлы

6. **`agent/agent.py`**: setup_logging(), metrics, health_checker initialization
7. **`agent/deps.py`**: get_metrics(), get_health_checker()
8. **`agent/routes.py`**: expanded health endpoint — `HealthResponse` includes `components` + `metrics`

### Тесты

- **`tests/test_s06_observability.py`** — 25 tests:
  - JSON logging (5): formatter, correlation, setup, adapter
  - Metrics (5): SMS, calls, component state, thread safety
  - Health status (5): ok/degraded/critical, to_dict
  - Alerting (4): send, cooldown, unknown rule, rule cooldown
  - Recovery (3): reconnector success/retry/give-up
  - Watchdog (2): recovery, reset on consecutive failures
  - HealthChecker (1): no-AMI behavior

---

## CONTROL — S06.2

| Check | Status | Evidence |
|---|---|---|
| Logs structured + UTC | ✅ | JSONFormatter tests, UTC in all timestamps |
| Health endpoints accurate | ✅ | HealthChecker with 5 component checks |
| Alerts fire | ✅ | AlertManager tests — cooldown + send |
| Recovery works | ⚠️ PARTIAL | Code + unit tests written. **Real modem unplug/replug requires MANUAL_VERIFY (TS06-6)** |

---

## Что требует ручной проверки

- **TS06-6**: Real modem disconnect → automatic recovery (ModemWatchdog на живом оборудовании)
- **TS06-5**: Real alert delivery to Telegram (AlertManager wired но не подключён к Telethon client)

---

## Следующий этап

**S06.3 — Restart and Resilience:** real reboot tests, modem replug during SMS, Tailscale outage. **Требует реального оборудования (Rule 3).**
