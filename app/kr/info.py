"""KrInfo：Kraken Futures 账户/行情读取，鸭子类型对齐 app/hl/info.HlInfo。

snapshot() 返回**冻结接口**（与 HlInfo.snapshot 同形状，另含统一账户兼容键）：
{ts, address, equity, equity_total, withdrawable, margin_used, notional,
 positions[{coin,side,size,entry_px,mark_px,position_value,liq_px,leverage,
            isolated,margin_used,unrealized_pnl,roe,funding_since_open}],
 open_orders[{coin,oid,is_buy,sz,limit_px,reduce_only,trigger_px,is_trigger,timestamp}],
 unified, spot_usdc}

已知数据缺口（2026-11-08，全部 TODO(sandbox)/PR-3）：
- /fills 不带 fee / reduceOnly / closedPnl → dir+reduce_only 由成交序列推演
  （净仓从 0/反向变小 = 平仓腿），fee 缺失按 taker 估并标 fee_estimated，
  closedPnl 对平仓腿按 (px−avg_entry)×qty 计算。
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
        """{coin: {"szDecimals": int, "maxLeverage": int}}（进程内缓存 1h，同 HL 口径）。"""
        now = time.time()
        if self._specs_cache is None or now - self._specs_ts > 3600:
            out: Dict[str, dict] = {}
            for i in self.instruments():
                coin = registry.kr_coin(str(i.get("symbol") or ""))
                if not coin:
                    continue
                mts = _num(i.get("minimumTradeSize"), 0.0)
                tick = _num(i.get("tickSize"), 0.0)
                step = mts if mts > 0 else tick
                out[coin] = {"szDecimals": _decimals_of(step) if step > 0 else 8,
                             "maxLeverage": int(self._max_lev.get(coin, 5))}
            if out:
                self._specs_cache = out
                self._specs_ts = now
        cache = self._specs_cache or {}
        if coins is None:
            return dict(cache)
        return {c: cache[c] for c in coins if c in cache}

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
        acct = self._ep.account() or {}
        pv = _num(acct.get("pv") or acct.get("portfolioValue"))
        av = _num(acct.get("av") or acct.get("availableMargin"))
        im = _num(acct.get("im") or acct.get("initialMargin"))
        positions = self._positions(mids=None)
        notional = sum(p["position_value"] for p in positions)
        asset_positions = [{
            "position": {"coin": p["coin"], "szi": p["side"] * p["size"],
                         "entryPx": p["entry_px"], "positionValue": p["position_value"],
                         "unrealizedPnl": p["unrealized_pnl"],
                         "leverage": {"type": "cross", "marginUsed": p["margin_used"]}},
        } for p in positions]
        return {"marginSummary": {"accountValue": pv, "withdrawable": av,
                                  "totalMarginUsed": im, "totalNtlPos": notional},
                "assetPositions": asset_positions,
                "withdrawable": av}

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
        acct = self._ep.account() or {}
        return {"balances": [{"asset": "USD", "total": _num(acct.get("pv"))}]}

    def account_equity(self, address: str) -> float:
        """MR 腿定仓基数：单端点口径 = /account pv。失败抛 KrInfoError。"""
        try:
            acct = self._ep.account() or {}
        except KrApiErrorWrapper as e:
            raise KrInfoError(f"account 读取失败：{e}") from e
        pv = _num(acct.get("pv") or acct.get("portfolioValue"))
        if pv <= 0:
            raise KrInfoError("account pv ≤ 0（数据不可信，fail-closed）")
        return pv

    def _positions(self, mids: Optional[Dict[str, float]]) -> List[dict]:
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
            qty = _num(item.get("netQty"))
            if qty == 0:
                continue
            side = 1 if qty > 0 else -1
            size = abs(qty)
            entry = _num(item.get("avgFillPrice"))
            mark = _num(item.get("markPrice"))
            if mark <= 0 and mids:
                mark = _num((mids or {}).get(coin))
            if mark <= 0:
                mark = entry
            value = mark * size
            upnl = _num(item.get("calculatedUnrealizedPnl"),
                        (mark - entry) * qty)
            lev = int(self._max_lev.get(coin, 5))
            out.append({
                "coin": coin, "side": side, "size": size, "entry_px": entry,
                "mark_px": mark, "position_value": value,
                "liq_px": _num(item.get("liquidationThresholdPrice")),
                "leverage": lev, "isolated": False,          # flex 只有 cross
                "margin_used": value / lev if lev > 0 else 0.0,
                "unrealized_pnl": upnl,
                "roe": (upnl / (value / lev)) if lev > 0 and value > 0 else 0.0,
                "funding_since_open": _num(item.get("realizedFundingPnl")),
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
            is_buy = str(o.get("side") or "").lower() == "buy"
            reduce_only = bool(o.get("reduceOnly"))
            out.append({
                "coin": coin, "oid": str(o.get("orderId") or ""),
                "is_buy": is_buy, "sz": _num(o.get("qty") or o.get("size")),
                "limit_px": _num(o.get("limitPrice")),
                "reduce_only": 1 if reduce_only else 0,
                "trigger_px": stop if stop > 0 else None,
                "is_trigger": 1 if stop > 0 else 0,
                "timestamp": _num(o.get("timestamp")),
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
        """全量成交（从 0 翻页），并推演 dir/reduce_only/closedPnl。

        Kraken /fills 字段：fillTime(ms)/fillType/price/seq/side/size/symbol/type
        （无 fee/reduceOnly/closedPnl）。按 (symbol, fillTime, seq) 顺序重放净仓：
        - 净仓从 0 或反向变化 → 开/平判断；平仓腿算 closedPnl=(px−avg_entry)×qty。
        - dir 用 HL 字符串口径（"open long"/"close long"/"long > short"…）。
        - fee 缺失按 taker 估算 + fee_estimated 标记（executor 的 QUOTE_FEE_TOKENS
          白名单口径照常生效）。
        """
        try:
            raw = self._ep.fills(0)
        except KrApiErrorWrapper as e:
            raise KrInfoError(f"fills 读取失败：{e}") from e
        raw.sort(key=lambda f: (_num(f.get("fillTime")), _num(f.get("seq"))))
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
            t = int(_num(f.get("fillTime")))
            oid = str(f.get("orderId") or "")
            tid = str(f.get("seq") or f"{t}-{len(out)}")
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
            elif close_qty > 0 and new != 0:
                direction = ("close long" if old > 0 else "close short")
            elif close_qty > 0:
                direction = ("close long" if old > 0 else "close short")
            else:
                direction = ("open long" if is_buy else "open short")
            out.append({
                "coin": coin, "px": px, "sz": sz, "side": "buy" if is_buy else "sell",
                "time": t, "hash": f"kr:{tid}", "dir": direction,
                "closedPnl": closed, "fee": sz * px * DEFAULT_TAKER_FEE,
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
            except KrInfoError:
                mids = {}
        acct = self._ep.account() or {}
        equity = _num(acct.get("pv") or acct.get("portfolioValue"))
        withdrawable = _num(acct.get("av") or acct.get("availableMargin"))
        im = _num(acct.get("im") or acct.get("initialMargin"))
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
                "equity": equity, "equity_total": equity,
                "withdrawable": withdrawable, "margin_used": im,
                "notional": notional, "positions": positions,
                "open_orders": orders, "orders_trusted": orders_trusted,
                "unified": True, "spot_usdc": withdrawable, "spot_hold": 0.0}


# 延迟引用：KrApiError 在 client 层，info 层把它当「读失败」转成 KrInfoError。
from .client import KrApiError as KrApiErrorWrapper  # noqa: E402

DEFAULT_MAX_LEVERAGE_OVERRIDE = None
