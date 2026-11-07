"""DeepSeek 客户端（OpenAI 兼容协议）。

没有 API Key、或调用失败（断网/额度不足）时统一返回 None，
由 agent.py 自动降级为规则匹配，保证 Demo 离线也能跑完整流程。
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()

API_KEY = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
BASE_URL = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip()
MODEL = (os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip()

_client = None
_init_error = ""


def available() -> bool:
    """是否配置了 Key（配置了也可能调用失败，失败时同样降级）。"""
    return bool(API_KEY)


def _get_client():
    global _client, _init_error
    if _client is not None:
        return _client
    try:
        from openai import OpenAI

        _client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=30, max_retries=0)
    except Exception as exc:  # pragma: no cover - 环境问题，不影响降级
        _init_error = str(exc)
        return None
    return _client


def chat(messages: list[dict], temperature: float = 0.7, json_mode: bool = False) -> str | None:
    """调用 DeepSeek，失败一律返回 None（调用方据此降级）。"""
    if not available():
        return None
    client = _get_client()
    if client is None:
        return None
    kwargs = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return None


def chat_json(messages: list[dict], temperature: float = 0.2) -> dict | None:
    """要求模型输出 JSON，解析失败返回 None。"""
    raw = chat(messages, temperature=temperature, json_mode=True)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 模型偶尔会用 ```json 包裹，兜底剥一层
        cleaned = raw.strip().strip("`").removeprefix("json").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def status() -> dict:
    return {
        "enabled": available(),
        "base_url": BASE_URL,
        "model": MODEL,
        "init_error": _init_error,
    }
