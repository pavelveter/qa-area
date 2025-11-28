import json
import os
import random
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
TEST_FILE = os.getenv("QUIZ_FILE", "test.json")
QUESTIONS_PATH = BASE_DIR / TEST_FILE
DB_PATH = QUESTIONS_PATH.with_suffix(".db")
ATTEMPT_LIMIT = int(os.getenv("QUIZ_ATTEMPT_LIMIT", "3"))
ATTEMPT_DURATION_SECONDS = int(os.getenv("QUIZ_ATTEMPT_MINUTES", "60")) * 60
STATE_TTL_SECONDS = 600
LECTOR = os.getenv("LECTOR", "").strip().lower()
UNLIMITED_ATTEMPTS = 10**9

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_REDIRECT_URL = os.getenv("GITHUB_REDIRECT_URL")  # optional override

state_store: Dict[str, datetime] = {}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def is_lector(username: Optional[str]) -> bool:
    return bool(username) and username.lower() == LECTOR


def attempts_left(username: str, attempts_done: int) -> int:
    if is_lector(username):
        return UNLIMITED_ATTEMPTS
    return max(0, ATTEMPT_LIMIT - attempts_done)


def ensure_schema():
    DB_PATH.touch(exist_ok=True)
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                github_username TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                attempt_number INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                finished_at TEXT,
                score INTEGER,
                total_questions INTEGER,
                answers_json TEXT,
                option_mapping_json TEXT NOT NULL,
                incorrect_json TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );
            """
        )
        conn.commit()


def load_questions() -> Dict:
    with QUESTIONS_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


RAW_TEST = load_questions()
TEST_NAME = RAW_TEST.get("name", "QA Quiz")
QUESTIONS = {q["id"]: q for q in RAW_TEST.get("questions", [])}


class StartAttemptRequest(BaseModel):
    userId: int


class AnswerPayload(BaseModel):
    questionId: int
    selectedIndexes: List[int]


class SubmitAttemptRequest(BaseModel):
    answers: List[AnswerPayload]
    userId: int


app = FastAPI(title="Quiz Runner", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.on_event("startup")
def startup():
    ensure_schema()
    # best-effort cleanup
    clean_state()


def get_user(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        "SELECT id, github_username AS username, created_at FROM users WHERE github_username = ?",
        (username,),
    )
    return cur.fetchone()


def clean_state():
    now = datetime.now(timezone.utc)
    stale = [
        k
        for k, v in state_store.items()
        if (now - v).total_seconds() > STATE_TTL_SECONDS
    ]
    for key in stale:
        state_store.pop(key, None)


@app.get("/api/auth/github/login")
def github_login(request: Request):
    if not (GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET):
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured")

    clean_state()
    state = secrets.token_urlsafe(32)
    state_store[state] = datetime.now(timezone.utc)

    redirect_uri = GITHUB_REDIRECT_URL or str(request.url_for("github_callback"))
    url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        "&scope=read:user"
        f"&state={state}"
    )
    return {"url": url}


@app.get("/api/auth/github/callback", name="github_callback")
async def github_callback(code: str, state: str, request: Request):
    clean_state()
    issued = state_store.pop(state, None)
    if issued is None:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    redirect_uri = GITHUB_REDIRECT_URL or str(request.url_for("github_callback"))
    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
                "state": state,
            },
            timeout=15,
        )
        token_res.raise_for_status()
        token_data = token_res.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="OAuth token missing")

        user_res = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        user_res.raise_for_status()
        gh_user = user_res.json()

    username = gh_user.get("login")
    if not username:
        raise HTTPException(status_code=400, detail="GitHub login missing")

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        user = get_user(conn, username)
        if user is None:
            conn.execute(
                "INSERT INTO users (github_username, created_at) VALUES (?, ?)",
                (username, now),
            )
            conn.commit()
            user = get_user(conn, username)

        attempts_done = conn.execute(
            "SELECT COUNT(*) AS cnt FROM attempts WHERE user_id = ?", (user["id"],)
        ).fetchone()["cnt"]

    is_lector_flag = is_lector(user["username"])
    attempts_left_value = attempts_left(user["username"], attempts_done)
    payload = {
        "userId": user["id"],
        "username": user["username"],
        "attemptsLeft": attempts_left_value,
        "isLector": is_lector_flag,
    }

    # Popup-friendly HTML sends data back to opener
    html = f"""
    <html><body><script>
      (function() {{
        const payload = {json.dumps(payload)};
        if (window.opener) {{
          window.opener.postMessage({{ type: "github-auth", payload }}, "*");
          window.close();
        }} else {{
          document.body.innerText = "OAuth завершён, окно можно закрыть.";
        }}
      }})();
    </script></body></html>
    """
    return HTMLResponse(content=html)


def build_option_mapping_and_questions() -> Dict[str, Dict]:
    option_mapping: Dict[int, List[int]] = {}
    questions_payload = []
    for q in QUESTIONS.values():
        order = list(range(len(q["options"])))
        random.shuffle(order)
        option_mapping[q["id"]] = order
        questions_payload.append(
            {
                "id": q["id"],
                "topic": q.get("topic"),
                "text": q["text"],
                "multiple": q.get("multiple", False),
                "options": [q["options"][idx] for idx in order],
            }
        )
    return {"option_mapping": option_mapping, "questions": questions_payload}


@app.post("/api/attempts/start")
def start_attempt(payload: StartAttemptRequest):
    with get_db() as conn:
        user = conn.execute(
            "SELECT id, github_username AS username FROM users WHERE id = ?",
            (payload.userId,),
        ).fetchone()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        attempts_done = conn.execute(
            "SELECT COUNT(*) AS cnt FROM attempts WHERE user_id = ?", (payload.userId,)
        ).fetchone()["cnt"]
        if not is_lector(user["username"]) and attempts_done >= ATTEMPT_LIMIT:
            raise HTTPException(status_code=403, detail="Attempt limit reached")

        attempt_number = attempts_done + 1
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(seconds=ATTEMPT_DURATION_SECONDS)
        mapping_and_questions = build_option_mapping_and_questions()

        conn.execute(
            """
            INSERT INTO attempts (
                user_id, attempt_number, started_at, deadline_at, option_mapping_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload.userId,
                attempt_number,
                now.isoformat(),
                deadline.isoformat(),
                json.dumps(mapping_and_questions["option_mapping"]),
            ),
        )
        conn.commit()
        attempt_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    return {
        "attemptId": attempt_id,
        "attemptNumber": attempt_number,
        "deadline": deadline.isoformat(),
        "questions": mapping_and_questions["questions"],
    }


def evaluate_attempt(
    option_mapping: Dict[str, List[int]], answers: List[AnswerPayload]
):
    score = 0
    incorrect_details = []
    total = len(QUESTIONS)
    answer_map = {a.questionId: a.selectedIndexes for a in answers}

    for qid, q in QUESTIONS.items():
        presented_indices = option_mapping.get(str(qid)) or option_mapping.get(qid)
        if presented_indices is None:
            continue

        selected = answer_map.get(qid, [])
        original_selected = [
            presented_indices[idx] for idx in selected if idx < len(presented_indices)
        ]

        if q.get("multiple"):
            correct = set(q.get("correctIndexes", []))
            is_correct = set(original_selected) == correct
        else:
            correct_idx = q.get("correctIndex")
            is_correct = (
                len(original_selected) == 1 and original_selected[0] == correct_idx
            )

        if is_correct:
            score += 1
        else:
            incorrect_details.append(
                {
                    "id": qid,
                    "text": q["text"],
                    "topic": q.get("topic"),
                    "correct": (
                        [q["options"][i] for i in q.get("correctIndexes", [])]
                        if q.get("multiple")
                        else [q["options"][q["correctIndex"]]]
                    ),
                    "selected": [q["options"][i] for i in original_selected],
                }
            )

    return score, total, incorrect_details


@app.post("/api/attempts/{attempt_id}/submit")
def submit_attempt(attempt_id: int, payload: SubmitAttemptRequest):
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        attempt = conn.execute(
            """
            SELECT a.*, u.github_username AS username
            FROM attempts a
            JOIN users u ON u.id = a.user_id
            WHERE a.id = ? AND a.user_id = ?
            """,
            (attempt_id, payload.userId),
        ).fetchone()
        if attempt is None:
            raise HTTPException(status_code=404, detail="Attempt not found")
        if attempt["finished_at"]:
            raise HTTPException(status_code=400, detail="Attempt already submitted")

        deadline = datetime.fromisoformat(attempt["deadline_at"])
        if now > deadline:
            raise HTTPException(status_code=400, detail="Attempt time expired")

        option_mapping = json.loads(attempt["option_mapping_json"])
        score, total, incorrect_details = evaluate_attempt(
            option_mapping, payload.answers
        )

        conn.execute(
            """
            UPDATE attempts
            SET finished_at = ?, score = ?, total_questions = ?, answers_json = ?, incorrect_json = ?
            WHERE id = ?
            """,
            (
                now.isoformat(),
                score,
                total,
                json.dumps([a.dict() for a in payload.answers]),
                json.dumps(incorrect_details),
                attempt_id,
            ),
        )
        conn.commit()

        attempts_done = conn.execute(
            "SELECT COUNT(*) AS cnt FROM attempts WHERE user_id = ?", (payload.userId,)
        ).fetchone()["cnt"]

    attempts_left_value = attempts_left(attempt["username"], attempts_done)
    return {
        "score": score,
        "total": total,
        "attemptsLeft": attempts_left_value,
        "incorrect": incorrect_details,
    }


@app.get("/")
def root():
    index = static_dir / "index.html"
    if not index.exists():
        raise HTTPException(status_code=500, detail="Frontend not built")
    return FileResponse(index)


@app.get("/api/attempts/status/{user_id}")
def attempt_status(user_id: int):
    with get_db() as conn:
        user_row = conn.execute(
            "SELECT github_username AS username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user_row is None:
            raise HTTPException(status_code=404, detail="User not found")

        attempts = conn.execute(
            """
            SELECT id, attempt_number, started_at, finished_at, deadline_at, score, total_questions
            FROM attempts WHERE user_id = ?
            ORDER BY attempt_number DESC
            """,
            (user_id,),
        ).fetchall()

    attempts_left_value = attempts_left(user_row["username"], len(attempts))
    is_lector_flag = is_lector(user_row["username"])
    return {
        "attempts": [dict(row) for row in attempts],
        "attemptsLeft": attempts_left_value,
        "isLector": is_lector_flag,
    }


@app.get("/api/questions/sample")
def sample_question():
    return {
        "topics": list({q["topic"] for q in QUESTIONS.values()}),
        "total": len(QUESTIONS),
    }


@app.get("/api/config")
def get_config():
    return {
        "attemptLimit": ATTEMPT_LIMIT,
        "attemptMinutes": ATTEMPT_DURATION_SECONDS // 60,
        "name": TEST_NAME,
    }
