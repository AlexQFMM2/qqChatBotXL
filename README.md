# qqChatBotXL

独立 Docker 部署的 QQ 群人格机器人。当前人格为夏莉·沃利克；通过 QQ 开放平台 Webhook 接收群消息，通过独立的 `qqchat` 大模型入口生成回复，并用 SQLite 保存每个群的短期上下文。

## 特点

微信 iLink 已接入同一份夏莉人格、Fusion 模型和工作区工具，并迁到服务器独立常驻。已验证 QQ 本人的新项目事实可在微信继续使用；每天 04:00 整理长期记忆，24 小时观察尚在进行。命令、权限与恢复见 [长期记忆运维](docs/shared-memory.md)。启动、文件使用和当前边界见 [夏莉微信私聊](docs/wechat-chat.md)；协议排障仍可使用 [微信通道测试](docs/wechat-probe.md)。

- 使用 QQ 官方接口，不登录或模拟个人 QQ 账号。
- QQ 与微信分别运行在 `qqchat-bot`、`wxchat-bot` 容器，共用固定版本代码。
- 容器端口只绑定服务器 `127.0.0.1:18080`，公网只能经 Nginx 访问精确的回调路径。
- Webhook 使用 Ed25519 验签、快速 ACK 和有界后台队列，模型慢请求不会阻塞 QQ 回调。
- 默认仅在被 `@` 时回复，避免刷屏。
- 各会话短期上下文独立；经授权的长期事实可跨端检索，私有文件仍隔离；重复事件自动去重。
- 每个群拥有互相隔离的专用工作区，可读写文本、只读解析 `.xlsx` 单元格并把文件发回 QQ。
- 主模型负责理解消息，自主选择识图、绘图、语音、联网和文件工具，再依据真实结果回复；具体模型仍在 CCR Fusion 配置。见 [主模型与能力工具](docs/controller-tools.md)。
- 内置仅容器网络可访问的 SearXNG 搜索，并以 VNDB Kana 核验视觉小说资料；网页正文读取和 Open-Meteo 天气仍受 SSRF 防护。
- 群成员发送的 QQ 附件会在大小限制内保存到该群的 `inbox/`，可继续整理或回传。
- 支持把 QQ 图片作为视觉输入交给多模态模型，可描述画面、读取截图文字和分析报错截图。
- 支持按需读取 Bilibili 视频元数据、字幕、热门评论和弹幕；只贴链接不会自动抓取。
- 明确要求语音时可使用夏莉参考音克隆合成，并转为 QQ SILK 语音；微信可单独启用 WAV 音频附件，失败会提示并回退文字。
- 复杂任务达到工具轮次上限时会停止执行并汇总结果，不再静默失败。
- 内置轻量中文 PDF 生成器，无需浏览器或额外服务；生成后可直接作为 QQ 群文件发送。
- 自动维护工作区和 SQLite：分群/全局容量限制、附件与生成文件分级保留、聊天记录裁剪、启动清理和周期清理。
- 群内不能切走 DeepSeek 总控；`/model list` 只展示各协助模型的职责。
- 人格在 `persona.md` 中维护，不写死在代码里。
- 英语单词解读会固定展示英美音标、翻译、对应词性、构词拆解和一条中英例句，再按现有权限附夏莉声音的单词读音；例如“解读 unhappy”。
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
LLM_MODEL=Fusion/qqchatbot
MEDIA_ROUTE=fusion
MODEL_CATALOG_JSON={"main":"Fusion/qqchatbot"}
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
VISION_ROUTE=fusion
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

`Fusion/qqchatbot` 是 CCR 中供机器人长期使用的逻辑模型。需要更换实际总控模型时，只修改 CCR 后台该 Fusion 的“基础模型”，不需要再修改或重建 QQChat。

设置 `MEDIA_ROUTE=fusion` 后，绘图、改图和语音统一读取 **CCR → Fusion → qqchatbot → 工具** 中的配置，下一次请求生效。新增「绘图（QQ / 微信）」「改图（QQ / 微信）」「语音（QQ / 微信）」并选择模型；语音还需选择匹配音色。对应 `*_MODEL` 环境变量仅用于 `MEDIA_ROUTE=direct` 的旧路径，Fusion 扩展停用或失败不会回退旧模型。渠道开关和文件权限仍由机器人控制。CCR 需安装 ccrAdmin 的 Fusion 媒体补丁。

`OWNER_USER_IDS` 用 QQ 回调中的 `member_openid` 绑定开发者/老师身份，不依赖可伪造的群昵称。绑定后，模型会在最近聊天记录中看到经过验证的专属关系标注；人格仍保留安全和隐私边界。

维护默认值：单群工作区 500 MiB、全部群合计 5 GiB、单文件 50 MiB；收到的附件保留 14 天，机器人生成的文件保留 90 天，残留下载分片保留 24 小时；聊天记录保留 30 天且每群最多 1000 条，Webhook 去重记录保留 7 天。SQLite 每 24 小时在线备份一次并保留 7 天，Docker 日志最多保留约 30 MiB。启动时会立即维护，此后每 6 小时执行。管理员可在群内发送 `/cleanup` 手动执行，`/status` 可查看本群容量。

识图支持 JPEG、PNG、GIF 和 WebP。主模型需要看图时调用 `understand_images`，程序只读取本会话最近的用户图片；普通聊天初始请求不携带历史图片。推荐使用 `VISION_ROUTE=fusion`，将选中的图片交给 `Fusion/qqchatbot` 在 CCR 中配置的 `vision_understand` 工具；视觉模型在 CCR 后台切换，无需修改或重启 QQChat。迁移期间可改回 `VISION_ROUTE=direct`，由 `VISION_MODEL` 直接生成中性画面观察和 OCR。机器人发出的表情与生成图片会记录哈希，QQ 回传缺少作者时也不会误存成用户附件。Bilibili 成片联系表暂时仍使用 `VISION_MODEL`，将在普通图片链路稳定后另行迁移。

是否使用参考图由主模型按画面内容决定：涉及夏莉自己时自主选用 `IMAGE_CHARACTER_REFERENCES` 中的角色参考，不要求用户特意说“参考图”；修改用户上传图片时只选本会话合法图片。`MEDIA_ROUTE=fusion` 统一调用 CCR 的绘图/改图扩展，型号由后台配置。旧 `MEDIA_ROUTE=direct` 才使用独立百炼适配器 `/v1/image-edits` 与 `.image-adapter.env`，供应商凭据不会进入机器人提示词。

图片提示词中通过引号明确指定、且上下文含“写着、对话框、拟声词、标题、字幕、标牌”等文字要求时，机器人会要求模型生成空白留字区域，再使用内置中文字体进行二次排字。这样能保证最终文字逐字正确；未检测到合适留白区域时，会使用底部安全区域回退排版。可通过 `IMAGE_TEXT_OVERLAY_ENABLED=false` 关闭。

QQ 主模型任务使用现有有界后台队列，每项写入 `task_runs`；模型自行选择耗时工具，选中后才发送对应媒体进度。工具错误交回主模型说明，发送结果不明立即停止后续动作；重启时遗留任务失败并通知。微信沿用独立持久收件队列。

B 站普通总结仍只读取元数据和字幕。只有明确要求“分析成片、评价剪辑/转场/画面/字幕呈现/音质”等时，才调用内网重型分析：每群 10 分钟最多一次，服务端全局串行；视频最多 15 分钟、下载最多 500 MiB、抽样最多 48 帧，超时或资源不足会明确拒绝。画面联系表交给视觉助手，FFmpeg 提供剪辑与响度辅助指标；没有 B 站字幕或外部 ASR 时会明确说明，不能猜测台词、咬字和完整配音表现。

语音由 CCR 调用所选模型，再由独立的 `qwen-voice-adapter` 下载和转码。在适配器的私有环境配置中设置 `FUSION_BASE_URL=https://你的模型入口/v1` 与 `FUSION_API_KEY`（CCR 客户端密钥）；供应商凭据留在 CCR。生成结果经 FFmpeg 统一为 24 kHz 单声道，再编码为 QQ Tencent SILK；微信请求 `format=wav`，发送可播放音频附件。群内可用 `/voice 想说的话`，或直接说“请用语音回复”；失败会明确提示并回退文字。

克隆音色按模型登记，不能跨型号混用。当前新版 `qwen-audio-3.1-tts-plus` 已使用原夏莉参考音频独立登记并验证；旧登记文件保留。后台更换模型后必须重新选择音色，可选兼容预置音色或该型号已登记的夏莉音色。旧 `direct` 路径仍保留首次登记到 `data/voice-adapter/voice_id.txt` 的流程，但原型号在当前上游入口已不可用，不推荐新部署继续使用。

## 单词解读

QQ @ 夏莉或在微信私聊发送“解读 unhappy”“讲讲 reusable 这个单词”，会先收到一句自然的夏莉式引入，再展示固定解析栏目，随后附单词读音；“解读 unhappy，只要文字”跳过语音，“只要解析，不要开场白”省略引入。文字可供所有可聊天成员使用，语音仍受原渠道开关、身份权限和长度限制；未启用或无权限时会在卡片末尾说明。语音合成失败不影响已发送的文字。

单词读音采用学习语速：在现有夏莉声音上保持音调、约 0.8 倍速，开头留 200 毫秒、结尾留 350 毫秒，便于听清；“再读一下”也可使用该模式。普通语音继续原来的速度。语音适配器先检查原音频完整性，再处理节奏并编码为 QQ SILK 或微信 WAV；没有新增语音供应商或切换音色。

一次解读一个英文词，可含常见连字符或撇号。拼写有误先确认；多词、短语和混合任务请拆开。可在同一会话继续说“再读一下”。内容是模型讲解，未知音标会标注，词根不确定时不强行拆解，不宣称经过词典核验。读音使用当前夏莉音色；多读音词默认常见独立形式，不保证指定英美口音或精确音素。

规则在 `app/prompts/word_learning.md`，程序显式加载并随应用镜像发布；不是自动安装的 Codex Skill。主模型调用 `submit_word_card`，程序校验排版并结束工具循环，避免漏栏目、重复朗读和二次改写。更新规则需随应用重启。

维护者可使用现有模型环境运行 `python scripts/check_word_learning.py --output-dir /tmp/word-learning-probe`，只进行合成对话和模拟平台发送；默认模拟 TTS。追加 `--synthesize` 会调用实际语音服务并保存 WAV 样本供试听。可用 `--env-file` 指定私有配置文件；运行结果只包含合成样本，不输出模型凭据。脚本通过表示流程正确，语义准确性及发音仍需核验。

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
- `/files`、`/read note.md`：浏览或读取本群隔离工作区；`/read table.xlsx` 可读取有界的 Excel 单元格。
- `/write note.md 一段内容`：写入 UTF-8 文本文件。
- `/send note.md`：通过 QQ 官方分片上传接口发送工作区文件。
- `/voice 想说的话`：使用夏莉音色发送一条 QQ 语音。

文件修改默认只允许群主或管理员操作。`.xlsx` 上传后只会保存，不会自动读取或处理；需要再 `@` 机器人明确提出读取请求。旧版 `.xls` 暂不支持。容器只挂载 `./workspace:/workspace`，不挂载宿主机其他目录、SSH 密钥或 Docker Socket；即使模型受到提示词注入，也无法越过这个目录访问宿主机。
默认单文件上限为 50MB、每群工作区总配额为 500MB，均可在 `.env` 中调整。

使用 Anthropic 或 OpenAI 兼容接口时，老师、群主和管理员可以自然地说“帮我新建一个 todo.md”“读一下 notes/周报.md”“把刚才生成的文件发出来”，机器人会通过受限工具完成。两种接口均支持自然语言工具循环，具体能力按开关和身份授权。

所有群成员都可以通过自然语言使用 `research`、`web_search`、`fetch_url` 和 `get_weather`，由主模型按请求选择工具。`research` 提供多来源核验，Galgame、会社、角色和作品归属会额外查 VNDB；程序不再按事实核验关键词提前分流。SearXNG 不健康时明确报告联网失败，不回退到 Bing RSS 或模型记忆。普通网页读取仍会拒绝本机、内网、保留地址与非标准端口。

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
