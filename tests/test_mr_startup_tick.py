"""冷启动窗口回归：调度器起来后必须**立刻**补跑一轮 MR tick。

背景（2026-11-11 取证）：`_MR_STATUS` 初始化是空 dict（main.py:585），只有
`_job_mr_tick` 跑完才整体重绑；排程 `CronTrigger(minute="2-59/5")` 决定了首次
执行要等下一个 ≡2 mod 5 的分钟。实测一次重启：03:36:23 容器 start、03:37:23
调度器 start、**03:42:00** 才首次 tick 完成 —— 窗口 4m39s，最坏接近 5 分钟。

窗口内 `/api/hl/status` 的 `mr` 是 null，oracle1 探针 `d.get("mr") or {}` 读成
`off:?`（稳态 `on:flat`）→ monitor 写假 pending → 每次重启白唤醒一次值班；
同时 `_mr_margin_used()` 恒 0 → 主策略定仓漏扣 MR 占用。

本文件钉住三条：
1. `_start_mr_startup_tick()` 真的把 `_job_mr_tick` 跑了一次（后台线程，不阻塞）；
2. 它起的是 **daemon** 线程（进程退出不被它拖住），且拿到 tick 的引用；
3. lifespan 里确实调用了它 —— 钉住「装配」这一层，别只留个没人用的函数。
"""
from __future__ import annotations

import inspect
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main as app_main


class TestMrStartupTick(unittest.TestCase):
    def test_runs_mr_tick_once_in_background(self):
        """启动补跑必须是「后台线程 + 恰好一次」，且不阻塞调用方。"""
        done = threading.Event()
        calls = []

        def fake_tick():
            calls.append(threading.current_thread().name)
            done.set()

        orig = app_main._job_mr_tick
        app_main._job_mr_tick = fake_tick          # 模块全局查找，替身能生效
        try:
            th = app_main._start_mr_startup_tick()
            self.assertIsNotNone(th, "应当返回线程对象")
            assert th is not None                      # 供类型检查收窄
            self.assertTrue(done.wait(2.0), "启动 tick 未在 2s 内执行")
            self.assertEqual(calls, ["mr-startup-tick"],
                             "应当恰好跑一次，且跑在专用后台线程里")
            self.assertTrue(th.daemon, "必须是 daemon 线程，别拖住进程退出")
            self.assertFalse(th.is_alive(), "tick 结束后线程应已退出")
        finally:
            app_main._job_mr_tick = orig

    def test_thread_does_not_block_caller(self):
        """tick 慢（模拟读账户/行情 0.5s）时，`_start_mr_startup_tick()` 必须立即返回。"""
        release = threading.Event()

        def slow_tick():
            release.wait(5.0)

        orig = app_main._job_mr_tick
        app_main._job_mr_tick = slow_tick
        try:
            import time
            t0 = time.monotonic()
            th = app_main._start_mr_startup_tick()
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.3, f"startup 被 tick 拖住：{elapsed:.3f}s")
            assert th is not None                      # 供类型检查收窄
            self.assertTrue(th.is_alive(), "慢 tick 应当还在后台跑")
        finally:
            release.set()
            app_main._job_mr_tick = orig

    def test_lifespan_wires_the_startup_tick(self):
        """装配层：lifespan 源码里必须调用它（且只在调度器分支内）。"""
        src = inspect.getsource(app_main.lifespan)
        self.assertIn("_start_mr_startup_tick()", src,
                      "lifespan 没有调用启动补跑，冷启动窗口会回来")
        # 必须在 ENABLE_SCHEDULER 分支内（隔离冒烟 ENABLE_SCHEDULER=false 不许触网）
        idx_branch = src.index("ENABLE_SCHEDULER")
        idx_call = src.index("_start_mr_startup_tick()")
        idx_else = src.index("_start_why_preheat()")
        self.assertGreater(idx_call, idx_branch)
        self.assertLess(idx_call, idx_else,
                        "启动补跑应在调度器分支内、why-preheat 之前")


if __name__ == "__main__":
    unittest.main()
