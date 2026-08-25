# KubeLab

KubeLab 是一个运行在 **Windows 11 + WSL2 Ubuntu** 中的本地 Kubernetes 运维练习平台。它以本机 Docker Engine 和 minikube 为实验环境，目标是把云原生运维面试知识转化为可以反复操作、验证和复盘的故障实验。

> 当前版本：`0.1.0a0`（M1-03）。项目仍处于早期开发阶段，目前提供环境诊断和 minikube Context 信任能力，实验启动、自动验证、Web 页面尚未实现。

## 当前可用功能

- `kubelab doctor`：检查 WSL2 Ubuntu、Python、Docker、kubectl、minikube、kubeconfig、节点、资源、StorageClass及可选组件；
- kubectl Client/API Server版本偏差检查，minor差值超过1时拒绝继续；
- `kubelab context inspect`：只读查看当前Context、minikube profile、脱敏API Server、CA指纹、`kube-system` UID和Server版本；
- `kubelab context trust`：仅信任经过验证的本地minikube身份；
- `kubelab context untrust`：只删除本地信任记录，不修改集群；
- `ContextTrustService.assert_trusted_context()`：为后续所有Kubernetes写操作提供统一安全守卫；
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

当前质量基线：81项测试，覆盖率91.88%，Ruff和strict mypy通过。

## 安全边界

- 当前阶段的集群操作全部只读；
- 仅允许显式信任可证明属于本机的minikube；
- API Server必须使用HTTPS，并且是回环地址或与`minikube ip`完全一致；
- Context、Server、CA、`kube-system` UID或profile漂移时，未来写操作会被拒绝；
- 不执行实验提供的任意宿主机命令；
- 不自动删除Namespace、移除finalizer或修改`kube-system`；
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
- [ ] M1-04 实验Schema和LabRegistry；
- [ ] M1-05 SQLite、状态机和操作锁；
- [ ] M1-06 KubernetesGateway；
- [ ] M1-07 ValidationEngine；
- [ ] M1-08 首批三个故障实验；
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

还不能。当前版本完成的是安全运行基线。实验Schema、资源创建、自动验证和清理能力将在后续M1阶段加入。
