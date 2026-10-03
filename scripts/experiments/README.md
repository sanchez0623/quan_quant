# scripts/experiments —— 一次性实验/诊断脚本收敛处

原散落在仓库根、`backend/`、`backend/scripts/`、`backend/tests/` 的 `_*.py`，
2026-10-03 按用户要求全部收敛到此，共 84 个。`_` 前缀含义是「一次性产物」：

- ruff 配置已排除 `_*.py`，本目录**不参与 CI 门禁**，也不是产品代码；
- 其中 36 个原本未跟踪，按 `.gitignore` 规则继续不入库（与 `.workbuddy/` 同类）；
- 其中 48 个是历史入库脚本，仍受 git 跟踪，可 `git log --follow` 追溯。

## ⚠️ 重跑前必读：路径锚点是按「原位置」写的

脚本里的 `Path(__file__).resolve().parents[N]` / `Path(__file__).parent` 是按搬家前
的深度计算的，搬家后语义如下：

| 原位置 | 原 `parents[1]` | 原 `parents[2]` | 现在（`scripts/experiments/`） |
|---|---|---|---|
| `backend/scripts/_x.py` | `backend/` | 仓库根 | `parents[1]`=`scripts/`，`parents[2]`=仓库根 |
| `backend/_x.py` | 仓库根 | 仓库根的上级 | `parents[2]`=仓库根 |
| `_x.py`（仓库根） | 仓库根的上级 | — | `parents[2]`=仓库根 |

结论：

- 取**仓库根**的写法（如 `parents[2] / "data" / "meta.db"`、`"data" / "reports"`）
  **语义未变，无需修改**；
- 取 **`backend/`** 的 `parents[1]` 需改为 `Path(__file__).resolve().parents[2] / "backend"`；
- `Path(__file__).parent / "out"` 现在指向 `scripts/experiments/out/`（原 `backend/scripts/out/`）；
- 用 `BACKEND / "scripts" / "_xxx_worker.py"` 引 worker 的（`_backfill_minute5.py`、
  `_backfill_m5_holes.py`）现在与 worker 同目录，改成
  `Path(__file__).parent / "_xxx_worker.py"` 即可。

搬动时未逐个改写锚点（用户只要求收敛归档，且多数脚本会拉行情/写业务库，不能实跑验证）。
`_backfill_minute5.py`、`_backfill_m5_holes.py`、`_fix_live_open_day.py`、
`_migrate_task_tags.py`、`_scan_refill_victims.py` 属仍可能复用的运维脚本，
下次要用时按上表先修锚点。
