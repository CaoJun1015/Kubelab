# KubeLab M6.1 发布候选验收契约

M6.1只加固KubeLab 0.3.0发布候选，不增加学习功能、实验内容、公共API或数据库Schema。候选版本必须在同一提交上完成无集群质量门、安装产物烟测和33个本机minikube场景验收。

## 安全边界

- 真实验收只允许WSL2 Ubuntu中的本机Docker驱动`minikube` profile。
- Context必须已经通过KubeLab身份信任；脚本不得自动信任身份漂移后的Context。
- 验收开始时记录profile状态；原状态为Stopped时，结束或中断后恢复Stopped。
- 不访问远程或生产集群，不修改`v0.1.0`标签或Release，不合并main。
- 用户数据库只允许通过SQLite备份API复制到临时目录后验证升级，禁止修改正式数据库。

## 四批场景

| 批次 | 场景 | 数量 |
|---|---|---:|
| `baseline-001-012` | LAB-001至012基线 | 12 |
| `baseline-013-021` | LAB-013至021基线 | 9 |
| `variants-013-015` | LAB-013至015的variant-b与variant-c | 6 |
| `variants-016-018` | LAB-016至018的variant-b与variant-c | 6 |

每批必须零失败、零错误、零跳过。批次结束后必须确认不存在KubeLab管理的Namespace、Workspace RBAC、Probe Pod、PVC、PV和临时Workspace目录。发现残留时停止后续批次并保留现场，不执行掩盖缺陷的强制清理。

## 候选完成条件

- Windows与独立WSL Python 3.11环境的pytest覆盖率不低于90%。
- 两端Ruff、Ruff format、strict mypy、JavaScript语法和`git diff --check`通过。
- 两端wheel/sdist构建及统一产物检查通过，产物包含21个实验族、12个变体、9个Web资源和0001至0003迁移。
- minikube停止时的隔离安装仍能完成版本、Doctor报告、包内Registry和loopback Web烟测。
- minikube运行时连续通过四批33场景，期间任何代码或实验改动都会使全部批次结果失效并要求从头执行。
- Draft PR的Windows与Ubuntu GitHub Actions全部通过；PR保持Draft且不合并。

原始JUnit、命令输出和临时数据库只保存在WSL临时目录。仓库只记录脱敏的版本、提交、环境、数量、耗时、覆盖率、产物摘要和残留审计结论。
