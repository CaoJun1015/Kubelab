# KubeLab

KubeLab 是一个运行在 **Windows 11 + WSL2 Ubuntu** 中的本地 Kubernetes 运维练习平台。它以本机 Docker Engine 和 minikube 为实验环境，目标是把云原生运维面试知识转化为可以反复操作、验证和复盘的故障实验。

> 当前版本：`0.1.0a0`（M1-07）。项目仍处于早期开发阶段，目前提供环境诊断、minikube Context信任、实验Schema、安全加载、持久化、状态机、安全Kubernetes网关和内部实验生命周期管理；公开CLI实验命令、真实自动验证、Web页面尚未实现。

## 当前可用功能

- `kubelab doctor`：检查 WSL2 Ubuntu、Python、Docker、kubectl、minikube、kubeconfig、节点、资源、StorageClass及可选组件；
- kubectl Client/API Server版本偏差检查，minor差值超过1时拒绝继续；
- `kubelab context inspect`：只读查看当前Context、minikube profile、脱敏API Server、CA指纹、`kube-system` UID和Server版本；
- `kubelab context trust`：仅信任经过验证的本地minikube身份；
- `kubelab context untrust`：只删除本地信任记录，不修改集群；
- `ContextTrustService.assert_trusted_context()`：为后续所有Kubernetes写操作提供统一安全守卫；
- `kubelab.io/v1alpha1`实验Schema：严格校验实验元数据、任务、检查、提示和声明式清理配置；
- `LabRegistry`：确定性扫描本地实验，隔离损坏实验并拒绝危险Manifest、路径逃逸和集群级资源；
- 可重复生成的`schemas/lab-v1alpha1.schema.json`及错误脱敏；
- SQLAlchemy 2与Alembic持久化：保存Session、状态事件、验证记录、提示和复盘；
- `SessionStateMachine`：拒绝非法生命周期转换，并由SQLite条件唯一索引保证最多一个活动实验；
- `SqlAlchemyUnitOfWork`和Repository：为后续CLI与Web提供统一事务边界；
- `OperationLock`：跨进程序列化未来的集群和数据库写操作；
- `KubernetesGateway`：只在Session作用域内创建受保护Namespace，执行server-side dry-run/apply，并提供脱敏资源、Pod、Events和受限Logs读取；
- Namespace删除前核对前缀、Session记录、管理标签、lab ID、Session ID和Context指纹；超时只报告finalizer和残留资源，不强制删除；
- `LabRegistry.materialize_for_gateway()`：Apply前重新读取、校验摘要并执行安全扫描，阻止扫描后替换文件；
- 受限curl探测Pod基础能力：固定镜像、资源限额和安全上下文，只允许访问当前实验Namespace的集群内Service DNS；
- `LabManager`：通过短数据库事务协调`start/status/reset/cleanup`，在每次写操作前重新验证Context，并在部分Apply或初始契约失败时安全回滚；
- 状态协调：外部删除环境时受控完成Session，Namespace身份不匹配时转为`error`且拒绝删除，reset保留Session ID并支持中断重试；
- JSON输出、稳定退出码、凭证脱敏和原子配置写入。

## 支持环境

正式支持的MVP环境：

- Windows 11；
- WSL2 Ubuntu 22.04或24.04；
- WSL Ubuntu内部运行的Docker Engine；
- Docker驱动的minikube；
- Python 3.11（由uv管理）；
- kubectl与Kubernetes API Server相差不超过1个minor。

Windows只负责编辑代码和打开未来的Web页面。`kubelab`、Docker、minikube、kubectl和kubeconfig必须位于同一个WSL Ubuntu环境中。

## 快速开始

如果WSL2、Docker、minikube和kubectl已经可用：

```bash
git clone https://github.com/CaoJun1015/Kubelab.git
cd Kubelab

curl -LsSf https://astral.sh/uv/0.12.5/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

uv python install 3.11
uv tool install --python 3.11 .

minikube start \
  --driver=docker \
  --cpus=2 \
  --memory=4096 \
  --kubernetes-version=v1.35.1

kubelab doctor
kubelab context inspect
kubelab context trust
kubelab context inspect
```

最后一次检查应显示：

```text
Trusted: yes
Trust state: trusted
```

从一台全新Windows电脑开始安装，请阅读[完整部署手册](docs/DEPLOYMENT.md)。

## 日常使用

打开Windows Terminal中的Ubuntu：

```bash
minikube status
kubelab doctor
kubelab context inspect
```

如果minikube没有运行：

```bash
minikube start
kubelab doctor
```

只有在确认自己主动重建了minikube，或者可信集群地址确实发生变化时，才重新执行：

```bash
kubelab context trust
```

不要对陌生、公司生产或远程Context执行信任命令。

### Doctor状态和退出码

| 状态 | 含义 | 退出码 |
|---|---|---:|
| `healthy` | 必需项和可选项全部正常 | 0 |
| `degraded` | 必需项正常，但Helm、Ingress或metrics-server等可选项缺失 | 0 |
| `unhealthy` | 必需环境不满足 | 3 |

查看适合脚本处理的结果：

```bash
kubelab doctor --json
kubelab context inspect --json
```

### Context命令

```bash
# 只读，不修改本地配置或集群
kubelab context inspect

# 验证当前环境是本地minikube后，保存无凭证身份指纹
kubelab context trust

# 仅删除当前Context的本地信任记录
kubelab context untrust
```

信任记录默认位于：

```text
~/.config/kubelab/config.toml
```

其中只保存Context名称、规范化API Server、CA SHA256、`kube-system` UID、minikube profile和信任时间，不保存Token、私钥、证书原文或Secret。

## 从源码开发

```bash
git clone https://github.com/CaoJun1015/Kubelab.git
cd Kubelab

uv python install 3.11
uv sync --frozen

uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

当前质量基线：Windows下收集304项测试，301项通过、3项跳过，覆盖率92.45%；WSL下收集304项测试，303项通过、1项默认关闭的真实集成测试跳过，覆盖率92.70%。M1-06另在已信任minikube中显式运行1项Namespace创建/安全清理集成测试并确认无残留。两端Ruff、格式检查和strict mypy均通过。

只有在WSL中确认`kubelab context inspect`显示`trusted`后，才可显式运行真实网关测试：

```bash
KUBELAB_RUN_INTEGRATION=1 uv run pytest --no-cov -q tests/test_kubernetes_gateway_integration.py
```

该测试只创建一个随机`kubelab-test-*` Namespace，并通过所有权校验清理；不要在远程或生产Context运行。

## 安全边界

- Schema和Registry扫描仍完全不访问集群；只有内部`KubernetesGateway`能够访问已信任Context，当前尚未提供面向用户的实验写命令；
- 数据库、备份和操作锁默认位于`${XDG_STATE_HOME:-~/.local/state}/kubelab/`，拒绝使用`/mnt/c`或`/mnt/d`作为正式状态目录；
- SQLite启用WAL、外键和5000ms busy timeout，迁移前在独占锁内创建安全备份；
- 仅允许显式信任可证明属于本机的minikube；
- API Server必须使用HTTPS，并且是回环地址或与`minikube ip`完全一致；
- Context、Server、CA、`kube-system` UID或profile漂移时，未来写操作会被拒绝；
- 不执行实验提供的任意宿主机命令；
- 只删除数据库Session作用域和全部所有权元数据完全匹配的`kubelab-*` Namespace；绝不移除finalizer或修改`kube-system`；
- 日志、JSON和配置不得保存Kubernetes凭证。

## 项目结构

```text
src/kubelab/              Python包和CLI
tests/                    单元测试
labs/                     后续实验定义
docs/                     部署与环境文档
PRD-KubeLab.md            产品需求基线
TDD-KubeLab.md            技术设计基线
cloud-native-ops-roadmap.html  云原生运维学习路线
```

## 开发路线

- [x] M1-01 Python工程基线和CLI；
- [x] M1-02 Environment Doctor；
- [x] M1-03 minikube Context信任；
- [x] M1-04 实验Schema和LabRegistry；
- [x] M1-05 SQLite、状态机和操作锁；
- [x] M1-06 KubernetesGateway；
- [x] M1-07 LabManager；
- [ ] M1-08 ValidationEngine；
- [ ] M1-09 首批三个故障实验；
- [ ] M1-10 CLI垂直切片验收；
- [ ] M2 本地Web界面。

详细设计见[PRD](PRD-KubeLab.md)和[TDD](TDD-KubeLab.md)。

## 常见问题

### 每次打开Ubuntu都要重新安装或信任吗？

不需要。uv工具环境、`kubelab`命令和Context信任都保存在WSL用户目录中。重启后通常只需要确认Docker和minikube是否运行。

### Doctor显示`degraded`能否继续？

可以，只要失败项中没有必需组件。Helm、Ingress和metrics-server当前是可选项。

### 为什么不支持PowerShell直接运行KubeLab？

因为KubeLab必须与Docker、minikube、kubectl和kubeconfig处于同一个Linux系统边界。PowerShell只适合进入WSL或管理Windows侧文件。

### 现在能开始故障实验吗？

还不能通过公开命令开始。当前版本已经具备内部的安全资源网关和生命周期管理，但仍需M1-08 ValidationEngine及M1-10 CLI把真实检查与用户命令接入。
