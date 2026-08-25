# KubeLab WSL2环境快照：kubectl 1.35.1升级前

> 采集时间：2026-08-25T14:59:48Z
> Git基线：`cbe4671 feat(platform): adopt WSL2 Ubuntu runtime baseline`
> 快照范围：无凭证的版本、路径、校验和与Kubernetes资源状态

## WSL运行环境

- 发行版：Ubuntu 22.04.5 LTS（Jammy）
- 内核：`5.10.16.3-microsoft-standard-WSL2`
- Docker Client：29.6.0
- Docker Server：29.6.0
- minikube：1.38.1
- minikube Profile：`minikube`
- minikube Host/Kubelet/API Server：Running

## kubectl升级前状态

- PATH解析：`/usr/local/bin/kubectl`
- Client版本：1.31.0
- Kustomize版本：5.4.2
- SHA256：`7c27adc64a84d1c0cc3dcf7bf4b6e916cc00f3f576a2dbac51b318d926032437`
- Kubernetes Server版本：1.35.1
- 版本偏差：Client落后Server 4个minor，不符合官方支持的正负1个minor范围
- 回退能力：本轮用户级安装不会覆盖或删除`/usr/local/bin/kubectl`

## 集群状态

- 当前Context：`minikube`
- Node：`minikube`，Ready，control-plane，Kubernetes 1.35.1
- CoreDNS、etcd、API Server、Controller Manager、Scheduler和kube-proxy：Running
- `default` Namespace已有redis和todo-app工作负载，后续KubeLab不得修改
- `kube-system/storage-provisioner`：`ImagePullBackOff`

`storage-provisioner`异常不阻塞M1-03 Context Trust，但可能导致后续PVC实验失败，进入存储实验前必须单独诊断并恢复。

## 隐私和恢复说明

- 未保存kubeconfig内容、证书、Token、Secret、API Server地址或Pod日志。
- 如需回退kubectl解析顺序，只需移除或重命名`~/.local/bin/kubectl`；原1.31.0二进制仍保留在`/usr/local/bin/kubectl`。
