# ADR-0010：Mattermost 身份与类型化 Scope 隔离

- 状态：Accepted
- 日期：2026-08-02

## 背景

旧实现将运行边界表示为 `mattermost:<team_id>/<channel_id>`。Mattermost DM/GM 没有 Team，Team 也不等于
企业租户；用户画像仅以 `user_id` 为主键，群聊还会加载发言人的私人画像。这些边界不足以支持个人工作台、
多实例接入和长期记忆。

## 决策

1. 每个部署显式配置稳定的 Mattermost Installation 和 MMAG Tenant ID；Team 只作为频道元数据。
2. 服务端从认证事件的 `user_id` 和权威频道元数据派生 Principal 与 Scope，模型、Manifest 和工具参数
   不能指定身份或扩大 Scope。
3. Bot DM 使用 `personal` Scope；公开频道、私有频道和 GM 使用 `channel` Scope。个人 Scope ID 绑定
   owner，频道 Scope ID 绑定 channel。
4. 个人模式可加载本人画像；共享模式不注入私人画像，也不向模型投影画像 Capability。
5. SQLite 记忆按 Installation + Tenant 分区；Artifact 继续精确匹配 Scope；Checkpoint 恢复必须匹配
   原 actor、scope、installation 和 tenant。
6. Bot Token 只代表服务身份。审批与后续资源访问仍需按原用户执行动态授权，失败时默认拒绝。

## 结果与边界

- 当前 Scope 形式为 `mattermost:<installation>:<tenant>:usr:<user>` 或
  `mattermost:<installation>:<tenant>:chn:<channel>`。
- 现有消息、知识、摘要、URL 缓存和用户画像已建立租户查询边界；历史数据迁移到 `default/default`，生产
  升级后应显式配置对应 ID。
- PersonalSpace、WorkCase、SkillDraft、成员变更/消息删除传播、向量索引以及生产 Sandbox 仍是后续阶段，
  本 ADR 不将它们视为已完成。
