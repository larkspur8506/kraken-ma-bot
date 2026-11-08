"""KrInfo：Kraken Futures 账户/行情读取，鸭子类型对齐 app/hl/info.HlInfo。

snapshot() 返回**冻结接口**（与 HlInfo.snapshot 同形状，另含统一账户兼容键）：
{ts, address, equity, equity_total, withdrawable, margin_used, notional,
 positions[{coin,side,size,entry_px,mark_px,position_value,liq_px,leverage,
            isolated,margin_used,unrealized_pnl,roe,funding_since_open}],
 open_orders[{coin,oid,is_buy,sz,limit_px,reduce_only,trigger_px,is_trigger,timestamp}],
 unified, spot_usdc}

已知数据缺口（2026-11-08，OpenAPI 逐字段核对）：
- /fills 不带 fee / reduceOnly，但**带 realized_pnl 与 sequence_id**：平仓盈亏优先
  用交易所真值，历史页（lastFillTime 路径）为 null 时才按净仓推演；fee 缺失按
  taker 估并标 fee_estimated，`feeToken` 恒为 "USD"（fee 已折算成 USD 金额）。
  dir/reduce_only 仍由成交序列推演，**dir 用 HL 原生大小写**（"Open Long" /
  "Close Long" / "Open Short" / "Close Short"）—— executor、pnl、面板的
  `"Long" in dir` / `"Close" in dir` / `_FILL_BS` 精确键全按这个契约写。
- 无公开的逐账户资金费/出入金端点 → user_funding_history / ledger_updates 返回
  空列表（面板资金费汇总与净入金推演会缺数，属已知 PR-3 事项）。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional

from app import symbol_registry as registry
from .auth import KrAuthError
from .client import KrEndpoints
from app.hl.info import HlInfoError as _HlInfoError

logger = logging.getLogger(__name__)

# taker 费率兜底（fee 字段缺失时的估算口径；实测 schedule 见
# docs/KRAKEN_MIGRATION_PLAN.md —— crypto/股票永续 Tier1 taker 0.05%）。
# Kraken Futures 手续费（**不是 HL 的 5bp**）。用户当前 6 级：taker 0.0275% / maker 0.0075%；
# 长期掉回 Tier 1 时 taker 0.05%。这里取 6 级 taker 作默认（真实费率目前无法从 API 读取：
# /fills 不带 fee 字段），要改就改 KR_TAKER_FEE / KR_MAKER_FEE，别在代码里硬编码第二次。
DEFAULT_TAKER_FEE = 0.000275
DEFAULT_MAKER_FEE = 0.000075

#: 读不到 /instruments 的 marginLevels 时的初始保证金率兜底（10x）。
#: **不放宽**到 100x：保证金宁可高估，ROE 宁可低估——编一个松的费率会让
#: 面板看起来比真实账户健康。
DEFAULT_INITIAL_MARGIN = 0.10


def _inst_initial_margin(i: dict) -> float:
    """合约的**散户**初始保证金率（min 档 = 最大杠杆那一档）。

    Kraken 的 marginLevels 是按持仓档位阶梯的：第 0 档（numNonContractUnits=0）
    initialMargin 最小 = 能开的最大杠杆。BTC/ETH/SOL=0.01（≈100x）、
    QQQX=0.05（≈20x）。retailMarginLevels 是同一张表对散户的口径，优先它。
    """
    rows = i.get("retailMarginLevels") or i.get("marginLevels") or []
    best = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        v = _num(r.get("initialMargin"))
        if v > 0 and (best <= 0 or v < best):
            best = v
    if best <= 0:
        best = _num(i.get("initialMargin"))
    return best


class KrInfoError(KrAuthError, _HlInfoError):
    """对齐 HlInfoError 的语义：读失败 = 数据不可信，调用方 fail-closed。

    同时继承 HL 的 `HlInfoError` 作为 venue 无关基类——executor/面板里的
    `except HlInfoError` 旧消费方在 PR-1 切 KrInfo 后必须继续接得住（迁移桥接）。
    """


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _decimals_of(step: float) -> int:
    if step <= 0:
        return 8
    d = 0
    while d < 18 and abs(round(step, d) - step) > 1e-12:
        d += 1
    return d


def _iso_ms(v: Any) -> int:
    """Kraken 的时间字段是 RFC3339 字符串（"2026-11-08T08:27:31.814Z"）。

    对抗审查 P0：旧代码用 `_num()` 直接 float() 解析 → 恒 0，成交/挂单时间全部
    塌成 0，`user_fills_by_time` 按时段过滤后永远为空。这里兼容 ISO 与毫秒数。
    """
    if isinstance(v, (int, float)):
        f = float(v)
        return int(f) if math.isfinite(f) and f > 0 else 0
    s = str(v or "").strip()
    if not s:
        return 0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:                                            # noqa: BLE001
        return 0


def _flex(acct: dict) -> dict:
    """/account 的余额在 `accounts.flex`（multi-collateral）。

    对抗审查 P0：旧代码把 `acct.get("pv") / acct.get("portfolioValue")` 打在**顶层
    响应**上，真实响应是 `{"result","accounts":{"flex":{...}}}` → equity 恒 0，
    `account_equity()` 直接 fail-closed，MR 腿定仓全废、面板权益全 0。
    同时兼容单币保证金账户（accounts.fi_xxx.auxiliary.pv / marginRequirements.im）。
    """
    accounts = acct.get("accounts") if isinstance(acct, dict) else None
    if isinstance(accounts, dict):
        flex = accounts.get("flex")
        if isinstance(flex, dict) and flex:
            return flex
        for v in accounts.values():
            if not isinstance(v, dict):
                continue
            aux = v.get("auxiliary") or {}
            mr = v.get("marginRequirements") or {}
            if aux or mr:
                pv = _num(aux.get("pv"))
                return {"portfolioValue": pv, "balanceValue": pv,
                        "availableMargin": _num(aux.get("af")),
                        "initialMargin": _num(mr.get("im")),
                        "pnl": _num(aux.get("pnl")),
                        "marginEquity": pv}
    # 扁平形状（老单元测试/兼容）：直接当已归一字典
    return acct if isinstance(acct, dict) else {}


def _acct_metrics(acct: dict) -> dict:
    """统一取出 equity / withdrawable / margin_used（flex 口径）。"""
    f = _flex(acct)
    equity = _num(f.get("portfolioValue"))
    if equity <= 0:
        equity = _num(f.get("marginEquity"))
    if equity <= 0:
        equity = _num(f.get("balanceValue"))
    if equity <= 0:                                  # 扁平/老形状兜底（pv/av/im）
        equity = _num(f.get("pv"))
    avail = _num(f.get("availableMargin"))
    if avail <= 0:
        avail = _num(f.get("av"))
    im = _num(f.get("initialMargin"))
    if im <= 0:
        im = _num(f.get("initialMarginWithOrders"))
    if im <= 0:
        im = _num(f.get("im"))
    return {"equity": equity, "withdrawable": avail, "margin_used": im,
            "unrealized": _num(f.get("pnl"))}


class KrInfo:
    #: Kraken 无钱包地址概念——views 的快照门卫据此放行空 address（HL 靠 address
    #: 空值拦「误导性主网数据」，Kraken 不存在该问题，照拦会把面板永远挡在门外）。
    venue_address_free = True

    def __init__(self, api_key: str = "", secret_b64: str = "", base_url: str = "",
                 timeout: float = 15.0, endpoints: Any = None,
                 max_leverage: Optional[Dict[str, int]] = None,
                 taker_fee: Optional[float] = None,
                 maker_fee: Optional[float] = None):
        if endpoints is not None:
            self._ep = endpoints
        else:
            from .auth import KrClient, DEFAULT_BASE
            self._ep = KrEndpoints(KrClient(
                api_key=api_key, secret_b64=secret_b64,
                base_url=base_url or DEFAULT_BASE, timeout=timeout))
        self._max_lev = dict(DEFAULT_MAX_LEVERAGE_OVERRIDE or max_leverage or {})
        #: 计费口径（用于 fills 的 fee 字段与面板「净手续费」）。Kraken 的 /fills 不带
        #: fee，拿不到真实值，只能按账号费率档配置；默认 6 级 taker 2.75bp。
        self._taker_fee = float(taker_fee if taker_fee is not None else DEFAULT_TAKER_FEE)
        self._maker_fee = float(maker_fee if maker_fee is not None else DEFAULT_MAKER_FEE)
        self._specs_cache: Optional[Dict[str, dict]] = None
        self._specs_ts = 0.0

    # ---------------------------------------------------------------- 行情
    def instruments(self) -> List[dict]:
        return self._ep.instruments()

    def specs(self, coins: Optional[List[str]] = None) -> Dict[str, dict]:
        """{coin: {"sz_decimals": int, "szDecimals": int, "price_tick": float,
                  "price_decimals": int, "initial_margin": float,
                  "max_leverage": int, "maxLeverage": int}}

        数量精度取 `contractValueTradePrecision`（XBT=4 / ETH=3 / SOL=2 / QQQX=3）。
        **不能**用 `tickSize`：那是价格 tick（XBT=1），当数量步长会 floor 成整数张
        = 1 BTC ≈ $75k 一张，1.33% 风险仓位直接变成 0 或 1 张（对抗审查 P0）。
        兼容键：executor/agent 读 `sz_decimals`，MR 腿读 `szDecimals` —— 两个都给。

        杠杆/保证金（对抗审查 P1-4）：`initial_margin` = /instruments 的
        retailMarginLevels/marginLevels 最小档 initialMargin（散户口径），
        `max_leverage` = round(1/initial_margin) 的**等效**值（BTC/ETH/SOL 100、
        QQQX 20）。旧代码硬编码 5x + value/5 的 HL 逐仓公式，把面板保证金高估
        20 倍、ROE 缩小 20 倍。构造参数 `max_leverage` 显式覆盖仍优先。
        """
        now = time.time()
        if self._specs_cache is None or now - self._specs_ts > 3600:
            out: Dict[str, dict] = {}
            for i in self.instruments():
                coin = registry.kr_coin(str(i.get("symbol") or ""))
                if not coin:
                    continue
                qty_dp = i.get("contractValueTradePrecision")
                sz_dec = int(qty_dp) if isinstance(qty_dp, (int, float)) \
                    and int(qty_dp) >= 0 else _decimals_of(_num(i.get("minimumTradeSize"), 0.0))
                tick = _num(i.get("tickSize"), 0.0)
                im = _inst_initial_margin(i)
                override = int(self._max_lev.get(coin, 0) or 0)
                if override > 0:
                    lev = override
                    if im <= 0:
                        im = 1.0 / override
                elif im > 0:
                    lev = max(1, int(round(1.0 / im)))
                else:
                    lev = max(1, int(round(1.0 / DEFAULT_INITIAL_MARGIN)))
                    im = DEFAULT_INITIAL_MARGIN
                out[coin] = {"sz_decimals": sz_dec, "szDecimals": sz_dec,
                             "price_tick": tick, "price_decimals": _decimals_of(tick),
                             "contract_size": _num(i.get("contractSize"), 0.0) or 1.0,
                             "initial_margin": im,
                             "max_leverage": lev, "maxLeverage": lev}
            if out:
                self._specs_cache = out
                self._specs_ts = now
        cache = self._specs_cache or {}
        if coins is None:
            return dict(cache)
        return {c: cache[c] for c in coins if c in cache}

    def _initial_margin(self, coin: str) -> float:
        """该合约的初始保证金率（真实 IM；读不到 → DEFAULT_INITIAL_MARGIN）。

        specs() 有 1h 缓存，这里只是取缓存；/instruments 抖动时不能把持仓
        保证金算成 0（会把 ROE 除零 / 把占用算没），所以兜底而不是抛。
        """
        try:
            sp = self.specs([coin]).get(coin) or {}
        except Exception:                                        # noqa: BLE001
            sp = {}
        im = _num(sp.get("initial_margin"), 0.0)
        return im if im > 0 else DEFAULT_INITIAL_MARGIN

    def price_tick(self, coin: str) -> float:
        """该合约的价格最小变动（Kraken tickSize）。读不到返回 0（调用方拒单）。"""
        return _num((self.specs([coin]).get(coin) or {}).get("price_tick"), 0.0)

    def contract_size(self, coin: str) -> float:
        """合约面值（1 张 = 多少币）。本系统按币量下单，非 1 时调用方必须拒单。"""
        v = _num((self.specs([coin]).get(coin) or {}).get("contract_size"), 0.0)
        if v <= 0:
            raise KrInfoError(f"{coin} 取不到 contractSize（fail-closed）")
        return v

    def round_price(self, coin: str, px: float) -> float:
        """价格取整到 Kraken tickSize 的整数倍（最近一档）。

        对抗审查 P0：执行器原先对所有场所套 HL 规则（≤5 位有效数字），
        PF_XBTUSD 的 tick=1 会拒 `75798.3` 这类价（invalidPrice）；
        Kraken 只接受 tick 整数倍，必须按 venue 自己的 tick 取整。
        """
        tick = self.price_tick(coin)
        if tick <= 0:
            raise KrInfoError(f"{coin} 取不到 Kraken tickSize（拒单，fail-closed）")
        if px <= 0:
            raise KrInfoError(f"{coin} 价格必须为正：{px}")
        from decimal import Decimal, ROUND_HALF_UP
        d = Decimal(str(px)) / Decimal(str(tick))
        n = d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return float(n * Decimal(str(tick)))

    def all_mids(self, dex: str = "") -> Dict[str, float]:
        out: Dict[str, float] = {}
        for t in self._ep.tickers():
            coin = registry.kr_coin(str(t.get("symbol") or ""))
            if coin:
                out[coin] = _num(t.get("markPrice"))
        return out

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> List[dict]:
        """HL candleSnapshot 形状：[{t(ms),o,h,l,c,v}]。interval 用 4h/1h/1d。"""
        sym = registry.kr_symbol(coin)
        if not sym:
            raise KrInfoError(f"未知 Kraken 合约：{coin}")
        raw = self._ep.charts_history(sym, interval, int(start_ms // 1000) - 1,
                                      int(end_ms // 1000) + 1)
        return [{"t": int(c["time"]), "o": _num(c["open"]), "h": _num(c["high"]),
                 "l": _num(c["low"]), "c": _num(c["close"]), "v": _num(c["volume"])}
                for c in raw]

    def funding_history(self, coin: str, start_ms: int, end_ms: Optional[int] = None) -> List[dict]:
        """无公开逐账户资金费端点（PR-3 再议口径），返回空。"""
        return []

    # ---------------------------------------------------------------- 账户
    def user_state(self, address: str) -> dict:
        """归一 /account + /openpositions 成 HL clearinghouseState 近似形状。

        assetPositions 用 **HL 原生形状**（position.coin/szi/entryPx/…）——
        MR 腿（HlMrLeg._read_account）与旧 HL 消费方都按这个形状读仓。
        """
        try:
            acct = self._ep.account() or {}
        except KrApiErrorWrapper as e:
            raise KrInfoError(f"account 读取失败：{e}") from e
        m = _acct_metrics(acct)
        positions = self._positions(mids=None)
        notional = sum(p["position_value"] for p in positions)
        asset_positions = [{
            "position": {"coin": p["coin"], "szi": p["side"] * p["size"],
                         "entryPx": p["entry_px"], "positionValue": p["position_value"],
                         "unrealizedPnl": p["unrealized_pnl"],
                         "leverage": {"type": "cross", "marginUsed": p["margin_used"]}},
        } for p in positions]
        return {"marginSummary": {"accountValue": m["equity"], "withdrawable": m["withdrawable"],
                                  "totalMarginUsed": m["margin_used"],
                                  "marginUsed": m["margin_used"],   # MR 腿读的键
                                  "totalNtlPos": notional},
                "assetPositions": asset_positions,
                "withdrawable": m["withdrawable"]}

    def user_state_dex(self, address: str, dex: str) -> dict:
        """Kraken 无 builder dex 概念；同账户同仓，原样返回 user_state。

        marginSummary 保持全账户口径——MR 腿的定仓占用走 position_value，
        `mr_margin_reserved` 展示项在 Kraken 下与全账户 im 同值（无 dex 拆分），
        不是 bug；面板文案已按统一账户口径写。
        """
        return self.user_state(address)

    def perp_meta_dex(self, dex: str) -> list:
        """mr.py 只用它取 szDecimals/maxLeverage——返回 meta.universe 近似形状。"""
        specs = self.specs()
        return [{"name": c, "szDecimals": s["szDecimals"], "maxLeverage": s["maxLeverage"]}
                for c, s in specs.items()]

    def spot_state(self, address: str) -> dict:
        try:
            acct = self._ep.account() or {}
        except KrApiErrorWrapper as e:
            raise KrInfoError(f"account 读取失败：{e}") from e
        return {"balances": [{"asset": "USD", "total": _acct_metrics(acct)["equity"]}]}

    def account_equity(self, address: str) -> float:
        """MR 腿定仓基数：单端点口径 = /account flex portfolioValue。失败抛 KrInfoError。"""
        try:
            acct = self._ep.account() or {}
        except KrApiErrorWrapper as e:
            raise KrInfoError(f"account 读取失败：{e}") from e
        pv = _acct_metrics(acct)["equity"]
        if pv <= 0:
            raise KrInfoError("account portfolioValue ≤ 0（数据不可信，fail-closed）")
        return pv

    def _positions(self, mids: Optional[Dict[str, float]]) -> List[dict]:
        """Kraken OpenPositionJson：symbol/side(long|short)/size/price/unrealizedPnl/
        unrealizedFunding。**没有** netQty/avgFillPrice/markPrice（对抗审查 P0：
        旧代码读 netQty → 恒 0 → `if qty == 0: continue` 把所有持仓当空仓丢弃，
        对账会把真实仓位判成「交易所无仓」）。
        """
        out: List[dict] = []
        try:
            rows = self._ep.openpositions()
        except KrApiErrorWrapper as e:
            raise KrInfoError(f"openpositions 读取失败：{e}") from e
        for item in rows:
            sym = str(item.get("symbol") or "")
            coin = registry.kr_coin(sym)
            if not coin:
                continue
            side_txt = str(item.get("side") or "").strip().lower()
            size = abs(_num(item.get("size")))
            if size <= 0:
                continue
            if side_txt not in ("long", "short"):
                # 有仓却读不出方向 = 数据不可信，宁可整轮 fail-closed
                raise KrInfoError(f"{sym} 持仓方向缺失（side={side_txt!r}）")
            side = 1 if side_txt == "long" else -1
            entry = _num(item.get("price"))
            upnl = _num(item.get("unrealizedPnl"))
            mark = _num((mids or {}).get(coin))
            if mark <= 0 and entry > 0:
                # Kraken 不返回标记价：用 未实现盈亏 反推（upnl = (mark-entry)*signed_qty）
                mark = entry + upnl / (side * size)
            if mark <= 0:
                mark = entry
            value = mark * size
            # 真实初始保证金 = 名义 × 初始保证金率（/instruments 的 IM，散户口径）。
            # 旧代码 value/5 是 HL 逐仓公式：BTC 真实 IM=1% → 保证金高估 20 倍、
            # ROE 缩小 20 倍（对抗审查 P1-4）。
            im = self._initial_margin(coin)
            margin_est = value * im
            out.append({
                "coin": coin, "side": side, "size": size, "entry_px": entry,
                "mark_px": mark, "position_value": value,
                "liq_px": _num(item.get("liquidationThresholdPrice")),  # 全仓口径无逐仓强平价
                # 全仓无「逐仓杠杆」这个量：给 None 让面板显示「全仓」，
                # 而不是编一个 100x 冒充用户设置的杠杆（跨组契约，2026-11-08）。
                "leverage": None, "isolated": False,          # flex 只有 cross
                "initial_margin": im,
                "margin_used": margin_est,                    # 真实 IM（全仓无法逐仓拆分）
                "margin_used_estimated": 1,
                "unrealized_pnl": upnl,
                "roe": (upnl / margin_est) if margin_est > 0 else 0.0,
                "funding_since_open": _num(item.get("unrealizedFunding")
                                           or item.get("realizedFundingPnl")),
            })
        return out

    def open_orders(self, address: str) -> List[dict]:
        return self._orders()

    def open_orders_checked(self, address: str) -> tuple:
        try:
            return self._orders(), True
        except KrInfoError:
            return [], False

    def _orders(self) -> List[dict]:
        """OpenOrderJson：order_id / side / unfilledSize / limitPrice / stopPrice /
        reduceOnly / orderType / receivedTime(ISO)。**没有** orderId/qty/size（对抗审查
        P0：旧代码 oid 恒空、挂单量恒 0 —— 止损单对账与撤单全部失效）。
        """
        try:
            rows = self._ep.openorders()
        except KrApiErrorWrapper as e:
            raise KrInfoError(f"openorders 读取失败：{e}") from e
        out: List[dict] = []
        for o in rows:
            sym = str(o.get("symbol") or "")
            coin = registry.kr_coin(sym)
            if not coin:
                continue
            stop = _num(o.get("stopPrice"))
            otype = str(o.get("orderType") or "").strip().lower()
            is_buy = str(o.get("side") or "").lower() == "buy"
            reduce_only = bool(o.get("reduceOnly"))
            sz = _num(o.get("unfilledSize"))
            if sz <= 0:
                sz = _num(o.get("size") or o.get("qty"))
            out.append({
                "coin": coin, "oid": str(o.get("order_id") or o.get("orderId") or ""),
                "is_buy": is_buy, "sz": sz,
                "limit_px": _num(o.get("limitPrice")),
                "reduce_only": 1 if reduce_only else 0,
                "trigger_px": stop if stop > 0 else None,
                "is_trigger": 1 if (stop > 0 or otype in ("stp", "stop", "take_profit")) else 0,
                "order_type": otype,
                "timestamp": _iso_ms(o.get("receivedTime")) or _num(o.get("timestamp")),
            })
        return out

    # ---------------------------------------------------------------- 成交
    def user_fills(self, address: str) -> List[dict]:
        return self._fills()

    def user_fills_by_time(self, address: str, start_ms: int,
                           end_ms: Optional[int] = None, **_) -> List[dict]:
        fills = self._fills()
        if start_ms:
            fills = [f for f in fills if f["time"] >= int(start_ms)]
        if end_ms:
            fills = [f for f in fills if f["time"] <= int(end_ms)]
        return fills

    def _fills(self) -> List[dict]:
        """全量成交（按 lastFillTime 向前翻页），并推演 dir/reduce_only/closedPnl。

        真实 FillJson：fill_id / fillTime(ISO 字符串) / fillType / price / size /
        side / symbol / order_id / sequence_id / realized_pnl。**没有** orderId/seq/
        fee（对抗审查 P0：旧代码把 ISO 时间 float() 成 0、orderId 读空、且只取最近
        100 条不分页 —— 面板成交记录空、按时段过滤全空、净仓推演起点错）。

        分页语义（官方）：不传参 = 最近 100 条（带 realized_pnl）；传 lastFillTime =
        该时间**之前**的 100 条（历史页 realized_pnl 为 null，用推演补）。

        输出 dir 用 **HL 原生格式**（"Open Long"/"Close Long"/"Open Short"/
        "Close Short"），反手成交按被平掉的旧仓方向给 Close Long/Close Short。
        `feeToken` 恒 "USD"（fee 已是 USD 金额），`reduce_only` 1=平仓腿。
        """
        raw: List[dict] = []
        seen = set()
        cursor: Optional[str] = None
        for _ in range(20):                       # 上限 2000 条，防死循环
            try:
                batch = self._ep.fills(cursor)
            except KrApiErrorWrapper as e:
                raise KrInfoError(f"fills 读取失败：{e}") from e
            if not batch:
                break
            for f in batch:
                fid = str(f.get("fill_id") or
                          f"{f.get('order_id')}:{f.get('fillTime')}:{f.get('size')}:{f.get('price')}")
                if fid in seen:
                    continue
                seen.add(fid)
                raw.append(f)
            if len(batch) < 100:
                break
            times = [str(f.get("fillTime")) for f in batch if f.get("fillTime")]
            if not times:
                break
            nxt = min(times)
            if cursor is not None and nxt >= cursor:
                break
            cursor = nxt
        raw.sort(key=lambda f: (_iso_ms(f.get("fillTime")), _num(f.get("sequence_id"))))
        net: Dict[str, float] = {}
        avg: Dict[str, float] = {}
        out: List[dict] = []
        for f in raw:
            coin = registry.kr_coin(str(f.get("symbol") or ""))
            if not coin:
                continue
            px = _num(f.get("price"))
            sz = abs(_num(f.get("size")))
            if sz <= 0:
                continue
            is_buy = str(f.get("side") or "").lower() == "buy"
            signed = sz if is_buy else -sz
            old = net.get(coin, 0.0)
            new = old + signed
            old_side = 1 if old > 0 else (-1 if old < 0 else 0)
            new_side = 1 if new > 0 else (-1 if new < 0 else 0)
            t = _iso_ms(f.get("fillTime"))
            oid = str(f.get("order_id") or f.get("orderId") or "")
            tid = str(f.get("fill_id") or f.get("sequence_id") or f"{t}-{len(out)}")
            close_qty = 0.0
            if old != 0 and (new == 0 or (old * new < 0) or
                             (old_side == new_side and abs(new) < abs(old))):
                close_qty = min(abs(old), sz)
                closed = (px - avg.get(coin, px)) * close_qty * old_side
            else:
                closed = 0.0
            if old == 0 or old * new <= 0:
                avg[coin] = px
            elif abs(new) > abs(old):
                total = abs(old) + (sz - close_qty)
                if total > 0:
                    avg[coin] = (avg.get(coin, px) * abs(old) + px * (sz - close_qty)) / total
            net[coin] = new
            if new == 0:
                net.pop(coin, None)
                avg.pop(coin, None)
            if old * new < 0:
                # 反手：这笔成交里有一部分在平旧仓 —— 对下游记账必须算「平仓腿」
                # （reduce_only=1、closedPnl 归到旧仓），所以 dir 取被平掉的方向。
                direction = "Close Long" if old > 0 else "Close Short"
            elif close_qty > 0:
                direction = "Close Long" if old > 0 else "Close Short"
            else:
                direction = "Open Long" if is_buy else "Open Short"
            # realized_pnl 是交易所真值（历史页为 null），null 时才用推演值
            rp = f.get("realized_pnl")
            if rp is None:
                closed_val, closed_src = closed, "derived"
            else:
                closed_val, closed_src = _num(rp), "exchange"
            fee_usd = sz * px * self._taker_fee
            out.append({
                "coin": coin, "px": px, "sz": sz, "side": "buy" if is_buy else "sell",
                "time": t, "hash": f"kr:{tid}", "dir": direction,
                "closedPnl": closed_val, "closedPnl_src": closed_src,
                # fee 已折算成 USD（Kraken flex 全额 USD 计价），feeToken 必须给
                # "USD"：executor._fee_quote 只认 QUOTE_FEE_TOKENS，缺这个键就
                # 每笔平仓刷「手续费以 未知币种 计价」并把 fee_quote 记成 NULL。
                "fee": fee_usd, "feeToken": "USD",
                "fee_estimated": 1, "tid": tid, "oid": oid,
                "reduce_only": 1 if close_qty > 0 else 0,
            })
        return out

    #: 本 venue 没有逐账户资金费端点（Kraken 期货：/history/funding 之类不存在，
    #: 实测取不到任何逐账户记录）。与 `flows_supported` 同一语义：**能力边界**，
    #: 不是故障。executor 据此整段跳过资金费同步 —— 不调接口、不写事件、不打日志。
    #: HL 版 info 不声明此属性，executor 用 getattr(..., True) 取值 → 行为不变。
    funding_supported = False

    def user_funding_history(self, address: str, start_ms: int,
                             end_ms: Optional[int] = None) -> List[dict]:
        """无公开逐账户资金费端点（已实测 404），返回空——面板资金费汇总缺数属已知 PR-3 项。"""
        return []

    #: 本 venue 没有出入金流水端点（Kraken 期货 /transfers 是 x-internalOnly，
    #: 实测返回非 JSON）。executor 据此走「显式未判」而不是「同步成功但 0 行」——
    #: 后者会把充提整笔当盈亏，提现就能误触发 kill。
    flows_supported = False

    def ledger_updates(self, address: str, start_ms: int = 0,
                       end_ms: Optional[int] = None,
                       deadline_ts: Optional[float] = None) -> List[dict]:
        """出入金：Kraken 期货无公开端点（/transfers 内部端点，实测不可用），返回空。

        `deadline_ts` 只是接口兼容（executor 会传），不参与任何预算控制。
        真正的「不可用」语义由类属性 `flows_supported=False` 承担。
        """
        return []

    def user_fees(self, address: str) -> dict:
        return {"takerFee": self._taker_fee, "makerFee": self._maker_fee}

    # ---------------------------------------------------------------- 代理钱包
    def agent_status(self, address: str) -> dict:
        """Kraken key 无过期——返回「无期限」形状让 agent 看护自然空闲。"""
        return {"kind": "kraken-api", "expires": None, "days_left": None}

    def extra_agents(self, address: str) -> List[dict]:
        return []

    # ---------------------------------------------------------------- 快照
    def snapshot(self, address: str, mids: Optional[Dict[str, float]] = None) -> dict:
        if mids is None:
            try:
                mids = self.all_mids()
            except Exception:                                    # noqa: BLE001
                mids = {}
        try:
            acct = self._ep.account() or {}
        except KrApiErrorWrapper as e:
            raise KrInfoError(f"account 读取失败：{e}") from e
        m = _acct_metrics(acct)
        positions = self._positions(mids)
        notional = sum(p["position_value"] for p in positions)
        try:
            orders = self._orders()
            orders_trusted = True
        except KrInfoError:
            orders, orders_trusted = [], False
        # 统一账户兼容键：Kraken 期货钱包本身就是「唯一余额」，现货口径 = 期货钱包现金。
        # spot_ok 恒 True（读不到 pv 会抛 KrInfoError，不会静默 0——对应 P1-H1 教训）。
        return {"ts": time.time(), "address": address or "kraken",
                "equity": m["equity"], "equity_total": m["equity"],
                "withdrawable": m["withdrawable"], "margin_used": m["margin_used"],
                "notional": notional, "positions": positions,
                "open_orders": orders, "orders_trusted": orders_trusted,
                "unified": True, "spot_usdc": m["withdrawable"], "spot_hold": 0.0,
                # 统一账户：期货钱包即唯一余额，读到 pv 就一定可信（读失败会抛
                # KrInfoError）。显式给键，让执行器 effective_equity 的 spot_ok
                # 熔断分支口径明确，不依赖 dict.get 默认值。
                "spot_ok": True}


# 延迟引用：KrApiError 在 client 层，info 层把它当「读失败」转成 KrInfoError。
from .client import KrApiError as KrApiErrorWrapper  # noqa: E402

DEFAULT_MAX_LEVERAGE_OVERRIDE = None
