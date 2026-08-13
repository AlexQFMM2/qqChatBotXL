# qqChatBotXL

独立 Docker 部署的 QQ 群人格机器人。当前人格为夏莉·沃利克；通过 QQ 开放平台 Webhook 接收群消息，通过独立的 `qqchat` 大模型入口生成回复，并用 SQLite 保存每个群的短期上下文。

## 特点

- 使用 QQ 官方接口，不登录或模拟个人 QQ 账号。
- 整个机器人运行在单独的 `qqchat-bot` 容器中。
- 容器端口只绑定服务器 `127.0.0.1:18080`，公网只能经 Nginx 访问精确的回调路径。
- Webhook 使用 Ed25519 验签、快速 ACK 和有界后台队列，模型慢请求不会阻塞 QQ 回调。
- 默认仅在被 `@` 时回复，避免刷屏。
- 每个群独立记忆；重复事件会自动去重。
- 每个群拥有互相隔离的专用工作区，可读写文本并把文件发回 QQ。
- DeepSeek 是固定总控和唯一最终发言模型；视觉、检索、生图和语音模型只返回观察、证据或产物。
- 内置仅容器网络可访问的 SearXNG 搜索，并以 VNDB Kana 核验视觉小说资料；网页正文读取和 Open-Meteo 天气仍受 SSRF 防护。
- 群成员发送的 QQ 附件会在大小限制内保存到该群的 `inbox/`，可继续整理或回传。
- 支持把 QQ 图片作为视觉输入交给多模态模型，可描述画面、读取截图文字和分析报错截图。
- 支持按需读取 Bilibili 视频元数据、字幕、热门评论和弹幕；只贴链接不会自动抓取。
- 明确要求语音时可使用夏莉参考音克隆合成，并转为 QQ SILK 语音；失败自动回退文字。
- 复杂任务达到工具轮次上限时会停止执行并汇总结果，不再静默失败。
- 内置轻量中文 PDF 生成器，无需浏览器或额外服务；生成后可直接作为 QQ 群文件发送。
- 自动维护工作区和 SQLite：分群/全局容量限制、附件与生成文件分级保留、聊天记录裁剪、启动清理和周期清理。
- 群内不能切走 DeepSeek 总控；`/model list` 只展示各协助模型的职责。
- 人格在 `persona.md` 中维护，不写死在代码里。
- 机器人显示身份由 `BOT_NAME` 配置，称呼别名由 `BOT_ALIASES` 配置。
- 支持 Anthropic 和 OpenAI 兼容中转接口。
- `/reset` 仅允许群主和管理员使用。

## 准备 QQ 机器人

1. 登录 [QQ 机器人开放平台](https://bot.q.qq.com/open) 创建机器人。
2. 在开发设置中取得 AppID 和 AppSecret。
3. 配置沙箱群或群白名单，并由群主/管理员把机器人添加到群。
4. 在“开发设置 → 事件订阅与回调”选择 Webhook，回调地址填写 `https://qqchat.example.com/qq/webhook`（替换为自己的域名）。
5. 订阅群消息事件。默认只需群 `@` 消息；使用 `smart` 或 `all` 前还需要开启“接收所有消息”。

## 配置

```bash
cp .env.example .env
chmod 600 .env
```

至少填写：

```dotenv
QQ_APP_ID=...
QQ_APP_SECRET=...
LLM_BASE_URL=https://qqchat.example.com
LLM_API_KEY=...
LLM_MODEL=deepseek-v4-flash
MODEL_CATALOG_JSON={"flash":"deepseek-v4-flash","pro":"deepseek-v4-pro[1m]"}
BOT_NAME=夏莉
BOT_ALIASES=夏莉,夏莉·沃利克,Shirley,シャーリィ,小Q
OWNER_USER_IDS=
OWNER_TITLE=老师
GOOD_MORNING_ENABLED=true
GOOD_MORNING_GROUPS=群OpenID
GOOD_MORNING_TIME=07:00
IMAGE_GENERATION_ENABLED=true
IMAGE_GENERATION_MODEL=qwen-image-3.0-pro
IMAGE_EDIT_ENABLED=true
IMAGE_EDIT_MODEL=qwen-image-edit-max
IMAGE_EDIT_MAX_IMAGES=3
VISION_ENABLED=true
VISION_MODEL=qwen3.7-flash
VISION_API_FORMAT=openai
VISION_CONTEXT_MESSAGES=20
VISION_MAX_IMAGES=4
VISION_MAX_IMAGE_MB=8
VOICE_ENABLED=true
VOICE_MODEL=qwen3-tts-vc-2026-01-22
VOICE_MAX_CHARS=240
SEARCH_PROVIDER=searxng
SEARXNG_BASE_URL=http://searxng:8080
WEB_SEARCH_RESULTS=8
```

不要把 `.env` 提交到 Git。建议为机器人单独创建一个带请求数和 Token 限额的中转站 Key。

`OWNER_USER_IDS` 用 QQ 回调中的 `member_openid` 绑定开发者/老师身份，不依赖可伪造的群昵称。绑定后，模型会在最近聊天记录中看到经过验证的专属关系标注；人格仍保留安全和隐私边界。

维护默认值：单群工作区 500 MiB、全部群合计 5 GiB、单文件 50 MiB；收到的附件保留 14 天，机器人生成的文件保留 90 天，残留下载分片保留 24 小时；聊天记录保留 30 天且每群最多 1000 条，Webhook 去重记录保留 7 天。SQLite 每 24 小时在线备份一次并保留 7 天，Docker 日志最多保留约 30 MiB。启动时会立即维护，此后每 6 小时执行。管理员可在群内发送 `/cleanup` 手动执行，`/status` 可查看本群容量。

识图支持 JPEG、PNG、GIF 和 WebP。只有当前消息实际带图，或文字明确提到“这张图/刚才的图/截图”等时才启用视觉助手；无关资料问答不会被历史图片切到千问。千问只输出中性画面观察和 OCR，观察结果再交给 DeepSeek 结合上下文与工具生成最终回复。机器人发出的表情与生成图片会记录哈希，QQ 回传缺少作者时也不会误存成用户附件。

参考图生图使用独立的百炼原生适配容器。普通“画一张图”仍调用 `IMAGE_GENERATION_MODEL`；说“以你自己为原型/画夏莉”时会从 `IMAGE_CHARACTER_REFERENCES` 选择内置 CG，说“参考刚才的图/修改图片”时会读取最近聊天图片并调用 `IMAGE_EDIT_MODEL`。适配端点复用机器人的中转站 Bearer Key，对外只开放精确路径 `/v1/image-edits`，百炼上游 Key 仅存在于 `.image-adapter.env`，不会进入机器人容器。

图片提示词中通过引号明确指定、且上下文含“写着、对话框、拟声词、标题、字幕、标牌”等文字要求时，机器人会要求模型生成空白留字区域，再使用内置中文字体进行二次排字。这样能保证最终文字逐字正确；未检测到合适留白区域时，会使用底部安全区域回退排版。可通过 `IMAGE_TEXT_OVERLAY_ENABLED=false` 关闭。

耗时任务使用独立后台队列：只有已经绑定真实执行器的搜索、绘图、语音、文件或 PDF 任务才会收到入队承诺，普通聊天不会。每项任务写入 `task_runs`，经历 queued/running/succeeded/failed；模型、下载或 QQ 发送任一步失败都会产生一次明确终态通知，重启时遗留任务也会自动失败并通知。

B 站普通总结仍只读取元数据和字幕。只有明确要求“分析成片、评价剪辑/转场/画面/字幕呈现/音质”等时，才调用内网重型分析：每群 10 分钟最多一次，服务端全局串行；视频最多 15 分钟、下载最多 500 MiB、抽样最多 48 帧，超时或资源不足会明确拒绝。画面联系表交给视觉助手，FFmpeg 提供剪辑与响度辅助指标；没有 B 站字幕或外部 ASR 时会明确说明，不能猜测台词、咬字和完整配音表现。

语音由独立的 `qwen-voice-adapter` 完成。它复用图片适配器已有的百炼凭证，但凭证不会进入机器人容器；首次明确要求语音时，适配器使用内置的 20 秒夏莉样本登记 Voice ID，并写入 `data/voice-adapter/voice_id.txt`。参考音只在登记期间通过不可猜测的临时路径提供，Voice ID 落盘后该路径永久返回 404。生成结果经 FFmpeg 统一为 24 kHz 单声道，再编码为 QQ 使用的 Tencent SILK。群内可用 `/voice 想说的话`，或直接说“请用语音回复”；语音失败时发送同一条文字，不会静默丢失。

## 启动

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100
```

默认从腾讯云 Docker 镜像源获取官方 Python 基础镜像，以适配当前服务器网络。如果服务器以后可以直连 Docker Hub，可在 `.env` 中改为 `PYTHON_IMAGE=python:3.12-slim`。

`deploy/nginx/qqchat.example.com.conf` 是独立 HTTPS 入口示例配置。先把其中的 `qqchat.example.com` 替换为自己的域名；它把精确路径 `/qq/webhook` 代理到机器人，把 `/v1/image-edits`、`/v1/voice-speech` 和一次性语音参考路径代理到隔离适配容器，其余模型 API 和 `/health` 代理到本机中转站，其他路径一律返回 404；证书可由 Certbot 定时器自动续期。

停止或更新：

```bash
docker compose down
docker compose up -d --build
```

SQLite 数据位于 `data/qqchat.db`。修改 `persona.md` 后重启容器：

```bash
docker compose restart qqchat-bot
```

## 群内命令

- `/help`：查看帮助。
- `/status`：查看回复模式和记忆窗口。
- `/persona`：查看简短人格说明。
- `/reset`：清空当前群上下文，仅群主或管理员可用。
- `/model`、`/model list`：查看 DeepSeek 总控与各协助模型；总控不能在群内切换。
- `/files`、`/read note.md`：浏览或读取本群隔离工作区。
- `/write note.md 一段内容`：写入 UTF-8 文本文件。
- `/send note.md`：通过 QQ 官方分片上传接口发送工作区文件。
- `/voice 想说的话`：使用夏莉音色发送一条 QQ 语音。

文件修改默认只允许群主或管理员操作。容器只挂载 `./workspace:/workspace`，不挂载宿主机其他目录、SSH 密钥或 Docker Socket；即使模型受到提示词注入，也无法越过这个目录访问宿主机。
默认单文件上限为 50MB、每群工作区总配额为 500MB，均可在 `.env` 中调整。

使用 Anthropic 兼容接口时，群主/管理员也可以自然地说“帮我新建一个 todo.md”“读一下 notes/周报.md”“把刚才生成的文件发出来”，机器人会通过受限工具完成。OpenAI 兼容模式目前保留斜杠命令，不启用自然语言工具循环。

所有群成员都可以通过自然语言使用 `research`、`web_search`、`fetch_url` 和 `get_weather`。事实核验意图会被程序强制检索，至少需要两个独立来源；Galgame、会社、角色和作品归属会额外查 VNDB。SearXNG 不健康时明确报告联网失败，不回退到 Bing RSS 或模型记忆。普通网页读取仍会拒绝本机、内网、保留地址与非标准端口。

部署旧版本后首次升级可先预览并净化历史污染；正式执行会先创建 SQLite 在线备份：

```bash
docker compose exec qqchat-bot python -m app.clean_history --dry-run
docker compose exec qqchat-bot python -m app.clean_history --apply
```

## 回复模式

- `mention`：只响应 `@机器人`，默认且最稳妥。
- `smart`：响应 `@`、叫机器人名字的消息，并以较低概率自然参与普通群聊。
- `all`：每条消息都回复，容易刷屏，不推荐。

QQ 被动回复必须在事件发生后 5 分钟内完成。本项目为模型请求设置了 110 秒超时；请求失败只记录错误，不会在群里连续发送故障提示。

## 本地测试

测试不需要真实 QQ 或模型凭证：

```bash
python3 -m unittest discover -s tests -v
```
