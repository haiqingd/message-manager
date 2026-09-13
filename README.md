# 讯枢 · 消息发送管理

一个供多个应用共用的本地消息发送服务。应用通过 HTTP 提交消息，服务将消息持久化到 SQLite，再异步投递到钉钉机器人、飞书机器人或 SMTP 邮箱。管理网页可配置渠道、测试发送、查看记录并手动重试失败投递。

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

当前主机已将容器接入统一 Nginx 网关，统一监听 80/443 和备用的 1080/1443；项目容器仅将 `.env` 中的端口映射到宿主机回环地址，供同机应用调用。推荐使用 `https://message-sender.nas.haiqingd.top:1443` 访问管理后台，此入口使用有效的域名证书，站点配置位于 `/home/haiqingd/services/nginx-gateway/sites/message-sender.conf`。原入口 `message-manager.nas.haiqingd.top` 仍可访问，但其 HTTPS 使用临时自签名证书，浏览器会提示不受信任。公网 80 端口被拦；1080 是明文 HTTP，不要在不可信网络中通过该入口提交管理员令牌或 API Key。本机应用可继续使用 `127.0.0.1` 入口。

## 配置渠道

登录后在「渠道管理」添加渠道：

- **钉钉**：填入自定义机器人 Webhook URL，可选填加签密钥。消息以 Markdown 格式发送。
- **飞书**：填入自定义机器人 Webhook URL，可选填签名密钥。消息以文本格式发送。
- **邮件**：填入 SMTP 主机、端口、安全方式、发件人；如服务器要求认证，再填用户名与密码。可设置默认收件人，也可在每次发送时指定。

机器人创建与安全设置可参考[钉钉开放平台](https://open.dingtalk.com/document/orgapp/custom-robot-access)和[飞书官方 Webhook 指南](https://www.feishu.cn/content/7271149634339422210)。

Webhook URL、签名密钥和 SMTP 密码使用 `.env` 中的 `ENCRYPTION_KEY` 加密后保存在数据库中，管理接口只返回是否已配置，不返回明文。请妥善备份 `.env` 和 `data/messages.db`；丢失加密密钥后无法读取原有渠道凭据。

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
