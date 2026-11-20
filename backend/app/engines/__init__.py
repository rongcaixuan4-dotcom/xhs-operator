# -*- coding: utf-8 -*-
"""引擎包：唯一接触 Playwright 的地方。

- browser.py  BrowserManager 单例（persistent context / 页面租借 / 登录态检查 / 截图留痕）
- collect.py  采集引擎；creator.py 二创引擎；publish.py 发布引擎；snapshot.py 快照引擎

安全红线（D3）：所有小红书读写只经页面 DOM 操作；
绝不逆向签名算法；绝不导出 Cookie 直连写接口。
"""
from .browser import BrowserManager, get_browser_manager

__all__ = ["BrowserManager", "get_browser_manager"]
