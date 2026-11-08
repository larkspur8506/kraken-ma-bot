"""Kraken Futures venue 层（PR-1）。

对齐 app/hl/exchange.HlExchange 与 app/hl/info.HlInfo 的鸭子类型接口，
让 app/hl/executor.py 的业务逻辑（闸门/grace/对账/推送/记账）原样复用。

已知语义差（docs/KRAKEN_MIGRATION_PLAN.md rev.2）：
- flex 多抵押永续只有 cross 全仓，没有逐仓 → update_leverage 是 no-op，
  尾部损失 = 整个期货钱包，靠「期货钱包只放机器人该亏的钱」缓解。
- cliOrdId 直接透传 HL 的 cloid 字符串（0x + 32 hex，长度 34 ≤ 64 上限）。
- /fills 不带 reduceOnly/fee 字段 → 由成交序列推演 dir/reduce_only，
  fee 缺失时按 taker 费率估并打 fee_estimated 标记（沙盒期核实）。
"""
