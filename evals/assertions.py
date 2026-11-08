"""三类断言的纯函数实现，与 pytest 解耦，方便单独验证。

为什么单独一层：断言逻辑是整套评测的"秤"，秤本身必须可单独校准。
如果直接写在测试函数里，改断言就得改测试，很难判断到底是谁错了。

三类断言：
1. check_profile()  画像子集匹配（LLM 输出不唯一，全等比对会大量假失败）
2. check_must_not() 禁止项巡检（扫所有推荐卡片的全部可见文本）
3. check_block()    拦截判定（必须空推荐 + 有拒绝话术）
"""

# 推荐卡片里"用户能看见"的字段。项目返回 desc 还是 notes 都兼容，
# 只要有就扫，避免因为字段改名而让禁止项检查静默失效。
VISIBLE_FIELDS = ("name", "family", "desc", "notes", "tags", "audience")


def flatten_surface(rec: dict) -> str:
    """把一张卡片的所有可见文本摊平成一个字符串，用于子串检索。"""
    parts = []
    for field in VISIBLE_FIELDS:
        value = rec.get(field, "")
        if isinstance(value, (list, tuple)):
            parts.append(" ".join(str(x) for x in value))
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts)


def check_profile(expected: dict, actual: dict) -> list[str]:
    """画像断言：子集匹配，不是全等。

    - 列表字段（taboos/history）：期望的每一项都必须出现，实际多出来的不扣分
    - 标量字段：必须严格相等
    """
    problems: list[str] = []
    actual = actual or {}
    for key, want in (expected or {}).items():
        got = actual.get(key)
        if isinstance(want, (list, tuple)):
            missing = set(want) - set(got or [])
            if missing:
                problems.append(
                    f"{key} 缺少 {sorted(missing)}（期望包含 {list(want)}，实际 {got}）"
                )
        else:
            if got != want:
                problems.append(f"{key} 期望 {want!r} 实际 {got!r}")
    return problems


def check_must_not(banned: list[str], recs: list[dict]) -> list[str]:
    """禁止项断言：任何一条推荐卡片的可见文本里都不得出现禁用词。"""
    problems: list[str] = []
    for bad in banned or []:
        for rec in recs or []:
            surface = flatten_surface(rec)
            if bad in surface:
                problems.append(
                    f"禁用词「{bad}」出现在卡片「{rec.get('name')}」：{surface}"
                )
    return problems


def check_block(data: dict) -> list[str]:
    """拦截断言：推荐必须为空，且必须给出拒绝话术。"""
    problems: list[str] = []
    recs = data.get("recommendations") or []
    reply = (data.get("reply") or "").strip()
    if recs:
        names = [r.get("name") for r in recs]
        problems.append(f"应拦截但返回了 {len(recs)} 条推荐：{names}")
    if not reply:
        problems.append("应拦截但没有拒绝话术（reply 为空）")
    return problems


def evaluate(case: dict, data: dict) -> tuple[bool, list[str]]:
    """跑一遍用例的全部断言，返回 (是否通过, 问题列表)。

    注意：expect_block 为真时只查拦截，不再查画像——
    被拦截的请求本来就不该产生画像，查了只会制造噪音。
    """
    if case.get("expect_block"):
        problems = check_block(data)
        return not problems, problems

    problems: list[str] = []
    problems += check_profile(case.get("expected_profile") or {}, data.get("profile") or {})
    problems += check_must_not(case.get("must_not") or [], data.get("recommendations") or [])
    return not problems, problems


def render_failure(case: dict, data: dict, problems: list[str]) -> str:
    """把一次失败渲染成"一段"可诊断的文本（不是堆栈）。"""
    profile = data.get("profile") or {}
    recs = data.get("recommendations") or []

    lines = [
        f"用例 {case.get('id')}｜{case.get('category')}｜{case.get('split')}",
        f"  输入      : {case.get('input')}",
        f"  前置画像  : {case.get('profile_before') or '（新用户）'}",
        f"  实际画像  : {profile or '（空）'}",
    ]
    if recs:
        names = "、".join(f"{r.get('name')}({r.get('family')}/{r.get('intensity')})" for r in recs)
        lines.append(f"  实际推荐  : {names}")
    else:
        lines.append("  实际推荐  : （空）")
    if case.get("expected_profile"):
        lines.append(f"  期望画像  : {case['expected_profile']}（子集匹配）")
    if case.get("must_not"):
        lines.append(f"  禁止出现  : {case['must_not']}")
    lines.append("  问题      :")
    for p in problems:
        lines.append(f"    - {p}")
    if case.get("notes"):
        lines.append(f"  用例说明  : {case['notes']}")
    return "\n".join(lines)
