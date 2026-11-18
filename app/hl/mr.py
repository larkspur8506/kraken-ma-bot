"""MR（超跌反弹）腿 — 交易所适配（现为 Kraken PF_QQQXUSD/QQQ），挂 5 分钟 job 跑。

与 lighter-ma-bot 的 `app/mr.py` 同一套规则（信号口径 `app/mr_signal.py` 逐字节
同码，P3 对抗审核时做两侧一致性断言）；差异只在与交易所的边界：

- **数据**：`HlInfo.candles(coin, '1h', …)` 直接给 ET 可聚合的小时 bar；
  szDecimals 从 `meta?dex=xyz` 现查，绝不硬编码。
- **权益口径**：统一账户下现货 USDC 余额**已含**全部持仓浮盈亏（见
  `app/hl/equity_fix.py` 的结论），所以 sizing 基数 = `info.account_equity()`
  （现货 USDC 总额）− MR 仓占用（MR 仓自身价格敞口 = 数量 × 标记价，与 Lighter 侧
  同口径；**不用** marginSummary.marginUsed —— 那是全账户口径，见 `_read_account`）。
  注意 `spotClearinghouseState` **没有 marginSummary**，必须读 `balances`。
- **市价单**：交易所没有真市价，用激进 IOC 限价（mid ± slippage_bps 带宽）。
  价格取整走 venue 自己的精度：Kraken 用 `info.round_price()`（tickSize 整数倍），
  取不到 tickSize 时才回退 HL 的「5 位有效数字」规则（历史兼容，别当现行口径）。
- **成交只认持仓**：回执 `status=filled` 之外还要看到交易所的仓真的出现/消失；
  `resting`/`dry`/零成交一律不记账（离场未清干净保持 OPEN 重试）。下单前写一条
  `mr_entry_intent` / `mr_exit_intent` 事件：确认因读错误/重启丢失时，下一轮凭意图
  把交易所真实仓位补记进台账（只认同一 signal_date），避免 recon_err 冻结。
- **杠杆**：入场前 `update_leverage(coin, 5x, cross)`（幂等）。Kraken 期货只有全仓
  （flex），`KrExchange.update_leverage` 是 no-op；HL 时代这条是为了避免落成 30x 逐仓
  （−3.3% 就强平，而 MR 不挂止损），历史语义保留。
- **幂等**：台账 `signal_date` 唯一约束 + 「只在空仓时入场」是护栏；cloid 按 tick 分钟
  派生（同一轮重放同键，跨轮重试换键 —— 已用过的 cloid 重试会被拒）。
- **费用**：Kraken 6 级 taker 2.75bp/边（Tier 1 5bp），净盈亏按 `taker_fee_rate`
  ×名义×边数估算入账（fee_quote 列）；funding 取交易所实际值（Kraken 无资金费
  流水 → 台账按 0 记，但推送明说「不可用、未计入」，不写 funding 0.0% 冒充已知）。
- **对账 fail-closed**、**kill/开关只挡入场不挡离场**、**MR 异常不拖死主腿**
  与 Lighter 侧完全一致。
- **基金级闸门走 `hl_kill`**（2026-11-02 第五轮核对）：执行器的回撤熔断会
  `hl_set_kill(origin="dd")`、当日亏损同理，MR 读 `db.hl_kill()` ⇒ 两类熔断都挡得住。
  （Lighter 侧没有这条链路，已在那边显式接 `RiskState`。）
- **占用口径 = 价格敞口（名义 = 数量 × 标记价）**，不是保证金：5x 下保证金只有名义的
  1/5，按保证金扣会让 crypto 腿在 MR 持仓期间被放大 1.8 倍（Lighter 侧是 1x，扣满额）。
- **组合总名义闸门**（第四轮审核 P2-E）：入场定仓后、下单前读**全账户**名义
  （`user_state()` 的 `marginSummary.totalNtlPos`，含 MR 与主策略全部持仓），
  与主策略 `_entry_gates` 用**同一个上限倍数**（env `HL_MAX_TOTAL_NOTIONAL_MULT`，
  即 `ExecParams.max_total_notional_mult` 的同一来源）比较；超限/读不到都不下单。
- 开关：configuration `mr_enabled`（默认 '0'）+ `/admin/hl/mr` 写入并落
  `mr_admin` 事件；全局 `hl_kill()` 同样挡 MR 入场。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, time as _at_time, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from app import mr_signal
from app.hl import (ST_FAILED, ST_FILLED, ST_INTENT, ST_PLANNED, ST_RESTING, ST_SENT)
from app.hl.exchange import _clean_alert_text, make_cloid

logger = logging.getLogger(__name__)

#: 与 lighter 侧同值：读不懂的全局 kill 事件不当开仓绿灯（db.hl_kill 读配置与事件）。
MR_EVENT_KIND = "mr_admin"
#: 下单意图事件（成交确认丢失时的认领凭据）：写在下单**之前**，每轮重试各一条。
_ENTRY_INTENT_KIND = "mr_entry_intent"
_EXIT_INTENT_KIND = "mr_exit_intent"

#: MR 影子订单的 purpose（写进 `hl_orders`，**不是**交易所挂单）。
#: 用途只有一个：让「当日下单上限 / 近一小时失败」这两道已有的风控闸门看得见 MR。
#: 前缀 `mr_` 与主腿的 entry/stop/exit 分开：主腿的当日计数只数 purpose=='entry'，
#: 两腿各数各的；失败计数按 status 统计，所以 MR 的失败也进全局失败熔断
#: （fail-closed：只挡**新仓**，永不挡离场）。
MR_PURPOSE_ENTRY = "mr_entry"
MR_PURPOSE_EXIT = "mr_exit"
#: 风控阈值复用主策略同一组 env（**不新增旋钮**）：`main.hl_params()` 读的也是这两个键，
#: 所以面板/部署侧不存在第二处配置。
ORDERS_PER_DAY_ENV = "HL_MAX_ORDERS_PER_DAY"
FAIL_STREAK_ENV = "HL_MAX_FAIL_STREAK"
DEFAULT_MAX_ORDERS_PER_DAY = 4
DEFAULT_MAX_FAIL_STREAK = 3
#: 需人工核对的事件 kind（B5 的恢复证据路径：面板/值班能查到「为什么停下」）。
ACTION_REQUIRED_KIND = "mr_action_required"
#: 离场「结果未知」的**阻塞上限**（秒）。IOC 在几秒内就定局：过了这个窗口还查不到
#: 成交，说明取证本身不可用，而不是「单还在飞」。继续不发单 = 仓位无限期裸奔
#: （MR 不挂止损），而下行只是「多一张 reduce-only 单」——不产生新敞口、不会反手。
#: 所以超过这个窗口就把旧尝试收口成 failed，允许下一张离场（会计仍按证据/缺口标注）。
_EXIT_UNKNOWN_BLOCK_S = 30 * 60.0
#: taker 费率（HIP-3 xyz 实测 0.09%/边）——净盈亏的估算项。
#: 单边 taker 费率，用于 fee_est（每次开/平各算一次名义 × 该值）。
#: 旧值 0.0009 是 HL 的往返口径（4.5bp/边 × 2）且被当成单边用了 → 双倍高估。
#: Kraken 6 级 taker 2.75bp；掉回 Tier 1 是 5bp。真实运行时以 HlMrConfig.taker_fee_rate 为准。
TAKER_FEE_RATE = 0.000275

#: 组合总名义上限的 env 键。与 `main.hl_params()` 装配
#: `ExecParams.max_total_notional_mult` 读的**是同一个键**（同一解析口径：float，
#: 读不懂/非正数 = 不设限）—— 不新增独立旋钮，主策略与 MR 腿共用一组配置。
#: MR 腿由 `main._build_mr_leg` 构造、拿不到 ExecParams，所以在 mr.py 侧解析同一 env。
TOTAL_NOTIONAL_MULT_ENV = "HL_MAX_TOTAL_NOTIONAL_MULT"


def parse_total_notional_mult(raw: Any) -> float:
    """配置值 / 环境变量 → 组合总名义上限倍数；读不懂、NaN、<=0 一律 0（不设限）。

    与 `ExecParams.max_total_notional_mult` 的语义对齐：`0` = 关闭该闸门
    （executor `_entry_gates` 也是 `if p.max_total_notional_mult and > 0`）。
    """
    if raw is None:
        return 0.0
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    if val != val or val <= 0:          # NaN / 非正数 = 不设限
        return 0.0
    return val


#: 有成交证据的价格来源（不算估算）：`fills`=交易所成交、`avg_px`=下单回执均价、
#: `order_receipt`=离场尝试影子行里的回执均价。三者都是交易所给的价；`limit_px` /
#: `mark_estimated` / `all_mids` 是估算（关账时 `net_pnl_complete=False`）。
_EVIDENCE_PRICE_SOURCES = ("fills", "avg_px", "order_receipt")


def _price_is_evidence(source: Any) -> bool:
    """该价格来源是否算「成交证据」（决定 `estimated` / `net_pnl_complete`）。"""
    return str(source or "") in _EVIDENCE_PRICE_SOURCES


#: 推送标题：MR 腿与主腿（执行器）各用各的前缀，Telegram 里一眼分清是谁的单。
MR_TITLE = "【Kraken·MR】"

#: Kraken 的 `KrInfo.funding_supported=False`（无资金费流水，返回 []）：这种「不可用」
#: 绝不能折算成 `funding 0.0%` —— 那是在说「没花钱」，而事实是「不知道」。
FUNDING_UNAVAILABLE = "不可用（交易所未提供资金费流水，未计入净盈亏）"

#: 「用户关掉成交推送」的行用 kind 后缀标记（alert_logs 无状态列，也不加迁移）：
#: sent_successfully=1 只表示「这笔不用再管」，面板据此显示「未推送（已关闭）」，
#: 不再误导成「已送达」。
SUPPRESSED_SUFFIX = ":suppressed"


def _fmt_hold(seconds: float) -> str:
    """持仓时长（秒）→ 「1 天 2 小时 / 3 小时 5 分钟 / 12 分钟」。"""
    try:
        mins = max(0, int(float(seconds) // 60))
    except (TypeError, ValueError):
        return ""
    if mins >= 1440:
        d, h = mins // 1440, mins % 1440 // 60
        m = mins % 60
        return f"{d} 天" + (f" {h} 小时" if h else "") + (f" {m} 分钟" if m else "")
    if mins >= 60:
        h, m = mins // 60, mins % 60
        return f"{h} 小时" + (f" {m} 分钟" if m else "")
    return f"{mins} 分钟"


def _exec_time(ts_s: Any) -> str:
    """成交时间戳 → 「YYYY-MM-DD HH:MM:SS 北京时间」（显式 Asia/Shanghai）。

    用户看的是北京时间；机器/日志仍是 UTC 时间戳，这里只做展示换算（不做任何
    交易时间口径计算）。tzdata 缺失时退回固定 +8（上海无夏令时，等价）。
    """
    try:
        t = float(ts_s)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    try:
        tz = ZoneInfo("Asia/Shanghai")
    except Exception:                                         # noqa: BLE001
        tz = timezone(timedelta(hours=8))
    try:
        return datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"
    except (OSError, OverflowError, ValueError):
        return ""


def _mr_entry_text(*, coin: str, qty: float, price: float, notional: float,
                   signal_date: str, trade_id: Any, exec_ts: Any = 0.0,
                   planned_qty: Optional[float] = None,
                   note: str = "", time_label: str = "成交时间") -> str:
    """MR 入场成交回报（纯文本：Telegram 通道不带 parse_mode，不能有 Markdown）。

    `planned_qty` 与成交量不同时明写「成交 X / 计划 Y（部分成交）」—— 只看成交量
    会把部分成交读成按计划满仓。
    """
    lines = [f"{MR_TITLE}入场成交 · {coin} 做多"]
    planned = None if planned_qty is None else float(planned_qty)
    partial = (planned is not None and planned > 0
               and abs(float(qty) - planned) > max(1e-9, planned * 1e-9))
    if partial:
        lines.append(
            f"数量      成交 {float(qty):g} / 计划 {planned:g} 张"
            f"（部分成交，名义 {float(notional):,.2f} USD）")
    else:
        lines.append(f"数量      {float(qty):g} 张（名义 {float(notional):,.2f} USD）")
    lines.append(f"成交价    {float(price):,.2f} USD")
    stamp = _exec_time(exec_ts)
    if stamp:
        lines.append(f"{time_label}  {stamp}")
    lines += [
        f"信号日    {signal_date}",
        f"台账      #{trade_id}",
    ]
    if note:
        lines.append(f"说明      {note}")
    return "\n".join(lines)


def _mr_exit_text(*, coin: str, filled: float, ledger_qty: float,
                  partial_qty: float, gap_qty: float, entry_px: float,
                  exit_px: float, entry_ts: float, now_s: float,
                  net_pnl: float, fee_est: float, complete: bool,
                  funding_paid: Optional[float], price_source: str,
                  signal_date: str, exec_ts: Any = 0.0,
                  note: str = "", time_label: str = "成交时间") -> str:
    """MR 离场成交回报：数量/价格/持仓时长/费用与净利口径一次说清。

    - 成交数量与台账数量分开写：部分成交/缺口不许被读成「整笔已平」；
    - funding 不可用（Kraken 无流水）→ 明说不可用，不写 0.0%；
    - 有值时按 `_funding_paid` 既有契约（正=已付出）写「净付/净收」，不改数值；
    - `complete=False`（估算价或缺成交证据）→ 净利标「未完成」并提示核对。
    """
    qty_line = f"数量      成交 {float(filled):g} / 台账 {float(ledger_qty):g} 张"
    if float(partial_qty or 0.0) > 0:
        qty_line += f"（另含已记部分平仓 {float(partial_qty):g} 张）"
    lines = [
        f"{MR_TITLE}离场成交 · {coin} 平多",
        qty_line,
        f"价格      开仓 {float(entry_px):,.2f} → 离场 {float(exit_px):,.2f} USD",
    ]
    hold = _fmt_hold(float(now_s or 0.0) - float(entry_ts or 0.0))
    if hold:
        lines.append(f"持仓      {hold}")
    stamp = _exec_time(exec_ts or now_s)
    if stamp:
        lines.append(f"{time_label}  {stamp}")
    if float(gap_qty or 0.0) > 1e-9:
        lines.append(f"缺口      {float(gap_qty):g} 张按标记价估算，未计入成交")
    if complete:
        pnl_tail = ("估算：已扣估算手续费，"
                    + ("资金费未计入" if funding_paid is None else "含资金费"))
    else:
        pnl_tail = (f"估算，未完成：离场价来自 {price_source}，请核对真实成交价；"
                    "已扣估算手续费，"
                    + ("资金费未计入" if funding_paid is None else "含资金费"))
    if funding_paid is None:
        funding_line = FUNDING_UNAVAILABLE
    else:
        # `_funding_paid` 的既有契约是「已付出为正」（正=我们付出去的钱）。
        # Kraken 无资金费流水，无法独立证实方向 ⇒ 只按该契约写「净付/净收」，
        # 不改数值、不改台账口径。
        value = float(funding_paid)
        if value > 0:
            funding_line = f"净付 {value:,.2f} USD（按交易所流水口径，已计入净盈亏）"
        elif value < 0:
            funding_line = f"净收 {-value:,.2f} USD（按交易所流水口径，已计入净盈亏）"
        else:
            funding_line = "0.00 USD（已计入净盈亏）"
    lines.extend([
        f"净盈亏    ≈{float(net_pnl):+,.2f} USD（{pnl_tail}）",
        f"费用      ≈{float(fee_est):,.2f} USD（按 taker 费率估算）",
        f"资金费    {funding_line}",
        f"信号日    {signal_date}",
    ])
    if note:
        lines.append(f"说明      {note}")
    return "\n".join(lines)


def _mr_partial_exit_text(*, coin: str, filled: float, remaining: float,
                          entry_px: float, exit_px: float,
                          price_source: str, signal_date: str,
                          trade_id: Any, exec_ts: Any = 0.0) -> str:
    """MR 部分离场成交回报：只报了这次平掉多少、交易所还剩多少。

    **不写净盈亏**：本笔只是片段，费用/资金费与整笔结算还没定，任何数字都会
    被当成「这笔已结算」。台账保持 OPEN，下一轮继续离场。
    """
    lines = [
        f"{MR_TITLE}离场部分成交 · {coin} 平多",
        "状态      部分成交，未全部平仓（台账保持 OPEN，下轮继续离场）",
        f"本次成交  {float(filled):g} 张 · 交易所剩余 {float(remaining):g} 张",
        f"价格      开仓 {float(entry_px):,.2f} → 本次 {float(exit_px):,.2f} USD"
        f"（来源 {price_source}）",
    ]
    stamp = _exec_time(exec_ts)
    if stamp:
        lines.append(f"成交时间  {stamp}")
    lines += [
        f"信号日    {signal_date}",
        f"台账      #{trade_id}",
        "盈亏      未结算（本片段单独不计净盈亏）",
    ]
    return "\n".join(lines)


def _partial_event_key(trade_id: Any, oid: Any, cloid: Any) -> str:
    """部分离场事件键：台账 #id + 本单身份（oid 优先，其次 cloid）。

    正常部分成交与「结果未知→按成交补记」共用这套键，同一张单的重复回执
    （含重启后的重放）只会命中同一个 alert_logs kind，不会被推两次。
    """
    return f"{int(trade_id)}:{oid or cloid}"


def _mr_partial_exit_recovery_text(*, coin: str, filled: float, cum_qty: float,
                                   ledger_qty: float, price: float,
                                   price_source: str, signal_date: str,
                                   trade_id: Any, recorded_ts: Any = 0.0) -> str:
    """「结果未知 → 按成交补记」的部分离场回报（与正常部分成交同一事件口径）。

    数字全部取**已落库的片段**（qty/price/price_source），累计量 = raw.partial_exits
    之和：这是台账真正的入账口径，回执/估算值不参与展示。时间标「补记时间」——
    成交实际发生在此前，这里不能伪造成交时刻。**不写净盈亏**（整笔未结算）。
    """
    lines = [
        f"{MR_TITLE}离场部分成交（按成交补记） · {coin} 平多",
        "状态      部分成交，未全部平仓（台账保持 OPEN）",
        f"本次补记  {float(filled):g} 张 · 累计已补记 {float(cum_qty):g} / "
        f"台账 {float(ledger_qty):g} 张",
        f"价格      {float(price):,.2f} USD（来源 {price_source}）",
    ]
    stamp = _exec_time(recorded_ts)
    if stamp:
        lines.append(f"补记时间  {stamp}")
    lines += [
        f"信号日    {signal_date}",
        f"台账      #{trade_id}",
        "盈亏      未结算（本片段单独不计净盈亏）",
    ]
    return "\n".join(lines)


def _finite(value: Any, default: float = 0.0) -> float:
    """数量/金额口径的 NaN / ±Inf / None / 脏串防护 → float（不可用则 default）。

    为什么要单列：`covered_qty=NaN` 会让结算里所有 `>` / `<` 比较**都变假** ——
    既不写 `reconciliation`、又把 NaN 净利写进台账，等于静默错误关账。数量口径一律
    走这里（回执 `filled_sz` 是交易所给的，脏值只能当「不可用」）。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if math.isfinite(out) else float(default)


def _int_env(name: str, default: int) -> int:
    """env → int（脏值/缺键退回 default）。

    与 `main.hl_params()` 同一解析口径（`int(f(NAME, default))`）：读不懂时**不退回 0**
    —— 0 在两道闸门里的语义是「一律拒绝」，一个读坏的字符串不该把 MR 永久锁死。
    """
    raw = os.environ.get(name, "")
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return int(default)


def _day_start_ts(now_s: float) -> float:
    """当日 0 点的**展示时区**时间戳（默认 UTC+8）。

    与 `executor._today_start_ts` 同口径（同一个 `DISPLAY_TZ_OFFSET`）：两腿的「当日
    下单数」必须按同一个日界切，否则同一天在两条腿上会得到不同的计数。
    """
    tz = timezone(timedelta(hours=int(os.environ.get("DISPLAY_TZ_OFFSET", "8"))))
    d = datetime.fromtimestamp(float(now_s), tz)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


@dataclass(frozen=True)
class HlMrConfig:
    """HL MR 腿参数。改动 = 改这里 + 走 PR（参数面刻意最小）。"""

    #: HIP-3 的 coin 全程用 **dex 限定名**：meta?dex=xyz 返回 `xyz:XYZ100`、
    #  candleSnapshot/ordering 同名（裸名 candleSnapshot 直接 500，2026-11-01 部署
    #  冒烟实测）。allMids 无此键 → _mark 自动走 K 线收盘回退，无需处理。
    # 默认值保持 HL 形状（测试/回放用）；运行期由 main._build_mr_leg 显式传
    # coin="QQQ" / dex="kr"（Kraken 的 PF_QQQXUSD）。
    coin: str = "xyz:XYZ100"
    dex: str = "xyz"
    exposure: float = 0.5               # 名义 = 权益 × 0.5
    min_notional: float = 10.0          # 仅作小单提醒（Kraken 最小 1 张合约，约 $7.6~$660 视标的）
    slippage_bps: int = 15              # IOC 限价的保护带宽（单边）
    candle_hours: int = 500             # 1h×500 ≈ 21 天会话
    #: funding 基线（%名义/小时）。实测 2026-01-22~11-01 中位 0.0004%/h（3.5%/年）。
    funding_baseline_pct_per_h: float = 0.0004
    #: 自动停用门槛：实际 funding 超基线 `funding_disable_multiple` 倍，**且**已付金额
    #: 超过 `funding_disable_min_pct`（%名义）。2026-11-02 第四轮复核：旧值 2 倍落在
    #: 正常分布中间（Lighter 9 个月 12 笔回合有 7 笔触发）；10 倍 + 0.10% 只拦尖峰。
    funding_disable_multiple: float = 10.0
    funding_disable_min_pct: float = 0.10
    #: 下单意图的认领窗口（秒）：成交确认因读错误/进程重启丢失时，下一轮凭意图
    #: 把交易所真实持仓补记进台账，避免「有仓无台账 → recon_err 要人工改库」。
    adopt_window_s: float = 6 * 3600.0
    #: 即时止损触发器：成交滑点（|avg_px−ref|/ref）超过该 bp → 自动停用 MR 腿。
    #: 注意：IOC 限价 = ref ± `slippage_bps`(15bp)，成交价不可能越过限价 ⇒ >20bp
    #: 这一支在正常路径上不可达（保留作防御性断言）。真正有信息量的是
    #: 「吃掉 ≥`slippage_warn_bps` 带宽」——那条会告警（不自动停用）。
    max_slippage_bps: float = 20.0
    slippage_warn_bps: float = 10.0
    #: 显式杠杆/保证金模式：Lighter 侧有 `ensure_isolated(1x)`，HL 侧原来完全没设 ——
    #: 若该 dex 落成 30x 逐仓，−3.3% 就强平（MR 无止损、历史最差 −17.8%）。
    #: 入场前按这里设一次（幂等）；**设置失败即拒绝入场**（fail-closed）。
    leverage: int = 5
    cross_margin: bool = True
    #: 成交确认：IOC 回执 `filled` 之外，还要看到 xyz dex 的持仓真的出现/消失才记账
    #: （`resting`/`dry`/部分成交都不算成交；2026-11-02 复核 P1）。
    fill_confirm_s: float = 12.0
    fill_poll_s: float = 1.0
    #: 单边 taker 费率（fee_est 用）。**加在末尾**：dataclass 位置参数构造不能错位。
    #: Kraken 6 级 2.75bp；改账号档位时同步 KR_TAKER_FEE。
    taker_fee_rate: float = TAKER_FEE_RATE
    #: 组合总名义上限（× 全账户权益），与主策略 `ExecParams.max_total_notional_mult`
    #: 同源同口径。**None = 读 env `HL_MAX_TOTAL_NOTIONAL_MULT`**（生产走这条，与
    #: main.hl_params 同一个键）；显式给 0 = 关闭闸门（不设限）。
    #: 也必须加在**末尾**：20+ 个 MR 测试用位置参数构造 HlMrConfig。
    max_total_notional_mult: Optional[float] = None


def _round_px(px: float, sz_decimals: int) -> float:
    """**回退**用的 HL 价格精度：≤5 位有效数字且小数位 ≤ 6−szDecimals。

    Kraken 下不应走到这里：`HlMrLeg._venue_px()` 优先调 `info.round_price()`
    （tickSize 整数倍）。保留此函数给无 tickSize 的 venue / 测试回放。
    """
    out = float(f"{px:.5g}")
    return round(out, max(6 - int(sz_decimals), 0))


def _floor_sz(sz: float, sz_decimals: int) -> float:
    step = 10 ** int(sz_decimals)
    return int(sz * step) / step


class HlMrLeg:
    """HL 侧 MR 腿。`tick()` 每 5 分钟由调度器调一次；其余一律不碰。"""

    def __init__(self, *, db: Any, info: Any, exchange: Any,
                 address_fn: Callable[[], str], config: Optional[HlMrConfig] = None,
                 notifier: Any = None, clock: Callable[[], float] = time.time):
        self.db = db
        self.info = info
        self.exchange = exchange
        self.address_fn = address_fn
        self.config = config or HlMrConfig()
        self.notifier = notifier
        self.clock = clock
        self._recon_alerted_at = 0.0
        #: 订单类告警去重（key → 上次发送时刻）；入场窗口内每 5 分钟重试不刷屏。
        self._order_alerted_at: Dict[str, float] = {}

    # ------------------------------------------------------------------ 读

    def _enabled(self) -> bool:
        """`mr_enabled` 布尔口径：**统一 strip + lower** 后再判。

        面板写的是 "1"/"0"，但手工改库/旧脚本会写 " 1 "、"TRUE"、"On"、"yes"。
        旧实现只做 `in ("1","true","on")` 精确匹配 → 带空格或大写会被静默当成**停用**
        （MR 不再进场，也不会有任何告警，看起来像「策略没信号」）。
        反过来「读不懂」仍然按停用处理（fail-closed，与 kill 的语义一致）。
        """
        try:
            raw = self.db.get_config_str("mr_enabled", "0")
        except Exception:                                     # noqa: BLE001
            return False
        return str(raw if raw is not None else "").strip().lower() in (
            "1", "true", "on", "yes")

    def _global_kill(self) -> bool:
        try:
            return bool(self.db.hl_kill())
        except Exception:                                     # noqa: BLE001
            return True                                       # 读不懂当 kill

    def _fills_on(self) -> bool:
        """面板「成交回报推送」(fill_push)：与 `main.push_on` 同一口径。

        每条成交回报现读 DB（而不是装配时快照）：MR 腿是长生命周期实例，面板改开关
        不会重建它；读失败按「开」处理（宁可多推一条，不静默吞掉成交）。
        """
        try:
            raw = str(self.db.get_config_str("fill_push", "1") or "1").strip().lower()
        except Exception:                                     # noqa: BLE001
            return True
        return raw not in ("0", "false", "no", "off")

    def _alert(self, text: str, *, kind: str = "mr_alert",
               key: Any = None, fill: bool = False,
               dedupe: bool = False) -> bool:
        """MR 告警：推送 + 落 `alert_logs`（`main._retry_failed_alerts` 的唯一依据）。

        - `fill=True`：成交回报，受面板 fill_push 控制。关掉 → 只落库并记成「已了结」
          （sent_successfully=1），不进补推队列 —— 那是用户意图，不是发送失败。
        - `key`：事件标识，拼进 kind 才是 alert_logs 里的事件键（`bar_ms=NULL` 时
          UNIQUE 不生效，不同事件全靠 kind 区分，绝不能共用同一个 kind）。未显式给
          时按「正文归一化哈希 + 小时桶」自动派生，避免所有无名告警挤进同一个事件。
        - `dedupe=True`：**已送达**的事件不再发；没送达（sent=0）照常重试 ——
          「事件去重」只覆盖成功送达，不能把失败推送一起吞掉。
        """
        body = str(text)
        # 旧文案自带「MR: 」前缀（标题化后会出现「【Kraken·MR】MR ...」），统一剥掉，
        # 由标题负责「这是谁的单」。
        if body.startswith(MR_TITLE):
            body = body[len(MR_TITLE):]
        elif body.startswith("MR:") or body.startswith("MR："):
            body = body[3:].lstrip()
        elif body.startswith("MR "):
            body = body[3:]
        # Telegram 通道不带 parse_mode：`**粗体**` 会原样显示成星号，推送前统一去掉。
        clean = _clean_alert_text(MR_TITLE + body, limit=800).replace("**", "")
        logger.warning("%s", clean)
        k = "" if key is None else str(key)
        if not k:
            # 每条告警都要有自己的事件键：`(symbol, kind, NULL)` 若共用，补推只会发
            # 其中一条，成功后还会把同组别的消息一起标「已送达」（等于静默丢消息）。
            # 正文归一化 + 小时桶 = 不同事件不同键、同一事件（含重复失败）同一键。
            norm = re.sub(r"\s+", " ", body).strip()
            digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
            bucket = int(float(self.clock()) // 3600.0)
            k = f"auto:{digest}:{bucket}"
        event_kind = f"{kind}:{k}"
        suppressed_kind = event_kind + SUPPRESSED_SUFFIX
        if dedupe:
            # 先判「已送达」再落事件行：重复进入同一事件时别把 hl_events 也刷一遍。
            try:
                if (self.db.was_alerted(self.config.coin, None, event_kind)
                        or self.db.was_alerted(self.config.coin, None, suppressed_kind)):
                    return True
            except Exception:                                 # noqa: BLE001
                logger.warning("mr: 读 alert_logs 失败，按未推送处理", exc_info=True)
        try:
            self.db.hl_log_event("mr", clean)
        except Exception:                                     # noqa: BLE001
            pass
        if fill and not self._fills_on():
            try:
                # sent=1 = 这笔不用再管；kind 后缀让面板显示「未推送（已关闭）」
                self.db.log_alert(self.config.coin, None, suppressed_kind, clean, True)
            except Exception:                                 # noqa: BLE001
                logger.error("MR 成交回报落库失败（审计缺失）", exc_info=True)
            return True
        ok = False
        if self.notifier is not None:
            try:
                ok = bool(self.notifier.send_message(clean))
            except Exception:                                 # noqa: BLE001
                logger.exception("MR 告警通知失败")
                ok = False
        try:
            self.db.log_alert(self.config.coin, None, event_kind, clean, ok)
            if ok and k:
                # 同事件之前的失败行一起了结，否则补推会把同一条再发一遍
                self.db.mark_alert_kind_sent(self.config.coin, event_kind)
        except Exception:                                     # noqa: BLE001
            logger.error("MR 告警落库失败（补推将不可用）", exc_info=True)
        return ok

    def _alert_throttled(self, key: str, text: str,
                         interval_s: float = 30 * 60.0) -> None:
        """同一类订单问题在 `interval_s` 内只发一次（入场窗口每轮重试不刷屏）。"""
        now_s = self.clock()
        last = float(self._order_alerted_at.get(key, 0.0) or 0.0)
        if last and now_s - last < interval_s:
            logger.warning("mr: %s（同类告警已去重）", text)
            return
        self._order_alerted_at[key] = now_s
        # 事件键带节流窗口桶：同一类问题隔几小时再发生是**新事件**，不能被上一轮
        # 的事件键吞掉（补推分组/「已送达」都按 kind 计）。
        bucket = int(now_s // max(float(interval_s), 60.0))
        self._alert(text, key=f"throttle:{key}:{bucket}")

    def _await_position(self, address: str, *, want: bool,
                        confirm_s: Optional[float] = None
                        ) -> Tuple[Optional[dict], bool]:
        """轮询 xyz dex 持仓，直到「出现」（want=True）或「消失」（want=False）。

        返回 (最后一次读到的持仓, read_ok)。`read_ok=False` = 整轮都没读到真相 ——
        离场方必须按「未知」处理，绝不当成「仓已消失」写出假 CLOSED。
        成交只认持仓，不认回执 —— 交易所的 `status=filled` 也可能只成交一部分。
        """
        cfg = self.config
        budget = float(cfg.fill_confirm_s if confirm_s is None else confirm_s)
        poll = max(float(cfg.fill_poll_s), 0.0)
        attempts = 1 if poll <= 0 or budget <= 0 else int(budget / poll) + 1
        pos = None
        read_ok = False
        started = time.monotonic()
        for i in range(max(attempts, 1)):
            try:
                pos = (self._read_account(address) or {}).get("mr_pos")
            except Exception as exc:                              # noqa: BLE001
                # 单次读失败（503/限流）不打断确认：否则已成交的仓没有台账 → recon_err。
                logger.warning("mr: 成交确认读账户失败（继续轮询）：%s: %s",
                               type(exc).__name__, exc)
            else:
                read_ok = True
                if (pos is not None) is bool(want):
                    return pos, True
            # 墙钟上限（2026-11-02 第五轮）：HL 一次失败读含 3 次重试 + 1/3/9s 退避，
            # 实测 ≈26s；固定 13 次会变成 5 分钟以上的阻塞，而 MR tick 持 `_tick_lock`，
            # 会把 :03/:33 主 tick（对账/离场/补挂止损）一起顶住。
            elapsed = time.monotonic() - started
            if elapsed >= budget:
                logger.warning("mr: 成交确认超时（%.1fs ≥ %.1fs，%d 次读），按当前真相返回",
                               elapsed, budget, i + 1)
                break
            if i + 1 < attempts:
                time.sleep(max(min(poll, budget - elapsed), 0.0))
        return pos, read_ok

    def _auto_disable(self, reason: str) -> None:
        """即时止损触发器：写 mr_enabled=0（只挡新入场）+ 审计事件 + 告警。"""
        try:
            self.db.set_config("mr_enabled", "0")
            self.db.hl_log_event(MR_EVENT_KIND, f"MR 腿自动停用：{reason}",
                                 data={"enabled": False, "by": "auto", "reason": reason})
        except Exception:                                     # noqa: BLE001
            logger.exception("MR 自动停用写库失败")
        self._alert(f"MR 腿自动停用：{reason}")

    def _slippage_guard(self, result: dict, ref: float, *, side: str) -> None:
        """成交价 vs 参考价的滑点：超 `max_slippage_bps` 自动停用；吃掉大半带宽则告警。"""
        try:
            avg = result.get("avg_px")
            if not avg or not ref or ref <= 0:
                return
            slip_bps = abs(float(avg) - ref) / ref * 10000.0
            if slip_bps > self.config.max_slippage_bps:
                self._auto_disable(f"{side}滑点 {slip_bps:.1f}bp > "
                                   f"{self.config.max_slippage_bps:.0f}bp"
                                   f"（ref={ref:.2f} avg={float(avg):.2f}）")
            elif slip_bps >= self.config.slippage_warn_bps:
                self._alert_throttled(
                    f"fill_quality_{side}",
                    f"MR {side}成交吃掉 {slip_bps:.1f}bp / "
                    f"{self.config.slippage_bps:.0f}bp 滑点带宽"
                    f"（ref={ref:.2f} avg={float(avg):.2f}）——盘口偏薄，观察后续成交")
        except Exception:                                     # noqa: BLE001
            logger.exception("MR 滑点检查失败")

    def _funding_guard(self, funding_pct: Optional[float], entry_ts: float,
                       now_s: float) -> None:
        """实际付出 funding 超基线 N 倍且绝对额 >0.10% → 自动停用（触发器）。"""
        if funding_pct is None or funding_pct <= 0:
            return
        hours = max(now_s - entry_ts, 0.0) / 3600.0
        expected = self.config.funding_baseline_pct_per_h * hours
        if (expected > 0
                and funding_pct > self.config.funding_disable_multiple * expected
                and funding_pct > self.config.funding_disable_min_pct):
            self._auto_disable(
                f"funding {funding_pct:.3f}% 超基线 "
                f"{self.config.funding_disable_multiple:g} 倍"
                f"（预期 {expected:.3f}%，持仓 {hours:.1f}h）")

    def _ensure_leverage(self, status: Dict[str, Any]) -> bool:
        """入场前把该 coin 的杠杆/保证金模式设成配置值（幂等）。

        失败 → **拒绝本轮入场**（fail-closed）：MR 不挂止损，若该 dex 还是 30x 逐仓，
        −3.3% 就强平；「按现有设置继续」正是这条保护要消灭的尾部风险。
        """
        cfg = self.config
        try:
            res = self.exchange.update_leverage(
                cfg.coin, int(cfg.leverage), cross=bool(cfg.cross_margin))
            if not res.get("ok"):
                raise RuntimeError(str(res.get("error") or res.get("status") or "unknown"))
            status["leverage"] = f"{cfg.leverage}x/{'cross' if cfg.cross_margin else 'isolated'}"
            return True
        except Exception as exc:                              # noqa: BLE001
            status["leverage_error"] = f"{type(exc).__name__}: {exc}"
            self._alert_throttled(
                "leverage", f"MR 杠杆设置失败，本轮拒绝入场：{type(exc).__name__}: {exc}")
            return False

    # ------------------------------------------------------------------ 入场

    # ------------------------------------------------------------------ 账户读数

    def _read_account(self, address: str) -> Dict[str, Any]:
        cfg = self.config
        # 2026-11-02 第四轮复核：`spotClearinghouseState` **没有** marginSummary，
        # 原来读 `["marginSummary"]["accountValue"]` 恒 0（no_equity 静默不进场）。
        # 统一账户的正确口径 = 现货 USDC 总额（已含各 dex 浮盈亏），由 info 统一实现。
        equity_total = float(self.info.account_equity(address) or 0.0)
        dex_state = self.info.user_state_dex(address, cfg.dex) or {}
        dms = dex_state.get("marginSummary") or {}
        # 【B9】`marginUsed` 是**全账户**口径（这个 venue 没有按 dex/按腿拆分的保证金读数），
        # 不能直接当「MR 腿占用」去扣：MR 空仓时它包含 crypto 腿的保证金，扣一次等于
        # 按「MR 也在用」把 crypto 腿的定仓基数压低。所以它只做展示（mr_margin_reserved），
        # 真正的占用由下面按 MR 仓自己的敞口算；MR 空仓 = 占用 0。
        mr_margin_used = float(dms.get("marginUsed") or 0.0)
        mr_pos = None
        bare = cfg.coin.split(":")[-1]
        for item in dex_state.get("assetPositions") or []:
            p = item.get("position") or {}
            # HIP-3 的 coin 命名两可（`xyz:XYZ100` 或裸 `XYZ100`）：meta 与 allMids 实测带前缀，
            # 但持仓行的命名没有仓时无法实测 —— 两种都认，避免「有仓却看不见」（第五轮）。
            if (p.get("coin") or "") not in (cfg.coin, bare):
                continue
            szi = float(p.get("szi") or 0.0)
            if szi == 0:
                continue
            lev = p.get("leverage") or {}
            mr_pos = {
                "szi": szi, "side": 1 if szi > 0 else -1,
                "entry_px": float(p.get("entryPx") or 0.0),
                "position_value": float(p.get("positionValue") or 0.0),
                "margin_used": float(lev.get("marginUsed") or 0.0),
                "unrealized_pnl": float(p.get("unrealizedPnl") or 0.0),
            }
            break
        # 占用口径 = **价格敞口（数量 × 标记价）**，不是保证金（2026-11-02 第五轮）。
        # 按保证金扣时，同一个 MR 敞口在 5x 下只扣 1/5 —— 扣多少取决于杠杆设置，
        # 而 Lighter 侧是 1x（扣满额）⇒ 同样 exposure，HL 的 crypto 腿会被放大 1.8 倍。
        # 保证金值单独保留在 `mr_margin_reserved` 里做展示，不参与定仓。
        # 【B9】MR **空仓**时占用必须是 0：旧代码 `occupied = mr_margin_used` 把全账户
        # marginUsed（含 crypto 腿自己占的保证金）当成 MR 占用 → crypto 腿定仓基数被
        # 无端扣掉一截，MR 停用期间也一样扣（而且随 crypto 仓位变大而变大，形成负反馈）。
        occupied = 0.0
        if mr_pos is not None:
            value = mr_pos["position_value"]
            if not value:
                value = abs(mr_pos["szi"]) * mr_pos["entry_px"]
            occupied = max(abs(value), 0.0)
        return {"equity_total": equity_total, "mr_margin_used": occupied,
                # 展示用：**全账户**保证金（无 dex 拆分），不要拿它定仓/扣减。
                "mr_margin_reserved": mr_margin_used,
                "mr_margin_raw": mr_margin_used, "mr_pos": mr_pos}

    # ------------------------------------------------------------------ 组合名义闸门

    def total_notional_mult(self) -> float:
        """组合总名义上限倍数：config 显式给值优先，否则读主策略同一个 env 键。

        `HlMrConfig.max_total_notional_mult=None`（默认）⇒ env `HL_MAX_TOTAL_NOTIONAL_MULT`。
        0（或读不懂）= 不设限：老配置/测试夹具没有这个能力时行为不变。
        """
        raw = getattr(self.config, "max_total_notional_mult", None)
        if raw is None:
            raw = os.environ.get(TOTAL_NOTIONAL_MULT_ENV, "")
        return parse_total_notional_mult(raw)

    def _total_notional_gate(self, equity_total: float, planned_notional: float
                             ) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        """组合总名义闸门（第四轮审核 P2-E）→ `(total, cap, reason)`。

        与主策略 `executor._entry_gates` 的 `HL_MAX_TOTAL_NOTIONAL_MULT` 闸门同一口径：
        全账户**现有名义 + 本笔计划名义 > 权益 × 上限** 即拒。`totalNtlPos` 来自
        `info.user_state()`，含 MR 与主策略全部持仓（KrInfo 已按 position_value 求和）。

        `reason is None` = 放行。`cap is None` = 未配置上限（<=0），**不做额外账户读数**
        —— 老配置/测试夹具没有 `user_state` 也不受影响。

        读不到 totalNtlPos / 权益不足以为基数时返回 reason（fail-closed：调用方不得下单）。
        """
        cap_mult = self.total_notional_mult()
        if cap_mult <= 0:
            return None, None, None
        try:
            eq = float(equity_total)
        except (TypeError, ValueError):
            eq = 0.0
        if not (eq > 0):
            return None, None, (f"权益不可用（equity={equity_total!r}），"
                                f"无法校验总名义上限，本轮不下单")
        cap = eq * cap_mult
        try:
            state = self.info.user_state(self.address_fn() or "")
        except Exception as exc:                              # noqa: BLE001
            # 读不到 ≠ 没有仓位：放行等于回到「MR 无视组合上限」的老 bug。
            return None, cap, (f"读不到全账户名义（{type(exc).__name__}: {exc}），"
                               f"无法校验总名义上限 {cap:.0f}U，本轮不下单")
        ms = (state or {}).get("marginSummary") if isinstance(state, Mapping) else None
        raw = ms.get("totalNtlPos") if isinstance(ms, Mapping) else None
        try:
            total = float(raw)
        except (TypeError, ValueError):
            return None, cap, (f"全账户名义 totalNtlPos 不可用（{raw!r}），"
                               f"无法校验总名义上限 {cap:.0f}U，本轮不下单")
        if not (total >= 0) or total != total:                # 负数 / NaN
            return None, cap, (f"全账户名义 totalNtlPos 非法（{raw!r}），"
                               f"无法校验总名义上限 {cap:.0f}U，本轮不下单")
        if total + planned_notional > cap:
            return total, cap, (f"组合名义将达 {total + planned_notional:.0f}U"
                                f"（现有 {total:.0f}U + 计划 {planned_notional:.0f}U）"
                                f"超过上限 {cap:.0f}U（{cap_mult:g}x × 权益 {eq:.0f}U）")
        return total, cap, None

    # ------------------------------------------------------------------ 认领

    # ------------------------------------------------ 风控覆盖（hl_orders 影子行）

    def _shadow_begin(self, purpose: str, *, side: int, sz: float, px: Optional[float],
                      reduce_only: bool, cloid: str, signal_date: str,
                      now_ms: int) -> Optional[Dict[str, Any]]:
        """写一条 `hl_orders` 影子意图（purpose=`mr_entry`/`mr_exit`）→ 定位键。

        为什么要有它（第七轮 F7）：MR 的入场/离场此前只写 `hl_events`，于是执行器的
        「当日下单上限」与「近一小时失败」两道闸门**完全看不到 MR** —— MR 若被持续
        拒单（精度/价格非法），只会每 5 分钟重试 + 30 分钟节流告警，没有全局刹车。
        影子行不是交易所挂单，只服务计数与审计。

        `bar_ms` 取**当前 tick 分钟**（与 cloid 同源）：同一轮重放命中同一行（表上有
        UNIQUE(symbol, bar_ms, purpose)），跨轮重试或崩溃后重跑各落一行 —— 失败计数
        才有意义。写库失败**不阻断交易**：影子行只负责可见性，不能让审计写失败变成
        「不平仓」。
        """
        rec = getattr(self.db, "hl_record_intent", None)
        if not callable(rec):
            return None
        symbol = str(self.config.coin or "MR")
        bar_ms = _attempt_ms(now_ms) * 60_000
        try:
            rec(symbol, symbol, bar_ms, purpose, int(side), float(sz),
                px=(float(px) if px else None), reduce_only=bool(reduce_only),
                cloid=cloid, status=ST_INTENT)
            upd = getattr(self.db, "hl_update_order", None)
            if callable(upd):
                # signal_date 落到 raw：崩溃后要按它把「结果未知的尝试」对回台账。
                # `ts` 用**本次 tick 的时刻**（不是 DB 的 now()）：判断「这张 IOC 还在
                # 不在飞」必须与调用方同一个时钟，否则宿主时钟跳变/测试夹具的假时刻
                # 会让 30 分钟的阻塞窗口算错（→ 提前重发或永久阻塞）。
                upd(symbol, bar_ms, purpose,
                    raw=json.dumps({"signal_date": str(signal_date), "cloid": cloid,
                                    "ts": round(float(now_ms) / 1000.0, 3)},
                                   ensure_ascii=False, default=str))
        except Exception:                                     # noqa: BLE001
            logger.warning("mr: 写影子订单行失败（不阻断本轮）", exc_info=True)
            return None
        return {"symbol": symbol, "bar_ms": bar_ms, "purpose": purpose}

    def _shadow_finish(self, key: Optional[Dict[str, Any]], *, status: str,
                       oid: Any = None, error: Any = None,
                       filled_sz: Optional[float] = None,
                       avg_px: Optional[float] = None) -> None:
        """影子行落终态（filled / failed / sent=结果未知）。失败只记日志。"""
        if not key:
            return
        upd = getattr(self.db, "hl_update_order", None)
        if not callable(upd):
            return
        kw: Dict[str, Any] = {"status": str(status)}
        if oid:
            kw["oid"] = str(oid)              # Kraken 单号是 UUID：一律 str，不 int()
        if error is not None:
            kw["error"] = str(error)[:300]
        if filled_sz is not None:
            kw["filled_sz"] = float(filled_sz)
        if avg_px is not None:
            kw["avg_px"] = float(avg_px)
        try:
            upd(key["symbol"], int(key["bar_ms"]), key["purpose"], **kw)
        except Exception:                                     # noqa: BLE001
            logger.warning("mr: 更新影子订单行失败（忽略）", exc_info=True)

    def _order_gate(self, now_s: float) -> Optional[str]:
        """MR 入场前的两道**既有**风控闸门 → None=放行，否则拒绝原因。

        阈值与 `executor._entry_gates` 同源（env `HL_MAX_ORDERS_PER_DAY` /
        `HL_MAX_FAIL_STREAK`，经 `main.hl_params()` 装配到主腿），**不新增旋钮**。
        计数只数**本腿**的影子行（主腿的当日计数只认 purpose=='entry'，两腿各数各的）。
        失败计数把 `mr_exit` 也算进来：离场连续被拒同样是「这个腿现在不正常」。
        **只挡入场**：离场永不因为这两道闸门被拦（否则亏损时反而平不掉）。
        """
        rows_fn = getattr(self.db, "hl_recent_orders", None)
        if not callable(rows_fn):
            return None
        try:
            rows = list(rows_fn(200) or [])
        except Exception as exc:                              # noqa: BLE001
            # 读不到台账 = 无法判断今天已经下了几单 → fail-closed，只挡入场
            return f"读不到下单台账（{type(exc).__name__}），本轮不入场"
        max_day = _int_env(ORDERS_PER_DAY_ENV, DEFAULT_MAX_ORDERS_PER_DAY)
        max_fail = _int_env(FAIL_STREAK_ENV, DEFAULT_MAX_FAIL_STREAK)
        day0 = _day_start_ts(now_s)
        entries = 0
        fails = 0
        for r in rows:
            try:
                purpose = str(r["purpose"] or "")
                status = str(r["status"] or "")
                created = float(r["created_at"] or 0.0)
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if (purpose == MR_PURPOSE_ENTRY and created >= day0
                    and status in (ST_SENT, ST_RESTING, ST_FILLED)):
                entries += 1
            if (purpose in (MR_PURPOSE_ENTRY, MR_PURPOSE_EXIT)
                    and status == ST_FAILED and now_s - created < 3600.0):
                fails += 1
        if entries >= max_day:
            return f"MR 当日下单已达 {entries} 次（上限 {max_day}）"
        if fails >= max_fail:
            return f"MR 近一小时失败 {fails} 次（上限 {max_fail}）"
        return None

    def _mr_attempts(self, purpose: str, signal_date: str,
                     statuses: Tuple[str, ...]) -> List[Tuple[Any, Dict[str, Any]]]:
        """本腿某 signal_date 上仍停在 `statuses` 里的影子行 → [(row, raw)]。

        查询窗口（跨重启的「不可重复单」全靠这里）：`statuses` 只含 intent/sent 时
        走 `hl_stuck_orders`（**无 LIMIT**，多久以前的未知尝试都看得见）。不能用
        `hl_recent_orders(200)`：主腿每根 bar 都在写行，十几个小时后 MR 的未知尝试
        就被挤出这 200 行 → 认不出来 → 换 cloid 重发就是第二笔仓。
        """
        rows: List[Any] = []
        if set(statuses) <= {ST_INTENT, ST_SENT}:
            fn = getattr(self.db, "hl_stuck_orders", None)
            if callable(fn):
                try:
                    # older_than_s=-1 → 连「这一秒刚写的」也算；默认 180s 会把本 tick
                    # 刚落的 intent 排除掉（那正是最需要取证的一条）。
                    rows = list(fn(-1.0) or [])
                except Exception:                             # noqa: BLE001
                    rows = []
        if not rows:
            rows_fn = getattr(self.db, "hl_recent_orders", None)
            if not callable(rows_fn):
                return []
            try:
                rows = list(rows_fn(500) or [])
            except Exception:                                 # noqa: BLE001
                return []
        out: List[Tuple[Any, Dict[str, Any]]] = []
        for r in rows:
            try:
                if str(r["purpose"] or "") != purpose:
                    continue
                if str(r["status"] or "") not in statuses:
                    continue
            except (KeyError, IndexError, TypeError):
                continue
            data = _raw_dict(r)          # hl_orders.raw 与 mr_trades.raw 同为 JSON 文本
            if str(data.get("signal_date") or "") != str(signal_date):
                continue
            out.append((r, data))
        return out

    def _key_of(self, row: Any) -> Dict[str, Any]:
        return {"symbol": str(row["symbol"]), "bar_ms": int(row["bar_ms"]),
                "purpose": str(row["purpose"])}

    # ------------------------------------------------------------ 意图与成交证据

    def _intent_event(self, kind: str, now_s: float, *,
                      signal_date: Optional[str] = None,
                      max_age_s: Optional[float] = None,
                      limit: int = 60) -> Optional[Tuple[float, Dict[str, Any]]]:
        """最近的意图事件 → (ts, payload)；可限定归属与年龄。

        - `signal_date=None`：任意归属（入场侧**必须**从意图里取 signal_date，所以
          超窗后无法再定归属 → 入场侧的年龄限制有语义）；
        - `signal_date` 给定：只要该 signal_date 的最新一条（离场认领用）；
        - `max_age_s=None`：**不限年龄**。离场侧的归属由台账那一行给出，意图只用来
          证明「这笔离场是本系统发起的」，这个证明没有保质期（B5/F2）。

        两个旧方法（`_latest_intent` / `_intent_for_signal`）是同一段扫描的两份拷贝，
        合并成一处：少一份重复逻辑，也避免两边 limit/过滤条件漂移。
        """
        try:
            rows = self.db.hl_recent_events(int(limit), kind=kind) or []
        except Exception:                                     # noqa: BLE001
            return None
        want = None if signal_date is None else str(signal_date)
        best: Optional[Tuple[float, Dict[str, Any]]] = None
        for row in rows:                       # id DESC ≈ 时间倒序
            try:
                ts = float(row["created_at"] or 0.0)
                payload = _event_data(row["data"])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if ts <= 0 or ts > now_s + 60.0:            # 未来时间戳 = 时钟异常
                continue
            if max_age_s is not None and now_s - ts > float(max_age_s):
                continue
            sd = str(payload.get("signal_date") or "")
            if not sd:                                  # 没有归属的意图无法用于认领
                continue
            if want is not None and sd != want:
                continue
            if best is None or ts > best[0]:
                best = (ts, payload)
        return best

    def _fills(self, *, since_s: float, until_s: Optional[float] = None,
               cloid: Optional[str] = None,
               close_side: Optional[bool] = None) -> Tuple[Optional[List[dict]], str]:
        """交易所成交（只读，**不写库**）→ (该标的相关成交, 说明)。

        `(None, reason)` = **读不到**：调用方必须按「未知」处理，绝不能当「没有成交」
        （否则一次接口抖动就会被写成「已确认无成交」）。
        `close_side=True/False` 只挑平仓腿/开仓腿（dir 前缀或 reduce_only 判）。
        """
        cfg = self.config
        start_ms = int(max(float(since_s or 0.0), 0.0) * 1000)
        end_ms = int(float(until_s) * 1000) if until_s else None
        address = self.address_fn() or ""
        fn = getattr(self.info, "user_fills_by_time", None)
        try:
            if callable(fn):
                rows = list(fn(address, start_ms, end_ms) or [])
            else:
                fn2 = getattr(self.info, "user_fills", None)
                if not callable(fn2):
                    return None, "venue 不提供成交查询"
                rows = list(fn2(address) or [])
        except Exception as exc:                              # noqa: BLE001
            logger.warning("mr: 读成交失败（按未知处理）：%s: %s", type(exc).__name__, exc)
            return None, f"读成交失败 {type(exc).__name__}"
        bare = str(cfg.coin).split(":")[-1]
        out: List[dict] = []
        for f in rows:
            if not isinstance(f, Mapping):
                continue
            coin = str(f.get("coin") or "")
            if coin and coin not in (cfg.coin, bare):
                continue
            try:
                t = float(f.get("time") or 0.0)
            except (TypeError, ValueError):
                t = 0.0
            if t and t < start_ms:            # 有些 venue 只在内存里过滤，双保险
                continue
            if end_ms and t and t > end_ms:
                continue
            if cloid and str(f.get("cloid") or "") != str(cloid):
                continue
            if close_side is not None:
                direction = str(f.get("dir") or "").lower()
                is_close = direction.startswith("close") or bool(
                    int(f.get("reduce_only") or 0))
                if bool(is_close) is not bool(close_side):
                    continue
            out.append(dict(f))
        out.sort(key=lambda x: float(x.get("time") or 0.0))
        return out, "ok"

    # ------------------------------------------------------------ 统一决算

    def _settlement(self, open_row: Any, *, covered_qty: float, covered_px: float,
                    gap_qty: float = 0.0, gap_px: float = 0.0,
                    price_source: str, estimated: bool,
                    funding_paid: float = 0.0,
                    extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """统一决算：正常收尾 / 补账 / 到期清仓共用一套会计口径。

        第七轮 B6：旧实现两条关账路径各算各的 —— `_maybe_exit` 只按**交易所剩余量**
        结算、从不与「台账 qty − Σ片段」核对（片段丢失时盈亏静默少记、无 reconciliation），
        而 `_adopt_exit` 反而有核对。现在统一：

        - `covered_qty/covered_px`：有**成交证据**的那一部分（真实成交优先，其次回执
          avg_px/限价，最后才是参考价估算）；
        - `gap_qty/gap_px`：台账有量但**拿不到成交证据**的差额 —— 按参考价估算入账，
          但 `net_pnl_complete=False` + `reconciliation`，绝不冒充「确定的已实现盈亏」；
        - `raw` 基于原行**合并**写回（保留 `partial_exits`/`cloid`/`ref_price` 等历史字段）。
        """
        rate = float(getattr(self.config, "taker_fee_rate", TAKER_FEE_RATE))
        qty = float(open_row["qty"] or 0.0)
        entry_price = float(open_row["entry_price"] or 0.0)
        notional = float(open_row["notional"] or 0.0)
        partials = _partial_exits(open_row)
        partial_qty = sum(abs(float(p.get("qty") or 0.0)) for p in partials)
        partial_notional = sum(abs(float(p.get("qty") or 0.0))
                               * float(p.get("price") or 0.0) for p in partials)
        partial_pnl = sum((float(p.get("price") or 0.0) - entry_price)
                          * float(p.get("qty") or 0.0) for p in partials)
        expected = qty - partial_qty
        covered = max(float(covered_qty or 0.0), 0.0)
        gap = float(gap_qty or 0.0)
        tol = max(1e-9, abs(qty) * 1e-9)
        # 「净利是否确定」= 数量没有缺口 **且** 价格有成交证据。只有前者时，
        # 一笔按限价/参考价估算的关账会被读成「确定的已实现盈亏」（第七轮 B6：
        # 缺口不能假确定，估算价同理）。
        partial_prices_known = all(_price_is_evidence(str(p.get("price_source") or ""))
                                   for p in partials)
        complete = abs(gap) <= tol and not bool(estimated) and partial_prices_known
        recon = None
        if (expected < -tol or abs(gap) > tol
                or covered + gap < expected - tol or covered > expected + tol):
            recon = {"ledger_qty": qty, "partial_qty": partial_qty,
                     "expected_remaining": expected, "covered_qty": covered,
                     "gap_qty": gap, "gap_estimated": gap > tol,
                     "reason": ("partial_exits exceed ledger qty" if expected < -tol else
                                "settled qty > ledger qty (understated ledger)" if
                                covered > expected + tol else
                                "settled qty < ledger qty (missing fill evidence)"),
                     "gap_px": float(gap_px or 0.0) if gap > tol else None}
            logger.warning("mr: 关账数量核对不平 #%s：台账 %.10g − 片段 %.10g = %.10g，"
                           "本次有证据 %.10g，差额 %.10g（按参考价估算，标 estimated）",
                           open_row["id"] if "id" in open_row.keys() else "?",
                           qty, partial_qty, expected, covered, gap)
        fee_est = rate * (notional + covered * covered_px + max(gap, 0.0) * gap_px
                          + partial_notional)
        net_pnl = (partial_pnl + (covered_px - entry_price) * covered
                   + (gap_px - entry_price) * max(gap, 0.0)
                   - float(funding_paid or 0.0) - fee_est)
        raw = _raw_dict(open_row)
        raw.update({
            "exit_price_used": float(covered_px),
            "price_source": str(price_source),
            "estimated": (not complete),
            "partial_exits": partials, "partial_pnl": partial_pnl,
            "partial_notional": partial_notional,
            "remaining_qty": covered + max(gap, 0.0),
            "settled_qty": covered, "gap_qty": gap if abs(gap) > tol else 0.0,
            "fee_est": fee_est, "net_pnl": net_pnl,
            "net_pnl_complete": complete,
            "funding_paid_usdc": float(funding_paid or 0.0),
        })
        if recon is not None:
            raw["reconciliation"] = recon
        if extra:
            raw.update(extra)
        return {"fee_est": fee_est, "net_pnl": net_pnl, "partial_pnl": partial_pnl,
                "partial_qty": partial_qty, "covered_qty": covered, "gap_qty": gap,
                "expected_remaining": expected, "reconciliation": recon,
                "net_pnl_complete": complete, "raw": raw,
                "entry_price": entry_price, "notional": notional}

    def _action_required(self, status: Dict[str, Any], *, kind: str, reason: str,
                         signal_date: str = "",
                         evidence: Optional[Dict[str, Any]] = None) -> None:
        """挂上「需人工核对」的恢复证据路径（B5 的明确出口）。

        不做的事：不按 mark 编造成交、不重复下单。做的事：把**已有的**证据（交易所
        持仓/成交、意图 ts、cloid/oid、台账行）摊开，落一条 `mr_action_required`
        事件（同一 signal_date+kind 只落一次，避免每 5 分钟刷屏），并按 30 分钟节流推送。
        """
        payload = {"kind": str(kind), "reason": str(reason),
                   "signal_date": str(signal_date or ""),
                   "coin": str(self.config.coin or ""),
                   "evidence": dict(evidence or {}), "at": self.clock()}
        status["action_required"] = payload
        # 事件去重：进程重启会清内存，所以按 DB 里最近的事件判（而不是实例属性）。
        # **但去重只针对「事件行」，不针对「推送」**：旧实现在这里 return，晚到的
        # 推送失败就永远补不回来（事件行已存在 ⇒ 每轮都提前返回）。现在事件照样只
        # 落一条，推送是否重试由 alert_logs 的送达状态决定（见 `_alert(dedupe=True)`）。
        seen = False
        try:
            for row in self.db.hl_recent_events(10, kind=ACTION_REQUIRED_KIND) or []:
                prev = _event_data(row["data"])
                if (str(prev.get("kind") or "") == payload["kind"]
                        and str(prev.get("signal_date") or "") == payload["signal_date"]):
                    seen = True
                    break
        except Exception:                                     # noqa: BLE001
            pass
        if not seen:
            try:
                self.db.hl_log_event(ACTION_REQUIRED_KIND,
                                     f"MR 需人工核对（{kind}）：{reason}", data=payload)
            except Exception:                                 # noqa: BLE001
                logger.warning("mr: 写 mr_action_required 失败", exc_info=True)
        # 这些 caller 里有些同 tick 就按估算关账、有些保留 OPEN 等证据，**没有**
        # 一条对所有 caller 都成立的「自动下一步」；所以只写普遍为真的现状：
        # 差异/证据已记录、本条不是成交确认、由人核对。原因放最前面。
        lines = [
            f"需人工核对（{kind}）",
            f"原因      {reason}",
            "现状      差异与证据已记录（事件/提醒记录可查），本结论可能不完整",
            "说明      本条不是成交确认；未消除时请人工核对交易所持仓与成交",
        ]
        if payload["signal_date"]:
            lines.append(f"信号日    {payload['signal_date']}")
        self._alert("\n".join(lines), kind="mr_action_required",
                    key=f"{kind}:{payload['signal_date'] or '-'}", dedupe=True)

    def _adopt_entry(self, pos: dict, now: int, status: Dict[str, Any]) -> bool:
        """有意图、无台账、交易所有多仓 → 补记 OPEN 台账（无需人工改库）。

        B5（第七轮）：**6h 窗口只约束入场侧**（signal_date 只能从意图里取，超窗后无法
        再确定归属）。但「超窗 = 永久 recon_err」也不对：交易所的仓就是事实，MR 不认领
        它就既没有离场也没有保护。所以超窗时**加一道成交证据闸**：同 coin、开仓方向、
        数量与持仓吻合、时间在意图之后的成交 → 按真实成交 VWAP 记价并入账；证据不足
        则明确 `action_required`（原因 + 已有证据），**不**把来源不明的仓当成 MR 仓
        （那会让 MR 去平别人的仓）。
        """
        now_s = now / 1000.0
        window = float(getattr(self.config, "adopt_window_s", 6 * 3600.0))
        found = self._intent_event(_ENTRY_INTENT_KIND, now_s, max_age_s=window)
        stale = None
        if found is None:
            stale = self._intent_event(_ENTRY_INTENT_KIND, now_s)
        if found is None and stale is None:
            return False
        ts, payload = found or stale            # type: ignore[assignment]
        signal_date = str(payload.get("signal_date") or "")
        if not signal_date or self.db.mr_trade_by_signal(signal_date) is not None:
            return False
        szi = float(pos.get("szi") or 0.0)
        if szi <= 0:
            # MR 只做多：空头仓不按 MR 入场认领（手工/别的系统开的）。
            logger.warning("mr: 交易所 %s 是空头（szi=%s），跳过入场认领", self.config.coin,
                           pos.get("szi"))
            return False
        filled_sz = abs(szi)
        entry_px = float(pos.get("entry_px") or 0.0) or float(payload.get("ref_price") or 0.0)
        raw_extra: Dict[str, Any] = {
            "adopted": True, "adopt_reason": "fill_confirmation_lost",
            "intent_ts": ts, "fill_source": "xyz_dex_position"}
        if found is None:
            # 超窗：必须有可归属的成交证据，否则交人工（不认领 = 不动那笔仓）
            fills, note = self._fills(since_s=float(ts) - 120.0, until_s=now_s,
                                      close_side=False)
            fill_qty, fill_px, fill_ids = _fill_totals(fills)
            tol = max(1e-9, filled_sz * 0.02)   # 容差 2%（部分成交/手续费扣量）
            if fills is None or fill_qty <= 0 or abs(fill_qty - filled_sz) > tol:
                self._action_required(
                    status, kind="entry_adopt_stale",
                    reason=(f"入场意图已超 {window / 3600:.1f}h（{note}），交易所有 "
                            f"{filled_sz:g} 张 {self.config.coin} 多仓，但拿不到可归属的成交"
                            f"证据（成交 {fill_qty:g} 张），不自动认领"),
                    signal_date=signal_date,
                    evidence={"position_szi": filled_sz, "fill_qty": fill_qty,
                              "fill_ids": fill_ids[:10], "intent_ts": ts,
                              "intent_cloid": payload.get("cloid"),
                              "intent_planned_sz": payload.get("planned_sz"),
                              "fills_api": note})
                status["recon"] = "err"
                return False
            entry_px = entry_px or fill_px
            raw_extra.update({"fill_source": "exchange_fills", "fill_ids": fill_ids[:10],
                              "fill_qty": fill_qty, "fill_vwap": fill_px,
                              "estimated": False})
        if filled_sz <= 0 or entry_px <= 0:
            return False
        trade_id = self.db.mr_record_entry(
            signal_date=signal_date,
            entry_session=str(payload.get("entry_session") or signal_date),
            qty=filled_sz, qty_int=int(round(filled_sz * 10 ** 4)),
            entry_price=entry_px, entry_order_id=str(payload.get("oid") or ""),
            entry_ts=ts,
            notional=filled_sz * entry_px, margin=filled_sz * entry_px,
            equity_at_entry=payload.get("equity"),
            raw=raw_extra)
        # 把「结果未知的入场尝试」影子行收口（否则看门狗会一直报卡单）
        for row, _data in self._mr_attempts(MR_PURPOSE_ENTRY, signal_date,
                                            (ST_INTENT, ST_SENT)):
            self._shadow_finish(self._key_of(row), status=ST_FILLED,
                                filled_sz=filled_sz, avg_px=entry_px,
                                error="按交易所持仓补记台账")
        status["state"] = "entered"
        status["adopted"] = "entry"
        # 补记也是一条真实成交：按成交回报口径推送（受 fill_push 控制、可补推、同事件不重发）
        self._alert(_mr_entry_text(
            coin=self.config.coin, qty=filled_sz, price=entry_px,
            notional=filled_sz * entry_px, signal_date=signal_date,
            trade_id=trade_id, exec_ts=now_s, time_label="补记时间",
            note=("入场确认丢失，按交易所持仓补记台账（未重复下单）"
                  + ("，含成交证据" if raw_extra.get("fill_ids") else ""))),
            kind="mr_fill_entry_adopt", key=trade_id, fill=True, dedupe=True)
        return True

    def _adopt_exit(self, open_row, now: int, status: Dict[str, Any]) -> bool:
        """台账 OPEN + 交易所已空仓 → 用离场意图 + 成交/订单证据补记 CLOSED。

        第七轮 B5：旧实现只认 **6h 内**的离场意图，超出后「台账 OPEN + 交易所空仓」
        **永久** recon_err —— MR 的自愈入口只剩认领本身，人工改库是唯一出口。离场侧
        的归属由台账那一行给出（signal_date 已在 `mr_trades`），意图只需要证明「这笔
        离场是本系统发起的」，这个证明没有保质期，所以这里**不限意图年龄**。

        证据阶梯（有证据才认领；不按参考价编造成交、不重复下单）：

        1. 平仓**成交**覆盖剩余量 → 按真实成交 VWAP 关账（source=fills，确定）；
        2. 成交只覆盖一部分 → 已覆盖按成交 VWAP，缺口按 mark 估算并写
           `reconciliation` + `net_pnl_complete=False` + action_required；
        3. 超窗（> `adopt_window_s`）且没有成交 → 只认**订单证据**（离场影子行里的
           回执均价，= 交易所给的成交均价）；订单证据也没有 → **不关账**，保持
           recon_err + `action_required`（B5：「证据不足明确人工核对，不按 mark 编造
           未知成交」）；
        4. 窗内的旧口径保留（意图存在即可按 mark 估算关账，`estimated=True` + 喊人）：
           这支撑 `tests/test_mr_audit_r6.py` 钉住的 PR #21 行为，且它只作用于 6h 内、
           可被人工快速复核的窗口；
        5. 连意图都没有 → 不认领（无法证明离场是本系统发起的），recon_err +
           action_required 说明缺什么证据。

        为什么超窗不再按 mark 关账：mark 是**当前**的中间价，与几小时前的真实成交价
        无关；那条 net_pnl 会直接进面板「累计已实现」，等于把一个编出来的数当成事实。
        宁可让台账停在 OPEN（它没有仓，安全）并喊人。
        """
        signal_date = str(open_row["signal_date"])
        now_s = now / 1000.0
        window = float(getattr(self.config, "adopt_window_s", 6 * 3600.0))
        found = self._intent_event(_EXIT_INTENT_KIND, now_s, signal_date=signal_date)
        if found is None:
            self._action_required(
                status, kind="exit_adopt_no_intent",
                reason=(f"台账 OPEN 但交易所已无 {self.config.coin} 仓，且找不到该 signal_date "
                        f"的离场意图 → 无法证明这笔离场由本系统发起；需核对 hl_fills/"
                        f"交易所成交与台账 qty"),
                signal_date=signal_date,
                evidence={"ledger_qty": open_row["qty"],
                          "ledger_entry_ts": open_row["entry_ts"],
                          "entry_order_id": open_row["entry_order_id"]})
            return False
        ts, payload = found
        stale = bool(window > 0 and now_s - float(ts) > window)
        mark = self._mark([], None)
        qty = float(open_row["qty"] or 0.0)
        partials = _partial_exits(open_row)
        partial_qty = sum(abs(float(p.get("qty") or 0.0)) for p in partials)
        expected = max(qty - partial_qty, 0.0)
        # 平仓成交证据（从入场前 2 分钟起，覆盖持仓期全部平仓腿）
        fills, note = self._fills(since_s=float(open_row["entry_ts"] or 0.0) - 120.0,
                                  until_s=now_s, close_side=True)
        fill_qty, fill_px, fill_ids = _fill_totals(fills)
        # 全持仓期成交已覆盖原始总量时，扣掉已入账片段的数量与名义。
        # 不能裁数量为 remaining 却仍使用全笔 VWAP（会把旧片段重复定价）。
        if partial_qty > 0 and abs(fill_qty - qty) <= max(1e-9, qty * 1e-9):
            if partials and all(_price_is_evidence(str(p.get("price_source") or ""))
                                for p in partials):
                partial_ntl = sum(float(p.get("qty") or 0.0) * float(p.get("price") or 0.0)
                                  for p in partials)
                remaining_ntl = fill_qty * fill_px - partial_ntl
                fill_qty = expected
                fill_px = remaining_ntl / expected if expected > 0 else 0.0
            else:
                # 旧片段含估算价：既然全笔真实成交齐了，就统一重建整笔，避免用估算价
                # 从真实名义中相减后造出另一段“真实价”。原片段保留为审计来源。
                raw_rebuilt = _raw_dict(open_row)
                raw_rebuilt["partial_exits_before_rebuild"] = partials
                raw_rebuilt["partial_exits"] = []
                open_row = {**dict(open_row), "raw": json.dumps(raw_rebuilt)}
                partials = []
                partial_qty = 0.0
                expected = qty
        elif partial_qty > 0 and expected > max(1e-9, qty * 1e-9) and fills:
            # 非全量窗口只接纳能证明尚未入片段的订单；无身份不猜归属。
            old_oids = {str(p.get("oid")) for p in partials if p.get("oid")}
            old_cloids = {str(p.get("cloid")) for p in partials if p.get("cloid")}
            if not old_oids and not old_cloids:
                self._action_required(status, kind="exit_adopt_ambiguous_fragments",
                                      reason="历史片段无订单身份且成交未覆盖全笔，无法排除重复证据",
                                      signal_date=signal_date)
                return False
            fills = [f for f in fills if
                     (f.get("oid") or f.get("cloid")) and
                     str(f.get("oid") or "") not in old_oids and
                     str(f.get("cloid") or "") not in old_cloids]
            fill_qty, fill_px, fill_ids = _fill_totals(fills)
        estimated = True
        source = "all_mids"
        covered = expected
        covered_px = mark
        gap_qty = 0.0
        gap_px = 0.0
        order_oid = ""
        fragments_complete = (qty > 0 and abs(qty - partial_qty) <= max(1e-9, qty * 1e-9)
                              and all(_price_is_evidence(str(p.get("price_source") or ""))
                                      for p in partials))
        if fragments_complete:
            covered = 0.0
            covered_px = sum(float(p["qty"]) * float(p["price"]) for p in partials) / partial_qty
            source = "fills"
            estimated = False
        elif fill_qty > 0:
            covered = min(expected, fill_qty) if expected > 0 else fill_qty
            covered_px = fill_px
            source = "fills"
            estimated = False
            if expected > 0 and fill_qty + 1e-9 < expected:
                gap_qty = expected - covered
                gap_px = mark
                estimated = True
                self._action_required(
                    status, kind="exit_adopt_partial_fills",
                    reason=(f"成交只覆盖 {fill_qty:g}/{expected:g} 张，缺口按 mark {mark:.2f} "
                            f"估算（net_pnl_complete=False），请人工核对"),
                    signal_date=signal_date,
                    evidence={"fill_qty": fill_qty, "fill_vwap": fill_px,
                              "fill_ids": fill_ids[:10], "gap_qty": gap_qty})
        elif stale:
            # 超窗：意图只能证明「我们发过单」，证明不了成交价 —— 必须有订单/成交证据
            ev_px, ev_qty, order_oid = self._exit_order_evidence(signal_date)
            if ev_px <= 0 or ev_qty <= 0:
                self._action_required(
                    status, kind="exit_adopt_stale_no_evidence",
                    reason=(f"离场意图已超 {window / 3600:.1f}h，交易所已无仓，但既查不到"
                            f"可归属的平仓成交（{note}）也没有离场回执均价 → 不按参考价"
                            f"编造成交；台账保持 OPEN，请核对 hl_fills/交易所成交与台账 qty"),
                    signal_date=signal_date,
                    evidence={"ledger_qty": qty, "partial_qty": partial_qty,
                              "expected_remaining": expected,
                              "intent_ts": ts, "intent_cloid": payload.get("cloid"),
                              "fills_api": note,
                              "recovery": ("人工核对后在 mr_trades 里补 exit_price/"
                                           "net_pnl，或按真实成交关账")})
                status["recon"] = "err"
                return False
            covered = min(expected, ev_qty) if expected > 0 else ev_qty
            covered_px = ev_px
            source = "order_receipt"
            estimated = False
            if expected > 0 and ev_qty + 1e-9 < expected:
                gap_qty = expected - covered
                gap_px = mark
                estimated = True
                self._action_required(
                    status, kind="exit_adopt_partial_order",
                    reason=(f"离场回执只覆盖 {ev_qty:g}/{expected:g} 张，缺口按 mark "
                            f"{mark:.2f} 估算（net_pnl_complete=False），请人工核对"),
                    signal_date=signal_date,
                    evidence={"order_qty": ev_qty, "order_px": ev_px,
                              "order_oid": order_oid, "gap_qty": gap_qty})
        else:
            # 6h 内：旧口径（意图存在 → 按 mark 估算关账），标 estimated + 喊人
            if mark <= 0:
                return False
            kind = ("exit_adopt_fills_unreadable" if fills is None
                    else "exit_adopt_mark_estimate")
            self._action_required(
                status, kind=kind,
                reason=(f"离场意图存在（{ts:.0f}），但"
                        + (f"成交读取失败（{note}）" if fills is None
                           else "交易所查不到可归属的平仓成交")
                        + f" → 按 mark {mark:.2f} 估算关账（source=all_mids，"
                          f"net_pnl_complete=False），请人工核对真实成交价"),
                signal_date=signal_date,
                evidence={"ledger_qty": qty, "partial_qty": partial_qty,
                          "intent_ts": ts, "intent_cloid": payload.get("cloid"),
                          "fills_api": note})
        if covered_px <= 0:
            return False
        st = self._settlement(
            open_row, covered_qty=covered, covered_px=covered_px,
            gap_qty=gap_qty, gap_px=gap_px, price_source=source, estimated=estimated,
            extra={"adopted": True, "intent_ts": ts, "adopt_ref_price": mark,
                   "fills_api": note, "fill_ids": fill_ids[:10],
                   "fill_qty": fill_qty, "adopt_reason": "exit_confirmation_lost",
                   "order_oid": order_oid or None})
        self.db.mr_close_trade(
            int(open_row["id"]), exit_price=covered_px, exit_order_id="",
            exit_ts=now_s, exit_reason="sma5_adopted",
            exit_signal_date=(str(payload.get("exit_signal_date") or "") or None),
            funding_pct=None, fee_quote=st["fee_est"], net_pnl=st["net_pnl"],
            raw=st["raw"])
        # 结果未知的离场尝试影子行收口
        for row, _data in self._mr_attempts(MR_PURPOSE_EXIT, signal_date,
                                            (ST_INTENT, ST_SENT)):
            self._shadow_finish(self._key_of(row), status=ST_FILLED,
                                avg_px=covered_px, filled_sz=covered,
                                error="按离场证据补记台账")
        status["state"] = "exited"
        status["adopted"] = "exit"
        status["net_pnl"] = st["net_pnl"]
        status["net_pnl_complete"] = st["net_pnl_complete"]
        # 补记关账也是成交类通知：受 fill_push 控制、可补推、同事件不重发。
        # funding 未参与本次补记（adopt 路径不读资金费流水）→ 明说未计入。
        self._alert(_mr_exit_text(
            coin=self.config.coin, filled=covered, ledger_qty=qty,
            partial_qty=partial_qty, gap_qty=gap_qty,
            entry_px=float(open_row["entry_price"] or 0.0),
            exit_px=covered_px, entry_ts=float(open_row["entry_ts"] or 0.0),
            now_s=now_s, net_pnl=st["net_pnl"], fee_est=st["fee_est"],
            complete=bool(st["net_pnl_complete"]),
            funding_paid=None, price_source=source, signal_date=signal_date,
            exec_ts=now_s, time_label="补记时间",
            note=(f"离场确认丢失，按当前证据补记台账（价格来源 {source}，"
                  "未重复下单）")),
            kind="mr_fill_exit_adopt", key=int(open_row["id"]),
            fill=True, dedupe=True)
        return True

    def _exit_order_evidence(self, signal_date: str) -> Tuple[float, float, str]:
        """离场尝试影子行（purpose=mr_exit, status=filled）里的回执证据。

        → `(VWAP, Σ数量, oid)`；没有任何回执均价/数量时 `(0,0,"")`。

        这是 B5 里点名的「**订单**证据」：`avg_px` 是交易所给的成交均价（不是我们估
        的），比 mark 强得多；它在「成交接口查不到（分页上限/读失败）」而离场回执已经
        落库的那条路径上，是唯一可信的价格来源。崩溃在 `place_order` 之后就断掉的
        尝试不会有回执，这里返回 0 → 调用方按「证据不足」处理。
        """
        qty = 0.0
        notional = 0.0
        oid = ""
        for row, _data in self._mr_attempts(MR_PURPOSE_EXIT, signal_date, (ST_FILLED,)):
            try:
                px = float(row["avg_px"] or 0.0)
                sz = abs(float(row["filled_sz"] or 0.0))
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if px <= 0 or sz <= 0:
                continue
            qty += sz
            notional += px * sz
            try:
                oid = str(row["oid"] or "") or oid
            except (KeyError, IndexError):
                pass
        return (notional / qty if qty else 0.0), qty, oid

    def _venue_px(self, px: float, sz_decimals: int) -> float:
        """限价取整：优先 venue 自己的 tickSize（Kraken 只接受 tick 整数倍）。

        HL 规则（≤5 位有效数字 + ≤6−szDecimals 位小数）在 PF_QQQXUSD（tick=0.1）
        上会给出 2 位小数的价，随后又被 `KrExchange.place_order` 里的
        `info.round_price` 二次取整 —— 台账 raw.limit_px 与实发价对不上。这里先按
        tick 取整（同一条规则只跑一遍）；`round_price` 抛错（取不到 tickSize）才回退
        旧公式，避免精度接口故障把离场卡死。
        """
        rp = getattr(self.info, "round_price", None)
        if callable(rp):
            try:
                return float(rp(self.config.coin, px))
            except Exception as exc:                          # noqa: BLE001
                logger.warning("mr: round_price(%s, %s) 失败，回退 HL 规则：%s",
                               self.config.coin, px, exc)
        return _round_px(px, sz_decimals)

    def _sz_decimals(self) -> Optional[int]:
        cfg = self.config
        try:
            for row in self.info.perp_meta_dex(cfg.dex) or []:
                if (row.get("name") or "") == cfg.coin:
                    return int(row.get("szDecimals"))
        except Exception as exc:                              # noqa: BLE001
            logger.warning("mr: perp_meta_dex failed: %s", exc)
            return None
        return None

    def _candles(self, now_ms: int) -> List[dict]:
        cfg = self.config
        start = now_ms - cfg.candle_hours * 3600_000
        return list(self.info.candles(cfg.coin, "1h", start, now_ms) or [])

    def _mark(self, rows: List[dict], mr_pos: Optional[dict]) -> float:
        cfg = self.config
        try:
            mids = self.info.all_mids(dex=cfg.dex) or {}
            mark = float(mids.get(cfg.coin) or 0.0)
        except Exception:                                     # noqa: BLE001
            mark = 0.0
        if mark <= 0 and rows:
            mark = float(rows[-1].get("c") or 0.0)
        if mark <= 0 and mr_pos:
            value, szi = mr_pos["position_value"], abs(mr_pos["szi"])
            mark = value / szi if szi else 0.0
        return mark

    # ------------------------------------------------------------------ 主流程

    def tick(self, now_ms: Optional[int] = None) -> dict:
        cfg = self.config
        now = int(now_ms if now_ms is not None else int(self.clock() * 1000))
        status: Dict[str, Any] = {"enabled": False, "kill": False,
                                  "state": "disabled", "recon": "unknown",
                                  "error": "", "updated_at": self.clock()}
        try:
            return self._tick_inner(now, status)
        except Exception as exc:                              # noqa: BLE001
            logger.exception("mr: tick failed")
            status.update(state="error", error=f"{type(exc).__name__}: {exc}",
                          updated_at=self.clock())
            # 去重：维护窗口里每 5 分钟一条会把告警淹掉（第五轮）。
            self._alert_throttled("tick_error",
                                  f"MR tick 异常：{type(exc).__name__}: {exc}")
            return status

    def _tick_inner(self, now: int, status: Dict[str, Any]) -> dict:
        cfg = self.config
        address = (self.address_fn() or "").strip()
        if not address:
            # Kraken 没有钱包地址：main 会填一个占位常量，读到空 = 装配异常。
            status["error"] = "账户标识为空（Kraken 下应是 main 填的占位常量，说明装配异常）"
            status["state"] = "error"
            return status
        enabled = self._enabled()
        kill = self._global_kill()
        status.update(enabled=enabled, kill=kill)
        open_row = self.db.mr_open_trade()
        account = self._read_account(address)
        pos = account["mr_pos"]
        # 给 crypto 腿定仓用的「MR 占用」：必须在 recon 分支之前写 —— recon_err 时
        # MR 仍可能占着保证金，crypto 腿不该按全权益放大。
        status["margin_used"] = float(account.get("mr_margin_used") or 0.0)
        status["margin_reserved"] = float(account.get("mr_margin_reserved") or 0.0)

        # ---- 认领：确认窗口丢失的成交（读错误 / 进程重启）-------------------------
        fresh = bool(open_row is not None and open_row["entry_ts"]
                     and now / 1000.0 - float(open_row["entry_ts"]) < 2 * 3600.0)
        if open_row is None and pos is not None:
            if self._adopt_entry(pos, now, status):
                open_row = self.db.mr_open_trade()
                fresh = True
        if open_row is not None and pos is None and not fresh:
            if self._adopt_exit(open_row, now, status):
                open_row = None

        # ---- 对账（双向） ------------------------------------------------------
        holding = False
        recon_ok = True
        if open_row is not None and pos is None and fresh:
            holding = True                                    # 刚下单，持仓行还没出现
        elif open_row is not None and pos is None:
            recon_ok = False
            self._recon_fail(f"台账有 OPEN 仓但交易所没有 {cfg.coin} 持仓（人工核对）")
        elif open_row is None and pos is not None:
            recon_ok = False
            self._recon_fail(f"交易所有 {cfg.coin} 持仓但台账没有 OPEN（人工核对，不自动吃）")
        elif open_row is not None and pos is not None:
            holding = True
        if recon_ok:
            status["recon"] = "ok"
            self._recon_alerted_at = 0.0
        else:
            status["recon"] = "err"
            status["state"] = "recon_err"
            return status
        if status.get("adopted") == "exit":
            return status            # 本轮刚补记完离场，不再评估新入场

        # 停用且空仓：连 K 线都不拉（每 5 分钟一轮，停用态不该有取数成本）
        if not enabled and not holding:
            status["state"] = "disabled"
            return status

        # ---- 会话与信号 --------------------------------------------------------
        rows = self._candles(now)
        sessions, dropped = mr_signal.aggregate_sessions_ex(rows)
        self._note_dropped_sessions(dropped, now, status)
        if len(sessions) < 6:
            status["state"] = "no_data"
            status["error"] = f"usable sessions={len(sessions)}"
            return status
        status["last_session_date"] = sessions[-1].date
        status["sessions_usable"] = len(sessions)
        self._fill_signal_status(status, sessions, rows, now)

        if holding:
            return self._maybe_exit(now, sessions, rows, open_row, pos, status)
        return self._maybe_enter(now, sessions, rows, account, status)

    def _fill_signal_status(self, status: Dict[str, Any], sessions, rows,
                            now_ms: int) -> None:
        """面板/聚合页要的只读视图：最近会话信号 + 离场线 + 下一次评估时刻。

        全部在这里算完（tick 本来就要 sessions/signals），面板零重算、零网络。
        离场线 = **最近 4 个已完成会话收盘均值** —— `close > SMA5`（SMA5 含当日）等价于
        `close > mean(前 4 个收盘)`，面板显示的数可直接和收盘价比。

        2026-11-13 第六轮：只认**已收盘完成**的会话（`completed_upto`：末根 bar 开盘 +
        1h <= now）。盘中调到 15:30 ET 时 `sessions[-1]` 是还没走完的当天会话，直接拿它
        算 RSI2/离场线 = 用未收盘的假收盘价出信号，面板会先把「信号已出」显示出来。
        一个已完成会话都没有时整块 signal 不生成（保持原「空行为」）。
        """
        k = mr_signal.completed_upto(sessions, now_ms)
        if k <= 0:
            return
        done = list(sessions[:k])
        try:
            sigs = mr_signal.session_signals(done)
        except Exception:                                     # noqa: BLE001
            return
        if not sigs:
            return
        last = done[-1]
        sig = sigs[-1]
        closes = [s.close for s in done]
        try:
            status["mark"] = float(rows[-1].get("c")) if rows else None
        except (TypeError, ValueError, AttributeError):
            status["mark"] = None
        status["signal"] = {
            "last_session_date": last.date,
            "rsi2": sig.get("rsi2"),
            "threshold": mr_signal.RSI_THRESHOLD,
            "entry_flag": bool(sig.get("entry")),
            "exit_flag": bool(sig.get("exit_above")),
            "exit_line": (sum(closes[-4:]) / 4.0) if len(closes) >= 4 else None,
            "sessions_usable": len(sessions),
            "next_eval_et": _next_eval_et(now_ms),
            "window": _entry_window(sessions, now_ms),
            "phase": _phase(sessions, now_ms),
        }

    def _recon_fail(self, message: str) -> None:
        now_s = self.clock()
        if now_s - self._recon_alerted_at >= 6 * 3600.0:
            self._recon_alerted_at = now_s
            self._alert(f"MR 对账不平：{message}")
        logger.error("mr recon: %s", message)

    def _note_dropped_sessions(self, dropped, now_ms: int, status: Dict[str, Any]) -> None:
        """「整个会话因数据不足被弃用」→ 落一条审计事件（同一会话只报一次）。

        2026-11-02 第五轮：末根闸把「用错收盘价」换成了「整天静默丢弃」——
        信号日整天消失、连事件都没有。这里只报**已收盘**的会话，也不推送。
        """
        if not dropped:
            return
        tz = ZoneInfo(mr_signal.SESSION_TZ)
        reported = []
        for item in dropped[-5:]:
            try:
                close_ms = int(datetime.strptime(str(item["date"]), "%Y-%m-%d")
                               .replace(hour=16, minute=0, tzinfo=tz).timestamp() * 1000)
            except (TypeError, ValueError):
                continue
            if close_ms + 3600_000 > now_ms:                  # 还没收盘：正常
                continue
            key = "session_dropped:" + str(item["date"])
            if self._order_alerted_at.get(key):
                continue
            self._order_alerted_at[key] = now_ms / 1000.0
            reported.append(item)
            try:
                self.db.hl_log_event(
                    "mr_session_dropped",
                    f"MR 会话 {item['date']} 数据不足已弃用（bars={item['bars']}、"
                    f"末根 {item['last_bar_et']} ET、{item['reason']}）",
                    data=item)
            except Exception:                                 # noqa: BLE001
                logger.warning("mr: 写 mr_session_dropped 失败", exc_info=True)
        if reported:
            status["dropped_sessions"] = [str(r["date"]) for r in reported]

    def _enabled_since(self) -> float:
        """最近一次「启用」事件的时刻（0 = 从未启用）。给 `_mark_expired_signal` 去噪。"""
        try:
            rows = self.db.hl_recent_events(5, kind=MR_EVENT_KIND)
        except Exception:                                     # noqa: BLE001
            return 0.0
        for row in rows:
            if _event_data(row.get("data") if isinstance(row, Mapping) else None).get("enabled"):
                try:
                    return float(row["created_at"] or 0.0)
                except (KeyError, TypeError, ValueError):
                    continue
        return 0.0

    # ------------------------------------------------------------------ 入场

    def _resolve_entry_unknown(self, signal_date: str, now: int,
                               status: Dict[str, Any]) -> str:
        """核对「结果未知」的入场尝试 → `adopted` / `blocked` / `clear`。

        第七轮（IOC 未知结果禁止无证重发）：`ok=false` 既可能是明确拒单，也可能是
        「交易所已受理但回执丢失」。旧实现下一轮直接换 cloid 重发 —— 在后一种情况下
        就是第二笔仓。现在只要有 intent/sent 的影子行（= 结果未知），就先取证据：

        * 交易所有多仓 → 按持仓补记台账（`adopted`，**不再发单**）
        * 读得到账户且确认无仓 → IOC 早已过期 = 确实没成交 → `clear`（允许重试）
        * 读不到账户 → `blocked`（未知，**不发单**，挂 action_required）
        """
        pending = self._mr_attempts(MR_PURPOSE_ENTRY, signal_date, (ST_INTENT, ST_SENT))
        if not pending:
            return "clear"
        address = self.address_fn() or ""
        pos_now, read_ok = self._await_position(address, want=True, confirm_s=0.0)
        if not read_ok:
            self._action_required(
                status, kind="entry_result_unknown",
                reason=("上一笔入场结果未知（影子行停在 intent/sent）且账户读失败 → "
                        "本轮不重发，等持仓/成交证据"),
                signal_date=signal_date,
                evidence={"pending_attempts": len(pending),
                          "attempt_status": [str(r["status"]) for r, _d in pending][:5],
                          "cloids": [d.get("cloid") for _r, d in pending][:5]})
            return "blocked"
        if pos_now is not None and float(pos_now.get("szi") or 0.0) > 0:
            if self._adopt_entry(pos_now, now, status):
                return "adopted"
            # 认领不成立（空头仓 / 无成交证据）→ 不动那笔仓，也绝不重发
            return "blocked"
        # 账户读得到、确认没有仓：前一张 IOC 早已不在场 = 明确未成交
        for row, _data in pending:
            self._shadow_finish(self._key_of(row), status=ST_FAILED,
                                error="核对：交易所无仓 → 视为未成交（IOC 已过期）")
        return "clear"

    def _maybe_enter(self, now: int, sessions, rows, account: dict,
                     status: Dict[str, Any]) -> dict:
        cfg = self.config
        if status.get("kill"):
            status["state"] = "flat"
            return status
        decision = mr_signal.entry_decision(sessions=sessions, now_ms=now, flat=True)
        status["state"] = "flat"
        if decision is None:
            self._mark_expired_signal(sessions, now)
            return status
        signal_date = decision["signal_date"]
        if self.db.mr_trade_by_signal(signal_date) is not None:
            return status
        # ---- 结果未知的入场尝试：先取证再决定要不要重发 --------------------------
        # IOC 的 ok=false 既可能是明确拒单，也可能是「交易所已受理但回执丢了」。
        # 旧实现下一轮直接换 cloid 重发 —— 恰好在后一种情况下变成第二笔仓。
        verdict = self._resolve_entry_unknown(signal_date, now, status)
        if verdict == "adopted":
            return status
        if verdict == "blocked":
            status["state"] = "action_required"
            return status
        sz_dec = self._sz_decimals()
        if sz_dec is None:
            status["state"] = "error"
            status["error"] = "szDecimals 未知（meta?dex 查不到该币）"
            self._alert("MR 放弃：szDecimals 未知，拒单（fail-closed）")
            return status
        mark = self._mark(rows, None)
        if mark <= 0:
            status["state"] = "no_data"
            status["error"] = "无可用标记价"
            return status
        equity_total = account["equity_total"]
        base = equity_total - account["mr_margin_used"]
        if equity_total <= 0 or base <= 0:
            status["state"] = "no_equity"
            return status
        notional = base * cfg.exposure
        sz = _floor_sz(notional / mark, sz_dec)
        if notional < cfg.min_notional or sz <= 0:
            reason = (f"权益不足：equity={equity_total:.2f} notional={notional:.2f} "
                      f"sz={sz}（min ${cfg.min_notional}）")
            self.db.mr_mark_skipped(signal_date, reason)
            self._alert(f"MR 信号 {signal_date} 放弃：{reason}")
            return status
        if notional > base:
            self.db.mr_mark_skipped(signal_date, "notional > equity（保证金不足保护）")
            return status

        # 【P2-E】组合总名义闸门：主策略 `_entry_gates` 用「全账户名义 + 本笔」对
        # `HL_MAX_TOTAL_NOTIONAL_MULT` 设限，而 MR 旧实现只看自身 base×exposure ——
        # 主策略先到 2.0x 后 MR 还能再加 0.5x（组合 2.5x）；反向顺序会被主策略拦住，
        # 所以偏差是单向「只超不收」。这里复用同一个上限（同一个 env），读全账户
        # totalNtlPos（含 MR 与主策略全部持仓）。
        total_ntl, cap, blocked = self._total_notional_gate(equity_total, notional)
        if total_ntl is not None:
            status["total_notional"] = total_ntl
        if cap is not None:
            status["total_notional_cap"] = cap
        if blocked and total_ntl is None:
            # 读不到账户名义/权益 → fail-closed：本轮不下单。**不写 SKIPPED 台账** ——
            # 单次接口抖动不该吃掉当天唯一信号（入场窗口内还有下几个 tick 重试），
            # 但按 30 分钟节流告警，让「MR 因读不到名义停摆」看得见。
            status["state"] = "total_notional_unknown"
            status["error"] = blocked
            self._alert_throttled("total_notional_unknown",
                                  f"MR 信号 {signal_date} 本轮不下单：{blocked}")
            return status
        if blocked:
            # 超上限 = 结构性放弃本信号（与「权益不足」同口径写 SKIPPED，可审计）。
            status["state"] = "skipped_total_notional"
            status["error"] = blocked
            self.db.mr_mark_skipped(signal_date, blocked)
            self._alert(f"MR 信号 {signal_date} 放弃（组合总名义上限）：{blocked}")
            return status

        # ---- 既有风控闸门（当日下单数 / 近一小时失败）：阈值与主腿同源，不新增旋钮 ----
        gate = self._order_gate(now / 1000.0)
        if gate:
            # **不写 SKIPPED**：这是临时性刹车（跨日/一小时后自解），信号的入场窗口
            # 内还有后续 tick 可以重试，不该因为一次刹车就把当天唯一信号记成放弃。
            status["state"] = "order_gate_blocked"
            status["error"] = gate
            self._alert_throttled("order_gate", f"MR 入场被风控闸门拦下：{gate}")
            return status
        if not self._ensure_leverage(status):
            status["state"] = "margin_mode_failed"
            return status
        cloid = make_cloid(cfg.coin, _attempt_ms(now), "mr_entry")
        limit_px = self._venue_px(mark * (1 + cfg.slippage_bps / 10000.0), sz_dec)
        self.db.hl_log_event(
            _ENTRY_INTENT_KIND,
            f"MR 入场下单意图：{cfg.coin} {sz} @≈{mark:.2f}（信号 {signal_date}）",
            data={"signal_date": signal_date,
                  "entry_session": decision["entry_session_date"],
                  "planned_sz": sz, "ref_price": mark, "equity": equity_total,
                  "cloid": cloid}, ts=now / 1000.0)
        shadow = self._shadow_begin(MR_PURPOSE_ENTRY, side=1, sz=sz, px=limit_px,
                                    reduce_only=False, cloid=cloid,
                                    signal_date=signal_date, now_ms=now)
        result = self.exchange.place_order(
            cfg.coin, True, sz, limit_px, reduce_only=False, tif="Ioc",
            cloid=cloid)
        if str(result.get("status") or "") == "dry":
            # 影子模式（exchange.dry_run）：一张真单都没发，不留错误计数
            status["state"] = "dry_run"
            status["error"] = "影子模式：未真实下单"
            self._shadow_finish(shadow, status=ST_PLANNED, error="dry_run")
            return status
        if not result.get("ok") or str(result.get("status") or "") not in (
                "filled", "partially_filled", "resting"):
            # `partially_filled` 必须走**正常建账**路径（下面 1621 那段按交易所实际持仓
            # 记台账）：Kraken 对部分成交也回 partially_filled（不是失败）。旧集合只认
            # filled/resting，把一次正常的部分成交塞进「回执丢失」通道 —— 台账虽会被
            # `_adopt_entry` 补上，但会推一条「入场回执失败」的错告警、raw 里还标
            # adopt_reason=fill_confirmation_lost。两者都是可证伪的假象（第十一轮）。
            # ok=false 有两个来源：明确拒单（invalidSize/资金不足）与**结果未知**
            # （回执丢失、无 EXECUTION 明细）。先用账户事实分辨，绝不盲目重发。
            err = str(result.get("error") or result.get("raw") or "unknown")
            status["error"] = err
            pos_now, read_ok = self._await_position(self.address_fn() or "",
                                                    want=True, confirm_s=0.0)
            if read_ok and pos_now is not None and float(pos_now.get("szi") or 0.0) > 0:
                # 交易所已成交、只是回执丢了 → 按持仓补记台账，本轮不再发单
                self._shadow_finish(shadow, status=ST_FILLED,
                                    oid=result.get("oid"),
                                    avg_px=result.get("avg_px"),
                                    filled_sz=abs(float(pos_now.get("szi") or 0.0)),
                                    error="回执失败但交易所已成交（按持仓补记）")
                if self._adopt_entry(pos_now, now, status):
                    status["state"] = "entered"
                    status["adopted"] = "entry"
                    self._alert_throttled(
                        "entry_receipt_lost",
                        f"MR 入场回执失败（{err[:120]}）但交易所已成交 → 已按持仓补记台账，"
                        f"未重复下单")
                    return status
                status["state"] = "action_required"
                return status
            if not read_ok:
                # 未知：IOC 可能已成交，但读不到账户 → 留影子行在 sent，等证据
                self._shadow_finish(shadow, status=ST_SENT,
                                    error=f"结果未知：读账户失败（{err[:160]}）")
                self._action_required(
                    status, kind="entry_result_unknown",
                    reason=("入场下单结果未知（回执失败且账户读失败），本轮不重发；"
                            "下一轮先按持仓/成交取证"),
                    signal_date=signal_date,
                    evidence={"cloid": cloid, "planned_sz": sz, "ref_price": mark,
                              "exchange_error": err[:200]})
                status["state"] = "action_required"
                return status
            # 账户读得到、确实没有仓 → 明确未成交，可以安全重试（下一轮换 cloid）
            self._shadow_finish(shadow, status=ST_FAILED, error=err)
            status["state"] = "order_rejected"
            self._alert_throttled("entry_rejected", f"MR 入场被拒：{err[:200]}")
            return status
        # 「受理」不等于「成交」：IOC 只认 filled；再用 xyz dex 持仓核一遍实际数量
        if str(result.get("status") or "") not in ("filled", "partially_filled"):
            not_executed = bool(result.get("not_executed"))
            status["state"] = "entry_not_filled"
            status["error"] = f"回执 status={result.get('status')!r}"
            self._shadow_finish(
                shadow, status=ST_FAILED if not_executed else ST_SENT,
                error=f"回执 status={result.get('status')!r}"
                      f"{'（IOC 未成交，已不在场）' if not_executed else '（在场/结果未知，不重发）'}")
            if not not_executed:
                # resting 且没有 not_executed 标记（IOC 不该出现）：结果未知 —— 状态沿用
                # entry_not_filled（既有契约），但影子行停在 sent 上，下一轮必须**先取证**
                # 才会重发（`_resolve_entry_unknown`）。
                self._action_required(
                    status, kind="entry_resting_unknown",
                    reason=f"入场回执 status={result.get('status')!r}（IOC 本不该在场）→ 不重发",
                    signal_date=signal_date,
                    evidence={"cloid": cloid, "oid": result.get("oid"), "planned_sz": sz})
                self._alert_throttled(
                    "entry_not_filled",
                    "MR 入场单回执不是 filled（也未标未成交）——台账保持空，"
                    "下一轮先按持仓/成交取证再决定是否重发")
                return status
            self._alert_throttled(
                "entry_not_filled",
                "MR 入场单未成交（IOC 未成交/已撤销）——不写台账，下轮重试")
            return status
        filled, _seen = self._await_position(self.address_fn() or "", want=True)
        if filled is None:
            status["state"] = "entry_not_filled"
            status["error"] = f"确认窗口内交易所没有 {cfg.coin} 持仓"
            self._shadow_finish(shadow, status=ST_SENT,
                                oid=result.get("oid"),
                                error="回执 filled 但确认窗口内没读到持仓（不重发，下轮取证）")
            self._alert_throttled(
                "entry_not_filled",
                f"MR 入场单回执 filled 但交易所没有出现 {cfg.coin} 持仓——不写台账，"
                f"下轮先按成交/持仓取证（不重复下单）")
            return status
        filled_sz = abs(float(filled.get("szi") or 0.0))
        if filled_sz <= 0:
            status["state"] = "entry_not_filled"
            self._shadow_finish(shadow, status=ST_FAILED, error="持仓数量为 0")
            return status
        entry_px = float(filled.get("entry_px") or 0.0)
        if entry_px <= 0:
            entry_px = float(result.get("avg_px") or mark)
        trade_id = self.db.mr_record_entry(
            signal_date=signal_date, entry_session=decision["entry_session_date"],
            qty=filled_sz, qty_int=int(round(filled_sz * 10 ** 4)),
            entry_price=entry_px, entry_order_id=str(result.get("oid") or ""),
            entry_ts=now / 1000.0, notional=filled_sz * entry_px,
            margin=filled_sz * entry_px, equity_at_entry=equity_total,
            raw={"ref_price": mark, "limit_px": limit_px, "cloid": cloid,
                 "planned_sz": sz, "filled_sz": filled_sz,
                 "fill_source": "xyz_dex_position",
                 "result_status": str(result.get("status"))})
        self._shadow_finish(shadow, status=ST_FILLED, oid=result.get("oid"),
                            filled_sz=filled_sz, avg_px=entry_px)
        if abs(filled_sz - sz) > 1e-9:
            try:
                self.db.hl_log_event("mr_entry_partial_fill",
                                     f"MR 入场部分成交：计划 {sz} / 实际 {filled_sz}")
            except Exception:                             # noqa: BLE001
                pass
        self._slippage_guard(result, mark, side="入场")
        status["state"] = "entered"
        status["last_fill_at"] = now / 1000.0
        self._alert(_mr_entry_text(coin=cfg.coin, qty=filled_sz, price=entry_px,
                                   notional=filled_sz * entry_px,
                                   signal_date=signal_date, trade_id=trade_id,
                                   exec_ts=now / 1000.0, planned_qty=sz),
                    kind="mr_fill_entry", key=trade_id, fill=True, dedupe=True)
        return status

    def _mark_expired_signal(self, sessions, now_ms: int) -> None:
        """信号日已整体错过（含「当天窗口已过」）却没有台账 → 记 SKIPPED，可审计。

        旧实现要求 `entry_day < today`，当天 12:30 之后才恢复运行的信号永远没有审计行。
        现在按**入场截止时刻**判定，并回看最近 5 个会话（覆盖多日停机）。
        """
        sigs = mr_signal.session_signals(sessions)
        k = mr_signal.completed_upto(sessions, now_ms)
        tz = mr_signal.ZoneInfo(mr_signal.SESSION_TZ)
        enabled_since = self._enabled_since()
        for i in range(max(k - 5, 0), k):
            if not sigs[i]["entry"]:
                continue
            s = sessions[i]
            entry_day = mr_signal.next_business_day(s.date)
            open_ms = int(datetime.strptime(entry_day, "%Y-%m-%d")
                          .replace(hour=9, minute=30, tzinfo=tz).timestamp() * 1000)
            deadline_ms = open_ms + int(mr_signal.ENTRY_WINDOW_H * 3600_000)
            if now_ms <= deadline_ms:
                continue
            if now_ms - deadline_ms > 14 * 86400_000:
                continue
            if enabled_since and deadline_ms / 1000.0 < enabled_since:
                # 停用期间过期的信号不补 SKIPPED（第五轮去噪）。
                continue
            if self.db.mr_trade_by_signal(s.date) is None:
                self.db.mr_mark_skipped(s.date, "窗口错过（停机/延迟）")

    # ------------------------------------------------------------------ 离场

    def _fragment_price(self, result: dict, limit_px: Optional[float],
                        mark: float) -> Tuple[float, str]:
        """部分平仓片段的成交价与来源：回执 avg_px → 限价 → 参考价（标估算）。

        第七轮 B6：旧实现片段价一律取**下单前**的 mark，而卖出 IOC 的限价是
        `mark×(1−15bp)`，真实成交只会 ≥ 限价 —— 于是片段价系统性偏高（最多 15bp），
        且整笔还标 `price_source=avg_px`。这里按证据强弱排序，并且**永远标来源**：
        卖出的限价是「最低可成交价」，用它记片段是保守方向，但仍不是成交价。
        """
        avg = float(result.get("avg_px") or 0.0)
        if avg > 0:
            return avg, "avg_px"
        if limit_px and float(limit_px) > 0:
            return float(limit_px), "limit_px"
        return float(mark or 0.0), "mark_estimated"

    def _append_fragment(self, trade_id: int, *, qty: float, price: float, ts: float,
                         price_source: str, oid: Any = None,
                         cloid: Optional[str] = None) -> None:
        """把一次部分平仓片段写进台账 `raw.partial_exits`（**带价格来源**）。

        2026-11-13 第七轮：价格来源整段交给 `db.mr_trade_append_partial()` —— 它把
        「查 raw → 追加 → 一次 commit」放在同一把 DB 锁里。旧实现在这里拿 `db.conn`
        就地补写一次 UPDATE（读-改-写跨出锁，与并发追加互相覆盖），而且调用方不该
        依赖 `db.conn` 这个内部字段。写失败只记日志：会计口径不受影响，缺的只是
        「这个价是成交还是估算」的审计标记。
        """
        try:
            self.db.mr_trade_append_partial(int(trade_id), qty=float(qty),
                                            price=float(price), ts=float(ts),
                                            price_source=str(price_source) or None,
                                            oid=oid, cloid=cloid)
        except Exception:                                     # noqa: BLE001
            logger.warning("mr: 记录部分成交片段失败", exc_info=True)

    def _resolve_exit_unknown(self, open_row, pos, now: int, pending, status
                              ) -> str:
        """核对「结果未知」的离场尝试 → `closed` / `clear` / `blocked`。

        第七轮：离场 IOC 的 ok=false、或「回执 filled 但读不到仓」，都可能意味着**已经
        成交**。旧实现下一轮直接再发一张 —— reduce_only 不会反手，但这仍是「无证重发」，
        会把成交价/数量归属搞乱。现在先按 cloid 回捞成交：

        * 回捞到成交 → 记片段（真实价）→ 仓若已空则关账（closed）
        * 成交查询可用且为空 → 上一张 IOC 确实没成交，允许本轮重新发（clear）
        * 成交查询不可用 → 不知道 → **在飞窗口内**不发单（blocked，挂 action_required）；
          超过 `_EXIT_UNKNOWN_BLOCK_S` 仍未取证 → 把该尝试收口成 failed 并放行下一张
          （见下面的说明：阻塞上限比「无证重发」更危险）
        """
        sd = str(open_row["signal_date"])
        now_s = now / 1000.0
        for row, data in pending:
            cloid = str(data.get("cloid") or "")
            attempt_sz = abs(float(row["sz"] or 0.0))
            created = float(data.get("ts") or row["created_at"] or 0.0)
            fills, note = self._fills(since_s=created - 60.0, until_s=now_s,
                                      cloid=(cloid or None), close_side=True)
            qty, px, ids = _fill_totals(fills)
            if fills is None:
                if created and now_s - created > _EXIT_UNKNOWN_BLOCK_S:
                    # IOC 在几秒内就定局；过了这个窗口还查不到成交，说明**取证本身**
                    # 不可用（接口长期不可用/权限问题），而不是「单还在飞」。
                    # 继续阻塞 = 仓位无限期裸奔（MR 不挂止损），而下行只是「多一张
                    # reduce_only 单（无仓时被拒）」——不会产生新敞口，也不会反手。
                    self._shadow_finish(
                        self._key_of(row), status=ST_FAILED,
                        error=f"核对超时：{note}（IOC 早已过期，允许下一张离场）")
                    try:
                        self.db.hl_log_event(
                            "mr_exit_unknown_expired",
                            f"MR 离场结果未知超过 {_EXIT_UNKNOWN_BLOCK_S / 60:.0f} 分钟且"
                            f"成交不可查（{note}）→ 收口为 failed，允许下一张离场")
                    except Exception:                     # noqa: BLE001
                        logger.warning("mr: 写 mr_exit_unknown_expired 失败", exc_info=True)
                    continue
                self._action_required(
                    status, kind="exit_result_unknown",
                    reason=(f"上一笔离场结果未知（cloid={cloid or '?'}，{attempt_sz:g} 张）"
                            f"且成交查询不可用（{note}）→ 本轮不重发"),
                    signal_date=sd,
                    evidence={"cloid": cloid, "oid": row["oid"],
                              "attempt_sz": attempt_sz, "fills_api": note})
                return "blocked"
            if qty > 0:
                try:
                    self.db.mr_resolve_exit_fragment(
                        int(open_row["id"]), self._key_of(row), qty=qty, price=px,
                        ts=now / 1000.0, cloid=cloid, oid=row["oid"], fill_ids=ids)
                except Exception:
                    logger.exception("mr: 原子补记成交失败，保留未知尝试")
                    self._action_required(status, kind="exit_fragment_uncommitted",
                                          reason="成交片段未能原子落库，本轮不重发离场",
                                          signal_date=sd)
                    return "blocked"
                try:
                    self.db.hl_log_event(
                        "mr_exit_partial",
                        f"MR 离场结果未知后按成交补记片段 {qty:g} @ {px:.2f}"
                        f"（cloid {cloid or '?'}，fill_ids {ids[:3]}）")
                except Exception:                             # noqa: BLE001
                    logger.warning("mr: 写补记片段事件失败", exc_info=True)
                # 与正常部分离场**同一事件口径**（kind + trade_id:oid/cloid）：
                # 数字取刚落库的片段（qty/price/price_source）与累计片段量，
                # 不用回执/估算值；重复回执（含重启后重放）靠同一键去重。
                frag, cum = None, float(qty)
                try:
                    trade = self.db.mr_open_trade()
                except Exception:                             # noqa: BLE001
                    trade = None
                if trade is not None and int(trade["id"]) == int(open_row["id"]):
                    parts = _partial_exits(trade)
                    cum = sum(abs(float(part.get("qty") or 0.0)) for part in parts)
                    for part in parts:
                        if ((str(row["oid"] or "") and str(part.get("oid") or "")
                             == str(row["oid"] or ""))
                                or (cloid and str(part.get("cloid") or "") == str(cloid))):
                            frag = part
                    if frag is None and parts:
                        frag = parts[-1]
                frag = frag or {}
                self._alert(_mr_partial_exit_recovery_text(
                    coin=self.config.coin,
                    filled=float(frag.get("qty") or qty),
                    cum_qty=cum,
                    ledger_qty=float(open_row["qty"] or 0.0),
                    price=float(frag.get("price") or px),
                    price_source=str(frag.get("price_source") or "fills"),
                    signal_date=sd, trade_id=int(open_row["id"]),
                    recorded_ts=now / 1000.0),
                    kind="mr_fill_exit_partial",
                    key=_partial_event_key(open_row["id"], row["oid"], cloid),
                    fill=True, dedupe=True)
                continue
            # 成交查询可用且为空：上一张 IOC 已不在场
            self._shadow_finish(self._key_of(row), status=ST_FAILED,
                                error="核对：无成交（IOC 已过期）")
        # 片段落库后再看交易所：已空仓 → 走补账关账（证据优先、不限意图年龄）
        left, seen = self._await_position(self.address_fn() or "", want=False,
                                          confirm_s=0.0)
        if not seen:
            self._action_required(
                status, kind="exit_state_unknown",
                reason="核对离场结果时读不到持仓 → 本轮不发单，等下一轮取证",
                signal_date=sd, evidence={"attempts": len(pending)})
            return "blocked"
        if left is None:
            fresh_row = self.db.mr_open_trade() or open_row
            if self._adopt_exit(fresh_row, now, status):
                return "closed"
            return "blocked"
        return "clear"

    def _maybe_exit(self, now: int, sessions, rows, open_row,
                    pos: Optional[dict], status: Dict[str, Any]) -> dict:
        cfg = self.config
        entry_session = str(open_row["entry_session"] or open_row["signal_date"])
        exit_date = mr_signal.exit_decision(sessions=sessions, now_ms=now,
                                            entry_session_date=entry_session)
        status["state"] = "holding"
        mark = self._mark(rows, pos)
        entry_px = float(open_row["entry_price"] or 0.0)
        signal = status.get("signal") or {}
        exit_line = signal.get("exit_line")
        status["pos"] = {
            "signal_date": open_row["signal_date"],
            "entry_session": entry_session,
            "qty": open_row["qty"],
            "entry_price": open_row["entry_price"],
            "notional": open_row["notional"],
            "entry_ts": open_row["entry_ts"],
            "upnl": pos["unrealized_pnl"] if pos else None,
            "funding_paid": None,
            # 面板口径（2026-11-02）：现价 / 浮盈% / 离场线 / 距离场% / 下一次评估
            "mark": mark or None,
            "upnl_pct": ((mark - entry_px) / entry_px * 100.0
                         if mark and entry_px > 0 else None),
            "exit_line": exit_line,
            "dist_to_exit_pct": ((float(exit_line) - mark) / mark * 100.0
                                 if exit_line and mark else None),
            "margin": (pos or {}).get("margin_used"),
            # Kraken 全仓没有逐仓杠杆：给交易所读数（KrInfo 给 None），别写死 5x。
            "leverage": (pos or {}).get("leverage"),
            "liq_price": None,
            "next_eval_et": signal.get("next_eval_et"),
        }
        # ---- 结果未知的上一笔离场尝试：先取证，禁止无证重发（IOC） ----------------
        pending = self._mr_attempts(MR_PURPOSE_EXIT, str(open_row["signal_date"]),
                                    (ST_INTENT, ST_SENT))
        if pending:
            outcome = self._resolve_exit_unknown(open_row, pos, now, pending, status)
            if outcome == "closed":
                return status
            if outcome == "blocked":
                status["state"] = "action_required"
                return status
            open_row = self.db.mr_open_trade() or open_row
            if pos is None:
                return status

        if exit_date is None:
            return status
        if pos is None:
            # 台账有 OPEN 但 xyz dex 还没看到仓（刚下单的宽限窗口）——没有仓可平
            status["state"] = "holding"
            return status
        sz_dec = self._sz_decimals()
        if sz_dec is None:
            status["state"] = "error"
            status["error"] = "szDecimals 未知，无法平仓"
            self._alert("MR 离场受阻：szDecimals 未知（fail-closed，下轮重试）")
            return status
        mark = self._mark(rows, pos)
        if mark <= 0:
            status["state"] = "error"
            status["error"] = "无可用标记价"
            return status
        sz = abs(float(pos["szi"])) if pos else float(open_row["qty"])
        cloid = make_cloid(cfg.coin, _attempt_ms(now), "mr_exit")
        limit_px = self._venue_px(mark * (1 - cfg.slippage_bps / 10000.0), sz_dec)
        self.db.hl_log_event(
            _EXIT_INTENT_KIND,
            f"MR 离场下单意图：{cfg.coin} {sz}（信号日 {open_row['signal_date']}）",
            data={"signal_date": open_row["signal_date"],
                  "exit_signal_date": exit_date, "sz": sz,
                  "ref_price": mark, "cloid": cloid}, ts=now / 1000.0)
        shadow = self._shadow_begin(MR_PURPOSE_EXIT, side=-1, sz=sz, px=limit_px,
                                    reduce_only=True, cloid=cloid,
                                    signal_date=str(open_row["signal_date"]),
                                    now_ms=now)
        result = self.exchange.place_order(
            cfg.coin, False, sz, limit_px, reduce_only=True, tif="Ioc",
            cloid=cloid)
        if str(result.get("status") or "") == "dry":
            status["state"] = "dry_run"
            status["error"] = "影子模式：未真实下单"
            self._shadow_finish(shadow, status=ST_PLANNED, error="dry_run")
            return status
        if not result.get("ok"):
            # 离场失败的两个来源：明确拒单 / 已成交但回执丢了 —— 用交易所持仓分辨
            err = str(result.get("error") or result.get("raw") or "unknown")
            status["error"] = err
            left, seen = self._await_position(self.address_fn() or "",
                                              want=False, confirm_s=0.0)
            if seen and left is None:
                # 已经空仓：按成交/意图证据关账，**绝不重发**
                self._shadow_finish(shadow, status=ST_FILLED, oid=result.get("oid"),
                                    filled_sz=sz, avg_px=result.get("avg_px"),
                                    error="回执失败但交易所已空仓（按证据关账）")
                if self._adopt_exit(open_row, now, status):
                    return status
                status["state"] = "action_required"
                return status
            if not seen:
                # 读不到持仓 = 未知：留影子行在 sent，下一轮先取证
                self._shadow_finish(shadow, status=ST_SENT,
                                    error=f"结果未知：读持仓失败（{err[:140]}）")
                self._action_required(
                    status, kind="exit_result_unknown",
                    reason=("离场下单结果未知（回执失败且持仓读失败）→ 本轮不重发，"
                            "下一轮先按成交/持仓取证"),
                    signal_date=str(open_row["signal_date"]),
                    evidence={"cloid": cloid, "oid": result.get("oid"),
                              "planned_sz": sz, "ref_price": mark,
                              "exchange_error": err[:200]})
                status["state"] = "action_required"
                return status
            # 仍有仓也可能已部分成交；未知回执必须保留，下一轮按 cloid 回捞片段。
            if str(result.get("status") or "") == "unknown":
                self._shadow_finish(shadow, status=ST_SENT, error=err)
                status["state"] = "action_required"
                self._action_required(status, kind="exit_result_unknown",
                                      reason="离场回执未知，仍有仓不代表零成交；等待成交核对",
                                      signal_date=str(open_row["signal_date"]),
                                      evidence={"cloid": cloid, "planned_sz": sz})
                return status
            # 交易所明确拒单才可收口为 failed。
            self._shadow_finish(shadow, status=ST_FAILED, error=err)
            status["state"] = "holding"
            self._alert_throttled("exit_rejected",
                                  f"MR 离场被拒（下轮重试）：{err[:200]}")
            return status
        # 受理 ≠ 成交：必须看到 xyz dex 的仓真的没了才关账（否则台账 CLOSED + 裸仓）
        left, left_seen = self._await_position(self.address_fn() or "", want=False)
        if not left_seen:
            self._shadow_finish(shadow, status=ST_SENT, oid=result.get("oid"),
                                filled_sz=result.get("filled_sz"), avg_px=result.get("avg_px"),
                                error="回执已受理，持仓确认不可读，结果待核对")
            self._action_required(status, kind="exit_result_unknown",
                                  reason="离场回执已返回，但持仓确认失败；保留订单证据，下轮先取证",
                                  signal_date=str(open_row["signal_date"]),
                                  evidence={"cloid": cloid, "oid": result.get("oid")})
            status["state"] = "action_required"
            return status
        # A flat snapshot cannot upgrade a partial receipt into a full ledger close.
        receipt_qty = _finite(result.get("filled_sz"))
        ledger_remaining = max(float(open_row["qty"] or 0.0)
                               - sum(abs(float(p.get("qty") or 0.0))
                                     for p in _partial_exits(open_row)), 0.0)
        if (left is None and (not math.isfinite(receipt_qty) or receipt_qty <= 0
                or abs(receipt_qty - ledger_remaining) > max(ledger_remaining * 1e-9, 1e-9))):
            self._shadow_finish(shadow, status=ST_SENT, oid=result.get("oid"),
                                filled_sz=result.get("filled_sz"), avg_px=result.get("avg_px"),
                                error="receipt quantity does not cover remaining MR ledger")
            self._action_required(status, kind="exit_quantity_conflict",
                                  reason="空仓快照与离场成交数量不守恒；保持OPEN等待成交核对",
                                  signal_date=str(open_row["signal_date"]),
                                  evidence={"cloid": cloid, "filled_sz": receipt_qty,
                                            "remaining_ledger": ledger_remaining})
            status["state"] = "action_required"
            return status
        if (left is not None or not left_seen
                or str(result.get("status") or "") not in ("filled", "partially_filled")):
            filled_here = 0.0
            if left is not None and left_seen:
                # 部分成交：先把已平掉的片段入账（优先真实成交价，其次限价，最后参考价
                # —— 绝不把参考价当成成交价写进去），否则最终决算只按剩余数量算盈亏。
                prev_sz = abs(float(pos["szi"])) if pos else float(open_row["qty"] or 0.0)
                left_sz = abs(float(left.get("szi") or 0.0))
                filled_here = prev_sz - left_sz
                if receipt_qty > 0 and filled_here <= 1e-12:
                    self._shadow_finish(shadow, status=ST_SENT, oid=result.get("oid"),
                                        filled_sz=receipt_qty, avg_px=result.get("avg_px"),
                                        error="execution receipt not yet reflected in position")
                    self._action_required(status, kind="exit_quantity_conflict",
                                          reason="已有成交回执但持仓尚未反映；保留未知订单待核对",
                                          signal_date=str(open_row["signal_date"]))
                    status["state"] = "action_required"
                    return status
                if filled_here > 1e-12:
                    if abs(receipt_qty - filled_here) > max(filled_here * 1e-9, 1e-9):
                        self._shadow_finish(shadow, status=ST_SENT, oid=result.get("oid"),
                                            filled_sz=result.get("filled_sz"), avg_px=result.get("avg_px"),
                                            error="receipt quantity disagrees with position delta")
                        self._action_required(status, kind="exit_quantity_conflict",
                                              reason="成交回执与持仓变化不符；本轮不补记、不重发",
                                              signal_date=str(open_row["signal_date"]))
                        status["state"] = "action_required"
                        return status
                    frag_px, frag_src = self._fragment_price(result, limit_px, mark)
                    try:
                        self.db.mr_resolve_exit_fragment(
                            int(open_row["id"]), shadow,
                            qty=filled_here, price=frag_px, ts=now / 1000.0,
                            price_source=frag_src, oid=result.get("oid"), cloid=cloid)
                    except Exception:
                        logger.exception("mr: 部分成交片段事务失败，保留未知订单")
                        self._shadow_finish(shadow, status=ST_SENT, oid=result.get("oid"),
                                            filled_sz=result.get("filled_sz"),
                                            avg_px=result.get("avg_px"),
                                            error="partial fragment transaction failed")
                        self._action_required(status, kind="exit_fragment_uncommitted",
                                              reason="部分成交片段未能原子落库；本轮不重发离场",
                                              signal_date=str(open_row["signal_date"]),
                                              evidence={"cloid": cloid, "qty": filled_here})
                        status["state"] = "action_required"
                        return status
                    try:
                        self.db.hl_log_event(
                            "mr_exit_partial",
                            f"MR 离场部分成交 {filled_here:g}（剩余 {left_sz:g}）—— "
                            f"片段价 {frag_px:.2f}（来源 {frag_src}）")
                    except Exception:                     # noqa: BLE001
                        logger.warning("mr: 写 mr_exit_partial 事件失败", exc_info=True)
                    # 片段已原子落库 → 给用户一条成交回报（旧实现只写事件，用户看不到
                    # 「平掉多少、还剩多少」）。事件键 = 台账 #id + 本单 oid/cloid：
                    # 同一张单的重复回执落到同一键，不会重复推送。
                    self._alert(_mr_partial_exit_text(
                        coin=cfg.coin, filled=filled_here, remaining=left_sz,
                        entry_px=entry_px, exit_px=frag_px,
                        price_source=frag_src,
                        signal_date=str(open_row["signal_date"]),
                        trade_id=int(open_row["id"]), exec_ts=now / 1000.0),
                        kind="mr_fill_exit_partial",
                        key=_partial_event_key(open_row["id"], result.get("oid"), cloid),
                        fill=True, dedupe=True)
            remain = (left or {}).get("szi", "?") if isinstance(left, dict) else "?"
            self._shadow_finish(
                shadow, status=ST_FILLED if filled_here > 1e-12 else ST_FAILED,
                oid=result.get("oid"), filled_sz=filled_here or None,
                error=f"partial/未确认（剩余 {remain}）")
            status["state"] = "exit_not_filled"
            status["error"] = f"回执 status={result.get('status')!r}，剩余 {remain}"
            self._alert_throttled(
                "exit_not_filled",
                "MR 离场单已受理但交易所仍有持仓或读不到持仓（零成交/部分成交/"
                "确认读失败）——台账保持 OPEN，下轮重试")
            return status
        funding_paid = self._funding_paid(open_row, now)
        notional = float(open_row["notional"] or 0.0)
        # 本次真正平掉的量：回执 filled_sz 优先（部分成交时它比 sz 准），否则按持仓量
        filled = float(result.get("filled_sz") or 0.0) or sz
        covered_px, price_source = self._fragment_price(result, limit_px, mark)
        if price_source != "avg_px":
            # 回执没给成交均价：先按 cloid 回捞真实成交（权威），再退回限价/参考价
            fills, note = self._fills(since_s=now / 1000.0 - 600.0,
                                      until_s=now / 1000.0, cloid=cloid,
                                      close_side=True)
            fq, fpx, _ids = _fill_totals(fills)
            if fq > 0 and fpx > 0:
                covered_px, price_source = fpx, "fills"
        partial_qty_now = sum(abs(float(p.get("qty") or 0.0))
                              for p in _partial_exits(open_row))
        expected = max(float(open_row["qty"] or 0.0) - partial_qty_now, 0.0)
        gap = max(expected - filled, 0.0)          # 台账有量但这次没有成交证据
        st = self._settlement(
            open_row, covered_qty=filled, covered_px=covered_px,
            gap_qty=gap, gap_px=mark, price_source=price_source,
            estimated=(not _price_is_evidence(price_source)),
            funding_paid=funding_paid,
            extra={"exit_ref_price": mark, "limit_px": limit_px,
                   # 用独立键：合并写回时不能把**入场**的 cloid 覆盖掉（审计链要
                   # 同时能看到「哪张单开的」和「哪张单平的」）
                   "exit_cloid": cloid,
                   "oid": str(result.get("oid") or ""),
                   "result_status": str(result.get("status"))})
        funding_pct = (funding_paid / notional * 100.0
                       if (funding_paid is not None and notional > 0) else None)
        self.db.mr_close_trade(
            int(open_row["id"]), exit_price=covered_px,
            exit_order_id=str(result.get("oid") or ""),
            exit_ts=now / 1000.0, exit_reason="sma5", exit_signal_date=exit_date,
            funding_pct=funding_pct, fee_quote=st["fee_est"], net_pnl=st["net_pnl"],
            raw=st["raw"])
        self._shadow_finish(shadow, status=ST_FILLED, oid=result.get("oid"),
                            filled_sz=filled, avg_px=covered_px)
        if gap > 1e-9:
            # 缺口不静默：净利已标 net_pnl_complete=False，再喊一声人工核对
            self._action_required(
                status, kind="exit_settle_gap",
                reason=(f"关账数量核对不平：台账 {open_row['qty']:g} − 已记片段 "
                        f"{partial_qty_now:g} = {expected:g}，本次成交证据只有 {filled:g}；"
                        f"缺口 {gap:g} 张按 mark {mark:.2f} 估算，净利标记为未完成"),
                signal_date=str(open_row["signal_date"]),
                evidence={"ledger_qty": open_row["qty"], "partial_qty": partial_qty_now,
                          "filled_qty": filled, "gap_qty": gap,
                          "reconciliation": st["reconciliation"]})
        self._slippage_guard(result, mark, side="离场")
        self._funding_guard(funding_pct, float(open_row["entry_ts"] or 0.0),
                            now / 1000.0)
        status["state"] = "exited"
        status["last_fill_at"] = now / 1000.0
        status["net_pnl"] = st["net_pnl"]
        status["net_pnl_complete"] = st["net_pnl_complete"]
        self._alert(_mr_exit_text(
            coin=cfg.coin, filled=filled,
            ledger_qty=float(open_row["qty"] or 0.0),
            partial_qty=partial_qty_now, gap_qty=gap,
            entry_px=float(open_row["entry_price"] or 0.0),
            exit_px=covered_px,
            entry_ts=float(open_row["entry_ts"] or 0.0),
            now_s=now / 1000.0,
            net_pnl=st["net_pnl"], fee_est=st["fee_est"],
            complete=bool(st["net_pnl_complete"]),
            funding_paid=funding_paid, price_source=price_source,
            signal_date=exit_date),
            kind="mr_fill_exit", key=int(open_row["id"]), fill=True, dedupe=True)
        return status

    def _funding_paid(self, open_row, now_ms: int) -> Optional[float]:
        """持仓期实际支付的资金费（USD，正=已付）。

        `None` = **不可用**（交易所不支持资金费流水 —— Kraken `funding_supported=False`
        恒返回 []，或这次读取失败）。绝不返回 0.0 冒充「没花钱」：文案会把它写成
        「不可用、未计入」，台账侧仍按 0 记账（会计口径不变）。
        """
        cfg = self.config
        if getattr(self.info, "funding_supported", True) is False:
            return None
        try:
            start_ms = int(float(open_row["entry_ts"]) * 1000) - 3600_000
            rows = self.info.user_funding_history(self.address_fn(), start_ms, now_ms)
            total = 0.0
            bare = cfg.coin.split(":")[-1]
            for item in rows or []:
                delta = item.get("delta") or {}
                # HIP-3 的 coin 命名两可（`xyz:XYZ100` 或裸 `XYZ100`），两种都认；
                # 实测主 dex 记录给的是裸名，HIP-3 无历史可证 ⇒ 不做单边假设。
                if (delta.get("type") == "funding"
                        and delta.get("coin") in (cfg.coin, bare)):
                    total += float(delta.get("usdc") or 0.0)
            return -total          # HL：usdc 负 = 多头支付 → 取反变「已付为正」
        except Exception as exc:                              # noqa: BLE001
            logger.warning("mr: funding history failed: %s", exc)
            return None


def _raw_dict(row: Any) -> Dict[str, Any]:
    """台账 `raw` 列（JSON 字符串或 dict）→ dict；解析失败返回 {}。

    注意行可能是 `sqlite3.Row`（不是 Mapping，没有 `.get`）—— 统一走下标。
    补账写回时必须基于它**合并**：`mr_close_trade` 用 `raw=COALESCE(?, raw)`
    整体覆盖，直接给一个新 dict 会把 `partial_exits` 等历史字段抹掉。
    """
    if isinstance(row, Mapping):
        raw = row.get("raw")
    else:
        try:
            raw = row["raw"]
        except (KeyError, IndexError, TypeError):
            raw = None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, Mapping):
        return {}
    return dict(raw)


def _partial_exits(row: Any) -> List[dict]:
    """台账 `raw.partial_exits`（部分成交片段）→ list；解析失败返回 []。

    注意行可能是 `sqlite3.Row`（不是 Mapping，没有 `.get`）—— 统一走下标。
    """
    raw = _raw_dict(row)
    parts = raw.get("partial_exits")
    if not isinstance(parts, list):
        return []
    return [dict(p) for p in parts if isinstance(p, Mapping)]


def _fill_totals(fills: Optional[List[dict]]) -> Tuple[float, float, List[str]]:
    """成交列表 → (Σ数量, VWAP, 成交号前若干个)。

    `fills is None`（读不到）与 `[]`（确认没有）在这里都得到 (0,0,[])，区别由调用方
    用 `_fills()` 返回的第二个值自己判 —— 这个函数只做算术，不做「有没有」的判断。
    """
    qty = 0.0
    notional = 0.0
    ids: List[str] = []
    for f in fills or []:
        if not isinstance(f, Mapping):
            continue
        try:
            sz = abs(float(f.get("sz") or 0.0))
            px = float(f.get("px") or 0.0)
        except (TypeError, ValueError):
            continue
        if sz <= 0 or px <= 0:
            continue
        qty += sz
        notional += sz * px
        tid = str(f.get("tid") or f.get("oid") or "")
        if tid:
            ids.append(tid)
    return qty, (notional / qty if qty else 0.0), ids


def _event_data(raw: Any) -> Dict[str, Any]:
    """事件 `data` 列（JSON 字符串或 dict）→ dict；解析失败返回 {}。"""
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _attempt_ms(now_ms: int) -> int:
    """按「当前 tick 分钟」派生 cloid 键：同一轮重放同键，跨轮重试换键。

    交易所会拒**已用过**的 cloid；固定用信号日期会让零成交后的重试永远被拒。
    真正的幂等护栏是台账 `mr_trades.signal_date` 唯一约束 + 只在空仓时入场。
    """
    return max(int(now_ms) // 60_000, 1)


# ------------------------------------------------------------------ 面板视图（只读）

def _next_eval_et(now_ms: int) -> str:
    """下一次「收盘评估」的 ET 时刻（ISO，带时区）。"""
    tz = ZoneInfo(mr_signal.SESSION_TZ)
    now = datetime.fromtimestamp(now_ms / 1000.0, tz=tz)
    day = now.date()
    close = datetime.combine(day, _at_time(16, 0), tzinfo=tz)
    if now >= close or day.weekday() >= 5 or mr_signal.is_market_holiday(day):
        day = datetime.strptime(mr_signal.next_business_day(day.isoformat()),
                                "%Y-%m-%d").date()
        close = datetime.combine(day, _at_time(16, 0), tzinfo=tz)
    return close.isoformat()


def _entry_window(sessions, now_ms: int):
    """待执行信号的入场窗口；没有待执行信号 → None。"""
    k = mr_signal.completed_upto(sessions, now_ms)
    if k <= 0:
        return None
    sig = mr_signal.session_signals(sessions[:k])[k - 1]
    if not sig.get("entry"):
        return None
    # 窗口从 09:30 ET 起算（不是 09:00）：09:30 + 3h = 12:30。
    end_min = 9 * 60 + 30 + int(round(mr_signal.ENTRY_WINDOW_H * 60))
    return {"date": mr_signal.next_business_day(sessions[k - 1].date),
            "start_et": "09:30",
            "deadline_et": f"{end_min // 60}:{end_min % 60:02d}"}


def _phase(sessions, now_ms: int) -> str:
    """no_signal / signal_ready / in_window / expired / no_data（disabled/holding 由面板叠加）。"""
    if not sessions:
        return "no_data"
    k = mr_signal.completed_upto(sessions, now_ms)
    if k <= 0:
        return "no_data"
    last = sessions[k - 1]
    sig = mr_signal.session_signals(sessions[:k])[k - 1]
    tz = ZoneInfo(mr_signal.SESSION_TZ)
    now = datetime.fromtimestamp(now_ms / 1000.0, tz=tz)
    today = now.strftime("%Y-%m-%d")
    entry_day = mr_signal.next_business_day(last.date)
    if not sig.get("entry"):
        return "expired" if entry_day < today else "no_signal"
    if entry_day > today:
        return "signal_ready"
    if entry_day < today:
        return "expired"
    open_ms = int(datetime.strptime(today, "%Y-%m-%d")
                  .replace(hour=9, minute=30, tzinfo=tz).timestamp() * 1000)
    deadline_ms = open_ms + int(mr_signal.ENTRY_WINDOW_H * 3600_000)
    if now_ms < open_ms:
        return "signal_ready"
    if now_ms <= deadline_ms:
        return "in_window"
    return "expired"
