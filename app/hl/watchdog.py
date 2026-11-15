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
from typing import List, Optional

from app.hl import MODE_OFF, MR_LEG_COINS, EV_HEARTBEAT
from app.hl.info import HlInfoError
from app.hl.stops import has_covering_stop


def _brief(msg: object, limit: int = 120) -> str:
    """异常/错误文本摘要：压掉换行与多余空白，超长截断加省略号。"""
    s = " ".join(str(msg).split())
    return s if len(s) <= limit else s[:limit] + "…"

STUCK_ORDER_S = 180.0
HEARTBEAT_MAX_S = 15 * 60
AGENT_WARN_DAYS = 3.0        # agent 授权剩余天数低于此值就告警（到期=无法下单）


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _f(v, default: float = 0.0) -> float:
    """宽松转 float（NaN/inf/脏值 → default）：文案格式化不能因一个空值整段崩掉。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if x == x and x not in (float("inf"), float("-inf")) else default


def _coin_of(symbol) -> Optional[str]:
    """signal symbol → display coin（env `HL_COIN_MAP` 覆盖 → 注册表）。

    与 `app.market.hl_coin()` 同一条解析链：**不做 `split("_")[0]` 兜底** ——
    `1000PEPE_USDT` 那样会拼出一个并不存在的合约名，于是「本地有仓/交易所有仓」
    两边对不上，看守要么误报要么漏报。解析不出返回 None（调用方按「无法对账」处理）。
    """
    try:
        from app.market import hl_coin
        return hl_coin(str(symbol))
    except Exception:                                            # noqa: BLE001
        return None


def _sz_ticks(info, coins) -> dict:
    """{coin: 1 个数量 tick}；拿不到就留空（由共享判据退化成严格相对容差）。

    `KrInfo.specs()` 有 1 小时缓存，正常只读进程内存；**失败不兜一个猜的精度**：
    猜错 100 倍会让「张数够不够」两种结论都不可信（见 app/hl/stops.py 的数值纪律）。
    """
    out: dict = {}
    fn = getattr(info, "specs", None)
    if not callable(fn):
        return out
    try:
        specs = dict(fn(list(coins)) or {})
    except Exception:                                            # noqa: BLE001
        return out
    for c in coins:
        try:
            dec = (specs.get(c) or {}).get("sz_decimals")
            if dec is not None:
                out[c] = 10.0 ** -int(dec)
        except (TypeError, ValueError, AttributeError):
            continue
    return out


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
        ticks = _sz_ticks(info, ex_pos.keys())
        for p in ex_pos.values():
            coin = str(p.get("coin"))
            if coin in MR_LEG_COINS:
                # MR 腿（QQQ 永续）设计上不挂交易所止损：退出由策略次日平仓负责。
                # 算进「裸奔仓位」会让看守在每次 MR 持仓期误报，真裸仓会被噪声淹没。
                add(f"{coin} 是 MR 腿持仓：策略设计不挂交易所止损，跳过止损检查", ok=True)
                continue
            if not orders_trusted:
                # 挂单接口回落（读不到 isTrigger）：与执行器同口径 —— 只提示、不判裸奔
                add(f"{coin} 仓位 {p.get('size')} @ {_f(p.get('entry_px')):,.2f}，"
                    f"止损状态无法确认（挂单接口不可信）")
                continue
            # 止损判据**只有一份**（app/hl/stops.py::covering_stop_px，执行器/面板/探针
            # 共用）：同 coin + reduce_only + is_trigger + 非止盈单 + trigger_px>0 +
            # 方向相反 + 触发价在亏损侧 + 张数 ≥ 持仓−半 tick。
            # 旧版只看前三项 → 盈利侧止盈单、只有 1 tick 的触发单都算「止损在场」，
            # 真裸仓被漏报（2026-11-15 第十一轮复审实测）。
            size = _f(p.get("size"))
            covered = has_covering_stop(
                snap.get("open_orders") or [], coin,
                side=int(p.get("side") or 0) or None,
                sz=(size if size > 0 else None),
                entry_px=(_f(p.get("entry_px")) or None),
                sz_tick=ticks.get(coin))
            if not covered:
                add(f"{coin} 有仓位但没有止损挂单（或现有挂单覆盖不足）——裸奔仓位，请立刻处理")
            else:
                add(f"{coin} 仓位 {p.get('size')} @ {_f(p.get('entry_px')):,.2f}，"
                    f"止损单在场（覆盖量足够）", ok=True)
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
            coin = _coin_of(r["symbol"])
            if coin is None:
                add(f"本地显示 {r['symbol']} 持仓中（#{r['id']}），但该标的没有合约映射，无法与交易所对账")
                continue
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

    # 6) 时钟偏差：**没有这一项**（2026-11-15 第十一轮）。
    #    本仓 Kraken v3 的签名是 `base64(HMAC-SHA512(secret, sha256(payload + "/api/v3/" + method)))`，
    #    头里只有 APIKey/Authent —— 没有 nonce、也没有时间戳（见 app/kr/auth.py）。
    #    因此「本机与交易所时间差 >2s 会让签名被拒」是 HL 时代的事实，在这里不成立；
    #    留着只会在反代时钟抖动时推一条可证伪的假告警。真正的时钟问题（容器时钟漂移）
    #    由平台/NTP 负责，且会先表现在「数据陈旧」这类判据上。

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
