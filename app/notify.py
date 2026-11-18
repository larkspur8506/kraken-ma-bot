"""推送层：企业微信 + Telegram 双通道（复用 qqq-leaps 的成熟实现口径）。

- 任一通道送达即算成功（CompositeNotifier），全部失败才返回 False，调用方保留重试机会
- 每段最多 3 次尝试 + 退避
- **去重标记只在发送成功后写**（qqq 踩过的 P1：先置去重再发送 = 一次网络抖动永久丢提醒）
- 长消息按行分段（企业微信 text 2048 字节 / Telegram 4096 字符）
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

SEND_ATTEMPTS = 3
SEND_BACKOFF = (1.0, 3.0)
WECOM_MAX_BYTES = 1900
TELEGRAM_MAX_BYTES = 3800

#: 补推白名单的排除项：这些 kind 不是「发送失败」，不该被补推。
#: （heartbeat/weekly 有各自的静默口径；exec_alert_fills 受面板开关控制；
#: data_error_pending 是观察期中间态。）
RETRY_EXCLUDED_KINDS = ("heartbeat", "heartbeat_local", "weekly", "weekly_local",
                        "exec_alert_fills",
                        # 观察中的数据源告警：故意不发，不是发送失败，不得补推
                        "data_error_pending")


def plain_text(message: str) -> str:
    """推送纯文本：`**粗体**` 在 Telegram/企微（都没有 parse_mode）会原样显示星号。

    只去 `**` 这一种 Markdown 强调标记 —— 不改写其它 `*`/`_`/反引号，避免把
    「2*3」「ETH_USDT」这类正文误伤。
    """
    return str(message or "").replace("**", "")


def retry_failed_alerts(db, notifier, *, max_age_h: float = 24.0, limit: int = 5,
                        fill_push: bool = True) -> List[int]:
    """补推近 `max_age_h` 小时内发送失败的提醒（最多 limit 条/轮），返回成功的行 id。

    - `bar_ms=NULL` 的行不受 UNIQUE 约束，同一事件失败 N 次会留 N 行；按
      (symbol, kind, bar_ms) 只补推最新一条。
    - MR 事件（kind 以 `mr_` 开头，事件键就在 kind 里）补推成功后，用
      `db.mark_alert_kind_sent` 把**同一事件**的旧失败行一起了结：只标本次那一行的话，
      下一轮补推会把同一条消息再发一遍。
    - `fill_push=False`：成交回报（`mr_fill*`）不补推 —— 开关是用户意图，不是失败。
    """
    sent: List[int] = []
    rows = db.failed_alerts_since(time.time() - float(max_age_h) * 3600.0)

    def _base(kind) -> str:
        # `exec_alert_fills:suppressed` 这类带状态后缀的 kind 仍按基名归类
        return str(kind or "").split(":", 1)[0]

    retry = [r for r in rows if _base(r["kind"]) not in RETRY_EXCLUDED_KINDS]
    if not fill_push:
        # 用户此刻关着成交推送：待补推的成交记录就地了结（标 :suppressed），
        # 否则开关重新打开后会把「用户当时不想看」的旧成交全部补发。
        for r in retry:
            if str(r["kind"] or "").startswith("mr_fill"):
                try:
                    db.mark_alert_suppressed(int(r["id"]))
                except Exception:                                # noqa: BLE001
                    logger.exception("[retry] 成交提醒标记关闭失败：id=%s", r["id"])
        retry = [r for r in retry if not str(r["kind"] or "").startswith("mr_fill")]
    seen = set()
    dedup = []
    for r in retry:
        key = (r["symbol"], r["kind"], r["bar_ms"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)
    for r in dedup[:max(int(limit), 0)]:
        text = r["message"] or ""
        if not text:
            continue
        try:
            ok = bool(notifier and notifier.send_message(plain_text(text)))
        except Exception:                                        # noqa: BLE001
            logger.exception("[retry] 补推发送失败")
            ok = False
        if not ok:
            continue
        db.mark_alert_sent(int(r["id"]))
        kind = str(r["kind"] or "")
        if kind.startswith("mr_"):
            try:
                db.mark_alert_kind_sent(r["symbol"], kind)
            except Exception:                                    # noqa: BLE001
                logger.exception("[retry] 同事件失败行了结失败：%s", kind)
        sent.append(int(r["id"]))
        logger.info("补推失败提醒成功：id=%s kind=%s", r["id"], kind)
    return sent


def split_message_chunks(message: str, max_bytes: int) -> List[str]:
    """按行切分，单行超长则硬切（按字节边界安全解码）。"""
    if not message:
        return [""]
    chunks: List[str] = []
    cur = ""
    for line in message.split("\n"):
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate.encode("utf-8")) <= max_bytes:
            cur = candidate
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if len(line.encode("utf-8")) <= max_bytes:
            cur = line
        else:
            buf = line
            while len(buf.encode("utf-8")) > max_bytes:
                cut = max_bytes
                while cut > 0 and len(buf[:cut].encode("utf-8")) > max_bytes:
                    cut -= 1
                chunks.append(buf[:cut])
                buf = buf[cut:]
            cur = buf
    if cur or not chunks:
        chunks.append(cur)
    return chunks


def _post_json(url: str, payload: dict, timeout: int = 10) -> Tuple[bool, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        return True, body
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:                                        # noqa: BLE001
        return False, str(e)


class WeChatNotifier:
    """企业微信群机器人。"""

    def __init__(self, webhook_url: str):
        self.webhook_url = (webhook_url or "").strip()

    def __bool__(self) -> bool:
        return bool(self.webhook_url)

    def send_message(self, message: str) -> bool:
        if not self.webhook_url:
            return False
        # 纯文本通道（无 parse_mode）：统一去掉 `**`，别把 Markdown 星号发给用户。
        for chunk in split_message_chunks(plain_text(message), WECOM_MAX_BYTES):
            if not self._post_chunk(chunk):
                return False
        return True

    def _post_chunk(self, chunk: str) -> bool:
        payload = {"msgtype": "text", "text": {"content": chunk, "mentioned_list": []}}
        last = ""
        for attempt in range(1, SEND_ATTEMPTS + 1):
            ok, body = _post_json(self.webhook_url, payload)
            if ok:
                try:
                    if json.loads(body).get("errcode", 0) == 0:
                        return True
                    last = f"errcode: {body[:120]}"
                except Exception as e:                                  # noqa: BLE001
                    last = f"非 JSON 响应: {str(e)[:80]} | {body[:120]}"
                    # 旧版把非 JSON 一律当成功（代理错误页会被记 sent=1，重试机会丢失）；
                    # 企业微信正常响应必是 JSON，非 JSON 视为失败进入重试。
            else:
                last = body
            if attempt < SEND_ATTEMPTS:
                time.sleep(SEND_BACKOFF[min(attempt - 1, len(SEND_BACKOFF) - 1)])
        logger.error("[wecom] 发送失败: %s", last)
        return False


class TelegramNotifier:
    """Telegram（与 Hermes 共用同一个 bot；只发不收，不冲突）。"""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()

    def __bool__(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send_message(self, message: str) -> bool:
        if not self:
            return False
        # 纯文本通道（payload 不带 parse_mode）：统一去掉 `**`，否则星号原样显示。
        for chunk in split_message_chunks(plain_text(message), TELEGRAM_MAX_BYTES):
            if not self._post_chunk(chunk):
                return False
        return True

    def _post_chunk(self, chunk: str) -> bool:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": chunk, "disable_web_page_preview": True}
        last = ""
        for attempt in range(1, SEND_ATTEMPTS + 1):
            ok, body = _post_json(url, payload)
            if ok:
                try:
                    if json.loads(body).get("ok"):
                        return True
                    last = body[:160]
                except Exception:                                     # noqa: BLE001
                    last = body[:160]
            else:
                last = body
            if attempt < SEND_ATTEMPTS:
                time.sleep(SEND_BACKOFF[min(attempt - 1, len(SEND_BACKOFF) - 1)])
        logger.error("[telegram] 发送失败: %s", last.replace(self.bot_token, "***"))
        return False


class CompositeNotifier:
    """多通道分发：任一送达即成功。"""

    def __init__(self, channels: List[Tuple[str, Any]]):
        self.channels = [(n, c) for n, c in (channels or []) if c]

    def __bool__(self) -> bool:
        return bool(self.channels)

    def send_message(self, message: str) -> bool:
        if not self.channels:
            logger.warning("[notify] 未配置任何通道，消息未发送")
            return False
        results = []
        for name, ch in self.channels:
            try:
                results.append((name, bool(ch.send_message(message))))
            except Exception as e:                                # noqa: BLE001
                logger.error("[notify] 通道 %s 异常: %s", name, e)
                results.append((name, False))
        delivered = any(ok for _, ok in results)
        if not delivered:
            logger.error("[notify] 全部通道失败: %s", results)
        elif not all(ok for _, ok in results):
            logger.warning("[notify] 部分通道失败: %s", results)
        return delivered


def get_notifier(config) -> Optional[CompositeNotifier]:
    channels: List[Tuple[str, Any]] = []
    if getattr(config, "wechat_webhook", ""):
        channels.append(("wecom", WeChatNotifier(config.wechat_webhook)))
    if getattr(config, "telegram_token", "") and getattr(config, "telegram_chat_id", ""):
        channels.append(("telegram", TelegramNotifier(config.telegram_token, config.telegram_chat_id)))
    return CompositeNotifier(channels) if channels else None
