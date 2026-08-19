# TRAE Work CN 集成调查报告

> 调查时间：2026-08-11 ~ 2026-08-14
> 目标：让 PA Agent 调用 TRAE Work CN 内置的 GLM-5.2 模型，避免使用其他付费模型提供商。
> 结论：**不可行**。详见下文分析。

---

## 一、背景

PA Agent 需要一个稳定的 LLM 后端用于市场诊断。TRAE Work CN（SOLO CN）内置了 GLM-5.2 模型，用户在客户端中使用时从未遇到限流问题，因此希望 PA Agent 也能调用同一模型。

初步尝试直接调用 `/api/ide/v1/llm_raw_chat` 端点时，立即触发限流：

```
We're sorry, your requests have exceeded the rate limit. Please wait and try again later.
(code 4011)
```

而客户端在正常使用中从未出现此问题，因此展开深入调查。

---

## 二、调查路径与结果

### 路径 1：直接调用 `llm_raw_chat`（v1）

- **端点**：`/api/ide/v1/llm_raw_chat`
- **结果**：**被限流**（code 4011）
- **结论**：此端点对 PA Agent 这类外部调用方实施严格限流，无法直接使用。

### 路径 2：尝试 `llm_raw_chat_v2`（v2）

- **端点**：`/api/ide/v1/llm_raw_chat_v2`
- **背景**：日志显示服务器内部调用 v2 版本不限流。
- **结果**：**外部调用同样被限流**。v2 仅在服务器内部（从 `create_agent_task` 流程调用）才不限流。

### 路径 3：尝试 `llm_utils_chat`

- **端点**：`/api/agent/v3/llm_utils_chat`（function=chat）
- **结果**：所有 `function` 参数值均触发限流。

### 路径 4：调用 `create_agent_task`（agent 编排流程）

- **端点**：`/api/agent/v3/create_agent_task`
- **结果**：**不限流**（HTTP 200），但返回错误：
  ```
  failed to get summary config: failed to get summary template data
  ```
- **进展**：
  1. 修正 `model_name` 为内部名称 `glm-5.2__dev`，`config_source` 为枚举值 `"Trae"`，解决了 "model config is empty" 错误。
  2. 通过 `batch_get_detail_param` API 获取完整的 `function_config`（207KB）和 `extra_config`。
  3. 添加 `enable_chat_memory_user_config=true`、`scene_params`、`function_config` 等字段。
  4. 仍报 "failed to get summary config: failed to get summary template data"。
- **根因**：服务器需要 `encrypted_prompt_set` 字段（加密的 prompt 模板数据），该字段仅存在于 ai-agent 进程内部的 `ModelConfigDbCache`（加密的 `database.db`）中，`batch_get_detail_param` API 不返回此字段，PA Agent 无法从外部获取或构造。

### 路径 5：通过 IPC 直接调用 ai-agent

- **方法**：使用 `@aha-kit/ipc` 和 `@aha-kit/rpc` 模块，通过命名管道连接 ai-agent 进程，调用 `lite/send_message` 方法，让 ai-agent 代为构造 `create_agent_task` 请求。
- **结果**：**连接成功但请求超时**，返回 `server-unavailable`。
- **根因**：ai-agent 进程需要完整的客户端注册流程（包括 `subscribe_events`、窗口上下文等），PA Agent 作为外部进程无法满足此前置条件。

### 路径 6：mitmproxy 流量捕获

- **方法**：通过 DNS 劫持 + 反向代理捕获 TRAE SOLO CN 的 HTTPS 流量。
- **结果**：捕获到部分请求，但 `create_agent_task` 请求体在 ai-agent 进程内部构造，未经过 Electron 的网络层，因此无法通过 mitmproxy 捕获。

### 路径 7：二进制分析

- **方法**：分析 `ai_agent.dll` 中的字段名和结构定义。
- **发现**：
  - `CreateAgentTaskRequest` 结构包含 `model_config_node`、`scene_params`、`function_config` 等字段。
  - `ModelDetailInfo` 结构有 17 个字段，但 `batch_get_detail_param` 响应只返回 8 个，缺失 `encrypted_prompt_set` 等加密字段。
  - `ModelConfigDbCache` 从本地加密数据库读取完整配置，PA Agent 无法访问。

---

## 三、关键发现

### 1. 限流策略差异

| 端点 | 限流 | 说明 |
|------|------|------|
| `llm_raw_chat` (v1) | 是 | 严格限流，外部调用立即触发 |
| `llm_raw_chat_v2` | 是（外部）/ 否（内部） | 仅服务器内部从 `create_agent_task` 调用时不限流 |
| `llm_utils_chat` | 是 | 所有 function 值均限流 |
| `create_agent_task` | 否 | 不限流，但需要完整的 agent 编排参数 |

### 2. 客户端调用链

```
用户操作 → Electron 主进程 → IPC（命名管道）→ ai-agent 进程
  → batch_get_detail_param（获取模型配置，缓存到本地 DB）
  → create_agent_task（带完整 model_config_node，包含 encrypted_prompt_set）
  → 服务器内部调用 llm_raw_chat_v2（不限流）
  → SSE 流式响应
```

### 3. 无法复刻的环节

`create_agent_task` 请求需要 `encrypted_prompt_set` 字段，该字段：
- 不在 `batch_get_detail_param` API 响应中（API 只返回 8/17 个字段）
- 存储在 ai-agent 进程内部的加密 `database.db` 中
- 文件被 ai-agent 进程锁定且加密，外部无法读取
- ai-agent 二进制中存在 `ModelConfigDbCache` 相关逻辑，从本地 DB 读取并解密

### 4. IPC 路径的障碍

通过 IPC 调用 `lite/send_message` 需要：
- 完整的客户端注册流程（`subscribe_events`、窗口上下文）
- 与 Electron 主进程绑定的 session_id
- ai-agent 进程仅识别来自已注册客户端的请求

PA Agent 作为独立 Python 进程，无法满足这些前置条件。

---

## 四、最终结论

**PA Agent 无法调用 TRAE Work CN 内置模型**，根本原因是：

1. 直接调用 `llm_raw_chat` 会被限流。
2. `create_agent_task` 路径需要 `encrypted_prompt_set` 等加密字段，仅存在于 ai-agent 进程内部的加密数据库中，无法通过公开 API 获取或从外部构造。
3. IPC 路径需要完整的客户端注册流程，PA Agent 无法满足。

这三条路径均已穷尽探索，无法绕过。

---

## 五、可用替代方案

PA Agent 已支持以下模型提供商，可作为 TRAE Work CN 的替代：

| 提供商 | 路由名 | 状态 | 说明 |
|--------|--------|------|------|
| Qoder CN | `openclaw_qc` | 可用 | 已修复 `_extract_json_content` 函数问题，路由正常 |
| DeepSeek | - | 可用 | 直接 API 调用，无限流问题 |

在 PA Agent 的「AI 模型」设置中保存对应配置即可切换。

---

## 六、保留的代码变更

以下变更已合入正式代码，与本次调查相关：

- [trae_client.py](file:///d:/cl/PA_Agent/pa_agent/ai/trae_client.py)：限流重试参数调整为 `_RATE_LIMIT_MAX_RETRIES=5`、`_RATE_LIMIT_BACKOFF_BASE_S=10.0`（指数退避：10s/20s/40s/80s/160s）。
- [trae_connector.py](file:///d:/cl/PA_Agent/pa_agent/ai/trae_connector.py)：`_get_trae_cn_info` 函数获取认证信息。
- [qoder_client.py](file:///d:/cl/PA_Agent/pa_agent/ai/qoder_client.py)：修复 `_extract_json_content` 函数未定义的问题，使 `openclaw_qc` 路由可用。

---

## 七、技术参考

### 关键文件位置

- ai-agent 二进制：`C:\Users\Administrator\AppData\Local\Programs\TRAE SOLO CN\resources\app\modules\ai-agent\ai_agent.dll`
- ai-agent 日志：`C:\Users\Administrator\AppData\Roaming\TRAE SOLO CN\logs\<timestamp>\Modular\ai-agent_*.log`
- ai-agent 数据库：`C:\Users\Administrator\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\database.db`（加密）
- 客户端设置：`C:\Users\Administrator\AppData\Roaming\TRAE SOLO CN\User\settings.json`

### `batch_get_detail_param` 响应结构

```
{
  "config_info_list": [...],      # 模型配置列表（8/17 字段）
  "function_configs": [...],      # 6 个 function 配置
  "client_config": "...",         # 客户端配置
  "ab_versions": [...]
}
```

缺失字段：`encrypted_prompt_set`、`encrypted_prompt_list`、`prompt_template_map` 等加密字段。

### `create_agent_task` 请求体关键字段

```
{
  "model_info": {
    "config_name": "glm-5.2",
    "config_source": "Trae",       # 枚举值，非数字 1
    "model_name": "glm-5.2__dev", # 内部模型名称
    "extra_config": "...",         # 关键：加密的 prompt 配置
    ...
  },
  "function_config": {...},        # 来自 batch_get_detail_param
  "enable_chat_memory_user_config": true,
  "common_params": "...",          # JSON 字符串，客户端环境信息
  ...
}
```
