# Kraken Bot 面板 UX 优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变交易逻辑和数据契约的前提下，分三轮改善 Kraken Bot 面板的主屏层级、手机仓位阅读、标的动态分层和渲染性能。

**Architecture:** 继续使用现有 Python 服务端 HTML 渲染和 CSS 响应式布局。第一轮只调整 `_render_body` 及相关 CSS/测试；第二轮在同一渲染层增加桌面表格/手机仓位行的稳定布局与标的卡信息层级；第三轮收口共用样式并建立真实 HTTP/浏览器验收，不引入前端运行时或新 API 字段。

**Tech Stack:** Python 3, FastAPI, server-rendered HTML, CSS, unittest/pytest, Browser Use。

**Spec:** `/opt/data/cache/scratch/kraken-panel-review/2026-11-18-kraken-panel-ux-design.md`

## Global Constraints

- 只改展示层、HTML/CSS 和必要的页面交互。
- 不修改策略参数、信号计算、下单、对账、风控、状态机和交易所 API 适配层。
- 不修改 `status_payload()` 结构；优先复用现有快照字段。
- 不引入 React/Vue/WebSocket/外部字体/外部资源。
- 保持服务端渲染、零外部资源、gzip、HTTP/2、黑白跟随系统三态主题。
- 正常状态减少噪声，异常状态提升到用户第一眼可见的位置。
- 桌面端保留高密度表格，手机端允许使用专门的仓位行布局。
- 本机只跑受影响的定向测试、`py_compile` 和浏览器验收；全量测试交 GitHub Actions。

## Review Focus

- 交易所不可达时，账户金额不能显示成 0，且异常提示必须位于主内容前部；测试覆盖不可达 payload。
- 止损未知或挂单表不可信时，不能被正常态隐藏逻辑吞掉；测试覆盖 `orders_trusted=False/None`。
- 空仓、有仓、MR 持仓和普通持仓不能重复显示同一仓位或现价；测试覆盖四种 payload。
- 手机端关键数据不能只存在 `title`，也不能造成横向溢出；测试断言可见文本和响应式 CSS。
- 快照数据时间不能被误标成浏览器/交易所实时延迟；测试保留现有延迟口径断言。

---

### Task 1: 第一轮主屏层级基线与回归测试

**Files:**
- Modify: `tests/test_panel_p1.py`
- Modify: `tests/test_panel_disp.py`
- Modify: `tests/test_panel_merge.py`
- Test fixture review: `tests/test_hl_views.py`, `tests/test_admin_ui.py`

**Interfaces:**
- Consumes: 现有 `_render_body(payload)`、`_overview_body(...)` 和测试 fixture。
- Produces: 明确的区块顺序、异常前置、单一现价/账户金额断言，供后续 CSS 与布局任务使用。

- [ ] **Step 1: 读取现有面板测试和 fixture，列出会被新结构影响的断言。**
- [ ] **Step 2: 为空仓、有仓、不可达、止损未知五种状态补充或收紧失败测试。**
- [ ] **Step 3: 运行受影响测试，记录当前失败断言作为实现边界。**

Run: `/tmp/kvenv/bin/python -m pytest tests/test_panel_p1.py tests/test_panel_disp.py tests/test_panel_merge.py tests/test_hl_views.py tests/test_admin_ui.py -q`

### Task 2: 第一轮主屏和账户/仓位布局

**Files:**
- Modify: `app/hl/views.py:2912-3320`
- Modify: `app/admin_ui.py:1541-1565, 2018-2037`
- Modify: `tests/test_panel_p1.py`, `tests/test_panel_disp.py`

**Interfaces:**
- Consumes: 现有 payload 字段 `network`, `account`, `positions`, `orders_open`, `system`, `mr`。
- Produces: 仍返回 HTML 字符串；不新增 payload 字段，不改变业务函数签名。

- [ ] **Step 1: 调整 `_render_body` 主区块输出顺序，使异常横幅、账户摘要、仓位位于动态和绩效之前。**
- [ ] **Step 2: 将有仓/空仓输出拆为稳定的语义结构，保留现有字段口径和单一现价来源。**
- [ ] **Step 3: 对不可达、对账未知、止损未知状态增加可见的短行动提示，并保留详细错误/证据。**
- [ ] **Step 4: 同步 `/admin?tab=overview` 的区块剥离逻辑，确保不会产生重复标题或状态条。**
- [ ] **Step 5: 运行第一轮定向测试和 `py_compile`。**

Run: `/tmp/kvenv/bin/python -m pytest tests/test_panel_p1.py tests/test_panel_disp.py tests/test_panel_merge.py tests/test_hl_views.py tests/test_admin_ui.py -q`
Run: `python3 -m py_compile app/hl/views.py app/admin_ui.py`

### Task 3: 第二轮手机仓位布局

**Files:**
- Modify: `app/hl/views.py` position rendering and `DASH_CSS`
- Modify: `app/admin_ui.py:ADMIN_CSS`
- Modify: `tests/test_panel_rows.py`, `tests/test_panel_p4.py`, `tests/test_panel_stop_strict.py`

**Interfaces:**
- Consumes: 现有仓位行字段和共享止损判据。
- Produces: 桌面端原表格、窄屏端可见仓位行结构；数值仍使用现有格式化函数。

- [ ] **Step 1: 为手机布局增加专属 class 和语义化可见行，不修改桌面表格字段口径。**
- [ ] **Step 2: 固定数字列和卡片高度约束，处理长订单号、长错误文案和缺失值。**
- [ ] **Step 3: 将关键止损/浮盈/离场距离从仅悬停说明改为手机可见短文案。**
- [ ] **Step 4: 补充 390px/342px HTML/CSS 断言和重复仓位断言。**
- [ ] **Step 5: 运行手机布局定向测试与编译检查。**

Run: `/tmp/kvenv/bin/python -m pytest tests/test_panel_rows.py tests/test_panel_p4.py tests/test_panel_stop_strict.py -q`
Run: `python3 -m py_compile app/hl/views.py app/admin_ui.py`

### Task 4: 第二轮标的动态分层

**Files:**
- Modify: `app/hl/views.py:2162-2285`
- Modify: `app/ui_theme.py:119-146`
- Modify: `tests/test_panel_signals.py`, `tests/test_panel_fields.py`, `tests/test_mr_panel_view.py`

**Interfaces:**
- Consumes: `_merged_cards`, `_sym_card`, `_reason_line`, `_gate_lines`, `_feed_line` 现有接口。
- Produces: 每张标的卡的固定结构：标的状态、价格、交易结论、原因/异常、数据源；不改变策略计算。

- [ ] **Step 1: 为 `_sym_card` 增加稳定的语义层 class，保持现价只从 `_merged_cards` 结果渲染。**
- [ ] **Step 2: 按持仓/空仓/MR 状态收敛距离行，删除重复且不提供决策价值的正常说明。**
- [ ] **Step 3: 让异常原因和数据源失败在手机端直接可见，正常数据源保持静默。**
- [ ] **Step 4: 补充 MR 已完成会话、异常优先级、单一现价和四卡渲染测试。**
- [ ] **Step 5: 运行标的动态定向测试与编译检查。**

Run: `/tmp/kvenv/bin/python -m pytest tests/test_panel_signals.py tests/test_panel_fields.py tests/test_mr_panel_view.py -q`
Run: `python3 -m py_compile app/hl/views.py app/ui_theme.py`

### Task 5: 第三轮视觉与性能收口

**Files:**
- Modify: `app/hl/views.py` CSS block
- Modify: `app/admin_ui.py` `ADMIN_CSS`
- Modify: `app/ui_theme.py` shared theme/symbol CSS
- Modify: `tests/test_panel_speed.py`, `tests/test_panel_disp.py`
- Create: `tools/panel_benchmark.py` only if existing benchmark cannot cover service-render timing

**Interfaces:**
- Consumes: 已完成的 HTML class 和主题变量。
- Produces: 无布局抖动的图表/数字、统一按钮与触摸尺寸、三态主题兼容的 CSS。

- [ ] **Step 1: 删除重复 CSS 规则，统一间距、数字排版、焦点态和移动端触摸区域。**
- [ ] **Step 2: 固定曲线容器尺寸并检查长文本换行，不改变曲线数据。**
- [ ] **Step 3: 保留现有 gzip、分页、CSV 流式导出和自动刷新行为。**
- [ ] **Step 4: 扩展渲染耗时/HTML 大小基准，明确服务端渲染与公网网络耗时的边界。**
- [ ] **Step 5: 运行视觉相关定向测试、编译检查和 benchmark。**

### Task 6: 独立浏览器验收

**Files:**
- No product code changes expected.
- Evidence: `/opt/data/cache/scratch/kraken-panel-review/`

- [ ] **Step 1: 用真实授权面板检查桌面 DOM 区块顺序和关键文本。**
- [ ] **Step 2: 检查 390px、342px、桌面宽度的 `scrollWidth == innerWidth`。**
- [ ] **Step 3: 检查黑、白、跟随系统主题、自动刷新和异常状态展示。**
- [ ] **Step 4: 验证 `/admin` 未授权 401、Bearer/浏览器 Cookie 会话正常，且 token 不进入页面链接。**
- [ ] **Step 5: 汇总每轮 diff、定向测试和浏览器证据，确认交易相关文件零改动。**

### Task 7: CI 与交付

**Files:**
- No additional product files expected.

- [ ] **Step 1: 检查 `git diff --name-only`，确认只包含规格允许的面板文件和测试。**
- [ ] **Step 2: 推送分支并等待 GitHub Actions 全量测试，核对 `Ran N tests` 和 `OK`。**
- [ ] **Step 3: 若 CI 失败，只修面板回归，不放宽安全/交易判据。**
- [ ] **Step 4: 生成分轮变更摘要、验证结果和未覆盖风险。**
