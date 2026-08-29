# KubeLab从零部署手册

本手册面向第一次配置KubeLab的Windows用户，最终得到以下运行形态：

```text
Windows 11
└── WSL2 Ubuntu
    ├── Docker Engine
    ├── minikube（Docker driver）
    ├── kubectl
    ├── uv + Python 3.11
    └── KubeLab CLI
```

Windows只负责运行Windows Terminal、编辑代码和访问未来的Web页面。所有Kubernetes工具和KubeLab进程都在Ubuntu终端中运行。

## 1. 准备条件

- Windows 11，已开启CPU虚拟化；
- 建议至少4核CPU、8GB内存和20GB可用磁盘；
- 可以访问Microsoft、Docker、Kubernetes、GitHub和Python软件源；
- Windows用户拥有管理员权限；
- 以下Linux命令默认在WSL Ubuntu中执行，明确标记为PowerShell的命令除外。

本项目当前验证组合：

| 组件 | 已验证版本 |
|---|---|
| Ubuntu | 22.04.5 LTS |
| Python | 3.11.16 |
| uv | 0.12.5 |
| Docker Engine | 29.6.0 |
| minikube | 1.38.1 |
| kubectl | 1.35.1 |
| Kubernetes Server | 1.35.1 |

其他较新版本可能可用，但应以`kubelab doctor`和项目测试结果为准。

KubeLab 0.2开发版还会在启动每个实验前重新检查该实验声明的Kubernetes版本、CPU、内存和Addon要求。环境页面的缓存结果用于说明问题，不能绕过Application Service中的强制门禁。

## 2. 安装WSL2 Ubuntu

以管理员身份打开PowerShell：

```powershell
wsl --install -d Ubuntu
```

完成后重启Windows，首次打开Ubuntu并设置Linux用户名和密码。

在PowerShell确认Ubuntu使用WSL2：

```powershell
wsl --list --verbose
```

期望Ubuntu对应的`VERSION`为`2`。如果不是：

```powershell
wsl --set-version Ubuntu 2
```

## 3. 确认systemd

在Ubuntu中执行：

```bash
ps -p 1 -o comm=
```

输出为`systemd`即可继续。如果不是，编辑配置：

```bash
sudo nano /etc/wsl.conf
```

写入：

```ini
[boot]
systemd=true
```

保存后，在PowerShell执行：

```powershell
wsl --shutdown
```

重新打开Ubuntu，再次确认PID 1为`systemd`。

## 4. 安装基础工具

```bash
sudo apt update
sudo apt install -y ca-certificates curl git

git --version
curl --version
```

可选：设置Git身份，仅在需要提交代码时执行：

```bash
git config --global user.name "你的名字"
git config --global user.email "你的邮箱"
```

## 5. 安装Docker Engine

以下方式使用Docker官方APT仓库。

### 5.1 添加官方仓库

```bash
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

```bash
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
```

### 5.2 安装并启动

```bash
sudo apt update
sudo apt install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

sudo systemctl enable --now docker
```

把当前用户加入docker组：

```bash
sudo usermod -aG docker "$USER"
```

关闭当前Ubuntu窗口并重新打开，或者临时执行：

```bash
newgrp docker
```

验证：

```bash
docker version
docker run --rm hello-world
```

> docker组拥有接近root的权限，只应加入可信的本机用户。

## 6. 安装minikube

以下命令适用于常见的amd64 WSL环境：

```bash
dpkg --print-architecture
```

确认输出为`amd64`后执行：

```bash
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
sudo install minikube-linux-amd64 /usr/local/bin/minikube
rm minikube-linux-amd64

minikube version
```

如果输出为`arm64`，请在minikube官方安装页选择对应架构，不要安装amd64二进制。

## 7. 安装匹配版本的kubectl

Kubernetes官方支持kubectl与API Server相差不超过1个minor。KubeLab会把超过此范围视为失败。

下面安装当前项目已验证的`v1.35.1`，校验SHA256，并只写入用户目录，不覆盖系统kubectl：

```bash
mkdir -p "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"

KUBECTL_VERSION="v1.35.1"
curl -LO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
curl -LO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256"

echo "$(cat kubectl.sha256)  kubectl" | sha256sum --check
install -m 0755 kubectl "$HOME/.local/bin/kubectl"
rm kubectl kubectl.sha256
```

确保新终端也能找到`~/.local/bin`：

```bash
grep -qxF 'export PATH="$HOME/.local/bin:$PATH"' "$HOME/.profile" || \
  printf '%s\n' 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.profile"

source "$HOME/.profile"
hash -r
```

验证：

```bash
command -v kubectl
kubectl version --client
```

期望路径为`/home/<你的用户名>/.local/bin/kubectl`。

## 8. 安装uv和Python 3.11

安装项目验证过的uv版本：

```bash
curl -LsSf https://astral.sh/uv/0.12.5/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

uv --version
uv python install 3.11
```

`uv`会把KubeLab安装到隔离的工具环境，不污染Ubuntu系统Python。

## 9. 下载并安装KubeLab

KubeLab只通过GitHub Release发布，不上传PyPI。下载wheel和校验和：

```bash
mkdir -p "$HOME/downloads/kubelab"
cd "$HOME/downloads/kubelab"

curl -LO https://github.com/CaoJun1015/Kubelab/releases/download/v0.1.0/kubelab-0.1.0-py3-none-any.whl
curl -LO https://github.com/CaoJun1015/Kubelab/releases/download/v0.1.0/SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
```

安装CLI：

```bash
uv tool install --python 3.11 ./kubelab-0.1.0-py3-none-any.whl
uv tool update-shell
hash -r

kubelab --version
kubelab --help
```

如果当前终端仍找不到`kubelab`，关闭Ubuntu窗口后重新打开。

## 10. 创建minikube集群

使用项目验证过的Kubernetes版本：

```bash
minikube start \
  --driver=docker \
  --cpus=2 \
  --memory=4096 \
  --kubernetes-version=v1.35.1
```

验证：

```bash
minikube status
kubectl config current-context
kubectl version
kubectl get nodes -o wide
kubectl get pods -A
```

期望：

- 当前Context为`minikube`；
- Node状态为`Ready`；
- kubectl Client和Server均为1.35.x，或minor差值不超过1；
- CoreDNS和控制面组件处于Running。

## 11. 执行KubeLab首次检查

```bash
kubelab doctor
echo $?
```

结果说明：

- `healthy`：全部检查正常；
- `degraded`且退出码为0：必需项正常，仅缺少可选组件，可以继续；
- `unhealthy`且退出码为3：先按输出中的`Fix`处理失败项。

查看完整JSON：

```bash
kubelab doctor --json
```

## 12. 信任本地minikube

先只读检查：

```bash
kubelab context inspect
```

确认以下信息符合预期：

- Context和minikube profile均为`minikube`；
- API Server是回环地址，或属于当前`minikube ip`；
- Kubernetes Server版本正确；
- 当前没有指向公司、云平台或生产集群。

显式信任：

```bash
kubelab context trust
kubelab context inspect
```

期望最后显示：

```text
Trusted: yes
Trust state: trusted
```

信任记录位于：

```text
~/.config/kubelab/config.toml
```

检查权限和敏感字段：

```bash
stat -c '%a %n' "$HOME/.config/kubelab/config.toml"
```

权限应为`600`。配置只保存身份指纹，不保存kubeconfig Token、私钥或证书原文。

## 13. 日常启动

每次打开Ubuntu后不需要重新安装KubeLab，也不需要重复信任。

```bash
docker info >/dev/null
minikube status
kubelab doctor
kubelab context inspect
```

如果minikube已停止：

```bash
minikube start
kubelab doctor
```

只有明确重建了本地集群，导致CA、UID或API Server变化时，才重新执行`kubelab context trust`。

### 13.1 启动本地Web界面与REST API

API进程必须在WSL2 Ubuntu内启动：

```bash
kubelab serve
```

服务固定监听`127.0.0.1:8765`，不接受host或port覆盖。Windows浏览器或本地客户端可通过WSL localhost转发访问：

```text
http://127.0.0.1:8765/
```

首次打开后进入“环境”页面，点击“重新检查”。页面GET只读取SQLite中的上次结果；按钮会运行KubeLab内置的固定只读诊断。页面展示的修复命令必须复制到WSL终端后由用户确认执行，Web不会自动运行任何系统命令。环境为`blocked`时实验启动按钮被禁用，服务端`LabManager.start()`也会在创建Session前再次阻断。

浏览器或服务重启后，活动Session从本地数据库恢复且不会自动访问集群。需要核对Namespace时点击“协调集群状态”；该动作是受Origin和CSRF保护的显式POST。中断在provisioning、resetting或cleaning的Session不会自动重放集群操作。

页面入口包括总览`/`、实验目录`/labs`和学习进度`/progress`。排障工作台会每2秒读取一次活动实验资源；切换到其他浏览器标签页时自动暂停，Events和Logs只在用户点击时读取。工作台可以复制活动Namespace，并根据这个受控值生成常用调查命令；所有文本都通过DOM文本节点渲染。

API不配置CORS；跨站Origin会被拒绝。所有`POST`和`PUT`请求都必须带精确的`Origin: http://127.0.0.1:8765`，并提交安全读取请求签发的HttpOnly、SameSite=Strict CSRF Cookie及同值`X-CSRF-Token`请求头。`reset`和`cleanup`还必须提交活动实验的精确Namespace。页面已经自动处理这些安全流程，直接调用API的客户端必须自行实现。

停止服务时在运行终端按`Ctrl+C`。不要用反向代理把该端口暴露到局域网或公网，也不要改为监听`0.0.0.0`。Web界面不提供Shell，Kubernetes调查和修复操作必须在下节的受限WSL工作区中完成。

## 14. 运行第一个故障实验

```bash
kubelab list
kubelab show lab-005-image-pull-backoff
kubelab start lab-005-image-pull-backoff
```

启动成功后，KubeLab会显示实验Namespace。在同一个WSL Ubuntu中进入受限工作区：

```bash
kubelab workspace enter
```

该命令只使用固定`/bin/bash --noprofile --norc -i`，不会接受任意shell、命令、路径或URL。KubeLab为活动Session创建固定名称的ServiceAccount、Role和RoleBinding，签发一小时以内的短期令牌，并生成权限为0600的临时kubeconfig。shell退出后在`finally`中撤销工作区资源并删除临时文件。

工作区默认Namespace已经固定，可直接调查和修复：

```bash
kubectl get all
kubectl describe pods
kubectl get events --sort-by=.lastTimestamp
kubectl get endpointslice
```

允许的权限只覆盖活动Namespace内排障所需的Pods、Logs、Events、ConfigMaps、Services、EndpointSlices、PVC和常见工作负载；不允许读取Secret、修改RBAC或访问集群级Namespace。可以用下面的命令自行验证边界：

```bash
kubectl auth can-i get pods          # yes
kubectl auth can-i patch deployments # yes
kubectl auth can-i get secrets       # no
kubectl auth can-i get namespaces    # no
```

修复完成后输入`exit`，回到Web点击验证，或在普通KubeLab CLI中验证并清理：

```bash
kubelab verify
kubelab retrospective edit
kubelab cleanup
```

卡住时运行`kubelab hint`，每次只会解锁下一层提示。`reset`和`cleanup`都会显示目标Namespace并要求确认。日常短命令默认选择唯一活动实验，不需要手工保存Session ID。

当前目录包含18个实验。运行LAB-011前，必须确认`kubelab doctor`中的Ingress addon为可用；LAB-012和LAB-018会把默认StorageClass声明为实验级必需项，检查失败时在创建Session或访问集群前阻止启动，并展示固定修复命令。PVC的StorageClass字段不可原地修改，应按实验提示删除故障PVC后使用可用StorageClass重新创建。

### 14.1 固定版本实验镜像缓存

真实实验不应依赖运行时公网拉取。先在WSL Docker中拉取并加载四个固定镜像：

```bash
docker pull nginx:1.26-alpine
docker pull nginx:1.27-alpine
docker pull busybox:1.36.1
docker pull curlimages/curl:8.12.1

minikube image load nginx:1.26-alpine
minikube image load nginx:1.27-alpine
minikube image load busybox:1.36.1
minikube image load curlimages/curl:8.12.1
minikube image ls
```

如果所在网络需要镜像代理，应在Docker daemon层配置可信镜像源，拉取后仍保留上述标准镜像名；不要把代理凭证或私有仓库Token写入KubeLab配置、实验Manifest或文档。

### 14.2 Ingress和PVC前置修复

一般环境先使用官方addon入口：

```bash
minikube addons enable ingress
minikube addons enable default-storageclass
minikube addons enable storage-provisioner

kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout=120s
kubectl wait --for=condition=Ready pod/storage-provisioner -n kube-system --timeout=120s
kubectl get storageclass standard
```

若profile设置了全局镜像仓库，minikube可能把`k8s-minikube/storage-provisioner:v5`错误拼成仓库中不存在的嵌套路径。确认同仓库的扁平`storage-provisioner:v5`已经缓存后，可通过addon镜像槽位修复：

```bash
minikube addons disable storage-provisioner
minikube addons enable storage-provisioner \
  --images=StorageProvisioner=storage-provisioner:v5
kubectl wait --for=condition=Ready pod/storage-provisioner -n kube-system --timeout=120s
```

Ingress遇到同类路径改写问题时，先用`minikube addons images ingress`查看当前版本和槽位，再为`IngressController`、`KubeWebhookCertgenCreate`、`KubeWebhookCertgenPatch`提供已经缓存且与默认版本一致的相对镜像名。不要凭猜测混用控制器或证书生成器版本。

### 14.3 十八实验真实契约入口（仅本机受信任minikube）

只有`kubelab context inspect`显示`trusted`，且当前Context明确为本机minikube时才运行：

```bash
KUBELAB_RUN_LAB_INTEGRATION=1 \
  uv run pytest --no-cov -q tests/test_first_labs_integration.py
```

测试定义会对全部18个实验执行`start → 受限workspace修复 → verify → reset → cleanup`，并验证工作区不能读取Secret或集群级Namespace。LAB-001至012已有真实验收记录；LAB-013至018在维护者明确运行并通过前只具备Fake Gateway契约，不声明真实集群验收完成。测试默认关闭；禁止在远程或生产集群设置该变量。

## 15. 升级KubeLab

从GitHub Release下载目标版本wheel并校验`SHA256SUMS`后执行：

```bash
uv tool install --force --python 3.11 ./kubelab-0.1.0-py3-none-any.whl

kubelab --version
kubelab doctor
```

## 16. 开发者安装

需要运行测试或修改源码时：

```bash
mkdir -p "$HOME/projects"
cd "$HOME/projects"
git clone https://github.com/CaoJun1015/Kubelab.git
cd Kubelab
uv python install 3.11
uv sync --frozen

uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

不要在Windows和WSL之间复用`.venv`。

## 17. 故障排查

### 找不到kubelab、uv或kubectl

```bash
export PATH="$HOME/.local/bin:$PATH"
source "$HOME/.profile"
hash -r

command -v uv
command -v kubectl
command -v kubelab
```

### Docker daemon不可用

```bash
sudo systemctl start docker
systemctl status docker --no-pager
docker info
```

如果出现权限错误，确认当前用户属于docker组，然后重新打开Ubuntu：

```bash
groups
```

### minikube停止或API不可达

```bash
minikube status
minikube start
kubectl get nodes
```

### kubectl版本偏差失败

```bash
kubectl version
kubelab doctor
```

重新按第7节安装与Server同minor，或前后相差不超过1个minor的kubectl。不要覆盖系统路径中的旧版本，优先安装到`~/.local/bin`。

### Context显示drifted

```bash
kubectl config current-context
minikube status
kubelab context inspect
```

只有在确认变化来自自己主动执行的`minikube delete`、重建或配置迁移后，才能重新运行：

```bash
kubelab context trust
```

如果Context指向陌生地址或远程集群，不要信任。

### Pod出现ImagePullBackOff

先只读检查，不要直接删除资源或finalizer：

```bash
kubectl get pods -A
kubectl describe pod -n kube-system storage-provisioner
minikube logs
```

常见原因包括镜像仓库不可达、代理配置或镜像地址错误。修复网络或镜像源后再重试；进入PVC实验前，`storage-provisioner`必须恢复正常。

## 18. 卸载

仅撤销KubeLab信任并卸载CLI：

```bash
kubelab context untrust
uv tool uninstall kubelab
```

这不会删除minikube或任何Kubernetes资源。

如果明确不再需要整个本地实验集群，下面的命令会永久删除minikube集群及其中数据，请先确认没有需要保留的内容：

```bash
minikube delete
```

## 19. 验收清单

部署完成后逐项确认：

- [ ] `wsl --list --verbose`显示Ubuntu为WSL2；
- [ ] `docker version`同时显示Client和Server；
- [ ] `minikube status`的Host、Kubelet和APIServer为Running；
- [ ] `command -v kubectl`优先指向`~/.local/bin/kubectl`；
- [ ] `kubectl get nodes`显示Ready；
- [ ] `kubelab --version`成功；
- [ ] `kubelab doctor`不是unhealthy；
- [ ] `kubelab context inspect`显示本地minikube；
- [ ] `Trust state`为trusted；
- [ ] 配置文件权限为600；
- [ ] 配置文件不含Token、私钥或证书原文。

## 20. 官方参考

- [Microsoft：安装WSL](https://learn.microsoft.com/windows/wsl/install)
- [Microsoft：WSL启用systemd](https://learn.microsoft.com/windows/wsl/systemd)
- [Docker：Ubuntu安装Docker Engine](https://docs.docker.com/engine/install/ubuntu/)
- [minikube：安装和启动](https://minikube.sigs.k8s.io/docs/start/)
- [Kubernetes：Linux安装kubectl](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/)
- [Kubernetes：版本偏差策略](https://kubernetes.io/releases/version-skew-policy/)
- [Astral：安装uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Astral：uv工具安装](https://docs.astral.sh/uv/guides/tools/)
