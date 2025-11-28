# Quiz Runner (FastAPI + SQLite + HTML/JS)

## Быстрый старт
- `uv venv .venv && source .venv/bin/activate`
- `uv pip install -r requirements.txt`
- Скопируйте `.env.example` в `.env` и заполните:
  - `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`
  - `GITHUB_REDIRECT_URL=http://<host>:8000/api/auth/github/callback` (должен совпадать с настройками OAuth App)
  - `QUIZ_FILE=test.json` (или другой JSON с вопросами; БД будет `<quiz>.db`)
  - `QUIZ_ATTEMPT_LIMIT`, `QUIZ_ATTEMPT_MINUTES`
  - `LECTOR=<github>` — этому пользователю попытки не ограничиваются
- `uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload`
- Открыть `http://<host>:8000/`

## Как это работает
- GitHub OAuth: редирект → callback → бэкенд меняет code на токен, создаёт пользователя и отдаёт данные через `postMessage` в окно.
- Попытки: лимит и длительность настраиваются через `.env`, таймер на фронте, дедлайн проверяется на бэке. При рефреше попытка продолжается.
- Вопросы: читаются из файла `QUIZ_FILE` (`name` + `questions`), порядок вариантов перемешивается и хранится в БД `<quiz-file>.db`.
- UI: показ по одному вопросу, без возврата назад, предупреждение при незаполненных ответах.
- Обфускация: текст вопросов и ответов рисуется на canvas с шумом, чтобы усложнить съём камерой.
- LECTOR: указанный GitHub-ник получает фактически бесконечные попытки.

## API
- `GET /api/auth/github/login` — ссылка на GitHub OAuth.
- `GET /api/auth/github/callback` — обмен code на токен, создание пользователя, отдача payload через postMessage.
- `POST /api/attempts/start` — `{ "userId": 1 }`, создаёт попытку и выдаёт вопросы.
- `POST /api/attempts/{id}/submit` — `{ "userId": 1, "answers": [{ "questionId": 1, "selectedIndexes": [0] }] }`, считает баллы, хранит ошибки.
- `GET /api/attempts/status/{userId}` — список попыток и оставшееся количество.
- `GET /api/config` — лимиты, длительность, имя теста.

## Экспорт результатов
- `just export` или `uv run python export_results.py --json test.json -o report.xlsx`
- БД берётся как `<json>.db` по умолчанию; можно указать `--db`.
- В XLSX вкладки по попыткам + лист вопросов, ячейки с ответами подсвечены (зелёный/красный), заголовки с вопросами линкуются на лист вопросов.

## Тесты
- Backend/экспорт: `uv run pytest`
- Для сброса БД: `just rmdb` (делает бэкап `<db>.bak.<timestamp>` и удаляет оригинал)
