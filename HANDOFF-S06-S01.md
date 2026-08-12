# HANDOFF — S06.1 Security Review & Hardening

**Commit:** `6e33f70 feat: S06.1 — security review and hardening`
**Date:** 2026-08-12
**Tests:** 239 passed, 6 skipped

---

## Что реализовано

### Код (5 файлов изменено)

1. **`agent/deps.py`** — три исправления:
   - `hmac.compare_digest()` для bearer token (был `!=` — уязвимость к timing-атаке)
   - `check_ip()` теперь отклоняет не-localhost, если `allowed_peers` не настроены (был bypass)
   - `get_call_limiter()` — новая зависимость для rate limiting звонков

2. **`agent/agent.py`** — инициализация `call_limiter` (per-user, `limits.calls_per_minute`, окно 60s)

3. **`agent/routes.py`** — rate limiting на `/call/outgoing` (HTTP 429 при превышении)

4. **`core/config.py`** — валидация bind-адреса: `0.0.0.0` отклоняется на старте (`_split_listen()` helper)

5. **`userbot/http_server.py`** — `hmac.compare_digest()` для HTTP secret (оба endpoint: SMS и voicemail)

### Документация

- **`docs/security-review.md`** — полный отчёт по 8 угрозам (T1-T8):
  - T1-T3, T5-T6, T8 — MITIGATED
  - T4 — PARTIALLY MITIGATED (accepted risk, нет криптографической идентичности узлов)
  - T7 — NOT MITIGATED (deferred to S06.2 — watchdog для зависшего модема)

### Тесты

- **`tests/test_security.py`** — 14 новых тестов:
  - Timing-safe comparison (2 теста)
  - IP allowlist enforcement (2 теста)
  - Bind-address validation (4 теста — 0.0.0.0 для agent и userbot, localhost OK, Tailscale OK)
  - Call rate limiting (4 теста — source verification)
  - No wildcard binds (3 теста — source verification)

---

## CONTROL — S06.1

| Check | Status | Evidence |
|---|---|---|
| Eight threats addressed | ✅ | `docs/security-review.md` — каждая угроза с статусом |
| No 0.0.0.0 binds | ✅ | Config validation + source tests (TS06-1) |
| Unauthorized access blocked | ✅ | ACL default-deny + IP allowlist enforcement |
| Repo clean of secrets | ✅ | `secrets_check.py` scan — все 151 match это false positive |
| SSH-based control gone | ✅ | `grep -rnE 'ssh\|scp\|rsync'` — 0 matches |
| Timing-safe comparisons | ✅ | `hmac.compare_digest()` в deps.py и http_server.py |
| Call rate limiting | ✅ | RateLimiter на /call/outgoing, 3/min по умолчанию |

---

## Что перенесено в S06.2

- T7: Watchdog для зависшего модема — автоматическое восстановление
- Health endpoint — расширенный, с проверкой всех компонентов
- Alerting — уведомления в Telegram при сбоях
- Automatic recovery — reconnect с backoff

---

## Следующий этап

**S06.2 — Observability and Recovery:** structured JSON logs, metrics, health endpoints, alerting, automatic recovery.
