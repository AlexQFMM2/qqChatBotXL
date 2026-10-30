# 计划目录

更新时间：2026-10-30（Asia/Shanghai）。各计划 README 的 `Status` 为状态唯一来源；本文件只负责索引。

新会话先读 [夏莉项目交接入口](../README.md)：当前能力、已修复问题、验收证据、线上版本、版本管理状态及接手顺序。

## 进行中

| 计划 | Owner | 更新时间 | 路径 |
|---|---|---|---|
| 夏莉单词解读与读音（bf22a13 已推送上线 QQ / 微信，367 项回归、真实预检及双端健康通过；待用户手机试听） | Codex，单执行者串行 | 2026-10-30 | [计划](../word-learning/README.md) · [任务](../word-learning/tasks.md) · [台账](../word-learning/taskList.md) |
| 宝可梦图鉴与全局单局对战（基础版已上线且用户确认可用；G-010/G-011已上线；340项回归及真实模型归属重放通过，待手机复测，怪猎延期） | 当前执行者，单执行者串行；维护者负责最终体验验收 | 2026-10-28 | [计划与接口设计](../game-catalog-battle/README.md) · [任务](../game-catalog-battle/tasks.md) · [台账](../game-catalog-battle/taskList.md) |
| QQChat 微信私聊与共享长期记忆（双端已常驻，最新时间修复 305 项应用测试；真实跨端与 10-23 实际整理已确认，24 小时观察从 10-23 15:01:45 重算；共享文件延期） | 当前执行者负责实现与集成；项目维护者负责扫码与用户验收 | 2026-10-23 | [交接记录](../wechat-private-chat/handoff.md) · [计划与方案取舍](../wechat-private-chat/README.md) · [任务](../wechat-private-chat/tasks.md) · [进度](../wechat-private-chat/taskList.md) |

## 已完成

| 计划 | Owner | 更新时间 | 路径 |
|---|---|---|---|
| Fusion 统一扩展配置与主模型工具调度（300 项回归、真实主控/自动夏莉参考预检和双端发布通过；用户确认微信头像图片收到、语音可播放） | Codex，单执行者串行 | 2026-10-23 | [计划](../fusion-media/README.md) · [任务](../fusion-media/tasks.md) · [进度](../fusion-media/taskList.md) |

## 既有单文件资料

以下文档保留原状，本次没有重新核验其线上实施状态，也未将其改记为完成或归档。

| 资料 | 路径 |
|---|---|
| QQChat 与 CCR 的模型解耦 | [原文](../QQChat%20与%20CCR%20的模型解耦.md) |
| QQChat 图片识别迁移至 CCR Fusion 工具 | [原文](../QQChat%20图片识别迁移至%20CCR%20Fusion%20工具.md) |

## 存储说明

`qqChat` 是独立 Git 仓库。2026-10-21 核查时，`plan/` 未跟踪且未忽略；2026-10-22 用户要求提交 GitHub，本轮将计划与代码一并纳入版本管理。私有状态、虚拟环境、运行凭证、库、工作区、发布目录和观察数据已忽略。计划不保存真实凭证、二维码、验证码、会话令牌或私人聊天。
