# -*- coding: utf-8 -*-
"""「XHS 运营助手」后端应用包。

分层架构（参考 CreatorHub）：
- api/       路由层：参数校验与转发，不直接调 engines/
- services/  业务服务：流水线编排、评分、审核、复盘（无 IO 细节）
- engines/   引擎层：唯一接触 Playwright 的地方
- ai/        AI 适配层：LLM 与生图双路由
- knowledge/ 知识检索
- models.py  SQLModel 表定义；scheduler.py APScheduler 编排

安全红线：不逆向小红书签名；不导出 Cookie 直连写接口；发布一律真浏览器。
"""
__version__ = "0.1.0"
