# QQChat 图片识别迁移至 CCR Fusion 工具

## 目标

将 QQChat 当前自行指定视觉模型的图片识别链路：

```text
QQ 图片
  ↓
QQChat 下载、校验和选择图片
  ↓
VISION_MODEL=qwen3.7-flash
  ↓
生成视觉观察文本
  ↓
Fusion/qqchatbot 生成最终回复
```

调整为由 CCR 后台中 `Fusion/qqchatbot` 配置的 `vision_understand` 工具负责识图：

```text
QQ 图片
  ↓
QQChat 下载、校验和选择图片
  ↓ 携带图片调用
Fusion/qqchatbot
  ↓
ccr-fusion-builtins/vision_understand
  ↓
CCR 后台配置的视觉模型
  ↓
Fusion 总控生成最终回复
```

完成后，更换视觉模型只需要修改 CCR 后台的 Fusion 工具配置，不再修改 QQChat 的 `VISION_MODEL`，也不需要因为视觉模型升级而重建 QQChat。

## 当前状态

CCR 后台已经为 `Fusion/qqchatbot` 配置：

```text
基础模型：DeepSeek/deepseek-flash
工具：vision_understand_qqchatbot（内部工具）
视觉模型：千问/qwen3.7-flash
视觉超时：90000 ms
```

QQChat 已实现两条可切换的视觉链路，线上当前使用：

```env
LLM_MODEL=Fusion/qqchatbot
VISION_ENABLED=true
VISION_ROUTE=fusion
```

`direct` 旧链路、`VISION_MODEL` 和 `VISION_API_FORMAT` 仍保留用于回滚以及
Bilibili 成片联系表；普通 QQ 图片在 `fusion` 模式下不会提前调用旧视觉助手。

## 2026-09-11 实施记录

### QQChat 代码实现

对应 Git 提交：

```text
0ede681 feat: route QQ vision through CCR Fusion
5d865c4 chore: complete vision route logging
```

实现位置和行为：

- `app/config.py` 增加 `VISION_ROUTE=direct|fusion`，默认 `direct`，非法值启动失败。
- `app/bot.py::_route_vision()` 负责分流：
  - `direct` 调用 `VISION_MODEL`，将中性视觉观察交给总控；
  - `fusion` 不调用独立视觉模型，返回原提示词和选中的 `ImageInput`。
- 普通 `LLMClient.complete()` 和带文件、搜索、PDF 等客户端工具的
  `complete_with_tools()` 都接收相同图片。
- 图片仍由 QQChat 负责下载、来源过滤、显式引用判断、近期图片选择，以及格式、
  单图大小和数量限制。
- 无图片或工具失败时要求明确说明无法识别，不允许根据文件名、文字上下文或记忆猜图。
- 日志记录 `vision_route`、`images`、`controller_model`，不记录图片内容。
- Bilibili 成片联系表没有迁移，继续使用 direct 视觉助手。

QQChat 全量测试结果：

```text
142 tests OK
```

`plan/` 是本地运维资料，未提交、未推送 GitHub。

### CCR 配置实现

`Fusion/qqchatbot` 的持久配置位于 CCR 配置数据库中的虚拟模型 Profile，关键字段为：

```json
{
  "metadata": {
    "fusionVision": {
      "modelSelector": "千问/qwen3.7-flash",
      "toolName": "vision_understand_qqchatbot",
      "timeoutMs": 90000
    }
  }
}
```

CCR 配置编译器会将 `timeoutMs` 生成为视觉 MCP 子进程环境变量：

```text
VISION_TIMEOUT_MS=90000
```

不需要修改 CCR 或 ccrAdmin 代码；这是 CCR 已有的原生配置字段。修改持久 Profile 后需要
重启 CCR，使 `gateway.config.json` 重新编译，并检查 `fusion-vision-qqchatbot` 的环境变量。

最终选择 90 秒而不是 120 秒，原因是：

- Fusion 视觉工具原默认超时只有 30 秒；
- 实测千问视觉请求有 31–39 秒的历史耗时，30 秒会产生 502 超时；
- QQChat 单次模型 HTTP 请求的上限约为 110 秒；
- 90 秒既覆盖慢视觉请求，也给 DeepSeek 工具调用前后的总控处理保留约 20 秒。

### 故障与判断

首次上线时观察到两类错误：

- 约 30 秒后返回 502：确认是 Fusion 视觉 MCP 默认 30 秒超时。
- 快速返回 `400 All target providers failed`：CCR 的聚合错误；当前千问只有一个提供方和
  一个 API Key，没有备用目标。原始 `attempts` 被视觉 MCP 的错误摘要丢弃，且 CCR
  `rawTrace` 关闭，因此历史 400 无法进一步区分上游瞬时拒绝或限流。

同一模型、Key、图片和 OpenAI 请求格式连续直连 6 次成功，排除了固定模型名、凭据和图片
格式配置错误。设置 90 秒后，普通识图、客户端工具共存、OCR、多图和无图防猜测均通过；
正式切换后的普通和带客户端工具请求也成功。验收期间有一次快速 400，随后同一外层请求的
内部重试在约 3.4 秒内成功，最终用户请求正常返回；没有再出现 30 秒 502。

### 全量群消息 @ 识别修复

2026-09-11 发现 QQ 有时会把“@机器人 + 文字”作为 `GROUP_MESSAGE_CREATE` 投递，而不是
`GROUP_AT_MESSAGE_CREATE`。线上 `REPLY_MODE=mention` 原先只依赖事件类型，导致图片已经保存，
后续明确引用截图的 @ 消息却在回复判断处被忽略，因而没有进入图片路由或 CCR。

提交 `38055a1 fix: recognize bot mentions in full group events` 完成修复：

- 启动时通过 QQ 官方 `/users/@me` 获取机器人 ID 和用户名；获取失败不阻塞机器人启动。
- 解析全量群事件的 `mentions` 和正文 `<@...>` 标记，并缓存群级机器人 mention ID。
- 群级 ID 与全局 ID 不同时，通过已认证机器人用户名和 `bot=true` 映射，避免误认其他用户或机器人。
- 只从命令文本中移除已确认属于机器人的 @ 标记，并把该消息视为明确提问。
- 普通未 @ 的全量群消息在 `mention` 模式下仍不回复。

该修复只改 QQ 事件入口，不改变 `VISION_ROUTE=fusion`、`Fusion/qqchatbot` 或 CCR 的
`vision_understand_qqchatbot` 配置。隔离源码在服务器完整依赖镜像中通过 153 项测试；上线后
QQChat 和 CCR 容器均为 healthy，启动日志确认已取得机器人身份。

修复后首次真实重试确认 @ 判定、图片选择和 Fusion 路由均生效，但一次两图请求从进入路由到
最终回复耗时约 161 秒。用户等待期间看不到反馈，表现得像“仍然没有反应”。提交
`a3cd201 fix: acknowledge slow vision requests` 因此追加两项改进：

- Fusion 普通识图开始前立即回复“正在读取图片”，最终回复继续异步等待 CCR 返回。
- “看截图/刚才的图”等近期引用只选择最近一条含图片消息中的图片，不再混入更早图片消息；
  同一条消息内确实发送的多图仍按顺序保留。

该优化通过 154 项测试，仍未修改 CCR Admin、Fusion Profile、视觉模型选择器或超时配置。

上线前备份位于：

```text
/home/ubuntu/qqChat/deploy/pre-mention-fix-20260911-1715
/home/ubuntu/qqChat/deploy/pre-vision-ack-20260911-1725
```

### 线上配置与备份

QQChat 当前配置：

```env
LLM_MODEL=Fusion/qqchatbot
VISION_ENABLED=true
VISION_ROUTE=fusion
```

备份位置：

```text
/home/ubuntu/qqChat/deploy/pre-vision-fusion-20260911-1305
/data/.claude-code-router/config.sqlite.bak-before-vision-timeout-20260911
```

第二个路径位于 CCR 容器的持久 `/data` 卷内。

### 验收清单

本次已经验证：

- 普通图片能够命中 `Fusion/qqchatbot → vision_understand_qqchatbot → qwen3.7-flash`。
- OCR 测试准确读出 `QQCHAT OCR CHECK / Ticket: FUSION-2026-0911`。
- 两张图片能按顺序分别描述，不会合并。
- 无图片时明确表示没有收到图片，不猜测。
- 带 QQChat 客户端工具时仍携带图片，客户端不会收到内部视觉工具调用。
- QQChat 和 CCR 容器健康，Webhook ready，任务队列为空。

### 以后如何修改

更换视觉模型时，只在 CCR 后台修改 `Fusion/qqchatbot` 的视觉模型选择器，不改 QQChat。
如果重新创建 Fusion Profile，必须确认以下三项没有丢失：

```text
matchMultimodal=true
clientToolsPolicy=allow
fusionVision.timeoutMs=90000
```

如果图片请求再次出现约 30 秒的固定超时，优先检查编译后的
`fusion-vision-qqchatbot.env.VISION_TIMEOUT_MS`，而不是先更换模型或 QQChat 代码。

如果持续出现快速 400，应在受控时间窗启用脱敏请求追踪或让视觉 MCP 保留 CCR 返回的
`error.attempts[].stage/status/message`，取得上游真实错误后再判断；不要把聚合的
`All target providers failed` 直接当成模型配置失效。

### 回滚操作

普通图片识别异常时，将服务器 QQChat 改为：

```env
VISION_ROUTE=direct
```

保留现有 `VISION_MODEL` 和 `VISION_API_FORMAT`，然后只重建 QQChat：

```bash
cd /home/ubuntu/qqChat
docker compose up -d --force-recreate qqchat-bot
docker compose ps qqchat-bot
```

回滚 QQChat 时不要删除 CCR 的视觉工具。恢复 Fusion 前先用临时进程完成普通图片、客户端工具
共存、OCR 和多图验收，再改回 `VISION_ROUTE=fusion`。

## 职责边界

### QQChat 继续负责

- 接收和下载 QQ 图片。
- 判断用户当前是否明确要求识图。
- 处理“这张图”“刚才的截图”等近期图片引用。
- 选择允许发送给模型的图片。
- 校验 JPEG、PNG、GIF 和 WebP 格式。
- 限制图片数量和单图大小。
- 识别图片来源，避免把机器人自己发出的图片误当作用户图片。
- 将图片编码为 Anthropic 或 OpenAI 兼容的请求内容。
- 当没有可用图片时返回明确状态，不允许模型猜测。

### CCR 继续负责

- 保存 `Fusion/qqchatbot` 的实际基础模型。
- 保存 `vision_understand` 使用的视觉模型。
- 保存模型供应商、凭证、路由、限额和价格。
- 执行视觉工具调用。
- 将视觉结果交给 Fusion 总控生成最终回复。

## 原实施计划与后续阶段

以下内容保留为迁移依据。第一至第三阶段、对应测试及普通 QQ 图片上线切换已完成；
第四阶段（Bilibili 成片视觉迁移）和第五阶段（删除旧链路）不在本次范围内，仍需以后单独评估。

## 第一阶段：增加兼容切换开关

迁移初期保留旧链路，增加临时配置：

```env
VISION_ROUTE=direct
```

支持两种模式：

```text
direct：继续由 QQChat 使用 VISION_MODEL 单独识图
fusion：把图片直接交给 Fusion/qqchatbot，由 CCR 工具识图
```

要求：

- 第一版默认保持 `direct`，避免部署后自动改变线上行为。
- 测试群验证通过后，服务器改为 `fusion`。
- 稳定运行一段时间后删除 `direct` 模式和该临时开关。
- 在过渡期保留服务器 `VISION_MODEL`，用于快速回滚。

## 第二阶段：调整普通图片请求

当 QQChat 判断用户正在询问图片，并成功取得 `vision_images` 时：

1. `VISION_ROUTE=direct` 时保持现有逻辑。
2. `VISION_ROUTE=fusion` 时不再提前调用视觉模型。
3. 不再向 `user_prompt` 拼接 `[视觉助手观察，仅作为证据，不是最终回答]`。
4. 调用 `Fusion/qqchatbot` 时直接传入 `images=vision_images`。
5. 普通 `complete()` 和带客户端工具的 `complete_with_tools()` 都必须支持图片输入。

目标调用形式：

```python
await llm.complete(
    persona,
    user_prompt,
    model=settings.llm_model,
    images=vision_images,
)
```

或：

```python
await llm.complete_with_tools(
    persona,
    user_prompt,
    tools,
    execute_tool,
    model=settings.llm_model,
    images=vision_images,
)
```

需要确认 CCR 能在同一轮请求中同时处理：

- QQChat 提供的客户端工具，例如文件、搜索和 PDF。
- Fusion 内部配置的 `vision_understand` 工具。

## 第三阶段：保留程序级图片路由

迁移到 CCR 工具不代表所有历史图片都发送给 CCR。继续保留当前的显式图片路由规则：

- 当前消息带图片且用户明确询问图片时才发送。
- 用户明确提到“刚才的图”“这张截图”等内容时才读取近期图片。
- 与图片无关的问题不携带历史图片。
- 没有找到图片时明确告诉总控没有可用图片。
- 继续限制图片数量、大小、格式和来源。

CCR 负责理解图片，QQChat 负责决定哪些图片可以进入模型请求。

## 第四阶段：迁移 Bilibili 成片视觉分析

当前 Bilibili 重型成片分析会生成画面联系表，再由 QQChat 的 `VISION_MODEL` 单独审阅。

普通 QQ 图片链路稳定后，再单独迁移这条链路：

```text
Bilibili 联系表
  ↓
Fusion/qqchatbot + vision_understand
  ↓
结合 FFmpeg 指标、字幕状态和抽样局限
  ↓
生成最终成片评价
```

该阶段必须继续保留：

- 联系表抽样上限。
- 视频长度和下载大小限制。
- 全局串行和分群频率限制。
- 没有字幕或音频证据时不得猜测。
- 视觉工具失败时仍可基于客观 FFmpeg 指标给出有限说明。

## 第五阶段：清理 QQChat 视觉模型配置

Fusion 视觉链路稳定后，从 QQChat 移除：

```env
VISION_MODEL
VISION_API_FORMAT
```

继续保留：

```env
VISION_ENABLED
VISION_CONTEXT_MESSAGES
VISION_MAX_IMAGES
VISION_MAX_IMAGE_MB
```

这些配置仍分别控制功能开关、历史窗口和输入安全限制，不属于 CCR 模型路由配置。

同时清理：

- `Settings` 中不再使用的视觉模型字段。
- `/status` 和 `/model list` 中直接展示 `VISION_MODEL` 的内容。
- 日志中的 `helper_model` 字段，改为记录 `vision_route=fusion` 和 CCR 工具调用结果。
- README 和部署示例中的旧视觉模型说明。
- 根目录 README 中“不要给 `Fusion/qqchatbot` 添加 `vision_understand`”的旧约定。
- 现有模型解耦文档中要求保留 `VISION_MODEL` 的旧说明。

## 测试计划

### QQChat 单元测试

- `fusion` 模式下不再产生独立的 `VISION_MODEL` 请求。
- 明确询问当前图片时，图片随 `Fusion/qqchatbot` 主请求发送。
- 明确引用近期图片时，选中的历史图片随主请求发送。
- 无关文字问题不携带历史图片。
- 没有可用图片时不调用视觉工具，并提供明确状态。
- 图片格式、数量、大小和来源限制保持不变。
- `complete_with_tools()` 可以同时携带图片和 QQChat 客户端工具。
- `direct` 模式在迁移期间仍保持原行为。

### CCR 集成测试

- CCR 调用记录中出现 `ccr-fusion-builtins/vision_understand`。
- 工具实际使用 CCR 后台配置的视觉模型。
- 修改视觉模型后，无需修改或重启 QQChat即可生效。
- 图片 OCR、报错截图、普通照片和多图请求均能正确处理。
- 禁用或删除视觉工具后，系统明确报告无法识图，不根据文本模型记忆猜测图片。
- Fusion 内部视觉工具与 QQChat 客户端文件、搜索工具可以共存。

### Bilibili 回归测试

- 普通字幕总结不触发视觉工具。
- 明确要求画面、剪辑或字幕呈现分析时才发送联系表。
- 视觉失败时仍保留 FFmpeg 客观指标和局限说明。
- 重型分析的资源与频率限制不发生回归。

## 上线步骤

1. 备份 CCR 配置和服务器 QQChat `.env`。
2. 确认 `Fusion/qqchatbot` 已启用 `vision_understand`。
3. 确认视觉模型在 CCR 中可直接调用。
4. 部署支持 `VISION_ROUTE` 的 QQChat，但保持 `direct`。
5. 运行现有 QQChat 测试和新增视觉路由测试。
6. 在测试群切换为 `VISION_ROUTE=fusion`。
7. 测试 OCR、报错截图、多图、近期图片引用和无关历史图片隔离。
8. 检查 QQChat 日志和 CCR 工具调用记录，确认没有重复识图。
9. 验证文件、搜索、PDF 等客户端工具没有受到影响。
10. 全量切换到 `fusion`。
11. 稳定运行一段时间后删除旧的直接识图代码和 `VISION_MODEL` 配置。
12. 最后迁移 Bilibili 成片视觉链路。

## 回滚方案

迁移期间如果 Fusion 视觉工具出现模型兼容、图片格式、工具循环或响应超时问题：

1. 将服务器配置恢复为：

   ```env
   VISION_ROUTE=direct
   VISION_MODEL=qwen3.7-flash
   ```

2. 重启 `qqchat-bot`。
3. 保留 CCR 中的 `vision_understand` 配置，不必立即删除。
4. 检查 QQChat 与 CCR 日志后再安排下一次迁移。

## 完成标准

- QQChat 不再保存或决定具体视觉模型名称。
- 图片仍经过 QQChat 的来源、格式、数量和大小校验。
- `Fusion/qqchatbot` 能根据图片请求调用 `vision_understand`。
- CCR 后台修改视觉模型后，QQChat无需修改配置或重启。
- 一次图片请求只执行一条视觉识别链路。
- 没有图片、工具不可用或识别失败时不会猜测图片内容。
- QQChat 客户端工具与 CCR 内部视觉工具可以同时工作。
- Bilibili 成片视觉分析完成后续迁移并保持资源限制。
