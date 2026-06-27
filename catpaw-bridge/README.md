# CatPawAI Bridge

将美团 CatPawAI IDE 的 **glm-5.2** 模型反向代理出来，供 **Claude Code** 使用。

## 架构

```
Claude Code (Anthropic /v1/messages 格式)
    │
    ▼
CLIProxyAPI (:8317)
    │  翻译 Anthropic -> OpenAI 格式
    │  翻译 OpenAI 响应 -> Anthropic 格式
    ▼
CatPawAI 反向代理 (:9000)
    │  从 state.vscdb 读取 SSO accessToken
    │  注入 Cookie + Catpaw-Auth Header
    │  转换 OpenAI 请求 -> CatPawAI Agent 格式
    │  AES-128-ECB + RSA-OAEP-SHA1 加密请求体
    │  解密并转换 CatPawAI SSE 响应 -> OpenAI 格式
    ▼
CatPawAI API (catpaw.meituan.com/api/gpt/openai/stream)
    │
    ▼
glm-5.2 模型
```

## 文件说明

| 文件 | 说明 |
|---|---|
| `bridge.conf.yaml` | **统一配置文件**（端口、API Key、模型名等所有配置的唯一数据源） |
| `catpaw_reverse_proxy.py` | CatPawAI 反向代理（核心），含 SSO 认证、加密通信、格式转换 |
| `config.yaml` | CLIProxyAPI 配置，定义 catpaw 上游 Provider（从 bridge.conf.yaml 同步端口） |
| `start.sh` | 一键启动/停止脚本 |
| `cc-switch.sh` | Claude Code 配置切换脚本（从 bridge.conf.yaml 读取配置） |
| `codex-config.toml` | Codex CLI 配置模板（手动复制到 ~/.codex/config.toml） |
| `codex-switch.sh` | Codex CLI 配置切换脚本（自动生成 ~/.codex/config.toml） |
| `cc-switch-profiles.json` | cc-switch 桌面应用导入配置（由 generate-profiles.py 从 YAML 生成） |
| `generate-profiles.py` | 从 bridge.conf.yaml 生成 cc-switch-profiles.json 的工具 |

## 快速开始

### 前提条件

1. **CatPawAI IDE 已登录** — 确保你已在 CatPawAI IDE 中登录美团 SSO 账号
2. **Python 3.8+** — `brew install python3`
3. **Go 1.24+** — 用于编译 CLIProxyAPI
4. **网络可达美团内网** — `catpaw.meituan.com` 需要内网或 VPN 访问

### 1. 配置

所有配置集中在 `bridge.conf.yaml` 一个文件中：

```yaml
# 修改端口、API Key、模型名等
cliproxy:
  port: 8317
  api_key: "sk-catpaw-bridge-key"

catpaw_proxy:
  port: 9000

model:
  name: "glm-5.2"
  type_code: 2  # 1=快速, 2=精确
```

配置优先级：**环境变量 > bridge.conf.yaml > 代码内默认值**

### 2. 启动 Bridge 服务

```bash
cd catpaw-bridge
./start.sh
```

脚本会自动：
- 安装 Python 依赖 (aiohttp, pycryptodome, pyyaml)
- 编译 CLIProxyAPI
- 启动 CatPawAI 反向代理 (:9000)
- 启动 CLIProxyAPI (:8317)

### 3. 在 Claude Code 中使用

**方式 A：环境变量（最简单）**

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8317
export ANTHROPIC_AUTH_TOKEN=sk-catpaw-bridge-key
export ANTHROPIC_MODEL=glm-5.2
claude
```

**方式 B：cc-switch 脚本（自动读取 bridge.conf.yaml）**

```bash
source catpaw-bridge/cc-switch.sh
cc_switch catpaw          # 切换到 CatPaw GLM-5.2
cc_switch catpaw-claude   # 切换到 CatPaw (伪装 Claude Sonnet 名)
cc_switch status          # 查看当前配置
claude
```

**方式 C：cc-switch 桌面应用**

1. 安装 [cc-switch](https://github.com/farion1231/cc-switch/releases)
2. 如果修改过 `bridge.conf.yaml`，先重新生成配置：
   ```bash
   python3 catpaw-bridge/generate-profiles.py
   ```
3. 导入配置文件 `catpaw-bridge/cc-switch-profiles.json`
4. 在 cc-switch 界面中选择 "CatPaw GLM-5.2" 并切换

### 4. 在 Codex CLI 中使用

**前提：已安装 Codex CLI**
```bash
npm install -g @openai/codex
```

**方式 A：自动配置（推荐）**
```bash
source catpaw-bridge/codex-switch.sh
codex_switch catpaw
codex
```

**方式 B：手动复制配置**
```bash
cp catpaw-bridge/codex-config.toml ~/.codex/config.toml
export OPENAI_API_KEY=sk-catpaw-bridge-key
codex
```

**切换回 OpenAI 官方：**
```bash
source catpaw-bridge/codex-switch.sh
codex_switch official
```

### 4. 验证

```bash
# 检查反向代理健康状态（含加密状态）
curl http://127.0.0.1:9000/health

# 检查 CLIProxyAPI 模型列表
curl -H "Authorization: Bearer sk-catpaw-bridge-key" http://127.0.0.1:8317/v1/models

# 测试非流式对话
curl http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer sk-catpaw-bridge-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}],"stream":false}'

# 测试流式对话
curl http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer sk-catpaw-bridge-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"用Python写hello world"}],"stream":true}'
```

## 工作原理

### 认证流程

1. CatPawAI IDE 登录后，SSO token 存储在 `~/Library/Application Support/CatPawAI/User/globalStorage/state.vscdb`（SQLite 数据库）
2. `catpaw_reverse_proxy.py` 从该数据库读取 `accessToken` 和用户信息（MIS ID）
3. 每个发往 CatPawAI API 的请求都会被注入：
   - `Cookie: 1d47d6ff96_passportid=<token>; f32a546874_ssoid=<token>`（双 Cookie 认证）
   - `Catpaw-Auth: <token>`（Header 认证）
   - `user-mis-id`, `mis-id`, `tenant`, `ide-type` 等 IDE 标识 Header

### 加密通信

CatPawAI API 要求请求体加密、响应体也加密。加密流程：

1. **请求加密**：
   - 生成随机 16 字节 AES-128 密钥
   - 用 AES-128-ECB 加密请求体（PKCS7 填充）
   - AES 密钥经 base64 编码后，用 RSA-OAEP-SHA1 公钥加密
   - 加密后的 AES 密钥放入 `encrypted-key` Header
   - 加密后的请求体作为 POST body 发送

2. **响应解密**：
   - 从响应 Header 读取 `encrypted-key`
   - 用 RSA-OAEP-SHA1 私钥解密得到 AES 密钥
   - 逐行解密 SSE 数据（AES-128-ECB）

3. **RSA 密钥来源**：
   - 公钥和私钥从 CatPawAI 扩展 `extension.js` 中提取
   - 密钥使用 XOR 加密（key: `ThisIsMyXorKey`）+ base64 编码存储
   - 运行时自动解密提取，无需手动配置

### Agent 模式适配

反代使用 `TOOLWINDOW_CHAT` 触发模式（Agent 模式），并做了以下适配：

1. **上下文合并**：CatPawAI 服务端不按 `conversationId` 缓存上下文，因此每次请求都将完整对话历史合并为单条用户消息（带 Human/Assistant 角色标签）
2. **会话追踪**：基于用户消息哈希实现 `conversationId` 复用，用于服务端日志追踪
3. **Agent XML 过滤**：禁用 `planPromptEnabled`，并通过正则过滤移除模型输出中可能出现的 `<function_calls>` 等 XML 标签，避免干扰 Claude Code 自身的工具调用
4. **流式稳定性**：正确识别 `: ping` 结束信号，发送标准 OpenAI `[DONE]` 标记

### 请求格式转换

OpenAI 格式 -> CatPawAI Agent 格式：

| OpenAI 字段 | CatPawAI 字段 | 说明 |
|---|---|---|
| `messages[]` | `messages[0].content` | 合并为单条消息（带角色标签） |
| - | `triggerMode` | `TOOLWINDOW_CHAT`（Agent 模式） |
| - | `userModelTypeCode` | 2 = 精确模式（可配置） |
| - | `conversationId` | 基于消息哈希的会话追踪 |
| - | `planPromptEnabled` | `false`（禁用规划提示） |
| `model` | - | 由反向代理使用，不传给上游 |

CatPawAI SSE -> OpenAI SSE：

| CatPawAI 字段 | OpenAI 字段 | 说明 |
|---|---|---|
| `choices[0].finishReason` | `choices[0].finish_reason` | camelCase -> snake_case |
| `choices[0].delta.content` | `choices[0].delta.content` | 直接映射（过滤 Agent XML） |
| `object: "chat.completion"` | `object: "chat.completion.chunk"` | 流式类型修正 |
| `lastOne: true` / `: ping` | `finish_reason: "stop"` + `[DONE]` | 结束标记转换 |

### 协议翻译

CLIProxyAPI 内置了 Anthropic <-> OpenAI 的双向翻译：
- Claude Code 发送 `/v1/messages`（Anthropic 格式）
- CLIProxyAPI 翻译为 `/v1/chat/completions`（OpenAI 格式）
- 转发给 CatPawAI 反向代理
- CatPawAI 返回 OpenAI 格式响应
- CLIProxyAPI 翻译回 Anthropic 格式
- Claude Code 收到 Anthropic 格式响应

### 模型别名

`config.yaml` 中配置了两个别名指向同一个上游模型：

| 别名 | 上游模型 | 用途 |
|---|---|---|
| `glm-5.2` | `glm-5.2` | 直接使用模型名 |
| `claude-sonnet-4-20250514` | `glm-5.2` | 伪装成 Claude Sonnet（某些工具硬编码模型名时有用） |

## 配置说明

### 统一配置文件 `bridge.conf.yaml`

所有配置集中在 `bridge.conf.yaml` 中，修改后重启服务即可生效：

```yaml
cliproxy:
  port: 8317                    # CLIProxyAPI 端口
  api_key: "sk-catpaw-bridge-key"

catpaw_proxy:
  port: 9000                    # 反向代理端口
  verbose: true                 # 详细日志

catpaw:
  api_host: "catpaw.meituan.com"
  data_dir: "~/Library/Application Support/CatPawAI"

model:
  name: "glm-5.2"
  type_code: 2                  # 1=快速, 2=精确

claude_code:
  model: "glm-5.2"
  masked_model: "claude-sonnet-4-20250514"
  official_base_url: "https://api.anthropic.com"
```

### 环境变量覆盖

环境变量优先于配置文件，可用于临时覆盖：

| 变量 | 配置文件路径 | 默认值 |
|---|---|---|
| `CLIPROXY_PORT` | `cliproxy.port` | `8317` |
| `CLIPROXY_API_KEY` | `cliproxy.api_key` | `sk-catpaw-bridge-key` |
| `CATPAW_PROXY_PORT` | `catpaw_proxy.port` | `9000` |
| `CATPAW_API_HOST` | `catpaw.api_host` | `catpaw.meituan.com` |
| `CATPAW_DATA_DIR` | `catpaw.data_dir` | `~/Library/Application Support/CatPawAI` |
| `CATPAW_PROXY_VERBOSE` | `catpaw_proxy.verbose` | `1` |
| `BRIDGE_CONFIG` | - | `bridge.conf.yaml` 的路径 |

## 停止服务

```bash
./start.sh --stop
```

## 排错

### 反向代理启动失败：state.vscdb not found

- 确保 CatPawAI IDE 已安装并登录
- 检查 `bridge.conf.yaml` 中 `catpaw.data_dir` 是否指向正确的数据目录
- 在 CatPawAI IDE 中重新登录 SSO

### 上游返回 406

- 确认 `catpaw.meituan.com` 可达（需内网或 VPN）
- 检查 RSA 密钥提取是否成功（查看日志中 "RSA keys extracted successfully"）
- 确认 SSO token 未过期（在 CatPawAI IDE 中重新登录刷新）
- 检查 `plugin-version` 是否与实际 CatPawAI 版本匹配

### Claude Code 报 401

- 检查 `ANTHROPIC_AUTH_TOKEN` 是否与 `bridge.conf.yaml` 中的 `cliproxy.api_key` 一致
- 默认 key 为 `sk-catpaw-bridge-key`

### Claude Code 报 502

- 检查 CatPawAI 反向代理是否在运行：`curl http://127.0.0.1:9000/health`
- 查看反向代理日志：`cat .logs/catpaw-proxy.log`
- 可能是 SSO token 过期，在 CatPawAI IDE 中重新登录

### 请求无响应或超时

- 检查 CLIProxyAPI 日志：`cat .logs/cliproxy.log`
- 确认 CatPawAI API 可达：`curl -I https://catpaw.meituan.com`
- 如果在内网外，需要连接美团 VPN

### Claude Code 中回答一次后丢失上下文

- 这是正常行为：反代每次发送完整对话历史（合并为单条消息），确保上下文不丢失
- 如果仍然丢失上下文，检查 Claude Code 的 `--max-turns` 设置
- 查看 `.logs/catpaw-proxy.log` 中的 `Cache HIT` / `New session` 日志确认会话追踪

### Claude Code 中 "Worked for 5s" 后无响应

- 通常是流式响应中断：查看 `.logs/catpaw-proxy.log` 中是否有 `: ping` 结束信号
- 确认 Agent XML 过滤是否生效（日志中不应出现 `<function_calls>` 内容）
- 尝试禁用 `planPromptEnabled`（已在配置中默认禁用）
