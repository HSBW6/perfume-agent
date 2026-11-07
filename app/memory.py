"""用户画像记忆层（SQLite）。

本模块同时承担整个 Demo 的数据库连接与建表职责：
- profile  : 用户画像，覆盖式更新（新值覆盖旧值，绝不追加）
- formula  : 配方库，含 secret_blob 机密列（只允许写入，任何读取路径都不取它）
- audit_log: 审计日志

设计约束（来自需求）：
1. 画像写入必须是"覆盖"语义，不做列表累加；
2. 画像落盘在 SQLite 文件里，进程重启后仍可读；
3. 时间统一用字符串，避免 Python 3.12+ 对 datetime 适配器的弃用警告。
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 项目根目录（app/ 的上一级），用于定位 data/ 下的种子文件与默认 db 路径
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DB_PATH") or (BASE_DIR / "data" / "perfume.db"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH

# 画像允许的结构化字段，顺序即前端展示顺序
PROFILE_FIELDS = ("occasion", "scent_family", "intensity", "taboos", "history")

# taboos / history 以 JSON 数组形式落库
_LIST_FIELDS = ("taboos", "history")

DEFAULT_PROFILE = {
    "user_id": "",
    "occasion": "",
    "scent_family": "",
    "intensity": "",
    "taboos": [],
    "history": [],
    "updated_at": "",
}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def get_conn():
    """短连接：每次操作独立开关，避免多线程共享连接的问题。"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """建表 + 首次启动灌入种子配方。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profile (
                user_id      TEXT PRIMARY KEY,
                occasion     TEXT DEFAULT '',
                scent_family TEXT DEFAULT '',
                intensity    TEXT DEFAULT '',
                taboos       TEXT DEFAULT '[]',
                history      TEXT DEFAULT '[]',
                updated_at   TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS formula (
                formula_id  TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                family      TEXT NOT NULL,
                notes       TEXT NOT NULL,
                intensity   TEXT NOT NULL,
                tags        TEXT NOT NULL,
                audience    TEXT DEFAULT '',
                secret_blob TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT,
                event      TEXT NOT NULL,
                detail     TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        count = conn.execute("SELECT COUNT(*) AS c FROM formula").fetchone()["c"]
    if count == 0:
        seed_formulas()


def seed_formulas() -> int:
    """把 data/seed_formulas.json 的虚构配方灌进库里，返回写入条数。"""
    seed_file = BASE_DIR / "data" / "seed_formulas.json"
    if not seed_file.exists():
        return 0
    rows = json.loads(seed_file.read_text(encoding="utf-8"))
    with get_conn() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO formula
                    (formula_id, name, family, notes, intensity, tags, audience, secret_blob)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["formula_id"],
                    r["name"],
                    r["family"],
                    r["notes"],
                    r["intensity"],
                    json.dumps(r.get("tags", []), ensure_ascii=False),
                    r.get("audience", ""),
                    r.get("secret_blob", ""),
                ),
            )
    return len(rows)


# --------------------------------------------------------------------------
# 画像读写
# --------------------------------------------------------------------------


def _row_to_profile(row: sqlite3.Row) -> dict:
    p = dict(DEFAULT_PROFILE)
    p["user_id"] = row["user_id"]
    p["occasion"] = row["occasion"] or ""
    p["scent_family"] = row["scent_family"] or ""
    p["intensity"] = row["intensity"] or ""
    p["taboos"] = json.loads(row["taboos"] or "[]")
    p["history"] = json.loads(row["history"] or "[]")
    p["updated_at"] = row["updated_at"] or ""
    return p


def get_profile(user_id: str) -> dict:
    """读取画像；没有记录时返回带默认值的空画像（不落库）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM profile WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None:
        p = dict(DEFAULT_PROFILE)
        p["user_id"] = user_id
        return p
    return _row_to_profile(row)


def upsert_profile(user_id: str, fields: dict) -> dict:
    """覆盖式更新画像。

    规则：
    - 只接受 PROFILE_FIELDS 里的键，未知键直接忽略；
    - 新值为空（None / "" / []）时视为"这次没提到"，保留旧值；
    - 新值非空则**整体替换**该字段，不做列表合并或累加。
    """
    current = get_profile(user_id)
    merged = {k: current.get(k) for k in PROFILE_FIELDS}

    for key in PROFILE_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple)) and len(value) == 0:
            continue
        merged[key] = list(value) if key in _LIST_FIELDS else value

    merged["updated_at"] = now_str()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO profile
                (user_id, occasion, scent_family, intensity, taboos, history, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                merged["occasion"] or "",
                merged["scent_family"] or "",
                merged["intensity"] or "",
                json.dumps(merged["taboos"] or [], ensure_ascii=False),
                json.dumps(merged["history"] or [], ensure_ascii=False),
                merged["updated_at"],
            ),
        )
    merged["user_id"] = user_id
    return merged


def list_profiles() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM profile ORDER BY user_id").fetchall()
    return [_row_to_profile(r) for r in rows]


# --------------------------------------------------------------------------
# 演示重置
# --------------------------------------------------------------------------


def reset_all() -> None:
    """清空演示数据并重新灌种子配方（画像不预置，由调用方决定）。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM profile")
        conn.execute("DELETE FROM formula")
        conn.execute("DELETE FROM audit_log")
    seed_formulas()
