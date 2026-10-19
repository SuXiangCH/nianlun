# Agent 版本与渠道发布

日期：2026-10-19。用户已确认设计并授权实现。

## 产品边界

草稿可编辑、调试；版本是不可变配置快照（r1、r2）；渠道引用某个版本。
第一阶段支持 API，沿用现有 application_deployments 的 API 入口，不创建不可用的微信渠道。
后续渠道复用 application_releases，分别维护运行版本与启停状态。

详情页提供 Agent 配置、版本管理、发布渠道、运行日志。版本列表提供创建、发布、查看/比较配置和复制到草稿。
统一发布面板选择已保存草稿或已有版本，并显示 API 当前版本、目标配置和变更。
草稿创建版本与部署是两个请求：创建成功后部署失败保留版本，面板切到该版本以便重试。
存在未保存修改时可以发布历史版本，但不能把未保存的表单当作当前草稿发布。

## 数据与接口

- application_releases 增加 description（最多 500 字）；已有记录迁移为空说明。
- application_deployment_events 记录 API 的 deploy/enable/disable、前后版本、启停状态、入口修订和时间。
- POST /apps/{id}/releases：expected_config_version、description，创建快照，不改变渠道。
- GET /apps/{id}/releases/{release_id}：读取指定版本。
- POST /apps/{id}/deployments：channel_type=api、release_id、expected_deployment_version。
- GET /apps/{id}/deployments：分页操作记录。
- POST /apps/{id}/releases/{release_id}/restore：expected_config_version，将运行字段复制到草稿，保留名称描述。
- 原有 POST publish 保持兼容，成功变更也记录部署事件。

## 一致性与限制

版本创建检查草稿修订和依赖；同一最新配置及说明的重试复用已有版本。
部署检查版本归属、当前程序兼容、模型实际配置/凭据、committed 知识库和入口修订。
事务内再次检查资源、入口修订；切换入口与写操作记录原子完成。失败不影响当前入口。
未创建 API 渠道时拒绝新部署接口；首次部署启用入口，已有入口保持启停状态。
相同版本重复部署不增加入口修订或操作记录。启停操作也保留记录。
复制草稿需要确认覆盖及草稿修订检查，不恢复历史密钥、知识内容或旧程序。
新会话使用当前渠道版本，旧会话继续原版本；知识库每轮使用最新 committed 内容。
不增加版本删除、灰度发布、审批、真实模型探活或多渠道批量事务。

## 验证

服务层/API：独立建版本、历史版本上线、停用保持、并发冲突、跨 Agent 拒绝、依赖失败、草稿复制、分页与脱敏。
迁移：旧数据库增加说明与操作记录表后重复启动，原入口与版本保留。
前端：建版本、已有版本发布、失败重试、比较/复制、分页、未保存修改与禁用状态；桌面/手机截图。
