# Техническая документация

Устройство таск-менеджера из `SPEC.md`: раскладка, решения и их причины, чеклист приёмки.
Разделы идут в порядке зависимостей — каждый следующий опирается на готовый предыдущий.

## 0. Стек и раскладка

Ядро: Python 3.13, FastAPI + uvicorn, SQLite из стандартной библиотеки, `mcp` SDK, `httpx`.
Дэшборд: React + TypeScript на [astralyx-ui](https://ui.astralyx.dev), Vite, Tailwind v4.
Node нужен только на сборке — в рантайме остаётся один Python-процесс.

Архитектура кода — service-based: слой решает *как*, домен решает *что*. Один домен даёт
одноимённый файл в каждом слое.

```
noted/
├── main.py                      # сборка FastAPI: роутеры, MCP, статика, uvicorn
├── requirements.txt
├── pyproject.toml               # entry points noted-api / noted-mcp
├── .dockerignore · .gitignore
├── SPEC.md · TECHNICAL_DOCUMENTATION.md · README.md
├── deploy/
│   ├── Dockerfile               # два этапа: Node собирает фронт, Python отдаёт
│   └── docker-compose.yml       # порт только на 127.0.0.1, база в томе
├── api/
│   ├── models/
│   │   ├── database.py          # соединение SQLite, PRAGMA, лок, схема, миграции
│   │   ├── task.py              # pydantic: задача, статусы, тела запросов
│   │   └── envelope.py          # конверт ответа, enum Outcome, маппинг на HTTP
│   ├── routes/
│   │   ├── __init__.py          # сборка роутеров
│   │   ├── tasks.py             # JSON /api
│   │   ├── live.py              # SSE /events
│   │   └── mcp.py               # MCP по HTTP, монтируется на /mcp
│   ├── services/
│   │   └── tasks.py             # логика домена задач + весь SQL
│   └── utils/
│       ├── __init__.py          # run_service (вызов сервиса в потоке) и respond
│       ├── authorization.py     # проверка NOTED_TOKEN на /api и /mcp
│       ├── events.py            # два Condition: работа для агентов, изменения для дэшборда
│       └── tool_docs.py         # описания инструментов, общие для обоих транспортов
├── mcp_adapter/                 # stdio-транспорт для клиентов без HTTP
│   ├── client.py                # httpx-клиент к ядру
│   └── server.py                # те же пять инструментов поверх /api
├── dashboard/                   # фронтенд, собирается в dist/
│   ├── components.json          # конфиг astralyx-ui: куда класть компоненты
│   ├── vite.config.ts           # алиас @, прокси /api и /events в разработке
│   ├── index.html               # ссылки на иконки и манифест
│   ├── public/                  # фавиконки и манифест, Vite кладёт их в dist как есть
│   └── src/
│       ├── api.ts               # типизированный клиент к /api
│       ├── hooks.ts             # фильтры в URL, загрузка, SSE
│       ├── App.tsx              # сборка страницы
│       ├── parts/               # FilterBar · TaskTable · NewTaskDialog · TaskDialog · ConnectDialog
│       ├── components/ui/       # копии компонентов astralyx-ui (в репозитории, не зависимость)
│       └── lib/                 # format.ts плюс helpers кита
└── tests/
    ├── conftest.py              # своя база на тест, живой uvicorn для потоковых проверок
    ├── services/test_tasks.py
    ├── routes/test_tasks.py · test_dashboard.py · test_mcp_http.py
    └── mcp/test_adapter.py
```

**Правила слоёв** — то, ради чего раскладка и заводится:

- `routes/` — только доставка: разобрать запрос, позвать сервис, упаковать ответ. Ни SQL, ни логики.
- `services/` — вся логика домена, включая SQL. Не импортируют FastAPI: не знают ни про `Request`,
  ни про `HTTPException`, возвращают данные и поднимают доменные исключения. Поэтому их тесты
  не поднимают приложение, а JSON-роуты, MCP и адаптер зовут один и тот же код.
- `models/` — pydantic-схемы и `database.py` с соединением и схемой БД. Без логики.
- `utils/` — сквозное, не привязанное к домену.
- Новый домен = новый одноимённый файл в `routes/` и `services/`; если файл появился только
  в одном слое, домен выделен неверно.

## 1. База и домен задач

**Схема** (`models/database.py`):

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT    NOT NULL,              -- JSON
    status      TEXT    NOT NULL DEFAULT 'pending',
    project     TEXT,                          -- скоуп, NULL = вне проектов
    assignee_id TEXT,
    created_by  TEXT,
    parent_id   INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    key         TEXT    UNIQUE,
    result      TEXT,                          -- JSON
    created_at  REAL    NOT NULL,              -- unix, наружу отдаётся ISO-8601
    updated_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_queue   ON tasks(status, assignee_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent  ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project, status, id);
```

**Миграция.** Таблица создаётся, затем `_migrate()` досыпает колонки из `LATER_COLUMNS`
(`PRAGMA table_info` → `ALTER TABLE ADD COLUMN`), и только потом создаются индексы — иначе
индекс по новой колонке упадёт на базе, созданной до неё.

**Соединение** — одно на процесс, `check_same_thread=False`, `isolation_level=None`, PRAGMA
`journal_mode=WAL`, `busy_timeout=10000`, `foreign_keys=ON`. Все операции под одним `RLock`;
контекстный менеджер `transaction()` берёт лок и выполняет `BEGIN IMMEDIATE`. Это и есть явная
сериализация: писатель один, а лок закрывает конкурентность внутри процесса.

**`services/tasks.py`** — `create`, `get`, `list_tasks`, `set_status`, `claim`, `stats`,
`projects`, `assignees`. Возвращают данные, а не HTTP-коды; кривой вход поднимает `TaskError`
с полем `code` (значение `Outcome`).

Ключевые места:

- Идемпотентность: `INSERT ... ON CONFLICT(key) DO NOTHING`; при `rowcount == 0` — `SELECT` по `key`
  и возврат существующей задачи с `created=False`. Гонку на вставке снимает UNIQUE-индекс.
- Compare-and-set: `SELECT` статуса и `UPDATE` в одной транзакции; несовпадение с `if_status` —
  возврат текущей задачи без записи.
- Атомарный захват:
  ```sql
  SELECT id FROM tasks
   WHERE status = 'pending'
     AND (assignee_id IS NULL OR assignee_id = :me)
     AND (:scope IS NULL OR project = :scope)
   ORDER BY (assignee_id IS NULL), id
   LIMIT 1;
  UPDATE tasks SET status='in_progress', assignee_id=:me, updated_at=:now
   WHERE id = :id AND status = 'pending';
  ```
  Оба запроса в одной транзакции. `ORDER BY (assignee_id IS NULL), id` даёт «сначала адресованные
  мне, потом общий пул», внутри группы FIFO. `AND status='pending'` в `UPDATE` — страховка на
  случай, если сериализацию когда-нибудь ослабят.
- `list_tasks` не отдаёт `result`, лимит зажат в `[1, 500]`.
- `stats` считает внутри скоупа: иначе при выбранном проекте чипы дэшборда врут.

## 2. Конверт ответов (`models/envelope.py`)

`Outcome` — строковый enum из таблицы в `SPEC.md`. `Envelope` — pydantic-модель с `ok`, `outcome`,
`message` и полезной нагрузкой (`task` / `tasks` / `count` / `stats` / `projects` / `assignees`).
Рядом — единственная на проект таблица `outcome → HTTP-код`.

Конверт лежит в `models/`, потому что им пользуются все три доставки — JSON-роуты, MCP и адаптер:
так форма ответа не разъезжается.

Сериализация идёт с `exclude_unset`: `task: null` остаётся, если его передали явно, а незаполненные
поля в ответ не попадают.

## 3. Ядро (`main.py`, `routes/`)

`create_app()` собирает приложение: роутеры, MCP-транспорт, статика дэшборда, middleware и
обработчики ошибок. Это **фабрика**, а не модульный синглтон — менеджер сессий MCP можно запустить
ровно один раз за свою жизнь, поэтому каждому приложению нужен свой.

`routes/tasks.py` — роуты из таблицы в `SPEC.md`. Каждый: валидация pydantic, вызов сервиса через
`anyio.to_thread.run_sync` (sqlite синхронный — нельзя блокировать event loop), упаковка в конверт.

- Обработчики ошибок: `TaskError` → свой код и конверт; `RequestValidationError` →
  `validation_error` (перекрыть дефолтный формат FastAPI, иначе агент получит чужую форму ответа);
  `HTTPException` сохраняет исходный код ответа; всё непойманное → `internal_error` с id записи
  в логе, без трейсбека наружу.
- `utils/authorization.py`: если `NOTED_TOKEN` задан, требовать заголовок на `/api` и `/mcp`.
  `/healthz` и дэшборд — без токена.
- Статика монтируется **последней**, уже после роутеров, поэтому корень не перехватывает `/api`,
  `/events` и `/mcp`. Каталог задаётся `NOTED_UI_DIR`.

## 4. Событийные ожидания (`utils/events.py`)

Два `asyncio.Condition` на процесс: `available` (появилась работа — будит `claim`) и `changed`
(состояние изменилось как-нибудь — будит SSE-потоки дэшборда). Создание задачи и возврат в
`pending` дёргают оба, захват и прочие смены статуса — только `changed`.

Захват с ожиданием: попробовать забрать → если пусто и `timeout_s > 0`, ждать на `Condition`
с остатком таймаута → после пробуждения пробовать снова (пробуждение не значит, что задача
досталась именно этому ждущему) → по дедлайну вернуть `empty`. `timeout_s` зажат сверху (300 с).

## 5. MCP: два транспорта

**По HTTP (`routes/mcp.py`) — основной.** `MCPServer` из SDK, транспорт streamable-http,
монтируется на `/mcp`. Инструменты зовут те же сервисы, что и JSON-роуты. Ядро уже слушает порт,
поэтому агенту не нужен отдельный процесс: контейнер поднялся — MCP доступен.

Менеджер сессий живёт в lifespan приложения (`session_manager.run()`), и именно из-за его
«запустить можно один раз» приложение собирается фабрикой.

**По stdio (`mcp_adapter/`) — для клиентов без HTTP-транспорта.** Тонкий процесс: собрать тело,
дёрнуть `httpx`, вернуть ответ как есть. Логики и доступа к базе нет. `httpx.ConnectError`
превращается в конверт `api_unavailable` с адресом API в `message`, а не в таймаут и трейсбек.

Описания инструментов лежат в `utils/tool_docs.py` — один источник для обоих транспортов, иначе
тексты, которые видит модель, разъедутся. Жёсткие исходы (`validation_error`, `unauthorized`,
`api_unavailable`, `internal_error`) поднимаются как ошибка инструмента; `not_found`,
`status_conflict` и `empty` возвращаются обычным результатом — это штатные ответы.

## 6. Дэшборд (`dashboard/`)

React + TypeScript на astralyx-ui. Компоненты кита копируются в репозиторий
(`npx astralyx-ui add …`), а не подключаются зависимостью — код наш, обновлять нечего.

- `src/api.ts` — типизированный клиент к `/api`. Разбирает конверт: `ok=false` превращается в
  `ApiError` с `outcome`, недоступное ядро — в понятное сообщение, а не в «Failed to fetch».
- `src/hooks.ts` — фильтры читаются из query-строки и пишутся обратно (`history.replaceState`),
  поэтому ссылка на отфильтрованный вид работает; `useDashboard` тянет список и обзор одним
  заходом; `useLive` держит `EventSource` и зовёт перезагрузку по событию.
- `src/parts/` — `FilterBar`, `TaskTable`, `NewTaskDialog`, `TaskDialog` (`?task=<id>` в URL),
  `ConnectDialog` (адрес MCP, команда для клиента и конфиг — с кнопками копирования).
- Марка в шапке — `Wordmark` из ui-kit (инлайновый SVG с моргающими веками, наследует
  `currentColor`), вертикальный разделитель и название продукта справа. Кейфреймы моргания лежат
  в `src/index.css`; при `prefers-reduced-motion` веки просто закрыты. Иконки — те же файлы,
  что у кита.
- Сборка кладёт статику в `dashboard/dist`, ядро отдаёт её с корня.

Разработка фронта: `npm run dev` поднимает Vite на 5173 и проксирует `/api` и `/events` в ядро,
так что бэкенд не нужно пересобирать на каждое изменение.

## 7. Упаковка и запуск

`deploy/Dockerfile` собирается в два этапа: Node ставит зависимости фронтенда и собирает `dist`,
дальше Python-образ забирает **только** готовый каталог — ни Node, ни `node_modules` в рантайм
не уезжают. База лежит в томе `/data`, чтобы пересборка не стирала задачи.

```
docker compose -f deploy/docker-compose.yml up -d
```

`HEALTHCHECK` дёргает `/healthz`, так что `docker ps` показывает не только «запущен»,
но и «отвечает».

`workers=1` зашит в `main()` и прокомментирован: несколько воркеров = несколько писателей =
возврат к исходной межпроцессной гонке.

Агент подключается по HTTP — ничего ставить не нужно:

```
claude mcp add --transport http noted http://127.0.0.1:8787/mcp/
```

Клиенту без HTTP-транспорта остаётся stdio-адаптер (`noted-mcp` из того же образа либо из venv).

## 8. Тесты

`pytest`, база в `tmp_path` через `NOTED_DB`. Дерево тестов повторяет дерево кода.

- `services/test_tasks.py` — идемпотентность по `key`, CAS и конфликт, приоритет адресных задач над
  пулом, строгость скоупа проекта, фильтры, `stale_seconds`, миграция старой базы, конкурентный
  захват в потоках. Приложение не поднимается.
- `routes/test_tasks.py` — по кейсу на каждый `outcome`, коды HTTP, токен-middleware, long-poll.
- `routes/test_dashboard.py` — SSE присылает событие после создания задачи, `/api/stats` отдаёт
  счётчики вместе со списками проектов и исполнителей, собранный дэшборд отдаётся с корня.
- `routes/test_mcp_http.py` — настоящий клиент из SDK: список инструментов, полный цикл
  (создать → захватить → завершить → конфликт → прочитать), ошибки и штатные исходы.
- `mcp/test_adapter.py` — то же через stdio-адаптер и поведение при выключенном ядре.

Потоковые проверки (SSE, MCP) идут против живого uvicorn из фикстуры `live_server`: ASGI-транспорт
в памяти не отдаёт поток инкрементально.

## Чеклист приёмки

- [ ] Два параллельных `claim` на одну задачу: ровно один `claimed`, второй `empty`.
- [ ] Два `set_status(..., if_status="in_progress")`: второй получает `status_conflict` и видит
      актуальное состояние задачи.
- [ ] Повтор создания с тем же `key`: `outcome="exists"`, второй задачи в базе нет.
- [ ] `claim_task(timeout_s=10)` просыпается от новой задачи за миллисекунды.
- [ ] Агент, упавший в `in_progress`, находится через `get_tasks(stale_seconds=...)`.
- [ ] Агент со скоупом проекта не видит и не забирает ни чужие задачи, ни задачи без проекта.
- [ ] База, созданная до появления колонки `project`, открывается и дополняется без ручных действий.
- [ ] `/mcp` отвечает клиенту из SDK сразу после старта контейнера, без отдельного процесса.
- [ ] Ядро выключено — stdio-адаптер отдаёт `api_unavailable`, агент не падает.
- [ ] Дэшборд показывает задачи, фильтры живут в URL, статус меняется из меню строки.
- [ ] Образ содержит собранный фронтенд, но не содержит Node; задачи переживают пересоздание
      контейнера.
- [ ] `grep -r "sqlite3\|SELECT" api/routes mcp_adapter` пуст: SQL не утёк из сервисов.

## Риски и решения

| Риск | Решение |
|---|---|
| Несколько воркеров uvicorn вернут межпроцессную гонку | `workers=1` в коде + комментарий + пункт в чеклисте |
| Синхронный sqlite блокирует event loop | все вызовы сервисов через `anyio.to_thread.run_sync` |
| Долгий long-poll держит соединение и поток | ожидание на `Condition` (поток не занят), `timeout_s` зажат 300 с |
| Менеджер сессий MCP запускается повторно | приложение собирается фабрикой, у каждого свой сервер |
| Ядро не поднято, агент висит | stdio-адаптер отдаёт `api_unavailable` с адресом в `message` |
| Крупные `task`/`result` раздувают контекст агента | `result` только в `get_task`, лимит списка 500 |
| Открытые SSE-соединения копятся | keepalive раз в 20 с, отвал соединения закрывает генератор |
| Описания инструментов разъезжаются между транспортами | общий `utils/tool_docs.py` |
| Логика расползается в роуты | сервисы не импортируют FastAPI; их тесты не поднимают приложение |
| Дэшборд перехватывает /api, /events или /mcp | статика монтируется последней, после роутеров |
| Фронтенд знает адрес ядра | ходит по относительным путям; в разработке адрес подставляет прокси Vite |
| Контейнер слушает 0.0.0.0 и уезжает в сеть | публикуется только `127.0.0.1:8787`, `NOTED_TOKEN` на `/api` и `/mcp` |
| Пересборка образа стирает задачи | база в томе `/data`, а не в слое образа |
