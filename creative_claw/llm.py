from __future__ import annotations

import json
import os
from pathlib import Path
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from .tools import ToolRegistry


DEFAULT_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MINIMAX_MODEL = "MiniMax-M3"


def _strip_reasoning_blocks(content: str) -> tuple[str, bool]:
    """Remove provider-visible reasoning tags while preserving the final answer."""

    cleaned, count = re.subn(r"<(?:think|analysis)>[\s\S]*?</(?:think|analysis)>\s*", "", content, flags=re.IGNORECASE)
    return cleaned.strip(), bool(count)


def runtime_model_config_path(database_path: str | Path) -> Path:
    """Return the plaintext model configuration path for a database."""

    return Path(database_path).with_suffix(".llm.json")


def configure_runtime_model(
    *,
    api_key: str,
    base_url: str,
    model: str,
    persist_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate, optionally persist, and install the runtime model configuration."""

    clean_key = str(api_key or "").strip()
    clean_base_url = str(base_url or DEFAULT_MINIMAX_BASE_URL).strip().rstrip("/")
    clean_model = str(model or DEFAULT_MINIMAX_MODEL).strip()
    if not clean_key:
        raise ValueError("API Key 不能为空")
    if not clean_model:
        raise ValueError("模型名称不能为空")

    parsed = urlparse(clean_base_url)
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not local_http:
        raise ValueError("模型地址必须使用 HTTPS；仅本机地址允许 HTTP")
    if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("模型地址格式无效")

    if persist_path is not None:
        destination = Path(persist_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "api_key": clean_key,
                    "base_url": clean_base_url,
                    "model": clean_model,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)

    os.environ["CREATIVE_CLAW_LLM_API_KEY"] = clean_key
    os.environ["CREATIVE_CLAW_LLM_BASE_URL"] = clean_base_url
    os.environ["CREATIVE_CLAW_LLM_MODEL"] = clean_model
    return public_model_config()


def load_persisted_runtime_model(path: str | Path) -> dict[str, Any]:
    """Load plaintext configuration unless the process already supplies a key."""

    if os.getenv("CREATIVE_CLAW_LLM_API_KEY"):
        return public_model_config()
    source = Path(path)
    if not source.is_file():
        return public_model_config()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("模型配置文件必须是 JSON 对象")
    return configure_runtime_model(
        api_key=str(payload.get("api_key") or ""),
        base_url=str(payload.get("base_url") or ""),
        model=str(payload.get("model") or ""),
    )


def _chat_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _llm_env() -> tuple[str, str, str]:
    api_key = os.getenv("CREATIVE_CLAW_LLM_API_KEY")
    if not api_key:
        raise ValueError("LLM planner is not configured; missing: CREATIVE_CLAW_LLM_API_KEY")
    return (
        os.getenv("CREATIVE_CLAW_LLM_BASE_URL", DEFAULT_MINIMAX_BASE_URL),
        api_key,
        os.getenv("CREATIVE_CLAW_LLM_MODEL", DEFAULT_MINIMAX_MODEL),
    )


@dataclass(slots=True)
class OpenAICompatiblePlanner:
    base_url: str
    api_key: str
    model: str
    timeout: float = 90.0

    @classmethod
    def from_env(cls) -> "OpenAICompatiblePlanner":
        base_url, api_key, model = _llm_env()
        return cls(base_url, api_key, model)

    def _url(self) -> str:
        return _chat_url(self.base_url)

    def plan(self, goal: str, registry: ToolRegistry) -> list[dict[str, Any]]:
        tools = registry.list()
        response = requests.post(
            self._url(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "temperature": 0.1,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a planning component for a creative-writing agent. "
                            "Return only a JSON array. Each item must be "
                            '{"tool":"registered_tool_name","args":{...}}. '
                            "Use read tools before write tools. Never invent a tool or omit required arguments. "
                            "Write and external tools will be approval-gated by the runtime.\n\n"
                            "Registered tools:\n" + json.dumps(tools, ensure_ascii=False)
                        ),
                    },
                    {"role": "user", "content": goal},
                ],
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            content = fenced.group(1)
        plan = json.loads(content)
        if not isinstance(plan, list):
            raise ValueError("Planner must return a JSON array")
        for index, step in enumerate(plan):
            if not isinstance(step, dict) or not isinstance(step.get("tool"), str) or not isinstance(step.get("args", {}), dict):
                raise ValueError(f"Invalid planner step {index}")
            registry.get(step["tool"])
        return plan


@dataclass(slots=True)
class OpenAICompatibleColdStartWriter:
    base_url: str
    api_key: str
    model: str
    timeout: float = 120.0

    @classmethod
    def from_env(cls) -> "OpenAICompatibleColdStartWriter":
        return cls(*_llm_env())

    def generate(
        self,
        prompt: str,
        *,
        repair: dict[str, str] | None = None,
    ) -> str:
        contract = (
            "只返回一个 JSON 对象，不要 Markdown、解释或思考过程。字段必须为："
            "title, premise, protagonist_key, kline_dimension, entities, relations, scenes。"
            "entities 必须有 3 至 5 项，每项包含 key/name/entity_type/description；"
            "entity_type 只能是 character/location/object/organization/canon_fact。"
            "relations 每项包含 source_key/predicate/target_key。"
            "scenes 必须有 6 至 8 项，每项包含 title/summary/story_time/entity_keys/ohlc，"
            "ohlc 必须包含 0 到 100 的 open/high/low/close。主人公必须引用 character 实体。"
            "只借鉴参照对象的抽象机制，必须使用原创名称、原创情节和原创表达；"
            "不得复刻专有角色名、完整情节、标志性台词或连续表达。"
        )
        if repair is None:
            user_content = str(prompt).strip()
            temperature = 0.65
        else:
            user_content = (
                "修复下面的无效响应，使其严格满足 JSON 契约。不得改变用户的创作意图。\n"
                f"校验错误：{repair.get('error', '')}\n"
                f"无效响应：{repair.get('response', '')}\n"
                f"原始创作意图：{str(prompt).strip()}"
            )
            temperature = 0.0
        response = requests.post(
            _chat_url(self.base_url),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": "你是 Creative Claw 冷启动框架生成器。" + contract},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"].get("content", "")
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        content, _ = _strip_reasoning_blocks(str(content))
        return content


@dataclass(slots=True)
class OpenAICompatibleWriter:
    base_url: str
    api_key: str
    model: str
    timeout: float = 120.0

    @classmethod
    def from_env(cls) -> "OpenAICompatibleWriter":
        return cls(*_llm_env())

    def answer(
        self,
        message: str,
        narrative_context: dict[str, Any],
        *,
        mode: str = "analysis",
    ) -> dict[str, Any]:
        mode_instructions = {
            "analysis": "分析叙事问题，指出证据、矛盾和可执行建议。",
            "continue": "在不违反正典、时间线和人物知情边界的前提下续写。",
            "consistency": "执行严格一致性检查，区分确定冲突、潜在风险和缺失证据。",
            "rewrite": "按用户要求改写，同时保留事实、人物动机和时间连续性。",
        }
        context_payload = {
            "resolved_scope": narrative_context.get("resolved_scope", {}),
            "evidence_refs": narrative_context.get("evidence_refs", []),
            "context_text": narrative_context.get("context_text", narrative_context.get("context", ""))[:28_000],
        }
        creative_mode = mode in {"continue", "rewrite"}
        response = requests.post(
            _chat_url(self.base_url),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "temperature": 0.62 if creative_mode else 0.2,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是 Creative Claw 的叙事协作模型。只依据提供的项目证据工作。"
                            "证据编号按类型区分：来源 [S#]、图谱 [G#]、时间线 [T#]、人物 K 线 [K#]、"
                            "版本 [V#]、规则 [R#]、问题 [I#]。引用必须紧跟在被支持的事实之后，"
                            "只能使用上下文中真实存在的编号。证据不足必须明确说明；"
                            "创作性补充必须标明为非正典，且不能附加引用。"
                            "人物 OHLC 是结构化状态：open 和 close 表示周期起点与终点，high 和 low 只是周期内极值，"
                            "没有先后顺序；除非时间线或证据明确给出事件顺序，否则绝不能把 high/low 推演成先涨后跌。"
                            "只输出交付给用户的最终内容，不得输出思考过程、检查清单、要求复述或 <think> 标签。"
                            + mode_instructions.get(mode, mode_instructions["analysis"])
                        ),
                    },
                    {
                        "role": "user",
                        "content": message
                        + "\n\n以下是检索到的项目上下文：\n"
                        + json.dumps(context_payload, ensure_ascii=False, default=str),
                    },
                ],
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"].get("content", "")
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        content, reasoning_filtered = _strip_reasoning_blocks(str(content))
        return {
            "answer": content,
            "model": payload.get("model", self.model),
            "usage": payload.get("usage", {}),
            "reasoning_filtered": reasoning_filtered,
        }


def public_model_config() -> dict[str, Any]:
    """Expose only non-secret connection status to the browser canvas."""

    base_url = os.getenv("CREATIVE_CLAW_LLM_BASE_URL", DEFAULT_MINIMAX_BASE_URL)
    return {
        "configured": bool(os.getenv("CREATIVE_CLAW_LLM_API_KEY")),
        "base_url": base_url,
        "model": os.getenv("CREATIVE_CLAW_LLM_MODEL", DEFAULT_MINIMAX_MODEL),
        "api_key_exposed": False,
    }
