"""止损覆盖判据 —— **执行器、面板、探针共用的唯一口径**。

为什么要抽出来：同一件事（「这个仓位有没有真正能保护它的止损」）原来有三份实现。
执行器那份（`Executor._has_stop`）经三轮审计补上了方向/数量/亏损侧/止盈排除；
面板那份（`views.status_payload`）停在第一版，只看 coin + reduce_only + is_trigger，
于是「买向 0.001 张的盈利侧止盈单」也能让面板显示「已有止损」，与执行器/探针互相
矛盾（2026-11-13 审核 P2-1）。三份实现迟早会再分叉，所以把判据收进这个纯函数，
调用方只负责把交易所挂单原样传进来。

判据（任何一条不成立就不算保护）：

- 同 ``coin``；``reduce_only`` 为真；``is_trigger`` 为真；
- ``order_type`` 不是止盈类型（``take_profit``/``tp``…）——止盈单是盈利侧了结，
  价格越过它之后对下行没有任何保护；
- ``trigger_px`` 必须 > 0（0/NULL 的触发单语义上是市价单）；
- 给了 ``side`` → 方向必须相反：多仓（+1）要卖单（``is_buy=False``），空仓（-1）要买单；
- 给了 ``entry_px`` 与 ``side`` → 触发价必须位于亏损侧：多仓 ``< entry``、空仓 ``> entry``；
- 给了 ``sz``（>0）→ 挂单张数必须 ≥ ``sz − 容差``；容差优先用调用方给的
  ``sz_tick``（交易所数量精度），拿不到时退化成 ``sz × 1e-6`` 的相对噪声容差。

``order_type`` 缺失（老桩/降级适配层）**不**当作止盈单放行，也不当作止损单放行 ——
后面 trigger/方向/数量/亏损侧四道判据照样要过。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

__all__ = ["TAKE_PROFIT_TYPES", "covering_stop_px", "has_covering_stop"]

#: 止盈类 order_type（小写比较）。Kraken 用 ``take_profit``，HL 侧历史值可能写作
#: ``tp``/``take-profit``；命中即排除，绝不因为写法不同就当止损。
TAKE_PROFIT_TYPES = frozenset({"take_profit", "take-profit", "take profit", "takeprofit",
                               "tp", "profit"})


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if x == x else default          # NaN → default


def _g(o: Any, key: str, default: Any = None) -> Any:
    """容错取值：dict / sqlite3.Row / 老桩对象都能读，缺键返回 default。"""
    try:
        v = o[key]
    except (KeyError, IndexError, TypeError, AttributeError):
        return default
    return default if v is None else v


def covering_stop_px(orders: Optional[Iterable[Any]], coin: str, *,
                     side: Optional[int] = None,
                     sz: Optional[float] = None,
                     entry_px: Optional[float] = None,
                     sz_tick: Optional[float] = None) -> Optional[float]:
    """返回第一张**真正覆盖该仓位**的止损触发价；没有则 None。

    参数语义见模块 docstring。``orders`` 是交易所挂单列表（dict 或 sqlite3.Row 均可，
    只按 key 读；缺字段按下述规则退化，绝不因为缺字段就当保护）。
    """
    need_sz = _f(sz, 0.0)
    ref_px = _f(entry_px, 0.0)
    loss_side = ref_px > 0 and side is not None
    side_i = int(side) if side is not None else 0
    tol = 0.0
    if need_sz > 0:
        tick = _f(sz_tick, 0.0)
        tol = tick if tick > 0 else need_sz * 1e-6
    for o in orders or []:
        if _g(o, "coin") != coin:
            continue
        if not _g(o, "reduce_only") or not _g(o, "is_trigger"):
            continue
        if str(_g(o, "order_type") or "").strip().lower() in TAKE_PROFIT_TYPES:
            continue
        trig = _f(_g(o, "trigger_px"), 0.0)
        if trig <= 0:
            continue
        if loss_side:
            if side_i > 0 and not trig < ref_px:
                continue
            if side_i < 0 and not trig > ref_px:
                continue
        if side is not None:
            is_buy = _g(o, "is_buy")
            if is_buy is None or bool(is_buy) != (side_i < 0):
                continue
        if need_sz > 0 and _f(_g(o, "sz"), 0.0) < need_sz - tol:
            continue
        return trig
    return None


def has_covering_stop(orders: Optional[Iterable[Any]], coin: str, **kw: Any) -> bool:
    """``covering_stop_px(...) is not None`` 的布尔外壳（判据只有一份）。"""
    return covering_stop_px(orders, coin, **kw) is not None
