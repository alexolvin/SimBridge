# HANDOFF — Stage 05 S05.1: Modem Abstraction and Provenance

**Date:** 2026-08-11
**Status:** COMPLETE (with MANUAL_VERIFY items)

---

## What Was Implemented

### S05.1 — Modem abstraction and provenance

#### 1. ModemProvider interface
- `core/modem.py` — `ModemProvider` abstract base class:
  - `get_info(modem_id)` — current state snapshot
  - `list_modems()` — enumerate all known modems
  - `is_available(modem_id)` — can accept new work
  - `update_state(modem_id, registered, signal_percent, operator, error)` — device-derived state update
  - `set_sms_active(modem_id, active)` — mark SMS activity
  - `set_call_active(modem_id, active)` — mark call activity

#### 2. ModemState enum (GPT §7.2)
- States: `OFFLINE`, `INITIALIZING`, `READY`, `BUSY`, `SMS_BUSY`, `CALL_BUSY`, `ERROR`, `DISABLED`
- `AVAILABLE_STATES` — states that can accept new work ({READY})
- `ONLINE_STATES` — states indicating the modem is detected

#### 3. SingleModemProvider
- Default implementation for single-SIM deployments
- State derived from device reports, not tracked optimistically
- Thread-safe with threading.Lock
- State derivation: OFFLINE → INITIALIZING → READY → SMS_BUSY/CALL_BUSY/ERROR

#### 4. ModemPool (S05.2 preview)
- Groups modems with routing and atomic reservation
- Single-modem deployment: one pool with one member — same code path
- `select_for_sms(destination)` — atomic selection + reservation
- `select_for_call(destination)` — atomic selection + reservation + call activity flag
- `release(modem_id)` — release reservation, clear call activity
- `is_all_busy()` — all modems unavailable
- `get_reserved_count()` — current reservation count

#### 5. Routing strategies (S05.2 preview)
- `RoutingStrategy` — abstract base class
- `FirstAvailableStrategy` — select by modem_id order (default)
- `RoundRobinStrategy` — distribute across available modems

#### 6. CallRegistry integration
- `core/call_control.py` — `CallRegistry.__init__()` accepts optional `modem_pool` parameter
- `create_outgoing()` — uses pool-based selection when pool is available, falls back to direct reservation (backward compat)
- `cleanup()` — releases modem via pool when pool is available
- `modem_id` in CallMachine.to_dict() — provenance on every record

#### 7. Agent initialization
- `agent/agent.py` — creates `SingleModemProvider` + `ModemPool` at startup
- `agent/deps.py` — `get_modem_pool()` dependency

#### 8. Tests
- `tests/test_call_control.py` — 30 new S05.1 tests:
  - ModemStates (3): all states, available states, online states
  - SingleModemProvider (10): offline → ready, SMS busy, call busy, error, unknown modem, list, availability, serialization
  - RoutingStrategies (4): first-available, empty, round-robin alternation, empty
  - ModemPool (8): select SMS, select call, release, all-busy, list, atomic reservation (threading), release allows new
  - CallRegistryWithPool (5): pool-based outgoing, pool busy raises, cleanup releases pool, provenance in dict, backward compat without pool

---

## What Was NOT Done (and Why)

| Item | Reason |
|---|---|
| TS05-1: SMS/call with modem_id on real hardware | MANUAL_VERIFY — requires running system |
| TS05-2: unplug/replug state reflection | MANUAL_VERIFY — requires physical modem |
| Real `dongle show devices` integration | State is derived via `update_state()` — AMI integration for automatic state updates is S05.2+ |

---

## CONTROL Table — S05.1

| Check | Required | Result |
|---|---|---|
| Interface implemented | Single-modem implementation cited | CODE: SingleModemProvider |
| Provenance on all records | modem_id in CallMachine.to_dict() | UNIT TEST PASS |
| State from device | Code citation; derive_state() | CODE: _derive_state() in SingleModemProvider |
| No behavior change | SMS and voice work as after Stage 04 | BACKWARD COMPAT: CallRegistry with modem_pool=None works identically |

---

## Test Results

```
224 passed, 6 skipped in 0.63s
```

Stages 01-04 tests (194) still pass. S05.1 adds 30 tests.

---

## ОТЧЁТ О ЧЕСТНОСТИ

**Что реально реализовано:**
- `core/modem.py` — полный ModemProvider интерфейс, SingleModemProvider, 8 состояний, ModemPool, 2 routing стратегии
- `core/call_control.py` — интеграция с ModemPool, backward compat без pool
- `agent/agent.py` — инициализация ModemPool + SingleModemProvider
- `agent/deps.py` — get_modem_pool()
- `tests/test_call_control.py` — 30 тестов (states, provider, strategies, pool, registry integration)

**Что не реализовано:**
- Реальная интеграция с `dongle show devices` — state обновляется через update_state(), автоматический poll пока не настроен
- TS05-1: реальная SMS/call с modem_id (MANUAL_VERIFY)
- TS05-2: unplug/replug (MANUAL_VERIFY)
- S05.2: routing policies на реальном оборудовании
- S05.3: second GSM node (не требуется для одного модема)

**Заглушки:** нет.
