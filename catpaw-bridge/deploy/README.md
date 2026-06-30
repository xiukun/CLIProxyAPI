# CatPawAI Bridge 服务器部署

## 链路

```
Claude Code → 服务器:40037 (CLIProxyAPI) → catpaw-bridge:9000 (内部) → catpaw.meituan.com
                         ↑
                   本机 curl -T 推送 token (端口 40036)
```

## 服务器目录

```
/userdata/docker/catpaw/
├── docker-compose.yml
├── config.yaml
├── upload-server.py
└── extension.js          # 首次手动复制，之后不变
```

## 端口

| 服务 | 端口 | 用途 |
|------|------|------|
| CLIProxyAPI | 40037 | Claude Code 连接 |
| uploader | 40036 | 本机推送 token |

## 关键配置

### base-url 必须包含 `/v1`

CLIProxyAPI 内部拼接路径时用的是 `/chat/completions`（不带 `/v1`），所以 base-url 必须包含 `/v1`：

```yaml
# 正确
base-url: "http://catpaw-bridge:9000/v1"

# 错误（会导致 404）
base-url: "http://catpaw-bridge:9000"
```

## 部署步骤

### 1. 本机导出镜像

```bash
cd /Users/enmaai/wplace/sourcecode/CLIProxy
docker save cliproxy-catpaw-bridge:latest eceasy/cli-proxy-api:latest | gzip > catpaw-full.tar.gz
```

### 2. 传输到服务器

```bash
SERVER="user@server"
REMOTE="/userdata/docker/catpaw"

ssh $SERVER "mkdir -p $REMOTE"

scp catpaw-bridge/deploy/docker-compose.yml $SERVER:$REMOTE/
scp catpaw-bridge/deploy/config.yaml $SERVER:$REMOTE/
scp catpaw-bridge/deploy/upload-server.py $SERVER:$REMOTE/
scp catpaw-full.tar.gz $SERVER:$REMOTE/

# extension.js 只需传一次
scp /Applications/CatPawAI.app/Contents/Resources/app/extensions/mt-idekit.mt-idekit-code/out/extension.js $SERVER:$REMOTE/
```

### 3. 服务器启动

```bash
ssh $SERVER
cd /userdata/docker/catpaw
docker load < catpaw-full.tar.gz
docker compose up -d
```

### 4. Claude Code 使用

```bash
export ANTHROPIC_BASE_URL=http://server-ip:40037
export ANTHROPIC_AUTH_TOKEN=sk-catpaw-bridge-key
claude
```

### 5. 本机同步 token

```bash
# 手动推送
curl -T "$HOME/Library/Application Support/CatPawAI/User/globalStorage/state.vscdb" http://server-ip:40036/state.vscdb

# crontab 每 10 分钟
# */10 * * * * curl -sT "$HOME/Library/Application Support/CatPawAI/User/globalStorage/state.vscdb" http://server-ip:40036/state.vscdb
```

## 改动概览

### 修改的文件

1. **`catpaw-bridge/proxy/crypto.py`**
   - 添加 `import os`
   - extension.js 路径改为从环境变量 `CATPAW_EXTENSION_JS` 读取，默认值不变

2. **`docker-compose.yml`**
   - 新增 `catpaw-bridge` 服务
   - 挂载宿主机的 `state.vscdb` 和 `extension.js`（只读）
   - 端口映射 `9001:9000`

### 新增的文件

3. **`catpaw-bridge/Dockerfile`**
   - Python 3.11-slim 基础镜像
   - 安装 aiohttp + pycryptodome + pyyaml

4. **`catpaw-bridge/.dockerignore`**
   - 排除测试文件、脚本、文档

5. **`catpaw-bridge/deploy/`** 目录（服务器部署用）
   - `docker-compose.yml` - 三个容器编排（CLIProxyAPI + catpaw-bridge + uploader）
   - `config.yaml` - CLIProxyAPI 配置
   - `upload-server.py` - 文件上传服务（替代 sync 脚本）
   - `README.md` - 部署说明（本文件）
