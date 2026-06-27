"""
CatPawAI Reverse Proxy — 模块化拆分包.

将 CatPawAI IDE 的 glm-5.2 模型暴露为标准 OpenAI 兼容 API，
供 CLIProxyAPI 调用，最终让 Claude Code 使用。

模块结构:
    config      — 配置加载 (bridge.conf.yaml + 环境变量 + 默认值)
    utils       — 共享工具函数
    crypto      — RSA 密钥提取 + AES-128-ECB 加解密
    auth        — SSO 认证 (从 state.vscdb 读取 accessToken)
    session     — 会话跟踪 (conversation ID 哈希)
    toolcall    — 工具调用注入 + 解析 (Prompt 注入 + <tool_call> 标签解析)
    translator  — OpenAI <-> CatPawAI 请求/响应格式转换
    sse         — SSE StreamResponse 辅助函数
    handlers    — HTTP handlers (chat/completions, models, health)
    app         — create_app() + main()

后续扩展 Codex 等新 provider 时，可新增 codex_*.py 模块或
providers/ 子包，复用 config / sse / utils 等共享层。
"""
