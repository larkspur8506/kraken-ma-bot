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
  taker 估并标 fee_estimated。dir/reduce_only 仍由成交序列推演。
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
DEFAULT_TAKER_FEE = 0.0005


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
                 max_leverage: Optional[Dict[str, int]] = None):
        if endpoints is not None:
            self._ep = endpoints
        else:
            from .auth import KrClient, DEFAULT_BASE
            self._ep = KrEndpoints(KrClient(
                api_key=api_key, secret_b64=secret_b64,
                base_url=base_url or DEFAULT_BASE, timeout=timeout))
        self._max_lev = dict(DEFAULT_MAX_LEVERAGE_OVERRIDE or max_leverage or {})
        self._specs_cache: Optional[Dict[str, dict]] = None
        self._specs_ts = 0.0

    # ---------------------------------------------------------------- 行情
    def instruments(self) -> List[dict]:
        return self._ep.instruments()

    def specs(self, coins: Optional[List[str]] = None) -> Dict[str, dict]:
        """{coin: {"sz_decimals": int, "szDecimals": int, "price_tick": float,
                  "price_decimals": int, "max_leverage": int, "maxLeverage": int}}

        数量精度取 `contractValueTradePrecision`（XBT=4 / ETH=3 / SOL=2 / QQQX=3）。
        **不能**用 `tickSize`：那是价格 tick（XBT=1），当数量步长会 floor 成整数张
        = 1 BTC ≈ $75k 一张，1.33% 风险仓位直接变成 0 或 1 张（对抗审查 P0）。
        兼容键：executor/agent 读 `sz_decimals`，MR 腿读 `szDecimals` —— 两个都给。
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
                lev = int(self._max_lev.get(coin, 5))
                out[coin] = {"sz_decimals": sz_dec, "szDecimals": sz_dec,
                             "price_tick": tick, "price_decimals": _decimals_of(tick),
                             "contract_size": _num(i.get("contractSize"), 0.0) or 1.0,
                             "max_leverage": lev, "maxLeverage": lev}
            if out:
                self._specs_cache = out
                self._specs_ts = now
        cache = self._specs_cache or {}
        if coins is None:
            return dict(cache)
        return {c: cache[c] for c in coins if c in cache}

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
            upnl = _num(item.get("unrealizedPnl"),
                        (0.0 if entry <= 0 else 0.0))
            mark = _num((mids or {}).get(coin))
            if mark <= 0 and entry > 0:
                # Kraken 不返回标记价：用 未实现盈亏 反推（upnl = (mark-entry)*signed_qty）
                mark = entry + upnl / (side * size)
            if mark <= 0:
                mark = entry
            value = mark * size
            lev = int(self._max_lev.get(coin, 5))
            margin_est = value / lev if lev > 0 else 0.0
            out.append({
                "coin": coin, "side": side, "size": size, "entry_px": entry,
                "mark_px": mark, "position_value": value,
                "liq_px": _num(item.get("liquidationThresholdPrice")),  # 全仓口径无逐仓强平价
                "leverage": lev, "isolated": False,          # flex 只有 cross
                "margin_used": margin_est,                   # 估算（全仓无法逐仓拆分）
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
                direction = "long > short" if old > 0 else "short > long"
            elif close_qty > 0:
                direction = "close long" if old > 0 else "close short"
            else:
                direction = "open long" if is_buy else "open short"
            # realized_pnl 是交易所真值（历史页为 null），null 时才用推演值
            rp = f.get("realized_pnl")
            if rp is None:
                closed_val, closed_src = closed, "derived"
            else:
                closed_val, closed_src = _num(rp), "exchange"
            out.append({
                "coin": coin, "px": px, "sz": sz, "side": "buy" if is_buy else "sell",
                "time": t, "hash": f"kr:{tid}", "dir": direction,
                "closedPnl": closed_val, "closedPnl_src": closed_src,
                "fee": sz * px * DEFAULT_TAKER_FEE,
                "fee_estimated": 1, "tid": tid, "oid": oid,
                "reduce_only": 1 if close_qty > 0 else 0,
            })
        return out

    def user_funding_history(self, address: str, start_ms: int,
                             end_ms: Optional[int] = None) -> List[dict]:
        """无公开逐账户资金费端点（已实测 404），返回空——面板资金费汇总缺数属已知 PR-3 项。"""
        return []

    def ledger_updates(self, address: str, start_ms: int = 0,
                       end_ms: Optional[int] = None) -> List[dict]:
        """出入金：期货 API 无此端点（外部充值在主 Kraken 账户），返回空；PR-3 用面板手工补账。"""
        return []

    def user_fees(self, address: str) -> dict:
        return {"takerFee": DEFAULT_TAKER_FEE, "makerFee": 0.0002}

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
