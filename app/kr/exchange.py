"""KrExchange：Kraken Futures 下单路径，鸭子类型对齐 app/hl/exchange.HlExchange。

调用面（executor.py / mr.py / main.py 实际用到的成员，逐一对应）：
available / init_error / testnet / _cloid / mid / slippage_limit / place_order /
cancel / cancel_all / cancel_all_orders / update_leverage / schedule_cancel /
_open_orders_checked + 模块级 make_cloid / parse_order_response / STATUS_*。

下单语义（2026-11-08 docs + 公开端点探针，TODO(sandbox) 真 payload 核对）：
- IOC 限价：orderType=ioc + limitPrice（executor 已用 slippage_limit 封顶滑点）。
- 触发单（止损）：orderType=stp + stopPrice + triggerSignal=mark + reduceOnly=true
  （触发后市价成交）。
- sendorder 成功形状：{"result":"success","sendStatus":{"orderId":..,"orderEvents":[...]}}；
  事件 orderEventStatus ∈ PLACED/OPEN/FILLED/CANCELLED/REJECT…
- Kraken flex 永续只有 cross：update_leverage 是 no-op（见包 docstring）。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional, Tuple

from app import symbol_registry as registry
from app.hl.exchange import (STATUS_DRY, STATUS_ERROR, STATUS_FILLED,  # noqa: F401
                             STATUS_RESTING, _clean_alert_text, _exc_text)
from .auth import KrAuthError
from .client import KrApiError, KrEndpoints

logger = logging.getLogger(__name__)

# Kraken flex 永续无逐仓/无杠杆设置：这里只给「杠杆安全上限」闸门用
# （executor 拿 maxLeverage 判断 plan.leverage 是否越界）。实际保证金是全仓口径。
DEFAULT_MAX_LEVERAGE = {"BTC": 50, "ETH": 50, "SOL": 50, "QQQ": 10}

_IOC_ORDER = "ioc"
_STOP_ORDER = "stp"


def make_cloid(symbol: str, bar_ms: int, purpose: str) -> str:
    """确定性 cliOrdId（0x + 32 hex，34 字符 ≤ Kraken 64 上限）。

    与 HL 侧 **同源推导**（同 `cma:` 前缀、同输入）——同一 (symbol, bar_ms,
    purpose) 在两个场所得到同一个 cloid，DB 记账/幂等键跨场馆不歧义。
    """
    h = hashlib.sha256(f"cma:{symbol}:{bar_ms}:{purpose}".encode()).hexdigest()[:32]
    return "0x" + h


def _coerce_cloid(cloid):
    """Kraken 直接收字符串；这里只做格式守门（非法宁可拒单）。"""
    if cloid is None:
        return None
    import re
    txt = str(cloid).strip()
    if not re.fullmatch(r"[a-zA-Z0-9\-]{1,64}", txt):
        raise ValueError(f"cliOrdId 格式非法（≤64 位字母数字/-）：{txt[:14]}…")
    return txt


def _ok(status: str, oid=None, avg_px=None, filled_sz=None, raw=None) -> dict:
    return {"ok": True, "status": status, "oid": oid, "avg_px": avg_px,
            "filled_sz": filled_sz, "raw": raw, "error": ""}


def _err(msg: str, raw: Any = None, status: str = STATUS_ERROR) -> dict:
    return {"ok": False, "status": status, "oid": None, "avg_px": None,
            "filled_sz": None, "raw": raw, "error": msg}


def parse_order_response(resp: dict) -> dict:
    """把 sendorder 响应归一化成 _ok/_err dict。

    Kraken 形状：{"result":"success","sendStatus":{"orderId":"…","orderEvents":[
        {"orderEventStatus":"FILLED","filledQty":..,"filledPricePrice":..} |
        {"orderEventStatus":"PLACED"|"OPEN"|…}]}}
    取第一个 FILLED 事件做成交价/量；多个 FILLED（部分成交分事件）按量加权合并。
    """
    if not isinstance(resp, dict):
        return _err(f"响应不是 dict：{str(resp)[:200]}", resp)
    send = resp.get("sendStatus")
    if not isinstance(send, dict):
        return _err(f"响应缺 sendStatus：{str(resp)[:200]}", resp)
    oid = send.get("orderId")
    events = send.get("orderEvents") or []
    fills = [(float(e.get("filledQty") or 0), float(e.get("filledPricePrice")
                       or e.get("filledPrice") or 0))
             for e in events if str(e.get("orderEventStatus") or "").upper() == "FILLED"]
    if fills:
        qty = sum(q for q, _ in fills)
        avg = (sum(q * p for q, p in fills) / qty) if qty > 0 else None
        return _ok(STATUS_FILLED, oid=oid, avg_px=avg, filled_sz=qty or None, raw=resp)
    placed = [e for e in events
              if str(e.get("orderEventStatus") or "").upper() in ("PLACED", "OPEN")]
    if placed or send.get("orderFlags") is not None:
        return _ok(STATUS_RESTING, oid=oid, raw=resp)
    rejected = [str(e.get("reason") or e.get("orderEventStatus")) for e in events
                if str(e.get("orderEventStatus") or "").upper() in ("REJECT", "CANCELLED")]
    if rejected:
        return _err(f"订单被拒/撤销：{'; '.join(rejected)[:300]}", resp)
    return _ok(STATUS_RESTING, oid=oid, raw=resp)


def _fmt_num(x: float) -> str:
    """下单参数的数字字符串化：避开 repr 的科学计数法（1e-05 这类 Kraken 会拒）。"""
    s = repr(float(x))
    return format(float(x), "f") if ("e" in s or "E" in s) else s


class KrExchange:
    """写路径包装。`endpoints` 参数只为测试注入；生产自建 KrClient。"""

    def __init__(self, api_key: str = "", secret_b64: str = "", base_url: str = "",
                 dry_run: bool = True, timeout: float = 15.0, max_slippage_pct: float = 0.003,
                 info: Any = None, endpoints: Any = None):
        self.api_key = (api_key or "").strip()
        self.secret_b64 = (secret_b64 or "").strip()
        self.base_url = (base_url or "").rstrip("/")
        self.dry_run = bool(dry_run)
        self.timeout = timeout
        self.max_slippage_pct = max_slippage_pct
        self.info = info                                    # KrInfo 注入（mid/all_mids 用）
        self._endpoints = endpoints
        self._init_error = ""

    @property
    def testnet(self) -> bool:
        return "demo-futures" in (self.base_url or "")

    @property
    def available(self) -> bool:
        return self.dry_run or bool(self.api_key and self.secret_b64)

    @property
    def init_error(self) -> str:
        return self._init_error

    def _ensure(self) -> bool:
        if self.dry_run:
            return True
        if self._endpoints is not None:
            return True
        if not self.api_key or not self.secret_b64:
            self._init_error = "缺少 Kraken api_key / secret_b64"
            return False
        try:
            from .auth import KrClient, DEFAULT_BASE
            self._endpoints = KrEndpoints(KrClient(
                api_key=self.api_key, secret_b64=self.secret_b64,
                base_url=self.base_url or DEFAULT_BASE, timeout=self.timeout))
            return True
        except Exception as e:                               # noqa: BLE001
            self._init_error = _exc_text(f"初始化 Kraken client 失败：{e}")
            logger.error("[kr-exchange] %s", self._init_error)
            return False

    @property
    def endpoints(self):
        self._ensure()
        return self._endpoints

    # ------------------------------------------------------------ 行情辅助
    def _symbol(self, coin: str) -> str:
        sym = registry.kr_symbol(coin)
        if not sym:
            raise KrAuthError(f"未知 Kraken 合约：{coin}")
        return sym

    def mid(self, coin: str) -> float:
        if self.info is not None:
            return float(self.info.all_mids().get(coin, 0.0) or 0.0)
        return 0.0

    def slippage_limit(self, coin: str, is_buy: bool, mid: Optional[float] = None) -> float:
        px = mid if mid else self.mid(coin)
        if px <= 0:
            raise KrAuthError(f"{coin} 取不到 mid")
        k = (1 + self.max_slippage_pct) if is_buy else (1 - self.max_slippage_pct)
        return px * k

    def _cloid(self, symbol: str, bar_ms: int, purpose: str):
        return make_cloid(symbol, bar_ms, purpose)

    # ------------------------------------------------------------ 写操作
    def place_order(self, coin: str, is_buy: bool, sz: float, limit_px: float,
                    reduce_only: bool = False, tif: str = "Ioc",
                    trigger_px: Optional[float] = None, cloid=None) -> dict:
        """IOC 限价 / 或 mark 触发的止损单。返回 _ok/_err dict（同 HL 契约）。"""
        try:
            _sz, _px = float(sz), float(limit_px)
        except (TypeError, ValueError):
            return _err(f"sz/limit_px 非数字：{sz!r}/{limit_px!r}")
        if _sz <= 0:
            return _err(f"sz 必须为正：sz={sz}")
        if trigger_px is not None:
            # 止损单：limit_px 不参与（HL 侧惯例传 0），校验触发价
            if float(trigger_px) <= 0:
                return _err(f"trigger_px 必须为正：{trigger_px}")
        elif _px <= 0:
            return _err(f"limit_px 必须为正：limit_px={limit_px}")
        try:
            cli = _coerce_cloid(cloid)
        except ValueError as e:
            return _err(str(e))
        try:
            symbol = self._symbol(coin)
        except Exception as e:                               # noqa: BLE001
            return _err(f"coin→symbol 失败：{_exc_text(e)}")
        params: dict[str, Any] = {"symbol": symbol, "side": "buy" if is_buy else "sell",
                                  "size": _fmt_num(_sz)}
        if trigger_px is not None:
            params.update({"orderType": _STOP_ORDER, "stopPrice": _fmt_num(trigger_px),
                           "triggerSignal": "mark", "reduceOnly": "true" if reduce_only else "false"})
        else:
            params.update({"orderType": _IOC_ORDER, "limitPrice": _fmt_num(_px),
                           "reduceOnly": "true" if reduce_only else "false"})
        if cli:
            params["cliOrdId"] = cli
        if self.dry_run:
            return _ok(STATUS_DRY, raw={"coin": coin, "is_buy": is_buy, "sz": sz,
                                        "limit_px": limit_px, "reduce_only": reduce_only,
                                        "params": params})
        if not self._ensure() or self.endpoints is None:
            return _err(self._init_error)
        try:
            resp = self.endpoints.sendorder(params)
        except KrApiError as e:
            return _err(f"下单被拒：{_exc_text(e)}", e.raw)
        except Exception as e:                               # noqa: BLE001
            return _err(f"下单异常：{_exc_text(e)}")
        out = parse_order_response(resp)
        out["request"] = {"coin": coin, "symbol": symbol, "is_buy": is_buy, "sz": sz,
                          "limit_px": limit_px, "reduce_only": reduce_only,
                          "trigger_px": trigger_px, "cliOrdId": cli}
        return out

    def cancel(self, coin: str, oid) -> dict:
        if self.dry_run:
            return _ok(STATUS_DRY, oid=oid)
        if not self._ensure() or self.endpoints is None:
            return _err(self._init_error)
        if not oid:
            return _err("cancel 缺 orderId")
        try:
            self.endpoints.cancelorder(oid)
            return _ok(STATUS_RESTING, oid=oid)
        except KrApiError as e:
            # 已成交/已不存在的单继续撤会报错——对调用方这就是「撤了，无所谓」
            if "notfound" in str(e).lower() or "not found" in str(e).lower() \
                    or "filled" in str(e).lower():
                return _ok(STATUS_FILLED, oid=oid, raw=e.raw)
            return _err(f"撤单失败：{_exc_text(e)}", e.raw)
        except Exception as e:                               # noqa: BLE001
            return _err(f"撤单异常：{_exc_text(e)}")

    def cancel_all(self, coin: str) -> dict:
        """撤该合约全部挂单：Kraken 没有 per-symbol 撤单端点，逐单撤。"""
        if self.dry_run:
            return _ok(STATUS_DRY)
        if not self._ensure() or self.endpoints is None:
            return _err(self._init_error)
        try:
            orders = self.endpoints.openorders()
            sym = self._symbol(coin)
            failed = []
            for o in orders:
                if str(o.get("symbol") or "") != sym:
                    continue
                oid = o.get("orderId")
                if not oid:
                    continue
                try:
                    self.endpoints.cancelorder(oid)
                except Exception as e:                       # noqa: BLE001
                    failed.append(f"{oid}:{_exc_text(e)[:80]}")
            if failed:
                return _err("部分撤单失败：" + "; ".join(failed[:5]))
            return _ok(STATUS_RESTING)
        except Exception as e:                               # noqa: BLE001
            return _err(f"cancel_all 异常：{_exc_text(e)}")

    def cancel_all_orders(self) -> dict:
        if self.dry_run:
            return _ok(STATUS_DRY)
        if not self._ensure() or self.endpoints is None:
            return _err(self._init_error)
        try:
            self.endpoints.cancelallorders()
            return _ok(STATUS_RESTING)
        except Exception as e:                               # noqa: BLE001
            return _err(f"cancel_all_orders 异常：{_exc_text(e)}")

    def update_leverage(self, coin: str, leverage: int, cross: bool = False) -> dict:
        """Kraken flex 永续无逐仓/杠杆设置 → no-op ok（语义差见包 docstring）。"""
        return _ok(STATUS_RESTING, raw={"coin": coin, "leverage": leverage,
                                        "cross": cross, "noop": "kraken-flex-cross-only"})

    def schedule_cancel(self, time_ms: Optional[int] = None, minutes: int = 30) -> dict:
        """死手开关在 Kraken 版同样默认关闭（用户拍板），no-op。"""
        return _ok(STATUS_RESTING, raw={"noop": "schedule-cancel-disabled"})

    def _open_orders_checked(self) -> Tuple[list, bool]:
        """(orders, trusted)：/openorders 成功即可信；异常时 ([], False)。

        有 info（KrInfo）时复用它的 HL 形状映射（coin/oid/is_trigger…），
        保证消费方字段口径一致。
        """
        if self.info is not None:
            try:
                return self.info.open_orders_checked("")
            except Exception as e:                           # noqa: BLE001
                logger.warning("[kr-exchange] openorders 读取失败：%s", _exc_text(e))
                return [], False
        if not self._ensure() or self.endpoints is None:
            return [], False
        try:
            return self.endpoints.openorders(), True
        except Exception as e:                               # noqa: BLE001
            logger.warning("[kr-exchange] openorders 读取失败：%s", _exc_text(e))
            return [], False
