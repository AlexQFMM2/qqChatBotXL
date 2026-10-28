# 实施任务与交接

权威状态见 [README](README.md)。单执行者串行，无并行代理授权；Integration owner 为当前执行者。所有任务 `Parallel candidate: no`，共用源码、配置、数据库的操作按依赖执行。

## B0

### G-001 基线、契约与范围确认

- Owner：当前执行者。
- Repository/files：qqChat 与 CB 规则、配置及本计划；怪猎只保留延期记录。
- Inputs：用户已确认修订方案与原工作区状态。
- Outputs：服务端变更边界、全局单局、5 分钟超时、原会话总结契约。
- Depends on：无。
- Acceptance：范围与现有入口相符；客户端与用户原有修改不受影响。
- Shared resources：工作区与部署配置；不触发生产操作。
- Handoff：README 和后续任务。

## B1

### G-002 宝可梦图鉴 HTTP 包装

- Owner：当前执行者。
- Repository/files：CB `apps/api/src/pokemonHttp.ts`、`server.ts` 与专项测试。
- Inputs：现有 dex facade 和鉴权。
- Outputs：搜索、详情与有限学招分页；固定分类/身份，来源版本。
- Depends on：G-001。
- Acceptance：A-01；不提供任意 SQL / 路径 / URL。
- Shared resources：与 G-004 共用 server，串行。
- Handoff：图鉴契约和测试。

### G-003 怪猎无界面图鉴（延期）

- Owner：后续维护者。
- Repository/files：mh-save-edit；本期不修改。
- Inputs：后续明确恢复怪猎范围的请求。
- Outputs：届时重新核对范围；保留现有 Repository 复用方向。
- Depends on：无；从本期所有任务依赖中移除。
- Acceptance：不参与本期验收，不标已完成。
- Shared resources：本期无。
- Handoff：延期记录。

## B2

### G-004 单局生命周期、幂等和超时

- Owner：当前执行者。
- Repository/files：CB `apps/api/src/chatBattle.ts`、`server.ts`、`chat-battle-smoke.ts`。
- Inputs：生成器、battle service、G-001 契约。
- Outputs：唯一名额、固定候选、六选三、p2 后生成、原 AI、操作去重、epoch、独立到期任务。
- Depends on：G-002。
- Acceptance：A-02/A-05/A-06；只限制聊天局，B 不能抢占；无请求也超时。
- Shared resources：CB 单实例状态，释放必须检查本局身份。
- Handoff：本地接口与故障证据。

### G-005 玩家视图与合法动作

- Owner：当前执行者。
- Repository/files：CB `chatBattle.ts` 和专项测试；复用现有引擎导出。
- Inputs：p1 request / player stream、原命令合法性校验。
- Outputs：稳定编号、合法动作、公开事件/血量及终局摘要。
- Depends on：G-004。
- Acceptance：A-03/A-07；自然终局、强制换人、无隐藏信息泄漏。
- Shared resources：同协调模块；不改引擎规则或客户端。
- Handoff：公开视图与运行证据。

## B3

### G-006 客户端、持久绑定与恢复

- Owner：当前执行者。
- Repository/files：qqChat `app/pokemon.py`、`app/config.py`、新增测试。
- Inputs：G-002/G-005 接口与现有 SQLite。
- Outputs：HTTP 客户端、一局一记录、固定操作键、响应丢失核对、重启恢复与中断。
- Depends on：G-005。
- Acceptance：A-04/A-05/A-06；数据库先保存待执行信息，未知状态不另发新动作。
- Shared resources：各渠道现有 SQLite；不作为长期记忆。
- Handoff：存储/客户端测试。

### G-007 主模型、忙碌拦截与总结通知

- Owner：当前执行者。
- Repository/files：qqChat `app/bot.py`、`app/channel.py`、`app/qq.py`、微信 transport/runtime、工具文档及测试。
- Inputs：G-006、现有模型工具和传输边界。
- Outputs：两个宝可梦工具，跨渠道忙碌拦截，事实战况，5 秒检查，原会话终局通知去重。
- Depends on：G-006。
- Acceptance：A-04/A-07；B 的所有消息不进入业务；正常结束立即总结，发送失败不阻塞新局。
- Shared resources：消息序号、上下文、运行队列；后台不能覆盖微信 active。
- Handoff：传输替身和共享 bot 测试。

## B4

### G-008 联调、回归与实现复核

- Owner：当前执行者。
- Repository/files：两个仓库专项/原测试，本计划。
- Inputs：G-002/G-004～G-007 实现。
- Outputs：实际命令、结果、版本、未测范围和问题修复。
- Depends on：G-007。
- Acceptance：A-01～A-08 本地可验证部分；完整 qqChat 测试、CB 类型/构建/相关 smoke，通过故障注入验证真实边界。
- Shared resources：测试引擎和隔离端口，不使用生产对局/真实聊天。
- Handoff：本地验收证据及部署说明。

## B5

### G-009 实际部署与用户体验验收（已上线，手机验收待确认）

- Owner：维护者与届时执行者。
- Repository/files：现有部署入口、配置示例、运维说明及计划索引。
- Inputs：G-008、已审查候选和实际部署范围。
- Outputs：同机内部可达服务、平台通知权限/上下文验证、真实体验记录和回退路径。
- Depends on：G-008。
- Acceptance：原客户端继续工作；至少一端完整对局，另一端跨渠道忙碌/恢复；超时通知真实送达。替身不能替代手机确认。
- Shared resources：真实运行服务、账号和用户对局；不发布桌面/Web 客户端。
- Handoff：验收后同步计划状态，未测不得勾完成。
