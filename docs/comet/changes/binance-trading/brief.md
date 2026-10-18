# Outcome

为 PA_Agent 增加 Binance 真实交易执行链路，覆盖**现货与 USDT-M 永续合约**两个市场：AI 两阶段分析产出交易决策后，用户经人工确认在币安真实下单（Testnet 默认），合约市场支持做多与做空；止盈支持 TP1/TP2 分批（各出一半）；并配套账户管理（余额/持仓/挂单/撤单/平仓），实现「决策 → 确认 → 下单 → 自动保护 → 账户管理」完整闭环。

# Scope

- 新增交易执行后端 `pa_agent/trade/`：统一双市场路由（现货 binance-sdk-spot / 合约 binance-sdk-derivatives-trading-usds-futures）；下单/撤单/账户查询封装；AI 决策 → 订单计划；保护单策略；限价成交后台轮询检测；交易所错误中文友好化
- **现货保护单**：未设 TP2 → 一组 OCO（TP1 全量 + SL 全量）；设 TP2 → 两组 OCO（TP1 半量+SL 半量 / TP2 半量+SL 半量，同 SL 价；任一 TP 成交其 SL 腿联动取消，价格打 SL 时剩余量全部了结）
- **合约保护单**：TP1/TP2 各半量 reduceOnly 止盈单；SL 用 closePosition 型止损单（无论剩余仓位多少全平）；做空（SELL 开空）时 SL/TP 方向自动反转；下单前按设置调整杠杆（默认 5x，可调 1-20x）、单向持仓模式
- **合约行情**：BinanceSource 扩展 market 模式——合约模式下 K 线切换为合约数据（fapi klines + fstream WebSocket），代理路由复用
- 新增 GUI：交易设置对话框（API Key/Secret、市场开关【现货/USDT-M 合约】、合约杠杆、Testnet/实盘切换、金额配置）、订单确认对话框（完整明细 + 金额微调 + 市场与环境徽标）、交易面板（余额/持仓（合约）/挂单/撤单/市价平仓）；决策面板与下单机会警报弹窗各加「执行下单」入口；菜单栏「交易」菜单
- 配置与安全：settings 扩展（trade_enabled 默认关、market 默认 spot、leverage 默认 5、api_key、api_secret_encrypted、testnet 默认开、stake_usdt、max_stake_usdt）；API Secret 用 Windows DPAPI 加密存储（恢复 `pa_agent/security/secret_store.py`，来源 git e41142d^）
- 执行留痕：下单/撤单/平仓追加 `trade_records/executions.jsonl`（含市场标记，永不写入 Secret）
- 下单执行在后台线程（QThread），结果经信号回主线程

## 目录结构

### 新建

- `pa_agent/trade/__init__.py` — 交易包入口
- `pa_agent/trade/binance_trader.py` — 统一双市场 Trader：按 market 路由到现货/合约 REST 客户端（Testnet/Prod base_path、代理复用 binance_source 探测、timeout 8000/retries 0、server_time 偏差校验）；place_market_entry / place_limit_entry / place_protection_orders（按市场走 OCO 或 reduceOnly+closePosition）/ cancel_order / cancel_all / balances / positions（合约）/ open_orders / close_position_market / symbol_filters
- `pa_agent/trade/order_plan.py` — build_order_plan：决策（含做多/做空）→ OrderPlan；按市场计算数量（现货按 USDT 金额、合约按金额/杠杆折算数量）；stepSize/tickSize/minNotional 截断；现货做空抛 TradeBlockedError（提示切合约）
- `pa_agent/trade/protection.py` — 保护单策略：现货单/双 OCO 构造、合约 TP 分批 + closePosition SL 构造（做空方向反转）
- `pa_agent/trade/fill_watcher.py` — FillWatcher：轮询限价单状态（失败退避），成交回调挂保护单；scan_unprotected 启动兜底
- `pa_agent/trade/errors.py` — TradeError（中文映射 -2010/-1013/-1021/-2019 等）、TradeBlockedError
- `pa_agent/security/secret_store.py` — SecretStore（DPAPI/Fernet），自 git e41142d^ 恢复
- `pa_agent/gui/binance_trade_settings_dialog.py` — 交易设置对话框（密文显隐、市场开关、杠杆、实盘红色警告）
- `pa_agent/gui/order_confirm_dialog.py` — 订单确认对话框（市场/环境徽标、TP1/TP2 分批明细）
- `pa_agent/gui/trade_panel.py` — 交易面板（非模态：余额/合约持仓/挂单/撤单/平仓）
- `tests/unit/test_trade_*.py` — 后端单测（fake SDK 注入，双市场用例）
- `tests/unit/test_secret_store.py` — 加密往返单测
- `trade_records/executions.jsonl` — 运行时生成的执行留痕（目录已存在）

### 修改

- `pa_agent/config/settings.py` — BinanceSettings 增交易字段（trade_enabled/market/leverage/api_key/api_secret_encrypted/testnet/stake_usdt/max_stake_usdt）+ 内存 api_secret（exclude 序列化）；load 解密 / save 剥离明文
- `pa_agent/data/binance_source.py` — 增 market 模式：合约 K 线（fapi /fapi/v1/klines + wss://fstream.binance.com WS 流），REST/WS 域名与路由按市场切换
- `pa_agent/data/factory.py` — BinanceSource 构造传入 market 设置
- `pa_agent/gui/decision_panel.py` — 底部加「执行下单」按钮 + order_execute_requested 信号（仅 binance 数据源且有下单机会时可见）
- `pa_agent/gui/order_opportunity.py` — 警报弹窗加「执行下单」按钮
- `pa_agent/gui/main_window.py` — 菜单「交易」入口、信号接线 → 确认对话框 → _TradeWorker(QThread) → 状态栏/结果弹窗；未启用/未配 Key 引导配置；市场切换时刷新行情与面板

### 删除

无。

### 明确不建

- 不做币本位合约（COIN-M）与期权——USDT-M 已覆盖主流做空/杠杆需求，双倍 SDK 与测试面不划算
- 不做无确认自动跟单——与「人工确认」核心决策冲突
- 不做对冲持仓模式（双向持仓）——单向持仓实现最简，做空=SELL
- 不改现货数据源架构——合约模式为新增分支，现货路径保持不变
- 不做 TP3+ 更多分批——两档分批已覆盖需求，复杂度陡增

# Non-goals

- 币本位合约（COIN-M）、期权
- 全自动跟单（跳过人工确认）
- 对冲（双向）持仓模式
- 多账户管理、跨所搬砖、资金费率套利策略

# Acceptance examples

- **现货市价闭环**：Testnet 配 Key（市场=现货）→ 决策「市价单·做多」→ 确认（现货+Testnet 徽标）→ 市价买入成交 → 自动挂 OCO → 面板可见持仓/挂单 → 撤单/平仓 → 留痕
- **现货 TP 分批**：决策带 TP1+TP2 → 确认后挂两组 OCO（TP1 半量+SL 半量 / TP2 半量+SL 半量）；TP1 成交后 OCO1 SL 腿联动取消，剩余半量由 OCO2 保护
- **合约做多闭环**：市场切合约（行情自动切 fstream K 线）→ 市价做多 → TP1/TP2 各半 reduceOnly + closePosition SL → 面板可见合约持仓与杠杆
- **合约做空**：决策「做空」→ 合约 SELL 开空 → SL/TP 方向自动反转（SL 在上、TP 在下）→ 保护单正确挂出
- **杠杆设置**：设置中调杠杆（默认 5x）→ 下单前按设置调整合约杠杆并回显在确认对话框
- **现货做空拦截**：市场=现货时做空决策点执行 → 提示「现货不支持做空，请切到合约市场」
- **限价成交检测**：限价挂单 → 轮询检测成交 → 自动挂保护单 → 通知
- **未启用引导**：trade_enabled 关闭或未配 Key → 提示并打开设置，不发交易请求
- **实盘切换警告**：切实盘 → 红色风险警告确认；取消保持 Testnet
- **错误友好化**：余额不足（-2010）→ 中文提示所需与可用金额；精度不符（-1013）→ 自动截断后仍失败时提示
- **金额护栏**：金额超上限被拒并提示上限值
- **Secret 安全**：settings.json 只存密文；executions.jsonl 无 Secret；重启后解密可用

# Constraints and invariants

- 每次 UI 下单必须经过确认对话框；无任何免确认路径
- trade_enabled 默认 false；testnet 默认 true；market 默认 spot；leverage 默认 5（范围 1-20）
- 合约杠杆调整仅在用户设置变更后、下单前执行，且确认对话框回显当前杠杆
- API Secret 仅存在运行内存与 DPAPI 密文，任何落盘文件不得含明文
- 下单 POST 不自动重试（SDK 保证），避免重复下单
- 现货做空必须拦截并引导切合约，不得下裸 SELL
- 所有 Binance 面向用户的文案为中文

# Decisions

以下决定已与用户确认：

1. **触发方式**：AI 决策 + 人工确认（决策面板按钮 + 警报弹窗按钮 → 确认对话框）（2026-10-18）
2. **环境**：Testnet 默认；切实盘需红色警告确认（2026-10-18）
3. **市场覆盖**：现货 + USDT-M 合约；做空在合约实现，现货拦截引导（2026-10-19）
4. **TP 分批**：设 TP2 时 TP1 出一半、TP2 出另一半；现货用双 OCO、合约用 reduceOnly+closePosition（2026-10-19）
5. **合约行情**：合约模式 K 线切合约数据（fstream/fapi）（2026-10-19）
6. **杠杆**：默认 5x 可调 1-20，单向持仓（2026-10-19）
7. **市场切换**：全局市场开关，执行链路自动路由（2026-10-19）
8. **账户功能**：余额/持仓（合约）/挂单/撤单/市价平仓（2026-10-18）
9. **Secret 存储**：恢复 SecretStore（DPAPI）加密 api_secret_encrypted（2026-10-18）
10. **GUI 形态**：三个对话框 + 菜单入口，不新增侧边 tab（2026-10-18）
11. **市价数量**：现货按 quoteOrderQty 下单，合约按金额与杠杆折算，保护单数量取实际成交回执（2026-10-18/19）

# Verification expectations

- 单元测试：fake SDK 注入（双市场下单参数组装、精度截断、做空/拦截、保护单构造（单 OCO/双 OCO/reduceOnly+closePosition）、FillWatcher 回调、留痕脱敏、加密往返）；pytest-qt 对话框往返与警告路径；BinanceSource 合约模式 K 线解析
- Testnet 手动冒烟：现货/合约×多空闭环、TP 分批、撤单、平仓、各拦截路径，结果记入留痕
- 全量 tests/unit 通过（存量 5 个已知失败除外：deepseek_client/json_validator，与本需求无关）
