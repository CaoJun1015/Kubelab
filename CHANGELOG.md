# Changelog

本项目的显著变更记录在此文件中，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- 首次使用环境引导、实验级readiness门禁和固定可复制修复建议。
- 可恢复Session、派生学习阶段、排障时间线及脱敏资源快照。
- 三层提示、公开验证三态、派生进度统计和脱敏Markdown复盘导出。
- Alembic `0002_guided_learning`迁移，兼容v0.1.0数据库原地升级。

### Changed

- 开始开发KubeLab 0.2.0a0的M5引导式排障学习闭环。
- 活动Session GET改为纯SQLite读取；资源、Events和Logs读取不再推进学习状态，集群协调改为显式写API。

### Security

- Web资源与evidence使用独立白名单DTO，完全排除Secret、labels、annotations、conditions、镜像信息和Kubernetes原始对象。
- 验证、Doctor、复盘导出和未知异常统一脱敏，不公开内部值、Manifest或堆栈。

## [0.1.0] - 2026-08-27

### Added

- 十二个声明式 Kubernetes 故障实验及一致的验证、提示、重置和清理流程。
- Typer CLI、FastAPI REST API 和中文本地 Web 运维控制台，共享同一 Application Service。
- 本机 minikube Context 身份信任、Namespace 所有权保护与受限 `kubelab workspace enter`。
- SQLAlchemy/Alembic 持久化、Session 状态机、复盘和跨进程操作锁。
- wheel 内置实验、Web 模板和静态资源。
- GitHub-only wheel/sdist发布流程、双平台CI、统一产物检查器和开源协作材料。

### Security

- Web 固定绑定 loopback，校验 Origin 与 CSRF，并设置 CSP 等安全响应头。
- 公共 DTO、日志和持久化结果不暴露 Secret、凭证、完整 Manifest 或验证内部值。
- 受限workspace显式授权`deployments/scale`子资源，使LAB-001扩容无需扩大Secret、RBAC或集群级权限。

[Unreleased]: https://github.com/CaoJun1015/Kubelab/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/CaoJun1015/Kubelab/releases/tag/v0.1.0
