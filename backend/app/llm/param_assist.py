# -*- coding: utf-8 -*-
"""AI 参数助手：回测表单内的「一句话调参」（方案 A，单轮，不落库）。

链路：表单当前草稿 config + 用户一句话
      -> LLM 产出配置补丁 {"top": {}, "params": {}, "risk_config": {}}
      -> 代码层护栏净化（白名单键 + 类型/区间/枚举；策略参数复用 analyzer 的口径，
         顶层字段按 backtest_schema.TOP_FIELDS 净化，风控按 RiskConfigModel 全字段）
      -> 合并进草稿 -> validate_backtest_config 探测「本次补丁新引入的」业务冲突
      -> 返回 diff 供前端预览；用户点「应用」后才写入表单。

边界（与 AGENTS.md 的「改动需用户明示」一致）：
  · 只产出建议与预览，不自动改表单、不自动提交回测——参数变更由用户确认；
  · 策略 / 周期 / 股票池 / 回测区间不参与自动调整（换策略会作废参数，换区间可能
    触发数据补拉），LLM 若提到则落到 unsupported 提示用户手动处理；
  · 幻觉护栏在代码层：不靠 LLM 自觉，表外的键一律丢弃。
"""
from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Optional

from fastapi import HTTPException

from ..api.backtest_schema import UI_FIELDS, dynamic_choices
from ..engine.strategies import REGISTRY

logger = logging.getLogger(__name__)

# 顶层字段里不参与自动调整的（结构性 / 数据区间 / 子结构），值 = 给用户的说明
_TOP_BLOCKED: dict[str, str] = {
    "name": "任务名称请用「AI生成」或手动填写",
    "strategy_id": "换策略会作废现有参数，请手动切换",
    "period": "换周期涉及数据可得性，请手动切换",
    "universe": "股票池是代码列表，请用选股器或动态选股设置",
    "start_date": "回测区间涉及数据可得性，请手动调整日期",
    "end_date": "回测区间涉及数据可得性，请手动调整日期",
    "end_date_today": "由「结束日跟随今天」开关控制",
    "universe_meta": "条件选股溯源字段，不参与调参",
    "params": "策略参数请写在 params 块",
    "risk_config": "风控参数请写在 risk_config 块",
}

_MISS = object()  # 净化失败的哨兵值


def adjustable_top_fields() -> list[dict[str, Any]]:
    """AI 可自动调整的顶层字段（表单可见且未被锁定）。"""
    return [f for f in UI_FIELDS if f["key"] not in _TOP_BLOCKED]


# ---------------------------------------------------------------- 取值净化


def _same(a: Any, b: Any) -> bool:
    """同值判断（bool 与 int 不混淆：True 不等于 1）"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def _as_bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "on", "yes", "是", "开", "启用"):
            return True
        if s in ("false", "0", "off", "no", "否", "关", "关闭"):
            return False
    return None


def _as_num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _allowed_values(field: dict[str, Any]) -> list[str]:
    """字段合法取值（静态 choices / choices_from 动态选项）；无约束返回 []。"""
    if field.get("choices_from"):
        return [str(c["value"]) for c in dynamic_choices().get(field["choices_from"], [])]
    out = []
    for c in field.get("choices") or []:
        out.append(str(c["value"]) if isinstance(c, dict) else str(c))
    return out


def _coerce_top(field: dict[str, Any], v: Any) -> Any:
    """按 TOP_FIELDS 的类型/范围/选项净化单个顶层值；非法返回 _MISS。"""
    t = field["type"]
    if t == "bool":
        b = _as_bool(v)
        return b if b is not None else _MISS
    if t == "bool_or_none":
        if v is None:
            return None
        b = _as_bool(v)
        return b if b is not None else _MISS
    if t in ("int", "float", "int_or_none", "float_or_none"):
        if v is None:
            return None if t.endswith("_or_none") else _MISS
        n = _as_num(v)
        if n is None:
            return _MISS
        if t.startswith("int"):
            n = int(round(n))
        if field.get("min") is not None:
            n = max(field["min"], n)
        if field.get("max") is not None:
            n = min(field["max"], n)
        return int(n) if t.startswith("int") else float(n)
    if t == "str":
        s = str(v).strip()
        allowed = _allowed_values(field)
        return s if (not allowed or s in allowed) else _MISS
    if t == "str_list":
        vals = v if isinstance(v, list) else [x.strip() for x in str(v).split(",")]
        allowed = _allowed_values(field)
        keep = [str(x) for x in vals if str(x).strip() and (not allowed or str(x) in allowed)]
        return keep if keep else _MISS
    return _MISS


def sanitize_top(raw: dict, cfg: dict) -> tuple[dict, list[str]]:
    """顶层字段补丁净化：只认可调整的表单字段；非法/锁定项丢弃并记原因。"""
    catalog = {f["key"]: f for f in adjustable_top_fields()}
    clean: dict[str, Any] = {}
    notes: list[str] = []
    for k, v in (raw or {}).items():
        key = str(k)
        if key in _TOP_BLOCKED:
            notes.append(f"{key}：{_TOP_BLOCKED[key]}")
            continue
        field = catalog.get(key)
        if field is None:
            notes.append(f"{key}：不是可调整的回测顶层字段，已忽略")
            continue
        val = _coerce_top(field, v)
        if val is _MISS:
            notes.append(f"{field.get('label') or key}（{key}）：取值 {v!r} 不合法，已忽略")
            continue
        if _same((cfg or {}).get(key), val):
            continue
        clean[key] = val
    return clean, notes


def sanitize_params(raw: dict, cfg: dict) -> dict:
    """策略参数净化：复用 AiAnalysis 的护栏（param_schema 的 min/max/choices），
    再剔除 frozen 参数（表单里锁定不可改，如 MACD 周期）。"""
    from .analyzer import _sanitize_suggestions

    clean = _sanitize_suggestions({"params": raw or {}, "risk_config": {}},
                                  {"config": cfg or {}})
    strategy = REGISTRY.get((cfg or {}).get("strategy_id"))
    if strategy is None:
        return {}
    frozen = {p["key"] for p in strategy.param_schema if p.get("frozen")}
    return {k: v for k, v in ((clean or {}).get("params") or {}).items()
            if k not in frozen}


def _risk_specs() -> dict[str, dict[str, Any]]:
    """风控字段规格：键取自 RiskConfigModel 全字段（比 analyzer 白名单多
    max_sector_pct / trade_* / regime_b_on，这些在表单里可调），
    边界/枚举复用 analyzer 的口径。"""
    from ..api.backtests import RiskConfigModel
    from .analyzer import _RISK_BOUNDS, _RISK_ENUMS, _RISK_INT_FIELDS

    specs: dict[str, dict[str, Any]] = {}
    for k, f in RiskConfigModel.model_fields.items():
        ann = str(f.annotation)
        if "bool" in ann:
            t = "bool"
        elif "int" in ann:
            t = "int"
        elif "float" in ann:
            t = "float"
        else:
            t = "str"
        specs[k] = {"type": t, "bounds": _RISK_BOUNDS.get(k),
                    "enum": _RISK_ENUMS.get(k), "is_int": k in _RISK_INT_FIELDS}
    return specs


def sanitize_risk(raw: dict, cfg: dict) -> dict:
    """风控参数净化：白名单键 + 枚举校验 + 数值越界丢弃（口径错误不 clamp）。"""
    specs = _risk_specs()
    cur = (cfg or {}).get("risk_config") or {}
    clean: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        key = str(k)
        spec = specs.get(key)
        if spec is None or v is None:
            continue
        if spec["enum"]:
            s = str(v).strip()
            if s not in spec["enum"]:
                continue
            val: Any = s
        elif spec["type"] == "bool":
            b = _as_bool(v)
            if b is None:
                continue
            val = b
        elif spec["type"] in ("int", "float"):
            n = _as_num(v)
            if n is None:
                continue
            if spec["bounds"]:
                lo, hi = spec["bounds"]
                if not lo <= n <= hi:
                    continue
            val = int(round(n)) if spec["is_int"] else float(n)
        else:
            continue
        if _same(cur.get(key), val):
            continue
        clean[key] = val
    return clean


def sanitize_patch(data: dict, cfg: dict) -> tuple[dict, list[str]]:
    """净化整个补丁，返回 (patch, 被丢弃的原因列表)。"""
    top, notes = sanitize_top(data.get("top") or {}, cfg)
    patch = {
        "top": top,
        "params": sanitize_params(data.get("params") or {}, cfg),
        "risk_config": sanitize_risk(data.get("risk_config") or {}, cfg),
    }
    return patch, notes


# ---------------------------------------------------------------- 喂给 LLM 的字段目录


def _top_catalog(cfg: dict) -> list[dict]:
    rows = []
    for f in adjustable_top_fields():
        row: dict[str, Any] = {"key": f["key"], "label": f.get("label"), "type": f["type"],
                               "current": cfg.get(f["key"], f.get("default"))}
        for k in ("min", "max"):
            if f.get(k) is not None:
                row[k] = f[k]
        opts = _allowed_values(f)
        if opts:
            row["options"] = opts
        if f.get("help"):
            row["help"] = f["help"]
        rows.append(row)
    return rows


def _param_catalog(cfg: dict) -> list[dict]:
    strategy = REGISTRY.get((cfg or {}).get("strategy_id"))
    if strategy is None:
        return []
    cur = cfg.get("params") or {}
    rows = []
    for p in strategy.param_schema:
        if p.get("frozen"):
            continue  # 表单里锁定不可改（如 MACD 周期），不给 LLM 开口子
        row: dict[str, Any] = {"key": p["key"], "label": p.get("label"), "type": p.get("type"),
                               "current": cur.get(p["key"], p.get("default"))}
        for k in ("min", "max"):
            if p.get(k) is not None:
                row[k] = p[k]
        if p.get("choices"):
            row["options"] = list(p["choices"])
        if p.get("description"):
            row["help"] = str(p["description"])[:60]
        rows.append(row)
    return rows


def _risk_catalog(cfg: dict) -> list[dict]:
    specs = _risk_specs()
    cur = cfg.get("risk_config") or {}
    rows = []
    for k, spec in specs.items():
        row: dict[str, Any] = {"key": k, "type": spec["type"], "current": cur.get(k)}
        if spec["bounds"]:
            row["min"], row["max"] = spec["bounds"]
        if spec["enum"]:
            row["options"] = sorted(spec["enum"])
        rows.append(row)
    return rows


_SYSTEM_PROMPT = (
    "你是 A 股量化回测配置助手。用户会用一句自然语言描述他想调整的回测参数，"
    "你把它翻译成对当前配置的精确补丁。\n"
    "硬性要求：\n"
    "1. 只输出需要改动的字段；值必须落在给定区间/选项内，类型与原值一致"
    "（布尔仍给布尔、整数仍给整数、枚举给 options 里的值）；\n"
    "2. 顶层字段只用「顶层字段表」的 key，策略参数只用「策略参数表」的 key，"
    "风控参数只用「风控参数表」的 key；\n"
    "3. 业务约束：动态选股 universe_auto、池级趋势开关 pool_gate、大盘趋势闸门 index_gate "
    "都只对 momentum_t / momentum_slot 有效；momentum_slot 的 pool_n（候选池大小）"
    "必须大于 max_holdings（最大持仓只数）；\n"
    "4. 表里没有的字段、或属于策略/周期/股票池/回测区间这类不能自动调整的，"
    "不要臆造，放进 unsupported 并一句话说明；\n"
    "5. 宁缺毋滥：用户这句话没提到的字段一律不要改。\n\n"
    "只输出一个 json 对象（不要 markdown 代码块、不要任何解释），格式：\n"
    '{"top": {"字段key": 新值}, "params": {"参数key": 新值}, '
    '"risk_config": {"字段key": 新值}, "notes": "一句话说明改了什么", '
    '"unsupported": ["无法自动调整的部分及原因"]}\n'
    "没有改动的块给空对象 {}。"
)


def build_messages(cfg: dict, message: str) -> list[dict]:
    payload = {
        "策略": f"{getattr(REGISTRY.get(cfg.get('strategy_id')), 'name', None) or cfg.get('strategy_id')}"
                f"（{cfg.get('strategy_id')}）",
        "顶层字段表": _top_catalog(cfg),
        "策略参数表": _param_catalog(cfg),
        "风控参数表": _risk_catalog(cfg),
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "当前配置的可调字段：\n"
                                    + json.dumps(payload, ensure_ascii=False)
                                    + f"\n\n用户需求：{message}"},
    ]


# ---------------------------------------------------------------- 主流程


def _extract_json(content: str) -> Optional[dict]:
    """从 LLM 回复里取 JSON 对象：优先 ```json 代码块，退化为首尾花括号区间。"""
    text = content or ""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    cand = m.group(1).strip() if m else None
    if not cand:
        start, end = text.find("{"), text.rfind("}")
        cand = text[start:end + 1] if 0 <= start < end else None
    if not cand:
        return None
    try:
        data = json.loads(cand)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _as_str_list(v: Any, limit: int) -> list[str]:
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()][:limit]
    return []


def _merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    out.update(patch["top"])
    out["params"] = {**(base.get("params") or {}), **patch["params"]}
    out["risk_config"] = {**(base.get("risk_config") or {}), **patch["risk_config"]}
    return out


def _diff(base: dict, patch: dict) -> list[dict]:
    """补丁 -> 预览行（scope/key/旧值/新值）；净化后只含真实变更。"""
    out = []
    for k, v in patch["top"].items():
        out.append({"scope": "top", "key": k, "old": base.get(k), "new": v})
    for scope in ("params", "risk_config"):
        cur = base.get(scope) or {}
        for k, v in patch[scope].items():
            out.append({"scope": scope, "key": k, "old": cur.get(k), "new": v})
    return out


def _validate_error(cfg: dict) -> Optional[str]:
    """配置校验错误（None=通过）。校验器会改动入参，故传副本。"""
    from ..api.backtests import validate_backtest_config

    try:
        validate_backtest_config(copy.deepcopy(cfg))
        return None
    except HTTPException as e:
        return str(e.detail)
    except Exception:  # noqa: BLE001
        logger.warning("参数助手配置校验异常", exc_info=True)
        return "配置校验失败"


def assist(cfg: dict, message: str, profile: Optional[str] = None,
           username: Optional[str] = None) -> dict:
    """一句话 -> 配置补丁 + diff 预览。只读，不落库、不改表单。"""
    base = copy.deepcopy(cfg or {})
    if not base.get("strategy_id"):
        raise ValueError("配置缺少 strategy_id")
    from . import provider

    res = provider.chat(profile, build_messages(base, message),
                        temperature=0.2, db_path=None, username=username)
    raw = _extract_json(res.get("content") or "")
    if raw is None:
        return {"ok": False, "changed": False, "patch": {}, "merged_config": base,
                "diff": [], "notes": "", "unsupported": [], "issues": [],
                "model": res.get("model"),
                "error": "AI 未返回可解析的配置补丁，请换个说法重试"}

    patch, dropped = sanitize_patch(raw, base)
    merged = _merge(base, patch)
    # 只报「本次补丁新引入的」冲突：草稿本身就没选好股票池等未完成状态不算 AI 的锅
    base_err = _validate_error(base)
    merged_err = _validate_error(merged)
    issues = [merged_err] if merged_err and merged_err != base_err else []

    diff = _diff(base, patch)
    return {
        "ok": True,
        "changed": bool(diff),
        "patch": patch,
        "merged_config": merged,
        "diff": diff,
        "notes": str(raw.get("notes") or "").strip()[:300],
        "unsupported": _as_str_list(raw.get("unsupported"), 5) + dropped,
        "issues": issues,
        "model": res.get("model"),
    }
