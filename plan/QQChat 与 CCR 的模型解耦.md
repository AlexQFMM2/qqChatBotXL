## QQChat 与 CCR 的模型解耦

### 修改目的

QQChat 原来直接使用具体的 DeepSeek 模型名，例如：

```env
LLM_MODEL=deepseek-v4-flash
```

这样 DeepSeek 每次升级或下线模型，都需要同步修改 QQChat 配置并重启服务。

现在改为让 QQChat 永久调用 CCR 中的 Fusion 虚拟模型：

```text
Fusion/qqchatbot
```

模型调用链路变为：

```text
QQChat
  ↓
Fusion/qqchatbot
  ├─ DeepSeek/deepseek-flash（总控与最终回复）
  └─ vision_understand_qqchatbot
       ↓
     千问/qwen3.7-flash（普通 QQ 图片理解）
```

以后更换实际模型时，只需要在 CCR 后台修改 `Fusion/qqchatbot` 的基础模型，不需要修改 QQChat。

### CCR 后台配置

已在 CCR 中创建 Fusion 虚拟模型：

```text
名称：qqchatbot
完整模型名：Fusion/qqchatbot
基础模型：DeepSeek/deepseek-flash
```

当前配置原则：

- `Fusion/qqchatbot` 是 QQChat 的稳定总控模型入口。
- 普通 QQ 图片通过它的内部工具 `vision_understand_qqchatbot` 识别。
- 视觉模型由 `metadata.fusionVision.modelSelector` 在 CCR 后台配置。
- DeepSeek 更新模型后，在 CCR 后台修改它的“基础模型”即可。
- QQChat 不再写死 `deepseek-v4-flash`、`deepseek-v4-pro` 等具体模型名。
- QQChat 使用 `VISION_ROUTE=fusion` 时不得再提前调用自己的视觉助手，避免重复识图。

### QQChat 配置修改

本地和服务器 QQChat 的主模型配置已改为：

```env
LLM_MODEL=Fusion/qqchatbot
```

本地模型目录配置为：

```env
MODEL_CATALOG_JSON={"main":"Fusion/qqchatbot","kimi":"Kimi API（国内）/kimi-k3[1m]"}
```

如果不需要保留 Kimi 备用入口，也可以简化为：

```env
MODEL_CATALOG_JSON={"main":"Fusion/qqchatbot"}
```

涉及的本地文件：

```text
qqChat/.env
qqChat/.env.example
qqChat/README.md
```

其中 `.env` 是实际运行配置，不应提交到 Git；`.env.example` 和 README 用于记录推荐配置。

### 图片模型说明

普通 QQ 图片已经迁移到 CCR Fusion；服务器当前使用：

```env
VISION_ROUTE=fusion
```

迁移期间仍保留旧链路作为回滚：

```env
VISION_ROUTE=direct
VISION_MODEL=qwen3.7-flash
```

因此：

- `fusion` 模式把 QQChat 选中的图片直接交给 `Fusion/qqchatbot`，由 CCR 内部工具识图。
- `direct` 模式仍由 QQChat 调用 `VISION_MODEL`，供紧急回滚使用。
- Bilibili 成片联系表暂未迁移，仍使用 `VISION_MODEL`。
- 生图、图片编辑和语音模型继续使用各自独立配置。
- 旧视觉链路至少保留到 Fusion 稳定运行 48 小时后再计划清理。

### API 配置说明

QQChat 仍然需要配置 CCR 的访问地址和访问密钥：

```env
LLM_BASE_URL=https://你的-CCR-地址
LLM_API_KEY=你的-CCR-Key
LLM_MODEL=Fusion/qqchatbot
```

代码中没有写死 DeepSeek 官方 API 地址或具体 DeepSeek 模型名。

这里的职责划分是：

```text
QQChat
  ├── 只知道 CCR 地址、Key 和 Fusion/qqchatbot
  ├── 决定哪些 QQ 图片可以进入模型请求
  │
CCR
  ├── 保存实际供应商配置
  ├── 决定使用哪个 DeepSeek 模型
  ├── 决定 vision_understand 使用哪个视觉模型
  └── 处理模型路由、限额和计费
```

### 服务器修改及验证

腾讯云上的 QQChat 已改为使用：

```env
LLM_MODEL=Fusion/qqchatbot
VISION_ROUTE=fusion
```

修改后已重启并检查：

- `qqchat-bot` 容器状态正常。
- 普通消息回复正常。
- QQChat 客户端工具调用正常。
- `Fusion/qqchatbot` 可以成功路由到 `DeepSeek/deepseek-flash`。
- 普通识图、OCR、多图和无图防猜测验证正常。
- 图片与 QQChat 客户端工具共存正常，客户端不会收到 CCR 内部视觉工具调用。
- CCR 视觉工具超时已设为 90 秒；具体实施记录见《QQChat 图片识别迁移至 CCR Fusion 工具》文档。

后续修改基础模型后，可以这样检查：

```bash
ssh_admin
```

连接“个人腾讯云服务器”后：

```bash
docker ps
docker logs --tail 100 qqchat-bot
```

然后在 QQ 中实际发送一条普通消息，并测试一次文件或客户端工具调用。

### 后续模型升级流程

当 DeepSeek 再次发布或下线模型时：

1. 在 CCR 后台确认新模型已经同步或手动添加。
2. 编辑 `Fusion/qqchatbot`。
3. 将它的基础模型切换到新的 DeepSeek 模型。
4. 保存后直接测试 QQChat。
5. 通常不需要修改 QQChat 的 `.env`。
6. 通常也不需要重新构建或重启 QQChat。

只有以下内容变化时，才需要修改 QQChat：

- CCR 地址发生变化。
- CCR API Key 发生变化。
- `Fusion/qqchatbot` 被改名或删除。
- 需要切换 `VISION_ROUTE` 回滚普通识图。
- QQChat 自身的 Bilibili 成片视觉、语音、生图等独立模型需要调整。
