"""执行器看守检查（Kraken 版；供 Hermes cron 每 5 分钟调用，异常才输出）。

与 cma_watch.py 分工：那个看「提醒服务」，这个看「执行器 + 交易所侧状态」。
本模块只读：不写库（除 kill switch 的提示不做任何状态变更）、不下单。

用法：
    python -m app.hl.watchdog --dry-run     # 打印所有检查项（含正常的）
    python -m app.hl.watchdog               # 只在有问题时输出（cron 静默模式）
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
import urllib.request
from email.utils import parsedate_to_datetime
from typing import List, Optional

from app.hl import MODE_OFF, EV_HEARTBEAT
from app.hl.info import HlInfoError


def _brief(msg: object, limit: int = 120) -> str:
    """异常/错误文本摘要：压掉换行与多余空白，超长截断加省略号。"""
    s = " ".join(str(msg).split())
    return s if len(s) <= limit else s[:limit] + "…"

STUCK_ORDER_S = 180.0
HEARTBEAT_MAX_S = 15 * 60
CLOCK_SKEW_MAX_S = 2.0
AGENT_WARN_DAYS = 3.0        # agent 授权剩余天数低于此值就告警（到期=无法下单）


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def check_clock_skew(base_url: str = "https://futures.kraken.com",
                     timeout: float = 10.0) -> Optional[float]:
    """用响应头 Date 估服务器时间偏差（秒）。取不到返回 None。

    Kraken（PR-12 去 HL 残留）：原来 POST `api.hyperliquid.xyz/info`，Kraken 部署下
    这条等于在量 HL 的时间。改用 Kraken **公开** GET（instruments，无需签名）——
    时钟偏差仍然有意义：Kraken 的签名请求带时间戳，本机跑快/跑慢会让请求时间戳对不齐，
    偏差过大就可能被拒。
    """
    try:
        req = urllib.request.Request(
            base_url.rstrip("/") + "/derivatives/api/v3/instruments",
            headers={"User-Agent": "cma-watchdog"}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            date_hdr = resp.headers.get("Date")
            resp.read()
        if not date_hdr:
            return None
        server = parsedate_to_datetime(date_hdr).timestamp()
        return time.time() - server
    except Exception:                                                # noqa: BLE001
        return None


def run_checks(db, cfg=None, info=None, mode: str = MODE_OFF, force: bool = False) -> List[str]:
    """返回问题列表（空 = 一切正常）。`force` 时连正常项也以「· 」前缀给出。"""
    notes: List[str] = []

    def add(msg: str, ok: bool = False) -> None:
        if ok and not force:
            return
        notes.append(("· " if ok else "⚠️ ") + msg)

    if mode == MODE_OFF:
        add("执行器处于 off 模式（只提醒）", ok=True)
        return notes

    # 1) 心跳
    last = db.hl_last_event_at(EV_HEARTBEAT)
    if last is None:
        add("执行器从未写过心跳（检查调度器/execute 钩子）")
    else:
        age = time.time() - last
        if age <= HEARTBEAT_MAX_S:
            add(f"执行器心跳 {age / 60:.1f} 分钟前", ok=True)
        else:
            add(f"执行器心跳超期 {age / 60:.1f} 分钟（应 ≤15 分钟）")

    # 2) kill switch
    if db.hl_kill():
        reason = db.get_config_str("hl_exec_kill_reason")
        add(f"kill switch 处于触发状态（{reason}）—— 自动交易已暂停，仅允许人工恢复")
    else:
        add("kill switch 未触发", ok=True)

    # 3) 卡住的意图单（可能已发出但结果未知）
    stuck = db.hl_stuck_orders(STUCK_ORDER_S)
    if stuck:
        detail = "；".join(f"#{r['id']} {r['symbol']}/{r['purpose']}({r['status']})" for r in stuck[:5])
        add(f"有 {len(stuck)} 条卡住的意图单（>3 分钟未回结果）：{detail}")

    # 4) 交易所侧：仓位与止损。
    #    审计三轮 L：地址缺失只跳过交易所侧（4/7/8），不提前 return ——
    #    后面的 24h 失败单、时钟偏差检查与交易所无关，必须照常执行。
    # 环境变量名沿用 HL_ACCOUNT_ADDRESS（部署侧已存在，改名=改 compose，收益为零）；
    # Kraken 下它只是 main 填的占位常量， KrInfo 的全部方法都忽略这个参数。
    # Kraken（`venue_address_free`）：**没有地址这个概念**，所以这里不许拿「环境变量为空」
    # 当「配置缺失」—— 那会在每次 cron 里刷一条假告警，并且把仓位/止损检查整块跳过
    # （实盘无看守）。判据按 venue 能力，不按 env 有没有填。
    address = _env("HL_ACCOUNT_ADDRESS")
    address_free = bool(getattr(info, "venue_address_free", False))
    if info is None or (not address and not address_free):
        add("未配置 HL_ACCOUNT_ADDRESS，跳过交易所侧检查")
    snap = None
    if info is not None and (address or address_free):
        try:
            snap = info.snapshot(address)
        except HlInfoError as e:
            add(f"读不到 Kraken 账户：{e}")
    if snap is not None:
        ex_pos = {p["coin"]: p for p in snap.get("positions") or []}
        orders_trusted = bool(snap.get("orders_trusted", False))
        for p in ex_pos.values():
            if not orders_trusted:
                # 挂单接口回落（读不到 isTrigger）：与执行器同口径 —— 只提示、不判裸奔
                add(f"{p['coin']} 仓位 {p['size']} @ {p['entry_px']:,.2f}，止损状态无法确认（挂单接口不可信）")
                continue
            has_stop = any(o.get("coin") == p["coin"] and o.get("reduce_only") and o.get("is_trigger")
                           for o in snap.get("open_orders") or [])
            if not has_stop:
                add(f"{p['coin']} 有仓位但没有止损挂单（裸奔仓位，请立刻处理）")
            else:
                add(f"{p['coin']} 仓位 {p['size']} @ {p['entry_px']:,.2f}，止损单在场", ok=True)
        for r in db.recent_positions(20):
            if (r["hl_status"] or "") == "mismatch":
                add(f"仓位 #{r['id']} {r['symbol']} 处于 mismatch（本地与交易所不一致）")
        # L4（对抗审查）：closing（平仓成交待核对）以前**两头都不报** —— mismatch 分行
        # 不收它，下面的「本地有仓但交易所查不到」只认 hl_status='open'，于是宽限期里
        # 的仓在看守报告里完全消失（用户以为没仓，其实账还挂着）。它是**已知的、自动
        # 重试中的**状态，不是异常：按 ok 行报一行，`--dry-run` 能看见，cron 静默不喊。
        for r in db.recent_positions(20):
            if (r["hl_status"] or "") == "closing" and r["status"] in ("open", "pending"):
                add(f"仓位 #{r['id']} {r['symbol']} 平仓待核对（宽限期自动重试中，暂不开新仓）",
                    ok=True)
        local_open = [r for r in db.recent_positions(20)
                      if r["status"] == "open" and (r["hl_status"] or "") == "open"]
        for r in local_open:
            coin = r["symbol"].split("_")[0]
            if coin not in ex_pos:
                add(f"本地显示 {r['symbol']} 持仓中（#{r['id']}），但交易所查不到该仓位")

    # 5) 近 24h 失败单
    since = time.time() - 86400
    failed = [r for r in db.hl_recent_orders(100)
              if r["status"] == "failed" and float(r["created_at"]) >= since]
    if failed:
        last_err = _brief(failed[0]["error"] or "无错误信息", 160)
        add(f"近 24h 有 {len(failed)} 条下单失败（最近：{last_err}）")
    else:
        add("近 24h 无失败下单", ok=True)

    # 6) 时钟偏差（nonce 依赖）
    skew = check_clock_skew()
    if skew is None:
        add("时钟偏差检查失败（拿不到服务器时间）")
    elif abs(skew) > CLOCK_SKEW_MAX_S:
        add(f"本机与 Kraken 服务器时间偏差 {skew:+.1f} 秒（>2s 会影响请求时间戳对齐，"
            f"签名请求可能被拒）")
    else:
        add(f"时钟偏差 {skew:+.2f} 秒", ok=True)

    # 7) 权益（Kraken 期货：只有一个衍生品钱包，不拆现货/合约）—— snap 可能因地址缺失/读取失败为 None
    if snap is not None and snap.get("unified") and address_free:
        # Kraken 的账本里没有「现货侧」这回事：equity 就是衍生品钱包总值，
        # 写「现货 X + 浮动盈亏」是在描述一个不存在的结构（审核 C4）。
        add(f"Kraken 衍生品账户权益 {snap.get('equity_total', 0) or snap.get('equity', 0):,.2f} USDC"
            f"（可用 {snap.get('withdrawable', 0):,.2f} · 已用保证金 {snap.get('margin_used', 0):,.2f}）",
            ok=True)
    elif snap is not None and snap.get("unified"):
        # 文案与面板同口径：用人话写「现货 X + 浮动盈亏」，不用行业口径术语
        # （用户 2026-10-15 明确反馈那类词看不懂）
        add(f"账户总值 {snap.get('equity_total', 0):,.2f} USDC"
            f"（统一账户：现货 {snap.get('spot_usdc', 0):,.2f} + 浮动盈亏）", ok=True)
    elif snap is not None:
        add(f"账户权益 {snap.get('equity', 0):,.2f} USDC（可用 {snap.get('withdrawable', 0):,.2f}）",
            ok=True)

    # 8) agent（API 钱包）授权到期 —— HL 时代的概念（代理钱包有到期时间）。
    #    Kraken 的 API key 没有到期、也没有代理钱包。KrInfo **实现了** agent_status，
    #    但返回的是 {"kind":"kraken-api","expires":None,"days_left":None}（无 agents /
    #    max_valid_until）—— 按「有没有这个方法」判会漏，反而把「无 agent」当故障误报。
    #    判据按 venue 能力：kind 是 kraken-api，或既无 agents 也无到期字段 → 不适用，
    #    跳过（这是「不适用」，不是「读失败」，不许报成告警）。HL 形状照旧查到期。
    #    这里的门槛与第 4 步同一条（venue_address_free 时不需要地址）。
    if info is None or (not address and not address_free):
        return notes
    ag_fn = getattr(info, "agent_status", None)
    if not callable(ag_fn):
        add("Kraken API key 无到期概念（也没有代理钱包），跳过 agent 授权检查", ok=True)
        return notes
    try:
        ag = ag_fn(address) or {}
        kraken_no_expiry = (str(ag.get("kind") or "") == "kraken-api"
                            or ("agents" not in ag and "max_valid_until" not in ag))
        if kraken_no_expiry:
            add("Kraken API key 无到期概念（也没有代理钱包），跳过 agent 授权检查", ok=True)
            return notes
        agents = ag.get("agents") or []
        until = ag.get("max_valid_until")
        if not agents:
            add("账户没有任何 agent（API 钱包）授权 —— 下单会被拒，"
                "请到交易所设置里生成 API 钱包并绑定主账户（仅 HL 时代部署需要）")
        elif until:
            days_left = (float(until) / 1000.0 - time.time()) / 86400.0
            names = "、".join(str(a.get("name") or str(a.get("address"))[:10]) for a in agents[:3])
            if days_left <= AGENT_WARN_DAYS:
                add(f"agent 授权只剩 {days_left:.1f} 天（{names}）"
                    f"—— 到期后自动交易会全部失败，请重新生成/延长 API 钱包（仅 HL 时代部署）")
            else:
                add(f"agent 授权 {len(agents)} 个（{names}），最长还有 {days_left:.0f} 天", ok=True)
        else:
            add(f"agent 授权 {len(agents)} 个，均无到期时间", ok=True)
    except Exception as e:                                           # noqa: BLE001
        add(f"读不到 agent 授权状态：{e}")
    return notes


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="打印全部检查项")
    ap.add_argument("--mode", default=None,
                    help="执行模式（默认从 DB 读，与面板/执行器同一口径；env HL_EXEC_MODE 只是兜底）")
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "/app/data/crypto_ma.db"))
    args = ap.parse_args(argv)

    from app.db import DB
    from app.kr.info import KrInfo

    # Kraken 端点（PR-12 去 HL 残留：原来这里 new 的是 HlInfo 连 Hyperliquid）。
    # 与 main.hl_info_base() 同口径：HL_TESTNET 复用为沙盒开关。
    base = ("https://demo-futures.kraken.com"
            if _env("HL_TESTNET", "0") in ("1", "true")
            else "https://futures.kraken.com")
    db = DB(args.db)
    # 模式判定必须与 main.hl_mode() 同口径：DB > env > off。
    # 只读 env 的话，面板切到 live 而 env 还是 dry/off 时，看守会在错误的模式下自检
    # （off 直接跳过全部检查 = 实盘无看守）。
    mode = args.mode
    if mode is None:
        raw = str(db.get_config().get("hl_exec_mode", "") or _env("HL_EXEC_MODE", MODE_OFF))
        mode = raw.strip().strip('"')
        if mode not in (MODE_OFF, "dry", "live"):
            mode = MODE_OFF
    # 凭据：DB（面板「更新 API」写入）优先，其次 env —— 与 main.kr_api_key/secret 同序。
    # 只读消费，缺凭据时公开端点仍可用、私有读数 fail-closed（KrInfo 自身语义）。
    api_key = (str(db.get_config_str("kr_api_key", "") or "").strip()
               or _env("KRAKEN_API_KEY"))
    secret = (str(db.get_config_str("kr_api_secret", "") or "").strip()
              or _env("KRAKEN_API_SECRET"))
    info = KrInfo(api_key=api_key, secret_b64=secret, base_url=base)
    problems = run_checks(db, info=info, mode=mode, force=args.dry_run)
    bad = [p for p in problems if p.startswith("⚠️")]
    if args.dry_run:
        print(f"[dry-run] {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M UTC} 模式={mode}")
        for p in problems:
            print(" ", p)
        return 0
    if bad:
        print("⚠️ Kraken 执行器看守告警")
        for p in bad:
            body = p[len("⚠️ "):] if p.startswith("⚠️ ") else p
            print("•", body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
