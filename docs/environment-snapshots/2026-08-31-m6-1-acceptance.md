# KubeLab M6.1 `0.3.0rc1`验收记录

日期：2026-08-31  
分支：`codex/m6-1-stabilization`  
受验源码提交：`39568db5b0b5c9927387ede7b5806713c99ea17b`  
候选版本：`0.3.0rc1`

## 结论

M6.1本地发布候选门禁通过。Windows与独立WSL质量门、wheel/sdist统一验证、minikube Stopped安装烟测和本机minikube四批33场景真实闭环均成功。验收没有访问远程或生产集群，没有修改`v0.1.0`标签或Release。

Draft PR的GitHub Windows/Ubuntu CI是进入后续发布决策前的剩余门禁；本记录不代表已经合并、打标签或发布。

## 脱敏环境

| 项目 | Windows | WSL正式运行环境 |
|---|---|---|
| 操作系统 | Windows 11 `10.0.22631.5624` | Ubuntu `22.04.5 LTS` |
| Python | `3.11.0` | `3.11.16` |
| Node.js | `22.23.0` | `22.16.0` |
| uv | `0.11.23` | `0.12.5` |
| Docker | 不用于真实集群 | `29.6.0` |
| minikube | 不运行 | `1.38.1`，Docker driver，本机profile |
| kubectl / Kubernetes | 不访问集群 | kubectl `1.35.1`，本机minikube Kubernetes `1.35.1` |

Context重新信任只发生在本机minikube停止再启动导致loopback API端口变化之后。每次操作前均确认CA、`kube-system` UID和profile未变化，并获得用户明确授权。本记录不保存API地址、CA摘要、UID、kubeconfig、Token或用户路径。

## 双平台质量门

| 门禁 | Windows | WSL |
|---|---:|---:|
| pytest | 544 passed / 37 skipped | 546 passed / 35 skipped |
| 覆盖率 | 91.75% | 91.88% |
| Ruff | 通过 | 通过 |
| Ruff format | 通过 | 通过 |
| strict mypy `src` | 29个源文件通过 | 29个源文件通过 |
| JavaScript语法 | 通过 | 通过 |
| `git diff --check` | 通过 | 通过 |
| wheel/sdist构建 | 通过 | 通过 |
| 统一产物检查 | 通过 | 通过 |

统一产物检查确认候选包为`0.3.0rc1`，包含21个实验族、12个固定变体、33个场景、9个Web资源和`0001`至`0003`迁移，并通过机器路径、凭证与禁止文件扫描。

## 四批真实场景

| 批次 | 范围 | 结果 | pytest耗时 |
|---|---|---:|---:|
| `baseline-001-012` | LAB-001至012基线 | 12/12 | 35分59秒 |
| `baseline-013-021` | LAB-013至021基线 | 9/9 | 38分28秒 |
| `variants-013-015` | LAB-013至015的variant-b/c | 6/6 | 25分29秒 |
| `variants-016-018` | LAB-016至018的variant-b/c | 6/6 | 24分45秒 |

四批均为零失败、零错误、零跳过。每批结束后审计KubeLab管理的Namespace、Workspace ServiceAccount/Role/RoleBinding、Probe Pod、PVC、PV和`/tmp/kubelab-workspace-*`，结果均为零残留。最终独立审计同样为零。

## 验收中发现并关闭的问题

- 预检已有明确业务失败时，不再被后续暂时不可检查覆盖为平台错误。
- DNS探针按固定`dns`容器读取终止状态；稳定Pod DNS同时要求Headless Service、目标Pod地址匹配和受限查询成功，原始输出与地址不进入公共DTO或数据库。
- Service端口列表键变化、DaemonSet嵌套亲和性删除等Server-Side Apply不能可靠表达的修复，改为删除归属明确的单一资源后声明式重建。
- 验收前新增全局残留门禁；发现异常残留时停止且不自动删除。一次中断残留在验证所有权后由用户明确授权删除，随后四批从头重跑。
- SQLite源库不再把瞬态`-shm`锁文件当作耐久内容变化，仍校验数据库与WAL签名。

## 安装、迁移与终态

- 最终wheel在minikube Stopped条件下隔离安装成功。
- Doctor返回`unhealthy`、退出码3时，烟测仍完成版本、21/12/33 Registry和`127.0.0.1:8765` Web校验；Context按设计记录为`skipped-not-ready`，Web进程正常停止。
- WSL用户数据库只通过SQLite备份API复制到临时目录。源修订为`0001_initial_persistence`，临时副本升级到`0003_lab_variants`，6张既有表计数保持、Session数为0、迁移备份存在，源库保持不变。
- 验收结束后本机minikube的host、kubelet和apiserver均为Stopped，`127.0.0.1:8765`未监听。
