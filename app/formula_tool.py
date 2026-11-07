"""配方库的**唯一**受控查询入口。

安全红线（演示的核心卖点）：
1. SQL 里只 SELECT 下面 PUBLIC_COLUMNS 这 7 列，secret_blob 永远不出现在 SELECT 子句中；
2. 任何模块想查配方，都必须调用本文件的 search_formulas() / get_formula() / list_catalog()，
   不允许在别处手写 SQL 直连 formula 表；
3. 返回的对象在出口处再校验一次键名，防止有人改代码时顺手把机密字段带出去。
"""

from .memory import get_conn

# 对外暴露的字段白名单（与需求文档一致，一个不多一个不少）
PUBLIC_COLUMNS = ("formula_id", "name", "family", "notes", "intensity", "tags", "audience")

# 所有查询共用这一段列清单，杜绝各处手写 "*" 或 "secret_blob"
_SELECT_LIST = ", ".join(PUBLIC_COLUMNS)


def _row_to_public(row) -> dict:
    """把数据库行转成只含白名单字段的 dict。"""
    import json

    item = {
        "formula_id": row["formula_id"],
        "name": row["name"],
        "family": row["family"],
        "notes": row["notes"],
        "intensity": row["intensity"],
        "tags": json.loads(row["tags"] or "[]"),
        "audience": row["audience"] or "",
    }
    assert_clean(item)
    return item


def assert_clean(obj) -> None:
    """出口断言：一旦发现机密字段混入返回值，立刻抛错而不是静默放行。"""
    forbidden = ("secret", "formula_secret", "ratio", "recipe")
    if isinstance(obj, dict):
        for k, v in obj.items():
            low = str(k).lower()
            for bad in forbidden:
                if bad in low:
                    raise RuntimeError(f"检测到机密字段外泄尝试：{k}")
            assert_clean(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            assert_clean(v)


def search_formulas(
    family: str | None = None,
    intensity: str | None = None,
    tags: list[str] | None = None,
    keyword: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """受控查询：按香调家族 / 强度 / 标签 / 关键词过滤配方。

    - family    精确匹配香调家族（柑橘/花香/木质/东方/清新）
    - intensity 精确匹配强度（淡/中/浓）
    - tags      任一标签命中即算（OR），用于氛围/感觉类筛选
    - keyword   在 name / notes / tags 上做模糊匹配
    """
    sql = f"SELECT {_SELECT_LIST} FROM formula WHERE 1 = 1"
    params: list = []

    if family:
        sql += " AND family = ?"
        params.append(family)
    if intensity:
        sql += " AND intensity = ?"
        params.append(intensity)
    if keyword:
        like = f"%{keyword}%"
        sql += " AND (name LIKE ? OR notes LIKE ? OR tags LIKE ?)"
        params.extend([like, like, like])

    sql += " ORDER BY formula_id LIMIT ?"
    params.append(int(limit))

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    items = [_row_to_public(r) for r in rows]

    if tags:
        wanted = set(tags)
        hit = [i for i in items if wanted & set(i["tags"])]
        # 标签筛选按"命中个数"排序，命中越多越靠前
        hit.sort(key=lambda i: len(wanted & set(i["tags"])), reverse=True)
        items = hit

    return items


def get_formula(formula_id: str) -> dict | None:
    """按 id 取单条配方（仍走白名单列）。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_SELECT_LIST} FROM formula WHERE formula_id = ?", (formula_id,)
        ).fetchone()
    return _row_to_public(row) if row else None


def list_catalog(family: str | None = None) -> list[dict]:
    """香氛库列表，可按家族筛选。"""
    return search_formulas(family=family, limit=200)


def count_formulas() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM formula").fetchone()["c"]
