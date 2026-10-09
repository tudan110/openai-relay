# OpenAI Relay

独立的 OpenAI 兼容中继服务：对外提供 Responses API，并提供用户 key、配额、用量统计和管理后台。服务默认监听 `55302`，使用独立的 SQLite 数据库，不依赖 `claude-relay`。

## 接口

- `POST /v1/responses`：OpenAI Responses API
- `POST /v1/chat/completions`：兼容常见 OpenAI 客户端
- `GET /v1/models`：上游模型列表
- `GET /health`：健康检查
- `GET /stats`：用户或管理员用量页面
- `GET /admin`：管理员用户管理页面

客户端通过 `cc-switch` 配置：

```text
Base URL: http://<relay-host>:55302/v1
API Key:  管理员分配的用户 key
```

后台创建用户后，将生成的 key 交给用户；用户不需要编辑 `auth.json`。中继 key 只用于访问本服务，和上游 OpenAI 凭据是两套不同的凭据。

## 上游认证

本项目支持两种明确且互斥的上游模式：

### 服务器 Codex 登录态

服务器先通过正常 Codex CLI 流程登录，PM2 以同一用户运行，并配置：

```text
OPENAI_UPSTREAM_AUTH_MODE=codex_auth_file
OPENAI_UPSTREAM_HOST=chatgpt.com
OPENAI_UPSTREAM_PREFIX=/backend-api/codex
CODEX_AUTH_FILE=/home/ubuntu/.codex/auth.json
```

中继只读取服务器上的 Codex 会话，将 access token 和 `ChatGPT-Account-Id` 注入上游请求；这些值不会返回给本机用户、写入 SQLite 或日志。本机仍只使用后台分配的 relay key。`auth.json` 必须由运行 PM2 的用户拥有且权限为 `0600`，不得复制到代码库或提交到 Git。

统计页会每 60 秒通过服务器端的已登录 Codex 会话查询一次上游账号额度，并只显示周期名称、剩余比例和重置时间。该额度由所有 relay 用户共享；原始上游响应、access token、account ID 不会进入浏览器、SQLite 或日志。上游不可用或返回格式变化时，页面会明确显示额度暂不可用，不会根据 relay 用量推测额度。

Codex 模式只代理已确认支持的 Responses 接口；上游要求 `input` 使用列表、`store=false` 且 `stream=true`，因此 relay 会自动设置 `store=false`，并移除 Claude Desktop 等 OpenAI 兼容客户端可能附带、但 Codex backend 不支持的 `max_output_tokens`。对非流式请求返回 400，避免错误地把 SSE 当成 JSON。它不把 Codex 登录态冒充标准 OpenAI API key，也不绕过上游登录、订阅、速率限制或风控控制。

### 标准 OpenAI API key

标准模式配置：

```text
OPENAI_UPSTREAM_AUTH_MODE=api_key
OPENAI_UPSTREAM_HOST=api.openai.com
OPENAI_UPSTREAM_PREFIX=/v1
OPENAI_API_KEY=<standard-openai-api-key>
```

两种模式不会互相回退；没有可用上游凭据时请求返回 `503`。
## 本地运行

```bash
RELAY_ADMIN_KEY='change-me' OPENAI_API_KEY='upstream-key' python3 app.py
```

常用环境变量：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `RELAY_PORT` | `55302` | 监听端口 |
| `RELAY_DB` | `./relay.db` | SQLite 文件 |
| `RELAY_ADMIN_KEY` | 启动时随机生成 | 管理后台和全员统计 key |
| `RELAY_STRICT` | `0` | `1` 时拒绝未分配的 key |
| `RELAY_PROXY_HOST` | `127.0.0.1` | HTTP CONNECT 代理 |
| `RELAY_PROXY_PORT` | `7897` | HTTP CONNECT 代理端口 |
| `OPENAI_UPSTREAM_AUTH_MODE` | `api_key` | `api_key` 或 `codex_auth_file` |
| `OPENAI_UPSTREAM_HOST` | 按认证模式决定 | 上游主机 |
| `OPENAI_UPSTREAM_PREFIX` | 按认证模式决定 | 上游 API 前缀 |
| `OPENAI_API_KEY` | 空 | 标准模式上游凭据，不写入代码库 |
| `CODEX_AUTH_FILE` | `~/.codex/auth.json` | Codex 模式服务器登录文件，不复制或提交 |

## 用户管理

```bash
python3 manage.py adduser alice --daily-token-limit 2000000 --daily-request-limit 100 --rpm 10
python3 manage.py listusers
python3 manage.py setlimit alice --daily-token-limit 0 --daily-request-limit 0 --rpm 20
python3 manage.py disable alice
python3 manage.py enable alice
python3 manage.py report --days 7
```

每日 token、每日请求和 RPM 限额均可在后台或 CLI 设置；`0` 表示不限。统计中的费用按符合条件的 ChatGPT Enterprise Token 计费协议中 Work/Codex 标准费率估算，不代表 API 账单或其他 Codex、ChatGPT 套餐的实际扣费。当前支持以下价格（USD / 1M tokens）：

| 模型 | 输入 | 缓存输入 | 输出 |
|---|---:|---:|---:|
| `gpt-6-astra` | 10.00 | 1.00 | 50.00 |
| `gpt-6.1-sol` | 2.00 | 0.10 | 10.00 |
| `gpt-6-sol` | 2.00 | 0.20 | 10.00 |
| `gpt-6-luna` | 0.10 | 0.01 | 0.50 |
| `gpt-5.6-sol` | 4.00 | 0.40 | 20.00 |
| `gpt-5.6-terra` | 2.00 | 0.20 | 12.00 |
| `gpt-5.6-luna` | 0.20 | 0.02 | 1.20 |
| `gpt-5.5` | 5.00 | 0.50 | 30.00 |
| `gpt-5.3-codex` | 1.75 | 0.175 | 14.00 |
| `gpt-5.2` | 1.75 | 0.175 | 14.00 |

价格来源：[ChatGPT Enterprise Token 费率卡](https://help.openai.com/zh-hans-cn/articles/20001415-chatgpt-rate-card-enterprise-token-based-pricing)。费用根据 relay 实际解析到的输入、输出、缓存输入和缓存写入 token 计算；GPT-6 Astra 的 Codex cache write 按官方费率卡计为 0。费率卡没有列出 `gpt-reserve`，因此该模型仍按未知模型处理并显示 $0。缓存输入 token 是输入总 token 的子集，不会重复按普通输入价格计费。当前按 Standard 模式估算，不包含 Fast、长上下文或区域处理等额外费率。

### 重算历史费用

价格表更新前已经记录、但费用为 0 的已知模型请求可以用当前价格重算。先停止 `openai-relay` 并备份数据库，再执行：

```bash
python3 manage.py reprice
python3 manage.py reprice --apply
```

不带 `--apply` 只预览，不修改数据库。该命令只处理已知模型且有 token usage 的记录；未知模型、失败请求和没有 usage 的记录保持 0。重算使用当前 Standard API 价格，只是 API 等价估算，不是历史账单还原。

## PM2

先确认 Claude relay 已停止，因为本服务使用同一个 `55302` 端口。服务器上的 `pm2.config.js` 只放真实环境变量，不要将真实 key/token 提交到仓库：

```bash
pm2 stop claude-relay
pm2 start pm2.config.js
pm2 save
pm2 startup systemd
```

备份数据库：

```bash
./backup_db.sh
```

只建议在受控局域网或经过认证的反向代理后使用，不要直接暴露管理页面或上游凭据到公网。

## 测试

```bash
python3 -m pytest
```
