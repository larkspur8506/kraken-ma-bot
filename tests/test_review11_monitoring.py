"""第十一轮复审（2026-11-15）修复的回归测试 —— 死手开关能力门闩 + MR 部分成交入场。

两个缺陷都不是交易主逻辑，但都会**说假话**给运维/复盘看：

D) **死手开关（scheduleCancel）**：`KrExchange.schedule_cancel` 是空操作（返 ok +
   `raw.noop`），旧 `_run_locked` 只看 `ok` → 会清 `hl_deadman_err` 并写一条
   「死手开关恢复可用」。运维照 runbook 打开 `HL_USE_SCHEDULE_CANCEL=1` 就会以为
   「失联 30 分钟自动撤全部挂单」这层保护存在。现在由 venue 显式声明
   `deadman_supported=False`（与 flows_supported / funding_supported 同一口径），
   执行器整段跳过；就算某个 venue 用 `ok + raw.noop` 表态，也落进失败分支。

E) **MR 部分成交入场**：Kraken 对部分成交回 `partially_filled`（不是失败）。旧集合
   只认 `filled/resting`，一次正常的部分成交被塞进「回执丢失」通道 —— 台账虽会被
   `_adopt_entry` 补上，但会推「入场回执失败」的错告警、raw 里标
   `adopt_reason=fill_confirmation_lost`。现在走正常建账路径（数量取交易所实际持仓）。

全部离线：临时 SQLite + 假 info/exchange，不连交易所、不发单。
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tests.test_hl_executor import Base as EntryBase
from tests.test_review7_mr import (ENTRY_SESSIONS, NOW, Ex, Info, MrBase, make_leg)


class AlertSpy:
    """通知器替身：记录 `_alert` 真正发出去的文本。

    别用 `leg._alert = alerts.append` 做「回调」：`_alert` 是生产告警入口，带
    `kind/key/fill/dedupe` 等关键字参数，用位置参数签名顶替它 → 一加参数就
    TypeError，而真实发送路径（落 `alert_logs`、事件键去重、fill_push 开关）
    反而一行都没被覆盖。走 `notifier` 这个正式注入点，既记文本又跑完整流程。
    """

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.messages = []

    def send_message(self, text):
        self.messages.append(str(text))
        return self.ok


class DeadmanCapability(EntryBase):
    """死手开关只在 venue 真的支持时才续期/记账。"""

    def _schedule_calls(self):
        return [c for c in self.ex.calls if c and c[0] == "schedule_cancel"]

    def _deadman_events(self):
        return [e for e in self.events()
                if "死手开关" in str(e.get("message") or "")]

    def test_unsupported_venue_never_calls_schedule_cancel(self):
        self.info.deadman_supported = False          # 真实 KrInfo 的声明方式
        self.p.use_schedule_cancel = True
        self.executor().run()
        self.assertEqual(self._schedule_calls(), [],
                         "venue 不支持死手，就不该再打这个空操作接口")
        self.assertEqual(self._deadman_events(), [],
                         "更不许写「死手开关恢复可用」这种假保护")

    def test_unsupported_venue_does_not_clear_error_flag(self):
        # 旧版会把这个标记清 0 并写「恢复可用」—— 那是「一句话说了两遍假保护」
        self.db.set_config("hl_deadman_err", "1")
        self.info.deadman_supported = False
        self.p.use_schedule_cancel = True
        self.executor().run()
        self.assertEqual(self.db.get_config_str("hl_deadman_err"), "1")
        self.assertEqual(self._deadman_events(), [])

    def test_noop_result_is_not_success(self):
        """venue 若用 `ok + raw.noop` 表态「这条调用什么都不做」，不许当成设置成功。"""
        self.p.use_schedule_cancel = True             # info 未声明 → 默认 True，会真的调用
        self.ex.schedule_cancel = lambda **kw: {
            "ok": True, "status": "resting", "error": None, "raw": {"noop": "disabled"}}
        self.executor().run()
        self.assertEqual(self.db.get_config_str("hl_deadman_err"), "1")
        self.assertIn("死手开关设置失败", " ".join(
            str(e.get("message") or "") for e in self.events()))

    def test_supported_venue_still_renews_and_clears_flag(self):
        """HL 形状（未声明该属性）行为不变：正常续期，成功后清掉错误标记。"""
        self.db.set_config("hl_deadman_err", "1")
        self.p.use_schedule_cancel = True
        self.executor().run()
        self.assertEqual(len(self._schedule_calls()), 1)
        self.assertEqual(self.db.get_config_str("hl_deadman_err"), "0")
        self.assertIn("死手开关恢复可用", " ".join(
            str(e.get("message") or "") for e in self.events()))


class MrPartialEntry(MrBase):
    """Kraken 部分成交入场 → 按交易所实际持仓建账，而不是报「回执失败」。"""

    def test_partial_fill_enters_via_normal_path(self):
        info = Info(szi=0.0, entry=100.0, mid=100.0)

        def partial_entry(coin, is_buy, sz, limit_px, **kw):
            info.szi = 6.0                            # 只成交计划的一部分
            info.entry = limit_px
            return {"ok": True, "status": "partially_filled", "oid": "p1",
                    "avg_px": limit_px, "filled_sz": 6.0, "error": "", "raw": {}}

        ex = Ex(info, entry=partial_entry)
        leg = make_leg(self.db, info, ex, clock_ms=NOW)
        spy = AlertSpy()
        leg.notifier = spy
        with patch.object(leg, "_candles", return_value=[{"c": "100"}]), \
                patch("app.hl.mr.mr_signal.aggregate_sessions_ex",
                      return_value=(ENTRY_SESSIONS, [])):
            res = leg.tick(now_ms=NOW)

        self.assertEqual(res["state"], "entered")
        alerts = spy.messages
        row = self.db.mr_open_trade()
        self.assertIsNotNone(row, "部分成交也必须建台账")
        self.assertAlmostEqual(float(row["qty"]), 6.0, places=9,
                              msg="台账数量取交易所实际持仓，不是计划量")
        self.assertFalse(any("回执失败" in str(a) for a in alerts),
                         "部分成交不是「回执失败」，不许推错告警")
        # 正常路径必须**推一条成交回报**，且把「计划 vs 实际」写明（只报 6 张
        # 会被读成按计划满仓）。按文案筛成交回报：夹具的限价离 mid 正好 20bp，
        # 会额外触发一条滑点停用告警（与本缺陷无关）。
        fills = [a for a in alerts if "入场成交" in a]
        self.assertEqual(len(fills), 1, f"部分成交只该有一条成交回报：{alerts}")
        msg = fills[0]
        self.assertIn("成交 6", msg)
        self.assertIn("部分成交", msg)
        kinds = [str(r["kind"] or "") for r in self.db.recent_alerts(50)]
        self.assertTrue(any(k.startswith("mr_fill_entry:") for k in kinds),
                        f"成交回报要落 alert_logs（补推依据）：{kinds}")
        raw = json.loads(row["raw"] or "{}")
        self.assertNotEqual(raw.get("adopt_reason"), "fill_confirmation_lost",
                            "正常部分成交不许被标成「确认丢失后补记」")
        self.assertEqual(raw.get("result_status"), "partially_filled")


if __name__ == "__main__":                                     # pragma: no cover
    unittest.main()
