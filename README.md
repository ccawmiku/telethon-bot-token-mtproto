# Telethon Media Downloader Bot

带中文网页控制面板的 Telegram 媒体下载 bot。容器启动后先打开 Web 面板填写 `API_ID`、`API_HASH`、`BOT_TOKEN`、允许用户 ID 和控制台密码，再启动 bot。你把图片、视频或文件发给 bot 后，bot 会在 Telegram 里返回排队、下载进度、完成或失败状态，同时控制面板会显示下载记录、文件列表和运行日志。

## 功能

- Web 控制面板填写和保存 Telegram 参数。
- 控制面板密码登录。
- Telegram 用户 ID 白名单，未授权用户不能触发下载。
- 从网页启动、停止、重启 bot。
- 使用 Telethon、Bot Token 和 MTProto 下载用户发给 bot 的图片、视频和文件。
- Telegram 消息内返回下载状态；相册或一组多图会合并成一条完成汇总。
- 控制面板显示 bot 状态、下载记录、进度、错误、已保存文件和运行日志。
- 自动清洗文件名，重名时追加时间戳和短 UUID，避免覆盖。
- Docker Compose 一键运行。

## 获取 Telegram 参数

1. 到 <https://my.telegram.org> 创建应用，获取 `API_ID` 和 `API_HASH`。
2. 到 Telegram 找 `@BotFather` 创建 bot，获取 `BOT_TOKEN`。

## Docker Compose 运行

```bash
docker compose pull
docker compose up -d
```

打开控制面板：

```text
http://localhost:8000
```

在网页里填写 `API_ID`、`API_HASH`、`BOT_TOKEN`、允许用户 ID 和控制台密码，点击 `保存设置`，再点击 `启动`。

如果不知道自己的 Telegram 用户 ID，可以先不给“允许用户 ID”填值，启动 bot 后给 bot 发任意消息。bot 会回复你的用户 ID。把这个 ID 填回控制面板并保存后，bot 才会处理下载。

图片保存在宿主机的 `/opt/telethon-media-bot/images`，视频保存在 `/opt/telethon-media-bot/videos`，其他文件保存在 `/opt/telethon-media-bot/files`。Telethon session 保存在 `/opt/telethon-media-bot/sessions`，网页保存的配置和下载记录保存在 `/opt/telethon-media-bot/config`。

查看日志：

```bash
docker compose logs -f
```

## 直接构建镜像

```bash
docker build -t telethon-media-bot:latest .
```

运行：

```bash
docker run -d --name telethon-media-bot \
  --restart unless-stopped \
  -p 8000:8000 \
  -v "%cd%/images:/downloads/images" \
  -v "%cd%/videos:/downloads/videos" \
  -v "%cd%/files:/downloads/files" \
  -v "%cd%/sessions:/sessions" \
  -v "%cd%/config:/config" \
  telethon-media-bot:latest
```

Linux/macOS 可把 `"%cd%"` 换成 `"$(pwd)"`。

## 使用已发布镜像

当前 `docker-compose.yml` 默认使用 GitHub Actions 构建好的镜像：

```text
ghcr.io/ccawmiku/telethon-bot-token-mtproto:latest
```

部署前创建挂载目录：

```bash
sudo mkdir -p /opt/telethon-media-bot/images /opt/telethon-media-bot/videos /opt/telethon-media-bot/files /opt/telethon-media-bot/sessions /opt/telethon-media-bot/config
```

启动：

```bash
docker compose pull
docker compose up -d
```

## GitHub Actions 构建镜像

仓库包含 `.github/workflows/docker-image.yml`。推送到 `main` 或在 GitHub Actions 页面手动运行 `Docker Image` 后，会构建并推送镜像到 GitHub Container Registry：

```text
ghcr.io/ccawmiku/telethon-bot-token-mtproto:latest
```

拉取镜像：

```bash
docker pull ghcr.io/ccawmiku/telethon-bot-token-mtproto:latest
```

如果仓库或 package 是私有的，需要先登录 GHCR：

```bash
echo YOUR_GITHUB_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

## 通过环境变量预填配置

也可以复制 `.env.example` 为 `.env`，然后用 `docker run --env-file .env` 或自行在 Compose 中加入环境变量。设置 `AUTO_START_BOT=true` 后，如果参数完整，服务启动时会自动启动 bot。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_ID` | 空 | Telegram app API ID |
| `API_HASH` | 空 | Telegram app API Hash |
| `BOT_TOKEN` | 空 | BotFather 生成的 token |
| `ALLOWED_USER_IDS` | 空 | 允许使用 bot 的 Telegram 用户 ID，多个用逗号分隔 |
| `ADMIN_PASSWORD` | 空 | 控制面板登录密码 |
| `DOWNLOAD_DIR` | `/downloads` | 容器内下载目录 |
| `IMAGE_DOWNLOAD_DIR` | `/downloads/images` | 容器内图片下载目录 |
| `VIDEO_DOWNLOAD_DIR` | `/downloads/videos` | 容器内视频下载目录 |
| `FILE_DOWNLOAD_DIR` | `/downloads/files` | 容器内其他文件下载目录 |
| `SESSION_DIR` | `/sessions` | 容器内 session 目录 |
| `CONFIG_DIR` | `/config` | 网页配置和下载记录目录 |
| `SESSION_NAME` | `media_downloader_bot` | session 文件名 |
| `PROGRESS_INTERVAL_SECONDS` | `3` | Telegram 状态消息最短更新间隔 |
| `PROGRESS_PERCENT_STEP` | `10` | 至少变化多少百分比才更新 |
| `MAX_FILENAME_STEM_LENGTH` | `120` | 文件主名最大长度 |
| `AUTO_START_BOT` | `false` | 参数完整时是否自动启动 bot |
| `WEB_PORT` | `8000` | Web 服务端口 |

## 注意

- 控制面板会保存 bot token，请不要把 `config/`、`.env`、`sessions/`、`images/`、`videos/`、`files/` 提交到 GitHub。
- 如果把服务部署到公网，请放在反向代理认证或内网访问控制后面。
- bot 默认只能接收用户主动发给它的消息。用于群组时，需要把 bot 拉入群组，并根据 BotFather 的隐私设置调整可见消息范围。
