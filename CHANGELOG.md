# Changelog

本项目的显著变更记录在此文件中，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- 开始开发M7专题学习路径，以“概念、基线、固定变体、综合故障、专题复盘”组织现有实验。
- 开始开发固定故障变体、渐进式盲练和双根因综合排障场景。
- LAB-013至018各增加两个固定变体；LAB-019至021增加三个双根因高级实验，目录达到21个实验族、33个场景。
- 严格`LabVariant`契约、变体Manifest摘要复核、确定性轮换、盲练揭示和已完成故障地图。
- 受限`dns_resolution`验证器，只允许平台构造同Namespace稳定Service DNS探测。
- Alembic `0003_lab_variants`迁移，为旧Session安全回填`baseline`。
- 首次使用环境引导、实验级readiness门禁和固定可复制修复建议。
- 可恢复Session、派生学习阶段、排障时间线及脱敏资源快照。
- 三层提示、公开验证三态、派生进度统计和脱敏Markdown复盘导出。
- Alembic `0002_guided_learning`迁移，兼容v0.1.0数据库原地升级。
- LAB-013至018六个中级实验，覆盖Service TargetPort、ConfigMap键契约、Job、StatefulSet Headless Service、DaemonSet调度和PVC依赖。

### Changed

- 开始开发KubeLab 0.4.0a0；路径状态和复习建议继续从既有Session事实派生。
- 完成KubeLab 0.3.0rc1的M6.1双平台质量门、停止态安装烟测和四批33场景真实验收。
- 开始开发KubeLab 0.3.0a0的M6可复现故障变体与综合排障场景。
- 开始开发KubeLab 0.2.0a0的M5引导式排障学习闭环。
- 活动Session GET改为纯SQLite读取；资源、Events和Logs读取不再推进学习状态，集群协调改为显式写API。
- LAB-012和LAB-018把默认StorageClass纳入实验级readiness强制门禁；wheel、sdist和Web目录契约扩展为18个实验。
- LabRegistry、Application Service、CLI和Web统一消费“基线或已选变体”的有效实验对象；客户端不能指定变体。
- 稳定Pod DNS验证同时确认Headless Service语义、目标Pod地址与受限探针结果；Service端口键和DaemonSet亲和性删除场景使用安全重建流程。
- 增加WSL与Windows一键启动脚本：只允许本机Docker驱动minikube，幂等管理loopback Web进程，且不自动修改Context信任。

### Security

- Web资源与evidence使用独立白名单DTO，完全排除Secret、labels、annotations、conditions、镜像信息和Kubernetes原始对象。
- 验证、Doctor、复盘导出和未知异常统一脱敏，不公开内部值、Manifest或堆栈。
- 盲练通过前不公开变体ID、名称、根因、标准修复或内部验证结构；活动变体缺失时禁止静默回退到基线。

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
