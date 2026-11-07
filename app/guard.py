"""安全护栏：注入防护 + 输出白名单 + 审计留痕。

三个职责分开写清楚：
- check_injection()  ：识别"套配方"的对话请求，命中就拒绝；
- sanitize_payload() ：最终出口清洗，递归剥掉任何机密键名（最后一道保险）；
- write_audit()/list_audit()：三类事件 query / recommend / blocked_injection 落库与查询。
"""

import json
import re
from datetime import datetime

from .memory import get_conn

# ---------------------------------------------------------------------------
# 一、注入防护
# ---------------------------------------------------------------------------

# 强信号：只要出现就说明用户在问配方细节，无需组合判断
STRONG_SIGNALS = (
    "配比",
    "比例",
    "用量",
    "克数",
    "多少克",
    "多少毫升",
    "毫升",
    "秘方",
    "配方表",
    "配方文件",
    "配方数据",
    "配方库的字段",
    "念给我",
    "念一遍",
    "念出",
    "背出",
    "列出来",
    "打印出来",
    "导出来",
    "数据库",
    "源码",
    "源代码",
    "secret",
    "blob",
    "sql",
)

# 弱信号组合：出现"配方"这类词时，还要叠加动作性词汇才判定为套话，
# 避免把"想要一个清新一点的配方推荐"这种正常需求误拦。
WEAK_NOUNS = ("配方", "香方", "原料", "成分")
WEAK_VERBS = (
    "念",
    "列",
    "给我看",
    "给我列",
    "说说",
    "详细",
    "具体",
    "全部",
    "所有",
    "逐个",
    "一条条",
    "发我",
)

# 明显的越权/越轨意图
EXPLICIT_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|above)", re.I),
    re.compile(r"system\s*prompt", re.I),
    re.compile(r"开发者模式|越狱|jailbreak", re.I),
)


def check_injection(message: str) -> tuple[bool, str]:
    """返回 (是否拦截, 命中原因)。"""
    text = (message or "").strip()
    if not text:
        return False, ""

    low = text.lower()

    for pat in EXPLICIT_PATTERNS:
        if pat.search(text):
            return True, f"越权指令：{pat.pattern}"

    for word in STRONG_SIGNALS:
        if word.lower() in low:
            return True, f"命中强信号词「{word}」"

    has_noun = any(n in text for n in WEAK_NOUNS)
    has_verb = any(v in text for v in WEAK_VERBS)
    if has_noun and has_verb:
        nouns = "、".join(n for n in WEAK_NOUNS if n in text)
        verbs = "、".join(v for v in WEAK_VERBS if v in text)
        return True, f"弱信号组合命中：{nouns} + {verbs}"

    return False, ""


# ---------------------------------------------------------------------------
# 二、输出白名单清洗（最后一道保险）
# ---------------------------------------------------------------------------

# 出现这些键名的一律剔除，不论值是什么
FORBIDDEN_KEYS = ("secret", "secret_blob", "formula_secret", "recipe", "ratio", "ingredient_ratio")


def sanitize_payload(obj):
    """递归剥离机密字段，返回一份干净的结构。"""
    if isinstance(obj, dict):
        return {
            k: sanitize_payload(v)
            for k, v in obj.items()
            if not any(bad in str(k).lower() for bad in FORBIDDEN_KEYS)
        }
    if isinstance(obj, list):
        return [sanitize_payload(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# 三、审计日志
# ---------------------------------------------------------------------------

# 事件类型：query（普通提问）/ recommend（产生推荐）/ blocked_injection（拦截）
EVENT_QUERY = "query"
EVENT_RECOMMEND = "recommend"
EVENT_BLOCKED = "blocked_injection"


def write_audit(user_id: str, event: str, detail) -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (user_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (user_id or "anonymous", event, detail, ts),
        )


def list_audit(limit: int = 100, event: str | None = None) -> list[dict]:
    """查询审计日志。

    兼容历史命名：外部若按 block_injection 过滤，这里一并返回 blocked_injection。
    """
    sql = "SELECT id, user_id, event, detail, created_at FROM audit_log"
    params: list = []
    if event:
        alias = "blocked_injection" if event == "block_injection" else event
        sql += " WHERE event = ?"
        params.append(alias)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
