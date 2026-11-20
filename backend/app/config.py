# -*- coding: utf-8 -*-
"""统一配置读取：config.yaml + 环境变量覆盖，单例 get_settings()。

约定（架构第 9 节 #2）：
- 服务器级配置（端口/数据目录/profile 路径）走本模块；
- AI 配置不走 yaml，存 ai_config 表，用户在工作台「设置」页维护；
- 环境变量以 XHS_ 前缀覆盖（XHS_PORT / XHS_HOST / XHS_DATA_DIR）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("app.config")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # XHS运营助手/
_BACKEND_DIR = Path(__file__).resolve().parents[1]   # backend/


def _as_path(value: str) -> Path:
    """相对路径统一解析到项目根目录下，返回绝对 Path。"""
    p = Path(value)
    if not p.is_absolute():
        p = (_PROJECT_ROOT / p).resolve()
    return p


@dataclass
class Settings:
    """服务器级配置（内存单例）。"""

    # server
    host: str = "127.0.0.1"
    port: int = 8787
    # data 目录（全部解析为绝对路径）
    data_dir: Path = field(default_factory=lambda: _as_path("./data"))
    profiles_dir: Path = field(default_factory=lambda: _as_path("./data/profiles"))
    collected_dir: Path = field(default_factory=lambda: _as_path("./data/collected"))
    creations_dir: Path = field(default_factory=lambda: _as_path("./data/creations"))
    screenshots_dir: Path = field(default_factory=lambda: _as_path("./data/screenshots"))
    # logging
    log_level: str = "INFO"
    log_dir: Path = field(default_factory=lambda: _as_path("./logs"))
    # pipeline 调度
    daily_run_time: str = "08:00"
    snapshot_interval_minutes: int = 60
    weekly_report_day: str = "mon"
    weekly_report_time: str = "09:00"
    publish_scan_interval_minutes: int = 10
    retry_scan_interval_minutes: int = 30
    # browser
    headless: bool = False
    slow_mo_ms: int = 80
    nav_timeout_seconds: int = 30
    # 预留多账号
    current_account: int = 1

    # ── 派生路径 ──
    @property
    def db_path(self) -> Path:
        return self.data_dir / "xhs_assistant.db"

    @property
    def frontend_dist(self) -> Path:
        """前端构建产物目录（vite build 输出位置）。"""
        return _BACKEND_DIR / "static"

    @property
    def frontend_src(self) -> Path:
        """前端开发源码目录（未 build 时尝试直接挂载 index.html 不可行，仅提示用）。"""
        return _PROJECT_ROOT / "frontend"


def _apply_yaml(s: Settings, data: dict) -> None:
    """把 yaml dict 应用到 Settings（容错：键不存在/类型不对就跳过并记日志）。"""
    server = data.get("server") or {}
    s.host = str(server.get("host", s.host))
    s.port = int(server.get("port", s.port))

    d = data.get("data") or {}
    if "dir" in d:
        s.data_dir = _as_path(str(d["dir"]))
    if "profiles_dir" in d:
        s.profiles_dir = _as_path(str(d["profiles_dir"]))
    if "collected_dir" in d:
        s.collected_dir = _as_path(str(d["collected_dir"]))
    if "creations_dir" in d:
        s.creations_dir = _as_path(str(d["creations_dir"]))
    if "screenshots_dir" in d:
        s.screenshots_dir = _as_path(str(d["screenshots_dir"]))

    lg = data.get("logging") or {}
    s.log_level = str(lg.get("level", s.log_level)).upper()
    if "dir" in lg:
        s.log_dir = _as_path(str(lg["dir"]))

    pl = data.get("pipeline") or {}
    s.daily_run_time = str(pl.get("daily_run_time", s.daily_run_time))
    s.snapshot_interval_minutes = int(pl.get("snapshot_interval_minutes", s.snapshot_interval_minutes))
    s.weekly_report_day = str(pl.get("weekly_report_day", s.weekly_report_day))
    s.weekly_report_time = str(pl.get("weekly_report_time", s.weekly_report_time))
    s.publish_scan_interval_minutes = int(pl.get("publish_scan_interval_minutes", s.publish_scan_interval_minutes))
    s.retry_scan_interval_minutes = int(pl.get("retry_scan_interval_minutes", s.retry_scan_interval_minutes))

    br = data.get("browser") or {}
    s.headless = bool(br.get("headless", s.headless))
    s.slow_mo_ms = int(br.get("slow_mo_ms", s.slow_mo_ms))
    s.nav_timeout_seconds = int(br.get("nav_timeout_seconds", s.nav_timeout_seconds))

    ac = data.get("accounts") or {}
    s.current_account = int(ac.get("current", s.current_account))


def _apply_env(s: Settings) -> None:
    """环境变量覆盖（XHS_ 前缀）。"""
    env = os.environ
    if "XHS_HOST" in env:
        s.host = env["XHS_HOST"]
    if "XHS_PORT" in env:
        s.port = int(env["XHS_PORT"])
    if "XHS_DATA_DIR" in env:
        s.data_dir = _as_path(env["XHS_DATA_DIR"])
    if "XHS_LOG_LEVEL" in env:
        s.log_level = env["XHS_LOG_LEVEL"].upper()


def _load_yaml_dict() -> dict:
    """读取 backend/config.yaml；不存在则回退 config.example.yaml；都无则空 dict。"""
    for name in ("config.yaml", "config.example.yaml"):
        path = _BACKEND_DIR / name
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception:  # noqa: BLE001 - 配置损坏时回退默认值并记日志
                logger.exception("配置文件解析失败：%s，使用默认值", path)
    return {}


def _ensure_dirs(s: Settings) -> None:
    """确保运行目录存在（幂等）。"""
    for d in (s.data_dir, s.profiles_dir, s.collected_dir, s.creations_dir,
              s.screenshots_dir, s.log_dir):
        Path(d).mkdir(parents=True, exist_ok=True)


def _build_settings() -> Settings:
    s = Settings()
    _apply_yaml(s, _load_yaml_dict())
    _apply_env(s)
    _ensure_dirs(s)
    return s


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """获取全局配置单例（首次调用时加载）。"""
    global _settings
    if _settings is None:
        _settings = _build_settings()
        logger.info("配置加载完成：port=%s data_dir=%s", _settings.port, _settings.data_dir)
    return _settings
