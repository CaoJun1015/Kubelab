# KubeLab实验目录

每个实验是一个独立目录，包含一个`lab.yaml`、一个或多个声明式Kubernetes Manifest、练习说明和维护者标准修复。M1-09已经提供首批三个真实故障实验；公开CLI命令将在M1-10接入，因此当前仍不能直接执行`kubelab start`。

## 目录结构

```text
labs/
└── lab-007-service-selector/
    ├── README.md
    ├── lab.yaml
    ├── manifests/
    │   └── resources.yaml
    └── solutions/
        └── fix.yaml
```

Registry会递归查找文件名严格为`lab.yaml`的实验。Manifest路径以该`lab.yaml`所在目录为基准，并必须使用`manifests/deployment.yaml`这样的POSIX相对路径。

开发和测试时可以用绝对目录覆盖默认位置：

```bash
export KUBELAB_LABS_DIR="$HOME/my-kubelab-labs"
```

## 最小实验示例

```yaml
apiVersion: kubelab.io/v1alpha1
kind: Lab
metadata:
  id: service-selector
  name: Service无法访问Pod
  description: Pod正常运行，但Service没有可用后端。
  difficulty: beginner
  durationMinutes: 20
  category: networking
  tags: [service, endpoint]
requirements:
  kubernetes: ">=1.28"
  minimumCpu: 2
  minimumMemoryMiB: 2048
  addons: []
environment:
  namespace: kubelab-service-selector
  manifests:
    - manifests/deployment.yaml
    - manifests/service.yaml
  provisionTimeoutSeconds: 120
task:
  description: 定位Service没有Endpoint的原因并恢复服务。
  completionDescription: Service至少有一个可用Endpoint。
  successMessage: Service已经重新匹配Pod。
initialChecks:
  - id: endpoints-empty
    type: service_endpoint_count
    name: web
    exactly: 0
    timeoutSeconds: 30
    unmetMessage: 初始故障场景未正确建立。
successChecks:
  - id: endpoints-restored
    type: service_endpoint_count
    name: web
    minimum: 1
    timeoutSeconds: 30
    unmetMessage: Service仍然没有可用Endpoint。
hints:
  - level: 1
    content: 比较Service Selector和Pod Label。
cleanup:
  deleteNamespace: true
interview:
  questions:
    - Service如何发现后端Pod？
```

完整JSON Schema位于`schemas/lab-v1alpha1.schema.json`。修改Pydantic模型后，维护者必须重新生成Schema并运行同步测试：

```bash
uv run python -m kubelab.schema_export
uv run pytest tests/test_lab_schema.py
```

## v1alpha1验证器

- `resource_exists`
- `pod_status`
- `deployment_available`
- `service_endpoint_count`
- `container_image`
- `config_value`
- `pvc_status`
- `http_response`

这些验证器由M1-08 ValidationEngine执行。每个实验必须同时证明初始故障契约、修复后的成功契约和重置后的故障恢复。

## 首批实验

- `lab-005-image-pull-backoff`：确定不存在的镜像，验证`ErrImagePull/ImagePullBackOff`、正确镜像和Pod稳定Ready；
- `lab-006-crash-loop-backoff`：容器主动退出，验证`CrashLoopBackOff`、最小重启次数及修复后零重启稳定窗口；
- `lab-007-service-selector`：Service Selector故意不匹配，验证Endpoint从0恢复并通过集群内HTTP 200探测。

每个`solutions/fix.yaml`仅供契约测试和维护者核对，不在`lab.yaml.environment.manifests`中，因此Registry启动实验时不会读取或自动应用答案。

## Manifest约束

允许的资源：Pod、Service、ConfigMap、Secret、PersistentVolumeClaim、Deployment、StatefulSet、DaemonSet、Job、CronJob和Ingress。

Manifest可以省略`metadata.namespace`；如果显式填写，只能等于实验的`environment.namespace`。多文档YAML会逐个扫描。

以下内容会被拒绝：

- Namespace、Node、PersistentVolume、StorageClass、CRD、ClusterRole等集群级资源；
- 未列入白名单的API或Kind；
- 绝对路径、`..`、Windows盘符、UNC路径和逃逸实验目录的符号链接；
- privileged、hostNetwork、hostPID、hostIPC、hostPath、hostPort；
- allowPrivilegeEscalation、Unmasked procMount、Unconfined seccomp、HostProcess；
- 任何新增Linux capability；
- NodePort、LoadBalancer、ExternalName和externalIPs；
- 指向实验外资源的ownerReference、超过平台上限的资源申请和外部URL。

Schema不提供Shell、Python、setup、verify或自定义cleanup命令字段。任务描述中的文字只用于展示，平台不会把它当作命令执行。

## 安全边界

- Registry只读取本地文件，不调用Docker、kubectl、minikube或Kubernetes API；
- 加载错误只包含实验ID、相对文件路径和字段路径；
- 错误不得包含Secret值、Token、完整Manifest或YAML源码片段；
- 内部符号链接只有在最终目标仍位于当前实验目录且为普通文件时才允许；
- 静态扫描不能审计容器镜像内部代码，镜像来源治理和运行时网络隔离属于后续阶段。
