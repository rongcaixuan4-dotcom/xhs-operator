# -*- coding: utf-8 -*-
"""pytest 共享夹具：测试数据目录隔离 + 每用例重建干净数据库。

- 在任何 app 模块导入前设置 XHS_DATA_DIR 环境变量，
  使 config.get_settings() / db.get_engine() 落到临时目录，
  不污染项目 data/xhs_assistant.db；
- autouse 夹具每个用例前后重建全部表 + 种子数据，保证用例独立幂等。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 必须在 app.* 首次 import 之前生效（config 单例懒加载）
_QA_DATA_DIR = Path(tempfile.mkdtemp(prefix="xhs_qa_data_"))
os.environ["XHS_DATA_DIR"] = str(_QA_DATA_DIR)


@pytest.fixture(autouse=True)
def _isolated_db() -> None:
    """每个用例：重建空库 + 种子数据（review_required=true 等默认值）。"""
    from sqlmodel import SQLModel

    from app.db import get_engine, init_db

    engine = get_engine()
    SQLModel.metadata.drop_all(engine)
    init_db()
    yield
    SQLModel.metadata.drop_all(engine)
