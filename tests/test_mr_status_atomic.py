"""P2-1 回归：MR 状态块必须「整体换版」，读方永不见空。

背景（第五轮审查 P2-1）：`_job_mr_tick` 原来是

    _MR_STATUS.clear(); _MR_STATUS.update(_mr_leg.tick())

`_mr_leg.tick()` 实测 0.7~0.9s，而读方（`/api/hl/status` 的 `mr_fn`、面板的
`mr_fn`、定仓用的 `_mr_margin_used`）**不持 `_tick_lock`** —— 在 clear 与 update
之间读到的就是空 dict，`mr_fn` 于是返回 None，`/api/hl/status` 出 `"mr": null`，
oracle1 探针打 `off:?`，值班模型被无效唤醒（每天数次、0.3~1M tokens/天）。

修复口径：先算完 payload，再一次性 `_MR_STATUS = payload`（STORE_GLOBAL 原子）。
本文件钉住三条：
1. slow tick 期间并发轮询，拿到的只能是**旧完整态**或**新完整态**，永不 None/空；
2. mode_off 分支与 tick 异常分支同样是整体换版，且旧状态在新状态就绪前一直可读；
3. `_MR_STATUS` 这个 dict 对象**从不被原地 clear/update**（TripwireDict 直接抛）。
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "mr-atomic-token"
ADDR = "0xMRATOMIC000000000000000000000000000001"

#: 上一轮 tick 的完整状态（读方在 slow tick 期间应当一直看到它）。
OLD = {"enabled": True, "state": "flat", "recon": "ok",
       "margin_used": 1.5, "updated_at": 1_700_000_000.0}
#: 本轮 tick 的新状态。
NEW = {"enabled": True, "state": "holding", "recon": "ok",
       "margin_used": 2.5, "updated_at": 1_700_000_300.0}
MODE_OFF = {"enabled": False, "state": "mode_off", "recon": "unknown", "error": ""}


class TripwireDict(dict):
    """只允许整体换版：任何原地改动都当场判失败（旧实现就是被它抓到的）。"""

    def clear(self, *a, **k):                                         # noqa: D102
        raise AssertionError("_MR_STATUS 被原地 clear()：读方会看到空 dict")

    def update(self, *a, **k):                                        # noqa: D102
        raise AssertionError("_MR_STATUS 被原地 update()：读方可能看到半截状态")

    def pop(self, *a, **k):                                           # noqa: D102
        raise AssertionError("_MR_STATUS 被原地 pop()")

    def popitem(self, *a, **k):                                       # noqa: D102
        raise AssertionError("_MR_STATUS 被原地 popitem()")

    def __setitem__(self, key, value):                                # noqa: D102
        raise AssertionError(f"_MR_STATUS 被原地写键 {key!r}")

    def __delitem__(self, key):                                       # noqa: D102
        raise AssertionError(f"_MR_STATUS 被原地删键 {key!r}")


class _FakeHlInfo:
    """顶掉 app.hl.info.HlInfo：app.main 一 import 就会构造真实客户端并打网络。"""

    def __init__(self, *args, **kwargs):
        self._mids = {}

    def snapshot(self, address, mids=None):
        return {"ts": time.time(), "equity_total": 100.0, "equity": 100.0,
                "spot_usdc": 100.0, "unified": True, "withdrawable": 90.0,
                "margin_used": 0.0, "notional": 0.0, "positions": [], "open_orders": []}

    def all_mids(self):
        return {}


class _SlowLeg:
    """慢 tick：进入后先置位、再 sleep（模拟 0.7~0.9s 的真实耗时），最后返回/抛错。"""

    def __init__(self, result=None, exc=None, delay=0.35):
        self.result = result
        self.exc = exc
        self.delay = delay
        self.entered = threading.Event()
        self.calls = 0

    def tick(self):
        self.calls += 1
        self.entered.set()
        time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return dict(self.result or {})


class TestMrStatusAtomicSwap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import fastapi                                            # noqa: F401
        except ImportError:                                           # pragma: no cover
            raise unittest.SkipTest("fastapi 未安装（本地环境），CI 里会跑")
        cls.tmp = tempfile.mkdtemp(prefix="mr_atomic_")
        cls._env = {k: os.environ.get(k) for k in
                    ("DB_PATH", "ENABLE_SCHEDULER", "ADMIN_TOKEN", "TOKEN_SECRET",
                     "BASE_URL", "DRY_RUN", "HL_ACCOUNT_ADDRESS")}
        os.environ["DB_PATH"] = os.path.join(cls.tmp, "t.db")
        os.environ["ENABLE_SCHEDULER"] = "false"
        os.environ["ADMIN_TOKEN"] = TOKEN
        os.environ["TOKEN_SECRET"] = "mr-atomic-secret"
        os.environ["BASE_URL"] = "https://mr-atomic.example"
        os.environ["DRY_RUN"] = "true"
        os.environ["HL_ACCOUNT_ADDRESS"] = ADDR
        import app.hl.info as hi
        cls._real_info_cls = hi.HlInfo
        hi.HlInfo = _FakeHlInfo
        importlib.reload(importlib.import_module("app.config"))
        cls.mod = importlib.reload(importlib.import_module("app.main"))

    @classmethod
    def tearDownClass(cls):
        import app.hl.info as hi
        hi.HlInfo = cls._real_info_cls
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        self.mod = type(self).mod
        self._saved = (self.mod._MR_STATUS, self.mod._mr_leg, self.mod._MR_IDENTITY)

    def tearDown(self):
        self.mod._MR_STATUS, self.mod._mr_leg, self.mod._MR_IDENTITY = self._saved

    # ------------------------------------------------------------ 工具

    def _attach_leg(self, leg):
        """把假腿装上并让指纹匹配，免得 tick 时去重建真腿（会触网）。"""
        self.mod._mr_leg = leg
        self.mod._MR_IDENTITY = self.mod._mr_identity()

    def _poll_while(self, fn, worker, max_wait=5.0):
        """一边跑 worker、一边高频读 fn，返回 (samples, worker)。"""
        errors = []

        def run():
            try:
                worker()
            except BaseException as e:                                # noqa: BLE001
                errors.append(e)

        samples = []
        th = threading.Thread(target=run, daemon=True)
        th.start()
        deadline = time.time() + max_wait
        while th.is_alive() and time.time() < deadline:
            samples.append(fn())
        th.join(max_wait)
        self.assertFalse(errors, f"worker 抛了异常：{errors}")
        samples.append(fn())
        return samples

    def _assert_only(self, samples, allowed, label=""):
        self.assertGreater(len(samples), 50, f"{label}轮询样本太少，没覆盖到慢 tick 窗口")
        for s in samples:
            self.assertIsNotNone(s, f"{label}读方拿到了 None（mr: null → 探针 off:?）")
            self.assertTrue(s, f"{label}读方拿到了空 dict")
            self.assertIn(s, allowed, f"{label}读方拿到了非法中间态：{s!r}")

    # ------------------------------------------------------------ 用例

    def test_slow_tick_swap_never_exposes_empty_state(self):
        """慢 tick 期间：要么旧完整态、要么新完整态，永不为 None/空。"""
        leg = _SlowLeg(NEW, delay=0.35)
        self._attach_leg(leg)
        old_obj = TripwireDict(OLD)
        self.mod._MR_STATUS = old_obj
        mr_fn = self.mod._hl_deps().mr_fn          # 真实消费方（/api/hl/status 用的就是这个）

        samples = self._poll_while(mr_fn, self.mod._job_mr_tick)

        self.assertTrue(leg.entered.is_set())
        self._assert_only(samples, (OLD, NEW), "正常 tick：")
        # 慢 tick 窗口里必须确实读到过旧状态（否则等于没验证到窗口）
        self.assertIn(OLD, samples, "没读到旧状态 —— 轮询没覆盖到 tick 窗口")
        self.assertEqual(samples[-1], NEW, "tick 结束后应切到新状态")
        # 整体换版：新块是新对象，从不复用被读方持有的旧对象
        self.assertIsNot(self.mod._MR_STATUS, old_obj)
        self.assertIsNotNone(self.mod._mr_status_snapshot())
        # 定仓剔除读的也是这个全局：换版后立刻读到新值
        self.assertAlmostEqual(self.mod._mr_margin_used(), 2.5)

    def test_mode_off_branch_swaps_atomically(self):
        """mode_off 分支：装配（慢）期间读方仍看到旧态，装配完整体换成 mode_off。"""
        def slow_build():
            time.sleep(0.35)
            return None

        old_obj = TripwireDict(OLD)
        self.mod._MR_STATUS = old_obj
        self.mod._mr_leg = None                    # 强制走重建分支
        mr_fn = self.mod._hl_deps().mr_fn
        with mock.patch.object(self.mod, "_build_mr_leg", side_effect=slow_build):
            samples = self._poll_while(mr_fn, self.mod._job_mr_tick)

        self._assert_only(samples, (OLD, MODE_OFF), "mode_off：")
        self.assertIn(OLD, samples, "mode_off 装配窗口里没读到旧状态")
        self.assertEqual(samples[-1], MODE_OFF)
        self.assertIsNot(self.mod._MR_STATUS, old_obj)

    def test_tick_exception_branch_swaps_atomically(self):
        """tick 抛错：抛之前一直是旧态，抛完整体换成 error 块，中间不空。"""
        leg = _SlowLeg(exc=RuntimeError("kaboom"), delay=0.35)
        self._attach_leg(leg)
        old_obj = TripwireDict(OLD)
        self.mod._MR_STATUS = old_obj
        mr_fn = self.mod._hl_deps().mr_fn

        with mock.patch.object(self.mod.logger, "exception"):        # 别刷 traceback
            samples = self._poll_while(mr_fn, self.mod._job_mr_tick)

        want = {"state": "error", "recon": "unknown", "error": "RuntimeError: kaboom"}
        self._assert_only(samples, (OLD, want), "异常分支：")
        self.assertIn(OLD, samples, "异常窗口里没读到旧状态")
        self.assertEqual(samples[-1], want)
        self.assertIsNot(self.mod._MR_STATUS, old_obj)

    def test_empty_tick_payload_does_not_blank_status(self):
        """tick 返回空 dict 也不能把状态块置空（否则读方又是 mr:null）。"""
        leg = _SlowLeg({}, delay=0.0)
        self._attach_leg(leg)
        self.mod._MR_STATUS = TripwireDict(OLD)

        self.mod._job_mr_tick()

        snap = self.mod._mr_status_snapshot()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.get("state"), "error")
        self.assertIn("空状态", snap.get("error", ""))

    def test_never_ticked_yet_is_still_none(self):
        """语义边界：进程起来还没 tick 过时 mr 仍可为 None（面板显示「—」）。"""
        self.mod._MR_STATUS = {}
        self.assertIsNone(self.mod._mr_status_snapshot())

    def test_consumers_read_the_same_snapshot_helper(self):
        """两处 mr_fn（/api/hl/status 与面板）都指向同一个快照函数。"""
        self.assertIs(self.mod._hl_deps().mr_fn, self.mod._mr_status_snapshot)
        self.assertIs(self.mod._admin_deps().mr_fn, self.mod._mr_status_snapshot)


if __name__ == "__main__":
    unittest.main()
