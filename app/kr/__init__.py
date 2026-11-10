"""Kraken Futures venue 层（PR-1）。

对齐 app/hl/exchange.HlExchange 与 app/hl/info.HlInfo 的鸭子类型接口，
让 app/hl/executor.py 的业务逻辑（闸门/grace/对账/推送/记账）原样复用。

已知语义差（Kraken Derivatives v3 OpenAPI，2026-11-08 逐字段核对）：
- flex 多抵押永续只有 cross 全仓，没有逐仓 → update_leverage 是 no-op，
  尾部损失 = 整个期货钱包，靠「期货钱包只放机器人该亏的钱」缓解。
- cliOrdId 直接透传 HL 的 cloid 字符串（0x + 32 hex，长度 34 ≤ 64 上限）。
- /fills 不带 fee/reduceOnly，但**带 realized_pnl**（第一页有值、历史页为 null）
  与 sequence_id；dir/reduce_only 由成交序列推演，fee 缺失时按 taker 费率估并打
  fee_estimated 标记。
- 所有响应字段名以 OpenAPI schema 为准（order_id / unfilledSize / fillTime 为
  RFC3339 字符串），**不要**按 HL 的 orderId/filledQty 习惯猜。
"""
