# -*- coding: utf-8 -*-
"""实盘信号机（LIVE_SIGNAL_SYSTEM）：盘前流程 / 推送 / 状态存取。

阶段对照（docs/LIVE_SIGNAL_SYSTEM.md §10）：
- M1 盘前信号机：premarket.py（本包）+ db.py sig_* 表 + api/live.py
- M2 盘中信号机：quotes.py 多源行情 + 状态机步进化（后续阶段）
"""
