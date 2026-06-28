# CatPawAI Bridge 配置说明书

> 本文档面向拉取代码后需要在本机运行 CatPawAI Bridge 的开发者，涵盖环境准备、配置项详解、启动管理、客户端接入及常见排错。

---

## 目录

- [1. 系统概述](#1-系统概述)
- [2. 环境要求](#2-环境要求)
- [3. 目录结构](#3-目录结构)
- [4. 配置文件详解](#4-配置文件详解)
  - [4.1 统一配置 `bridge.conf.yaml`](#41-统一配置-bridgeconfyaml)
  - [4.2 CLIProxyAPI 配置 `config.yaml`](#42-cliproxyapi-配置-configyaml)
  - [4.3 Codex 配置模板 `codex-config.toml`](#43-codex-配置模板-codex-configtoml)
- [5. 环境变量](#5-环境变量)
- [6. 启动与管理](#6-启动与管理)
- [7. 客户端接入](#7-客户端接入)
  - [7.1 Claude Code](#71-claude-code)
  - [7.2 Codex CLI](#72-codex-cli)
- [8. 健康检查与验证](#8-健康检查与验证)
- [9. 日志说明](#9-日志说明)
- [10. 常见问题排错](#10-常见问题排错)
- [11. 高级配置](#11-高级配置)

---

## 1. 系统概述

CatPawAI Bridge 将美团 **CatPawAI IDE** 内置的 `glm-5.2` 模型反向代理出来，供 **Claude Code** 和 **Codex CLI** 使用。整体架构如下：

```
Claude Code / Codex CLI
    │  (Anthropic /v1/messages 或 OpenAI /responses 格式)
    ▼
CLIProxyAPI (:8317)          ← Go 服务，负责协议翻译 (Anthropic ↔ OpenAI)
    │  (OpenAI /v1/chat/completions 格式)
    ▼
CatPawAI 反向代理 (:9000)    ← Python 服务，注入 SSO 认证 + 加密通信
    │  (CatPawAI Agent 格式，AES-128-ECB + RSA-OAEP-SHA1 加密)
    ▼
CatPawAI API (catpaw.meituan.com/api/gpt/openai/stream)
    │
    ▼
glm-5.2 模型
```

### 工作流程

1. **Claude Code** 发送 Anthropic 格式请求到 `CLIProxyAPI (:8317)`
2. **CLIProxyAPI** 将 Anthropic 格式翻译为 OpenAI 格式，转发给反向代理
3. **反向代理** 从 CatPawAI IDE 的 `state.vscdb` 读取 SSO Token，注入 Cookie 和 Header，加密请求体后发送给 CatPawAI API
4. **CatPawAI API** 返回加密的 SSE 响应，反向代理解密并转换为 OpenAI 格式
5. **CLIProxyAPI** 将 OpenAI 响应翻译回 Anthropic 格式返回给 Claude Code

---

## 2. 环境要求

### 必需环境

| 依赖 | 版本要求 | 安装方式 | 用途 |
|------|---------|---------|------|
| **CatPawAI IDE** | 已登录 SSO | [美团内网下载](https://catpaw.meituan.com) | 提供 SSO Token 和 RSA 密钥 |
| **Python** | 3.8+ | `brew install python3` | 运行反向代理 |
| **Go** | 1.24+ | `brew install go` | 编译 CLIProxyAPI |
| **美团内网/VPN** | 可达 `catpaw.meituan.com` | — | 访问上游 API |

### Python 依赖（自动安装）

`start.sh` 会自动检测并安装以下依赖：

| 包 | 用途 |
|----|------|
| `aiohttp` | 异步 HTTP 服务器/客户端 |
| `pycryptodome` | AES-128-ECB + RSA-OAEP-SHA1 加解密 |
| `pyyaml` | 解析 `bridge.conf.yaml` 配置文件 |

> 也可手动安装：`pip3 install aiohttp pycryptodome pyyaml`

### 可选依赖

| 工具 | 用途 |
|------|------|
| **Claude Code** | Anthropic 官方 CLI 工具 |
| **Codex CLI** | OpenAI 官方 CLI 工具，`npm install -g @openai/codex` |
| **cc-switch 桌面应用** | Claude Code 配置切换 GUI，[下载地址](https://github.com/farion1231/cc-switch/releases) |

### 平台支持

当前仅支持 **macOS**，原因：
- SSO Token 存储路径硬编码为 `~/Library/Application Support/CatPawAI/`
- RSA 密钥从 `/Applications/CatPawAI.app/Contents/Resources/app/extensions/` 提取

---

## 3. 目录结构

```
catpaw-bridge/
├── bridge.conf.yaml          # ★ 统一配置文件（所有配置的唯一数据源）
├── config.yaml               # CLIProxyAPI 配置（端口、模型别名、上游 Provider）
├── catpaw_reverse_proxy.py   # 反向代理入口脚本
├── proxy/                    # 反向代理核心代码（Python 包）
│   ├── config.py             # 配置加载（bridge.conf.yaml + 环境变量 + 默认值）
│   ├── app.py                # aiohttp 应用创建 + main() 入口
│   ├── auth.py               # SSO 认证（从 state.vscdb 读取 accessToken）
│   ├── crypto.py             # RSA 密钥提取 + AES-128-ECB 加解密
│   ├── translator.py         # OpenAI ↔ CatPawAI 请求/响应格式转换
│   ├── toolcall.py           # 工具调用注入 + <tool_call> 标签解析
│   ├── handlers.py           # HTTP 处理器（/v1/chat/completions, /v1/models, /health）
│   ├── session.py            # 会话跟踪（conversation ID 哈希）
│   ├── sse.py                # SSE StreamResponse 辅助函数
│   ├── compactor.py          # 上下文压缩
│   ├── memory.py             # 记忆管理
│   └── utils.py              # 共享工具函数
├── start.sh                  # ★ 一键启动/停止脚本
├── cc-switch.sh              # Claude Code 配置切换脚本
├── codex-switch.sh           # Codex CLI 配置切换脚本
├── codex-config.toml         # Codex CLI 配置模板（手动复制用）
├── cc-switch-profiles.json   # cc-switch 桌面应用导入配置
├── generate-profiles.py      # 从 bridge.conf.yaml 生成 cc-switch-profiles.json
├── static/
│   └── management.html       # 管理面板 HTML
├── .logs/                    # 运行日志目录（自动创建）
│   ├── catpaw-proxy.log      # 反向代理日志
│   └── cliproxy.log          # CLIProxyAPI 日志
└── .pids/                    # PID 文件目录（自动创建）
    ├── catpaw-proxy.pid
    └── cliproxy.pid
```

---

## 4. 配置文件详解

### 4.1 统一配置 `bridge.conf.yaml`

**这是所有配置的唯一数据源**。反向代理、CLIProxyAPI 启动脚本、cc-switch、codex-switch 都从此文件读取配置。

配置优先级：**环境变量 > `bridge.conf.yaml` > 代码内默认值**

完整配置项说明：

```yaml
# ============================================================================
# 网络配置
# ============================================================================
cliproxy:
  port: 8317                      # CLIProxyAPI 监听端口
  api_key: "sk-catpaw-bridge-key" # Claude Code / Codex CLI 认证密钥
                                  # ⚠️ 必须与 config.yaml 中 api-keys 一致

catpaw_proxy:
  port: 9000                      # CatPawAI 反向代理监听端口
  verbose: true                   # 详细日志（true/false）
  strip_tool_definitions: false   # 是否剥离工具定义注入
                                  # false（默认）: 将 tools 数组注入 CatPawAI 请求
                                  # true: 不注入 tools 定义，减小请求体
                                  #       仍保留 tool 消息转换 + <tool_call> 解析
                                  #       可在 system prompt 中手动描述工具

# ============================================================================
# 请求体大小限制
# ============================================================================
limits:
  # CatPawAI 上游对加密请求体有大小限制（约 128KB）
  # 超过此值上游会直接关闭连接，不返回任何 HTTP 响应
  max_encrypted_body: 180000      # 加密后最大字节数（警告阈值，默认 180000）
  # 系统消息截断阈值
  # Claude Code 的 system prompt 含完整工具描述（通常 80-120KB）
  max_system_content: 8000        # 系统消息最大字符数（默认 8000）
  # 普通消息截断阈值（如 Read 工具返回的大文件内容）
  max_message_content: 15000      # 非系统消息最大字符数（默认 15000）

# ============================================================================
# CatPawAI 上游配置
# ============================================================================
catpaw:
  api_host: "catpaw.meituan.com"                              # CatPawAI API 主机
  data_dir: "~/Library/Application Support/CatPawAI"          # CatPawAI IDE 数据目录
                                                              # 包含 state.vscdb（SSO Token）
  sso_client_id: "1d47d6ff96"      # SSO Cookie 第一个 client_id
  sso_client_id_2: "f32a546874"    # SSO Cookie 第二个 client_id
  tenant_id: "5282fa6645"          # 租户 ID
  need_passport_id: true           # 是否使用 passportid Cookie
                                    # tenant 5282fa6645 使用 passportid cookie

# ============================================================================
# 模型配置
# ============================================================================
model:
  name: "glm-5.2"                 # CatPawAI 上游模型名
  type_code: 2                    # 模型类型: 1=快速, 2=精确
  aliases:                        # 伪装别名列表
    - "glm-5.2"                   # 直接使用模型名
    - "claude-sonnet-4-20250514"  # 伪装成 Claude Sonnet（某些工具硬编码模型名时有用）

# ============================================================================
# Claude Code 配置（cc-switch.sh 读取）
# ============================================================================
claude_code:
  model: "glm-5.2"                            # 直接使用模型名
  small_model: "glm-5.2"                      # 小模型（用于快速任务）
  masked_model: "claude-sonnet-4-20250514"    # 伪装模式模型名
  masked_small_model: "claude-sonnet-4-20250514"
  # 官方 API 配置（切换回官方时用）
  official_base_url: "https://api.anthropic.com"
  official_model: "claude-sonnet-4-20250514"
  official_small_model: "claude-3-5-haiku-20241022"

# ============================================================================
# Codex CLI 配置（codex-switch.sh 读取）
# ============================================================================
codex:
  model: "gpt-5.5"               # Codex CLI 默认模型别名（映射到 glm-5.2）
  wire_api: "responses"          # Codex 强制要求 responses = /v1/responses
                                 # CLIProxyAPI 翻译层已支持此格式
  official_model: "gpt-4.1"      # 切换回官方时的模型
  official_base_url: "https://api.openai.com/v1"
```

#### 常见修改场景

| 场景 | 修改项 |
|------|--------|
| 修改端口 | `cliproxy.port` + `catpaw_proxy.port` |
| 修改认证密钥 | `cliproxy.api_key`（同时修改 `config.yaml` 中的 `api-keys`） |
| 切换模型类型 | `model.type_code`：`1`=快速，`2`=精确 |
| 减小请求体大小 | `catpaw_proxy.strip_tool_definitions: true` |
| 调整截断阈值 | `limits.max_system_content` / `limits.max_message_content` |
| 非 macOS 路径 | `catpaw.data_dir` |

---

### 4.2 CLIProxyAPI 配置 `config.yaml`

此文件是 CLIProxyAPI（Go 服务）的配置，定义上游 Provider、模型别名、路由策略等。

```yaml
# 绑定地址
host: "127.0.0.1"        # 仅本地访问
port: 8317               # 监听端口（与 bridge.conf.yaml 中 cliproxy.port 一致）

# 调试模式
debug: true              # 输出详细日志

# 认证
auth-dir: "~/.cli-proxy-api"
api-keys:
  - "sk-catpaw-bridge-key"   # ⚠️ 必须与 bridge.conf.yaml 中 cliproxy.api_key 一致

# 请求重试
request-retry: 3             # 失败重试次数
max-retry-credentials: 0     # 最大尝试不同凭据数（0=全部尝试）

# 禁用冷却机制
# 防止 401 时挂起客户端 30 分钟
disable-cooling: true

# 管理面板
remote-management:
  allow-remote: false                    # 仅允许本地管理
  secret-key: "$2a$10$..."               # bcrypt 哈希密钥

# 上游 Provider 配置
openai-compatibility:
  - name: "catpaw"
    base-url: "http://127.0.0.1:9000/v1"    # 指向反向代理
    disable-cooling: true                   # 禁用冷却
    api-key-entries:
      - api-key: "catpaw-internal"          # 内部密钥（反向代理不验证）
    models:
      # 直接使用模型名
      - name: "glm-5.2"
        alias: "glm-5.2"
        force-mapping: true                 # 重写响应中的 model 字段
      # 伪装 Claude Sonnet（某些工具硬编码模型名时有用）
      - name: "glm-5.2"
        alias: "claude-sonnet-4-20250514"
        force-mapping: true
      # Codex CLI 别名（不启用 force-mapping，因 Responses API SSE 格式问题）
      - name: "glm-5.2"
        alias: "o3"
      - name: "glm-5.2"
        alias: "o4-mini"
      - name: "glm-5.2"
        alias: "gpt-4.1"
      - name: "glm-5.2"
        alias: "gpt-4.1-mini"
      - name: "glm-5.2"
        alias: "gpt-5-codex"
      - name: "glm-5.2"
        alias: "gpt-5.5"

# 路由策略
routing:
  strategy: "round-robin"      # 轮询策略
  session-affinity: false      # 不启用会话亲和性
```

#### `force-mapping` 说明

| 值 | 行为 |
|----|------|
| `true` | 将上游响应中的 `model` 字段重写为客户端请求的别名 |
| `false`（默认） | 保留上游原始模型名 |

> Codex CLI 的别名不启用 `force-mapping`，因为 `StreamRewriter` 无法处理 Responses API 的 SSE 格式。

---

### 4.3 Codex 配置模板 `codex-config.toml`

Codex CLI 的配置模板，可手动复制到 `~/.codex/config.toml`，或通过 `codex-switch.sh` 自动生成。

```toml
model_provider = "catpaw"
model = "glm-5.2"                  # 默认模型
model_reasoning_effort = "high"    # 推理强度
wire_api = "responses"             # 使用 Responses API
disable_response_storage = true    # CatPawAI 不支持服务端存储

[model_providers.catpaw]
name = "CatPawAI Bridge"
base_url = "http://127.0.0.1:8317"  # 不带 /v1 后缀
wire_api = "responses"
api_key = "sk-catpaw-bridge-key"    # 与 bridge.conf.yaml 中一致
```

---

## 5. 环境变量

环境变量优先于 `bridge.conf.yaml`，可用于临时覆盖配置。

### 反向代理相关

| 环境变量 | 对应配置项 | 默认值 | 说明 |
|---------|-----------|--------|------|
| `BRIDGE_CONFIG` | — | `bridge.conf.yaml` | 配置文件路径 |
| `CATPAW_PROXY_HOST` | — | `127.0.0.1` | 反向代理监听地址 |
| `CATPAW_PROXY_PORT` | `catpaw_proxy.port` | `9000` | 反向代理监听端口 |
| `CATPAW_PROXY_VERBOSE` | `catpaw_proxy.verbose` | `1` | 详细日志 |
| `STRIP_TOOL_DEFINITIONS` | `catpaw_proxy.strip_tool_definitions` | `false` | 剥离工具定义 |
| `CATPAW_API_HOST` | `catpaw.api_host` | `catpaw.meituan.com` | 上游 API 主机 |
| `CATPAW_DATA_DIR` | `catpaw.data_dir` | `~/Library/Application Support/CatPawAI` | IDE 数据目录 |
| `SSO_CLIENT_ID` | `catpaw.sso_client_id` | `1d47d6ff96` | SSO Cookie ID 1 |
| `SSO_CLIENT_ID_2` | `catpaw.sso_client_id_2` | `f32a546874` | SSO Cookie ID 2 |
| `TENANT_ID` | `catpaw.tenant_id` | `5282fa6645` | 租户 ID |
| `NEED_PASSPORT_ID` | `catpaw.need_passport_id` | `true` | 使用 passportid Cookie |

### CLIProxyAPI 相关

| 环境变量 | 对应配置项 | 默认值 | 说明 |
|---------|-----------|--------|------|
| `CLIPROXY_PORT` | `cliproxy.port` | `8317` | CLIProxyAPI 监听端口 |
| `CLIPROXY_API_KEY` | `cliproxy.api_key` | `sk-catpaw-bridge-key` | 认证密钥 |
| `CLIPROXY_HOST` | — | `127.0.0.1` | CLIProxyAPI 主机地址（cc-switch 用） |

### 使用示例

```bash
# 临时修改端口
CLIPROXY_PORT=9001 CATPAW_PROXY_PORT=9002 ./start.sh

# 临时关闭详细日志
CATPAW_PROXY_VERBOSE=0 ./start.sh

# 使用自定义配置文件
BRIDGE_CONFIG=/path/to/my-bridge.conf.yaml ./start.sh
```

---

## 6. 启动与管理

### 一键启动

```bash
cd catpaw-bridge
./start.sh
```

脚本自动完成：
1. 检查 Python 依赖（aiohttp, pycryptodome, pyyaml），缺失自动安装
2. 编译 CLIProxyAPI（如二进制不存在）
3. 启动 CatPawAI 反向代理（`:9000`）
4. 启动 CLIProxyAPI（`:8317`）
5. 显示连接信息和客户端配置指引

### 停止服务

```bash
./start.sh --stop
```

### 查看状态

```bash
./start.sh --status
```

输出示例：
```
========================================
  CatPawAI Bridge 状态
========================================

  ● catpaw-proxy  (PID: 12345, :9000)
  ● cliproxy      (PID: 12346, :8317)
```

### 重新编译 CLIProxyAPI

如需重新编译（修改了 Go 代码后），先删除旧二进制：

```bash
rm ../cli-proxy-api
./start.sh
```

或手动编译：

```bash
cd ..  # 回到项目根目录
go build -o cli-proxy-api ./cmd/server/
```

### 配置修改后生效

修改 `bridge.conf.yaml` 或 `config.yaml` 后，需重启服务：

```bash
./start.sh --stop && ./start.sh
```

---

## 7. 客户端接入

### 7.1 Claude Code

#### 方式 A：环境变量（最简单）

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8317
export ANTHROPIC_AUTH_TOKEN=sk-catpaw-bridge-key
export ANTHROPIC_MODEL=glm-5.2
claude
```

#### 方式 B：cc-switch 脚本（自动读取配置）

```bash
source catpaw-bridge/cc-switch.sh

cc_switch catpaw          # 切换到 CatPaw（直接模型名 glm-5.2）
cc_switch catpaw-claude   # 切换到 CatPaw（伪装 claude-sonnet-4-20250514）
cc_switch official        # 切换到 Claude 官方 API
cc_switch status          # 查看当前配置
claude
```

#### 方式 C：cc-switch 桌面应用

1. 安装 [cc-switch](https://github.com/farion1231/cc-switch/releases)
2. 如修改过 `bridge.conf.yaml`，先重新生成配置：
   ```bash
   python3 catpaw-bridge/generate-profiles.py
   ```
3. 导入配置文件 `catpaw-bridge/cc-switch-profiles.json`
4. 在 cc-switch 界面中选择 "CatPaw glm-5.2" 并切换

### 7.2 Codex CLI

#### 前提：安装 Codex CLI

```bash
npm install -g @openai/codex
```

#### 方式 A：自动配置（推荐）

```bash
source catpaw-bridge/codex-switch.sh
codex_switch catpaw       # 自动生成 ~/.codex/config.toml
codex
```

#### 方式 B：手动复制配置

```bash
cp catpaw-bridge/codex-config.toml ~/.codex/config.toml
export OPENAI_API_KEY=sk-catpaw-bridge-key
codex
```

#### 切换回 OpenAI 官方

```bash
source catpaw-bridge/codex-switch.sh
codex_switch official     # 恢复 ~/.codex/config.toml 为官方配置
# 需设置 OPENAI_API_KEY 为你的官方密钥
```

---

## 8. 健康检查与验证

### 检查反向代理健康状态

```bash
curl http://127.0.0.1:9000/health
```

正常响应：
```json
{
  "status": "ok",
  "auth": {"mis_id": "your_mis_id", "token_age": 12},
  "encryption": {"enabled": true},
  "upstream": "https://catpaw.meituan.com"
}
```

### 检查 CLIProxyAPI 模型列表

```bash
curl -H "Authorization: Bearer sk-catpaw-bridge-key" http://127.0.0.1:8317/v1/models
```

### 测试非流式对话

```bash
curl http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer sk-catpaw-bridge-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}],"stream":false}'
```

### 测试流式对话

```bash
curl http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer sk-catpaw-bridge-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"用Python写hello world"}],"stream":true}'
```

---

## 9. 日志说明

日志文件位于 `catpaw-bridge/.logs/` 目录：

| 文件 | 内容 |
|------|------|
| `catpaw-proxy.log` | 反向代理日志（认证状态、加密状态、请求/响应详情） |
| `cliproxy.log` | CLIProxyAPI 日志（协议翻译、路由、重试） |

### 查看实时日志

```bash
tail -f catpaw-bridge/.logs/catpaw-proxy.log
tail -f catpaw-bridge/.logs/cliproxy.log
```

### 关键日志行说明

| 日志内容 | 含义 |
|---------|------|
| `Config loaded from .../bridge.conf.yaml` | 配置加载成功 |
| `RSA keys extracted successfully` | RSA 密钥提取成功（加密可用） |
| `Auth OK: mis_id=xxx` | SSO 认证成功 |
| `WARNING: Auth pre-load failed` | SSO Token 读取失败（需在 IDE 中重新登录） |
| `Got 401 from upstream` | SSO Token 过期，正在自动刷新重试 |
| `Stream ended, chunks sent: N` | 流式响应正常结束 |
| `WARNING: Large request body` | 请求体过大，上游可能拒绝 |
| `Encryption will be disabled` | RSA 密钥提取失败，将发送明文（可能失败） |

---

## 10. 常见问题排错

### 问题 1：`state.vscdb not found`

**原因**：CatPawAI IDE 未安装或未登录

**解决**：
1. 确保 CatPawAI IDE 已安装并登录 SSO
2. 检查 `bridge.conf.yaml` 中 `catpaw.data_dir` 路径是否正确
3. 默认路径：`~/Library/Application Support/CatPawAI/User/globalStorage/state.vscdb`
4. 在 CatPawAI IDE 中重新登录 SSO 刷新 Token

### 问题 2：上游返回 406

**原因**：认证信息不正确或网络不通

**解决**：
1. 确认 `catpaw.meituan.com` 可达（需内网或 VPN）
2. 检查日志中是否有 `RSA keys extracted successfully`
3. 确认 SSO Token 未过期（在 CatPawAI IDE 中重新登录）
4. 检查 `plugin-version` 是否与实际 CatPawAI 版本匹配（见 `proxy/auth.py` 中 `build_catpaw_headers`）

### 问题 3：Claude Code 报 401

**原因**：认证密钥不匹配

**解决**：
- 检查 `ANTHROPIC_AUTH_TOKEN` 是否与 `bridge.conf.yaml` 中的 `cliproxy.api_key` 一致
- 默认 key 为 `sk-catpaw-bridge-key`
- 同时检查 `config.yaml` 中的 `api-keys` 列表

### 问题 4：Claude Code 报 502

**原因**：反向代理未运行或上游错误

**解决**：
1. 检查反向代理是否在运行：`curl http://127.0.0.1:9000/health`
2. 查看反向代理日志：`cat .logs/catpaw-proxy.log`
3. 可能是 SSO Token 过期，在 CatPawAI IDE 中重新登录
4. 检查网络是否可达美团内网

### 问题 5：请求无响应或超时

**原因**：网络问题或上游不可达

**解决**：
1. 检查 CLIProxyAPI 日志：`cat .logs/cliproxy.log`
2. 确认 CatPawAI API 可达：`curl -I https://catpaw.meituan.com`
3. 如果在内网外，需要连接美团 VPN

### 问题 6：回答一次后丢失上下文

**说明**：这是正常行为。反向代理每次发送完整对话历史（合并为单条消息），确保上下文不丢失。

**排查**：
- 查看 `.logs/catpaw-proxy.log` 中的 `Cache HIT` / `New session` 日志确认会话追踪
- 如果仍然丢失上下文，检查 Claude Code 的 `--max-turns` 设置

### 问题 7：Claude Code 中 "Worked for 5s" 后无响应

**原因**：流式响应中断

**解决**：
1. 查看 `.logs/catpaw-proxy.log` 中是否有 `: ping` 结束信号
2. 确认 Agent XML 过滤是否生效（日志中不应出现 `<function_calls>` 内容）
3. 检查 `planPromptEnabled` 是否已禁用（默认禁用）

### 问题 8：RSA 密钥提取失败

**原因**：CatPawAI 扩展路径变化或版本更新

**解决**：
1. 确认 CatPawAI IDE 安装在 `/Applications/CatPawAI.app`
2. 检查扩展文件是否存在：
   ```bash
   ls -la /Applications/CatPawAI.app/Contents/Resources/app/extensions/mt-idekit.mt-idekit-code/out/extension.js
   ```
3. 如果 CatPawAI 更新了扩展，可能需要更新密钥提取正则（见 `proxy/crypto.py`）

### 问题 9：请求体过大导致上游关闭连接

**原因**：CatPawAI 上游对加密请求体有大小限制（约 128KB）

**解决**：
1. 在 `bridge.conf.yaml` 中启用 `strip_tool_definitions: true`，减少请求体大小
2. 调整截断阈值：减小 `limits.max_system_content` 和 `limits.max_message_content`
3. 查看日志中的 `WARNING: Large request body` 警告

---

## 11. 高级配置

### 11.1 添加额外上游 Provider

`config.yaml` 支持同时配置多个 Provider。取消注释相关配置即可：

```yaml
# OpenAI 官方
openai-compatibility:
  - name: "openai"
    base-url: "https://api.openai.com/v1"
    api-key-entries:
      - api-key: "sk-your-openai-key"
    models:
      - name: "gpt-4o"
        alias: "gpt-4o"

# DeepSeek
openai-compatibility:
  - name: "deepseek"
    base-url: "https://api.deepseek.com/v1"
    api-key-entries:
      - api-key: "sk-your-deepseek-key"
    models:
      - name: "deepseek-chat"
        alias: "deepseek-chat"
      - name: "deepseek-reasoner"
        alias: "deepseek-reasoner"

# Claude API
claude-api-key:
  - api-key: "sk-ant-your-claude-key"
    models:
      - name: "claude-sonnet-4-20250514"
        alias: "claude-sonnet-4"
```

### 11.2 管理面板

CLIProxyAPI 内置管理面板，通过 `config.yaml` 中的 `remote-management` 配置：

```yaml
remote-management:
  allow-remote: false              # 是否允许远程管理（建议 false）
  secret-key: "your-secret-key"    # 管理密钥（明文会自动 bcrypt 哈希）
  disable-control-panel: false     # 是否禁用管理面板
```

访问地址：`http://127.0.0.1:8317/v0/management/`

### 11.3 模型别名机制

`config.yaml` 中的模型别名允许客户端使用不同的模型名访问同一个上游模型：

| 客户端请求模型名 | 实际上游模型 | `force-mapping` | 说明 |
|-----------------|-------------|-----------------|------|
| `glm-5.2` | `glm-5.2` | `true` | 直接使用模型名 |
| `claude-sonnet-4-20250514` | `glm-5.2` | `true` | 伪装 Claude Sonnet |
| `o3` / `o4-mini` | `glm-5.2` | `false` | Codex CLI 兼容 |
| `gpt-4.1` / `gpt-4.1-mini` | `glm-5.2` | `false` | Codex CLI 兼容 |
| `gpt-5-codex` / `gpt-5.5` | `glm-5.2` | `false` | Codex CLI 兼容 |

### 11.4 认证流程详解

1. CatPawAI IDE 登录后，SSO Token 存储在 `state.vscdb`（SQLite 数据库）的 `catpaw.mt-authentication` 键中
2. `proxy/auth.py` 从该数据库读取 `accessToken` 和用户信息（MIS ID）
3. Token 缓存 60 秒，过期后自动重新读取
4. 收到 401 时自动失效缓存并重试一次
5. 每个请求注入以下认证信息：
   - `Cookie`: 双 Cookie 认证（`passportid` + `ssoid`）
   - `Catpaw-Auth`: Header 认证
   - `user-mis-id`, `mis-id`, `tenant`, `ide-type` 等 IDE 标识 Header

### 11.5 加密通信详解

CatPawAI API 要求请求体加密、响应体也加密：

**请求加密流程**：
1. 生成随机 16 字节 AES-128 密钥
2. 用 AES-128-ECB 加密请求体（PKCS7 填充）
3. AES 密钥经 base64 编码后，用 RSA-OAEP-SHA1 公钥加密
4. 加密后的 AES 密钥放入 `encrypted-key` Header
5. 加密后的请求体作为 POST body 发送

**响应解密流程**：
1. 从响应 Header 读取 `encrypted-key`
2. 用 RSA-OAEP-SHA1 私钥解密得到 AES 密钥
3. 逐行解密 SSE 数据（AES-128-ECB）

**RSA 密钥来源**：
- 公钥和私钥从 CatPawAI 扩展 `extension.js` 中提取
- 密钥使用 XOR 加密（key: `ThisIsMyXorKey`）+ base64 编码存储
- 运行时自动解密提取，无需手动配置
- 密钥路径：`/Applications/CatPawAI.app/Contents/Resources/app/extensions/mt-idekit.mt-idekit-code/out/extension.js`

### 11.6 `strip_tool_definitions` 详解

当 Claude Code 发送包含 `tools` 数组的请求时，反代会将工具定义注入到 CatPawAI 请求中。但 CatPawAI 上游对请求体大小有限制（约 128KB），完整的工具定义可能导致请求被拒绝。

| 值 | 行为 | 适用场景 |
|----|------|---------|
| `false`（默认） | 将 tools 数组注入 CatPawAI 请求 | 请求体较小时 |
| `true` | 不注入 tools 定义，但仍保留 tool 消息转换和 `<tool_call>` 响应解析 | 请求体过大时，可在 system prompt 中手动描述工具 |

---

## 附录：快速配置清单

新开发者拉取代码后的操作清单：

- [ ] 1. 安装 CatPawAI IDE 并登录美团 SSO
- [ ] 2. 确认 Python 3.8+ 已安装：`python3 --version`
- [ ] 3. 确认 Go 1.24+ 已安装：`go version`
- [ ] 4. 确认可达美团内网（或已连接 VPN）
- [ ] 5. 进入 catpaw-bridge 目录：`cd catpaw-bridge`
- [ ] 6. 检查 `bridge.conf.yaml` 配置（通常默认值即可使用）
- [ ] 7. 启动服务：`./start.sh`
- [ ] 8. 验证健康状态：`curl http://127.0.0.1:9000/health`
- [ ] 9. 配置 Claude Code（三选一）：
  - [ ] 环境变量方式
  - [ ] `source cc-switch.sh && cc_switch catpaw`
  - [ ] cc-switch 桌面应用导入 `cc-switch-profiles.json`
- [ ] 10. （可选）配置 Codex CLI：`source codex-switch.sh && codex_switch catpaw`
- [ ] 11. 开始使用：`claude` 或 `codex`
