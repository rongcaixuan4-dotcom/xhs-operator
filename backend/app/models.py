# -*- coding: utf-8 -*-
"""全部 SQLModel 表定义（12 张表，架构第 3 节）。

约定：
- 所有业务表含 account_id（默认 1，D2 MVP 单账号，预留 P1 多账号）；
- JSON 数组/对象字段统一存 TEXT（JSON 字符串），服务层负责序列化/反序列化；
- 时间统一本地时区（Asia/Shanghai）naive datetime，API 序列化为 ISO 8601；
- 状态字段取值见各类 docstring。

安全红线（全项目）：不逆向小红书签名；不导出 Cookie 直连写接口；发布一律真浏览器。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def now_cn() -> datetime:
    """统一取本地时区 naive 当前时间（约定第 9 节 #8）。"""
    return datetime.now()


def parse_json_field(raw: Optional[str], default):
    """JSON 字段安全解析：空/损坏时回退默认值（错误不吞，记日志由调用方处理）。"""
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


# ────────────────────────── 1. persona 人设 ──────────────────────────
class Persona(SQLModel, table=True):
    """人设：昵称定位/风格/人群/禁忌词/示例笔记；同账号至多一条 is_active=1（服务层保证）。"""

    __table_args__ = (Index("ix_persona_account_active", "account_id", "is_active"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(default=1, index=True)
    name: str = ""                       # 人设名
    nickname: str = ""                   # 账号昵称定位
    positioning: str = ""                # 一句话定位
    style_tags: str = Field(default="[]")    # JSON 数组：风格标签
    audience: str = ""                   # 目标人群
    taboo_words: str = Field(default="[]")   # JSON 数组：禁忌词
    sample_notes: str = Field(default="[]")  # JSON 数组：示例笔记文本
    is_active: bool = False
    created_at: datetime = Field(default_factory=now_cn)
    updated_at: datetime = Field(default_factory=now_cn)

    def style_tag_list(self) -> list[str]:
        return parse_json_field(self.style_tags, [])

    def taboo_word_list(self) -> list[str]:
        return parse_json_field(self.taboo_words, [])

    def sample_note_list(self) -> list[str]:
        return parse_json_field(self.sample_notes, [])


# ────────────────────────── 2. knowledge_chunk 知识切片 ──────────────────────────
class KnowledgeChunk(SQLModel, table=True):
    """知识切片：experience 人工经验 | review 周复盘回流 | negative 驳回负样本。"""

    __table_args__ = (
        Index("ix_knowledge_account_source", "account_id", "source_type"),
        Index("ix_knowledge_account_enabled", "account_id", "enabled"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(default=1, index=True)
    source_type: str = "experience"      # experience | review | negative
    source_title: str = ""
    content: str = Field(default="", sa_column_kwargs={"type_": None})  # TEXT
    char_count: int = 0
    enabled: bool = True
    created_at: datetime = Field(default_factory=now_cn)


# ────────────────────────── 3. topic 监测话题 ──────────────────────────
class Topic(SQLModel, table=True):
    """监测话题：关键词、评分权重、每日篇数、采集触发时刻（"09:00" 等）。"""

    __table_args__ = (Index("ix_topic_account_enabled", "account_id", "enabled"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(default=1, index=True)
    keyword: str = ""
    priority_weight: float = 1.0         # 评分加权
    daily_quota: int = 5                 # 每日篇数
    collect_times: str = Field(default='["09:00","14:00","19:00"]')  # JSON 数组
    enabled: bool = True
    created_at: datetime = Field(default_factory=now_cn)

    def collect_time_list(self) -> list[str]:
        return parse_json_field(self.collect_times, [])


# ────────────────────────── 4. collected_note 采集笔记 ──────────────────────────
class CollectedNote(SQLModel, table=True):
    """采集笔记；UNIQUE(account_id, xhs_note_id) 防重采。status: collected|selected|discarded。"""

    __table_args__ = (
        UniqueConstraint("account_id", "xhs_note_id", name="uq_collected_account_note"),
        Index("ix_collected_account_status", "account_id", "status"),
        Index("ix_collected_account_time", "account_id", "collected_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(default=1, index=True)
    topic_id: Optional[int] = Field(default=None, foreign_key="topic.id")
    xhs_note_id: str = Field(index=True)
    title: str = ""
    content: str = ""
    author: str = ""
    image_urls: str = Field(default="[]")     # JSON 数组：原帖图片 URL
    local_images: str = Field(default="[]")   # JSON 数组：本地路径（data/collected/ 相对路径）
    likes: int = 0
    collects: int = 0
    comments: int = 0
    match_score: float = 0.0             # 人设+知识库评分
    score_detail: str = Field(default="{}")  # JSON：评分构成明细（便于归因）
    status: str = "collected"            # collected | selected | discarded
    collected_at: datetime = Field(default_factory=now_cn)

    def image_url_list(self) -> list[str]:
        return parse_json_field(self.image_urls, [])

    def local_image_list(self) -> list[str]:
        return parse_json_field(self.local_images, [])


# ────────────────────────── 5. creation_draft 二创稿 ──────────────────────────
class CreationDraft(SQLModel, table=True):
    """二创稿。status: generating|pending_review|approved|rejected|queued。

    similarity_score：与原稿标题+文案相似度，要求 <0.70（R07）。
    """

    __table_args__ = (Index("ix_draft_account_status", "account_id", "status"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(default=1, index=True)
    collected_note_id: Optional[int] = Field(default=None, foreign_key="collectednote.id")
    persona_id: Optional[int] = Field(default=None, foreign_key="persona.id")
    title: str = ""
    content: str = ""
    image_paths: str = Field(default="[]")    # JSON 数组：data/creations/ 相对路径
    cover_path: str = ""                      # 封面路径
    similarity_score: float = 0.0
    status: str = "generating"
    reject_reason: str = ""
    created_at: datetime = Field(default_factory=now_cn)
    updated_at: datetime = Field(default_factory=now_cn)

    def image_path_list(self) -> list[str]:
        return parse_json_field(self.image_paths, [])


# ────────────────────────── 6. publish_task 发布任务 ──────────────────────────
class PublishTask(SQLModel, table=True):
    """发布任务。状态机：queued→publishing→verifying→done / failed / canceled。

    dedup_hash = SHA1(标题 + 图片字节指纹)，UNIQUE(account_id, dedup_hash) 硬防重发（R10）。
    """

    __table_args__ = (
        UniqueConstraint("account_id", "dedup_hash", name="uq_publish_account_dedup"),
        Index("ix_publish_account_status", "account_id", "status"),
        Index("ix_publish_account_scheduled", "account_id", "scheduled_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(default=1, index=True)
    draft_id: Optional[int] = Field(default=None, foreign_key="creationdraft.id")
    status: str = "queued"
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    publish_config: str = Field(default="{}")  # JSON：topics/location/source_declare/collection/mention_users/is_schedule 等
    dedup_hash: str = ""
    note_url: str = ""
    xhs_note_id: str = ""
    retry_count: int = 0
    last_error: str = ""
    created_at: datetime = Field(default_factory=now_cn)

    def publish_config_dict(self) -> dict:
        return parse_json_field(self.publish_config, {})


# ────────────────────────── 7. note_stat_snapshot 笔记数据快照 ──────────────────────────
class NoteStatSnapshot(SQLModel, table=True):
    """笔记数据快照：验收要求每篇笔记每日 ≥1 条（R11）。"""

    __table_args__ = (
        Index("ix_snapshot_account_note_time", "account_id", "xhs_note_id", "captured_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(default=1, index=True)
    publish_task_id: Optional[int] = Field(default=None, foreign_key="publishtask.id")
    xhs_note_id: str = ""
    title: str = ""                      # 冗余标题便于 Top10 展示
    exposure: int = 0
    likes: int = 0
    collects: int = 0
    comments: int = 0
    captured_at: datetime = Field(default_factory=now_cn)


# ────────────────────────── 8. account_snapshot 账号级快照 ──────────────────────────
class AccountSnapshot(SQLModel, table=True):
    """账号级快照（数据看板顶部指标卡）。"""

    __table_args__ = (Index("ix_account_snapshot_time", "account_id", "captured_at"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(default=1, index=True)
    followers: int = 0
    week_exposure: int = 0
    week_interaction: int = 0
    captured_at: datetime = Field(default_factory=now_cn)


# ────────────────────────── 9. weekly_report 周复盘报告 ──────────────────────────
class WeeklyReport(SQLModel, table=True):
    """周复盘报告。status: pending_confirm|confirmed|expired。

    insights：JSON 数组，每条 {scope: topic_weight|publish_time|prompt, content, impact, confirmed}。
    确认后按 scope 写入 KnowledgeChunk(source_type='review')。
    """

    __table_args__ = (
        UniqueConstraint("account_id", "week_start", name="uq_report_account_week"),
        Index("ix_report_account_week", "account_id", "week_start"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(default=1, index=True)
    week_start: date = Field(default_factory=date.today)
    week_end: date = Field(default_factory=date.today)
    report_md: str = ""                  # Markdown 报告正文
    insights: str = Field(default="[]")  # JSON 数组：结构化改进项
    status: str = "pending_confirm"
    confirmed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=now_cn)

    def insight_list(self) -> list[dict]:
        return parse_json_field(self.insights, [])


# ────────────────────────── 10. pipeline_run 流水线运行记录 ──────────────────────────
class PipelineRun(SQLModel, table=True):
    """流水线运行记录：UNIQUE(account_id, run_date) 当日单行更新。

    stage: monitoring/collecting/analyzing/selecting/creating/reviewing/
           queued/publishing/verifying/done/failed/snapshot/idle
    status: running | ok | error
    detail: JSON，各环节耗时/计数/错误信息/截图路径。
    """

    __table_args__ = (
        UniqueConstraint("account_id", "run_date", name="uq_pipeline_account_date"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(default=1, index=True)
    run_date: date = Field(default_factory=date.today)
    stage: str = "idle"
    status: str = "idle"
    detail: str = Field(default="{}")    # JSON
    started_at: datetime = Field(default_factory=now_cn)
    updated_at: datetime = Field(default_factory=now_cn)

    def detail_dict(self) -> dict:
        return parse_json_field(self.detail, {})


# ────────────────────────── 11. ai_config AI 双配置 ──────────────────────────
class AiConfig(SQLModel, table=True):
    """AI 双配置：每 account 每 kind（llm|image）一行；UNIQUE(account_id, kind)。"""

    __table_args__ = (UniqueConstraint("account_id", "kind", name="uq_ai_config_account_kind"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(default=1, index=True)
    kind: str = "llm"                    # llm | image
    base_url: str = ""
    api_key: str = ""                    # 接口返回时脱敏为 sk-***last4（第 9 节 #10）
    model: str = ""
    extra: str = Field(default="{}")     # JSON：超时、尺寸、默认参数
    enabled: bool = False
    updated_at: datetime = Field(default_factory=now_cn)

    def extra_dict(self) -> dict:
        return parse_json_field(self.extra, {})


# ────────────────────────── 12. app_setting 应用设置 KV ──────────────────────────
class AppSetting(SQLModel, table=True):
    """应用设置 KV。已知键：review_required / publish_slots / daily_min_notes / current_account_id。"""

    key: str = Field(primary_key=True)
    value: str = "{}"                    # JSON 序列化值
    updated_at: datetime = Field(default_factory=now_cn)

    def touch(self) -> None:
        self.updated_at = now_cn()
