# CLAUDE.md — SimBridge

## Агент

Claude Code (Anthropic CLI). Проект — telephony-система класса финансовых решений: сбои грозят потерей капитала. Корректность выше скорости.

## Язык

- Коммиты, комментарии в коде, документация — **английский** (код), **русский** (отчёты пользователю).
- Ответы пользователю — на том же языке, на котором он пишет.

## Стиль коммитов

```
<тип>: <краткое описание>

- тип: feat, fix, refactor, docs, chore, test, ci
- пример: `feat: add agent HTTP API for outgoing SMS`
- подпись: `Co-Authored-By: Claude <noreply@anthropic.com>`
```

## Задания — `.tasks/`

- Новые задания пользователь кладёт в `.tasks/` или указывает файл явно.
- Формат: `docs/task-template.md` (не коммитить сами файлы `.tasks/`).
- Папка `.tasks/` **никогда** не добавляется в git и не пушится.
- После завершения задания:
  1. Статус в файле задания → `done` (локально).
  2. Осмысленный commit.
  3. Push в GitHub (`origin main`).
  4. HANDOFF документ для каждого stage → писать в `.handoff/` (никогда не коммитить).
  5. «ОТЧЁТ О ЧЕСТНОСТИ»: что реализовано, что нет, где допущения.

## HANDOFF — `.handoff/`

- Документы-передач между stage пишутся в папку `.handoff/`.
- Папка `.handoff/` **никогда** не коммитится и не пушится в git (уже есть в `.gitignore`).

## Standing Rules (из OVERVIEW)

**Rule 1** — no hardcoding, no duplicate mechanisms. Любой literal в логике, который должен быть в config, — дефект.

**Rule 2** — no unverified claims. Каждый "works" требует артефакта: log, test run, output.

**Rule 3** — real-device evidence for telephony. Каждый stage с SMS/voice включает хотя бы один end-to-end run на физическом модеме. Если оборудование недоступно — честно фиксировать `MANUAL_VERIFY`.

**Rule 4** — preserve working behavior. Существующая система работает для пользователей. Регрессия хуже медленного прогресса.

**Rule 5** — secrets never enter git. Pre-commit hook + CI check.

## Безопасность

- Не коммить секреты, ключи, `.env`, `*.session`.
- Перед деструктивными действиями (`force push`, удаление веток, `reset --hard`) — **спросить** подтверждение.
- Хардкод секретов — запрещён. Все секреты через environment variables.

## GitHub

- Remote: `origin` → `git@github.com:alexolvin/SimBridge.git` (SSH).
- Главная ветка: `main`.
