# Telethon Media Downloader Bot

使用 Bot Token 和 MTProto 下载 Telegram 图片、视频及文件，提供中文 Web 控制台、用户白名单、有界下载队列、实时进度、自动重试和运行时控制。

当前版本：`v1.6`

## v1.6 主要能力

- 下载失败后自动重试最多 3 次，采用 2、4、8 秒指数退避。
- 显式有界队列，默认最多排队 100 个任务，单 worker 避免无界协程堆积。
- 状态包括排队、下载、暂停、重试、校验、完成、失败、中断和取消。
- Web 和 Telegram 同时显示百分比、已下载/总大小、速度、ETA、限速和重试次数。
- 服务重启时自动校正遗留的“下载中”记录；完整文件恢复为完成，其余标为中断。
- 同名文件通过原子预占避免并发覆盖，下载先写隐藏临时文件，完成后原子替换。
- 下载历史按间隔节流落盘；设置和历史均使用临时文件加 `os.replace()` 原子保存。
- 相册创建串行化，避免同一相册生成多个批次状态。
- 控制台强制登录；首次启动使用一次性初始化口令，不再默认公开。
- 登录失败限制、安全 Cookie 配置、CSP 和常用安全响应头。
- 区分允许用户与管理员用户；危险命令仅管理员可用。
- CI 自动执行编译、Ruff、测试、`pip-audit`、镜像构建和 Trivy 漏洞扫描。

## 获取 Telegram 参数

1. 在 <https://my.telegram.org> 创建应用，获取 `API_ID` 和 `API_HASH`。
2. 在 Telegram 中通过 `@BotFather` 创建 bot，获取 `BOT_TOKEN`。

## Docker Compose 部署

创建持久化目录：

```bash
sudo mkdir -p /opt/telethon-media-bot/{images,videos,files,sessions,config}
```

启动：

```bash
docker compose pull
docker compose up -d
```

出于安全考虑，Compose 默认只监听宿主机 `127.0.0.1:8000`。本机访问：

```text
http://127.0.0.1:8000
```

远程服务器建议使用 SSH 隧道：

```bash
ssh -L 8000:127.0.0.1:8000 user@server
```

### 首次登录

如果没有通过 `ADMIN_PASSWORD` 设置密码，服务会在日志中生成一次性初始化口令：

```bash
docker compose logs telethon-media-bot
```

使用日志中的“一次性初始化口令”登录，然后在控制台立即设置至少 10 个字符的管理员密码。也可以在部署前通过环境变量设置固定密码或初始化口令：

```yaml
services:
  telethon-media-bot:
    environment:
      ADMIN_PASSWORD: "请替换为强密码"
      COOKIE_SECURE: "true"
```

`COOKIE_SECURE=true` 只应在 HTTPS 反向代理后启用。需要从其他主机直接访问时，应由反向代理提供 TLS 和访问控制，不建议把容器端口直接暴露到公网。

## Bot 命令

所有允许用户可用：

```text
/help             显示帮助
/status           当前任务、进度、速度、ETA、队列、暂停和限速
/queue [n]        查看前 n 个队列任务
/history [n]      查看最近记录
/failed           查看失败、中断和取消的记录
/storage          查看文件数量、占用和磁盘剩余空间
/whoami           查看自己的 Telegram 用户 ID
/version          查看版本和启动时间
/ping             查看 Bot 响应延迟
```

管理员可用：

```text
/cancel current   取消当前下载
/cancel all       取消全部当前和排队任务
/cancel <任务ID>  取消指定任务
/pause            无限期暂停
/pause 30m        暂停 30 分钟，支持 s、m、h，最长 12 小时
/resume           立即恢复
/limit 2          限速 2 MB/s
/limit off        取消限速
/retry <任务ID>   重试指定失败/中断任务
/retry failed     重试最近最多 10 个失败/中断任务
/delay 1.5        兼容旧命令，暂停 1.5 小时
```

如果未填写 `ADMIN_USER_IDS`，所有 `ALLOWED_USER_IDS` 都视为管理员。填写后，只有管理员 ID 能执行取消、暂停、恢复、限速和重试命令。管理员 ID 即使未重复写入允许列表，也可以使用 bot。

## Web 控制台

控制台支持：

- 启动、停止和重启 bot。
- 恢复暂停、取消限速。
- 查看队列状态、速度、ETA、限速和重试次数。
- 取消当前或排队任务、重试失败任务、清理失败/中断历史。
- 浏览并下载已保存文件。
- 查看内存运行日志。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_ID` | 空 | Telegram app API ID |
| `API_HASH` | 空 | Telegram app API Hash |
| `BOT_TOKEN` | 空 | BotFather token |
| `ALLOWED_USER_IDS` | 空 | 允许使用 bot 的用户 ID，逗号分隔 |
| `ADMIN_USER_IDS` | 空 | 管理员 ID；空时允许用户均为管理员 |
| `ADMIN_PASSWORD` | 空 | 控制台密码；设置后覆盖保存的密码 |
| `BOOTSTRAP_TOKEN` | 随机 | 未设置密码时的一次性初始化口令 |
| `DOWNLOAD_DIR` | `/downloads` | 下载根目录 |
| `IMAGE_DOWNLOAD_DIR` | `/downloads/images` | 图片目录 |
| `VIDEO_DOWNLOAD_DIR` | `/downloads/videos` | 视频目录 |
| `FILE_DOWNLOAD_DIR` | `/downloads/files` | 其他文件目录 |
| `SESSION_DIR` | `/sessions` | Telethon session 目录 |
| `CONFIG_DIR` | `/config` | 设置和历史目录 |
| `SESSION_NAME` | `media_downloader_bot` | Session 名称，只允许字母、数字、点、下划线和连字符 |
| `PROGRESS_INTERVAL_SECONDS` | `3` | Telegram 进度消息最短更新间隔 |
| `PROGRESS_PERCENT_STEP` | `5` | 进度消息最小百分比变化 |
| `MAX_FILENAME_STEM_LENGTH` | `120` | 文件主名最大长度 |
| `MAX_AUTO_RETRIES` | `3` | 下载失败后的自动重试次数 |
| `QUEUE_MAXSIZE` | `100` | 最大排队任务数 |
| `HISTORY_FLUSH_INTERVAL_SECONDS` | `2` | 下载进度历史落盘间隔 |
| `AUTO_START_BOT` | `true` | 配置完整时自动启动 |
| `COOKIE_SECURE` | `false` | 为登录 Cookie 设置 Secure，仅用于 HTTPS |
| `WEB_HOST` | `0.0.0.0` | 容器内监听地址 |
| `WEB_PORT` | `8000` | Web 端口 |

## 本地开发

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m ruff check app tests
.venv/bin/python -m pytest -q
.venv/bin/python -m pip_audit -r requirements.txt
```

Windows 使用 `.venv\Scripts\python.exe`。

## 发布镜像

推送 `v*` 标签后，GitHub Actions 会先运行测试和依赖审计，通过后构建并推送镜像，然后执行 Trivy 严重漏洞扫描：

```text
ghcr.io/ccawmiku/telethon-bot-token-mtproto:v1.6
```

## 数据安全

- 不要提交 `.env`、`config/`、`sessions/` 或下载目录。
- `settings.json` 包含 API Hash 和 Bot Token，文件会以 `0600` 权限原子写入。
- 下载文件接口需要控制台登录，并对解析后的目标路径再次检查，防止目录穿越。
- 配置验证或 Telegram 启动失败时，新设置不会提交，运行配置会回滚。
