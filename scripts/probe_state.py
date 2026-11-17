#!/usr/bin/env python3
"""Kraken 双均线状态探针（oracle1 本地，monitor.py 每 2 分钟调用）。

替代原来的 HL+Lighter 双所探针（2026-11-08 迁移 Kraken）。全部本地读取：
docker / sqlite / 127.0.0.1:8140 面板。输出必须逐字节稳定（monitor 按 sha256 判变化）：
只放会因成交/故障而变的量，健康类一律布尔/稳定枚举，不带浮点权益（每轮都变）。

用法：python3 probe_state.py → stdout 一份 JSON
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

DIR = "/opt/dual-ma"
CONTAINER = "kraken-ma-bot"
DB = "/var/lib/docker/volumes/kraken-ma-bot_kr_data/_data/crypto_ma.db"
LOCAL_STATUS = "http://127.0.0.1:8140/api/hl/status"
PANEL_HEALTH = "https://kma.222677.xyz/health"
EGRESS_URL = "https://api.ipify.org"
STOP_MIN, STOP_MAX = 0.015, 0.10
STALE_BAR_H = 6                      # 4h 周期：最后一根已收盘 bar 距今超过 6h 视为数据源异常
MR_COINS = {"QQQ"}                   # MR 腿（QQQ 永续）：策略设计上不挂止损，不参与止损覆盖检查
HB_OK_S = 15 * 60                    # 执行器心跳（hl_events.kind='heartbeat'）新鲜度门槛
HB_DEAD_S = 60 * 60
# 止损判据常量：**必须与执行器 app/hl/executor.py 的 `_has_stop` 同源同严**
# （止盈单排除 / trigger_px>0 / 方向相反 / 张数够 / 触发价在亏损侧）。
# 数量 1 tick 来自 Kraken `/instruments` 的 `contractValueTradePrecision`
# （XBT=4 / ETH=3 / SOL=2 / QQQX=3，见 KrInfo.specs）。探针不外呼 Kraken，
# `/api/hl/status` 也不带合约规格，所以只能按这张表；**未登记的币种只判 unknown**，
# 绝不拿猜出来的精度判「裸仓」。
SZ_TICK = {"BTC": 0.0001, "ETH": 0.001, "SOL": 0.01, "QQQ": 0.001}
# 与 executor `_TAKE_PROFIT_TYPES` 同一个列表（类型缺失时不当作止盈，四道数值判据兜底）。
TAKE_PROFIT_TYPES = ("take_profit", "take-profit", "takeprofit", "tp")
BAR_MS = 4 * 3600 * 1000             # bar 周期（4h），last_bar_ms 是开盘时间
FAIL_WORDS = ("error", "fail", "reject", "mismatch", "missing", "void",
              "unfilled", "stuck", "timeout", "invalid", "corrupt", "drift",
              "lost", "naked", "panic")
_ABSENT = object()                   # 区分「面板没给这个字段」与「显式给了 None」


def sh(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip(), p.returncode
    except Exception as e:                                        # noqa: BLE001
        return f"{type(e).__name__}: {e}", 1


UA = "dual-ma-probe/3"


def get_json(url, tk=None, timeout=12):
    """GET 面板 status。ADMIN_TOKEN 走 Authorization header，绝不放进 URL。

    token 进 query 会被面板 access log 原样记下（P1-C：最近 60 行容器日志里有 18 行
    带 `?k=`），再随 triage 进 .alert_pending 和值班 agent 的上下文。header 不进
    access log；对应地，**探针必须在新面板支持 Bearer 之后才能上线**，否则会 401。
    """
    headers = {"User-Agent": UA}
    if tk:
        headers["Authorization"] = "Bearer " + tk
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_code(url, tries=2):
    for _ in range(max(1, tries)):
        out, rc = sh(f"curl -s -o /dev/null -m 10 -w '%{{http_code}}' {url!r}")
        if rc == 0 and out.isdigit() and out != "000":
            return int(out)
        time.sleep(1)
    return 0


def sql_json(query, timeout=20):
    """sqlite3 -json 查询；**失败必须抛 RuntimeError**，绝不静默降级。

    旧版 rc!=0 时返回 None、调用处 `if rows:` 直接跳过 → 「DB 读失败」被伪装成
    「无事件」（events_max 恒 0、bar_stale 恒 None），实测引发快照 flap
    （正常↔降级交替 12 次，每次写一条假 pending）。现在让异常穿出 kraken()/main()
    使进程 rc!=0，交给 monitor 已有的「重试 1 次 → 连续 3 次才判不可达」管线。
    `-cmd '.timeout 5000'`：等锁 5 秒而不是立刻报 "database is locked"。
    空结果集（rc==0 且无输出）不是错误，返回 []。
    """
    cmd = f"sqlite3 -json -cmd '.timeout 5000' {DB} {query!r}"
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("sqlite3 timeout %ss [query: %s]" % (timeout, query[:80])) from None
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        # sh() 丢 stderr 才让「DB 读失败」看起来像「无事件」；这里自己拿全三项。
        lines = [ln for ln in ((p.stderr or p.stdout or "").strip().splitlines()) if ln]
        err = " | ".join(lines[-3:])[:300]
        raise RuntimeError("sqlite3 rc=%d: %s [query: %s]"
                           % (p.returncode, err or "no stderr", query[:80]))
    if not out:
        return []
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError("sqlite3 output not JSON: %r [query: %s]"
                           % (out[:200], query[:80])) from None


def token():
    out, _ = sh(f"docker inspect {CONTAINER} --format " +
                "'{{range .Config.Env}}{{println .}}{{end}}' | grep ^ADMIN_TOKEN= | cut -d= -f2")
    return out.strip()


def containers():
    out, _ = sh("docker ps -a --filter name=kraken-ma-bot --format '{{.Names}} {{.State}}'")
    state = "missing"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == CONTAINER:
            state = parts[1]
    return {CONTAINER: state}


def _risk_code(side, entry, stop):
    try:
        e, s = float(entry), float(stop)
    except (TypeError, ValueError):
        return "no_stop"
    if e <= 0 or s <= 0:
        return "no_stop"
    try:
        sd = int(side)
    except (TypeError, ValueError):
        return "bad_side"
    if sd > 0 and s >= e:
        return "stop_side"
    if sd < 0 and s <= e:
        return "stop_side"
    dist = abs(s - e) / e
    if dist < STOP_MIN or dist > STOP_MAX:
        return "stop_dist"
    return None


def orders_trust(d):
    """面板顶层 `orders_trusted` 的三态解释：(bool 可信, 稳定标签)。

    跨组契约：新面板显式给布尔 true 才算「挂单表可信」；旧面板没有这个字段，
    一律按 false 处理 —— 过渡期只报 unknown，绝不报 stops_bad。

    除布尔 True 之外的一切值（false / null / "true" / 1）都按不可信处理：宁可只报
    unknown，也不能因为一次宽松的 truthy 判断，让值班员照着「裸仓」给一张其实还在
    线上的止损再补一张（P1-A，重复止损会在旧仓平掉后咬住下一笔仓）。
    """
    raw = d.get("orders_trusted", _ABSENT)
    if raw is True:
        return True, "true"
    if raw is _ABSENT:
        return False, "missing"
    return False, "false"


def _fnum(v):
    """宽松转 float；脏值/缺失返回 None（调用方据此判「判不了」，不猜 0）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def stop_coverage(pos, orders, trusted):
    """返回 (stops_bad, stops_unknown)，均排好序 —— 快照必须逐字节稳定。

    判据与执行器 `app/hl/executor.py::_has_stop`（PR-16 / 四轮 P2-C）同源：
    某个持仓算「有止损」，必须存在一条挂单同时满足
      ① 同一 coin、reduce_only、is_trigger；
      ② order_type 不是 take_profit/tp（止盈单不是止损）；
      ③ trigger_px > 0（0/NULL 的触发单等于市价单，不是有效保护）；
      ④ 方向相反：多仓（side=1）要卖单（is_buy=False），空仓（side=-1）要买单；
      ⑤ 触发价在**亏损侧**：多仓 trigger < entry_px，空仓 trigger > entry_px；
      ⑥ 张数 ≥ 持仓 − 半 tick（tick 见 SZ_TICK）。
    旧实现只判 ①+④，比执行器宽松 → 只有止盈单 / 反向 0.0001 的假单 / 量不足的
    部分成交残留都能让裸仓「通过检查」，值班员看不到真裸仓（五轮 P2-③）。

    三态分流（宁可 unknown，绝不制造假 stops_bad —— 误报裸仓会惊动值班）：
    - trusted=False（挂单表不可信）→ unknown；
    - 判据所需输入缺失/脏 → unknown：side 不是 ±1、entry_px ≤ 0（判不了亏损侧）、
      size ≤ 0、coin 没登记 tick（判不了张数）；
    - 输入齐全且没有一条单能同时满足①~⑥ → bad（真裸仓）。
    MR 腿（QQQ）策略上不挂交易所止损，两边都不进；否则每个 MR 持仓日都误报，
    真裸仓会被噪声淹没，值班员还会给它补一张策略里不存在的单。
    """
    bad, unknown = [], []
    for p in pos or []:
        coin = str(p.get("coin"))
        if coin in MR_COINS:
            continue
        if not trusted:
            unknown.append(coin)
            continue
        try:
            side = int(p.get("side"))
        except (TypeError, ValueError):
            side = 0
        entry = _fnum(p.get("entry_px")) or 0.0
        size = _fnum(p.get("size")) or 0.0
        tick = SZ_TICK.get(coin)
        if side not in (1, -1) or entry <= 0 or size <= 0 or not tick:
            # 方向/入场价/持仓量/精度任一缺失 → 判不了，只报 unknown。
            unknown.append(coin)
            continue
        want_buy = side < 0                     # 空仓的止损 = 买入
        covered = False
        undecided = False
        for o in orders or []:
            if str(o.get("coin")) != coin:
                continue
            if not o.get("reduce_only") or not o.get("is_trigger"):
                continue
            # ②止盈排除。注意：/api/hl/status 的 orders_open 目前**不带 order_type**
            # （app/hl/views.py 构造处只挑 coin/oid/is_buy/sz/limit_px/reduce_only/
            # trigger_px/is_trigger/timestamp），所以这一条现在恒不命中；面板补字段后
            # 这里自动生效。残余缺口只有「触发价落在亏损侧的止盈单」，④⑤⑥ 已覆盖
            # 全部「盈利侧止盈单冒充止损」的情形。
            if str(o.get("order_type") or "").strip().lower() in TAKE_PROFIT_TYPES:
                continue
            trig = _fnum(o.get("trigger_px")) or 0.0
            if trig <= 0:                       # ③
                continue
            if side > 0 and not trig < entry:    # ⑤ 多仓止损必须在入场价下方
                continue
            if side < 0 and not trig > entry:    # ⑤ 空仓在上方
                continue
            if bool(o.get("is_buy")) != want_buy:    # ④
                continue
            osz = _fnum(o.get("sz"))
            if osz is None:
                # 挂单数量字段缺失/脏（面板构造回归、降级适配层）→ 判不了量是否够，
                # 记 undecided，最终落 stops_unknown 而不是假裸仓告警。
                undecided = True
                continue
            # 与 app.hl.stops 同口径：半 tick 只吸收浮点误差，不能漏掉整 tick。
            if size - osz > tick * 0.5 + 1e-12 * max(1.0, abs(size)):
                continue
            covered = True
            break
        if not covered:
            (unknown if undecided else bad).append(coin)
    return sorted(bad), sorted(unknown)


# ---- 权益分档（~10% 档宽自适应 + 迟滞）-----------------------------------
# 只用于发现入金/提现/大亏这类量级变化，不是精确权益。旧实现 `int(eq//50)*50`
# 两个问题：档宽固定 50U（权益 1 万时仅 0.5%，日常浮盈亏天天跨档）；档位边界
# 上任何微小波动都整档翻转（2026-11-17 实例：500.47→499.97 标签 500→450 误报）。
EQ_BUCKET_STATE = DIR + "/.eq_bucket.json"


def _nice_step(x: float) -> int:
    """把 ≈0.1×权益 的目标档宽吸附到 1-2-5×10^k，保证标签是整数且逐字节稳定。"""
    if x <= 0:
        return 50
    cands, e = [], 1
    while e <= x * 20 + 100:
        for m in (1, 2, 5):
            cands.append(m * e)
        e *= 10
    return min(cands, key=lambda c: (abs(c - x), -c))


def _eq_bucket_prev():
    """读上次档位；缺失/损坏返回 None，退化为无迟滞的普通分档（不影响其他字段）。"""
    try:
        with open(EQ_BUCKET_STATE, "r", encoding="utf-8") as f:
            d = json.loads(f.read() or "{}")
        b, s = float(d.get("bucket") or 0), float(d.get("step") or 0)
        if b > 0 and s > 0:
            return b, s
    except Exception:
        pass
    return None


def _eq_bucket_save(b, s):
    try:
        tmp = EQ_BUCKET_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"bucket": b, "step": s}))
        os.replace(tmp, EQ_BUCKET_STATE)
    except Exception:
        pass  # 写失败只丢迟滞，下一轮重新分档


def equity_bucket(eq: float) -> int:
    """权益分档：档宽 ≈ 权益 10%（随规模自适应，下限 50U），标签取最近档。

    迟滞：权益仍在上次档位 ±1 个档宽内时维持原档，防止档位边界随浮盈亏
    反复翻档（2026-11-17 实例：500.47→499.97 整档翻转误报）。
    """
    if eq <= 0:
        return 0
    prev = _eq_bucket_prev()
    if prev:
        b, s = prev
        if b - s <= eq < b + s:
            return int(b)
    step = max(50, _nice_step(eq * 0.1))
    b = int(eq / step + 0.5) * step
    _eq_bucket_save(b, step)
    return int(b)


def kraken():
    """Kraken 腿 + MR 腿的统一读数（来源：面板 /api/hl/status + DB）。"""
    out = {"mode": "unknown", "kill": None, "open": 0, "positions": [],
           "stops_bad": [], "stops_unknown": [], "orders_trusted": "unknown",
           "api_key": "unknown", "reachable": None,
           "orders": 0, "bar_stale": None, "risk_bad": [], "mr": "unknown",
           "executor_hb": "unknown",
           "events_max": 0, "unknown_fail": [], "equity_bucket": 0}
    tk = token()
    try:
        d = get_json(LOCAL_STATUS, tk)
    except Exception as e:                                        # noqa: BLE001
        out["errors"] = [f"status: {type(e).__name__}"]
        return out
    acct = d.get("account") or {}
    net = d.get("network") or {}
    out["mode"] = str(d.get("mode") or "unknown")
    out["kill"] = bool(d.get("kill"))
    out["reachable"] = bool(net.get("reachable"))
    out["api_key"] = "ok" if net.get("reachable") else ("missing" if "未配置" in str(net.get("error")) else "bad")
    eq = float(acct.get("equity") or 0.0)
    out["equity_bucket"] = equity_bucket(eq)   # 档宽≈权益10%自适应 + 迟滞（见函数 docstring）

    pos = d.get("positions") or []
    out["open"] = len(pos)
    out["positions"] = sorted([f"{p.get('coin')}:{int(p.get('side') or 0)}:{float(p.get('size') or 0):g}"
                               for p in pos])
    out["orders"] = len(d.get("orders_open") or [])

    # 止损覆盖：每个主腿持仓必须有方向正确的 reduce-only 触发单。
    # 挂单表是否可信由面板顶层 orders_trusted 决定（旧面板缺字段 → 按不可信）：
    # 不可信时无法区分「真裸仓」与「/openorders 临时超时」，只能报 unknown。
    trusted, trust_label = orders_trust(d)
    out["orders_trusted"] = trust_label
    out["stops_bad"], out["stops_unknown"] = stop_coverage(pos, d.get("orders_open"), trusted)
    for p in pos:
        coin = p.get("coin")
        side = int(p.get("side") or 0)
        if str(coin) in MR_COINS:
            # MR 腿（RSI2 日内）设计上不挂交易所止损：退出由策略次日平仓负责。
            # 把它算进 stops_bad 会让值班员给它补一张策略里不存在的止损单（改语义），
            # 并在每次 MR 持仓期持续误报 → 真裸仓被噪声淹没。
            continue
        rc = _risk_code(side, p.get("entry_px"), p.get("stop_px")) if p.get("stop_px") else "no_stop"
        if rc:
            out["risk_bad"].append(f"{coin}:{rc}")

    mr = d.get("mr") or {}
    out["mr"] = {True: "on", False: "off"}.get(bool(mr.get("enabled")), "off") + ":" + str(mr.get("state") or "?")

    # 执行器心跳年龄（三态，避免每轮都变）：调度线程/监控轮死了时，仓位仍「看起来正常」，
    # 但止损成交不会再入账。ok ≤15min；stale ≤60min；dead = 超过 1 小时或从未写过。
    rows = sql_json("select max(created_at) as t from hl_events where kind='heartbeat'")
    if rows and rows[0].get("t") not in (None, ""):
        try:
            hb_age = time.time() - float(rows[0]["t"])
            out["executor_hb"] = "ok" if hb_age <= HB_OK_S else ("stale" if hb_age <= HB_DEAD_S else "dead")
        except (TypeError, ValueError):
            out["executor_hb"] = "unknown"
    else:
        out["executor_hb"] = "dead"

    # 事件水位：**必须排除心跳类**。executor 每轮都写一条 heartbeat，若用 max(id)
    # 当水位，探针每轮都会判「变了」→ 值班员每 5 分钟被唤醒一次（实测刷屏）。
    rows = sql_json("select max(id) as m from hl_events "
                    "where kind in ('error','kill','dd')")
    if rows:
        out["events_max"] = int(rows[0].get("m") or 0)
    # 只统计**最近 15 分钟**的失败类事件：历史 error 会一直躺在表里，
    # 用「最近 40 条」会让已修好的老问题被反复报出来（实测：修完流水 bug 后
    # unknown_fail 仍挂着 ['error']，因为那条事件还能被 limit 40 取到）。
    rows = sql_json("select id,kind,message from hl_events "
                    "where created_at > strftime('%s','now') - 900 "
                    "order by id desc limit 40")
    if rows:
        seen = []
        for r in rows:
            k = str(r.get("kind") or "")
            if k and not any(w in k.lower() for w in ("open", "close", "entry", "exit", "order", "fill", "decision")):
                if any(w in k.lower() for w in FAIL_WORDS):
                    seen.append(k)
        out["unknown_fail"] = sorted(set(seen))

    # 最后一根已收盘 bar 的新鲜度（bar_state 表）
    # last_bar_ms 存的是 bar 的**开盘**时间（db.py 注释「已处理到哪根 bar」），
    # 4h 周期下 age 会在 4h→8h 间波动，直接用开盘时间算 age 会让每根 bar
    # 收盘前的 2 小时必然误报。改成从收盘时间（开盘 + 4h）算，age 峰值 4h。
    rows = sql_json("select symbol,last_bar_ms from bar_state")
    if rows:
        now_ms = time.time() * 1000
        ages = []
        for r in rows:
            try:
                ages.append((now_ms - int(r.get("last_bar_ms") or 0) - BAR_MS) / 3600000.0)
            except (TypeError, ValueError):
                continue
        if ages:
            out["bar_stale"] = bool(max(ages) > STALE_BAR_H)
    return out


def egress_ok():
    out, rc = sh(f"curl -s -m 12 {EGRESS_URL}")
    return rc == 0 and bool(out.strip())


def main():
    snap = {"kr": kraken(), "containers": containers(),
            "panel": {"kma_ok": http_code(PANEL_HEALTH) == 200, "egress_ok": bool(egress_ok())}}
    print(json.dumps(snap, ensure_ascii=False, sort_keys=True, indent=1))


if __name__ == "__main__":
    main()
