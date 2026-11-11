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
2. 它起的是 **daemon** 线程（进程退出不被它拖住），慢 tick 也不拖住 startup；
3. lifespan 里确实调用了它 —— 钉住「装配」这一层，别只留个没人用的函数。

注意：**不能在模块顶层 `from app import main`** —— `app.main` 一 import 就在
`DB_PATH` 上建库（默认 `/app/data`），CI 里 `/app` 不可写会直接 ImportError
（2026-11-11 PR #20 首次 CI 就是这么红的）。必须先把 `DB_PATH` 指到临时目录再导入。
"""
from __future__ import annotations

import inspect
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMrStartupTick(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="mr-startup-")
        cls._env = {k: os.environ.get(k) for k in
                    ("DB_PATH", "ENABLE_SCHEDULER", "ADMIN_TOKEN", "TOKEN_SECRET")}
        os.environ["DB_PATH"] = os.path.join(cls.tmp, "t.db")
        os.environ["ENABLE_SCHEDULER"] = "false"      # 本组只测装配函数，不起调度器
        os.environ["ADMIN_TOKEN"] = cls._env.get("ADMIN_TOKEN") or "mr-startup-token"
        os.environ["TOKEN_SECRET"] = cls._env.get("TOKEN_SECRET") or "mr-startup-secret"
        from app import main as m                     # 必须在设好 DB_PATH 之后导入
        cls.m = m

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_runs_mr_tick_once_in_background(self):
        """启动补跑必须是「后台线程 + 恰好一次」，且不阻塞调用方。"""
        m = self.m
        done = threading.Event()
        calls = []

        def fake_tick():
            calls.append(threading.current_thread().name)
            done.set()

        orig = m._job_mr_tick
        m._job_mr_tick = fake_tick                     # 模块全局查找，替身能生效
        try:
            th = m._start_mr_startup_tick()
            self.assertIsNotNone(th, "应当返回线程对象")
            assert th is not None                      # 供类型检查收窄
            self.assertTrue(done.wait(2.0), "启动 tick 未在 2s 内执行")
            self.assertEqual(calls, ["mr-startup-tick"],
                             "应当恰好跑一次，且跑在专用后台线程里")
            self.assertTrue(th.daemon, "必须是 daemon 线程，别拖住进程退出")
            self.assertFalse(th.is_alive(), "tick 结束后线程应已退出")
        finally:
            m._job_mr_tick = orig

    def test_thread_does_not_block_caller(self):
        """tick 慢（模拟读账户/行情）时，`_start_mr_startup_tick()` 必须立即返回。"""
        m = self.m
        release = threading.Event()

        def slow_tick():
            release.wait(5.0)

        orig = m._job_mr_tick
        m._job_mr_tick = slow_tick
        try:
            t0 = time.monotonic()
            th = m._start_mr_startup_tick()
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.3, f"startup 被 tick 拖住：{elapsed:.3f}s")
            assert th is not None                      # 供类型检查收窄
            self.assertTrue(th.is_alive(), "慢 tick 应当还在后台跑")
        finally:
            release.set()
            m._job_mr_tick = orig

    def test_lifespan_wires_the_startup_tick(self):
        """装配层：lifespan 源码里必须调用它，且落在调度器分支内、why-preheat 之前。"""
        src = inspect.getsource(self.m.lifespan)
        self.assertIn("_start_mr_startup_tick()", src,
                      "lifespan 没有调用启动补跑，冷启动窗口会回来")
        idx_branch = src.index("ENABLE_SCHEDULER")
        idx_call = src.index("_start_mr_startup_tick()")
        idx_after = src.index("_start_why_preheat()")
        self.assertGreater(idx_call, idx_branch,
                           "启动补跑必须在 ENABLE_SCHEDULER 分支内（隔离冒烟不许触网）")
        self.assertLess(idx_call, idx_after,
                        "启动补跑应在 scheduler.start() 之后、why-preheat 之前")


if __name__ == "__main__":
    unittest.main()
