# -*- coding: utf-8 -*-
"""实盘 AI 简报/点评（P0-2）：盘前信号流程后生成自然语言简报、
盘后对账后生成信号质量点评，推送飞书（P0 脑暴方向三）。

设计约束：
- best-effort：LLM 未配置/调用失败一律返回 None，绝不阻断盘前/盘后主流程
  （实盘信号机的可靠性优先级高于 AI 增强）。
- 只解读系统产出的数据，禁止编造；prompt 明确约束。
- 系统级调用（scheduler/任务无用户上下文）→ 走环境变量池/profiles 兜底，
  不消费任何用户的私有 Key 池。
- AI 产出仅作当下决策辅助，不回填历史信号记录（无后视镜原则在 AI 侧同样适用）。
"""
import json
from typing import Optional

BRIEF_SYSTEM_PROMPT = (
    "你是量化交易实盘助手。基于系统盘前信号流程的输出数据（JSON），生成一段"
    "简洁的中文盘前简报，帮助用户开盘前 1 分钟内抓住重点。要求：\n"
    "1. 纯文本要点式（可用 - 列表），不超过 250 字；\n"
    "2. 依次覆盖：池级状态（gate/健康度）、持仓退出预警、今日开仓名单要点、"
    "数据滞后等风险提示（若有）；\n"
    "3. 只依据提供的数据，禁止编造任何数字或股票；无持仓且无信号时简短说明即可；\n"
    "4. 语气克制专业，不给确定性涨跌判断。"
)

REVIEW_SYSTEM_PROMPT = (
    "你是量化交易实盘助手。基于系统盘后对账数据（JSON：当日信号流水、虚拟持仓、"
    "权益、滑点/影子统计），生成一段中文盘后点评。要求：\n"
    "1. 纯文本要点式，不超过 300 字；\n"
    "2. 覆盖：当日信号执行情况（已成交/忽略/过期）、信号质量观察（如止损/清仓"
    "信号占比、是否与衰退预警一致）、权益与浮盈概况、滑点与执行差距（若有数据）；\n"
    "3. 只依据提供的数据，禁止编造；数据不足时如实说明；\n"
    "4. 结尾给 1 条明天值得注意的具体事项（基于数据，不给确定性判断）。"
)


def _chat_text(system: str, data: dict, db_path: Optional[str] = None) -> Optional[str]:
    """统一调用入口：任何失败（未配置 key/超时/异常）返回 None。"""
    try:
        from .provider import chat
        user_msg = ("系统输出数据（JSON）：\n"
                    + json.dumps(data, ensure_ascii=False, default=str))
        result = chat(None, [{"role": "system", "content": system},
                             {"role": "user", "content": user_msg}],
                      temperature=0.4, db_path=db_path, username=None)
        text = (result.get("content") or "").strip()
        return text or None
    except Exception:  # noqa: BLE001  AI 增强失败不阻断主流程
        return None


def premarket_briefing(result: dict, db_path: Optional[str] = None) -> Optional[str]:
    """盘前简报：输入 run_premarket 结果摘要，输出自然语言简报文本。"""
    data = {
        "as_of": result.get("as_of"),
        "数据滞后": {"stale": result.get("stale"), "days": result.get("stale_days")},
        "池级gate": {"state": result.get("gate_state"), "health": result.get("health")},
        "持仓数": result.get("positions"), "空仓天数": result.get("idle_days"),
        "触发重选": result.get("rebalanced"),
        "当前池子": (result.get("pool") or [])[:20],
        "开仓信号": [{"code": s.get("code"), "name": s.get("name"),
                     "reason": s.get("reason"), "金额": s.get("suggest_amount"),
                     "参考价": s.get("ref_price")}
                    for s in (result.get("signals") or [])],
        "退出预警": [{"code": w.get("code"), "name": w.get("name"),
                     "reason": w.get("reason")}
                    for w in (result.get("warns") or [])],
    }
    return _chat_text(BRIEF_SYSTEM_PROMPT, data, db_path)


def postclose_commentary(result: dict, signals_today: list,
                         shadow: Optional[dict] = None,
                         slippage: Optional[dict] = None,
                         db_path: Optional[str] = None) -> Optional[str]:
    """盘后点评：对账结果 + 当日信号流水 + 影子/滑点统计 → 信号质量点评。"""
    data = {
        "date": result.get("date"),
        "分钟线落库": {"saved": len(result.get("saved") or []),
                     "skipped": len(result.get("skipped") or [])},
        "虚拟权益": result.get("equity"), "可用现金": result.get("cash"),
        "回撤熔断警示": result.get("dd_warning"),
        "持仓": [{"code": p.get("code"), "name": p.get("name"),
                  "volume": p.get("volume"), "cost": p.get("cost_price"),
                  "现价": p.get("last_price"), "open_day": p.get("open_day")}
                 for p in (result.get("positions") or [])]
        if isinstance(result.get("positions"), list)
        else f"{result.get('positions')} 只",
        "当日信号": [{"kind": s.get("kind"), "stype": s.get("stype"),
                     "code": s.get("code"), "name": s.get("name"),
                     "reason": s.get("reason"), "status": s.get("status")}
                    for s in signals_today],
        "影子统计": shadow, "滑点统计": slippage,
    }
    return _chat_text(REVIEW_SYSTEM_PROMPT, data, db_path)
