# HANDOFF — S06.3 Restart & Resilience

**Commit:** `4af24fe feat: S06.3 — restart and resilience (code + docs)`
**Date:** 2026-08-12
**Tests:** 264 passed, 6 skipped

---

## Что реализовано

### Код (2 файла)

1. **`agent/agent.py`** — AMI auto-reconnect via `BackoffReconnector`
   - 10 retries with exponential backoff (2s → 60s)
   - `on_give_up`: critical log — agent non-functional
2. **`deploy/systemd/*.service`** — `WatchdogSec=120`, `TimeoutStartSec=30`, `TimeoutStopSec=15`

### Документация

3. **`docs/re-auth.md`** — полная процедура re-auth при невалидной Telegram session

---

## CONTROL — S06.3

| Check | Status | Evidence |
|---|---|---|
| Survives reboot | ⚠️ MANUAL_VERIFY | systemd `Restart=on-failure` + `Wants=network-online.target` |
| Survives modem replug | ⚠️ MANUAL_VERIFY | ModemWatchdog (S06.2) + AMI reconnect (S06.3) |
| Survives network loss | ⚠️ MANUAL_VERIFY | BackoffReconnector + symmetric hangup (S04.4) |
| No stuck state | ⚠️ MANUAL_VERIFY | Reconnect + watchdog + cleanup |

---

## MANUAL_VERIFY — требует реального оборудования

- **TS06-7**: reboot both nodes → confirm SMS and call work
- **TS06-8**: mid-SMS modem replug → recover with clear error message
- **TS06-9**: Tailscale down/up cycle → calls terminate cleanly

---

## Следующий этап

**S06.4 — Documentation and GitHub publication:** README, install guides, voice-bridge.md, troubleshooting.md, example config, license, pre-publication check.
