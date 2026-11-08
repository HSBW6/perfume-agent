"""Agent 编排：画像抽取 + 千人千香推荐。

流程：
1. 先过注入防护（由调用方 main.py 调用 guard，这里只管业务）；
2. 从这句话里抽取结构化偏好（LLM 优先，无 Key 或失败时用规则）；
3. 覆盖式写回画像；
4. 用「画像 + 本次诉求」给配方打分排序，产出推荐卡片；
5. 生成回复文案。

千人千香的关键在第 4 步的打分函数：同一句话，画像不同（场合、强度、禁忌、历史）
会得到不同的排序，所以结果必然不同。
"""

import json
import re

from . import llm, memory
from .formula_tool import assert_clean, search_formulas

FAMILIES = ("柑橘", "花香", "木质", "东方", "清新")
INTENSITIES = ("淡", "中", "浓")

# 家族关键词（顺序有意义：先匹配更具体的）
FAMILY_KEYWORDS = {
    "柑橘": ("柑橘", "柠檬", "青柠", "橙", "佛手柑", "柚", "橘"),
    "花香": ("花香", "鸢尾", "玫瑰", "茉莉", "栀子", "铃兰", "桃", "花瓣", "紫罗兰"),
    "木质": ("木质", "檀", "雪松", "广藿", "松木", "苔藓", "木调", "木香"),
    "东方": ("东方", "琥珀", "沉香", "香草", "麝香", "乳香", "焚香", "树脂"),
    "清新": ("清新", "清爽", "清透", "干净", "茶香", "海洋", "薄荷", "绿叶", "雨后", "白花"),
}

# 场合关键词 → 受众词（用于和 formula.audience 做匹配）
OCCASION_KEYWORDS = {
    "约会": ("约会", "恋爱", "见他", "见她", "心动"),
    "婚礼": ("婚礼", "结婚", "新娘", "伴娘"),
    "伴手礼": ("伴手礼", "礼物", "送人", "送礼"),
    "成年礼": ("成年礼", "18岁", "十八岁", "18 岁"),
    "通勤": ("通勤", "上班", "办公", "工位", "面试"),
    "运动": ("运动", "健身", "跑步", "瑜伽"),
    "晚宴": ("晚宴", "宴会", "聚會", "聚会", "酒会"),
    "日常": ("日常", "平时", "出门", "逛街"),
}

# 强度词：正向（"别太浓/不浓"这类带否定的说法不放在这里，交给否定逻辑处理）
INTENSITY_STRONG = ("浓", "浓郁", "强烈", "厚重", "持久", "重一点")
INTENSITY_LIGHT = ("淡", "清淡", "轻盈", "轻一点", "薄")
INTENSITY_MID = ("中", "适中", "中等", "刚好")

# 相对调整信号：出现这些词说明是在"原来基础上调一档"，而不是直接指定
RELATIVE_WORDS = ("一点", "稍微", "稍稍", "别太", "不要太", "降低", "弱化", "加强")


# ---------------------------------------------------------------------------
# 否定语义：作用域切分 + 对象归类
# ---------------------------------------------------------------------------
# 为什么要单独一层：旧实现是"整句扫描，否定词挨着目标词就算否定"，有两个典型错误：
#   1. 「我不太喜欢太浓的味道」——看到"浓"就写 intensity=浓，前面的否定被无视；
#   2. 「不要玫瑰，但茉莉可以」——否定越过逗号，把后一分句的茉莉也否定了。
#
# 现在的做法四步走：
#   切分句 → 分句内定位否定标记与被否定对象 → 对象按类别归档 → 正向表达优先。
# 否定只管辖它所在的分句，跨分句不继承、不传递。

# 分句边界：逗号/句号/分号/感叹问号/冒号/换行，以及"但""不过"。
# "除了"用零宽断言切开，让它留在后一分句开头——它本身是否定标记，不能当边界丢掉。
SCOPE_SPLIT = re.compile(r"[，,。；;！!？?：:\n]+|但是|但|不过|(?=除了)")

# 前缀式否定：否定词在被否定对象之前
NEG_PREFIX_TRIGGERS = (
    "不太喜欢",
    "不喜欢",
    "不想要",
    "不太爱",
    "受不了",
    "不要",
    "不太",
    "别太",
    "别",
    "讨厌",
    "避开",
    "除了",
    "不想",
    "忌",
)

# 后缀式否定：对象在前、否定词在后（"柑橘…受不了""玫瑰也过敏""茉莉不行"）
NEG_SUFFIX_TRIGGERS = (
    "受不了",
    "不行",
    "闻着头晕",
    "头晕",
    "有点上头",
    "上头",
    "过敏",
    "不喜欢",
    "讨厌",
)

# 香调类对象：命中后记进 taboos
FAMILY_TARGETS = (
    "佛手柑",
    "紫罗兰",
    "白花",
    "玫瑰",
    "茉莉",
    "柑橘",
    "柠檬",
    "青柠",
    "花香",
    "木质",
    "檀香",
    "雪松",
    "广藿",
    "苔藓",
    "东方",
    "琥珀",
    "沉香",
    "香草",
    "麝香",
    "乳香",
    "焚香",
    "树脂",
    "清新",
    "海洋",
    "薄荷",
    "绿叶",
    "茶香",
    "栀子",
    "铃兰",
    "鸢尾",
    "松木",
    "花瓣",
    "檀",
    "橘",
    "桃",
)

# 强度类对象：被否定后沿强度轴取反，而不是记进 taboos
INTENSITY_TARGETS = (
    ("太浓", "浓"),
    ("浓烈", "浓"),
    ("强烈", "浓"),
    ("厚重", "浓"),
    ("太重", "浓"),
    ("太冲", "浓"),
    ("刺鼻", "浓"),
    ("浓郁", "浓"),
    ("浓", "浓"),
    ("重", "浓"),
    ("冲", "浓"),
    ("甜", "浓"),
    ("腻", "浓"),
    ("太淡", "淡"),
    ("清淡", "淡"),
    ("淡", "淡"),
    ("轻", "淡"),
)

# 场合类对象：被否定时不做反向推断，直接忽略这个场合
OCCASION_TARGETS = ("上班", "通勤", "约会", "晚宴", "婚礼", "运动", "面试", "聚会")

# 强度轴取反：否定"浓"意味着想要更淡，反之亦然
INTENSITY_INVERT = {"浓": "淡", "淡": "浓"}

_INTENSITY_DIRECTION = dict(INTENSITY_TARGETS)

# 预生成检索词典：同类按词长降序（"太浓"必须先于"浓"匹配，否则长词永远吃不到）
_TARGET_LEXICON: list[tuple[str, str]] = []
for _kind, _words in (
    ("intensity", {w for w, _ in INTENSITY_TARGETS}),
    ("family", set(FAMILY_TARGETS)),
    ("occasion", set(OCCASION_TARGETS)),
):
    for _w in sorted(_words, key=len, reverse=True):
        _TARGET_LEXICON.append((_w, _kind))


def _split_scope(text: str) -> list[str]:
    """按分句边界切分，空片段丢掉。"""
    parts = SCOPE_SPLIT.split(text or "")
    return [p.strip() for p in parts if p and p.strip()]


def _iter_targets(clause: str):
    """列出分句里出现的全部"可能被否定的对象"，长词优先且互不重叠。"""
    spans: list[tuple[int, int]] = []
    for word, kind in _TARGET_LEXICON:
        for m in re.finditer(re.escape(word), clause):
            if any(not (m.end() <= s or m.start() >= e) for s, e in spans):
                continue  # 已被更长的词占用（"太浓"里的那个"浓"）
            spans.append((m.start(), m.end()))
            yield word, kind, m.start(), m.end()


def _is_negated(clause: str, start: int, end: int) -> bool:
    """判断分句内某个对象是否处于否定作用域里。

    只在本分句内判断——跨分句一律 False，这就是"否定不跨句"的落点。
    """
    before = clause[:start]
    after = clause[end:]

    # 后缀式："柑橘类我有点受不了"——对象后面 6 个字内出现否定
    for trig in NEG_SUFFIX_TRIGGERS:
        pos = after.find(trig)
        if 0 <= pos <= 6:
            return True

    # 前缀式："不喜欢玫瑰"——对象前面 6 个字内出现否定
    for trig in NEG_PREFIX_TRIGGERS:
        pos = before.rfind(trig)
        if pos >= 0 and len(before[pos + len(trig) :]) <= 6:
            return True
    return False


def _strip_negated(clause: str) -> str:
    """把分句里被否定的对象抹掉，防止它们稍后被当成正向偏好再命中一次。"""
    hits = [(s, e) for _w, _k, s, e in _iter_targets(clause) if _is_negated(clause, s, e)]
    if not hits:
        return clause
    chars = list(clause)
    for s, e in hits:
        for i in range(s, e):
            chars[i] = "□"
    return "".join(chars)


def _negated_objects(clause: str) -> tuple[list[str], list[str], list[str]]:
    """返回 (香调类禁忌, 强度方向列表, 被排除的场合)。"""
    taboos: list[str] = []
    directions: list[str] = []
    blocked_occasions: list[str] = []
    for word, kind, s, e in _iter_targets(clause):
        if not _is_negated(clause, s, e):
            continue
        if kind == "family":
            taboos.append(word)
        elif kind == "intensity":
            directions.append(_INTENSITY_DIRECTION.get(word, "浓"))
        else:
            blocked_occasions.append(word)
    return taboos, directions, blocked_occasions


def analyze_negation(message: str) -> dict:
    """整句范围的否定分析，供画像抽取与推荐两处复用。"""
    taboos: list[str] = []
    directions: list[str] = []
    blocked: list[str] = []
    for clause in _split_scope(message):
        t, d, b = _negated_objects(clause)
        taboos.extend(t)
        directions.extend(d)
        blocked.extend(b)
    return {
        "taboos": _dedupe(taboos),
        "intensity_direction": directions[0] if directions else "",
        "blocked_occasions": _dedupe(blocked),
    }


def _dedupe(items) -> list:
    """保序去重，顺手过滤空值。"""
    seen = set()
    out = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _find_family(clause: str) -> str:
    """在分句里找正向香调；处于否定作用域内的词不参与。"""
    for family, words in FAMILY_KEYWORDS.items():
        for word in words:
            pos = clause.find(word)
            if pos >= 0 and not _is_negated(clause, pos, pos + len(word)):
                return family
    return ""


def _find_occasion(clause: str) -> str:
    """在分句里找正向场合；"不适合上班"这类被否定的场合直接跳过。"""
    for occasion, words in OCCASION_KEYWORDS.items():
        for word in words:
            pos = clause.find(word)
            if pos >= 0 and not _is_negated(clause, pos, pos + len(word)):
                return occasion
    return ""


def _positive_intensity(clause: str, current: str) -> tuple[str, bool]:
    """从"已抹掉否定对象"的分句里读正向强度，返回 (强度, 是否相对调整)。

    "清新一点""别太浓"这类说法没有直接给出档位，只有方向：
    约定"一点/稍微"表示往更轻的方向调，并以**用户自己的旧画像**为基准上下调一档。
    """
    relative = any(w in clause for w in RELATIVE_WORDS)
    current = current if current in INTENSITIES else ""

    if any(w in clause for w in INTENSITY_STRONG):
        base = "浓"
    elif any(w in clause for w in INTENSITY_LIGHT):
        base = "淡"
    elif any(w in clause for w in INTENSITY_MID):
        base = "中"
    elif relative:
        base = "淡"
    else:
        return "", False

    if relative and current:
        idx = INTENSITIES.index(current)
        if base == "淡":
            idx = max(0, idx - 1)
        elif base == "浓":
            idx = min(len(INTENSITIES) - 1, idx + 1)
        return INTENSITIES[idx], True
    return base, False


# ---------------------------------------------------------------------------
# 画像抽取
# ---------------------------------------------------------------------------

LLM_EXTRACT_PROMPT = """你是香水顾问系统的偏好抽取模块。根据用户这句话和已有画像，抽取**新增或变更**的偏好。

只输出 JSON，字段含义：
- occasion: 使用场合，可选 约会/婚礼/伴手礼/成年礼/通勤/运动/晚宴/日常，没有就给 null
- scent_family: 香调家族，可选 柑橘/花香/木质/东方/清新，没有就给 null
- intensity: 浓度，可选 淡/中/浓，没有就给 null
- taboos: 用户明确排斥的元素，字符串数组，没有就给 []
- history: 一句话记录用户对某款香的反馈，没有就给 null

规则：只填用户这句话里真实出现的信息，不要编造；用户没有提到的字段一律 null 或 []。"""


def extract_fields(message: str, current_profile: dict) -> dict:
    """规则抽取（永远可用，保证离线 Demo 跑通）。

    按分句处理，保证否定只作用于它所在的分句：
    - 香调/场合只取"未被否定"的正向表述，取到第一个就停；
    - 强度：有正向就以正向为准，没有才用被否定强度的取反结果（正向优先）；
    - 禁忌：新命中并入旧列表并去重，不覆盖。
    """
    fields: dict = {}
    clauses = _split_scope(message)

    family = ""
    occasion = ""
    positive_intensity = ""
    relative = False
    negated_intensity = ""
    new_taboos: list[str] = []

    for clause in clauses:
        if not family:
            family = _find_family(clause)
        if not occasion:
            occasion = _find_occasion(clause)

        clause_taboos, clause_dirs, _blocked = _negated_objects(clause)
        new_taboos.extend(clause_taboos)
        if clause_dirs and not negated_intensity:
            negated_intensity = clause_dirs[0]

        # 本分句已经有"被否定的强度"，就不再从它里面读正向强度，
        # 否则「我不太喜欢太浓的味道」会把"浓"再当正向偏好算一遍
        if not positive_intensity and not clause_dirs:
            pos, rel = _positive_intensity(
                _strip_negated(clause), current_profile.get("intensity", "")
            )
            if pos:
                positive_intensity, relative = pos, rel

    if family:
        fields["scent_family"] = family
    if occasion:
        fields["occasion"] = occasion

    if positive_intensity:
        fields["intensity"] = positive_intensity
    elif negated_intensity:
        inverted = INTENSITY_INVERT.get(negated_intensity, "")
        if inverted:
            fields["intensity"] = inverted

    if new_taboos:
        # 禁忌是累积语义：旧禁忌不能因为这次没提就被清掉
        fields["taboos"] = _dedupe(list(current_profile.get("taboos") or []) + new_taboos)

    # 把相对调整的痕迹记进历史，便于演示"跨会话记忆"
    if relative:
        fields["history"] = [f"偏好调整：{message[:20]}"]
    return fields


def extract_fields_llm(message: str, current_profile: dict) -> dict:
    """LLM 抽取；失败返回空 dict，由规则结果兜底。"""
    if not llm.available():
        return {}
    user_prompt = (
        f"已有画像：{json.dumps({k: current_profile.get(k) for k in ('occasion', 'scent_family', 'intensity', 'taboos')}, ensure_ascii=False)}\n"
        f"用户这句话：{message}"
    )
    data = llm.chat_json(
        [
            {"role": "system", "content": LLM_EXTRACT_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    )
    if not isinstance(data, dict):
        return {}

    fields: dict = {}
    if data.get("occasion"):
        fields["occasion"] = str(data["occasion"]).strip()
    fam = data.get("scent_family")
    if fam in FAMILIES:
        fields["scent_family"] = fam
    inten = data.get("intensity")
    if inten in INTENSITIES:
        fields["intensity"] = inten
    if isinstance(data.get("taboos"), list) and data["taboos"]:
        fields["taboos"] = [str(x) for x in data["taboos"]]
    if data.get("history"):
        fields["history"] = [str(data["history"])]
    return fields


def merge_fields(rule_fields: dict, llm_fields: dict) -> dict:
    """LLM 结果优先，规则结果补空。"""
    merged = dict(rule_fields)
    merged.update(llm_fields)
    return merged


# ---------------------------------------------------------------------------
# 千人千香：打分
# ---------------------------------------------------------------------------

AUDIENCE_ALIASES = {
    "约会": ("约会", "夜间", "性感", "正式场合"),
    "婚礼": ("婚礼", "正式场合", "通用"),
    "伴手礼": ("伴手礼", "通用", "正式场合"),
    "成年礼": ("成年礼", "通用", "清甜"),
    "通勤": ("通勤", "日常"),
    "运动": ("运动", "夏日"),
    "晚宴": ("夜间", "正式场合"),
    "日常": ("日常", "通勤"),
}


def score_formula(item: dict, profile: dict, intent: dict) -> float:
    """给一条配方打分。分数越高越贴合这位用户此刻的需求。

    三档权重（意图 > 画像）是刻意设计的：
    本次明确说出的诉求最重，画像只作为"没说的时候的默认值"参与，
    这样同一句话在不同画像的人身上会走出不同的排序。
    """
    score = 0.0

    want_family = intent.get("scent_family") or profile.get("scent_family") or ""
    want_intensity = intent.get("intensity") or profile.get("intensity") or ""
    want_occasion = intent.get("occasion") or profile.get("occasion") or ""

    if want_family and item["family"] == want_family:
        score += 45 if intent.get("scent_family") else 35

    if want_intensity:
        if item["intensity"] == want_intensity:
            score += 30 if intent.get("intensity") else 20
        elif want_intensity in INTENSITIES and item["intensity"] in INTENSITIES:
            # 差一档给一点分，避免"淡/中"之间出现不自然的硬切
            gap = abs(INTENSITIES.index(item["intensity"]) - INTENSITIES.index(want_intensity))
            if gap == 1:
                score += 8

    if want_occasion:
        aliases = AUDIENCE_ALIASES.get(want_occasion, (want_occasion,))
        if item["audience"] in aliases:
            # 场合权重与强度同量级：场合错了，香调再对也不合用
            score += 25

    # 禁忌与避雷：命中就直接沉底
    haystack = f"{item['name']}{item['notes']}{''.join(item['tags'])}"
    for taboo in intent.get("taboos", []) + profile.get("taboos", []):
        if taboo and taboo in haystack:
            score -= 100

    for avoid in intent.get("avoid", []):
        if avoid and avoid in haystack:
            score -= 100

    # 历史反馈只按"具体香名"加权：
    # 不做宽泛的标签匹配，否则历史里出现"清新"两个字就会给所有清新香同时加分，
    # 反而把不同画像的推荐结果抹平（千人千香就演不出来了）。
    for h in profile.get("history", []):
        if item["name"] and item["name"] in h:
            negative = any(w in h for w in ("不", "太", "腻"))
            score += -12 if negative else 12

    return score


def recommend(profile: dict, intent: dict, limit: int = 3) -> list[dict]:
    """受控查询 + 打分排序，返回推荐卡片。"""
    candidates = search_formulas(limit=200)

    # 意图里的家族只用于加分，不把候选池锁死在同一家族：
    # 否则 A/B 两个画像会看到一模一样的 3 张卡，千人千香就演不出来了。
    # 只有在同家族候选足够多（>= 2 倍需求量）时才收窄，减少噪音。
    if intent.get("scent_family"):
        narrowed = [c for c in candidates if c["family"] == intent["scent_family"]]
        if len(narrowed) >= limit * 2:
            candidates = narrowed

    ranked = sorted(
        candidates,
        key=lambda c: (-score_formula(c, profile, intent), c["formula_id"]),
    )
    picked = [c for c in ranked if score_formula(c, profile, intent) > -50][:limit]
    assert_clean(picked)
    return picked


# ---------------------------------------------------------------------------
# 回复文案
# ---------------------------------------------------------------------------

REPLY_SYSTEM_PROMPT = """你是香水品牌「香屿 Scent Isle」的定制顾问。
根据用户画像和为你挑好的配方，用 2~3 句中文自然地介绍这几款香，语气温柔克制。
硬性要求：
1. 只能提到给你的配方名称、香调、氛围标签，不能编造任何原料、配比、成分数字；
2. 如果用户问配方细节，直接说明这是品牌机密，可以帮他们挑选更合适的香；
3. 不要一次性罗列所有配方，重点讲 2~3 款。"""


def build_reply(profile: dict, message: str, recs: list[dict]) -> str:
    """生成回复：有 Key 走 LLM，否则用模板。"""
    rule_reply = _template_reply(profile, message, recs)

    if not llm.available() or not recs:
        return rule_reply

    names = "、".join(r["name"] for r in recs)
    user_prompt = (
        f"用户画像：场合={profile.get('occasion') or '未知'}，"
        f"香调={profile.get('scent_family') or '未知'}，"
        f"浓度={profile.get('intensity') or '未知'}，"
        f"禁忌={profile.get('taboos') or '无'}\n"
        f"用户这次说：{message}\n"
        f"已挑好的配方（仅可用于介绍名与香调）：{names}\n"
        f"它们的信息：{json.dumps(recs, ensure_ascii=False)}"
    )
    text = llm.chat(
        [
            {"role": "system", "content": REPLY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    )
    return text or rule_reply


def _template_reply(profile: dict, message: str, recs: list[dict]) -> str:
    if not recs:
        return "我还没找到足够贴合的选择，可以再说说场合或你喜欢的香调吗？"

    intensity_text = {"淡": "清淡", "中": "中等", "浓": "浓郁"}
    bits = []
    if profile.get("scent_family"):
        bits.append(f"你偏好的{profile['scent_family']}调")
    if profile.get("occasion"):
        bits.append(f"{profile['occasion']}的场景")
    if profile.get("intensity"):
        word = intensity_text.get(profile["intensity"], profile["intensity"])
        bits.append(f"{word}的浓度")
    scene = "、".join(bits) if bits else "你这次的描述"

    names = "、".join(f"「{r['name']}」" for r in recs)

    # 按推荐顺序去重，别用 sorted()——中文排序会让家族顺序显得随机
    families: list[str] = []
    for r in recs:
        if r["family"] not in families:
            families.append(r["family"])

    tail = ""
    if profile.get("taboos"):
        tail = f"（已避开你提过的{'、'.join(profile['taboos'])}）"

    return (
        f"结合{scene}{tail}，我从配方库里为你挑了 {names}。"
        f"它们分别是{'、'.join(families)}香调，可以先闻一闻再做决定。"
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def handle_chat(user_id: str, message: str) -> dict:
    """处理一次对话，返回 reply / recommendations / profile。"""
    profile_before = memory.get_profile(user_id)

    rule_fields = extract_fields(message, profile_before)
    llm_fields = extract_fields_llm(message, profile_before)
    fields = merge_fields(rule_fields, llm_fields)

    # avoid 只参与本次打分，不落库（属于"这一次不要"的临时约束）
    intent = dict(fields)
    intent["avoid"] = analyze_negation(message)["taboos"]

    profile = memory.upsert_profile(user_id, fields) if fields else profile_before

    recs = recommend(profile, intent)
    reply = build_reply(profile, message, recs)

    return {
        "reply": reply,
        "recommendations": recs,
        "profile": profile,
        "mode": "llm" if llm.available() else "rule",
    }
