"""Hyperliquid 执行器后台监控页（服务器渲染 HTML + JSON API）。

公开入口（签名冻结，改动需同步 tests/test_hl_views.py）：
    status_payload(deps)       组装监控 JSON。**任何字段缺失/接口失败都不抛异常**。
    render_dashboard(payload)  JSON → 自包含 HTML（内联 <style>，零外部 CDN/JS）。
    build_router(deps)         路由：/hl、/api/hl/status、/api/hl/equity、/api/hl/snapshot

装配（在 app/main.py 里挂一次即可）：
    from app.hl.views import DashboardDeps, build_router
    app.include_router(build_router(DashboardDeps(
        db=db, config_fn=current_config, info=hl_info,
        address_fn=lambda: os.environ.get("HL_ADDRESS", ""),
        mode_fn=lambda: os.environ.get("HL_EXEC_MODE", "off"))))

本页只读、不持私钥、不发起任何写请求；鉴权交给外层网关或可选的 ?k=（见 HL_DASH_REQUIRE_ADMIN）。

对账口径（system.mismatch 三态：True 需人工处理 / False 一致 / None 无法核对）：
    1) 有停在 intent/sent 超过 3 分钟的 hl_orders（进程崩溃/超时，结果未知）
    2) DB 里 status='open' 的仓位，快照里查不到对应 coin
    3) DB 里 hl_stop_oid 非空，但快照里没有该 coin 的 reduce_only 触发单
    4) 快照里有仓位，但 DB 没有任何对应 open 记录（手工/外部仓位）
    快照读不到（reachable=False）时**不判一致也不判不一致**，返回 None（面板显示
    「对账 无法核对」）—— 以前返回 False 等于把「没读到」说成「账实相符」。
    第 1 条只看本地 hl_orders，与快照无关，所以照常判。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import auth as authn
from app import posfields
from app.hl import flows as hl_flows
from app.hl import dd as hl_dd
from app.hl import stops as hl_stops
from app.ui_theme import (ICON_LINKS, SYMBOL_CSS, THEME_CSS, H1_BLOCK, PRODUCT_NAME,
                          normalize_theme, theme_attr, theme_switch_html)

logger = logging.getLogger(__name__)

#: 401 提示：只说明「凭据放哪」，**不含任何真值**（也不用示例值冒充）。
_AUTH_HINT = "需要 ADMIN_TOKEN：Authorization: Bearer 请求头，或 ?k= 参数"

# 公开导出（含面板合并时复用的 _render_body / DASH_CSS；其余为原有公开 API）。
__all__ = [
    "SCHEMA_VERSION", "MAINNET_DEFAULT", "DashboardDeps",
    "status_payload", "render_dashboard", "build_router",
    "DASH_CSS", "_render_body", "THEME_CSS",
    "ROW_CHOICES", "DEFAULT_ROWS", "ROWS_COOKIE", "normalize_rows", "_signals_html",
    "_agent_badge_html",
]

# 长表每页行数（手机优先）：默认 20，面板上可切 10/20/50，选择存 cookie `cma_rows`。
# 只作用于「记录/历史」类长表（记录页的下单/成交/信号/提醒/事件五块）；盈亏曲线不受影响。
ROW_CHOICES = (10, 20, 50)
DEFAULT_ROWS = 20
ROWS_COOKIE = "cma_rows"

SCHEMA_VERSION = 1
_DAY = 86400.0
MAINNET_DEFAULT = "https://futures.kraken.com"   # 线上 venue（Kraken 期货）；HL 只在历史分支里

_MODE_TXT = {"off": "只提醒", "dry": "影子", "live": "实盘"}
_MODE_CLS = {"off": "muted", "dry": "warn", "live": "neg"}
# 信号表状态 → 中文（_signals_html 用；记录页复用同一映射）
_ST_TXT = {"pending": "待确认", "open": "持仓中", "closed": "已平仓", "ignored": "已忽略"}

# hl_status（执行器侧的账实状态）→ 中文。只在「不是 open」时才显示在状态列第二行 ——
# open 就是「持仓中」，再写一遍纯噪音。未知值兜底原文（不编一个看着像结论的词）。
_HL_ST_TXT = {"closing": "平仓待核对", "mismatch": "账实不符", "aborted": "开仓中止",
              # 执行器平仓后会把 hl_status 写成 closed（executor 关账路径），不映射的话
              # 状态列会出现「已平仓 + closed」两种写法同现（原始英文值漏到界面上）。
              "closed": "已平仓"}
# 宽限期（hl_close_grace_s）：与 Config/Executor 同一默认与范围（面板不 import main，
# 也不 import executor —— 这里只要一个「读配置 + 兜底」的小工具）。
_GRACE_DEFAULT_S = 900.0
_GRACE_MIN_S, _GRACE_MAX_S = 300.0, 3600.0

# 成交「买卖」列的四向推导（side 是多空方向不是买卖方向 —— 平多其实是卖出）。
# 总览的成交表 2026-10-18 迁到记录页后，这里是唯一口径源，记录页 import 复用
# （别在 admin_ui 里再抄一份）。
_FILL_BS = {"Open Long": "买", "Close Short": "买",
            "Close Long": "卖", "Open Short": "卖"}

#: 大小写不敏感索引。Kraken 的 `KrInfo._fills()` 与历史行可能给 "open long"/
#: "OPEN LONG" 等写法，精确键查找会让买卖列显示「—」。渲染层一律走 `_fill_bs()`，
#: 别再直接 `_FILL_BS.get(...)`（admin_ui 也 import 这个 helper，不另抄一份）。
_FILL_BS_LC = {k.lower(): v for k, v in _FILL_BS.items()}


def _fill_bs(direction: Any) -> str:
    """成交方向 → 买/卖。大小写/多余空白不敏感；未知值返回「—」。"""
    key = " ".join(str(direction or "").split()).lower()
    return _FILL_BS_LC.get(key, "—")


# ---------------------------------------------------------------- 依赖注入
@dataclass
class DashboardDeps:
    """监控页依赖。全部走可替换函数，便于测试注入假实现。"""
    db: Any
    config_fn: Callable[[], Any]        # 返回 app.config.Config
    info: Any                           # app.hl.info.HlInfo 实例；可为 None
    address_fn: Callable[[], str]       # HL 钱包地址；可为空串
    mode_fn: Callable[[], str]          # off | dry | live
    base_url_fn: Callable[[], str] = lambda: MAINNET_DEFAULT
    now_fn: Callable[[], float] = time.time
    #: `() -> dict | None`，MR 腿状态块（app/hl/mr.py 的 tick() 返回值；main 是唯一 writer）。
    mr_fn: Optional[Callable[[], Any]] = None
    #: 可选：快照优先读取器（`SnapshotInfo`）。`/api/hl/status` 默认（`live=0`）用它免去每次实连交易所，
    #: 显式传 `?live=1` 或未注入时仍用 `info`（实时客户端）。
    snapshot_info_fn: Optional[Callable[[], Any]] = None
    #: 可选：API 凭据状态（`main._agent_state`，三态 missing/configured/verified）。
    #: 由 main 注入后，`/api/hl/status` 与 `/hl` 的 API 徽章与设置页卡片**同一个生产者**，
    #: 不再出现「设置页说未配置、状态条说不限期」这种两套结论。
    agent_state_fn: Optional[Callable[[], Any]] = None


# ---------------------------------------------------------------- 小工具
def _num(v: Any, default: float = 0.0) -> float:
    """安全转 float：None/垃圾值/NaN/inf → default。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _opt_num(v: Any) -> Optional[float]:
    """安全转 float，保留 None。"""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _fmt(v: Any, dp: int = 2, dash: str = "—") -> str:
    f = _opt_num(v)
    return dash if f is None else f"{f:,.{dp}f}"


def _opt_int(v: Any) -> Optional[int]:
    """安全转 int，保留 None（symbol_state.dense_cnt 这类可空整数）。"""
    f = _opt_num(v)
    return None if f is None else int(f)


def _lev_text(pos: Dict[str, Any], long: bool = False) -> str:
    """杠杆展示。

    Kraken 期货只有全仓（flex），没有逐仓杠杆概念 —— KrInfo 的持仓行 `leverage`
    给 None，**不能**兜成 1x/5x（那是编出来的数字）。`long=True` 用于窄屏小字行，
    多带一句原因（「全仓」两个字在细字行里容易看不出是结论还是缺失）。
    """
    lev = _opt_num(pos.get("leverage"))
    if not lev:
        return "全仓（Kraken 不做逐仓杠杆设置）" if long else "全仓"
    return f"杠杆 {lev:.0f}x" if long else f"{lev:.0f}x"


def _fee_quoted(d: Dict[str, Any]) -> Optional[float]:
    """一笔成交的手续费（U 口径）：优先 ``fee_quote``（PR-1 写入时折算好的），
    为 NULL 时回退原始 ``fee``（PR-1 之前的历史行 / 非计价币行）。

    与执行器 ``_fees_since`` 严格同口径（#34 的读侧语义）：面板与净利计算看到同一个
    手续费数字，否则「面板说 0.35U、净利算 0.21U」永远对不上。
    """
    fq = _opt_num(d.get("fee_quote"))
    if fq is not None:
        return fq
    return _opt_num(d.get("fee"))


def _pnl_from_fills(cfg: Any) -> bool:
    """面板净额口径开关（hl_pnl_from_fills，默认关 = 交易所 closedPnl 口径）。

    默认关的理由见 ExecParams.pnl_from_fills：流水口径依赖 hl_fills 已同步完整，
    上线初期先让差异被记录，再决定是否切换读数。
    """
    try:
        return bool(getattr(cfg, "hl_pnl_from_fills", False))
    except Exception:                                            # noqa: BLE001
        return False


def _utc_day_start(ts: float) -> float:
    """当日 0 点（北京时间，与展示层「今日」一致；名字保留以免破坏调用方）。"""
    tz = _display_tz()
    d = dt.datetime.fromtimestamp(ts, tz).date()
    return dt.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()


def _to_seconds(v: Any) -> Optional[float]:
    """交易所时间戳单位不统一（ms/s）：>1e12 视为毫秒。"""
    f = _opt_num(v)
    if f is None:
        return None
    return f / 1000.0 if f > 1e12 else f


def _display_tz() -> dt.timezone:
    """展示时区（与 engine.DISPLAY_TZ 同源，env DISPLAY_TZ_OFFSET，默认北京时间）。

    用 float 解析（engine 侧也是 float）：int("8.0") 会抛 ValueError 打穿面板。
    """
    try:
        off = float(os.environ.get("DISPLAY_TZ_OFFSET", "8") or 8)
    except (TypeError, ValueError):
        off = 8.0
    return dt.timezone(dt.timedelta(hours=off))


def _fmt_dt(v: Any, pattern: str = "%m-%d %H:%M:%S") -> str:
    """时间展示：北京时间（原为 UTC，与 admin 面板/推送口径不一致，已统一）。"""
    f = _to_seconds(v)
    if f is None:
        return "—"
    try:
        return dt.datetime.fromtimestamp(f, _display_tz()).strftime(pattern)
    except (OverflowError, OSError, ValueError):
        return "—"


def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def normalize_rows(v: Any) -> int:
    """cookie/查询参数 → 合法行数；认不出一律回落默认（不改用途，只影响展示）。"""
    try:
        n = int(str(v or "").strip())
    except (TypeError, ValueError):
        return DEFAULT_ROWS
    return n if n in ROW_CHOICES else DEFAULT_ROWS


def _fmt_sz(v: Any) -> str:
    """数量展示：BTC 这类小数量按精度显示（0.00095 不能显示成 0.00）。"""
    f = _opt_num(v)
    if f is None:
        return "—"
    for dp in (2, 3, 4, 5, 6):
        if f == round(f, dp):
            return f"{f:,.{dp}f}".rstrip("0").rstrip(".") if dp > 2 else f"{f:,.2f}"
    return f"{f:,.6f}"


def _kv(label: str, value: str, cls: str = "", title: str = "") -> str:
    c = f" {cls}" if cls else ""
    t = f" title='{_esc(title)}'" if title else ""
    return f"<div class='kv'{t}><span class='k'>{label}</span><span class='v{c}'>{value}</span></div>"


def _asub(label: str, value: str, cls: str = "", title: str = "") -> str:
    """账户卡次级行（P2）：标签+数值的小字单元，flex 换行排开，不再逐行键值。

    cls 着色在数值 <b> 上（标签恒 muted 色，由 .acct-sub 统一给）。
    """
    c = f" class='{cls}'" if cls else ""
    t = f" title='{_esc(title)}'" if title else ""
    return f"<span{t}>{label} <b{c}>{value}</b></span>"


def _badge(dot_cls: str, label: str, value_html: str, extra_cls: str = "") -> str:
    """状态条徽章（HTML 值）。extra_cls 用于分层：badge-status（核心）/ badge-small（辅助）。"""
    extra = f" {extra_cls}" if extra_cls else ""
    return (f"<span class='badge{extra}'><span class='{dot_cls}'>&#9679;</span> "
            f"{label} <b>{value_html}</b></span>")


def _strip_badge(dot_cls: str, label: str, value: str, title: str = "",
                 extra_cls: str = "") -> str:
    """状态条徽章（/hl 与 /admin 两条状态条共用一种写法；admin_ui._status_strip 也用它）。"""
    t = f" title='{_esc(title)}'" if title else ""
    extra = f" {extra_cls}" if extra_cls else ""
    return (f"<span class='badge{extra}'{t}><span class='{dot_cls}'>&#9679;</span> "
            f"{label} <b>{value}</b></span>")


def _health_badge_html(system: Any, kill: bool = False, kill_reason: str = "") -> str:
    """系统健康徽章（P0-2 第一层）：熔断 + 对账 + 卡单 合成一个结论。

    三件事回答的是同一个问题（「系统现在能不能放心跑」），平铺三枚圆点只是刷屏：
    正常态收成一枚绿点「系统 正常」，异常时把出问题的那几项写进值里，红色优先于
    琥珀（熔断/对账不一致是真停手，卡单/无法核对只是需要注意）。正常态与异常态的
    口径细节都落在 title（桌面可悬停；手机上值里已经写清了出问题的那一项）。

    正常态**不出现「卡单」字样**（沿用 P3 R2 的降噪规则：正常时不该让人去猜
    「卡单」是什么）—— 卡单只在真的 >0 时才出现在文字里（值 + title）。
    对账仍保留三态：None（快照没读到）=「无法核对」琥珀，不是「一致」—— 把
    「不知道」说成「没问题」比不说更糟（审核 C1 口径不变）。
    """
    s = _as_dict(system)
    mismatch = s.get("mismatch")
    stuck = _opt_int(s.get("stuck_orders")) or 0
    reason = str(kill_reason or "").strip()
    parts: List[str] = []
    notes: List[str] = []
    dot = "pos"
    if kill:
        parts.append("熔断中" + (f" · {reason}" if reason else ""))
        notes.append("熔断：已实现亏损熔断已触发，自动开仓暂停，人工确认后解除")
        dot = "neg"
    else:
        notes.append("熔断：未触发")
    if mismatch is True:
        parts.append("对账不一致")
        notes.append("对账：本地账与交易所快照不一致")
        dot = "neg"
    elif mismatch is None:
        parts.append("对账无法核对")
        notes.append("对账：交易所数据没读到，账实是否一致无从判断")
        dot = "warn" if dot != "neg" else dot
    else:
        notes.append("对账：一致")
    if stuck > 0:
        parts.append(f"卡单 {stuck} 笔")
        notes.append("卡单：持续 3 分钟未推进才计入")
        dot = "warn" if dot != "neg" else dot
    if not parts:
        return _strip_badge("pos", "系统", "正常", " · ".join(notes), "badge-status")
    return _strip_badge(dot, "系统", " · ".join(_esc(x) for x in parts),
                        " · ".join(notes), "badge-status")


def _positions_badge_html(positions: Any, mr: Any = None, reachable: bool = True) -> str:
    """持仓徽章（P0-2 第一层）：几仓、什么方向。

    只做汇总：逐仓细节在「仓位」表里（那是唯一出处）。空仓给灰点「空仓」，
    有仓给绿点「N 仓 · 多/空」—— 一眼能看出「现在手里有没有东西」。

    QQQ 超跌腿（MR）与交易所仓位同一张表，所以它必须计入，否则会出现
    「徽章说空仓、表里有一行 QQQ」的自相矛盾。MR 腿单独标出（不同策略、不同离场口径），
    不混进「多/空」计数。

    ``reachable=False``（交易所读数不可得）→ 灰点「无法确认」，绝不写「空仓」：
    「读不到」和「没有」是两件事，全页最显眼的第一层徽章说错方向最贵（第七轮 B3）。

    有未被 MR 台账认领的 QQQ 仓（``mr_unclaimed``）→ 琥珀色并把「QQQ 异常」写进值里，
    否则那张真实的、没有止损、也不知道该由谁管的仓会被一句「N 仓」掩盖。
    """
    rows = [_as_dict(x) for x in _as_list(positions)]
    if not reachable:
        extra = f" · 含 {sum(1 for r in rows if r.get('mr_unclaimed'))} 个异常仓" if any(
            r.get("mr_unclaimed") for r in rows) else ""
        return _strip_badge("muted", "持仓", "无法确认" + extra,
                            "交易所读数不可得：既不能确认有仓，也不能确认空仓", "badge-status")
    mr_v = _mr_view({"mr": mr}) if mr else None
    mr_hold = bool(mr_v and mr_v.get("holding"))
    if not rows and not mr_hold:
        return _strip_badge("muted", "持仓", "空仓", "当前没有任何持仓", "badge-status")
    bits = ""
    unclaimed = sum(1 for r in rows if r.get("mr_unclaimed"))
    if rows:
        longs = sum(1 for r in rows if _num(r.get("side"), 1) >= 0)
        shorts = len(rows) - longs
        mix = f"多{longs} · 空{shorts}" if (longs and shorts) else ("多" if longs else "空")
        bits = f"{len(rows)} 仓 <span class='muted'>· {mix}</span>"
        if unclaimed:
            bits += f" <span class='warn'>· QQQ 异常 {unclaimed}</span>"
    if mr_hold:
        bits = (bits + " · " if bits else "") + "<span class='muted'>QQQ(MR)</span>"
    return _strip_badge("warn" if unclaimed else "pos", "持仓", bits,
                        "当前持仓数与方向（QQQ 为超跌 MR 腿，单列）；逐仓细节见「仓位」表"
                        + ("；QQQ 异常 = 交易所仓位没有被 MR 台账认领" if unclaimed else ""),
                        "badge-status")


def _agent_badge_html(agent: Any, extra_cls: str = "") -> str:
    """API 有效期徽章（P3 R3）：状态条是唯一常驻出处。

    ≤14 天琥珀、≤7 天红、已触发自动清仓红；不限期绿；读不到到期灰（如实显示，
    下一个整点会自动检查）。数字来自 hl_agent_state（DB 缓存，零网络调用）。
    extra_cls：分层后它落在辅助层，调用方传 badge-small。
    """
    a = _as_dict(agent)
    level = str(a.get("level") or "")
    cleanup = str(a.get("cleanup") or "none")
    days = _opt_int(a.get("days_left"))
    if cleanup == "done":
        return _strip_badge("neg", "API", "已清仓", "API 授权已到期并触发自动清仓", extra_cls)
    if str(a.get("source") or "") == "kraken-key-no-expiry":
        # Kraken Derivatives key 无有效期（PR-1），但**可用性必须三态**：
        # 「配置项非空」不等于「能签名」，更不等于「刚刚读通过」。
        if "configured" not in a:
            return _strip_badge("muted", "API", "—",
                                "凭据状态未知（该响应没有三态字段）", extra_cls)
        if not a.get("configured"):
            return _strip_badge("neg", "API", "未配置",
                                "Kraken key/secret 缺失或不成对：执行器无法签名，"
                                "私有读会失败（面板设置页「更新 API」）", extra_cls)
        if a.get("last_read_ok") is False:
            return _strip_badge("neg", "API", "读取失败",
                                "最近一次私有读取失败："
                                + str(a.get("verify_error") or "原因未知")
                                + "（无有效期不等于是可用的 key）", extra_cls)
        if a.get("verified") is True:
            age = _opt_num(a.get("verify_age_s"))
            when = "" if age is None else f"（{max(0.0, age):.0f} 秒前）"
            return _strip_badge("pos", "API", "已验证",
                                "最近一次调度轮私有读取成功" + when + "；Kraken key 无有效期",
                                extra_cls)
        return _strip_badge("warn", "API", "未验证",
                            "key/secret 成对存在，但还没有成功的私有读证据："
                            "下一轮调度读取成功后才算可用", extra_cls)
    if level == "no_expiry":
        return _strip_badge("pos", "API", "不限期", "API 授权未设到期时间", extra_cls)
    if days is None:
        return _strip_badge("muted", "API", "—", "API 授权有效期未知（每个整点自动检查）",
                            extra_cls)
    cls = "neg" if days <= 7 else ("warn" if days <= 14 else "pos")
    return _strip_badge(cls, "API", f"{days} 天",
                        "API 授权剩余有效期；≤3 天且有仓位时自动清仓（关闭走 runbook §6.H 的 DB 直改）",
                        extra_cls)


def _orders_normal(positions: List[dict], orders: List[dict]) -> bool:
    """挂单表按需渲染判定（P3 R1）。

    正常态 = 每个持仓标的恰好一条 reduce-only 触发单、无其它挂单、无孤儿 ——
    此时止损已由仓位表「止损 ✔️」列唯一表达，挂单表整块不渲染。
    判不清（挂单不可信 / 数据缺失）一律返回 False = 照常渲染（fail-open：
    宁可多显示，不可漏报）。纯渲染层判定，/api/hl/status 的 orders_open 口径不变。
    """
    # 挂单不可信（快照 orders_trusted=False）→ 止损列本身是「? 无法确认」，判不清。
    if any(_as_dict(p).get("stop_unknown") for p in positions):
        return False
    by_coin: Dict[str, int] = {}
    for od in (_as_dict(x) for x in orders):
        coin = str(od.get("coin") or "")
        if not (od.get("is_trigger") and od.get("reduce_only")):
            return False                      # 非止损类型挂单 = 异常态
        by_coin[coin] = by_coin.get(coin, 0) + 1
    pos_coins = {str(_as_dict(p).get("coin") or "") for p in positions}
    # 集合不等 = 有孤儿挂单 / 有仓没单；再逐标的数一遍保证「恰好一条」。
    if set(by_coin) != pos_coins:
        return False
    return all(n == 1 for n in by_coin.values())


def _td(text: str, cls: str = "") -> str:
    return f"<td class='{cls}'>{text}</td>" if cls else f"<td>{text}</td>"


def _rowd(r: Any) -> Dict[str, Any]:
    """sqlite Row / dict / 任意对象 → dict；脏数据返回空 dict（渲染永不因类型炸掉）。"""
    try:
        return dict(r)
    except Exception:
        return {}


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []


def _call(fn: Optional[Callable], default: Any = None) -> Any:
    """调用一个可能为 None / 会抛异常的依赖函数。"""
    if fn is None:
        return default
    try:
        r = fn()
        return default if r is None else r
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] 依赖调用失败：%s", e)
        return default


def _db_call(db: Any, name: str, *args) -> Optional[Any]:
    """调用 db.<name>(...)；方法缺失或抛异常都返回 None（面板必须能撑住坏数据）。

    注意 getattr 本身也要兜住：db 代理对象的 __getattr__ 可能直接抛异常。
    """
    try:
        fn = getattr(db, name, None)
    except Exception:                                              # noqa: BLE001
        return None
    if not callable(fn):
        return None
    try:
        return fn(*args)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] db.%s 失败：%s", name, e)
        return None


def _coin_of(symbol: Any) -> str:
    s = str(symbol or "").strip()
    return s.split("_")[0] if s else ""


def _tick_from_decimals(dec: Any) -> Optional[float]:
    try:
        d = int(dec)
    except (TypeError, ValueError):
        return None
    return 10.0 ** (-d) if 0 <= d <= 12 else None


def _sz_tick(info: Any, coin: str, snap: Optional[dict] = None) -> Optional[float]:
    """该 coin 的数量步长（`10 ** -sz_decimals`）；**零网络**拿不到就返回 None。

    止损覆盖判据（`app/hl/stops.covering_stop_px`）用它做「挂单张数够不够」的容差：
    有精度就用交易所精度，没有就退化成 `sz × 1e-6` 的相对噪声容差（更严）。

    只有两个来源，都不触发网络（第七轮审核 #8：PR21 这里直接调 `info.specs()`，
    冷缓存时会让 `/admin` 与 `/api/hl/status` 各多打一次 `/instruments`）：

    1. 调度器随快照写下的 `sz_ticks`（`main._panel_sz_ticks`，首选；老快照没有这个键）；
    2. 只读客户端**已经热了**的规格缓存（`KrInfo._specs_cache`，1h TTL，执行器预热）。

    **不调用 `specs()`**：它是「查一次、可能外呼」的接口，渲染层拿到的对象是不是热的
    只有它自己知道 —— 生产里 KrInfo 有缓存属性（未热时是 None），但降级适配层/新客户端
    未必有，留一个「没有缓存属性就问一次」的后门等于把零网络的红线交给类型判断。
    未热 / 没有缓存 → 返回 None（精度未知）：判据退化成更严的相对容差，宁严不松。
    """
    if not coin:
        return None
    ticks = (snap or {}).get("sz_ticks") if isinstance(snap, dict) else None
    if isinstance(ticks, dict) and coin in ticks:
        got = _opt_num(ticks.get(coin))
        if got and got > 0:
            return got
    if info is None:
        return None
    cache = getattr(info, "_specs_cache", None)
    if cache is None:
        # 没有缓存属性（降级适配层）或缓存未热（None）：精度未知，不外呼。
        return None
    try:
        spec = (cache or {}).get(coin) or {}
    except (TypeError, AttributeError):
        return None
    return _tick_from_decimals(spec.get("sz_decimals", spec.get("szDecimals")))


def _stop_order_dicts(orders: Any) -> List[dict]:
    """快照挂单 → 判据可直读的 dict 列表（缺键补默认值）。

    `covering_stop_px` 按 key 直读 `order_type`/`is_buy`/`sz`/`trigger_px`，而老快照行或
    降级适配层可能缺这些键；缺键会让整个 `/api/hl/status` 抛 KeyError。这里统一补齐：
    缺 `order_type` → None（「未知」，不当止盈单放行，也不当止损单放行），
    其余数值缺键 → 0/False，与下方 `orders_open` 的序列化口径一致。
    """
    out: List[dict] = []
    for o in orders or []:
        od = _rowd(o)
        if not od:
            continue
        out.append({
            "coin": str(od.get("coin") or ""),
            "is_buy": bool(od.get("is_buy")),
            "sz": _num(od.get("sz")),
            "reduce_only": bool(od.get("reduce_only")),
            "is_trigger": bool(od.get("is_trigger")),
            "trigger_px": _opt_num(od.get("trigger_px")) or 0.0,
            "order_type": od.get("order_type"),
        })
    return out


# 显示口径：信号层用的是 MEXC 的合约名（BTC_USDT），但真实成交发生在 Hyperliquid，
# 那里合约就叫 BTC / ETH。面板一律显示 HL 侧的名字，免得看的人以为在交易 USDT 对。
# 内部键（signal_bar_ms、幂等键、DB 里的 symbol）保持不动 —— 只换展示。
_DISP_COIN = {"BTC_USDT": "BTC", "ETH_USDT": "ETH"}
# 其它合约名（SOL_USDT…）的通用展示剥离：与 _disp 的后缀口径一致（2026-11-07）。
_GENERIC_SYM_RE = re.compile(r"\b([A-Z]{2,10})_(?:USDT|USDC|USD)\b")


def _disp(sym: Any) -> str:
    s = str(sym or "").strip()
    if not s:
        return ""
    if s in _DISP_COIN:
        return _DISP_COIN[s]
    for suf in ("_USDT", "_USDC", "_USD"):
        if s.upper().endswith(suf):
            return s[: -len(suf)]
    return s


def _disp_text(text: Any) -> str:
    """展示用：把日志文案里的信号层合约名换成 HL 侧合约名。

    DB 里存的原文本不动（审计保真），只有页面渲染走这里 ——
    否则交易后事件列表会出现「BTC_USDT 已开仓 0.00833 BTC」这种混着两种名字的句子。
    已知映射（BTC/ETH）先精确替换；其余合约（如 SOL_USDT，2026-11-07 补）走通用
    后缀剥离，与 ``_disp`` 的口径一致 —— 页面任何角落都不出现内部合约名。
    """
    out = str(text or "")
    for k, v in _DISP_COIN.items():
        out = out.replace(k, v)
    return _GENERIC_SYM_RE.sub(r"\1", out)


def _addr_text(a: Any) -> str:
    s = str(a or "").strip()
    if not s:
        return "<span class='muted'>未配置</span>"
    return f"<span class='mono' title='{_esc(s)}'>{_esc(s[:6])}&hellip;{_esc(s[-4:])}</span>"


# ---------------------------------------------------------------- JSON 组装
def _config_get(deps: DashboardDeps, key: str, default: str = "") -> str:
    """读 configuration 表的一个字符串值（DB 里存的是 json.dumps 过的）。

    优先用 db.get_config_str（已解 JSON）；没有该方法时退回 get_config + json.loads。
    """
    miss = "\x00__MISS__"
    v = _db_call(deps.db, "get_config_str", key, miss)
    if isinstance(v, str) and v != miss:
        return v
    raw = _db_call(deps.db, "get_config") or {}
    try:
        raw_v = raw.get(key)
    except Exception:                                            # noqa: BLE001
        raw_v = None
    if raw_v in (None, ""):
        return default
    try:
        val = json.loads(raw_v)
    except (TypeError, ValueError):
        return str(raw_v)
    return default if val is None else str(val)


def _kill_state(deps: DashboardDeps) -> tuple:
    try:
        on = bool(deps.db.hl_kill())
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] hl_kill 失败：%s", e)
        on = False
    return on, _config_get(deps, "hl_exec_kill_reason")


# 回撤熔断（DD Guard）状态词。iOS 没有 title 悬停 → 状态必须是**可见文字**（§7）。
# 规格只给了四个词（正常/已达告警线/已熔断/暂不可用）；总开关关掉时**复用「暂不可用」**
# 而不是新造第五个词，把原因写进括号里 —— 判据确实不可用，词汇表保持封闭。
_DD_ST_TXT = {
    "normal": "正常",
    "warn": "已达告警线",
    # 【低3】「已熔断」必须带上「停手中」：这个词只允许在 kill **实态为真** 时出现。
    # 旧文案就四个字，用户无法判断「是还在停手，还是指标越线但已经恢复交易」。
    "killed": "已熔断（停手中）",
    # 【低3】DD 越了熔断线、但 kill 实态是假的（例如人工解熔断后峰值还没重置，
    # 或 monitor 还没跑到下一轮）：既不能说「已熔断」（已恢复交易，会误导人以为
    # 还在停手），也不能说「正常」（下一轮就会重新熔断，会误导人现在可以开仓）。
    "over_kill": "已越熔断线（当前未停手）",
    "unavailable": "暂不可用",
    "off": "暂不可用",
}


def _flows_supported(deps: Any) -> bool:
    """该交易所有没有**出入金流水**端点。

    Kraken Futures 没有公开的流水端点（`KrInfo.flows_supported = False`），
    所有「剔除出入金的盈亏曲线 / 回撤熔断 / 资金费 / 资金流水」在那边都没有数据源。
    渲染层据此整块不画，而不是画一个恒空的「暂不可用」。
    """
    return getattr(getattr(deps, "info", None), "flows_supported", True) is not False


def _dd_meta(deps: DashboardDeps, cfg: Any, flows_ok: bool) -> Dict[str, Any]:
    """总览「策略回撤」那一行的数据源：**只读**持久化状态，不重算 TWR 链。

    为什么不重算：判据是执行器每轮推进的复利链（每轮一格），面板若按快照逐点重算，
    得到的是另一条曲线（采样密度不同）→ 用户看到的数字与真正触发熔断的数字不一致，
    那比不显示更糟。这里只做「读状态 + 算 DD + 选状态词」。
    """
    risk = _num(getattr(cfg, "risk_pct", 0.02) if cfg is not None else 0.02, 0.02)
    th = hl_dd.thresholds(
        risk,
        _num(getattr(cfg, "hl_dd_warn_pct", 0.0) if cfg is not None else 0.0),
        _num(getattr(cfg, "hl_dd_kill_pct", 0.0) if cfg is not None else 0.0))
    out: Dict[str, Any] = {
        "dd_ok": False, "dd_status": "unavailable",
        "dd_status_text": _DD_ST_TXT["unavailable"],
        "dd_pct": None, "dd_index": None, "dd_peak_index": None, "dd_asof": None,
        "dd_warn_pct": round(th["warn"], 6), "dd_kill_pct": round(th["kill"], 6),
        "dd_auto": bool(th["auto"]), "dd_note": "",
    }
    if not bool(getattr(cfg, "hl_dd_guard", True) if cfg is not None else True):
        out.update(dd_status="off", dd_status_text=_DD_ST_TXT["off"],
                   dd_note="回撤熔断已关闭")
        return out
    if not flows_ok:
        # 流水读不到 → 指数的历史段不可信（少一笔入金就会被当成亏损）→ 不显示数字。
        # 若该 venue 根本没有出入金流水端点（Kraken 期货），要说「未启用」而不是
        # 「读取失败」——后者会让人以为重试几次就会好。
        if getattr(getattr(deps, "info", None), "flows_supported", True) is False:
            out["dd_note"] = "该交易所无出入金流水源 → 回撤/日亏熔断未启用"
        else:
            out["dd_note"] = "出入金流水读取失败"
        return out
    st = hl_dd.load_state(deps.db)
    if not st:
        out["dd_note"] = "尚未计算（等执行器跑一轮）"
        return out
    pct = hl_dd.drawdown(st.get("index"), st.get("peak_index"))
    if pct is None:
        out["dd_note"] = "状态数据不完整"
        return out
    try:
        killed = bool(deps.db.hl_kill())
    except Exception as e:                                       # noqa: BLE001
        logger.warning("[hl-views] hl_kill 失败（按未熔断显示）：%s", e)
        killed = False
    # 「已熔断」既看当前回撤，也看 kill 的来源：指数回升后 DD 可能掉回线下，
    # 但停手还在（dd 永不自解）—— 那种时候显示「正常」会让人以为可以开仓了。
    origin = str(_config_get(deps, "hl_exec_kill_origin", "") or "")
    over = pct >= th["kill"]
    # 【低3】「已熔断」只允许在 kill **实态为真** 时出现。旧判据
    # `pct >= kill or (killed and origin == "dd")` 把「DD 越线但已恢复交易」
    # 也显示成「已熔断」—— 用户以为还在停手，实际下一轮就要重新熔断前仓位已开得出去。
    # 四态：killed（真停手）/ over_kill（越线未停手）/ warn / normal。
    if killed and (origin == "dd" or over):
        status = "killed"
    elif over:
        status = "over_kill"
    elif pct >= th["warn"]:
        status = "warn"
    else:
        status = "normal"
    out.update(dd_ok=True, dd_status=status, dd_status_text=_DD_ST_TXT[status],
               dd_pct=round(pct, 6), dd_index=round(float(st["index"]), 4),
               dd_peak_index=round(float(st["peak_index"]), 4),
               dd_asof=float(st.get("updated_at") or 0.0) or None)
    # 【v2·高1】执行器「本轮没判」的说明（只有流水接口失败/无地址时才写，闸门内跳过
    # 属正常节奏、不写）。这里只读、不自己推断 —— 判据有没有真的跑过只有执行器知道。
    wait = str(_config_get(deps, hl_dd.WAIT_KEY, "") or "").strip()
    if wait:
        out["dd_wait_note"] = wait
    return out



def _reconcile(reachable: bool, snap_positions: List[dict], snap_orders: List[dict],
               open_by_coin: Dict[str, dict], stuck_orders: List[Any],
               orders_trusted: bool = True,
               own_coins: Optional[set] = None) -> Optional[bool]:
    """对账（口径见模块 docstring），三态返回：True 不一致 / False 一致 / None 无法核对。

    `orders_trusted=False`（挂单接口回落/非 list）时**跳过止损判断**——
    与执行器同一口径：读不到 isTrigger 时把「有止损」读成「没止损」，
    据此判 mismatch 会吓用户去手动平仓。

    `reachable=False` → None（不是 False）：快照没读到就既证明不了不一致、也证明不了
    一致，写成 False 会让面板顶部的徽章显示「对账 一致」——那是把「不知道」说成
    「没问题」，正是这类静默 fail-open 最危险的地方。卡单（只看本地 hl_orders）
    与快照无关，仍然照判。

    `own_coins`（主策略标的集合，cfg.symbols 的合约名）：给了就只对**这些标的**判
    「交易所有仓、本地无记录 = 人工单」。Kraken 的账户是**全账户**快照，MR 腿（QQQ）
    也挂在同一个账户上，而 positions 表里本来就不该有它的行 —— 不排除的话，MR 持仓
    期间状态条会永久显示「对账 不一致」，把一个正常状态说成异常（与执行器 B4 同口径）。
    传 None（默认）= 保持旧行为（任何未记录仓位都算不一致）。
    """
    if stuck_orders:
        return True
    if not reachable:
        return None
    snap_coins = {str(p.get("coin") or "") for p in snap_positions if str(p.get("coin") or "")}
    for coin, row in open_by_coin.items():
        if coin not in snap_coins:
            return True
        if row.get("hl_stop_oid") and orders_trusted:
            # 与止损列/执行器同一口径（app/hl/stops.py）：旧实现只判
            # coin + reduce_only + is_trigger，一张同向小额止盈单就能让对账显示「一致」，
            # 而同一行的止损列却是 ❌ —— 两个结论互相矛盾，用户不知道信哪个。
            try:
                _side = int(row.get("side") or 1)
                _sz = float(row.get("hl_sz") or row.get("coin_qty") or 0.0) or None
                _entry = float(row.get("actual_entry") or row.get("entry_price") or 0.0) or None
            except (TypeError, ValueError):
                _side, _sz, _entry = None, None, None
            if hl_stops.covering_stop_px(snap_orders, coin, side=_side, sz=_sz,
                                         entry_px=_entry) is None:
                return True
    for coin in snap_coins:
        if coin in open_by_coin:
            continue
        if own_coins is not None and coin not in own_coins:
            # 非主策略标的（MR 腿 QQQ 之类）：DB 的 positions 表不会有它的行，不算人工单
            logger.debug("[hl-views] 对账跳过非主策略标的 %s（MR 腿/外部标的）", coin)
            continue
        return True
    return False


def _symbol_state_rows(deps: DashboardDeps, db_rows: List[Any],
                       symbols: List[str]) -> List[dict]:
    """symbol_state 表 → 「三标的动态」每标的一条（缺行/缺字段一律 None，永不抛异常）。

    顺序与配置的标的顺序一致；`position_side` 只看 status='open' 的仓位行
    （pending 不算仓位，与「仓位」表同口径）。
    """
    cached: Dict[str, dict] = {}
    for r in (_db_call(deps.db, "symbol_states") or []):
        d = _rowd(r)
        sym = str(d.get("symbol") or "")
        if sym:
            cached[sym] = d
    open_side: Dict[str, int] = {}
    for r in db_rows:
        d = _rowd(r)
        if str(d.get("status")) == "open":
            sym = str(d.get("symbol") or "")
            if sym:
                open_side[sym] = int(_num(d.get("side"), 1))
    out: List[dict] = []
    for sym in symbols:
        d = cached.get(sym) or {}
        out.append({
            "symbol": sym,                       # 内部键（保留）
            "coin": _disp(sym),                  # 展示用：HL 侧合约名
            "price": _opt_num(d.get("price")),
            "sma20": _opt_num(d.get("sma20")),
            "sma200": _opt_num(d.get("sma200")),
            "long_trigger": _opt_num(d.get("long_trigger")),
            "short_trigger": _opt_num(d.get("short_trigger")),
            "dense_cnt": _opt_int(d.get("dense_cnt")),
            "updated_at": _opt_num(d.get("updated_at")),
            "position_side": open_side.get(sym),
        })
    return out


def _perf_stats(rows: List[Any]) -> Dict[str, Any]:
    """「策略绩效（上线以来）」的只读聚合：positions 表 status='closed' 行 → 统计量。

    过滤口径（血泪防坑，见规格 P1）：
    - 库里混有 2026-10-14 彩排/演练的**合成行**：其 signal_bar_ms 不在 4h 网格上
      （4h = 14_400_000 ms，UTC 对齐），不对齐的一律不计入 —— 宁可少算一笔真实交易，
      也不让排练数据污染均R/胜率。
    - ``r_multiple`` 为 NULL 的行不计入笔数（没算过 R ≠ R 为 0）。
    - side 约定 1=多 / -1=空（与 positions 表 DDL 及既有展示层一致，历史上映射写反过，
      这里只按 DDL 口径分组，不做任何「聪明」的纠偏）。
    """
    bar_ms = 4 * 3600 * 1000                     # 4h 信号网格（UTC 对齐）
    n = long = short = wins = 0
    r_sum = 0.0
    for r in rows or []:
        d = _rowd(r)
        bar = _opt_num(d.get("signal_bar_ms"))
        if bar is None or bar <= 0 or int(bar) % bar_ms != 0:
            continue                             # 合成行/脏行：不计入
        r = _opt_num(d.get("r_multiple"))
        if r is None:
            continue                             # 没算过 R：不计入笔数
        n += 1
        if int(_num(d.get("side"), 1)) < 0:
            short += 1
        else:
            long += 1
        r_sum += r
        if r > 0:
            wins += 1
    if n <= 0:
        return {"n": 0, "long": 0, "short": 0, "wins": 0, "avg_r": None,
                "win_rate": None, "total_r": None}
    return {"n": n, "long": long, "short": short, "wins": wins,
            "avg_r": round(r_sum / n, 4),
            "win_rate": round(wins / n, 4),
            "total_r": round(r_sum, 4)}


#: `account` 里所有「金额」键。交易所读不到时统一置 None（不是 0），渲染层显示「—」。
_ACCT_NUM_KEYS = ("equity", "perp_equity", "withdrawable", "margin_used", "notional",
                  "spot_usdc", "spot_hold", "today_realized", "total_realized",
                  "fees_total", "funding_total")


def status_payload(deps: DashboardDeps, *, include_curve_points: bool = True) -> dict:
    """组装监控 JSON（schema_version=1）。任何字段缺失/接口失败都不抛异常。

    `include_curve_points=False` 时省去 `hl_snapshots` 30 天全量读取与逐点计算（约 440KB JSON），
    `curve.pnl`/`curve.twr` 置空列表，但保留 `curve.meta` 的回撤熔断（DD Guard）与分窗净入金状态。
    此时**由点列导出的汇总数一律置 None 并标 `curve_omitted=True`** —— 它们是「没算」而不是 0，
    写成 0 会让消费方把「省了 440KB」读成「这账户没赚没亏、最大回撤 0」（2026-11-08 审核 P3）。
    """
    now = _num(_call(deps.now_fn), time.time())
    address = ""
    try:
        address = str(deps.address_fn() or "")
    except Exception:                                             # noqa: BLE001
        address = ""
    base_url = str(_call(deps.base_url_fn, MAINNET_DEFAULT) or MAINNET_DEFAULT)
    mode = str(_call(deps.mode_fn, "off") or "off")
    cfg = _call(deps.config_fn)
    # venue 是否提供出入金流水（Kraken 期货：否）。渲染层用它决定整块不画。
    flows_supported = _flows_supported(deps)
    # venue 有没有「钱包地址」这个概念（Kraken 期货：没有）。面板据此不渲染「钱包地址」行。
    address_free = bool(getattr(getattr(deps, "info", None), "venue_address_free", False))

    # ---------------- 交易所快照 ----------------
    snap: Optional[dict] = None
    net_err = ""
    t0 = time.time()
    if deps.info is None:
        net_err = "未配置只读客户端"
    elif not address and not address_free:
        # HL：address 空 → 拦住（公开主网数据会误导成自己的账户）。
        # Kraken（venue_address_free）：没有地址概念，空 address 照常拉快照。
        net_err = "未配置钱包地址"
    else:
        try:
            snap = deps.info.snapshot(address)
        except Exception as e:                                    # noqa: BLE001
            net_err = f"{type(e).__name__}: {e}"
            logger.warning("[hl-views] snapshot 失败：%s", e)
    latency_ms = round((time.time() - t0) * 1000.0, 1)
    reachable = bool(snap is not None and not net_err)
    snap = snap or {}
    # 读数来自调度器快照时（阶段 B），上面那个 latency_ms 只是「读一次 SQLite」的耗时（毫秒级），
    # 不是网络延迟。快照里带了调度器写入时实测的那一轮耗时（`panel_net`）就用它，并标记来源 +
    # 采样距今多久 —— 面板据此说明「这是 N 分钟前那一轮的耗时」，而不是假装刚刚测过。
    latency_from_snapshot = False
    latency_age_s: Optional[float] = None
    panel_net = snap.get("panel_net")
    stored_ms = _opt_num(panel_net.get("latency_ms")) if isinstance(panel_net, dict) else None
    if stored_ms is not None and stored_ms > 0:      # ≤0 视为没测出来
        latency_ms = stored_ms
        latency_from_snapshot = True
        sampled = _opt_num(panel_net.get("ts"))
        if sampled:
            latency_age_s = round(max(0.0, now - sampled), 1)
    elif snap.get("panel_snapshot_age_s") is not None:
        # 读数确实来自快照、但快照里没有采样值（升级前写的老行）⇒ 报「没测」（None），
        # 不能把上面那次 SQLite 读取的 0~1 ms 当成网络延迟（这正是不显示「1 ms」的原因）。
        latency_ms = None
    snap_positions: List[dict] = snap.get("positions") or []
    snap_orders: List[dict] = snap.get("open_orders") or []
    # 挂单列表可信度：**唯一来源**是快照的 orders_trusted（挂单接口回落/非 list 时 False）。
    # 顶层也发给 /api/hl/status（监控契约）：snap 读不到 = {} → 一律 False（宁缺勿信）。
    # 严格布尔：只有 JSON 布尔 true 可信（`bool("false")` == True，会把脏值翻成可信，
    # 方向正好反了）。与监控端 fail-closed 同口径。
    # 下面「止损列」与 system.mismatch 对账都用这同一个布尔，不许各算一份。
    orders_trusted = snap.get("orders_trusted") is True

    # ---------------- DB 侧 ----------------
    db_rows = _db_call(deps.db, "recent_positions", 50) or []
    open_by_coin: Dict[str, dict] = {}
    for r in db_rows:
        d = _rowd(r)
        coin = _coin_of(d.get("symbol"))
        if coin and str(d.get("status")) == "open":
            open_by_coin[coin] = d

    # ---------------- MR 腿状态（**先算**：下面「仓位」表要按它分层去重）----------------
    # 第七轮 B1：交易所快照里本来就有 QQQ，`_render_body` 又会追加一行 MR ——
    # 同一张仓两行，普通那行必然「❌ 无止损」。去重规则必须是「MR **已认领**才隐藏
    # 普通行」，而不是按币种一律删：一律删会把「真实存在、但 MR 台账不认」的 QQQ 仓
    # （手工单 / 台账丢失 / 数量对不上）藏起来，那是最需要被看见的一类异常。
    mr_out: Optional[dict] = None
    if deps.mr_fn is not None:
        try:
            raw_mr = deps.mr_fn()
        except Exception:                                         # noqa: BLE001
            raw_mr = {"state": "error", "error": "mr status read failed"}
        mr_out = dict(raw_mr) if raw_mr else None
    if not (isinstance(mr_out, dict) and _as_dict(mr_out.get("pos"))):
        # 运行时没有 MR 持仓（停用 / 尚未 tick / 刚重启）→ 用台账补位。
        # **只读**：不改 MR 状态机、不下单，只让「交易所那张仓」有个出处。
        ledger: Optional[dict] = None
        try:
            row = _db_call(deps.db, "mr_open_trade")
            if row is not None:
                ld = _rowd(row)
                lqty = _opt_num(ld.get("qty"))
                if lqty and lqty > 0:
                    ledger = {
                        "qty": lqty,
                        "entry_price": _opt_num(ld.get("entry_price")),
                        "signal_date": ld.get("signal_date"),
                        "entry_session": ld.get("entry_session"),
                        "entry_ts": _opt_num(ld.get("entry_ts")),
                        "state": str(ld.get("state") or "OPEN"),
                    }
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[hl-views] 读 MR 台账失败（不展示台账仓）：%s", e)
            ledger = None
        if ledger:
            if mr_out is None:
                mr_out = {"enabled": False, "state": "unknown", "recon": "unknown"}
            mr_out["ledger_pos"] = ledger
    _mr_now = _mr_view({"mr": mr_out}) if mr_out else None
    mr_claim_qty = _opt_num(_mr_now.get("qty")) if _mr_now else None
    mr_holding = bool(_mr_now and _mr_now.get("holding"))

    # ---------------- 持仓（快照为主，补 DB 的 signal/stop 信息）----------------
    # 判据要按 key 直读挂单（含 order_type），先统一补齐缺键，避免老快照行触发 KeyError。
    stop_orders = _stop_order_dicts(snap_orders)
    positions_out: List[dict] = []
    for p in snap_positions:
        d = _rowd(p)
        coin = str(d.get("coin") or "")
        row = open_by_coin.get(coin)
        size = _num(d.get("size"))
        entry = _num(d.get("entry_px"))
        mark = _num(d.get("mark_px"))
        upnl = _num(d.get("unrealized_pnl"))
        # 分层去重：MR 已认领（有持仓且数量对得上）才把普通行让给 MR 行；
        # 否则保留并标成异常（`mr_unclaimed`），渲染层会把它单独喊出来。
        unclaimed = coin in MR_COINS
        if unclaimed:
            tick = _sz_tick(deps.info, coin, snap) or 0.0
            tol = tick * 0.5 + 1e-12
            if (mr_holding and mr_claim_qty and
                    abs(size - float(mr_claim_qty)) <= tol):
                continue

        # 方向以快照 side 为准（size 是绝对值，不能用来推方向；旧兜底恒返回 1）。
        try:
            side = int(d.get("side")) if d.get("side") not in (None, "") else 1
        except (TypeError, ValueError):
            side = 1

        # 止损覆盖判据与执行器/探针**同源**（app/hl/stops.covering_stop_px）：同 coin、
        # reduce_only、is_trigger、非止盈类型、trigger_px>0、方向相反、触发价在亏损侧、
        # 张数够覆盖，全部由那一份函数判。以前这里内联只看 coin+reduce_only+is_trigger，
        # 于是「买向 0.001 张的盈利侧止盈单」也能让面板显示「已有止损」，与执行器/探针
        # 互相矛盾（2026-11-13 审核 P2-1）。
        stop_px: Optional[float] = None
        has_stop = False
        trusted = orders_trusted
        if trusted:
            stop_px = hl_stops.covering_stop_px(
                stop_orders, coin, side=side, sz=size, entry_px=entry,
                sz_tick=_sz_tick(deps.info, coin, snap))
            has_stop = stop_px is not None

        risk = abs(entry - stop_px) * size if (stop_px and entry and size) else 0.0
        # 没有（可信的）止损就没有 R 与「止损距离」—— None → 渲染「—」。
        # 旧版写 0.0：`+0.00R` 读起来像「刚好打平」、`0.00%` 像「止损就在现价」，
        # 两个数都是凭空造的，而这一行恰恰标着「❌ 无」（第七轮 #5）。
        r_unrealized = round(upnl / risk, 4) if risk > 0 else None
        if has_stop and stop_px and mark:
            stop_dist_pct = abs(stop_px - mark) / mark
        else:
            stop_dist_pct = None

        positions_out.append({
            "coin": coin,
            "side": side,
            "size": size,
            "entry_px": entry,
            "mark_px": mark,
            "position_value": _num(d.get("position_value")),
            "liq_px": _num(d.get("liq_px")),
            # Kraken 全仓没有逐仓杠杆：KrInfo 给 None，**保留 None**（早先兜成 1
            # 会让界面把「全仓」写成 1x，等于编了一个用户没设过的杠杆）。
            "leverage": _opt_int(d.get("leverage")),
            "isolated": bool(d.get("isolated")),
            "margin_used": _num(d.get("margin_used")),
            "unrealized_pnl": upnl,
            "stop_px": stop_px,
            "has_stop": has_stop,
            "stop_unknown": has_stop is False and not trusted,   # 挂单不可信：显示「无法确认」而非 ❌
            "stop_dist_pct": stop_dist_pct,
            "r_unrealized": r_unrealized,
            # 单粒度资金费（feat/funds-ledger）：快照的 cumFunding.sinceOpen 是**成本
            # 视角**（付出为正），与面板「正=收入」口径相反，此处取反 —— 与账户级
            # funding_total 同一套换算（见下方 funding_total 注释）。
            "funding_income": -_num(d.get("funding_since_open")),
            "signal_bar_ms": int(row.get("signal_bar_ms")) if row is not None else None,
            "position_id": int(row.get("id")) if row is not None else None,
            # 交易所里有 QQQ、但 MR 台账没认领它 → 异常仓，渲染层要显式提示。
            "mr_unclaimed": bool(unclaimed),
        })

    # ---------------- 挂单 ----------------
    orders_open: List[dict] = []
    for o in snap_orders:
        od = _rowd(o)
        orders_open.append({
            "coin": str(od.get("coin") or ""),
            "oid": od.get("oid"),
            "is_buy": bool(od.get("is_buy")),
            "sz": _num(od.get("sz")),
            "limit_px": _num(od.get("limit_px")),
            "reduce_only": bool(od.get("reduce_only")),
            "trigger_px": _opt_num(od.get("trigger_px")) or 0.0,
            "is_trigger": bool(od.get("is_trigger")),
            # 下游要区分止盈单与止损单：order_type 原样透传（Kraken 用 take_profit），
            # 快照没给就是 None（未知 ≠ 止损），不在这里猜。
            "order_type": od.get("order_type"),
            "timestamp": _to_seconds(od.get("timestamp")),
        })

    orders_recent: List[dict] = []
    for r in (_db_call(deps.db, "hl_recent_orders", 50) or []):
        d = _rowd(r)
        orders_recent.append({
            "symbol": d.get("symbol"),
            "coin": d.get("coin"),
            "purpose": d.get("purpose"),
            "status": d.get("status"),
            "sz": _opt_num(d.get("sz")),
            "px": _opt_num(d.get("px")),
            "oid": d.get("oid"),
            "filled_sz": _opt_num(d.get("filled_sz")),
            "avg_px": _opt_num(d.get("avg_px")),
            "error": d.get("error"),
            "updated_at": _opt_num(d.get("updated_at")),
        })

    # ---------------- 成交 / 盈亏 ----------------
    # 审计三轮 M8：hl_fills_since 是 ORDER BY id ASC（最旧在前），行数超过截断数后
    # 「最近成交」会全是最老数据。改用 hl_recent_fills（id DESC）取最近 N 条；
    # 今日/累计已实现与手续费仍按这批全量算（保留窗口内语义）。
    fills_rows = _db_call(deps.db, "hl_recent_fills", 2000) or []
    fills_out: List[dict] = []
    today_realized = 0.0
    window_realized = 0.0          # 窗口口径兜底（fills_totals 缺失时才用）
    window_fees = 0.0
    day_start = _utc_day_start(now)
    for r in (fills_rows or []):
        d = _rowd(r)
        cp = _num(d.get("closed_pnl"))
        window_realized += cp
        # 费用口径统一走 fee_quote（NULL 回退 fee）：非计价币行的 fee 是「币的数量」，
        # 直接当 U 累加会把 0.2 ARB 记成 0.2U（#34 修的就是这个，读侧漏了这里）。
        window_fees += _fee_quoted(d) or 0.0
        t_s = _to_seconds(d.get("ts"))
        if t_s is not None and day_start <= t_s < day_start + _DAY:
            today_realized += cp
        fills_out.append({
            "ts": _opt_num(d.get("ts")),
            "coin": d.get("coin"),
            "side": d.get("side"),
            "sz": _opt_num(d.get("sz")),
            "px": _opt_num(d.get("px")),
            # 逐笔也走 fee_quote（口径同 fees_total，逐笔加起来等于合计）
            "fee": _fee_quoted(d),
            "fee_token": d.get("fee_token"),
            "dir": d.get("dir"),
            "closed_pnl": _opt_num(d.get("closed_pnl")),
            "reduce_only": bool(d.get("reduce_only")),
        })
    # 累计已实现 / 净手续费走**全表**聚合（db.fills_totals）：以前是拿上面这批
    # 「最近 2000 条成交」求和当累计 —— 成交一多，面板上的「累计」就静默缩水成
    # 「最近一段」。方法缺失（老库/测试桩）时退回窗口和：宁可与以前一样少算，
    # 也不能把真实数字变成 0。
    _totals = _db_call(deps.db, "fills_totals")
    if isinstance(_totals, (tuple, list)) and len(_totals) >= 2:
        total_realized = _num(_totals[0])
        fees_total = _num(_totals[1])
    else:
        total_realized, fees_total = window_realized, window_fees

    # 资金费取「收付视角」：HL 的 cumFunding.sinceOpen 是**仓位成本视角**（付出为正，
    # 空头收钱时该值为负），与 userFunding.delta.usdc 的**入账视角**（正=收入）符号相反。
    # 面板统一按「正=收入」渲染，所以此处对每个 sinceOpen 取反；
    # payload 的 funding_total（及 /api/hl/* 消费者）与总览同用这一个口径。
    funding_total = sum(-_num(d.get("funding_since_open")) for d in (_rowd(p) for p in snap_positions))
    unrealized_pnl = sum(p["unrealized_pnl"] for p in positions_out)

    # ---------------- 信号 ----------------
    signals: List[dict] = []
    pnl_from_fills = _pnl_from_fills(cfg)      # 净额口径开关（默认关）
    # 单粒度资金费（feat/funds-ledger）：{position_id: Σ amount_usd}，一次分组查询
    # 拿全量，循环里只查字典。给「已平仓行」的资金费小字用（持仓中的在仓位卡列）。
    fund_by_pos = _db_call(deps.db, "hl_funding_by_position") or {}
    for r in db_rows:
        d = _rowd(r)
        entry = d.get("actual_entry")
        if entry in (None, ""):
            entry = d.get("entry_price")
        stop = d.get("actual_stop")
        if stop in (None, ""):
            stop = d.get("stop_price")
        sig = {
            "symbol": d.get("symbol"),          # 内部键（信号层 MEXC 名），保留不变
            "coin": _disp(d.get("symbol")),     # 展示用：HL 侧合约名
            "status": d.get("status"),
            "side": int(_num(d.get("side"), 1)),
            "entry": _opt_num(entry),
            "stop": _opt_num(stop),
            "contracts": d.get("contracts"),    # 原始 MEXC 口径张数（兼容字段，展示不用）
            # 双口径收敛（app/posfields.py）：数量走统一读侧 —— HL 仓 = 真实币量
            # （actual_contracts/hl_sz），历史 MEXC 行 = 张数。行里的 contracts 在
            # HL 仓上是 MEXC 人工口径的「张数」，直接显示会出现「85 张 vs 0.00084 BTC」。
            "qty": _opt_num(posfields.qty(d)),
            "qty_unit": posfields.qty_unit(d),
            "bar_ms": _opt_num(d.get("signal_bar_ms")),
        }
        # hl_status（执行器侧账实状态）：只在「有必要说」的时候进 payload ——
        # 空/open 意味着「一切正常」或「不是 HL 仓」，面板不必多一行字。
        hl_st = str(d.get("hl_status") or "")
        if hl_st and hl_st != "open":
            sig["hl_status"] = hl_st
            sig["hl_status_text"] = hl_status_text(hl_st)
            if hl_st == "closing":
                # 剩余宽限：留出「超时前不留空白」——读不到起点就按「刚进入」算满额。
                total = _grace_seconds_cfg(cfg)
                first = _grace_first_seen(deps.db, d.get("id"))
                left = total if first is None else max(0.0, total - max(0.0, now - first))
                sig["grace_left_s"] = round(left, 1)
        if pnl_from_fills:
            # 流水口径净额（PR-3）：**只在开关打开时才给出这些键** —— 记录表以
            # 「有没有 net_pnl 键」作为「要不要多显示一列净额」的依据，关掉时表格结构
            # 与老版本逐字相同（不新增空列，也不改 _signals_html 的默认形态）。
            # 值为 NULL = 没算过/查不到成交（不知道），不是 0。
            sig["net_pnl"] = _opt_num(d.get("net_pnl"))
            sig["pnl_source"] = d.get("pnl_source")
        # 单粒度资金费（feat/funds-ledger）：**只挂 HL 仓行**（非 HL 忽略），且该仓
        # 在 hl_funding 里有归集行；没有资金费记录的仓不带键，渲染层不加小字。
        # Kraken 期货 `user_funding_history` 恒空 → 库里可能残留迁移前的 HL 旧行，
        # 按 venue 能力直接不产这个键（否则会拿旧数据冒充当前资金费）。
        if flows_supported and posfields.is_hl(d) and d.get("id") is not None:
            fs = fund_by_pos.get(int(d["id"]))
            if fs is not None:
                sig["funding_sum"] = round(fs, 6)
        signals.append(sig)

    # ---------------- 策略绩效（上线以来，只读聚合，零行情调用）----------------
    # 纯读库（positions closed 行）；口径与过滤见 _perf_stats。放在 payload 里而非
    # 渲染层现查：/api/hl/status 的调用方（看守/巡检）也能拿到同一份数字。
    perf = {"n": 0, "long": 0, "short": 0, "wins": 0, "avg_r": None,
            "win_rate": None, "total_r": None}
    try:
        perf = _perf_stats(_db_call(deps.db, "closed_positions", 0.0) or [])
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] 策略绩效聚合失败（按空数据处理）：%s", e)

    # ---------------- 盈亏曲线（已剔除出入金）+ TWR 指数 ----------------
    # 旧版这里直接画 hl_snapshots.equity —— 出入金会把曲线顶出一个台阶（本账户
    # 2026-10-14 +100.79、10-21 +901.80 两笔转入），看着像暴赚。现在主曲线是
    # 「剔除出入金的累计盈亏」，另叠一条时间加权收益指数（TWR，起点 100）：
    # 前者回答「交易赚了多少 U」，后者回答「收益率多少」（入金的时点不影响它）。
    # 计算全在 app/hl/flows.py（纯本地，不打接口）；equity 仍用于熔断基线与顶部数字。
    flows = _db_call(deps.db, "hl_flows_since", 0.0) or []
    # 「流水可信吗」：有过一次**成功**同步（hl_flows_synced_at）或本地已有流水。
    # 两者都没有 = 从没拉成功过 → 不画曲线（不退回权益曲线：那正是要删掉的东西，
    # 而且会给出误导性走势），只显示一句提示。
    synced_at = _opt_num(_config_get(deps, "hl_flows_synced_at", "0")) or 0.0
    flows_ok = bool(flows) or synced_at > 0
    snaps = (_db_call(deps.db, "hl_snapshots", now - 30 * _DAY, 20000) or []) if include_curve_points else []
    curve = hl_flows.curve(
        snaps, flows, now=now, window_s=30 * _DAY, flows_ok=flows_ok,
        flows_note="" if flows_ok else "无出入金流水源（Kraken 期货无该端点），盈亏曲线不画")
    # 回撤熔断（DD Guard）字段并进 curve.meta：面板与 /api/hl/status 共用同一份数字
    # （§10.5 要求 curve meta 里出现 DD 字段）。**只读**执行器持久化的状态。
    if isinstance(curve.get("meta"), dict):
        curve["meta"].update(_dd_meta(deps, cfg, flows_ok))
        # 各时间窗的净入金（span 切换时摘要行要跟着窗口走，见 _render_body）。
        # 「全部」就是 net_deposit 本身，不重复存（同一事实只存一份）。
        try:
            dep_7 = dep_30 = 0.0
            for f in flows:
                d = _rowd(f)
                ts, amt = _num(d.get("ts")), _num(d.get("amount"))
                if ts >= now - 7 * _DAY:
                    dep_7 += amt
                if ts >= now - 30 * _DAY:
                    dep_30 += amt
            curve["meta"]["net_deposit_span"] = {
                "7": round(dep_7, 6), "30": round(dep_30, 6)}
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[hl-views] 分窗净入金计算失败（渲染层回退全期口径）：%s", e)
        # 省略点列（`curve=0`）时，由点列导出的量是**没算**而不是 0：一律 None + 显式标记。
        # 净入金（net_deposit / net_deposit_span）与 dd_* 仍是真的（各自独立于点列）。
        if not include_curve_points:
            for _k in ("pnl_total", "pnl_7d", "max_dd_pnl", "twr_total_pct",
                       "points", "equity_base"):
                curve["meta"][_k] = None
            curve["meta"]["curve_omitted"] = True

    # ---------------- 事件 / 系统 ----------------
    events: List[dict] = []
    for r in (_db_call(deps.db, "hl_recent_events", 30) or []):
        d = _rowd(r)
        events.append({
            "ts": _opt_num(d.get("created_at")),
            "kind": d.get("kind"),
            "symbol": d.get("symbol"),
            "message": d.get("message"),
        })

    feed: List[dict] = []
    for r in (_db_call(deps.db, "feed_status") or []):
        d = _rowd(r)
        # 故障判定与引擎心跳同一口径（engine.heartbeat_text）：「最近一次失败是否
        # 晚于最近一次成功」——只看 last_ok_at 的话，出过一次错的源永远显示正常
        #（mark_feed 失败路径不清 last_ok_at）。
        ok_at = _opt_num(d.get("last_ok_at")) or 0.0
        err_at = _opt_num(d.get("error_at")) or 0.0
        ok = ok_at > 0 and ok_at >= err_at
        feed.append({
            "symbol": d.get("symbol"),          # 内部键，保留
            "coin": _disp(d.get("symbol")),     # 展示用
            "source": d.get("source"),
            "last_ok_at": _opt_num(d.get("last_ok_at")),
            "last_error": d.get("last_error"),
            # 最后一次失败的时间（P2 合并后「三标的动态」的失败小字行要用它显示
            # 「最后错误 HH:MM」；老 payload 没带，读不到就如实显示 —）
            "error_at": _opt_num(d.get("error_at")),
            "ok": ok,
        })

    symbols: List[str] = []
    try:
        symbols = [str(s) for s in (list(getattr(cfg, "symbols", []) or []))]
    except Exception:                                            # noqa: BLE001
        symbols = []
    # 主策略标的的合约名（BTC/ETH/…）：面板对账判「交易所有仓、本地无记录 = 人工单」
    # 时只认这些标的 —— Kraken 账户是全账户，MR 腿（QQQ）也在里面而 positions 表
    # 本来就没有它的行（与执行器 reconcile 的 B4 同口径）。
    own_coins = {_coin_of(s) for s in symbols if _coin_of(s)} or None
    last_bar: Dict[str, Optional[int]] = {}
    for s in symbols:
        v = _db_call(deps.db, "get_last_bar", s)
        last_bar[s] = int(v) if v else None

    # ---------------- 三标的动态（纯读 symbol_state 缓存）----------------
    symbol_state_out = _symbol_state_rows(deps, db_rows, symbols)

    stuck = _db_call(deps.db, "hl_stuck_orders", 180.0) or []
    executor_last_event_at = _opt_num(_db_call(deps.db, "hl_last_event_at"))

    kill, kill_reason = _kill_state(deps)

    # API 有效期（P3 R3）：状态条徽章的数据源。只读 hl_agent_state DB 缓存
    #（执行器每个整点刷新），面板渲染零网络调用；缺失/损坏一律空 dict → 徽章显「—」。
    agent_out: dict = {}
    # 注入了统一生产者（main._agent_state）就用它：设置页卡片 / 状态条徽章 / JSON
    # 三处必须是同一份三态结论（missing / configured / verified）。
    _agent_fn = getattr(deps, "agent_state_fn", None)
    if _agent_fn is not None:
        try:
            _st = _agent_fn()
            if isinstance(_st, dict) and _st:
                agent_out = dict(_st)
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[hl-views] 读凭据三态失败（留空 → 徽章显 —）：%s", e)
    info_obj = getattr(deps, "info", None)
    if agent_out:
        pass
    elif info_obj is None or callable(getattr(info_obj, "price_tick", None)):
        # Kraken Derivatives API key 无到期概念，也没有代理钱包授权体系：
        # 不读 HL 遗留的 hl_agent_state（那里的到期日会让徽章显示「169 天」）。
        # info 为 None（只读客户端没装配成功）时同样走这条：那时候读 hl_agent_state
        # 只能读到 HL 时代的陈旧到期日，而那套体系在 Kraken 部署里根本不存在 ——
        # 显示「不限期」比显示一个假的到期日诚实（审核 C1）。
        agent_out = {"days_left": None, "level": "no_expiry", "cleanup": "none",
                     "source": "kraken-key-no-expiry"}
    else:
        try:
            from app.hl import agent as _agent_mod
            st = _agent_mod.read_state(deps.db) if deps.db is not None else {}
            st = st if isinstance(st, dict) else {}
            lvl = str(st.get("level") or "")
            if not lvl:
                # 老 state 没有 level 字段时的兜底（有 until 就按毫秒重算），口径同 admin_ui.render_agent_card
                lvl = (_agent_mod.level_ms(st.get("valid_until_ms"), st.get("checked_ms"))
                       if st.get("valid_until_ms") else "unknown")
            agent_out = {"days_left": _opt_int(st.get("days_left")), "level": lvl,
                         "cleanup": str(st.get("cleanup") or "none")}
        except Exception as e:                                        # noqa: BLE001
            logger.warning("[hl-views] 读 API 有效期状态失败（徽章显 —）：%s", e)

    # MR 腿状态块：JSON 与面板 HTML 共用 —— 已在「持仓」之前算过（分层去重要用），
    # 这里不再重复读取（读两次会让同一请求里的 enabled/pos 有机会来自两个版本）。

    account_out: Dict[str, Any] = {
        "address": address,
        "equity": _num(snap.get("equity_total", snap.get("equity"))),   # 总值（统一账户友好）
        "perp_equity": _num(snap.get("equity")),                        # 永续口径（原字段语义）
        "spot_usdc": _num(snap.get("spot_usdc")),
        "spot_hold": _num(snap.get("spot_hold")),
        "spot_ok": bool(snap.get("spot_ok", True)),
        "unified": bool(snap.get("unified", False)),
        "withdrawable": _num(snap.get("withdrawable")),
        "margin_used": _num(snap.get("margin_used")),
        "notional": _num(snap.get("notional")),
        "unrealized_pnl": unrealized_pnl,
        "today_realized": today_realized,
        "total_realized": total_realized,
        "fees_total": fees_total,
        "funding_total": funding_total,
    }
    # 读不到交易所（reachable=False）时金额一律 None —— 不是 0：渲染层 _fmt(None) 给
    # 「—」，而 0 会渲染成「账户权益 0.00 U」，那是把「没读到」说成「钱没了」。
    # 键集合不随可达性变化（消费方按键读，schema 稳定）。
    if not reachable:
        for _k in _ACCT_NUM_KEYS:
            account_out[_k] = None

    return {
        "schema_version": SCHEMA_VERSION,
        "ts": now,
        # 本次渲染的读数到底来自哪：调度器快照（带年龄）还是实时读取。
        # 徽章只认这个字段 —— `SnapshotInfo.age_s()` 报的是「库里最新一行多旧」，
        # 快照过期回落实时后它仍在涨，会让面板写「数据 16 分钟前」而实际是实时值
        # （第七轮 #6：年龄与来源必须同一件事）。
        "panel_snapshot_age_s": _opt_num(snap.get("panel_snapshot_age_s")),
        "data_source": ("snapshot" if snap.get("panel_snapshot_age_s") is not None else "live"),
        "mode": mode,
        "kill": bool(kill),
        "kill_reason": kill_reason,
        "mr": mr_out,
        "network": {
            "base_url": base_url,
            "testnet": "testnet" in base_url.lower(),
            "reachable": bool(reachable),
            "latency_ms": latency_ms,
            "latency_from_snapshot": latency_from_snapshot,
            "latency_age_s": latency_age_s,
            "error": net_err,
        },
        "account": account_out,
        "positions": positions_out,
        "orders_open": orders_open,
        "orders_recent": orders_recent,
        # 挂单列表可信度（监控契约 2026-11-09）：顶层布尔，与上面止损列/下面对账
        # 同一来源（快照 orders_trusted）；读不到快照时为 False。
        "orders_trusted": orders_trusted,
        "fills": fills_out[:100],   # JSON 也截断（看守/巡检只看头部；全量会让响应无限膨胀）
        "signals": signals,
        "symbol_state": symbol_state_out,
        "system": {
            "feed": feed,
            "last_bar": last_bar,
            "executor_last_event_at": executor_last_event_at,
            "stuck_orders": len(stuck),
            "mismatch": _reconcile(reachable, snap_positions, snap_orders,
                                   open_by_coin, stuck,
                                   orders_trusted=orders_trusted,
                                   own_coins=own_coins),
        },
        # 盈亏曲线（已剔除出入金）+ TWR 指数。旧键 equity_curve（原始权益曲线）已按
        # 用户要求**整个删掉**，不是开关 —— 全仓只有这一个图表源。
        # 该 venue 没有流水端点时（Kraken 期货）`curve` 仍是同一份结构（/api 消费方
        # 不变），只是面板渲染层整块不画 —— 判据是 `flows_supported`。
        "curve": curve,
        # venue 是否提供出入金流水端点（False = Kraken 期货；面板据此隐藏
        # 盈亏曲线 / 策略回撤 / 资金费 / 资金流水，见 _render_body）。
        "flows_supported": flows_supported,
        # venue 有没有「钱包地址」概念（True = Kraken 期货）。渲染层据此整行不画
        # 「钱包地址」—— 那里只有一个 main 填的占位常量，展示出来等于编了个地址。
        "venue_address_free": address_free,
        # 策略绩效（上线以来）：总览卡片数据源（只读聚合，见 _perf_stats）。
        "perf": perf,
        # API 有效期（P3 R3）：状态条徽章唯一常驻出处；/admin 的 get_payload 会用
        # deps.agent_state_fn 的同一份缓存覆盖（注入测试路径）。
        "agent": agent_out,
        "events": events,
    }


# ---------------------------------------------------------------- SVG 盈亏曲线
_CURVE_EMPTY = "暂无数据（等执行器落快照一次 hl_snapshots）"


def _curve_points(v: Any) -> List[List[float]]:
    """payload 的 [[ts, val], ...] → 干净的 float 点列（脏数据直接丢）。"""
    out: List[List[float]] = []
    for x in _as_list(v):
        if isinstance(x, (list, tuple)) and len(x) >= 2:
            t, y = _opt_num(x[0]), _opt_num(x[1])
            if t is not None and y is not None:
                out.append([t, y])
    return out


SPAN_CHOICES = (("7", "近 7 天"), ("30", "近 30 天"), ("all", "全部"))
# 盈亏曲线时间窗（P1）：GET span ∈ 7/30/all，缺参/非法值回落 7。「全部」受快照
# 滚动清理（hl_snapshots 约 30 天）限制，图例里必须写明，不能让人以为有历史全量。
_SNAPSHOT_RETENTION_NOTE = "快照保留约 30 天，更早数据已清理"


def normalize_span(v: Any) -> str:
    """span 参数白名单：只认 7/30/all，其余（含缺参）一律回落 7。"""
    s = str(v or "").strip().lower()
    return s if s in ("7", "30", "all") else "7"


def _curve_window(pnl: List[List[float]], twr: List[List[float]],
                  span: str = "7") -> tuple:
    """按所选时间窗切展示点列，返回 (pnl, twr, 标题, 实际天数)。

    - 标题按**实际首尾**换算（快照不足所选窗口时如实标注，不编「近 7 天」）；
      「全部」固定标「全部」。
    - 窗口内不足 2 个点画不成线 → 回落用实际可得的全序列（旧「近 7 天不足回 30 天」
      行为的推广：宁多画不空屏，但标题按真实跨度写）。
    """
    span = normalize_span(span)
    if not pnl:
        return [], [], "近 7 天", 0.0
    if span == "all":
        sel_p, sel_t = pnl, twr
    else:
        days = int(span)
        cut = pnl[-1][0] - days * _DAY
        sel_p = [p for p in pnl if p[0] >= cut]
        sel_t = [q for q in twr if q[0] >= cut]
        if len(sel_p) < 2:
            sel_p, sel_t = pnl, twr
    title = "全部" if span == "all" else f"近 {max(1, int((sel_p[-1][0] - sel_p[0][0]) / _DAY + 0.5))} 天"
    return sel_p, sel_t, title, (sel_p[-1][0] - sel_p[0][0]) / _DAY if len(sel_p) >= 2 else 0.0


def _downsample(n: int, cap: int) -> List[int]:
    """按像素列降采样（抽样下标，审计三轮 L）：30 天 5 分钟一张快照 ≈ 8600 点，全量内联
    polyline 会让页面体积和渲染开销线性膨胀。返回要保留的下标列表。"""
    if n <= cap:
        return list(range(n))
    step = n / float(cap)
    idx = [min(n - 1, int(i * step)) for i in range(cap)]
    if idx[-1] != n - 1:
        idx[-1] = n - 1
    return idx


def _pnl_svg(pnl: List[List[float]], twr: List[List[float]],
             width: int = 760, height: int = 160) -> str:
    """手写内联 SVG（零外部资源）：

    - 主曲线 = 累计盈亏（USDC，左轴）：0 基准线灰色虚线；正区间绿、负区间红
      —— 用**分段折线**实现（穿过 0 时插值出交点，两段各自着色），不是渐变、不靠 JS。
    - 叠加曲线 = 收益指数 TWR（独立缩放，右轴）：虚线；右侧标出该轴的 min/max。
    - 桌面端铺满（2026-11-07）：容器宽 > viewBox 宽（760）时，默认的等比缩放会在
      两侧留大片空白 —— 改 ``preserveAspectRatio="none"`` 拉伸铺满，折线全部加
      ``vector-effect="non-scaling-stroke"``（线宽不随拉伸变粗）；SVG 内的 <text>
      会被横向拉变形，所以 TWR min/max 刻度移成 HTML 覆盖层（.chartbox .axlbl），
      零 JS、两套主题都走现有变量。
    """
    if len(pnl) < 2:
        return (f"<svg class='chart' viewBox='0 0 {width} {height}' role='img' "
                f"aria-label='盈亏曲线'>"
                f"<text x='{width / 2}' y='{height / 2}' text-anchor='middle' "
                f"class='gtext' font-size='12'>{_CURVE_EMPTY}</text></svg>")

    pad_l, pad_r, pad_t, pad_b = 6, 46, 12, 16
    idx = _downsample(len(pnl), width * 2)
    pts_p = [pnl[i] for i in idx]
    # TWR 与 pnl 同一时间轴（同一份快照序列）。长度不一致时按时间对齐，不强凑。
    if len(twr) == len(pnl):
        pts_t = [twr[i] for i in idx]
    else:
        by_ts = {round(p[0], 3): p[1] for p in twr}
        pts_t = [[p[0], by_ts[round(p[0], 3)]] for p in pts_p if round(p[0], 3) in by_ts]

    xs = [p[0] for p in pts_p]
    ys = [p[1] for p in pts_p]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(min(ys), 0.0), max(max(ys), 0.0)      # 0 一定在可视范围内
    span_x = (x1 - x0) or 1.0
    span_y = (y1 - y0) or 1.0

    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def sx(x: float) -> float:
        return round(pad_l + (x - x0) / span_x * plot_w, 1)

    def sy(y: float) -> float:
        return round(height - pad_b - (y - y0) / span_y * plot_h, 1)

    # ---- 主曲线：按 0 轴切段（正绿负红），穿过 0 时插值出交点
    segs: List[tuple] = []
    cur_cls: Optional[str] = None
    cur: List[tuple] = []
    for i, (x, y) in enumerate(zip(xs, ys)):
        cls = "gpos" if y >= 0 else "gneg"
        px, py = sx(x), sy(y)
        if cur_cls is None:
            cur_cls, cur = cls, [(px, py)]
            continue
        if cls == cur_cls:
            cur.append((px, py))
            continue
        prev_x, prev_y = xs[i - 1], ys[i - 1]
        denom = (y - prev_y) or (1e-12 if y >= 0 else -1e-12)
        x_cross = prev_x + (-prev_y) / denom * (x - prev_x)
        cross = (sx(x_cross), sy(0.0))
        cur.append(cross)
        segs.append((cur_cls, cur))
        cur_cls, cur = cls, [cross, (px, py)]
    if cur:
        segs.append((cur_cls, cur))

    paths = []
    for cls, pts in segs:
        if len(pts) < 2:
            pts = [pts[0], (pts[0][0] + 0.1, pts[0][1])]   # 单点段也要可见
        coords = " ".join(f"{px},{py}" for px, py in pts)
        paths.append(f"<polyline points='{coords}' fill='none' class='{cls}' "
                     f"stroke-width='2' stroke-linejoin='round' stroke-linecap='round' "
                     f"vector-effect='non-scaling-stroke'/>")

    # ---- 叠加曲线：TWR 独立缩放（右轴）
    right = ""
    labels = ""
    if len(pts_t) >= 2:
        ts_ = [p[0] for p in pts_t]
        vs = [p[1] for p in pts_t]
        t0, t1 = min(vs), max(vs)
        pad = (t1 - t0) * 0.08 or max(abs(t1) * 0.01, 1.0)
        t0, t1 = t0 - pad, t1 + pad
        span_t = (t1 - t0) or 1.0

        def ty(v: float) -> float:
            return round(height - pad_b - (v - t0) / span_t * plot_h, 1)

        coords = " ".join(f"{sx(x)},{ty(v)}" for x, v in zip(ts_, vs))
        right = (f"<polyline points='{coords}' fill='none' class='gtwr' stroke-width='1.6' "
                 f"stroke-linejoin='round' stroke-linecap='round' "
                 f"vector-effect='non-scaling-stroke'/>")
        # 右轴 min/max 两个刻度：preserveAspectRatio=none 会把 SVG 内 <text> 横向拉变形，
        # 所以改成 HTML 覆盖层（.chartbox 相对定位 + axlbl 按 y 百分比定位，零 JS）。
        labels = (f"<span class='axlbl' style='top:{ty(max(vs)) / height * 100:.1f}%'>"
                  f"{max(vs):,.1f}</span>"
                  f"<span class='axlbl' style='top:{ty(min(vs)) / height * 100:.1f}%'>"
                  f"{min(vs):,.1f}</span>")

    grid = "".join(
        f"<line x1='{pad_l}' y1='{round(pad_t + f * plot_h, 1)}' "
        f"x2='{width - pad_r}' y2='{round(pad_t + f * plot_h, 1)}' "
        f"class='gline' stroke-width='1' vector-effect='non-scaling-stroke'/>"
        for f in (0.25, 0.5, 0.75))
    zero_y = sy(0.0)
    last_x, last_y = sx(xs[-1]), sy(ys[-1])

    return (
        # preserveAspectRatio=none：横向铺满容器（桌面端 1180 宽不再两侧留白）；
        # 线宽/圆点由 vector-effect 与 round-cap 短线兜住，不随拉伸变形。
        f"<div class='chartbox'>"
        f"<svg class='chart' viewBox='0 0 {width} {height}' preserveAspectRatio='none' "
        f"role='img' aria-label='盈亏曲线'>"
        + grid
        # 0 基准线：盈亏曲线读的是「赚还是亏」，这条线必须在
        + f"<line x1='{pad_l}' y1='{zero_y}' x2='{width - pad_r}' y2='{zero_y}' "
        f"class='gbase' stroke-width='1' stroke-dasharray='4 4' opacity='0.55' "
        f"vector-effect='non-scaling-stroke'/>"
        + right
        + "".join(paths)
        # 末点圆点：circle 会被非等比拉伸成椭圆 —— 改用极短 round-cap 线段画点，
        # 配合 non-scaling-stroke 在任何宽度下都是正圆。
        + f"<line x1='{last_x}' y1='{last_y}' x2='{round(last_x + 0.01, 3)}' y2='{last_y}' "
        f"class='gdot' stroke-width='6' stroke-linecap='round' "
        f"vector-effect='non-scaling-stroke'/>"
        + "</svg>"
        + labels
        + "</div>")
# ---------------------------------------------------------------- HTML 渲染
DASH_CSS = """<style>
*{box-sizing:border-box}
html,body{margin:0;padding:0;-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
     background:var(--bg);color:var(--fg);line-height:1.55;font-size:14px;padding:14px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:19px;margin:0 0 2px;font-weight:600}
/* 头部副题（P3 命名重设计）：h1 下一行 muted 小字，一句话说清标的/周期/交易所。 */
.h1sub{margin:0 0 10px;font-size:12px;color:var(--muted)}
h2{font-size:12px;margin:20px 0 8px;color:var(--h2);letter-spacing:1.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.badge{display:inline-flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--line);
       border-radius:999px;padding:6px 12px;font-size:12px;white-space:nowrap}
.badge b{font-size:13px;font-weight:600}
/* 状态条分层（P0-2）：第一层 = 模式 / 系统健康 / 持仓 三个核心结论（.badge-status，
   字号与常规徽章同、描边加重）；第二层 = 延迟 / API / 更新时间（.badge-small，更小更淡）。
   两层都是 width:100% 的 flex 容器，父 .bar 的 flex-wrap 让它们各占一行 —— 不重定义
   .bar 本身（/hl 与 /admin 共用），也不引入任何 JS。 */
.bar-primary,.bar-secondary{display:flex;flex-wrap:wrap;gap:8px;align-items:center;width:100%}
.bar-secondary{gap:6px 8px;padding-top:2px}
.badge-status{border-color:var(--line)}
.badge-small{padding:2px 9px;font-size:11px;gap:5px;color:var(--muted)}
.badge-small b{font-size:11.5px;color:var(--fg)}
.badge-small .muted{font-size:11px}
.sec{font-size:11px;color:var(--muted);letter-spacing:1.5px;margin-bottom:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:2px 20px}
/* 三标的横向对比（P1-1）：三张 .sym 卡在桌面端一行 3 列等宽。
   为什么不直接改 .grid：执行器卡等其它块也用 .grid（auto-fit minmax(220px)），
   在 1180 宽下会开 4 列 —— 3 张标的卡只填满 3 列、右侧留一个空槽，看起来就是
   「竖排/muddled」。这里给三标的容器另挂 .sym-grid，只覆盖列数，display/gap 仍
   沿用 .grid；minmax(0,1fr) 让长数字/长标的名可收缩，不在 390px 上撑出横向滚动。
   .mrline（超跌腿 QQQ）的 grid-column:1/-1 因此横跨三列，语义不变。 */
.sym-grid{grid-template-columns:repeat(3,minmax(0,1fr))}
.mrline{grid-column:1/-1;margin-top:8px;padding-top:8px;border-top:1px dashed var(--line2);
        font-size:12px}
.kv{display:flex;justify-content:space-between;gap:12px;font-size:13px;padding:4px 0;
    border-bottom:1px solid var(--line2)}
.kv:last-child{border-bottom:0}
.kv .k{color:var(--muted);white-space:nowrap}
.kv .v{font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
/* 卡内的句子行（原因 / 闸门异常 / 待确认提示）：与 .kv 的「键→数字」两列区分开，
   整句折行、不抢现价的视觉重量。 */
.whyline{margin-top:4px;font-size:12px;line-height:1.5;overflow-wrap:anywhere}
.pos{color:var(--pos)}.neg{color:var(--neg)}.warn{color:var(--warn)}.muted{color:var(--muted);font-size:12px}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch;
            border:1px solid var(--line);border-radius:10px;background:var(--card)}
/* 右缘渐隐提示可横滚（2026-11-07）：渐隐层挂在包一层 .tscroll 的 ::after 上
   （不能挂在 .tablewrap 自己 —— 滚动容器内的绝对定位子元素会随内容滚走）。
   渐变到 var(--card) 色，亮暗主题各自融入卡底；无溢出的表格只有最右 16px
   一层很淡的渐变，右对齐数字基本不受影响。 */
.tscroll{position:relative}
.tscroll::after{content:'';position:absolute;top:1px;bottom:1px;right:1px;width:16px;
                border-radius:0 9px 9px 0;pointer-events:none;
                background:linear-gradient(to left,var(--card) 3px,rgba(0,0,0,0))}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border-bottom:1px solid var(--line);padding:6px 9px;text-align:left;white-space:nowrap}
thead th{color:var(--muted);font-weight:500;font-size:11.5px;background:var(--card2)}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.banner{border:1px solid var(--banner-line);background:var(--banner-bg);color:var(--banner-fg);border-radius:10px;
        padding:10px 14px;margin-bottom:12px;font-size:13px}
.banner .muted{color:var(--banner-muted)}
.chart-wrap{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px 6px}
.chart-head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;
            font-size:12px;color:var(--muted);margin-bottom:2px}
.chart-head b{color:var(--fg)}
/* 图表的定位容器：SVG preserveAspectRatio=none 铺满后，右轴刻度以 HTML
   覆盖层（.axlbl）按 y 百分比叠在图上 —— 相对定位锚点在这里。 */
.chartbox{position:relative}
.chartbox .axlbl{position:absolute;right:4px;transform:translateY(-50%);font-size:10px;
                 line-height:1;color:var(--muted);font-variant-numeric:tabular-nums;
                 pointer-events:none}
svg.chart{display:block;width:100%;height:160px}
svg.chart .gtext{fill:var(--muted)}
svg.chart .gline{stroke:var(--line)}
svg.chart .gbase{stroke:var(--muted)}
svg.chart .garea{fill:var(--link)}
svg.chart .gline2{stroke:var(--link)}
svg.chart .gpos{stroke:var(--pos)}
svg.chart .gneg{stroke:var(--neg)}
svg.chart .gtwr{stroke:var(--link);stroke-dasharray:5 4;opacity:.9}
/* 末点圆点已改成 round-cap 短线段（抗非等比拉伸），所以走 stroke 不走 fill。 */
svg.chart .gdot{stroke:var(--link);fill:none}
.btn{display:inline-block;background:var(--btn);color:#fff;border-radius:6px;padding:5px 11px;
     font-size:12px;text-decoration:none;white-space:nowrap}
.btn.gray{background:var(--btn-gray)}
.foot{margin:18px 0 6px;color:var(--muted);font-size:12px;display:flex;flex-wrap:wrap;
      gap:8px;align-items:center}
.foot .spacer{flex:1}
a{color:var(--link)}
/* 账户卡（P2 分层）：顶层 3 关键数大字一行；次级项 muted 小字 flex 换行排开
   （不再逐行键值，同一卡内少一半行高）。 */
.acct-top{display:flex;flex-wrap:wrap;gap:6px 24px;padding:2px 0 8px;
          border-bottom:1px solid var(--line2)}
.acct-top b{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums}
.acct-top .muted{font-size:11px}
.acct-sub{display:flex;flex-wrap:wrap;gap:2px 16px;padding:8px 0 2px;
          font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.acct-sub b{font-weight:600;font-variant-numeric:tabular-nums}
/* 账户卡次级折叠（P3 R4）与 API 有效期折叠（P3 R3）：原生 <details>，零 JS。
   summary 摸起来要像可点的：指针光标 + muted 小字。 */
details.acct-details>summary,details.card>summary{cursor:pointer;color:var(--muted);
   font-size:12.5px;letter-spacing:1px;padding:2px 0}
details.card>summary{font-size:12px;color:var(--h2)}
/* 账户横幅（P1-2）：账户卡从「仓位之后」提到状态条之后 —— 首屏先给钱、再看监控。
   外层 **专属 class .acct-banner**（不复用 .grid / .sym-grid，那些是标的与执行器
   区块的容器，改它们会连带另一块布局）；内层仍是 .acct-top（P2 的关键数 flex 行）。
   这里只加位置/尺寸上的横幅外观：关键数一行铺开、窄屏自动换行，零 JS，
   颜色全部取主题变量（亮/暗主题由 THEME_CSS 给值，本文件不写死色值）。 */
.acct-banner{padding:10px 14px 6px}
.acct-banner>.sec{margin-bottom:2px}
.acct-banner .acct-top{gap:2px 30px;align-items:baseline;padding:0 0 6px}
.acct-banner .acct-top b{font-size:20px}
.acct-banner details.acct-details>summary{padding:4px 0 2px}
/* 横幅窄屏（≤640px）：单独成块，不与下面那块公共窄屏规则混在一起 ——
   横幅的尺寸只属于横幅，改横幅时不必动别的区块的 media query。
   不加任何 nowrap / 固定宽度，靠 .acct-top 的 flex-wrap 自然折行，390px 不横向溢出。 */
@media (max-width:640px){
    .acct-banner{padding:9px 11px 5px}
    .acct-banner .acct-top{gap:0 18px}
    .acct-banner .acct-top b{font-size:17px}}
/* 仓位表窄屏适配（P2）：小字行 tr.subm 默认隐藏（宽屏 13 列齐全）；≤640px 时
   隐藏 .hidem 列、显示小字行 —— 显隐只靠 CSS media query，零 JS。 */
tr.subm{display:none}
tr.subm td{white-space:normal;font-size:11px;line-height:1.4;color:var(--muted);
            overflow-wrap:anywhere}
/* 空仓引导卡（P0-3）：空仓时替代表格 —— 说清「在等什么、离触发线还差多远」。
   有仓时整块不渲染，所以这些规则不会影响仓位表。 */
.card-empty-state{text-align:center;padding:24px}
.card-empty-state h3{font-size:15px;margin:0;font-weight:600}
.empty-icon{font-size:48px;margin-bottom:12px;line-height:1}
.empty-desc{font-size:13px;color:var(--muted);margin:12px 0}
.empty-desc p{margin:2px 0}
.empty-desc b{color:var(--fg)}
.empty-triggers{display:flex;gap:12px;justify-content:center;margin-top:16px;flex-wrap:wrap}
.trigger-item{display:flex;flex-direction:column;gap:2px;padding:8px 12px;
              background:var(--card2);border-radius:6px;min-width:100px}
.trigger-item .coin{font-size:13px;font-weight:600}
.trigger-label{font-size:11px;color:var(--muted)}
.trigger-val{font-size:16px;font-weight:600;margin:4px 0;font-variant-numeric:tabular-nums}
/* .trigger-price（引导卡内的「现价 XX」小字）随 N3 一起删除：现价统一由三标的
   大字卡展示，同屏不再出现第二个现价。 */
@media (max-width:640px){body{padding:9px;font-size:13px}h1{font-size:17px}
    .badge{padding:5px 10px;font-size:11px}.grid{grid-template-columns:1fr}
    /* 三标的：窄屏降为 1 列（垂直堆叠），与卡片自身的窄屏字级规则配合。 */
    .sym-grid{grid-template-columns:1fr}
    .bar-primary,.bar-secondary{gap:6px}
    .badge-small{padding:2px 8px}
    .hidem{display:none}tr.subm{display:table-row}
    .card-empty-state{padding:18px 12px}.empty-icon{font-size:40px}
    .empty-triggers{gap:8px}
    .trigger-item{flex:1 1 96px;min-width:88px;padding:8px 10px}
    .trigger-val{font-size:15px}}
""" + SYMBOL_CSS + "</style>"


# ---------------------------------------------------------------- 三标的动态区块
# 位置：总览页最顶部（tabs 之后第一个区块、在「账户」卡片之前）。每标的一张卡，自上而下：
#   名称 · 状态词 · 现价 → 距离行 → 原因行 → 闸门异常行 → （区块底部一句可见脚注）
#
# 为什么把「为什么没开仓」并进来：两块回答的是同一个问题（现在能不能动）。分开渲染时同一屏
# 会出现两个现价（缓存 vs 现取，数字还不一样）、两套状态词（「均线未密集」/「均线不密集」）、
# 两处密集根数；而闸门正常时整列都是同一条「放行」，纯噪音。合并后**现价只有一个来源、
# 状态词只有一套、闸门只在异常时出现**，同时保留两块各自不可替代的信息：距离行的线价
# （可操作）与原因行的六步拦截数字（带带宽/门槛）。
#
# 数据：why 行（app.why.build_rows，现算）为主源；why 缺失/报错时退回 symbol_state
# （引擎 tick 落的缓存）并标注读数时间。面板依旧不重算任何判定，只挑字段渲染。
_SQUEEZE_CAP = 6          # 密集根数展示封顶：满 6 根即已满足入场前提
_NEAR_TITLE = "接近触发（按收盘判定）"
# 图例行（区块底部，手机看不见 title 悬停，口径必须落在可见文字上）：
_EXIT_TITLE = {1: "按 4h 收盘判定，非盘中触发：收盘跌破该线即自动平仓",
               -1: "按 4h 收盘判定，非盘中触发：收盘站上该线即自动平仓"}
_LONG_TITLE = "4h 收盘突破该线即入场（需均线密集满 6 根）"
_SHORT_TITLE = "收盘跌破该线且在 MA200 下方才开空"


def _need_pct(kind: str, price: Any, line: Any) -> Optional[float]:
    """到达触发线所需变动幅度（%，自带方向符号）。四方向：

    exit_short  持仓空：需要**涨**到 SMA20（收盘站上才自动平仓）→ (线 − 现价)/现价
    exit_long   持仓多：需要**跌**到 SMA20（收盘跌破）        → (现价 − 线)/现价
    break_up    无仓上破开多：需要涨到 6均线上轨               → (线 − 现价)/现价
    break_down  无仓下破开空：需要跌到 6均线下轨               → (现价 − 线)/现价

    现价/触发线缺失或非法（<=0）→ None（该子行整体不渲染，fail-soft）。
    """
    px, v = _opt_num(price), _opt_num(line)
    if px is None or px <= 0 or v is None:
        return None
    if kind in ("exit_short", "break_up"):
        return (v - px) / px * 100.0
    if kind in ("exit_long", "break_down"):
        return (px - v) / px * 100.0
    return None


def _sym_state_word(side: Any, dense: Any) -> tuple:
    """状态词 (文本, 颜色 class, 悬停 title)。

    - 有 status='open' 仓位 →「持仓中（多/空）」muted（持仓标的 walk 不评估入场，不谈密集）
    - 无仓：密集根数 NULL/0 →「均线未密集」muted；1~5 →「密集酝酿 n/6」hot；
      ≥6 → 封顶显示「密集酝酿 6/6」hot，title 里给真实根数
    """
    sd = _opt_int(side)
    if sd in (1, -1):
        return f"持仓中（{'多' if sd == 1 else '空'}）", "muted", ""
    n = _opt_int(dense) or 0
    if n <= 0:
        return "均线未密集", "muted", ""
    return (f"密集酝酿 {min(n, _SQUEEZE_CAP)}/{_SQUEEZE_CAP}", "hot",
            f"6 均线已连续 {n} 根密集，满 {_SQUEEZE_CAP} 根后收盘突破即入场")


def _sym_row(label: str, mark: str, title: str, need: Optional[float], line: Any,
             allow_warn: bool = True) -> str:
    """距离行：`还差 +X.X%`（0<need<1% 且 allow_warn 时标 warn + 悬停「接近触发」）/
    `待收盘判定`，百分比之后跟触发线价格（价格格式复用面板 _fmt 口径）。

    allow_warn 的语义 =「距离近 = 今晚可能动作」是否成立：
      ① 离场行（持仓）只看 4h 收盘破线，距离近确实意味着可能动作 → 恒 True；
      ② 上破开多行只需密集满 6 根（做多无 MA200 过滤），由调用方传
        `dense_cnt >= _SQUEEZE_CAP`（缺 dense_cnt → False）；
      ③ 下破开空行是三重的：密集满 6 根 **且** sma200 非 None **且** 现价 < sma200；
        任一不成立（含 sma200 缺失 = 无法核实过滤条件）都不亮琥珀 → fail-closed。
    """
    line_txt = f" <span class=\"muted\">线 {_fmt(line)}</span>"
    if need is None or need <= 0:
        val = f"<span class=\"muted\">待收盘判定</span>{line_txt}"
    else:
        cls = (f" class=\"warn\" title=\"{_NEAR_TITLE}\"" if allow_warn and need < 1.0
               else "")
        val = f"还差 <b{cls}>{need:+.1f}%</b>{line_txt}"
    return (f"<div class=\"kv\"><span class=\"k\">{label} "
            f"<span class=\"fn\" title=\"{_esc(title)}\">{mark}</span></span>"
            f"<span class=\"v\">{val}</span></div>")


def _pos_of(r: Dict[str, Any]) -> tuple:
    """卡片用的仓位口径：(status, side)。

    两个来源不同：why 行给 why.position（status='open'/'pending' 才有意义）；只有
    symbol_state 的降级路径没有 status，position_side 非空即代表「有 open 仓」
    （_symbol_state_rows 只把 status='open' 的仓写成 position_side）。
    返回 ("", None) = 无仓 → 走 ②③ 入场距离行。
    """
    pos = _as_dict(r.get("position"))
    st = str(pos.get("status") or "")
    if st in ("open", "pending"):
        return st, (_opt_int(pos.get("side")) or 1)
    sd = _opt_int(r.get("position_side"))
    if sd in (1, -1):
        return "open", sd
    return "", None


def _sym_state(r: Dict[str, Any]) -> tuple:
    """状态词（唯一一套，按优先级，命中即止）：(文本, 颜色 class, 悬停 title)。

    1. 行情读取失败 / 没有现价 → neg（数字都没有，谈密集/信号都是空话）
    2. 有 open 仓 → muted（walk 不评估入场，密集根数对它没意义）
    3. 有 pending 仓 → warn（占着槽位）
    4. why.fired → pos（信号成立，等执行）
    5~6. 密集酝酿 n/6（hot，封顶 6）→ 均线未密集（muted）

    旧代码在两块里各写一套（「均线未密集」vs「均线不密集」），统一取前者。
    """
    px = _opt_num(r.get("price"))
    if r.get("error") or px is None or px <= 0:
        return "行情读取失败", "neg", ""
    status, side = _pos_of(r)
    if status == "open":
        return f"持仓中（{'多' if side == 1 else '空'}）", "muted", ""
    if status == "pending":
        return "待确认（占槽）", "warn", "同一标的至多 1 仓，确认或忽略后才会评估新信号"
    why = _as_dict(r.get("why"))
    if why.get("fired"):
        return (f"信号成立（{'多' if int(_num(why.get('side'))) > 0 else '空'}）", "pos",
                str(why.get("reason") or ""))
    return _sym_state_word(None, r.get("dense_cnt"))


def _whyline(text: str, cls: str = "muted") -> str:
    """卡内的一句人话（原因行 / 闸门异常行 / 待确认提示）。

    与距离行（.kv）分开的类名：.kv 是「键 → 数字」两列，这里是一整句，样式与语义都不同。
    """
    return f"<div class=\"whyline {cls}\">{_esc(text)}</div>" if cls else \
        f"<div class=\"whyline\">{_esc(text)}</div>"


def _reason_line(r: Dict[str, Any]) -> str:
    """原因行：吸收原「信号态 / 拦截原因」单元格，一句人话、带数字。

    顺序刻意与 _sym_state 一致（仓位在行情之上）：有仓时真正该做的是处理这一仓，
    而不是去读「假如无仓」的行情读数。降级路径必须把**读数时间**写出来，
    否则用户会把缓存里的旧价当成现价（这正是合并前两个现价打架的根源）。
    """
    status, _ = _pos_of(r)
    if status == "open":
        return _whyline("持仓中，本轮不评估新入场", "muted")
    if status == "pending":
        return _whyline("待确认占用仓位槽，确认或忽略后才会评估新信号", "warn")
    err = str(r.get("error") or "")
    when = _fmt_dt(r.get("updated_at"), "%H:%M")
    if err:
        tail = f"（显示 {when} 的读数）" if _opt_num(r.get("price")) is not None else ""
        return _whyline(f"行情读取失败：{err}{tail}", "neg")
    why = _as_dict(r.get("why"))
    if not why:
        return _whyline(f"诊断数据暂不可用（显示 {when} 的读数）", "muted")
    if why.get("fired"):
        sig = _as_dict(why.get("signal"))
        ep, sp = _opt_num(sig.get("entry_price")), _opt_num(sig.get("stop_price"))
        sd = _opt_num(sig.get("stop_dist"))
        if ep is not None and sp is not None:
            pct = f"（{sd * 100:.2f}%）" if sd is not None else ""
            return _whyline(f"信号成立：入场 {_fmt(ep)}、止损 {_fmt(sp)}{pct}", "pos")
        return _whyline(str(why.get("reason") or "信号成立"), "pos")
    # reason 由 strategy.explain_entry 给（带带宽/门槛数字的人话），面板只做三件展示层的事：
    # ① 词面统一：strategy 的文案是「均线不密集」，而本卡状态词与图例统一用「均线未密集」——
    #    同一张卡上下两种说法等于又回到合并前「两套状态词」的老毛病。只替换展示文本，
    #    不动 strategy 的文案与判定（信号口径禁止改），/api/hl/why 仍返回 strategy 原文。
    # ② 密集根数口径统一：strategy 报的是「本轮判定所用那根」（squeeze[i-1]）的连续根数，
    #    而状态词与距离行取的是最后一根已收盘 bar（ind[i]）——两者**恒差一根**，同一张卡
    #    上下两个数打架（用户实测：状态词 2/6、原因行「现 1 根」）。同样只改展示文本，
    #    把「（现 N 根）」换成卡片自己的 dense_cnt；/api/hl/why 仍是 strategy 原文。
    # ③ 按真拦截点组句：strategy 的密集拦截模板把「带宽没压进」与「根数不够」用「或」
    #    并列、从不区分——已压进门槛时「未压到门槛以内」半句是假话（2026-10-21 用户实测：
    #    带宽 1.62% < 门槛 1.73% 却显示「未压到」）。装配层（app/why.py）给了**当下 bar**
    #    的带宽/门槛（band_cur/thr_cur），拦截点是密集时按真话组句；字段缺失（旧缓存/
    #    降级路径）回落 strategy 原文 + ①②的替换。
    if str(why.get("blocked_by") or "") == "squeeze":
        # 精简（2026-10-29 用户要求）：只报当下的带宽与门槛两个数，密集根数等细节
        # 不再重复（状态词里已有 N/6）。字段缺失（旧缓存/降级路径）回落 strategy 原文。
        band_cur, thr_cur = _opt_num(r.get("band_cur")), _opt_num(r.get("thr_cur"))
        if band_cur is not None and thr_cur is not None:
            txt = f"带宽 {band_cur * 100:.2f}% · 门槛 {thr_cur * 100:.2f}%"
            if why.get("drift"):
                txt += " ⚠️口径漂移"
            return _whyline(txt, "muted")
    txt = str(why.get("reason") or "—").replace("均线不密集", "均线未密集")
    dense = _opt_int(r.get("dense_cnt"))
    if dense is not None:
        txt = re.sub(r"（现 \d+ 根）", f"（现 {dense} 根）", txt)
    if why.get("drift"):
        txt += " ⚠️口径漂移"
    return _whyline(txt, "muted")


def _gate_lines(r: Dict[str, Any]) -> List[str]:
    """闸门异常行：**只在不正常时产生 DOM**（正常放行是噪音，一个字都不渲染）。

    关注侧沿用现有语义：持仓方向 > 信号方向 > 双侧都给（why.gate_side 已算好）。
    churn_limit<=0 = 熔断被人为关闭，此时「正在静置」只报静置 —— 报 churn 次数没有意义
    （永远不会熔断），只会让人以为它随时会停手。
    """
    gates = _as_dict(r.get("gates"))
    gate = _as_dict(r.get("gate"))
    items: List[tuple] = []
    if gate:
        items.append(gate)
    else:
        for key in ("long", "short"):
            g = _as_dict(gates.get(key))
            if g:
                items.append(g)
    out: List[str] = []
    for g in items:
        verdict = str(g.get("verdict") or "ok")
        if verdict == "ok":
            continue
        if verdict == "settle":
            out.append(_whyline(f"⚠️ 静置剩余 {int(_num(g.get('settle_remaining_s')))}s", "warn"))
        elif verdict == "churn":
            limit = int(_num(g.get("churn_limit")))
            if limit <= 0:
                continue
            out.append(_whyline(f"⚠️ churn {int(_num(g.get('churn_count')))}/{limit}"
                                f"（10 分钟内 {limit} 次撤挂即熔断）", "warn"))
        else:
            # 未知 verdict（执行器改了枚举而面板没跟上）：如实显示，别假装没事
            out.append(_whyline(f"⚠️ 闸门状态未知：{verdict}", "warn"))
    return out


def _merge_one(w: Dict[str, Any], st: Dict[str, Any]) -> Dict[str, Any]:
    """why 行（主源）+ symbol_state 行（降级源）→ 一张卡的数据。纯挑字段，不算判定。

    索引口径统一取「最后一根已收盘 bar」(ind[i])：sma20 / ②③ 的线价 / 密集根数 / SMA200
    全部同索引 —— 因为下一次入场判定比的就是这一根。why 行里的 ma_max_prev / ma_min_prev /
    values.dense_cnt 是 ind[i-1]（**上一根**，本轮判定用的那根），拿它算「到线距离」会让
    线价与距离差一根 bar、和 ① 的 SMA20 对不上；所以只在 symbol_state 缺失时兜底。
    """
    why = _as_dict(w.get("why"))
    vals = _as_dict(why.get("values"))
    err = str(w.get("error") or "")
    px_w = _opt_num(w.get("price"))
    # 降级判定：why 行缺失 / 取行情报错 / why 没有现价 —— 三者都只能用缓存读数
    degraded = bool(not w or err or px_w is None)
    if degraded:
        price, sma20 = _opt_num(st.get("price")), _opt_num(st.get("sma20"))
        long_trig, short_trig = _opt_num(st.get("long_trigger")), _opt_num(st.get("short_trigger"))
        dense, ma200 = _opt_int(st.get("dense_cnt")), _opt_num(st.get("sma200"))
    else:
        price = px_w
        sma20 = _opt_num(w.get("sma20"))
        if sma20 is None:
            sma20 = _opt_num(st.get("sma20"))
        long_trig = _opt_num(st.get("long_trigger"))
        if long_trig is None:
            long_trig = _opt_num(vals.get("ma_max_prev"))
        short_trig = _opt_num(st.get("short_trigger"))
        if short_trig is None:
            short_trig = _opt_num(vals.get("ma_min_prev"))
        dense = _opt_int(st.get("dense_cnt"))
        if dense is None:
            dense = _opt_int(vals.get("dense_cnt"))
        ma200 = _opt_num(st.get("sma200"))
        if ma200 is None and int(_num(vals.get("regime_ma"))) == 200:
            # ③ 琥珀要「现价 < MA200」；regime_val 只在看空方向时算过，且窗口可配
            ma200 = _opt_num(vals.get("regime_val"))
    sym = str(w.get("symbol") or st.get("symbol") or "")
    # 24h 涨跌幅（P0-1）：why 行（build_rows 现算）为主源；老快照/降级路径没有就取
    # symbol_state（目前不带此字段 → None，卡片不渲染胶囊，绝不编数字）。
    chg_24h = _opt_num(w.get("chg_24h"))
    if chg_24h is None:
        chg_24h = _opt_num(st.get("chg_24h"))
    return {
        "symbol": sym,
        "coin": str(w.get("coin") or st.get("coin") or "") or _disp(sym),
        "price": price, "sma20": sma20, "sma200": ma200,
        "chg_24h": chg_24h,
        "long_trigger": long_trig, "short_trigger": short_trig,
        "dense_cnt": dense,
        "band_cur": _opt_num(w.get("band_cur")),
        "thr_cur": _opt_num(w.get("thr_cur")),
        "min_squeeze_bars": _opt_int(w.get("min_squeeze_bars")),
        "position": _as_dict(w.get("position")),
        "position_side": st.get("position_side"),
        "why": why, "error": err or None,
        "gates": _as_dict(w.get("gates")), "gate": _as_dict(w.get("gate")),
        "gate_side": _opt_int(w.get("gate_side")) or 0,
        "updated_at": _opt_num(st.get("updated_at")),
    }


def _merged_cards(p: dict) -> List[Dict[str, Any]]:
    """payload → 卡片数据（why 顺序为主，why 缺的标的按 symbol_state 顺序补在后面）。

    两个方向都要兜：why 少一个标的不能让这张卡消失（fail-soft，只是没有原因行）；
    symbol_state 整块缺失时 why 照样能画（价格/线价取 why 行）。
    """
    why_rows = [_as_dict(x) for x in _as_list(p.get("why"))]
    st_rows: List[Dict[str, Any]] = []
    st_by_sym: Dict[str, Dict[str, Any]] = {}
    for x in _as_list(p.get("symbol_state")):
        d = _as_dict(x)
        sym = str(d.get("symbol") or "")
        if sym:
            st_by_sym[sym] = d
            st_rows.append(d)
    cards: List[Dict[str, Any]] = []
    seen = set()
    for w in why_rows:
        sym = str(w.get("symbol") or "")
        cards.append(_merge_one(w, st_by_sym.get(sym) or {}))
        seen.add(sym)
    for d in st_rows:
        sym = str(d.get("symbol") or "")
        if sym in seen:
            continue
        seen.add(sym)
        cards.append(_merge_one({}, d))
    # 既没 symbol 也没 coin 的脏行无法定位标的（也没有可展示的名字）→ 直接略去
    return [c for c in cards if c.get("symbol") or c.get("coin")]


def _sym_card(r: Dict[str, Any], feed_map: Dict[str, Dict[str, Any]] = None,
              last_bar: Dict[str, Any] = None) -> str:
    """一个标的的卡片：现价 + 状态词 + 距离行（持仓只给离场线；无仓给两条入场线）
    + 原因行 + 闸门异常行 + 数据源/已处理到小字行。现价全页只在这里出现一次
    （两块合并的核心约束）。
    """
    price = _opt_num(r.get("price"))
    status, side = _pos_of(r)
    st_txt, st_cls, st_title = _sym_state(r)
    title_attr = f" title=\"{_esc(st_title)}\"" if st_title else ""
    name = _esc(r.get("coin") or _disp(r.get("symbol")) or "—")
    # 24h 涨跌幅（P0-1）：有值才渲染胶囊 —— 数据缺失时宁可少一块，也不写「—%」
    # 让人以为「涨跌幅是 0」。颜色只借语义变量：正 pos / 负 neg / 平 muted。
    chg = _opt_num(r.get("chg_24h"))
    if chg is None:
        chg_html = ""
    else:
        ccls = "pos" if chg > 0 else ("neg" if chg < 0 else "muted")
        chg_html = (f'<span class="chg {ccls}" title="近 24 小时涨跌幅'
                    f'（4h 收盘口径：现价 vs 24h 前那根收盘）">{chg:+.1f}%</span>')
    # 现价与标的名**同一行**（flex，现价 margin-left:auto 贴右）：24px 大字是全卡
    # 第一眼锚点（P0-1），状态词/涨跌幅留在左侧与名字同基线。
    o = [(f'<div class="sym"><div class="sym-h">'
          f'<span class="nm">{name}</span>'
          f'<span class="st {st_cls}"{title_attr}>{st_txt}</span>'
          f'{chg_html}'
          f'<span class="px-large">{_fmt(price)}</span></div>')]
    if price is not None and price > 0:
        if status == "pending":
            # pending 占着仓位槽：确认前根本不会评估新入场，距离行是假信息
            o.append(_whyline("确认前不评估新入场", "muted"))
        elif status == "open":
            need = _need_pct("exit_short" if side < 0 else "exit_long", price, r.get("sma20"))
            if need is not None:
                o.append(_sym_row("距自动离场线", "①", _EXIT_TITLE[-1 if side < 0 else 1],
                                  need, r.get("sma20")))
        else:
            # 入场行（②③）的琥珀门槛：
            #   ② 上破开多：密集满 6 根（做多无 MA200 过滤）。
            #   ③ 下破开空：密集满 6 根 且 现价 < sma200（严格小于，与
            #      strategy.evaluate_entry 做空过滤同向）；sma200 缺失 = 无法核实
            #      过滤条件 → fail-closed 不亮琥珀。只有三重都成立，「距离近」才代表
            #      「今晚可能开空」。
            dense_ok = (_opt_int(r.get("dense_cnt")) or 0) >= _SQUEEZE_CAP
            ma200 = _opt_num(r.get("sma200"))
            short_ok = dense_ok and ma200 is not None and price < ma200
            need = _need_pct("break_up", price, r.get("long_trigger"))
            if need is not None:
                o.append(_sym_row("上破开多", "②", _LONG_TITLE, need, r.get("long_trigger"),
                                  allow_warn=dense_ok))
            need = _need_pct("break_down", price, r.get("short_trigger"))
            if need is not None:
                o.append(_sym_row("下破开空", "③", _SHORT_TITLE, need, r.get("short_trigger"),
                                  allow_warn=short_ok))
    o.append(_reason_line(r))
    o.extend(_gate_lines(r))
    # 数据源 / 已处理到小字行（P2 合并）：放在该标的块末尾、原因/闸门行之后。
    o.append(_feed_line(r, feed_map or {}, last_bar or {}))
    o.append("</div>")
    return "".join(o)


def _feed_line(r: Dict[str, Any], feed_map: Dict[str, Dict[str, Any]],
               last_bar: Dict[str, Any]) -> str:
    """标的卡末尾的数据源小字行（2026-10-29 精简：正常态不渲染，异常才出现）。

    正常：不产生 DOM（正常态静默；信号源固定 kr-chart、fail-closed，正常没话可说）。
    失败：`数据源 kr-chart 失败（最后错误 HH:MM） · 已处理到 …`，用 neg 样式醒目
    （源失败即 fail-closed 跳过本轮，这是最该被看见的异常）。字段缺失时如实写
    「无记录 / —」，绝不编时间。feed 里没这个标的 → 「数据源 无记录」。
    """
    sym = str(r.get("symbol") or "")
    fd = feed_map.get(sym)
    bar_txt = _fmt_dt((last_bar or {}).get(sym), "%m-%d %H:%M")
    if fd is None:
        return _whyline(f"数据源 无记录 · 已处理到 {bar_txt}", "muted")
    # 失败行的 source 可能为空（引擎 mark_feed 失败路径不写源名）；本系统信号源
    # 固定 kr-chart（fail-closed、不换源），空缺时兜底写实名，不算编造。
    src = str(fd.get("source") or "kr-chart")
    if fd.get("ok"):
        return ""
    err_at = _fmt_dt(fd.get("error_at"), "%H:%M") if fd.get("error_at") else "—"
    return _whyline(f"数据源 {src} 失败（最后错误 {err_at}） · 已处理到 {bar_txt}", "neg")


def _symbol_dynamics(p: dict) -> str:
    """「三标的动态」区块（2026-10-29 起与「仓位」表对换：仓位在最顶，本块在其后）。

    合并后这里同时回答「离触发线多远」（距离行）和「卡在哪一步 / 被闸门挡了吗」
    （原因行 / 闸门异常行）—— 原「为什么没开仓」区块已删除。P2 再吸收「数据源健康」
    与「已处理到」两张表：每标的名下加一行数据源小字（见 _feed_line），
    三张块合成一张卡。

    fail-soft：某标的两侧数据都缺失 → 价格显示「—」、子行整体不渲染，其余标的不受影响；
    整块任何异常都不能弄挂总览页（调用点在 try 里）。
    区块头时间 = 三个标的里最新的 updated_at（北京时间，与面板其它时间同口径）。
    """
    rows = _merged_cards(p)
    if not rows:
        return ""
    system = _as_dict(p.get("system"))
    feed_map: Dict[str, Dict[str, Any]] = {}
    for fd in (_as_dict(x) for x in _as_list(system.get("feed"))):
        sym = str(fd.get("symbol") or "")
        if sym:
            feed_map[sym] = fd
    last_bar = _as_dict(system.get("last_bar"))
    stamps = [t for t in (_opt_num(r.get("updated_at")) for r in rows) if t]
    when = _fmt_dt(max(stamps), "%H:%M") if stamps else "—"
    o = [f"<h2>三标的动态<span class=\"muted\">行情 {when} · 每 30 分钟随信号 tick 更新</span></h2>",
         # 桌面端 3 列横排（.sym-grid，见 DASH_CSS）；≤640px 由 media query 降为 1 列。
         "<div class=\"card\"><div class=\"grid sym-grid\">"]
    for r in rows:
        o.append(_sym_card(r, feed_map, last_bar))
    # 超跌腿（QQQ）：与上面三个「均线腿」同卡、不同列（RSI2/离场线/距入场），
    # 2026-11-02 用户拍板「标的动态也要能看到 QQQ 离仓价多远」。
    mr_line = _mr_card_line(p)
    if mr_line:
        o.append(mr_line)
    # 图例行已按用户要求整段删除（2026-10-29）；口径细节留在 title 悬停里。
    o.append("</div>")
    o.append("</div>")
    return "".join(o)


# ---------------------------------------------------------------- /why 数据源
# 面板的「原因行 / 闸门异常行」数据来自这里：`app.why.build_rows`（纯只读：现取 4h K 线 →
# strategy.explain_entry 给六步 → Executor.gate_state_for 读闸门）。**面板不重算任何判定**，
# 只做渲染；`GET /api/hl/why` 与容器内 `python -m app.why` 走的也是这一条链路。
def _why_rows(deps: "DashboardDeps") -> List[Dict[str, Any]]:
    """现算 /why 行（只读）。任何异常都退回空列表 —— 诊断区块绝不弄挂总览页。"""
    try:
        from app import why as why_mod
        cfg = _call(deps.config_fn)
        if cfg is None:
            return []
        return why_mod.build_rows(deps.db, cfg)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] /why 诊断计算失败（区块略去）：%s", e)
        return []


_MR_PHASE_TEXT = {"in_window": "窗口内等成交", "no_signal": "空仓", "no_data": "无数据"}


def _et_iso_local(iso: Any) -> str:
    """ET ISO 时刻 → 面板时区（北京时间）短格式；解析失败给「—」，不猜。"""
    try:
        return dt.datetime.fromisoformat(str(iso)).astimezone(
            _display_tz()).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return "—"


def _mr_phase_text(mr: Dict[str, Any]) -> str:
    """MR 相位 → 一句中文结论（仓位表 / 动态卡共用；只描述状态，不复述数字）。"""
    if str(mr.get("recon") or "") == "err" or str(mr.get("state") or "") == "recon_err":
        return "对账不平"
    if not mr.get("enabled"):
        return "停用"
    sig = _as_dict(mr.get("signal"))
    phase = str(sig.get("phase") or "")
    win = _as_dict(sig.get("window"))
    if phase == "signal_ready":
        day = str(win.get("date") or "").strip()
        return f"信号已出，等 {day} 开盘" if day else "信号已出，等开盘"
    if phase == "expired":
        return "窗口已过，等明日"
    if phase == "holding":
        return "持仓中"
    return _MR_PHASE_TEXT.get(phase, "空仓")


#: MR 腿的展示币种（与执行器 ``MR_LEG_COINS`` 同一组；面板只用来分层去重）。
MR_COINS = frozenset({"QQQ"})

#: MR 运行时状态（``mr.py`` 的 ``status["state"]``）→ 面板一句话。
#:
#: 这些状态原来在面板上**完全不可见**：``_mr_phase_text`` 只看信号相位，于是
#: ``action_required``（需人工核对）/ ``order_gate_blocked``（被风控闸门拦下）/
#: ``dry_run``（影子模式，一张真单都没发）全都渲染成「空仓」—— 最需要人动手的三种
#: 状态在页面上长得跟「什么都没发生」一模一样（第七轮复审）。
_MR_STATE_TEXT = {
    "action_required": "需人工核对",
    "order_gate_blocked": "风控闸门拦下",
    "dry_run": "影子模式（未真实下单）",
    "error": "异常",
    "no_equity": "权益不足",
    "no_data": "无标记价",
    "total_notional_unknown": "名义未知（本轮不下单）",
    "skipped_total_notional": "放弃信号（名义上限）",
    "margin_mode_failed": "保证金模式失败",
}


def _mr_view(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 `status.mr` 归一成面板用的视图；停用且空仓 → None（正常态整块不出现）。

    口径（2026-11-02）：离场线 = 最近 4 个已完成会话收盘均值（与 `close > SMA5` 等价）；
    「距离场%」正 = 现价仍在线下方（多单安全）。

    **台账仓（第七轮 B1 / 2026-11-13）**：``status.mr`` 里可能只有 ``ledger_pos``
    （MR 停用/尚未 tick，但 DB ``mr_trades`` 里有一行 OPEN）。这时仍要展示 —— 交易所
    上那张 QQQ 仓是真的，藏起来等于让人忘了它；同时明确标出 ``auto_exit=False``
    （MR 停用 = 不管理，不会自动离场），别再出现「以为它会自己走」的误解。

    **运行时状态（第七轮复审）**：``state`` / ``error`` / ``action_required`` 原样带出，
    台账仓、闸门拦下、影子模式、需人工核对都必须能在页面上看见（见 `_MR_STATE_TEXT`）。
    """
    mr = _as_dict(p.get("mr"))
    if not mr:
        return None
    pos = _as_dict(mr.get("pos"))
    ledger = _as_dict(mr.get("ledger_pos"))
    enabled = bool(mr.get("enabled"))
    from_ledger = not pos and bool(ledger)
    if not enabled and not pos and not ledger:
        return None
    sig = _as_dict(mr.get("signal"))
    src = ledger if from_ledger else pos
    holding = bool(pos) or bool(ledger)
    entry = _opt_num(src.get("entry_price"))
    mark = _opt_num(src.get("mark")) or _opt_num(mr.get("mark"))
    exit_line = _opt_num(src.get("exit_line")) or _opt_num(sig.get("exit_line"))
    dist_exit = _opt_num(src.get("dist_to_exit_pct"))
    # 运行时状态与「需人工核对」的证据包（mr.py 的 _action_required 写入）。
    state = str(mr.get("state") or "")
    error = str(mr.get("error") or "")
    ar = _as_dict(mr.get("action_required"))
    ar_kind = str(ar.get("kind") or "")
    ar_reason = str(ar.get("reason") or "")
    state_txt = _MR_STATE_TEXT.get(state, "")
    recon_bad = (str(mr.get("recon") or "") == "err" or state == "recon_err")
    if recon_bad:
        status = "对账不平"
    elif ar or state == "action_required":
        status = "需人工核对"
    elif state_txt:
        status = state_txt
    elif not enabled and from_ledger:
        status = "停用（不会自动离场）"
    elif not enabled:
        status = "停用"
    elif holding:
        status = "持仓中"
    else:
        status = _mr_phase_text(mr)
    # 一句可执行的原因（面板与 /api/hl/status 同一份文案；不编造，没有就空串）。
    detail = ar_reason or (error if (state_txt or recon_bad) else "")
    if ar and ar_kind:
        detail = f"{ar_kind}：{detail}" if detail else ar_kind
    return {
        "enabled": enabled, "holding": holding, "recon": mr.get("recon"),
        "qty": _opt_num(src.get("qty")), "entry": entry, "mark": mark,
        "upnl": _opt_num(src.get("upnl")), "upnl_pct": _opt_num(src.get("upnl_pct")),
        "notional": _opt_num(src.get("notional")), "margin": _opt_num(src.get("margin")),
        "leverage": _opt_num(src.get("leverage")),
        "entry_ts": _opt_num(src.get("entry_ts")),
        "exit_line": exit_line, "dist_to_exit_pct": dist_exit,
        "dist_to_entry_pct": ((mark / entry - 1.0) * 100.0
                              if (holding and entry and mark) else None),
        "rsi2": _opt_num(sig.get("rsi2")), "threshold": _opt_num(sig.get("threshold")),
        "last_session_date": sig.get("last_session_date"),
        "sessions_usable": sig.get("sessions_usable"),
        "next_eval_et": sig.get("next_eval_et"),
        "phase": sig.get("phase"), "window": _as_dict(sig.get("window")),
        # 台账行只读、且停用时不管理：这两个字段驱动渲染层提示「不会自动离场」。
        "from_ledger": bool(from_ledger),
        "auto_exit": bool(enabled),
        "read_only": bool(from_ledger or not enabled),
        # 运行时状态：面板必须能看见「需人工核对 / 闸门拦下 / 影子模式」。
        "state": state, "state_text": state_txt, "error": error,
        "action_required": bool(ar), "action_kind": ar_kind,
        "action_reason": ar_reason, "detail": detail,
        "anomaly": bool(ar or recon_bad or state_txt),
        "status": status,
    }


def _mr_card_line(p: Dict[str, Any]) -> str:
    """「三标的动态」卡内的超跌腿行（QQQ）：现价/持仓/距入场/RSI2/离场线/距离场/结论。

    与 crypto 三张 `_sym_card` 同卡不同列 —— RSI2 与「密集带宽」不是一套语义，
    硬塞进同一组列会出现 6 个永远为「—」的格子。停用且空仓 → 空串（整行不出现）。
    """
    mr = _mr_view(p)
    if mr is None:
        return ""
    parts: List[str] = []
    if mr.get("mark"):
        parts.append(f"现价 {_fmt(mr['mark'])}")
    if mr["holding"]:
        parts.append(f"持仓 多 {_fmt_sz(mr.get('qty'))} @ {_fmt(mr.get('entry'))}")
        if mr.get("from_ledger"):
            # 台账来源（MR 未 tick/已停用）：价格/浮盈不是实时读数，必须标出来
            parts.append("（台账）")
        if mr.get("dist_to_entry_pct") is not None:
            parts.append(f"距入场 {mr['dist_to_entry_pct']:+.2f}%")
        upnl_txt, upnl_cls = _sign(mr.get("upnl"))
        parts.append(f"浮盈 <span class='{upnl_cls}'>{upnl_txt}</span>")
    if mr.get("rsi2") is not None:
        thr = mr.get("threshold")
        ok = thr is not None and mr["rsi2"] < thr
        parts.append(f"RSI2 {mr['rsi2']:.2f}{' ✔' if ok else ''}"
                     + (f"（阈 {thr:.0f}）" if thr is not None else ""))
    # 离场线/距离场只在持仓时显示（空仓时是假想值，2026-11-02 用户指出）
    if mr["holding"] and mr.get("exit_line"):
        parts.append(f"离场线(SMA5) {_fmt(mr['exit_line'])}")
    if mr["holding"] and mr.get("dist_to_exit_pct") is not None:
        d = mr["dist_to_exit_pct"]
        cls = "warn" if d < 0 else "muted"
        parts.append(f"距离场 <span class='{cls}'>{d:+.2f}%</span>"
                     + ("（已站上，等收盘）" if d < 0 else ""))
    if mr.get("next_eval_et"):
        parts.append(f"下一次评估 {_et_iso_local(mr['next_eval_et'])}")
    # 运行时异常（需人工核对 / 闸门拦下 / 影子模式 / API 异常）：状态词不能只染成
    # 灰色说明 —— 这三种状态下 MR 这一轮**没有按预期工作**，必须一眼看见。
    if mr.get("action_required") or mr.get("recon") == "err" or mr.get("error"):
        status_css = "neg"
    elif mr.get("anomaly"):
        status_css = "warn"
    else:
        status_css = "muted"
    detail_html = (f" <span class='{status_css}'>{_esc(mr['detail'])}</span>"
                   if mr.get("detail") else "")
    return ("<div class='mrline'>"
            "<b>超跌腿 · QQQ</b> <span class='muted'>MR</span> "
            + "".join(f"{x} · " for x in parts)
            + f"<b class='{status_css}'>{_esc(mr['status'])}</b>"
            + detail_html + "</div>")


def _sign(v: Any, unit: str = " U") -> tuple:
    """返回 (带符号文本, 颜色 class)。"""
    f = _opt_num(v)
    if f is None:
        return "—", ""
    cls = "pos" if f > 0 else ("neg" if f < 0 else "")
    return f"{f:+,.2f}{unit}", cls


def _neg_txt(v: Any, txt: str) -> str:
    """统计行里负值标红（现有 neg 类）；0 与正值原样返回。"""
    f = _opt_num(v)
    return f"<span class='neg'>{txt}</span>" if (f is not None and f < 0) else txt


# ---------------------------------------------------------------- 已实现亏损熔断：解除前复核
# 口径必须与执行器 `Executor._realized_dd_guard`（ExecParams 的默认值）一致：
# 近 N 天 Σ(closed_pnl − fee) / 当前权益，rate ≤ −kill 即熔断。这里**只读复核**，
# 不改判据、不重置任何基线 —— 真正的风控动作在执行器里。
REALIZED_DD_WINDOW_DAYS = 30
REALIZED_DD_KILL_PCT = 0.20


def equity_hint(db: Any, cfg: Any = None) -> tuple:
    """当前权益的最佳可得值（**零网络**）→ ``(value|None, 来源说明)``。

    优先用面板快照里调度器刚读到的权益（≤ 快照过期阈值），否则退回配置里的定仓基数
    （``equity``）。两者都拿不到 → ``(None, ...)``：调用方必须把它写成「无法判定」，
    绝不能拿 0 当权益（比率会被放大成任何数）。
    """
    try:
        from app.panel_store import DEFAULT_STALE_S
    except Exception:                                             # noqa: BLE001
        DEFAULT_STALE_S = 900.0
    try:
        row = db.panel_snapshot_latest() if db is not None else None
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] 读面板快照权益失败（退回配置基数）：%s", e)
        row = None
    if row:
        try:
            age = time.time() - float(row.get("ts") or 0.0)
        except (TypeError, ValueError):
            age = DEFAULT_STALE_S + 1
        snap = row.get("snapshot") or {}
        eq = _opt_num((snap or {}).get("equity_total")) or _opt_num((snap or {}).get("equity"))
        if 0 <= age <= DEFAULT_STALE_S and eq and eq > 0:
            return eq, f"面板快照（{int(age // 60)} 分钟前）"
    base = _opt_num(getattr(cfg, "equity", None)) if cfg is not None else None
    if base and base > 0:
        return base, "配置里的定仓基数"
    return None, "权益不可得"


def realized_dd_clear_block(db: Any, cfg: Any = None, equity: Any = None) -> Optional[dict]:
    """解除「已实现亏损熔断」之前，用**同一个 30 天窗口**再算一遍。

    为什么需要（第七轮 B7 / 信号报告 F2）：判据是滚动窗口，解除后下一轮（≤5 分钟）
    会再触发 —— 面板/curl 承诺「可以恢复」，实际按下去必然弹回来，唯一的累计亏损
    保护看起来像坏按钮。政策（本轮用户拍板）：**保留 30 天窗口、不重置基线**；
    仍越阈值时**拒绝解除并解释剩余亏损与阈值**。

    返回 ``None`` 表示「不判」（熔断线关掉 / 权益不可得 → 不该拦），否则返回::

        {"net", "rate", "base", "window_days", "kill_pct", "warn_pct", "over"}

    ``over=True`` 时调用方必须拒绝解除，并把 net / rate / base 原样解释给用户。
    """
    win = int(getattr(cfg, "realized_dd_window_days", REALIZED_DD_WINDOW_DAYS) or 0)
    kill = float(getattr(cfg, "realized_dd_kill_pct", REALIZED_DD_KILL_PCT) or 0.0)
    warn = float(getattr(cfg, "realized_dd_warn_pct", 0.10) or 0.0)
    if win <= 0 or kill <= 0:
        return None
    base = _opt_num(equity)
    if base is None or base <= 0:
        return None
    try:
        net = float(db.hl_realized_net_since(time.time() - win * 86400.0) or 0.0)
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] 读已实现净额失败（不拦解除）：%s", e)
        return None
    rate = net / base
    return {"net": net, "rate": rate, "base": base, "window_days": win,
            "kill_pct": kill, "warn_pct": warn, "over": rate <= -kill}


def realized_dd_refusal_text(detail: Dict[str, Any]) -> str:
    """拒绝解除时的人话（同一条文案给面板与 curl，避免两处口径分叉）。"""
    net = _num(detail.get("net"))
    base = _num(detail.get("base"))
    rate = _num(detail.get("rate"))
    return (f"未解除：近 {int(_num(detail.get('window_days'), 30))} 天已实现净额 "
            f"{net:+,.2f}U（{rate * 100:+.1f}%），仍越过熔断线 "
            f"-{_num(detail.get('kill_pct'), REALIZED_DD_KILL_PCT) * 100:g}%"
            f"（按权益 {base:,.2f}U 计）。亏损窗口没有重置：确认基线等于改风控口径，"
            f"面板不提供该动作。要继续交易请等 30 天滚动窗口自然回正，"
            f"或按 runbook 由人工直接改 DB/停用执行器。")


def _grace_seconds_cfg(cfg: Any) -> float:
    """面板侧读宽限秒数：脏值/越界一律按 900（与 main._grace_seconds 同口径）。

    这里**不 import** app.main / app.hl.executor：面板是纯渲染层，为了一个数字把
    建库、SDK 依赖拖进来不值得（历史教训：views 一旦 import 重模块，面板测试就会
    连带建库）。两个常量各自一份，但都钉在同样的默认与范围上。
    """
    try:
        v = float(getattr(cfg, "hl_close_grace_s", _GRACE_DEFAULT_S))
    except (TypeError, ValueError):
        return _GRACE_DEFAULT_S
    return v if _GRACE_MIN_S <= v <= _GRACE_MAX_S else _GRACE_DEFAULT_S


def _grace_first_seen(db: Any, pid: Any) -> Optional[float]:
    """读该仓宽限期的起点（configuration 的 `hl_close_grace:<pid>`，格式 "<ts>:<rounds>"）。

    读不到/读不懂 → None（调用方按「刚进入宽限」算剩余，绝不因为读不到就显示空白）。
    """
    try:
        raw = str(_db_call(db, "get_config_str", f"hl_close_grace:{int(pid)}", "") or "").strip()
    except Exception:                                            # noqa: BLE001
        return None
    head = raw.split(":", 1)[0]
    try:
        ts = float(head)
    except (TypeError, ValueError):
        return None
    return ts if ts > 0 else None


def hl_status_text(status: Any) -> str:
    """hl_status → 中文（未知值兜底原文；空串 → 空串）。"""
    s = str(status or "")
    return _HL_ST_TXT.get(s, s)


def _signals_html(signals: list) -> str:
    """「信号」表（仓位历史）的 HTML 片段：标题 + 表格，仅此一块。

    2026-10-18 从总览迁到 /admin?tab=records（用户要求：总览只看当下，历史表格归记录页）。
    渲染逻辑原样保留：状态文案走 _ST_TXT，open=pos / pending=warn 着色，
    数量用 _fmt_sz（HL 仓是真实币量 0.00084，不能显示成 0.00）。

    PR-3：payload 里带 ``net_pnl`` 键时（只在 hl_pnl_from_fills 打开时才有）多显示
    一列「净额（流水）」。**按行探测而不是按全局开关**：这样本函数保持纯渲染、不依赖
    config，关掉开关时表格结构与老版本完全一致。
    """
    o: List[str] = []
    rows = _as_list(signals)
    dicts = [_as_dict(x) for x in rows]
    with_pnl = any("net_pnl" in d for d in dicts)
    o.append("<h2>信号</h2>")
    o.append("<div class='tscroll'><div class='tablewrap'><table><thead><tr>"
             "<th>标的</th><th>状态</th><th>方向</th><th class='num'>入场</th>"
             "<th class='num'>止损</th><th class='num'>数量</th>"
             + ("<th class='num'>净额（流水）</th>" if with_pnl else "")
             + "<th>信号 bar</th>"
             "</tr></thead><tbody>")
    for sg in dicts:
        st = str(sg.get("status") or "—")
        st_cls = {"open": "pos", "pending": "warn"}.get(st, "")
        hl_st = str(sg.get("hl_status") or "")
        hl_note = ""
        hl_txt = hl_status_text(hl_st) if hl_st else ""
        # hl_status 与主状态同词时不显示第二行（曾出现「已平仓」+ 原始值 closed 两种
        # 写法同现；现在 closed 已映射成中文，但同词再显示一遍仍是噪音）。
        if hl_txt and hl_txt != _ST_TXT.get(st, st):
            # 第二行：执行器侧的账实状态（closing/mismatch/...）。宽限期额外给剩余时间，
            # 「超时前不留空白」—— 剩 0 分钟也要写出来（那正是「马上要升级成异常」）。
            extra = ""
            if hl_st == "closing":
                left = _opt_num(sg.get("grace_left_s"))
                if left is not None:
                    mins = max(0, int(math.ceil(max(0.0, left) / 60.0)))
                    extra = f" · 剩 {mins} 分钟"
            hl_note = (f"<div class='muted'>{_esc(hl_txt)}"
                       f"{_esc(extra)}</div>")
        # 单粒度资金费小字（feat/funds-ledger）：HL 持仓/已平仓行在状态下方多一行
        # 「本单资金费合计 ±X U」（正=收入，与面板全局口径一致）。非 HL 行 / 没有资金费
        # 记录的仓不带 funding_sum 键 → 不加行，表格结构不变。
        if sg.get("funding_sum") is not None:
            fund_txt, _cls = _sign(sg.get("funding_sum"))
            hl_note += f"<div class='muted'>本单资金费合计 {_esc(fund_txt)}</div>"
        pnl_cell = ""
        if with_pnl:
            # net_pnl 为 NULL 时显示「—」：没算过/查不到成交 ≠ 0 收益
            pnl_cell = _td(_fmt(sg.get("net_pnl")), "num")
        o.append("<tr>"
                 + _td(_esc(sg.get("coin") or _disp(sg.get("symbol")) or "—"))
                 + _td(f"<span class='{st_cls}'>{_esc(_ST_TXT.get(st, st))}</span>" + hl_note)
                 + _td("多" if _num(sg.get("side"), 1) >= 0 else "空")
                 + _td(_fmt(sg.get("entry")), "num")
                 + _td(_fmt(sg.get("stop")), "num")
                 + _td(_fmt_sz(sg.get("qty")), "num")
                 + pnl_cell
                 + _td(_fmt_dt(sg.get("bar_ms")), "num")
                 + "</tr>")
    if not rows:
        span = 8 if with_pnl else 7
        o.append(f"<tr><td colspan='{span}' class='muted'>暂无信号</td></tr>")
    o.append("</tbody></table></div></div>")
    return "".join(o)


def _pct_txt(v: Any, dash: str = "—") -> str:
    """阈值 → 人话百分比（0.25 → 25%）。面板上只讲「25%」，不暴露内部小数。"""
    f = _opt_num(v)
    return dash if f is None else f"{f * 100:g}%"


def _span_links(cur: str, k: str = "", refresh: int = 0) -> str:
    """盈亏曲线时间窗切换链接（P1 功能二，零 JS 纯 <a>）。

    当前项标 ✅ 并用高亮 btn；``refresh`` 原样带回。链接统一指向 /admin?tab=overview
    （面板已合并为单一入口，在 /hl 旧页点上也会回到面板）。

    P1-C：``k`` 形参保留只为调用点稳定，**不再拼进链接** —— 已鉴权的 HTML 不许含
    ADMIN_TOKEN 真值，会话靠 ``cma_admin`` Cookie 延续。
    """
    cur = normalize_span(cur)
    parts: List[str] = []
    for val, label in SPAN_CHOICES:
        href = f"/admin?tab=overview&span={val}"
        if refresh:
            href += f"&refresh={int(refresh)}"
        if val == cur:
            parts.append(f"<a class='btn' href='{href}'>✅ {label}</a>")
        else:
            parts.append(f"<a class='btn gray' href='{href}'>{label}</a>")
    return " ".join(parts)


def _perf_line_html(p: dict) -> str:
    """「上线以来」一行（2026-10-29 用户要求：原策略绩效整卡精简成这一行，并入盈亏曲线）。

    纯渲染：数字全部来自 payload.perf（_perf_stats 只读聚合）+ account.total_realized
    （U 对照，全页唯一出处仍是这行）。无已平仓记录 → 只显「暂无」+ U 对照。
    """
    perf = _as_dict(p.get("perf"))
    n = int(_num(perf.get("n")))
    avg_r = _opt_num(perf.get("avg_r"))
    acc = _as_dict(p.get("account"))
    u_txt, u_cls = _sign(acc.get("total_realized"))
    u_seg = (f" · 累计已实现 <b class='{u_cls}'>{u_txt}</b>"
             if u_txt != "—" else "")
    if n <= 0 or avg_r is None:
        # N=0 不除零、不编数；U 对照仍要出现（「累计已实现」全页唯一出处在这行）。
        return f"<div class=\"legend\">上线以来：暂无已平仓记录{u_seg}</div>"
    total_r = _num(perf.get("total_r"))
    wins = int(_num(perf.get("wins")))
    win_rate = _num(perf.get("win_rate"))
    return ("<div class=\"legend\">上线以来：已平仓 <b>"
            f"{n}</b> 笔（多 {int(_num(perf.get('long')))} · 空 {int(_num(perf.get('short')))}） · "
            f"均R <b class=\"{'pos' if avg_r >= 0 else 'neg'}\">{avg_r:+.2f}</b> · "
            f"胜率 {win_rate * 100:.0f}%（赢 {wins} 笔） · "
            f"累计 R <b class=\"{'pos' if total_r >= 0 else 'neg'}\">{total_r:+.1f}</b>"
            f"{u_seg}</div>")


def _perf_block_html(p: dict) -> str:
    """「上线以来」一行的渲染兜底：只读聚合，**不依赖出入金流水**。

    抽成函数是为了让 Kraken（无流水端点、曲线整卡不画）也能单独拿到这一行 ——
    它的数字来自已平仓记录与 account.total_realized，与流水无关。
    """
    try:
        return _perf_line_html(p) or ""
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] 上线以来统计行渲染失败：%s", e)
        return ""


def _dd_line_html(meta: Dict[str, Any]) -> str:
    """「策略回撤 …」那一行（§7）：可见文字，不靠 title 悬停。

    必须写清两个「回撤」的区别 —— 同一区块里「最大回撤 19.54U」是 **U 金额**口径
    （pnl 序列峰谷），本行是 **百分比**口径（TWR 峰谷、已剔除出入金）；不写清用户会
    以为两个数字在打架。
    """
    st = str(meta.get("dd_status") or "unavailable")
    txt = str(meta.get("dd_status_text") or _DD_ST_TXT.get(st, "暂不可用"))
    note = str(meta.get("dd_note") or "")
    # 不可用/已关闭都要把**原因**写在括号里：只写「暂不可用」等于让人去猜是流水坏了
    # 还是自己把开关关了。
    if st in ("unavailable", "off") and note:
        txt = f"{txt}（{note}）"
    pct = _opt_num(meta.get("dd_pct"))
    pct_txt = "—" if pct is None else f"{pct * 100:.2f}%"
    # 【低3】over_kill 用 warn 色（不是 neg/红）：它是「需要注意」而不是「已停手」——
    # 红色会和 killed 抢视觉优先级，用户分不清哪个才真的要停手。
    cls = ("neg" if st == "killed"
           else "warn" if st == "over_kill"
           else ("muted" if st != "warn" else ""))
    span = f"<span class='{cls}'>{_esc(txt)}</span>" if cls else _esc(txt)
    # 「剔除出入金，峰值以来；按流水同步节奏更新」括注已按用户要求删除（2026-10-29）。
    wait = str(meta.get("dd_wait_note") or "").strip()
    tail = f" · <span class='neg'>{_esc(wait)}</span>" if wait else ""
    return (f"<div class=\"legend\">策略回撤 {pct_txt} · "
            f"告警线 {_pct_txt(meta.get('dd_warn_pct'))} · "
            f"熔断线 {_pct_txt(meta.get('dd_kill_pct'))} · 当前状态：{span}{tail}</div>")



def _empty_positions_html(p: dict, reachable: bool) -> str:
    """空仓引导卡（P0-3）：空仓时回答「在等什么、还差多远」。

    只在**确实没有仓**（无交易所仓位、也无 QQQ MR 腿）时渲染；有仓时表格自己说话，
    本卡整块不出现 —— 同一屏不出现两套「现在什么状态」。

    reachable=False → 简化版：交易所读不到就**不知道**有没有仓，绝不能写「交易所无持仓」
    （把「不知道」说成「没有」）；此时 symbol_state 缓存也可能过期，距离一概不展示。

    距离口径复用 ``_need_pct``（上破 = (线−现价)/现价、下破 = (现价−线)/现价），
    与「三标的动态」卡里的 ②③ 两行同一套算法，不做第二套算术。

    现价（N2/N3）：距离里的现价取 ``_merged_cards`` 的同源值（与三标的大字卡同一个数，
    不再读 signal tick 缓存的 price）；本卡**只答「还差多远」，不再单独显示现价** ——
    现价由大字卡统一展示，同屏只出现一次。
    """
    if not reachable:
        return ("<div class='card card-empty-state'>"
                "<div class='empty-icon'>&#128225;</div>"
                "<h3>交易所数据暂不可读</h3>"
                "<div class='empty-desc'>"
                "<p>当前持仓 <b>无法确认</b>（不是「已确认空仓」）</p>"
                "<p>恢复读取后本卡自动换成实时状态 · 信号 / 下单 / 成交见「记录」页</p>"
                "</div></div>")

    rows = [_as_dict(x) for x in _as_list(p.get("symbol_state"))]
    # 现价同源（N2）：_merged_cards 是「why 现价优先、symbol_state 兜底」的收敛点，
    # 也就是「三标的动态」大字卡显示的那个数 —— 从这里取，同屏才不会出现
    # 74,407（why） vs 74,416（signal tick 缓存）两个现价。
    # 触发线仍然只取 symbol_state 的 long/short_trigger（策略唯一出处，不从这里换源）。
    px_by_sym: Dict[str, Optional[float]] = {}
    px_by_coin: Dict[str, Optional[float]] = {}
    for c in _merged_cards(p):
        cpx = _opt_num(c.get("price"))
        if cpx is None:
            continue
        csym = str(c.get("symbol") or "").strip()
        ccoin = str(c.get("coin") or "").strip().upper()
        if csym:
            px_by_sym[csym] = cpx
        if ccoin:
            px_by_coin[ccoin] = cpx
    coins = [str(r.get("coin") or _disp(r.get("symbol")) or "").strip() for r in rows]
    coins = [c for c in coins if c]
    watch = " / ".join(coins) if coins else "—"

    items: List[str] = []
    for r in rows:
        coin = str(r.get("coin") or _disp(r.get("symbol")) or "").strip()
        if not coin:
            continue
        # 现价取同源值（N2）：先按 symbol 命中，再按 coin 兜一次，最后才退回本行缓存
        # （why 行缺这个标的时 _merged_cards 本来就兜到 symbol_state，这里只是双保险）。
        sym = str(r.get("symbol") or "").strip()
        px = px_by_sym.get(sym)
        if px is None:
            px = px_by_coin.get(coin.upper())
        if px is None:
            px = _opt_num(r.get("price"))
        parts = [f"<span class='coin'>{_esc(coin)}</span>"]
        # (标签, 到位文案, 颜色, 所需幅度)：上破绿 / 下破红，与全站方向配色一致
        for label, hit, cls, need in (
                ("上破还差", "已上破", "pos",
                 _need_pct("break_up", px, r.get("long_trigger"))),
                ("下破还差", "已下破", "neg",
                 _need_pct("break_down", px, r.get("short_trigger")))):
            parts.append(f"<span class='trigger-label'>{label}</span>")
            if need is None:
                # 触发线没算/没缓存：如实「—」，不拿 0 代替
                parts.append("<span class='trigger-val muted'>—</span>")
            elif need <= 0:
                parts.append(f"<span class='trigger-val {cls}'>{hit}</span>")
            else:
                parts.append(f"<span class='trigger-val {cls}'>{need:+.1f}%</span>")
        # 现价不再在引导卡里重复一遍（N3）：三标的大字卡就在下方，同屏出现两个现价
        # 只是噪音（历史上还因数据源不同打过架）。引导卡只回答「还差多远」。
        items.append("<div class='trigger-item'>" + "".join(parts) + "</div>")

    trig_html = ("<div class='empty-triggers'>" + "".join(items) + "</div>"
                 if items else
                 "<div class='empty-triggers'><span class='muted'>"
                 "行情缓存暂无记录，下一轮 tick 后显示距离</span></div>")
    return ("<div class='card card-empty-state'>"
            "<div class='empty-icon'>&#128269;</div>"
            f"<h3>正在监控 {_esc(watch)}</h3>"
            "<div class='empty-desc'>"
            "<p>策略：4h 密集突破自动开仓</p>"
            "<p>等待：6 均线密集满 6 根 + 收盘突破触发线</p>"
            "</div>"
            + trig_html +
            "<div class='empty-desc'><p>"
            "<span class='muted'>信号 / 下单 / 成交见「记录」页</span></p></div>"
            "</div>")


def _render_body(p: dict) -> str:
    mode = str(p.get("mode") or "off")
    # 该 venue 有没有出入金流水端点（Kraken 期货 = 没有）。面板据此整块不画
    # 「盈亏曲线 / 策略回撤 / 资金费」—— 那些 U 全是流水口径，缺源就是缺源。
    no_flows = p.get("flows_supported") is False
    net = _as_dict(p.get("network"))
    acc = _as_dict(p.get("account"))
    system = _as_dict(p.get("system"))
    positions = _as_list(p.get("positions"))
    orders_open = _as_list(p.get("orders_open"))
    curve = _as_dict(p.get("curve"))
    curve_meta = _as_dict(curve.get("meta"))
    kill = bool(p.get("kill"))
    kill_reason = p.get("kill_reason") or ""
    reachable = bool(net.get("reachable"))
    latency = _opt_num(net.get("latency_ms"))
    error = str(net.get("error") or "")
    # 对账三态（审核 C1：True 不一致 / False 一致 / None 无法核对）的展示口径已搬进
    # _health_badge_html —— 状态条合并成一枚「系统」徽章后，本函数不再直接判对账。
    pnl_all = _curve_points(curve.get("pnl"))
    twr_all = _curve_points(curve.get("twr"))
    # 时间窗（P1）：GET span ∈ 7/30/all，缺参/非法值回落 7（normalize_span 白名单）。
    span = normalize_span(p.get("curve_span"))
    shown_pnl, shown_twr, curve_span, _actual_days = _curve_window(pnl_all, twr_all, span)

    o: List[str] = []
    o.append(H1_BLOCK)

    # ---------------- 顶部状态条（P0-2：两层） ----------------
    # 第一层 3 个核心结论：模式 / 系统健康（熔断+对账+卡单 已合并成一句）/ 持仓。
    # 第二层辅助读数（字号更小）：Kraken 延迟 / API 有效期 / 更新时间。
    # 两层都是 width:100% 的 flex 容器，靠父 .bar 的 flex-wrap 各占一行 —— .bar 本身
    # 的规则不动（/hl 与 /admin 共用同一份 CSS，admin 的状态条也是同一个结构）。
    o.append("<div class='bar'>")
    o.append("<span class='bar-primary'>")
    o.append(_badge(_MODE_CLS.get(mode, "muted"), "模式", _esc(_MODE_TXT.get(mode, mode)),
                    "badge-status"))
    o.append(_health_badge_html(system, kill, kill_reason))
    o.append(_positions_badge_html(positions, p.get("mr"),
                                   reachable=bool(_as_dict(p.get("network")).get("reachable", True))))
    o.append("</span>")
    o.append("<span class='bar-secondary'>")
    lat_txt = f"{latency:.0f} ms" if latency is not None else "—"
    # 这是「服务器 → Kraken」的耗时（容器每次拉账户快照的开销），不是你到面板的网络延迟。
    # 快照模式（阶段 B）下本次渲染没访交易所，数字来自调度器上一轮的实测，标题必须说清。
    if bool(net.get("latency_from_snapshot")):
        lat_title = "调度器上一轮读 Kraken 的耗时（本次渲染没有访问交易所）；与你的浏览器到本站的延迟无关"
    else:
        lat_title = ("oracle1 上的容器请求 Kraken /derivatives 的耗时（含失败重试）；"
                     "与你的浏览器到本站的延迟无关")
    o.append(_strip_badge("pos" if latency is not None else "muted", "Kraken 延迟", lat_txt,
                          lat_title, "badge-small"))
    # API 有效期徽章（P3 R3）：唯一常驻出处（原「API 有效期」卡降级为总览末尾的 details）。
    o.append(_agent_badge_html(p.get("agent"), "badge-small"))
    o.append(_strip_badge("muted", "更新", _fmt_dt(p.get("ts")),
                          "本页数据的生成时刻（北京时间）", "badge-small"))
    o.append("</span>")
    o.append("</div>")

    if not reachable:
        o.append(f"<div class='banner'>&#9888;&#65039; 未配置 / 读取失败：{_esc(error or '未知原因')}<br>"
                 f"<span class='muted'>交易所侧数据暂不可用；信号 / 下单 / 成交记录见「记录」页。</span></div>")

    # ---------------- 账户横幅（P1-2：只搬位置 + 换壳）----------------
    # 位置（用户钉死）：H1 → 状态条 →（不可达横幅）→ 账户横幅 → 仓位 → 曲线 → …
    # 账户权益是这一页的主语，先给钱、再看监控。整块内容原样搬来：字段、口径、
    # legend 文案、<details> 资金明细一项不少、一个字不改，只改位置与外层 class。
    # 横幅样式（.acct-banner）见 DASH_CSS：关键数一行 flex 铺开、窄屏自动换行，零 JS。
    o.append("<div class='card acct-banner'><div class='sec'>账户</div>")
    today_txt, today_cls = _sign(acc.get("today_realized"))
    fees = _opt_num(acc.get("fees_total"))
    # 可提口径与旧版一致：统一账户且交易所 withdrawable<=0 时按 现货−占用 现算
    # （手续费说明已按用户要求删除，2026-10-29；口径细节保留在 title 悬停）。
    wd = _opt_num(acc.get("withdrawable"))
    if wd is None:
        # 交易所读不到（reachable=False → account 金额是 None）：可提余额**未知**，
        # 不是 0。以前 _num 兜成 0 会显示「可用保证金 0.00 U」，用户会以为钱没了。
        wd_html, wd_title = "—", ""
    elif acc.get("unified") and wd <= 0:
        avail = max(_num(acc.get("spot_usdc")) - _num(acc.get("spot_hold")), 0.0)
        wd_html = f"{_fmt(avail)} U"
        wd_title = "可用保证金：扣掉持仓与挂单占用后还能用来开新仓的金额"
    else:
        wd_html = f"{_fmt(wd)} U"
        wd_title = ""
    wd_t = f" title='{_esc(wd_title)}'" if wd_title else ""
    o.append("<div class='acct-top'>"
             f"<span>账户权益 <b>{_fmt(acc.get('equity'))} U</b></span>"
             f"<span{wd_t}>可用保证金 <b>{wd_html}</b></span></div>")
    o.append("<details class='acct-details'><summary>资金明细</summary>")
    o.append("<div class='acct-sub'>")
    # Kraken 期货没有钱包地址（venue_address_free）：这一行整行不画 —— 那里的
    # address 只是 main 填的占位常量，展示出来等于凭空造了个地址。
    if not p.get("venue_address_free"):
        o.append(_asub("钱包地址", _addr_text(acc.get("address"))))
    if acc.get("unified") and not acc.get("spot_ok", True):
        # 现货状态读取失败 → equity_total 只剩浮盈，数字完全不可信，必须喊出来
        o.append("<span class='neg'>⚠️ 现货余额读取失败：总值/可提不可信（只剩浮盈），不是没钱</span>")
    if acc.get("unified"):
        # 统一账户：资金分布在「现货」和「合约」两侧 —— 用人话一行说完。
        # 合约侧占用的就是已用保证金（同一个数），不再单列重复一列。
        # 衍生品钱包：不拆现货/合约两侧，只报「可用 / 已用保证金」。
        o.append(_asub("保证金（独立衍生品）",
                       f"可用 {_fmt(acc.get('spot_usdc'))} U · 已用 {_fmt(acc.get('margin_used'))} U",
                       title="独立衍生品钱包：多币种抵押统一折算；已用 = 持仓与挂单占用的保证金"))
    if not acc.get("unified"):
        o.append(_asub("已用保证金", f"{_fmt(acc.get('margin_used'))} U"))
    o.append(_asub("持仓名义", f"{_fmt(acc.get('notional'))} U"))
    o.append(_asub("今日已实现", today_txt, today_cls,
                   title="交易所 closedPnl 口径，未扣手续费与资金费"))
    if fees is None:
        o.append(_asub("净手续费（付出）", "—"))
    else:
        o.append(_asub("净手续费（付出）", f"{-fees:+,.2f} U", "neg" if fees > 0 else ""))
    o.append("</div>")
    o.append("</details>")
    # 注脚（可见文字，iOS Safari 无 title 悬停）：账户相关数字的口径收口。
    # 「已实现」两行的 title 保留作桌面增强，这里给出手机唯一能看到的口径。
    o.append('<div class="legend">已实现为交易所口径，未扣手续费与资金费 · '
             '可用 = 权益 − 持仓/挂单占用</div>')
    o.append("</div>")

    # ---------------- 仓位 ----------------
    # 窄屏（≤640px）只留 8 个关键列（.hidem 列 display:none，不横向滚动）；被隐藏列
    # 的数值在每仓下方 tr.subm 小字行**原样补足**（宽屏 CSS 隐藏小字行、窄屏隐藏列，
    # 同一时刻同一事实只显示一份 —— 纯 CSS 显隐，零 JS）。表格仍是单一实现，
    # 不复制第二套「手机版表」。
    # 「本单资金费」列：数据源是交易所资金费流水；Kraken 期货 `user_funding_history`
    # 恒空 → 整列不渲染（表头 / 单元格 / 小字行 / colspan 四处同步，不留空列错位）。
    # QQQ MR 腿与交易所仓位同一张表：先取视图，供「有没有仓」判定与表格行共用。
    mr = _mr_view(p)
    has_pos = bool(positions) or (mr is not None and mr["holding"])
    o.append("<h2>仓位</h2>")
    if not has_pos:
        # 空仓：一张只有表头的空表说不出任何事 —— 换成引导卡（在等什么、还差多远）。
        o.append(_empty_positions_html(p, reachable))
    else:
        show_fund_col = not no_flows
        ncol = 13 if show_fund_col else 12
        o.append("<div class='tscroll'><div class='tablewrap'><table><thead><tr>"
                 "<th>币种</th><th>方向</th><th class='num hidem'>数量</th><th class='num'>入场</th>"
                 "<th class='num'>标记</th><th class='num'>浮盈浮亏</th><th class='num'>R</th>"
                 "<th class='num'>止损</th><th class='num'>止损距离</th><th class='num hidem'>杠杆</th>"
                 "<th class='num hidem'>保证金</th><th class='num hidem'>强平价</th>"
                 + ("<th class='num hidem'>本单资金费</th>" if show_fund_col else "")
                 + "</tr></thead><tbody>")
        for pos in (_as_dict(x) for x in positions):
            upnl_txt, upnl_cls = _sign(pos.get("unrealized_pnl"))
            side_txt = "多" if _num(pos.get("side"), 1) >= 0 else "空"
            dist = _opt_num(pos.get("stop_dist_pct"))
            r_txt, r_cls = _sign(pos.get("r_unrealized"), "R")
            # 本单资金费（正=收入）：交易所 cumFunding.sinceOpen 已在 status_payload
            # 换算成收入视角；交易所没给（老快照）就如实显示「—」，不猜 0。
            # Kraken 期货没有资金费流水 → 该列整列不渲染（fund_txt 仍算，仅小字行用）。
            fund_txt, fund_cls = _sign(pos.get("funding_income"))
            # ROE%（对齐 HL 网页习惯）＝ 浮盈 ÷ 本仓保证金。保证金 0/取不到时只显 U
            # 不显 % —— 除数都没有，百分比是编出来的。
            margin_v = _opt_num(pos.get("margin_used"))
            upnl_v = _opt_num(pos.get("unrealized_pnl"))
            roe_txt = (f" · {upnl_v / margin_v * 100.0:+.2f}%"
                       if margin_v and upnl_v is not None else "")
            if pos.get("has_stop"):
                stop_cell = f"<span class='pos'>&#10004;&#65039; {_fmt(pos.get('stop_px'))}</span>"
            elif pos.get("stop_unknown"):
                stop_cell = "<span class='muted'>? 无法确认</span>"
            else:
                stop_cell = "<span class='neg'>&#10060; 无</span>"
            # 交易所里有 QQQ 但 MR 台账没认领：这行必须按「异常」标出来，否则它就是
            # 一张没人管、没有止损、且不知道属于谁的仓（第七轮 B1 要求不能藏）。
            coin_cell = f"<b>{_esc(pos.get('coin') or '—')}</b>"
            if pos.get("mr_unclaimed"):
                coin_cell += " <span class='warn' title='交易所仓位没有被 MR 台账认领："\
                             "可能是手工单、台账丢失或数量对不上'>未认领</span>"
            o.append("<tr>"
                     + _td(coin_cell)
                     + _td(f"<b>{side_txt}</b>")
                     + _td(_fmt_sz(pos.get("size")), "num hidem")
                     + _td(_fmt(pos.get("entry_px")), "num")
                     + _td(_fmt(pos.get("mark_px")), "num")
                     + _td(upnl_txt + roe_txt, f"num {upnl_cls}".strip())
                     + _td(r_txt, f"num {r_cls}".strip())
                     + _td(stop_cell, "num")
                     + _td(f"{dist * 100:.2f}%" if dist is not None else "—", "num")
                     + _td(_lev_text(pos)
                           + (" <span class='muted'>逐</span>" if pos.get("isolated") else ""),
                           "num hidem")
                     + _td(_fmt(pos.get("margin_used")), "num hidem")
                     + _td(_fmt(pos.get("liq_px")) if _opt_num(pos.get("liq_px")) else "—",
                           "num hidem")
                     + (_td(fund_txt, f"num hidem {fund_cls}".strip()) if show_fund_col else "")
                     + "</tr>")
            # 小字行：与表格同一份 payload 数值、同一套格式化函数（同一事实一种写法），
            # 只是载体不同 —— 窄屏隐藏列时由它补足，宽屏整个 <tr> 被 CSS 藏掉。
            subm_fund = f" · 本单资金费 {fund_txt}" if show_fund_col else ""
            subm_extra = (" · ⚠️ 未被 MR 台账认领（手工单 / 台账缺失）：不会自动离场"
                          if pos.get("mr_unclaimed") else "")
            o.append(f"<tr class='subm'><td colspan='{ncol}'>"
                     f"数量 {_fmt_sz(pos.get('size'))} · "
                     f"{_lev_text(pos, long=True)}"
                     f"{' 逐' if pos.get('isolated') else ''} · "
                     f"保证金 {_fmt(pos.get('margin_used'))} U · "
                     f"强平 {_fmt(pos.get('liq_px')) if _opt_num(pos.get('liq_px')) else '—'}"
                     f"{subm_fund}{subm_extra}</td></tr>")
        if mr is not None and mr["holding"]:
                upnl_txt, upnl_cls = _sign(mr["upnl"])
                pct = mr["upnl_pct"]
                # MR 腿的 upnl_pct 是「价格涨跌幅」口径（mr.py 按 (mark-entry)/entry 算），
                # 没有「浮盈 ÷ 保证金」的 ROE 分母 —— 不标注会被下面的 ROE 图例带偏（2026-11-07）。
                pct_txt = (f" · {pct:+.2f}%（价格）" if pct is not None else "")
                dist_txt = (f"{mr['dist_to_exit_pct']:+.2f}%"
                            if mr["dist_to_exit_pct"] is not None else "—")
                # 台账来源（MR 停用/未 tick）→ 明确写「台账」，别让人以为这是实时状态。
                mr_src = (" <span class='muted'>MR · 台账</span>" if mr.get("from_ledger")
                          else " <span class='muted'>MR</span>")
                o.append("<tr>"
                         + _td("<b>QQQ</b>" + mr_src)
                         + _td("<b>多</b>")
                         + _td(_fmt_sz(mr.get("qty")), "num hidem")
                         + _td(_fmt(mr.get("entry")), "num")
                         + _td(_fmt(mr.get("mark")), "num")
                         + _td(upnl_txt + pct_txt, f"num {upnl_cls}".strip())
                         + _td("—", "num")
                         + _td("<span class='muted'>—</span>", "num")
                         + _td("—", "num")
                         + _td(_lev_text({"leverage": mr.get("leverage")}),
                               "num hidem")
                         + _td(_fmt(mr.get("margin")), "num hidem")
                         + _td("—", "num hidem")
                         + (_td("—", "num hidem") if show_fund_col else "")
                         + "</tr>")
                o.append(f"<tr class='subm'><td colspan='{ncol}'>"
                         f"离场线(SMA5) {_fmt(mr.get('exit_line'))} · 距离场 {dist_txt} · "
                         f"数量 {_fmt_sz(mr.get('qty'))} · 保证金 {_fmt(mr.get('margin'))} U · "
                         f"下一次评估 {_et_iso_local(mr.get('next_eval_et'))}"
                         # 「不会自动离场」只在真的不管理时写（auto_exit=False）；
                         # 单纯是台账来源但 MR 仍启用 → 只是数据还没到，别吓人。
                         + (" · ⚠️ MR 未启用：不会自动离场，需人工处理（离场线仅供参考）"
                            if not mr.get("auto_exit") else
                            (" · 台账读数（下一轮 MR tick 后转为实时）"
                             if mr.get("from_ledger") else ""))
                         # 运行时异常（需人工核对 / 闸门拦下 / 影子模式）：原因写在小字行，
                         # 否则「持仓中」这一行会把「这一轮其实没按预期工作」盖掉。
                         + (f" · ⚠️ {_esc(mr['detail'])}" if mr.get("detail") else "")
                         + "</td></tr>")
        o.append("</tbody></table></div></div>")
        # 图例（可见文字，iOS 无悬停）：ROE 口径收口 + 窄屏小字行的存在说明。
        o.append('<div class="legend">浮盈 % = 浮盈 ÷ 保证金（ROE）· '
                 'QQQ 行标注「（价格）」的 % 是价格涨跌幅（无保证金分母，非 ROE）· '
                 '窄屏隐藏列的数值见各仓下方小字行 · '
                 'QQQ(MR) 腿不加止损，按收盘价与 SMA5 判离场</div>')

    # Kraken 期货没有出入金流水端点（KrInfo.flows_supported=False）→ 这张卡整块
    # 不渲染，连 <h2>盈亏曲线</h2> 也不出现：曲线/净入金/TWR/最大回撤全是流水口径，
    # 缺源时画一个恒空图只会让人以为「还没同步，再等等」。
    if not no_flows:
        # ---------------- 盈亏曲线（已剔除出入金）+ 收益指数 ----------------
        # 页面只有这一张图（旧「权益曲线」已删掉，没有开关、没有视图切换）：
        # 主曲线 = 累计盈亏（U），虚线 = 收益指数 TWR（起点 100，右轴）。
        # 流水读不到时**不画任何曲线**，也不退回权益 —— 那条线正是要删掉的误导源。
        o.append("<h2>盈亏曲线（已剔除出入金）</h2>")
        o.append("<div class='chart-wrap'>")
        if curve_meta.get("flows_ok") is False:
            o.append("<div class='chart-head'><span class='muted'>暂无数据</span></div>")
            o.append(_pnl_svg([], []))
            note = str(curve_meta.get("flows_note")
                       or "无出入金流水源（Kraken 期货无该端点），盈亏曲线不画")
            o.append(f"<div class=\"legend\">{_esc(note)}</div>")
        elif len(shown_pnl) >= 2:
            # 摘要行四个数字**按所选窗口重算**（P1 功能二）：都在同一份曲线序列上切窗，
            # 不另起炉灶 —— span=all 时与 curve.meta 的全期数字逐字相同（首点 pnl=0、
            # 指数=100），两种口径不会打架。
            w_pnl = shown_pnl[-1][1] - shown_pnl[0][1]
            # 峰谷回撤复用 flows 的同一份实现（同一事实一种写法，不复制第二份逻辑）。
            w_dd = hl_flows._max_drawdown([v for _, v in shown_pnl])
            dep_span = _as_dict(curve_meta.get("net_deposit_span"))
            w_dep = (_opt_num(dep_span.get(span)) if span in ("7", "30") else None)
            if w_dep is None:
                w_dep = _opt_num(curve_meta.get("net_deposit")) or 0.0
            if len(shown_twr) >= 2 and shown_twr[0][1] > 0:
                # 区间链式收益：指数末点 ÷ 首点 − 1（窗口内任意一点都是链式乘积，直接相除）。
                w_twr = (shown_twr[-1][1] / shown_twr[0][1] - 1.0) * 100.0
                twr_label = "收益指数（区间）"
            else:
                w_twr = _opt_num(curve_meta.get("twr_total_pct")) or 0.0
                twr_label = "收益指数"
            # 小字一行（iOS 没有悬停，口径必须落在可见文字上）：四个数字一次说完。
            # 统计行里的负值用现有 neg 类标红（净入金为负=净出金）。
            o.append(f"<div class='chart-head'><span>{curve_span}（{len(shown_pnl)} 个快照）</span>"
                     f"<span>累计盈亏 <b class='{'pos' if w_pnl >= 0 else 'neg'}'>"
                     f"{w_pnl:+,.2f} U</b></span>"
                     f"<span>{_span_links(span, str(p.get('admin_k') or ''), int(_num(p.get('refresh'))))}</span></div>")
            o.append(f"<div class=\"legend\">{curve_span}口径：累计盈亏 {_neg_txt(w_pnl, f'{w_pnl:+,.2f}U')} · "
                     f"净入金 {_neg_txt(w_dep, f'{w_dep:,.2f}U')} · 最大回撤 {w_dd:,.2f}U · "
                     f"{twr_label} {_neg_txt(w_twr, f'{w_twr:+.2f}%')}</div>")
            o.append(_pnl_svg(shown_pnl, shown_twr))
            tail = f" · {_SNAPSHOT_RETENTION_NOTE}" if span == "all" else ""
            o.append('<div class="legend">实线=累计盈亏（U，左轴）· '
                     f'虚线=收益指数（起点 100，右轴）{tail}</div>')
        else:
            o.append("<div class='chart-head'><span class='muted'>暂无数据（等执行器落快照）</span></div>")
            o.append(_pnl_svg([], []))
        # 策略回撤（DD Guard）：**任何分支都要有这一行** —— 曲线画不出来（流水坏）时更需要
        # 告诉人「回撤判据现在可不可信」，而「暂不可用」本身就是一句结论。
        o.append(_dd_line_html(curve_meta))
        # 「上线以来」一行（原策略绩效整卡，2026-10-29 用户要求精简并入本区块）。
        o.append(_perf_block_html(p))
    else:
        # 「上线以来」不依赖流水（perf 来自已平仓记录 + account.total_realized）→
        # 保留，单独成一块，不把曲线容器带出来。
        o.append("<h2>策略绩效</h2>")
        o.append(_perf_block_html(p))

    # ---------------- 三标的动态（含 /why 原因行与闸门） ----------------
    # 2026-10-29 用户要求与「仓位」表对换：仓位移到最顶，本块跟在盈亏曲线之后。
    # 距离行 + 原因行 + 闸门异常行都在这一块里 —— 原「为什么没开仓」表格已合并进来。
    # 数据由调用方注入 payload（why 由 admin_ui 现算，symbol_state 来自 DB 缓存），
    # 本函数不做 I/O，逐块兜底：缺哪份数据就只少那几行，绝不弄挂总览页。
    try:
        o.append(_symbol_dynamics(p))
    except Exception as e:                                        # noqa: BLE001
        logger.warning("[hl-views] 三标的动态区块渲染失败：%s", e)


    # ---------------- 挂单（按需渲染，P3 R1）----------------
    # 正常态（每仓恰好一条 reduce-only 触发单、无孤儿、无其它挂单）整块不渲染：
    # 止损已由仓位表「止损 ✔️」列唯一表达（顺带消掉止损价在挂单表的第二次出现）。
    # 异常态 / 判不清（fail-open）才照常渲染完整挂单表。子页 /admin/list/orders 不变。
    if not _orders_normal(positions, orders_open):
        o.append("<h2>交易所挂单</h2>")
        o.append("<div class='tscroll'><div class='tablewrap'><table><thead><tr>"
                 "<th>币种</th><th>类型</th><th>方向</th><th class='num'>数量</th>"
                 "<th class='num'>限价</th><th class='num'>触发价</th><th class='num'>时间</th>"
                 "</tr></thead><tbody>")
        for od in (_as_dict(x) for x in orders_open):
            kind = "止损触发" if od.get("is_trigger") else ("市价/限价" if od.get("limit_px") else "下单")
            if od.get("reduce_only"):
                kind = (kind + " <span class='muted'>(减仓)</span>")
            o.append("<tr>"
                     + _td(_esc(od.get("coin") or "—"))
                     + _td(kind)
                     + _td("买" if od.get("is_buy") else "卖")
                     + _td(_fmt_sz(od.get("sz")), "num")
                     + _td(_fmt(od.get("limit_px")), "num")
                     + _td(_fmt(od.get("trigger_px")), "num")
                     + _td(_fmt_dt(od.get("timestamp")), "num")
                     + "</tr>")
        if not orders_open:
            o.append("<tr><td colspan='7' class='muted'>交易所无挂单</td></tr>")
        o.append("</tbody></table></div></div>")

    # ---------------- 执行器（P3 R2 瘦身）----------------
    # 原「系统」卡收敛：对账/熔断两行删除（顶部状态条徽章是唯一出处）、
    # 卡单移状态条徽章（>0 才出现）—— 卡内不再重复任何一处状态条已有的事实。
    # 只留「执行器最后事件」（判活读数）+ 交易所地址/环境一行 muted。
    # 注：「下单记录」「最近成交」两张表已从总览移除（2026-10-18 用户要求：与「记录」页
    # 的 hl_orders / hl_fills 完全重复 → 去 /admin?tab=records 看）。
    # 更早的「执行器事件」表同理（2026-10-15 移除）。「信号」表也在本次迁到记录页，
    # 渲染函数保留为 _signals_html（记录页复用它，两边长得一样）。
    base_txt = str(net.get("base_url") or "—").replace("https://", "").replace("http://", "")
    o.append("<h2>执行器</h2>")
    o.append("<div class='card'><div class='grid'>")
    o.append(_kv("执行器最后事件", _fmt_dt(system.get("executor_last_event_at"))))
    o.append("</div>")
    o.append(f"<div class='muted'>交易所 {_esc(base_txt)} · "
             f"{'测试网' if net.get('testnet') else '主网'}</div>")
    # P2：原「数据源健康」「已处理到」两张表已整块删除（不是折叠）—— 每标的数据源
    # 状态由「三标的动态」卡在该标的名下的小字行承载（见 _feed_line，2026-10-29 起
    # 正常态不渲染、仅源失败时出现）。
    o.append("</div>")

    return "".join(o)


def _render_footer(cur: int, theme: str = "auto", k: str = "") -> str:
    links = []
    for n, label in ((0, "手动刷新"), (10, "每 10 秒"), (30, "每 30 秒"), (60, "每 60 秒")):
        href = "/hl" if n == 0 else f"/hl?refresh={n}"
        cls = "btn" if n == cur else "btn gray"
        links.append(f"<a class='{cls}' href='{href}'>{label}</a>")
    # 主题切换（黑/白/跟随系统）：与 /admin 面板同一套 cookie 与路由
    theme_row = theme_switch_html("overview", theme, k)
    return ("<div class='foot'><span class='muted'>自动刷新：</span>"
            + " ".join(links)
            + "<span class='spacer'></span>"
            + f"<span class='muted'>本页只读、不持私钥 &middot; schema v{SCHEMA_VERSION}</span></div>"
            + theme_row)


def render_dashboard(payload: dict) -> str:
    """dict → 完整自包含 HTML 字符串（内联 <style>，零外部 CDN/JS）。永不抛异常。"""
    p = payload if isinstance(payload, dict) else {}
    try:
        body = _render_body(p)
    except Exception as e:                                        # noqa: BLE001
        logger.exception("[hl-views] render_dashboard 渲染失败：%s", e)
        body = (f"{H1_BLOCK}"
                f"<div class='banner'>渲染失败：{_esc(e)}</div>")
    refresh = int(_num(p.get("refresh")))
    if refresh not in (10, 30, 60):
        refresh = 0
    meta = f"<meta http-equiv='refresh' content='{refresh}'>" if refresh else ""
    theme = normalize_theme(p.get("theme"))
    attr = theme_attr(theme)
    attr_html = f" data-theme='{attr}'" if attr else ""
    return (
        f"<!doctype html><html lang='zh-CN'{attr_html}><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{ICON_LINKS}"
        f"{meta}<title>{PRODUCT_NAME} · 总览</title>{THEME_CSS}{DASH_CSS}</head>"
        f"<body><div class='wrap'>{body}{_render_footer(refresh, theme, str(p.get('admin_k') or ''))}</div></body></html>")


# ---------------------------------------------------------------- 路由
def _auth_token(deps: DashboardDeps) -> str:
    """面板当前 ADMIN_TOKEN（读不到就空串 = 不鉴权/不种 Cookie）。"""
    try:
        return str(getattr(_call(deps.config_fn), "admin_token", "") or "")
    except Exception:                                            # noqa: BLE001
        return ""


def _admin_required(deps: DashboardDeps, k: str, request: "Request" = None) -> bool:
    """可选鉴权：仅 HL_DASH_REQUIRE_ADMIN=1 时生效（默认关闭，便于内网直连）。

    返回 True = 应拒绝（401）。未开启或未配 ADMIN_TOKEN 一律放行。
    与 main.hl_guard 同口径（app/auth.py）：Bearer 头 / ?k= / cma_admin Cookie
    任一命中即放行 —— 监控统一用 Authorization 头，浏览器会话用 Cookie。
    """
    if os.environ.get("HL_DASH_REQUIRE_ADMIN", "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    token = _auth_token(deps)
    if not token:
        return False
    return not authn.is_admin(request, token, k)


def build_router(deps: DashboardDeps) -> Any:
    """构建监控路由：/hl（HTML）、/api/hl/status、/api/hl/equity、/api/hl/snapshot。"""
    router = APIRouter(tags=["hl"])

    @router.get("/hl", response_class=HTMLResponse)
    def hl_page(request: Request, refresh: int = 0, k: str = "", span: str = "7"):
        if _admin_required(deps, k, request):
            raise HTTPException(401, _AUTH_HINT)
        payload = dict(status_payload(deps))
        payload["refresh"] = refresh if refresh in (10, 30, 60) else 0
        # 盈亏曲线时间窗（P1）：白名单外（含缺参）回落 7，见 normalize_span。
        payload["curve_span"] = normalize_span(span)
        payload["theme"] = normalize_theme(request.cookies.get("cma_theme"))
        # P1-C：页面里不带 token 真值 —— admin_k 恒空（链接与隐藏域都靠 Cookie 鉴权）。
        # 只用 ?k= 的第一跳由这次响应的 Set-Cookie 转成会话，后续点击走 Cookie。
        payload["admin_k"] = ""
        resp = HTMLResponse(render_dashboard(payload))
        return authn.set_session_cookie(resp, request, _auth_token(deps), k or None)

    @router.get("/api/hl/status")
    def hl_status(request: Request, k: str = "", curve: int = 0, live: int = 0):
        """只读状态 JSON。

        - `curve=0`（默认）：不展开 30 天逐点 `curve.pnl`/`curve.twr`（省 ~440KB），保留 `curve.meta`；
          传 `?curve=1` 返回完整点列。
        - `live=0`（默认）：若注入了 `snapshot_info_fn` 则优先读面板快照（缺失/过期自动回落实时），
          省去每轮实连 Kraken 的 ~1s 延迟；传 `?live=1` 强制走实时客户端。
        """
        if _admin_required(deps, k, request):
            raise HTTPException(401, _AUTH_HINT)
        eff_deps = deps
        if not live and deps.snapshot_info_fn is not None:
            sinfo = _call(deps.snapshot_info_fn)
            if sinfo is not None:
                eff_deps = replace(deps, info=sinfo)
        return status_payload(eff_deps, include_curve_points=bool(curve))

    @router.get("/api/hl/equity")
    def hl_equity(request: Request, k: str = "", curve: int = 0, live: int = 0):
        """只读权益 JSON（与 /api/hl/status 同一套瘦身口径，2026-11-07）。

        - `curve=0`（默认）：不展开 30 天逐点 `curve.pnl`/`curve.twr`（省 ~410KB）；
          传 `?curve=1` 返回完整点列（要画图才传）。
        - `live=0`（默认）：若注入了 `snapshot_info_fn` 则优先读面板快照（缺失/过期
          自动回落实时），省去每轮实连 Kraken 的 ~1s 延迟；`?live=1` 强制实时。
        - 当前无已知外部消费方（trade-hub / 脚本 / cron 均不读它），参数保持向后兼容。
        """
        if _admin_required(deps, k, request):
            raise HTTPException(401, _AUTH_HINT)
        eff_deps = deps
        if not live and deps.snapshot_info_fn is not None:
            sinfo = _call(deps.snapshot_info_fn)
            if sinfo is not None:
                eff_deps = replace(deps, info=sinfo)
        p = status_payload(eff_deps, include_curve_points=bool(curve))
        a = p.get("account") or {}
        return {
            "ts": p.get("ts"),
            "mode": p.get("mode"),
            "kill": p.get("kill"),
            "reachable": bool((p.get("network") or {}).get("reachable")),
            "equity": a.get("equity"),
            "unrealized_pnl": a.get("unrealized_pnl"),
            "today_realized": a.get("today_realized"),
            "total_realized": a.get("total_realized"),
            "fees_total": a.get("fees_total"),
            "funding_total": a.get("funding_total"),
            "positions": p.get("positions") or [],
            # 盈亏曲线（已剔除出入金）+ TWR 指数：与面板同一份 payload，
            # 老的原始权益曲线已删除（不是开关），调用方要画图只读这个。
            "curve": p.get("curve") or {},
        }

    @router.get("/api/hl/why")
    def hl_why(request: Request, k: str = ""):
        """`/why` 决策解释（JSON，只读）：每标的一行六步 + 闸门状态。

        与面板同一份数据（`_why_rows` → `app.why.build_rows`），方便运维用 curl 看
        「现在为什么不开仓」，而不必去翻 HTML。只读，无任何副作用。
        """
        if _admin_required(deps, k, request):
            raise HTTPException(401, _AUTH_HINT)
        rows = _why_rows(deps)
        return {"ts": _num(_call(deps.now_fn), time.time()), "symbols": rows}

    @router.get("/api/hl/snapshot")
    def hl_snapshot(request: Request, k: str = ""):
        if _admin_required(deps, k, request):
            raise HTTPException(401, _AUTH_HINT)
        p = status_payload(deps)
        return {
            "ts": p.get("ts"),
            "network": p.get("network") or {},
            "account": p.get("account") or {},
            "positions": p.get("positions") or [],
            "orders_open": p.get("orders_open") or [],
        }

    return router
