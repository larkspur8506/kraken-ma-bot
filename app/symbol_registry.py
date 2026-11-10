"""标的注册表：**单一元数据来源**（纯 stdlib、import 无副作用）。

背景（血泪）：加一个新标的要同步改四处元数据 —— `market.HL_COIN_NAMES`、
`config` 默认 `SYMBOLS`、`sizing.SPECS`、HL 动态规格。漏任何一处都**静默坏**：
`SPECS` 漏了会让 `engine.walk → size_order(symbol)` 抛 `KeyError`，被 `tick` 的
per-symbol `try/except` 吞成一句「处理异常」，整个标的的信号一个都出不来，
而单测不会红。这里把「系统认识哪些标的、各场所的规格长什么样」收进一张表，
供信号链路（以及后续的行情层 / 执行器）消费，避免再靠人肉同步。

设计约束（下一批任务按此契约消费）：

- **纯 stdlib、无副作用**：import 不读网络 / 文件 / env，可被测试与 CI 安全导入。
- `env SYMBOLS` 仍是**启用清单**（`config.symbols` 不动）；本表只管**元数据**。
  「启用但未注册」属于配置错误，由调用方 fail-loud（本表自身不做启用判定）。
- 未注册（元数据缺失）：`get()` / `hl_coin()` 抛 `SymbolNotConfigured`；
  `mexc_spec()` 返回 `None`（供 `size_order` 优雅降级，不抛）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


class SymbolNotConfigured(Exception):
    """symbol 未在本注册表中登记（元数据缺失，属配置错误，调用方应 fail-loud）。"""


@dataclass(frozen=True)
class MexcSpec:
    """MEXC 合约规格。

    字段语义与旧 `app.sizing.SPECS` 的四元组 `(合约面值 coin/张, 张数步长, 最小张数, 价格精度)`
    一一对齐，便于 `sizing` 无痛迁移（`as_tuple()` 即旧四元组）。
    """

    contract_size: float
    step: int
    min_vol: int
    price_precision: int

    def as_tuple(self) -> Tuple[float, int, int, int]:
        return (self.contract_size, self.step, self.min_vol, self.price_precision)


@dataclass(frozen=True)
class SymbolEntry:
    """一个标的的全部场所元数据（HL 合约名 + MEXC 规格；MEXC 段可为空）。"""

    symbol: str
    hl_coin: str
    mexc: Optional[MexcSpec] = None


# ---------------------------------------------------------------- 注册表本体
# 值从旧 `app.sizing.SPECS` / `market.HL_COIN_NAMES` **原样迁移**，不改任何数值：
#   BTC_USDT → BTC + MEXC(面值 0.0001, 步长 1, 最小 1 张, 价格精度 1)
#   ETH_USDT → ETH + MEXC(面值 0.01,   步长 1, 最小 1 张, 价格精度 2)
#   SOL_USDT → SOL + MEXC(面值 0.1,    步长 1, 最小 1 张, 价格精度 2)
_REGISTRY: Dict[str, SymbolEntry] = {
    "BTC_USDT": SymbolEntry("BTC_USDT", "BTC", MexcSpec(0.0001, 1, 1, 1)),
    "ETH_USDT": SymbolEntry("ETH_USDT", "ETH", MexcSpec(0.01, 1, 1, 2)),
    "SOL_USDT": SymbolEntry("SOL_USDT", "SOL", MexcSpec(0.1, 1, 1, 2)),
    # QQQ 是 MR 腿（QQQX 永续）的标的，**没有** MEXC 合约段（mexc=None）。
    # 登记它的目的：env SYMBOLS 里误配成 QQQ_USDT 时，信号层能查到 hl_coin/规格
    # （而不是静默抛 KeyError 被 per-symbol try/except 吞掉 → 该标的永远没信号）；
    # 同时 coin_map 的取值集合（executor 判定「主策略标的」）仍是显式配置的。
    "QQQ_USDT": SymbolEntry("QQQ_USDT", "QQQ", None),
}


def known_symbols() -> List[str]:
    """已登记的全部 symbol（顺序稳定，与定义一致）。"""
    return list(_REGISTRY.keys())


def get(symbol: str) -> SymbolEntry:
    """取一条标的元数据；未注册抛 `SymbolNotConfigured`。"""
    entry = _REGISTRY.get(symbol)
    if entry is None:
        raise SymbolNotConfigured(
            f"{symbol}: 未在 symbol_registry 中登记（加标的必须同时登记元数据）")
    return entry


def hl_coin(symbol: str) -> str:
    """symbol → HL 合约名（如 BTC_USDT → BTC）；未注册抛 `SymbolNotConfigured`。"""
    return get(symbol).hl_coin


def mexc_spec(symbol: str) -> Optional[MexcSpec]:
    entry = _REGISTRY.get(symbol)
    return entry.mexc if entry is not None else None


# ---------------------------------------------------------------- Kraken 映射
# display coin（BTC/ETH/SOL/QQQ）→ Kraken flex 永续合约名。
# 信号层 symbol（BTC_USDT 等）→ coin 沿用 hl_coin；场所换名只发生在 Kr 客户端层。
_KR_SYMBOLS: Dict[str, str] = {
    "BTC": "PF_XBTUSD",
    "ETH": "PF_ETHUSD",
    "SOL": "PF_SOLUSD",
    "QQQ": "PF_QQQXUSD",
    # HL MR 腿的 xStock 名（xyz:XYZ100 / 裸 XYZ100）：kr_symbol 取冒号后段查表，
    # 老执行器代码迁移期可以直接传旧名，两个入口都落到同一张合约。
    "XYZ100": "PF_QQQXUSD",
}


def kr_symbol(coin: str) -> Optional[str]:
    """display coin → Kraken 合约名；未登记返回 None（调用方 fail-closed）。

    接受 dex 限定名（HL MR 腿的 `xyz:XYZ100` 形态）：取冒号后段再查表，
    所以 `xyz:XYZ100` / `XYZ100` 都能落到 PF_QQQXUSD。
    """
    c = str(coin or "").strip().upper()
    if ":" in c:
        c = c.split(":")[-1]
    return _KR_SYMBOLS.get(c)


def kr_coin(trade_symbol: str) -> Optional[str]:
    """Kraken 合约名 → display coin（PF_XBTUSD → BTC）；未登记返回 None。"""
    s = str(trade_symbol or "").strip().upper()
    for coin, sym in _KR_SYMBOLS.items():
        if sym == s:
            return coin
    return None
