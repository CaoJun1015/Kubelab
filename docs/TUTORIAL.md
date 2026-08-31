# KubeLab 21个实验操作教程

本教程解释KubeLab 21个基线实验中的核心对象、故障价值和操作方法。每一节都按“是什么、为什么、怎么做”组织，适合完成一次自主排障后复习，也可以在卡住时作为分步教材。

> 教程包含基线场景的根因和修复方法。为了保留学习效果，建议先在Web中观察现象、使用三层提示并独立完成一次，再阅读对应章节。LAB-013至018的`variant-b`和`variant-c`属于盲练场景，本教程不会提前公开它们的答案。

## 1. 使用方法

### 1.1 准备环境

在WSL2 Ubuntu中启动本机环境：

```bash
cd /path/to/KubeLab
minikube start --profile minikube
uv run kubelab doctor
uv run kubelab context inspect
uv run kubelab serve
```

只有确认当前对象是自己的本机Docker驱动`minikube`后，才可在需要时执行`uv run kubelab context trust`。不要信任远程、公司或生产Context。

### 1.2 标准练习闭环

1. 在Web实验目录中启动实验，或执行`uv run kubelab start <lab-id>`。
2. 新开一个WSL终端，进入源码目录并执行`uv run kubelab workspace enter`。
3. 在受限Workspace中运行本教程的`kubectl`命令。Workspace已经固定Namespace，因此命令不再重复传入`-n`。
4. 每次修改后重新执行观察命令，确认状态变化，而不是一次修改多个不相关字段。
5. 输入`exit`离开Workspace，回到Web点击“验证”，或在普通WSL终端执行`uv run kubelab verify`。
6. 写复盘并清理实验。需要再次练习时使用reset，不要手动删除Namespace。

### 1.3 通用排障顺序

```text
控制器期望状态
    ↓
Pod调度与容器状态
    ↓
Events和前一次容器日志
    ↓
配置、端口、探针、存储等依赖
    ↓
Endpoint、DNS和HTTP业务验证
```

`Running`不等于`Ready`，`Ready`不等于Service可访问，Service有Endpoint也不等于应用端口正确。教程会在不同实验中逐层建立这些判断。

## 2. 基础工作负载与配置

### LAB-001 Deployment扩缩容

- 难度：入门；预计15分钟；Namespace：`kubelab-deployment-scaling`。

#### 是什么

Deployment维护期望副本数，并通过ReplicaSet创建Pod。扩容不是直接复制Pod，而是修改Deployment的声明式期望状态。

#### 为什么

生产流量增长、容量预留和故障冗余都依赖正确扩缩容。只看到`spec.replicas=3`还不够，必须确认三个副本都已Available和稳定Ready。

#### 怎么做

先观察期望、当前和可用副本：

```bash
kubectl get deployment web
kubectl get replicaset
kubectl get pods -l app=deployment-scaling-lab -o wide
```

把Deployment扩容到三个副本并观察Rollout：

```bash
kubectl scale deployment/web --replicas=3
kubectl rollout status deployment/web
kubectl get deployment web
kubectl get pods -l app=deployment-scaling-lab
```

完成标准是Deployment有三个可用副本，且三个Pod都持续Running和Ready。

### LAB-002 滚动更新与回滚

- 难度：入门；预计20分钟；Namespace：`kubelab-rolling-update`。

#### 是什么

RollingUpdate用新的ReplicaSet逐步替换旧Pod。`maxSurge`控制额外创建数量，`maxUnavailable`控制更新期间允许不可用的数量。

#### 为什么

镜像发布不能只修改标签，还要观察新旧ReplicaSet切换、Deployment条件和业务可用性。掌握rollout历史也能在坏版本出现时快速回退。

#### 怎么做

```bash
kubectl get deployment,replicaset,pod
kubectl describe deployment web
kubectl rollout history deployment/web
kubectl get deployment web -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

更新镜像并等待两个副本完成滚动：

```bash
kubectl set image deployment/web web=nginx:1.27-alpine
kubectl rollout status deployment/web
kubectl get replicaset
kubectl get pods -l app=rolling-update-lab
```

如果真实发布失败，常用回退入口是`kubectl rollout undo deployment/web`；本实验目标是完成正确更新，不需要回退。完成标准是镜像精确为`nginx:1.27-alpine`且两个副本Available。

### LAB-003 ConfigMap注入

- 难度：入门；预计20分钟；Namespace：`kubelab-configmap-injection`。

#### 是什么

ConfigMap把非敏感配置与镜像分离。通过环境变量注入的值只在容器创建时读取，修改ConfigMap不会自动改变已有容器环境。

#### 为什么

常见事故是“配置已经改了，应用仍使用旧值”。根因往往不是ConfigMap更新失败，而是工作负载没有滚动生成新Pod。

#### 怎么做

```bash
kubectl get configmap app-settings -o yaml
kubectl get deployment worker -o yaml
kubectl logs deployment/worker
```

更新配置，并显式触发Deployment滚动：

```bash
kubectl patch configmap app-settings --type=merge \
  -p '{"data":{"APP_MODE":"production"}}'
kubectl rollout restart deployment/worker
kubectl rollout status deployment/worker
kubectl logs deployment/worker
```

完成标准是ConfigMap中的`APP_MODE`符合目标值，且新Pod已经稳定Ready并加载新配置。

### LAB-004 探针基础

- 难度：入门；预计20分钟；Namespace：`kubelab-probes`。

#### 是什么

Liveness判断容器是否需要重启；Readiness判断Pod是否可以接收流量。错误的Liveness路径会杀死健康进程，而错误的Readiness只会把Pod移出Endpoint。

#### 为什么

探针配置错误会把应用小故障放大成重启风暴。排障时必须区分“进程活着”和“业务可以接流量”两个问题。

#### 怎么做

```bash
kubectl get pods -l app=probes-lab
kubectl describe pods -l app=probes-lab
kubectl get events --sort-by=.lastTimestamp
kubectl get deployment web -o yaml
```

初始Liveness访问不存在的路径。将Liveness与Readiness都指向nginx实际可用的`/`：

```bash
kubectl patch deployment web --type=strategic -p \
'{"spec":{"template":{"spec":{"containers":[{"name":"web","livenessProbe":{"httpGet":{"path":"/","port":"http"},"initialDelaySeconds":2,"periodSeconds":2,"failureThreshold":1},"readinessProbe":{"httpGet":{"path":"/","port":"http"},"initialDelaySeconds":1,"periodSeconds":2}}]}}}}'
kubectl rollout status deployment/web
kubectl get pods -l app=probes-lab
```

完成标准是新Pod稳定Ready且重启次数为零。

## 3. 容器启动与服务发现

### LAB-005 ImagePullBackOff

- 难度：入门；预计20分钟；Namespace：`kubelab-image-pull-backoff`。

#### 是什么

`ErrImagePull`表示本次镜像拉取失败，`ImagePullBackOff`表示Kubelet进入指数退避。常见原因包括镜像不存在、认证失败、网络错误和Registry限流。

#### 为什么

Pod处于Pending时不能只看Phase。容器waiting reason和Events才能说明是调度、卷、配置还是镜像问题。

#### 怎么做

```bash
kubectl get deployment,pod
kubectl describe pods -l app=image-pull-lab
kubectl get events --sort-by=.lastTimestamp
kubectl get deployment web -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

把不存在的镜像改成固定可用版本：

```bash
kubectl set image deployment/web web=nginx:1.27-alpine
kubectl rollout status deployment/web
kubectl get pods -l app=image-pull-lab
```

完成标准是镜像字段正确，且新Pod持续Running和Ready。

### LAB-006 CrashLoopBackOff

- 难度：入门；预计25分钟；Namespace：`kubelab-crash-loop-backoff`。

#### 是什么

`CrashLoopBackOff`不是根因，而是容器反复退出后Kubelet的重启退避状态。真正原因通常在退出码、Last State、当前日志或previous日志中。

#### 为什么

Pod Phase可能仍显示Running，但其中的业务容器正在反复崩溃。只看`kubectl get pods`的一列状态容易误判。

#### 怎么做

```bash
kubectl get pods -l app=crash-loop-lab
kubectl describe pods -l app=crash-loop-lab
kubectl logs deployment/worker
kubectl logs deployment/worker --previous
kubectl get deployment worker -o yaml
```

保留BusyBox镜像，把主动退出的命令改为持续运行：

```bash
kubectl patch deployment worker --type=strategic -p \
'{"spec":{"template":{"spec":{"containers":[{"name":"worker","command":["sh","-c","while true; do sleep 3600; done"]}]}}}}'
kubectl rollout status deployment/worker
kubectl get pods -l app=crash-loop-lab
```

完成标准是新Pod稳定至少10秒、容器重启次数为零且Deployment有可用副本。

### LAB-007 Service Selector错误

- 难度：入门；预计25分钟；Namespace：`kubelab-service-selector`。

#### 是什么

Service通过Selector匹配Pod Label，并由EndpointSlice记录可接收流量的Ready地址。Service存在并不代表它拥有后端。

#### 为什么

“Pod正常但服务不可访问”经常来自标签契约断裂。排障必须沿Deployment→Pod Label→Service Selector→EndpointSlice→HTTP逐层检查。

#### 怎么做

```bash
kubectl get deployment,pod,service
kubectl get pods --show-labels
kubectl describe service web
kubectl get endpointslice -l kubernetes.io/service-name=web
```

保留Pod标签，只修复Service Selector：

```bash
kubectl patch service web --type=merge \
  -p '{"spec":{"selector":{"app":"service-selector-lab"}}}'
kubectl get endpointslice -l kubernetes.io/service-name=web -w
```

看到Ready Endpoint后退出`-w`。最终由KubeLab固定curl探针验证Service返回HTTP 200。

### LAB-008 ConfigMap缺失

- 难度：入门；预计20分钟；Namespace：`kubelab-configmap-missing`。

#### 是什么

Deployment通过`envFrom`引用不存在的ConfigMap时，Pod可以被调度，但Kubelet无法构造容器配置，通常显示`CreateContainerConfigError`。

#### 为什么

这类问题与镜像、应用进程无关。Events中的“对象不存在”比反复重启或重建Deployment更有诊断价值。

#### 怎么做

```bash
kubectl get deployment,pod,configmap
kubectl describe pods -l app=configmap-missing-lab
kubectl get events --sort-by=.lastTimestamp
kubectl get deployment worker -o yaml
```

创建Deployment所引用的对象：

```bash
kubectl create configmap app-settings --from-literal=APP_MODE=production
kubectl get pods -l app=configmap-missing-lab -w
```

完成标准是ConfigMap存在，Pod稳定Running和Ready；公开验证不会返回配置实际值。

### LAB-009 Readiness路径错误

- 难度：入门；预计25分钟；Namespace：`kubelab-readiness-path`。

#### 是什么

Readiness失败不会重启容器，但会把Pod标记为NotReady并从Service Endpoint中移除。因此Pod可能Running，业务流量仍然完全不可达。

#### 为什么

这是理解“进程状态”和“流量状态”差异的关键实验。只修复Pod外观而不检查Endpoint和HTTP，不足以证明服务恢复。

#### 怎么做

```bash
kubectl get pod,service
kubectl describe pods -l app=readiness-path-lab
kubectl get endpointslice -l kubernetes.io/service-name=web
kubectl get deployment web -o yaml
```

把Readiness路径改为nginx可用的`/`：

```bash
kubectl patch deployment web --type=json -p \
'[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/"}]'
kubectl rollout status deployment/web
kubectl get endpointslice -l kubernetes.io/service-name=web
```

完成标准是Pod Ready、Endpoint恢复且固定集群内探针得到HTTP 200。

### LAB-010 OOMKilled

- 难度：入门；预计25分钟；Namespace：`kubelab-oom-killed`。

#### 是什么

容器超过memory limit时会被内核终止，Last State通常记录`OOMKilled`。如果应用继续无界分配，Pod会进入重启循环。

#### 为什么

仅提高内存上限可能掩盖内存泄漏，仅修改程序但不给合理边界也缺少资源治理。需要同时理解工作负载行为和requests/limits。

#### 怎么做

```bash
kubectl get pods -l app=oom-killed-lab
kubectl describe pods -l app=oom-killed-lab
kubectl get pod -l app=oom-killed-lab \
  -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.reason}{"\n"}'
kubectl get deployment worker -o yaml
```

移除无界内存分配，并设置合理资源边界：

```bash
kubectl patch deployment worker --type=strategic -p \
'{"spec":{"template":{"spec":{"containers":[{"name":"worker","command":["sh","-c","while true; do echo worker-ok; sleep 30; done"],"resources":{"requests":{"cpu":"25m","memory":"32Mi"},"limits":{"cpu":"200m","memory":"128Mi"}}}]}}}}'
kubectl rollout status deployment/worker
kubectl get pods -l app=oom-killed-lab
```

完成标准是新Pod零重启并稳定Ready。

## 4. 入口、存储与中级控制器

### LAB-011 Ingress后端端口错误

- 难度：入门；预计25分钟；Namespace：`kubelab-ingress-backend-port`；要求Ingress addon。

#### 是什么

Ingress把Host和Path路由到Service端口。Deployment、Service和Endpoint都正常时，Ingress引用不存在的Service端口仍会阻断入口流量。

#### 为什么

入口故障必须区分Ingress规则、Service端口、Endpoint和Pod端口。直接修改Pod不能修复错误的Ingress后端契约。

#### 怎么做

```bash
kubectl get ingress,service,pod
kubectl describe ingress web
kubectl get service web -o yaml
kubectl get endpointslice -l kubernetes.io/service-name=web
```

把Ingress后端端口从错误值修正为Service实际暴露的80：

```bash
kubectl patch ingress web --type=json -p \
'[{"op":"replace","path":"/spec/rules/0/http/paths/0/backend/service/port/number","value":80}]'
kubectl describe ingress web
```

KubeLab使用固定的集群内Ingress Controller Service和`web.kubelab.local` Host验证HTTP，不接受任意外部URL。

### LAB-012 PVC无法绑定

- 难度：入门；预计25分钟；Namespace：`kubelab-pvc-pending`；要求默认StorageClass。

#### 是什么

PVC向集群声明存储需求。它引用不存在的StorageClass时会保持Pending，依赖该PVC的Pod也无法调度。

#### 为什么

Pod Pending不一定是CPU或节点问题。PVC Events、StorageClass和provisioner状态是存储调度链路的核心证据。

#### 怎么做

```bash
kubectl get pvc,pod
kubectl describe pvc data
kubectl get events --sort-by=.lastTimestamp
```

`storageClassName`不可原地修改，因此删除故障PVC并按相同名称重建：

```bash
kubectl delete pvc data
kubectl apply -f - <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard
  resources:
    requests:
      storage: 128Mi
YAML
kubectl get pvc data -w
```

看到`Bound`后退出观察。完成标准是PVC Bound且依赖Pod可以正常运行。

### LAB-013 Service流量转发异常

- 难度：中级；预计30分钟；Namespace：`kubelab-service-target-port`。

#### 是什么

Service的`port`是客户端访问端口，`targetPort`是转发到Pod的端口或命名端口。Endpoint存在只表示Service发现了Pod，不证明端口映射正确。

#### 为什么

这是“有Endpoint但连接失败”的典型根因。标签链路正常时，应继续核对Service端口、容器端口和应用监听端口。

#### 怎么做

```bash
kubectl get deployment,pod,service
kubectl get service web -o yaml
kubectl get endpointslice -l kubernetes.io/service-name=web
kubectl get pods -l app=service-target-port-lab \
  -o jsonpath='{.items[0].spec.containers[0].ports}{"\n"}'
```

保持Deployment和标签不变，只修复Service端口映射：

```bash
kubectl patch service web --type=merge -p \
'{"spec":{"ports":[{"name":"http","port":80,"targetPort":"http"}]}}'
kubectl get service web -o yaml
```

完成标准是Endpoint仍存在且固定集群内HTTP探针返回200。

### LAB-014 工作负载配置引用异常

- 难度：中级；预计25分钟；Namespace：`kubelab-configmap-key-missing`。

#### 是什么

ConfigMap对象存在不代表引用有效。`configMapKeyRef`同时依赖对象名称和键名；缺少目标键时容器仍会进入`CreateContainerConfigError`。

#### 为什么

排障不能停在“ConfigMap存在”。需要沿Deployment引用定位到具体对象和键，并保留无关的已有配置。

#### 怎么做

```bash
kubectl get configmap app-settings -o yaml
kubectl get deployment worker -o yaml
kubectl describe pods -l app=config-key-lab
kubectl get events --sort-by=.lastTimestamp
```

只补充缺失键，不覆盖`LOG_LEVEL`：

```bash
kubectl patch configmap app-settings --type=merge \
  -p '{"data":{"APP_MODE":"production"}}'
kubectl get configmap app-settings -o yaml
kubectl get pods -l app=config-key-lab -w
```

完成标准是配置契约满足且Pod稳定Running/Ready；公开结果不会显示配置值。

### LAB-015 Job执行失败

- 难度：中级；预计30分钟；Namespace：`kubelab-job-command-failure`。

#### 是什么

Job表示必须成功完成的一次性任务。Pod退出码非零时Job失败；其Pod模板不可变，不能像Deployment一样直接修改命令。

#### 为什么

批处理任务的成功标准是Completed/Succeeded，而不是长期Running。修复不可变Job通常需要删除并用相同名称重建。

#### 怎么做

```bash
kubectl get job,pod
kubectl describe job data-check
kubectl logs -l job-name=data-check
kubectl get pod -l job-name=data-check \
  -o jsonpath='{.items[0].status.containerStatuses[0].state.terminated.exitCode}{"\n"}'
```

删除旧Job并以安全配置重建成功命令：

```bash
kubectl delete job data-check
kubectl apply -f - <<'YAML'
apiVersion: batch/v1
kind: Job
metadata:
  name: data-check
  labels: {app: job-command-lab}
spec:
  backoffLimit: 0
  template:
    metadata:
      labels: {app: job-command-lab}
    spec:
      restartPolicy: Never
      containers:
        - name: checker
          image: busybox:1.36.1
          command: [sh, -c, "echo data-check-succeeded; exit 0"]
          resources:
            requests: {cpu: 25m, memory: 32Mi}
            limits: {cpu: 100m, memory: 128Mi}
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
            seccompProfile: {type: RuntimeDefault}
YAML
kubectl wait --for=condition=complete job/data-check --timeout=60s
```

完成标准是新Job的Pod进入Succeeded。

### LAB-016 StatefulSet稳定网络异常

- 难度：中级；预计35分钟；Namespace：`kubelab-statefulset-headless`。

#### 是什么

StatefulSet依赖Headless Service为Pod提供稳定网络身份。Service应保持`clusterIP: None`，并用正确Selector发现有状态Pod。

#### 为什么

StatefulSet Pod健康不代表稳定DNS和服务发现健康。数据库、消息队列等有状态系统经常依赖固定Pod DNS。

#### 怎么做

```bash
kubectl get statefulset,pod,service
kubectl get pods --show-labels
kubectl get service web-headless -o yaml
kubectl get endpointslice -l kubernetes.io/service-name=web-headless
```

保持StatefulSet、Pod标签和Headless属性不变，只修复Selector：

```bash
kubectl patch service web-headless --type=merge -p \
'{"spec":{"selector":{"app":"stateful-headless-lab"}}}'
kubectl get endpointslice -l kubernetes.io/service-name=web-headless -w
```

完成标准是Endpoint恢复且HTTP探针成功。后续变体还会进一步验证治理Service名称和Headless语义。

### LAB-017 DaemonSet调度异常

- 难度：中级；预计30分钟；Namespace：`kubelab-daemonset-node-selector`。

#### 是什么

DaemonSet通常在每个符合条件的节点运行一个Pod。错误的`nodeSelector`会让控制器找不到目标节点，甚至一个Pod都不会创建。

#### 为什么

节点代理、日志采集器和监控Agent常使用DaemonSet。排障应检查控制器调度条件，而不是修改Node来迎合错误工作负载。

#### 怎么做

```bash
kubectl get daemonset node-agent
kubectl describe daemonset node-agent
kubectl get pods -l app=daemonset-node-selector-lab
kubectl get daemonset node-agent -o yaml
```

移除工作负载中错误的节点条件：

```bash
kubectl patch daemonset node-agent --type=json -p \
'[{"op":"remove","path":"/spec/template/spec/nodeSelector"}]'
kubectl rollout status daemonset/node-agent
kubectl get pods -l app=daemonset-node-selector-lab
```

完成标准是至少一个DaemonSet Pod稳定Running和Ready。受限Workspace不允许读取或修改Node。

### LAB-018 工作负载存储异常

- 难度：中级；预计30分钟；Namespace：`kubelab-pvc-claim-missing`；要求默认StorageClass。

#### 是什么

Deployment引用名为`app-data`的PVC，但对象不存在。它与LAB-012的区别是：这里首先缺少PVC，而不是PVC存在但无法绑定。

#### 为什么

同样表现为Pod Pending，Events可能分别指出“claim not found”和“provisioning failed”。准确区分能避免错误修改StorageClass。

#### 怎么做

```bash
kubectl get deployment,pod,pvc
kubectl describe pods -l app=pvc-consumer
kubectl get events --sort-by=.lastTimestamp
kubectl get deployment consumer -o yaml
```

创建Deployment所需的受限PVC：

```bash
kubectl apply -f - <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: app-data
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard
  resources:
    requests:
      storage: 128Mi
YAML
kubectl get pvc app-data -w
```

PVC Bound后继续观察Pod。完成标准是PVC Bound且Pod稳定Running/Ready。

## 5. 双根因综合场景

综合实验的关键规则是：修复第一个根因后必须重新观察。状态变化会揭示被前一层故障遮挡的第二个根因。

### LAB-019 配置到服务链路故障

- 难度：高级；预计50分钟；Namespace：`kubelab-configuration-service-chain`。

#### 是什么

第一层是ConfigMap缺少`APP_MODE`，使Pod无法创建；第二层是Service把流量发到错误的`targetPort`。配置修好后服务仍不可访问。

#### 为什么

真实事故经常包含串联故障。一次修改后看到Pod Ready，不代表端到端业务恢复；最终必须验证Endpoint和HTTP。

#### 怎么做

第一阶段观察并恢复配置：

```bash
kubectl get configmap,deployment,pod,service
kubectl describe pods -l app=configuration-service-chain
kubectl patch configmap app-settings --type=merge \
  -p '{"data":{"APP_MODE":"production"}}'
kubectl get pods -l app=configuration-service-chain -w
```

Pod Ready后不要停止，继续检查服务链路：

```bash
kubectl get service web -o yaml
kubectl get endpointslice -l kubernetes.io/service-name=web
kubectl patch service web --type=merge -p \
'{"spec":{"ports":[{"name":"http","port":80,"targetPort":"http"}]}}'
```

完成标准是配置正确、Pod Ready、Endpoint存在且HTTP 200同时成立。

### LAB-020 存储到就绪链路故障

- 难度：高级；预计50分钟；Namespace：`kubelab-storage-readiness-chain`；要求默认StorageClass。

#### 是什么

第一层缺少PVC，Pod无法调度；创建PVC后容器可以运行，但错误的Readiness路径让Pod持续NotReady。

#### 为什么

存储修复会把故障从“调度前”推进到“运行后”。如果不重新查看Pod条件和Events，很容易把部分恢复误当作完成。

#### 怎么做

先确认缺少的claim并创建PVC：

```bash
kubectl get deployment,pod,pvc
kubectl describe pods -l app=storage-readiness-chain
kubectl apply -f - <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: app-data
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard
  resources:
    requests:
      storage: 128Mi
YAML
kubectl get pvc app-data -w
```

Pod开始Running后检查Ready条件并修复探针：

```bash
kubectl describe pods -l app=storage-readiness-chain
kubectl get deployment web -o yaml
kubectl patch deployment web --type=json -p \
'[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/"}]'
kubectl rollout status deployment/web
```

完成标准是PVC Bound、Pod稳定Ready、Endpoint存在且HTTP 200。

### LAB-021 有状态服务发现链路故障

- 难度：高级；预计55分钟；Namespace：`kubelab-stateful-service-chain`。

#### 是什么

第一层是Headless Service Selector错误，Endpoint为空；第二层是`targetPort`错误。修复发现关系后，稳定Pod DNS可以建立，但业务端口仍不可用。

#### 为什么

有状态服务需要同时满足Pod稳定身份、Service发现和应用协议可达。任何一层恢复都不代表整个链路恢复。

#### 怎么做

先检查StatefulSet、标签、Headless属性和Endpoint：

```bash
kubectl get statefulset,pod,service
kubectl get pods --show-labels
kubectl get service web-headless -o yaml
kubectl get endpointslice -l kubernetes.io/service-name=web-headless
```

第一步只修复Selector并确认Endpoint出现：

```bash
kubectl patch service web-headless --type=merge -p \
'{"spec":{"selector":{"app":"stateful-service-chain"}}}'
kubectl get endpointslice -l kubernetes.io/service-name=web-headless
```

第二步继续核对端口，并把流量指向Pod的`http`命名端口：

```bash
kubectl patch service web-headless --type=merge -p \
'{"spec":{"ports":[{"name":"http","port":80,"targetPort":"http"}]}}'
kubectl get service web-headless -o yaml
```

完成标准是Endpoint、稳定Pod DNS和HTTP检查全部通过。DNS原始地址和探针输出不会进入公开DTO或数据库。

## 6. 每次实验都要完成的复盘

不要只记录“改了哪个字段”。建议按照以下结构写入KubeLab复盘：

1. 现象：用户或监控看到什么，哪些资源仍然健康？
2. 调查过程：按什么顺序查看控制器、Pod、Events、日志、依赖和网络？
3. 根因：哪一段声明式契约与实际对象不一致？
4. 修复：为什么只修改这些字段，如何避免扩大变更？
5. 预防措施：可以增加什么发布检查、策略、监控或演练？

复盘完成后由Web导出脱敏Markdown。不要粘贴Secret、完整kubeconfig、Token、凭证、完整Manifest或异常堆栈。

## 7. 推荐学习路径

```text
工作负载基础
LAB-001 → LAB-002 → LAB-004 → LAB-005 → LAB-006 → LAB-010

配置管理
LAB-003 → LAB-008 → LAB-014 → LAB-019

服务与流量
LAB-007 → LAB-009 → LAB-011 → LAB-013 → LAB-016 → LAB-021

存储与调度
LAB-012 → LAB-017 → LAB-018 → LAB-020
```

对LAB-013至018，先完成基线，再让KubeLab按确定性规则轮换盲练变体。完成一个基线加一个变体后，再进入相关综合实验，能更好地区分记答案和真正掌握排障模型。
