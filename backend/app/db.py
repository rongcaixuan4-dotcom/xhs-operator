# -*- coding: utf-8 -*-
"""SQLite engine / Session / init_db（自动建表 + 种子数据）。

种子数据（架构第 3.2 节）：
- AppSetting 默认值：review_required=true（D6 审核默认开启）、
  publish_slots（默认发布时段）、daily_min_notes=3、current_account_id=1；
- ai_config：llm 与 image 两行默认配置（base_url 默认 lk888.ai，model 可空待用户填写）。
"""
from __future__ import annotations

import json
import logging
from typing import Iterator, Optional

from sqlmodel import Session, SQLModel, create_engine, select

from .config import get_settings
from .models import AiConfig, AppSetting

logger = logging.getLogger("app.db")

_engine = None  # 全局 engine 单例（懒创建，等 get_settings 就绪）


def get_engine():
    """获取（或创建）SQLite engine。"""
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{settings.db_path}",
            echo=False,
            connect_args={"check_same_thread": False},  # FastAPI 多线程访问
        )
        logger.info("SQLite engine 就绪：%s", settings.db_path)
    return _engine


def init_db() -> None:
    """自动建表 + 写入种子数据（幂等，可重复调用）。"""
    # 确保所有模型模块已被导入（SQLModel.metadata 才能收集到表）
    from . import models  # noqa: F401

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _seed_defaults(engine)
    logger.info("数据库初始化完成（12 张表 + 种子数据校验）")


def _seed_defaults(engine) -> None:
    """写入默认 AppSetting 与 AiConfig（已存在则跳过）。"""
    default_settings: dict[str, object] = {
        # D6：二创审核默认开启，可配置
        "review_required": True,
        # 默认发布时段（每日 3 个候选槽位，发布引擎按顺序取用）
        "publish_slots": ["10:00", "15:00", "19:00"],
        # 每日最少入选笔记数（R06：每日 ≥3 篇通过筛选）
        "daily_min_notes": 3,
        # 当前生效账号（MVP 单账号，D2）
        "current_account_id": 1,
    }
    with Session(engine) as session:
        for key, value in default_settings.items():
            existing = session.exec(
                select(AppSetting).where(AppSetting.key == key)
            ).first()
            if existing is None:
                session.add(AppSetting(key=key, value=json.dumps(value, ensure_ascii=False)))
                logger.info("种子 AppSetting 写入：%s=%s", key, value)

        # AI 双配置默认行（lk888.ai 默认网关；api_key/model 留空待用户填写）
        ai_defaults = {
            "llm": {"base_url": "https://lk888.ai/v1", "model": "", "extra": {"timeout": 120}},
            "image": {
                "base_url": "https://lk888.ai/v1",
                "model": "tt-image-2.5",
                "extra": {"size": "1024x1024", "fallback_models": ["banana-1", "banana-2"]},
            },
        }
        for kind, conf in ai_defaults.items():
            existing = session.exec(
                select(AiConfig).where(AiConfig.kind == kind, AiConfig.account_id == 1)
            ).first()
            if existing is None:
                session.add(AiConfig(
                    account_id=1,
                    kind=kind,
                    base_url=conf["base_url"],
                    api_key="",
                    model=conf["model"],
                    extra=json.dumps(conf["extra"], ensure_ascii=False),
                    enabled=False,  # 未配置 api_key 前不可用
                ))
                logger.info("种子 AiConfig 写入：kind=%s", kind)
        session.commit()


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：获取数据库会话。"""
    with Session(get_engine()) as session:
        yield session


def get_current_account_id(session: Session) -> int:
    """统一取当前账号 id（架构第 9 节 #3：P1 多账号的唯一改动点）。"""
    setting = session.exec(
        select(AppSetting).where(AppSetting.key == "current_account_id")
    ).first()
    if setting is None:
        return 1
    try:
        return int(json.loads(setting.value))
    except (ValueError, TypeError):
        return 1


def get_app_setting(session: Session, key: str, default=None):
    """读取 AppSetting（JSON 反序列化）。"""
    row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except (ValueError, TypeError):
        return default


def set_app_setting(session: Session, key: str, value) -> None:
    """写入（或覆盖）AppSetting。"""
    row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
    payload = json.dumps(value, ensure_ascii=False)
    if row is None:
        session.add(AppSetting(key=key, value=payload))
    else:
        row.value = payload
        row.touch()
    session.commit()


def reset_engine() -> None:
    """测试辅助：重置 engine 单例。"""
    global _engine
    _engine = None
