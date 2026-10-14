"""SQLite 저장소. 분석/평가 결과는 JSON 문자열로 보관한다."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL,          -- path | git | upload | url
    source_ref TEXT NOT NULL,           -- 원본 경로/URL/파일명
    local_path TEXT NOT NULL,           -- 실제 분석 대상 경로
    owner TEXT DEFAULT '',
    department TEXT DEFAULT '',
    description TEXT DEFAULT '',
    importance INTEGER,                 -- 업무 중요도 1~5 (사람이 입력)
    monthly_users INTEGER,              -- 월간 사용자 수 (사람이 입력)
    decision TEXT DEFAULT '',           -- 최종 의사결정 (사람이 입력)
    decision_note TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',      -- pending | analyzing | done | error
    error TEXT DEFAULT '',
    analysis TEXT,
    evaluation TEXT,
    created_at TEXT,
    evaluated_at TEXT,
    UNIQUE (source_type, source_ref)
);
CREATE TABLE IF NOT EXISTS group_plans (
    key TEXT PRIMARY KEY,               -- 정렬된 artifact id 목록 (예: "3,7,12")
    plan TEXT,
    created_at TEXT
);
"""

JSON_FIELDS = ("analysis", "evaluation")
META_FIELDS = ("name", "owner", "department", "description", "importance",
               "monthly_users", "decision", "decision_note")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    s = config.settings
    s.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(s.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for f in JSON_FIELDS:
        d[f] = json.loads(d[f]) if d.get(f) else None
    return d


def find_by_source(source_type: str, source_ref: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM artifacts WHERE source_type=? AND source_ref=?",
                      (source_type, source_ref)).fetchone()
    return _row(r)


def create(name: str, source_type: str, source_ref: str, local_path: str) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO artifacts (name, source_type, source_ref, local_path, created_at) VALUES (?,?,?,?,?)",
            (name, source_type, source_ref, local_path, now()))
        return int(cur.lastrowid)


def get(artifact_id: int) -> dict | None:
    with connect() as c:
        return _row(c.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone())


def list_all() -> list[dict]:
    with connect() as c:
        return [_row(r) for r in c.execute("SELECT * FROM artifacts ORDER BY id").fetchall()]


def update(artifact_id: int, **fields: Any) -> None:
    if not fields:
        return
    for f in JSON_FIELDS:
        if f in fields and fields[f] is not None:
            fields[f] = json.dumps(fields[f], ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE artifacts SET {cols} WHERE id=?", (*fields.values(), artifact_id))


def delete(artifact_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))


def get_plan(key: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM group_plans WHERE key=?", (key,)).fetchone()
    if r is None:
        return None
    return {"key": r["key"], "plan": json.loads(r["plan"]), "created_at": r["created_at"]}


def save_plan(key: str, plan: dict) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO group_plans (key, plan, created_at) VALUES (?,?,?)",
                  (key, json.dumps(plan, ensure_ascii=False), now()))
