"""FastAPI 入口：9 个对外接口 + 统一响应结构 {code, msg, data}。

约定：
- code = 0 表示成功，非 0 表示业务错误；
- 所有响应体在返回前都过一遍 guard.sanitize_payload，杜绝机密字段外泄；
- 配置了 DEEPSEEK_API_KEY 就走大模型，否则自动降级为规则匹配。
"""

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import agent, guard, llm, memory
from .formula_tool import count_formulas, get_formula, list_catalog, search_formulas

# 香调家族（前端"探索香调"卡片用）
FAMILY_META = [
    {"name": "柑橘", "desc": "明亮多汁，像剥开一颗新鲜柑橘", "keywords": ["清爽", "活力", "夏日"]},
    {"name": "花香", "desc": "柔软花瓣调，温柔而有存在感", "keywords": ["温柔", "优雅", "浪漫"]},
    {"name": "木质", "desc": "沉稳干燥的木材与苔藓气息", "keywords": ["沉稳", "温暖", "神秘"]},
    {"name": "东方", "desc": "树脂、香草与焚香的浓郁底色", "keywords": ["浓郁", "性感", "异域"]},
    {"name": "清新", "desc": "水汽、绿叶与茶香的干净通透", "keywords": ["干净", "通透", "舒缓"]},
]

# 专属场景定制（对齐前端首页三张卡）
SCENES = [
    {"scene": "18岁成年礼", "desc": "第一支属于自己的香，干净、有朝气", "suggest_family": "清新"},
    {"scene": "婚礼伴手礼", "desc": "适合分享给宾客的温柔气息", "suggest_family": "花香"},
    {"scene": "结婚礼", "desc": "浓一些、有仪式感的主香", "suggest_family": "东方"},
    {"scene": "约会", "desc": "近距离的温柔，留香持久", "suggest_family": "花香"},
    {"scene": "通勤", "desc": "低调不打扰同事的干净气息", "suggest_family": "柑橘"},
    {"scene": "运动后", "desc": "冰凉清爽，迅速恢复状态", "suggest_family": "清新"},
]

# 演示用初始画像：两个人设差异足够大，才能演出"同一句话、不同结果"
DEMO_PROFILES = {
    "user_a": {
        "occasion": "日常",
        "scent_family": "清新",
        "intensity": "淡",
        "taboos": [],
        "history": ["喜欢雨后森林的干净感"],
    },
    "user_b": {
        "occasion": "约会",
        "scent_family": "东方",
        "intensity": "浓",
        "taboos": ["柑橘"],
        "history": ["上次觉得太甜，希望不那么腻"],
    },
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时建表 + 首次灌种子配方（文件库，重启后画像仍在）
    memory.init_db()
    if count_formulas() == 0:
        memory.seed_formulas()
    yield


app = FastAPI(
    title="香屿 Scent Isle · 香水定制 Agent 后端 Demo",
    description="演示：用户画像记忆 / 千人千香推荐 / 配方保密审计",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def ok(data=None, msg: str = "ok") -> dict:
    """统一成功响应，并做出口清洗。"""
    return guard.sanitize_payload({"code": 0, "msg": msg, "data": data})


def fail(msg: str, code: int = 400) -> dict:
    return guard.sanitize_payload({"code": code, "msg": msg, "data": None})


class ChatRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    message: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# 1. 对话（核心接口）
# ---------------------------------------------------------------------------


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    user_id = req.user_id.strip()
    message = req.message.strip()

    # 注入防护：命中即拒绝，并留下审计记录
    blocked, reason = guard.check_injection(message)
    if blocked:
        guard.write_audit(user_id, guard.EVENT_BLOCKED, f"拦截原因：{reason}｜原文：{message[:120]}")
        return ok(
            {
                "reply": "配方属于品牌商业机密，我不能提供原料或配比信息。不过可以告诉我你想要的场合和感觉，我帮你挑一款更合适的香。",
                "recommendations": [],
                "profile": memory.get_profile(user_id),
                "blocked": True,
                "reason": reason,
            },
            msg="请求已被安全策略拒绝",
        )

    result = agent.handle_chat(user_id, message)

    guard.write_audit(user_id, guard.EVENT_QUERY, message[:200])
    if result["recommendations"]:
        guard.write_audit(
            user_id,
            guard.EVENT_RECOMMEND,
            {"message": message[:100], "picked": [r["formula_id"] for r in result["recommendations"]]},
        )

    result["blocked"] = False
    return ok(result)


# ---------------------------------------------------------------------------
# 2. 香调家族
# ---------------------------------------------------------------------------


@app.get("/api/families")
def families(family: str | None = Query(default=None, description="按家族名筛选")) -> dict:
    items = []
    for meta in FAMILY_META:
        if family and meta["name"] != family:
            continue
        items.append({**meta, "count": len(search_formulas(family=meta["name"], limit=200))})
    return ok(items)


# ---------------------------------------------------------------------------
# 3. 场景
# ---------------------------------------------------------------------------


@app.get("/api/scenes")
def scenes() -> dict:
    return ok(SCENES)


# ---------------------------------------------------------------------------
# 4. AI 精选推荐
# ---------------------------------------------------------------------------


@app.get("/api/recommend")
def recommend(user_id: str = Query(..., min_length=1), limit: int = Query(default=3, ge=1, le=10)) -> dict:
    profile = memory.get_profile(user_id)
    if not any(profile.get(k) for k in ("occasion", "scent_family", "intensity")):
        # 画像为空时给一组通用精选，避免前端空白
        items = search_formulas(limit=limit)
    else:
        items = agent.recommend(profile, {"avoid": []}, limit=limit)
    return ok({"user_id": user_id, "profile": profile, "recommendations": items})


# ---------------------------------------------------------------------------
# 5. 卡片详情
# ---------------------------------------------------------------------------


@app.get("/api/products/{formula_id}")
def product_detail(formula_id: str) -> dict:
    item = get_formula(formula_id)
    if item is None:
        return fail(f"配方不存在：{formula_id}", code=404)
    return ok(item)


# ---------------------------------------------------------------------------
# 6. 香氛库
# ---------------------------------------------------------------------------


@app.get("/api/catalog")
def catalog(
    family: str | None = Query(default=None),
    intensity: str | None = Query(default=None),
    tag: str | None = Query(default=None),
) -> dict:
    items = list_catalog(family=family)
    if intensity:
        items = [i for i in items if i["intensity"] == intensity]
    if tag:
        items = [i for i in items if tag in i["tags"]]
    return ok({"total": len(items), "items": items})


# ---------------------------------------------------------------------------
# 7. 用户画像
# ---------------------------------------------------------------------------


@app.get("/api/profile/{user_id}")
def profile(user_id: str) -> dict:
    return ok(memory.get_profile(user_id))


# ---------------------------------------------------------------------------
# 8. 审计日志
# ---------------------------------------------------------------------------


@app.get("/api/audit")
def audit(
    limit: int = Query(default=100, ge=1, le=500),
    event: str | None = Query(default=None, description="query / recommend / blocked_injection"),
) -> dict:
    items = guard.list_audit(limit=limit, event=event)
    return ok({"total": len(items), "items": items})


# ---------------------------------------------------------------------------
# 9. 演示数据重置
# ---------------------------------------------------------------------------


@app.post("/api/demo/reset")
def demo_reset() -> dict:
    memory.reset_all()
    for uid, fields in DEMO_PROFILES.items():
        memory.upsert_profile(uid, fields)
    guard.write_audit("system", "reset", "演示数据已重置，并预置 user_a / user_b 两个演示画像")
    return ok(
        {
            "formulas": count_formulas(),
            "profiles": list(DEMO_PROFILES.keys()),
            "audit_cleared": True,
        },
        msg="演示数据已重置",
    )


@app.get("/")
def root() -> dict:
    return ok(
        {
            "service": "perfume-agent",
            "docs": "/docs",
            "llm": llm.status(),
            "formulas": count_formulas(),
        }
    )


# ---------------------------------------------------------------------------
# 评测钩子（不属于业务接口，默认关闭）
# ---------------------------------------------------------------------------
# 为什么需要：评测集里的用例大多依赖"测前已有画像"（例如"比上次淡一点点"
# 必须读到历史强度），而正常对话无法凭空构造出一个精确的初始画像。
# 这两个接口只做两件事：写入/清除某个用户的 profile，不碰保密逻辑、不返回配方。
#
# 三道保险：
#   1. 必须 EVAL_HOOKS=1 启动服务才生效，默认 404；
#   2. 只允许操作 eval_ 前缀的 user_id，防止误伤真实用户；
#   3. include_in_schema=False，不出现在 /docs 里。

EVAL_HOOKS_ON = os.getenv("EVAL_HOOKS", "").strip().lower() in ("1", "true", "yes")


class TestProfileRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    profile: dict = Field(default_factory=dict)


def _require_eval_hooks(user_id: str) -> None:
    if not EVAL_HOOKS_ON:
        raise HTTPException(
            status_code=404,
            detail="评测钩子未启用：请用 EVAL_HOOKS=1 启动服务",
        )
    if not user_id.startswith("eval_"):
        raise HTTPException(
            status_code=403,
            detail="评测钩子只允许操作 eval_ 前缀的用户",
        )


@app.post("/_test/profile", include_in_schema=False)
def test_set_profile(req: TestProfileRequest) -> dict:
    """写入（合并式覆盖）某个评测用户的画像，用于构造测试前置条件。"""
    _require_eval_hooks(req.user_id)
    profile = memory.upsert_profile(req.user_id, req.profile)
    return ok({"user_id": req.user_id, "profile": profile})


@app.delete("/_test/profile/{user_id}", include_in_schema=False)
def test_clear_profile(user_id: str) -> dict:
    """清除某个评测用户的画像，保证用例之间不串味。"""
    _require_eval_hooks(user_id)
    with memory.get_conn() as conn:
        conn.execute("DELETE FROM profile WHERE user_id = ?", (user_id,))
    return ok({"cleared": user_id})


@app.get("/_test/health", include_in_schema=False)
def test_health() -> dict:
    """供评测脚本探测钩子是否可用。"""
    if not EVAL_HOOKS_ON:
        raise HTTPException(status_code=404, detail="评测钩子未启用")
    return ok({"eval_hooks": True, "service": "perfume-agent"})
