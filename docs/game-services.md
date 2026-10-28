# 宝可梦图鉴与聊天对战

实现范围为宝可梦。怪猎图鉴延期。随机配队、规则、对手 AI 使用 ChangeBattle 现有实现，CB 网页/桌面客户端无需更新。

2026-10-28 已按用户要求在原服务器启用 QQ / 微信图鉴与对战。可发送“来一场宝可梦对战”开始测试；发布证据与待确认的手机体验见 [计划记录](../plan/game-catalog-battle/README.md#上线记录2026-10-28-1605-0800)。

## 启用

先使用包含新接口的 CB 服务端，在原受保护环境配置：

```dotenv
CHANGEBATTLE_POKEMON_CHAT_ENABLED=true
CHANGEBATTLE_POKEMON_CHAT_TOKEN=<机器人专用令牌>
```

qqChat 的 QQ 与微信环境分别配置相同的服务 URL/token：

```dotenv
POKEMON_SERVICE_URL=http://<内部服务地址>:5191
POKEMON_SERVICE_TOKEN=<与服务端一致>
POKEMON_DEX_ENABLED=true
POKEMON_BATTLE_ENABLED=true
```

代码及示例默认关闭；现有服务器已显式配置为启用。可以先只开图鉴。URL 可包含既有 API base path；容器的 localhost 仅指自身。沿用同机内部网络，无需增加公网端口。CB 服务必须单实例承载聊天协调状态，不部署多个独立副本来分流这组接口。

CB `/health` 增加 `pokemonEnabled`、`chatContractVersion=2`、`chatInstanceId`，原字段保持。新路由在无专用 token 时拒绝启用；不改变原 `CHANGEBATTLE_BATTLE_SERVICE_TOKEN`，公网客户端继续沿用房间鉴权。机器人对战启用后需先查询占用状态；服务不可达时提示状态不可用，不能在状态不明时擅自开局或放行其他业务。

## 使用与边界

- “来一场宝可梦对战”→选择环境（普通 / Mega 与 Z 招式 / 极巨化 / 太晶化）→六候选→“选 2、5、1”→按当前招式/换人编号操作。第一只是首发；对手由 CB AI 决策。
- QQ/微信所有会话合计一局，选择环境和选队也占用。其他人所有应答入口只返回忙碌，不执行媒体/图鉴/聊天。原 CB 客户端的对局不占此名额。
- 等待有效操作超过 5 分钟自动中断，不判胜负。查状态、闲聊、错误指令不会续时；取消和认输也释放名额。
- 双方固定 50 级，展示属性、性格及能力加减成、特性、道具；太晶环境另外显示太晶属性。Gen7 复用 CB 正式候选的 Mega 保底：六候选至少两只适配 Mega 并携带对应石头，其余配备匹配招式的 Z 纯晶；夏莉三只也保留 Mega/Z。卡片标明可用机制，玩家仍可自由选三只，不暗改选队。
- 普通出招不附加特殊机制；太晶、Mega、极巨化或 Z 招式必须在当前消息明确指定，例如“太晶化，用第一招”。合法选项由实际引擎返回。
- 夏莉以对战者第一人称回应。程序附真实候选/血量/合法动作，模型不能改变战斗结果。终局使用真实战况生成一次简短感想并缓存；生成失败有兜底，不延长占用。
- 终局摘要发到原群/原私聊，每局独立去重。后台每 5 秒核对本渠道绑定，只有实际存在绑定时访问 CB，无模型轮询。
- QQ 后台通知需要平台允许该群主动消息；微信使用本局短期回复上下文。平台拒发保留摘要；不明结果不自动重发。实际手机体验须部署后验证，不能以 API 或替身成功冒充送达。
- 微信回复上下文只为本局短期保留（最多 6 分钟并随当前消息更新），终局通知处理后清理，过期维护也清理。不会改变正在处理的入站消息或向任意联系人发送。

## 存储、恢复与回退

`game_battle_bindings` 随现有 SQLite 增量创建，保存可信 scope、视图、固定待执行请求、事件游标和通知结果。一局一行，保留未送达结果；已送达结果保留七天。库内可能短期有微信回复上下文，继续使用现有受保护数据卷和备份权限，不提交运行库。

HTTP 写入结果不明时保存原参数/操作键，用服务器操作状态核对；确认未接受才以原键重试。CB 进程 epoch 变化后标记中断，不重建旧局。机器人重启能找回仍存活的局；发送中重启标记结果不明，避免重复通知。

关闭新功能前让活动局结束或告知中断；回退只切换程序和开关，不回退聊天/记忆库、微信账号或游标。新增表可保留，旧代码不会读取。发布 CB 前留意原有内存对局，安排空闲窗口；不需要发布新客户端。

## 本地验证

```bash
# qqChat
PYTHONDONTWRITEBYTECODE=1 .venv-wechat/bin/python -m unittest discover -s tests -v
# 先在 CB 构建 apps/api，再运行跨仓库真实 HTTP 检查；只启动本机隔离 CB，不发社交消息。
PYTHONDONTWRITEBYTECODE=1 .venv-wechat/bin/python scripts/check_pokemon_integration.py --cb-root /path/to/changeBattleV2
```

CB 的 `pnpm --filter @changebattle-v2/api test:chat-battle` 验证生成器、3v3 自然终局、全局占用、幂等、超时和 HTTP 鉴权；原 formal-game smoke 验证既有流程兼容。

## 环境契约（版本 2）

`POST /chat-battles` 进入 `rules`，返回 `environments`；`POST /chat-battles/:id/configure` 使用同一身份、revision、operationId，提交 `environment=standard|gen7|gen8|gen9`。成功后生成固定六候选并进入 `selecting`。选环境阶段同样有 5 分钟上限，无效选择不续时；选择后不能中途更换规则。其余操作沿用原接口。

版本 2 的开局阶段与版本 1 不同，CB 与机器人应配套升级/回退；发布前确认全局聊天占用为空及原 CB 引擎空闲。原正式房间和客户端接口不变。
