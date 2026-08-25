# -*- coding: utf-8 -*-
"""端到端联调检查：登录→演示数据→回测→报告/K线→寻优→AI→数据状态"""
import sys, time, json
sys.stdout.reconfigure(encoding="utf-8")
import httpx

BASE = "http://127.0.0.1:8000/api"
client = httpx.Client(base_url=BASE, timeout=60)
FAIL = []

def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  -- {detail}" if (detail and not cond) else ""))
    if not cond:
        FAIL.append(name)

def wait_task(task_id, max_wait=180):
    """轮询任务直到终态，返回最终 status json"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        r = client.get(f"/backtests/{task_id}/status")
        if r.status_code == 200:
            st = r.json()
            if st["status"] in ("success", "failed", "cancelled"):
                return st
        time.sleep(1)
    return {"status": "timeout", "error": "等待超时", "progress": 0}

# 1. 登录
r = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
check("登录", r.status_code == 200 and "token" in r.json(), r.text)
client.headers["Authorization"] = f"Bearer {r.json()['token']}"
check("错误密码401", client.post("/auth/login", json={"username": "admin", "password": "x"}).status_code == 401)

# 2. 基础数据接口
r = client.get("/strategies")
check("策略列表", r.status_code == 200 and len(r.json()) >= 2, r.text)
strategies = {s["id"]: s for s in r.json()}
check("策略含param_schema", all("param_schema" in s and len(s["param_schema"]) > 0 for s in strategies.values()))

# 3. 演示数据
r = client.post("/data/demo", json={})
check("生成演示数据提交", r.status_code == 200, r.text)
st = wait_task(r.json()["task_id"])
check("演示数据成功", st["status"] == "success", str(st))

r = client.get("/stocks?keyword=600")
check("股票搜索", r.status_code == 200 and len(r.json()) >= 1, r.text)

# 4. 日线回测（日期取演示数据实际范围）
ds = client.get("/data/status").json()
d_start, d_end = ds["daily"]["start"], ds["daily"]["end"]
print(f"  数据范围: {d_start} ~ {d_end}")
bt_cfg = {
    "name": "联调-双均线日线",
    "strategy_id": "ma_cross",
    "params": {"fast": 5, "slow": 20},
    "universe": ["600000", "000001"],
    "start_date": d_start, "end_date": d_end,
    "period": "daily",
    "initial_capital": 1000000,
}
r = client.post("/backtests", json=bt_cfg)
check("创建日线回测", r.status_code == 200, r.text)
bt_id = r.json()["task_id"]
st = wait_task(bt_id)
check("日线回测成功", st["status"] == "success", str(st))

r = client.get(f"/backtests/{bt_id}/report")
check("报告可取", r.status_code == 200, r.text)
report = r.json()
need_metrics = ["total_return", "annual_return", "max_drawdown", "sharpe", "sortino", "calmar",
                "win_rate", "profit_loss_ratio", "total_trades", "t_trade_count", "t_win_rate",
                "t_pnl", "open_pnl", "add_pnl", "reduce_pnl", "stop_loss_pnl", "commission_total"]
check("报告指标完整", all(k in report["metrics"] for k in need_metrics),
      str([k for k in need_metrics if k not in report.get("metrics", {})]))
check("资金曲线非空", len(report.get("equity_curve", [])) > 0)
check("交易明细字段", len(report.get("trade_log", [])) > 0 and all(
    k in report["trade_log"][0] for k in ["code", "time", "side", "price", "volume", "fee", "type", "reason"]))
check("月度收益", len(report.get("monthly_returns", [])) > 0)
check("持仓快照", len(report.get("position_snapshots", [])) > 0)

code = report["trade_log"][0]["code"] if report["trade_log"] else "600000"
r = client.get(f"/backtests/{bt_id}/kline", params={"code": code})
check("K线数据", r.status_code == 200 and len(r.json().get("bars", [])) > 0 and len(r.json().get("marks", [])) > 0, r.text[:300])
check("K线marks字段", r.status_code == 200 and all(k in r.json()["marks"][0] for k in ["time", "price", "side", "type", "trade_id"]))

# 5. 分钟级做T回测（grid_atr_mult 调小确保随机数据下网格稳定触发；关闭止损避免清仓打断做T周期）
bt2 = dict(bt_cfg, name="联调-网格做T分钟", strategy_id="grid_t", period="minute5",
           params={"base_pct": 30, "grid_atr_mult": 0.8, "max_t_times": 6},
           risk_config={"stop_loss_mode": "none", "max_intraday_trades": 20})
r = client.post("/backtests", json=bt2)
check("创建分钟回测", r.status_code == 200, r.text)
st = wait_task(r.json()["task_id"], 300)
check("分钟回测成功", st["status"] == "success", str(st))
if st["status"] == "success":
    rep2 = client.get(f"/backtests/{r.json()['task_id']}/report").json()
    types = {t["type"] for t in rep2["trade_log"]}
    check("存在做T记录", "做T" in types, f"实际类型: {types}")
    check("做T统计", rep2["metrics"]["t_trade_count"] > 0, str(rep2["metrics"]))
    k2 = client.get(f"/backtests/{r.json()['task_id']}/kline", params={"code": "600000"})
    check("分钟K线格式", k2.status_code == 200 and len(k2.json()["bars"][0]["date"]) >= 10)

# 6. 寻优
opt_req = {
    "name": "联调-寻优",
    "backtest_config": bt_cfg,
    "param_space": {"fast": {"type": "int", "low": 3, "high": 10}, "slow": {"type": "int", "low": 15, "high": 30}},
    "n_trials": 8,
    "metric": "annual_return",
}
r = client.post("/optimize", json=opt_req)
check("创建寻优", r.status_code == 200, r.text)
opt_id = r.json()["task_id"]
st = wait_task(opt_id, 600)
check("寻优成功", st["status"] == "success", str(st))
r = client.get(f"/optimize/{opt_id}")
check("寻优详情", r.status_code == 200, r.text[:300])
opt = r.json()
check("最优参数", "best_params" in opt and opt.get("best_params"), str(opt)[:300])
check("trials完整", isinstance(opt.get("trials"), list) and len(opt["trials"]) == 8)
check("参数重要性", "param_importance" in opt and opt["param_importance"], str(opt.get("param_importance")))
check("样本外验证", "oos_validation" in opt and "overfit_risk" in opt["oos_validation"], str(opt.get("oos_validation"))[:300])
check("寻优含backtest_config", "backtest_config" in opt, "前端一键重跑依赖此字段")

# 7. AI 分析（无key应友好失败）
r = client.get("/ai/profiles")
check("AI profiles", r.status_code == 200 and "profiles" in r.json(), r.text)
r = client.post("/ai/analyze", json={"backtest_id": bt_id})
if r.status_code == 200:
    st = wait_task(r.json()["task_id"], 120)
    ok = (st["status"] == "success") or (st["status"] == "failed" and "API Key" in st.get("error", ""))
    check("AI分析(无key友好失败或成功)", ok, str(st))
else:
    check("AI分析(无key友好失败或成功)", r.status_code in (400, 422, 500) and "Key" in r.text, r.text)

# 7.5 Key 管理 + 多用户隔离
r = client.get("/keys")
check("Key列表", r.status_code == 200 and "keys" in r.json() and "registry" in r.json(), r.text)
r = client.post("/keys", json={"provider": "deepseek", "api_key": "sk-e2e-test-9999abcd", "label": "e2e", "sort_order": 1})
check("新增Key", r.status_code == 200, r.text)
kid = r.json()["id"]
r = client.get("/keys")
check("Key脱敏", "sk-e2e-test-9999abcd" not in json.dumps(r.json(), ensure_ascii=False)
      and r.json()["keys"][0]["api_key"].startswith("sk-"), str(r.json()["keys"][:1]))
r = client.put(f"/keys/{kid}", json={"label": "e2e改", "enabled": False})
check("改Key", r.status_code == 200 and client.get("/keys").json()["keys"][0]["enabled"] is False)
check("删Key", client.delete(f"/keys/{kid}").status_code == 200 and client.get("/keys").json()["keys"] == [])
r = client.post("/keys", json={"provider": "bad", "api_key": "sk-12345678"})
check("非法provider拒绝", r.status_code == 400)

# 多用户：admin 创建用户 → 新用户 key 隔离
r = client.post("/users", json={"username": "e2e_user", "password": "e2e123456"})
check("创建用户", r.status_code == 200, r.text)
c2 = httpx.Client(base_url=BASE, timeout=60)
r2 = c2.post("/auth/login", json={"username": "e2e_user", "password": "e2e123456"})
check("新用户登录", r2.status_code == 200, r2.text)
c2.headers["Authorization"] = f"Bearer {r2.json()['token']}"
check("新用户无权管理用户", c2.get("/users").status_code == 403)
r2 = c2.post("/keys", json={"provider": "openrouter", "api_key": "sk-e2e-user-key-777", "label": "u"})
check("新用户加Key", r2.status_code == 200, r2.text)
check("admin看不到新用户Key", all(k["provider"] != "openrouter" for k in client.get("/keys").json()["keys"]))
check("新用户看不到admin数据隔离OK", len(c2.get("/keys").json()["keys"]) == 1)
r2 = c2.get("/ai/profiles")
check("新用户profiles含DB池", r2.status_code == 200 and r2.json().get("mode") == "db_key_pool"
      and r2.json()["user_key_pool"][0]["provider"] == "openrouter", r2.text[:200])
check("删除用户", client.delete("/users/e2e_user").status_code == 200)
check("删除后无法登录", c2.post("/auth/login", json={"username": "e2e_user", "password": "e2e123456"}).status_code == 401)
c2.close()

# 8. 数据状态
r = client.get("/data/status")
check("数据状态", r.status_code == 200 and r.json().get("daily", {}).get("stocks", 0) >= 1, r.text[:300])

# 9. 未完成任务进度
r = client.get("/backtests/not_exist/status")
check("不存在任务404", r.status_code == 404)

print("\n" + "=" * 50)
print(f"结果: {'全部通过' if not FAIL else '失败项: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
