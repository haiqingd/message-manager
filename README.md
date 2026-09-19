# 讯枢 · 消息发送管理

一个供多个应用共用的本地消息发送服务。应用通过 HTTP 提交消息，服务将消息持久化到 SQLite，再异步投递到钉钉机器人、飞书机器人、QQ 机器人（群聊或私聊）或 SMTP 邮箱。管理网页可配置渠道、测试发送、查看记录并手动重试失败投递。

## 界面预览

以下截图取自当前部署版本的页面。管理后台使用演示数据展示界面，不包含实际渠道、消息或凭据。

**消息中心概览**：查看发送统计、最近消息和渠道状态。

![消息中心概览](docs/screenshots/overview.png)

**发送测试**：填写消息并选择钉钉、飞书或邮件渠道。

![发送测试页面](docs/screenshots/compose.png)

**渠道管理**：统一维护各应用共享的发送出口。

![渠道管理页面](docs/screenshots/channels.png)

<details>
<summary>查看登录页面</summary>

![登录页面](docs/screenshots/login.png)

</details>

## 本机启动

需要 Python 3.11+。在项目目录运行：

```bash
python3 -m pip install --target .deps -r requirements.txt
PYTHONPATH=.deps python3 -m app.bootstrap
PYTHONPATH=.deps python3 -m app
```

访问 `http://127.0.0.1:8000`。首次登录使用 `.env` 中的 `ADMIN_TOKEN`。如果 8000 端口已被占用，修改 `.env` 的 `PORT`，再启动服务。`.env` 和 `data/` 默认不加入版本控制。

也可以运行 `docker compose up --build -d`。先执行初始化命令生成 `.env`；Docker Compose 仅在本机 `.env` 指定的端口开放服务。

查看容器日志可运行 `docker compose logs -f message-manager`。应用和 HTTP 访问日志使用北京时间（UTC+08:00）的完整时间戳；提交、投递成功和失败都会记录消息或投递 ID，Webhook 凭据不会写入日志。管理后台的「发送测试」会等待投递结果并显示失败原因，完整历史保留在「发送记录」。

当前主机已将容器接入统一 Nginx 网关，统一监听 80/443 和备用的 1080/1443；项目容器仅将 `.env` 中的端口映射到宿主机回环地址，供同机应用调用。推荐使用 `https://message-sender.nas.haiqingd.top/` 访问管理后台，备用 HTTPS 端口为 1443；此入口使用有效的域名证书，站点配置位于 `/home/haiqingd/services/nginx-gateway/sites/message-sender.conf`。原入口 `message-manager.nas.haiqingd.top` 仍可访问，但其 HTTPS 使用临时自签名证书，浏览器会提示不受信任。公网 80 端口被拦；1080 是明文 HTTP，不要在不可信网络中通过该入口提交管理员令牌或 API Key。本机应用可继续使用 `127.0.0.1` 入口。

## 配置渠道

登录后在「渠道管理」添加渠道：

- **钉钉**：填入自定义机器人 Webhook URL，可选填加签密钥。消息以 Markdown 格式发送。
- **飞书**：填入自定义机器人 Webhook URL，可选填签名密钥。消息以文本格式发送。
- **QQ**：填入 QQ 开放平台机器人的 AppID 和 AppSecret；目标 OpenID 可以留空——留空时该渠道仅作为「机器人身份」，用于回调验签，并向自动创建的渠道提供凭据，不能直接发送。填了 OpenID 的渠道可直接发送：群聊走 `/v2/groups/{group_openid}/messages`，私聊走 `/v2/users/{openid}/messages`。目标 OpenID 来自机器人收到的事件（群消息事件中的 `group_openid`、私聊事件中的 `user_openid`），不是 QQ 号或群号。**注意：QQ 平台自 2025 年 4 月起关闭了机器人主动推送消息的能力**，本服务发送 QQ 消息时会自动转为「被动回复」：使用回调记录里该群/用户最近一条消息的 `msg_id`（60 分钟窗口内有效，每条消息最多回复 4 次）。因此发送前需要有人在 QQ 里 @ 过机器人（私聊则为给机器人发过消息）；窗口过期或次数用完时投递会失败并提示重新 @ 机器人。无人值守的告警推送建议使用钉钉/飞书渠道。
- **邮件**：填入 SMTP 主机、端口、安全方式、发件人；如服务器要求认证，再填用户名与密码。可设置默认收件人，也可在每次发送时指定。

机器人创建与安全设置可参考[钉钉开放平台](https://open.dingtalk.com/document/orgapp/custom-robot-access)、[飞书官方 Webhook 指南](https://www.feishu.cn/content/7271149634339422210)和[QQ 机器人官方文档](https://bot.q.qq.com/wiki/develop/api-v2/)。

### QQ 回调与插槽路由

本服务内置 QQ 开放平台的事件回调接口。回调签名密钥按官方算法由机器人 AppSecret 派生（AppSecret 重复至 32 字节作为 Ed25519 种子），凭据直接取自渠道配置，不需要在开放平台上传任何公钥，也不需要额外的 `.env` 项：

1. 在「渠道管理」新建一个 QQ 渠道，只填 AppID 和 AppSecret（目标 OpenID 留空），它就是回调使用的机器人身份。
2. 在 QQ 开放平台的回调配置中填写 `https://message-sender.nas.haiqingd.top/api/qq/callback/<机器人AppID>`，使用当前已经开放的标准 443 端口。QQ 开放平台只接受 80/443/8080/8443 端口，因此本机备用的 1443 端口仍可访问管理后台和消息 API，但不能用于 QQ 回调配置。平台发起的 URL 校验由服务自动应答。
3. 订阅事件（群 @ 消息 `GROUP_AT_MESSAGE_CREATE`、单聊消息 `C2C_MESSAGE_CREATE` 等）后，每个回调请求都会记录在管理页「QQ 回调」的事件记录里（兜底插槽），展开可查看原始事件与处理结果。

收到的事件按「路由规则」分发给插槽处理，规则在「QQ 回调」页添加和管理。每条规则包含：匹配条件（事件类型、聊天类型、目标 OpenID 精确匹配）和处理插槽，规则按顺序匹配、命中即执行：

- **自动创建渠道（本应用）**：从事件里的 OpenID 创建可发送的 QQ 渠道并继承机器人凭据。系统预置了一条名为「兜底：自动创建渠道」的通配规则，排在所有自定义规则之后，因此开箱即用，也可以停用或删除。
- **转发到外部插槽（Webhook）**：把事件原始 JSON POST 到任意外部服务（带 `X-QQ-Appid` / `X-QQ-Event` 头），让其他应用处理 QQ 消息；目标需在数秒内返回 2xx。
- **仅记录（兜底）**：只保留事件记录，不做任何处理；所有未命中规则的事件也走这条路。

插槽处理失败不会向 QQ 返回错误（避免平台重试造成重复副作用），失败原因记录在事件详情中。注意沙箱环境与正式环境的 OpenID 不通用，主动消息有平台频率限制。

### 组队游戏插槽

内置的互动插槽，在路由规则中选择「组队游戏（本应用）」并匹配群 @ 消息即可启用。群里 @ 机器人发送：

- **「组队」「斗」「有无」**等关键词（如「晚上有无斗地主的」「组队开黑」）：创建当日组队邀请，机器人回复报名卡片，附一排**指令按钮**：`✅ 我参加`、`20点`、`21点`、`22点`、`❌ 退出`、`📋 查询组队`。点击按钮等价于以自己的身份 @ 机器人发送对应指令，无需手打字。
- **「参加 21点」**（也识别 `加入/报名/上号/+1`）：报名并登记预计上号时间；可附昵称如「参加 21点 我是小明」，否则显示匿名 ID。
- **「查询组队」**（也识别 `谁组队/名单`）：回复当前报名名单和每个人的预计上号时间。
- **「退出」**（也识别 `不参加/取消`）：取消报名。

组队数据按「群 × 自然日」存放在本地数据库，次日自动开新局。未命中任何关键词的 @ 消息会穿透到下一条路由规则（如兜底的自动建渠道）。所有回复都是对触发消息的被动回复，符合平台的推送限制。**群聊的按钮只能挂在 Markdown 模板消息底部**（平台不支持纯按钮消息，也会丢弃挂在纯文本消息上的键盘）：未配置模板时卡片以纯文本发出、不带按钮；在 QQ 开放平台「开发设置 → 消息配置」创建含参数 `{{content}}` 的 Markdown 模板，并把模板 ID 填到路由规则的 `markdown_template_id` 配置后，卡片以 Markdown 外观发出并附按钮（`我参加`/`20点`/`21点`/`22点`/`退出`/`查询组队`），按钮权限设为所有人可见。在路由配置中设置 `{"buttons": false}` 可强制关闭按钮。

Webhook URL、签名密钥、QQ AppSecret、路由中的转发地址和 SMTP 密码使用 `.env` 中的 `ENCRYPTION_KEY` 加密后保存在数据库中，管理接口只返回是否已配置，不返回明文。请妥善备份 `.env` 和 `data/messages.db`；丢失加密密钥后无法读取原有渠道凭据。

## HTTP 接口

先从渠道管理复制渠道 ID，调用：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <.env 中的 MESSAGE_API_KEY>' \
  -d '{
    "source": "order-service",
    "title": "订单服务状态提醒",
    "body": "订单服务已恢复正常。",
    "destinations": [{"channel_id": "替换为渠道 ID"}],
    "idempotency_key": "recovered-001"
  }'
```

接口返回 HTTP `202`、消息 ID 和各投递初始状态。`source` 标识调用应用，默认为 `default`；同一 `source` 内重复使用 `idempotency_key` 会返回原消息，避免重复投递。邮箱收件人可在目标中传入 `"recipients": ["user@example.com"]`；未传时使用渠道默认收件人。一个请求可包含多个不同的渠道。

使用相同的 `X-API-Key` 请求 `GET /api/v1/messages/{id}` 查看投递结果。状态包括 `pending`、`sending`、`sent` 和 `failed`。投递最多自动尝试 3 次，失败间隔逐步增加；最终失败可在管理网页手动重试。完整接口说明见 `/docs`。

管理接口使用 `Authorization: Bearer <ADMIN_TOKEN>`。应用调用方只需要 `MESSAGE_API_KEY`，不要把 `ADMIN_TOKEN` 分发给业务应用或放入浏览器前端代码。

## 运行边界

默认只监听 `127.0.0.1`，适合同机多个应用调用。跨机器使用时，请通过 HTTPS 反向代理暴露服务，并限制访问来源。当前是单进程、轻量队列方案；部署时保持一个服务进程，避免多进程重复管理发送任务。服务重启会恢复尚未完成的投递，但外部服务已接受而本地尚未记录成功的极端情况仍可能重复发送；业务侧应使用幂等键处理重复提交。

## 测试

```bash
PYTHONPATH=.deps:. python3 -m pytest -q
```
