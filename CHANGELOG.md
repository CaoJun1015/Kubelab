# Changelog

本项目的显著变更记录在此文件中，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
