# -*- coding: utf-8 -*-
"""pytest fixtures：合成演示数据（3只股票300天，独立tmp目录）

注意：必须在 import app 之前设置 DATA_DIR（conftest 先于测试模块加载），
使所有测试的 meta.db / reports 等都落在临时目录，避免污染真实数据库
（llm_usage 用量统计、测试用户、模板等）。
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 测试专用 DATA_DIR（.env 中的同名配置因已存在而被跳过，不会覆盖）
if not os.environ.get("DATA_DIR"):
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="quant_test_data_")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

TEST_STOCKS = ["600000", "000001", "600036"]


@pytest.fixture(autouse=True, scope="session")
def _init_db():
    """临时 meta.db 建表（幂等）：让 bs_usage 等表不依赖测试文件执行顺序"""
    from app import db
    db.init_db()
    yield


@pytest.fixture(scope="session")
def demo_env(tmp_path_factory):
    """生成合成数据，返回 (data_dir, start_date, end_date)"""
    from app.data import synthetic
    d = tmp_path_factory.mktemp("data")
    stats = synthetic.generate_demo_data(stocks=TEST_STOCKS, days=300,
                                         data_dir=str(d), seed=42)
    return str(d), stats["start"], stats["end"]
