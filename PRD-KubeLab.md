# KubeLab 产品需求文档（PRD）

> 产品名称：KubeLab（暂定）
> 文档版本：v0.2
> 文档状态：WSL2 Ubuntu MVP需求基线
> 正式运行平台：Windows 11上的WSL2 Ubuntu
> 目标集群：minikube优先，后续兼容kind
> 产品定位比例：80%个人练习与面试准备，10%简历展示，10%开源复用

---

## 1. 产品概述

KubeLab是一个运行在WSL2 Ubuntu中的本地Kubernetes实战训练平台。平台连接同一WSL发行版内的Docker Engine和minikube集群，通过预定义实验自动创建资源、注入故障、展示必要的排障信息、验证修复结果并保存学习记录。Windows只用于编辑代码和通过localhost访问Web页面，不运行KubeLab Python进程或Kubernetes工具。

平台不以替代Kubernetes Dashboard为目标，而是围绕以下训练闭环设计：

```text
选择实验 → 创建故障环境 → 使用kubectl排查 → 修复资源 → 自动验证 → 记录复盘
```

用户主要在WSL2 Ubuntu终端中执行`kubectl`、`helm`等命令。平台负责提供任务、管理实验生命周期、判定结果和沉淀复盘，从而训练真实Linux命令行操作能力。

---

## 2. 背景与问题定义

### 2.1 用户问题

初学者在本地学习Kubernetes时通常面临以下问题：

1. 教程以成功部署为主，缺少系统性的故障练习。
2. 不清楚一个知识点应练到什么程度才满足面试要求。
3. 实验之间容易相互污染，失败后难以恢复初始状态。
4. 能照着教程执行命令，但无法独立判断排障顺序。
5. 缺少自动判定机制，不确定自己的修复是否真正恢复了业务。
6. 练习过程没有形成可复用的故障案例和面试表达。

### 2.2 产品解决方案

KubeLab通过YAML驱动的实验定义提供：

- 可重复创建的故障环境；
- 每个实验独立的Namespace隔离；
- 面向任务而非答案的故障描述；
- 资源状态、Events和Logs等必要观察入口；
- 多条件自动验证；
- 分级提示；
- 一键重置和清理；
- 学习进度与故障复盘记录。

---

## 3. 产品目标

### 3.1 MVP目标

1. 环境齐全的用户可以在5分钟内开始第一个实验。
2. 用户无需手动搭建故障场景，即可练习至少12个高频Kubernetes问题。
3. 用户修复后，平台能够从资源状态和业务可用性两个层面自动验证结果。
4. 任一实验都可以安全重置，并恢复到一致的初始故障状态。
5. 用户可以查看完成记录、提示使用情况和故障复盘。
6. 项目结构允许贡献者仅增加YAML和Manifest即可新增大部分实验。

### 3.2 长期目标

1. 覆盖初级云原生运维、DevOps和SRE常见面试场景。
2. 支持确定性故障变体、综合故障和专题学习路径。
3. 支持minikube和kind，并提供可复用的开源安装方式。
4. 形成具备安全作者工具链的可扩展Kubernetes故障实验库。

### 3.3 非目标

MVP阶段不包含：

- 多用户与账号系统；
- 云端托管实验集群；
- 完整Kubernetes Dashboard；
- 浏览器内置Shell终端；
- AI自动给出根因或自动修复；
- 多集群统一管理；
- 自研容器运行时或调度器；
- 社区排行和社交功能；
- Kubernetes Operator开发。

---

## 4. 目标用户

### 4.1 核心用户

正在准备初级云原生运维、Kubernetes运维或DevOps技术面试的个人学习者。

典型特征：

- 已在同一WSL2 Ubuntu发行版中安装Docker Engine、minikube、kubectl；
- 能执行基础Linux和kubectl命令；
- 缺少生产环境故障经验；
- 希望通过本地实验提升排障能力；
- 使用Windows编辑器和浏览器，并愿意在WSL2 Ubuntu终端中完成练习。

### 4.2 次要用户

- 希望快速复习Kubernetes故障场景的在职运维人员；
- 希望贡献实验内容的开源用户；
- 希望查看项目设计和工程能力的面试官。

---

## 5. 核心使用场景

### 场景A：日常专项练习

用户选择“Service无法访问Pod”，平台创建带有错误Selector的Service。用户在本地终端执行`kubectl get endpoints`等命令定位问题，修复后回到平台验证。

### 场景B：故障复盘

用户完成实验后填写故障现象、排查路径、根因、修复和防复发措施，形成可用于面试表达的案例。

### 场景C：专题综合练习

用户完成同一知识点的原始基线和固定变体后，进入包含多个根因的综合实验。平台只给出业务现象和成功目标，用户按证据链逐步定位并修复，完成后形成可用于面试表达的复盘记录。

### 场景D：重复练习

用户重置实验，平台清理相关Namespace后重新创建相同故障，保证实验状态可重复。

### 场景E：新增社区实验

贡献者按照实验规范新增`lab.yaml`和Kubernetes Manifest，通过校验测试后即可加入实验目录，无需修改核心代码。

---

## 6. 产品范围与优先级

### 6.1 P0：MVP必须实现

| 模块 | 功能 | 说明 |
|---|---|---|
| 环境检查 | Docker/minikube/kubectl检查 | 显示状态、版本、Context和可操作建议 |
| 实验目录 | 浏览实验 | 显示难度、分类、时间、知识点和完成状态 |
| 生命周期 | 开始/状态/验证/重置/结束 | 管理实验完整生命周期 |
| 故障注入 | 自动创建初始故障 | 由Manifest或初始化动作产生 |
| 资源观察 | Pod/Deployment/Service/Endpoint | 只展示排障必要信息 |
| 日志事件 | Events/当前日志/Previous日志 | 支持常用过滤和刷新 |
| 自动验证 | 资源状态与业务探测 | 支持组合检查与明确失败原因 |
| 分级提示 | 三级提示 | 从方向、资源到建议命令逐步递进 |
| 安全隔离 | Namespace与资源限制 | 保护非实验资源和宿主环境 |
| 学习记录 | 进度、耗时、验证和提示 | 使用SQLite本地持久化 |
| 故障复盘 | 用户填写并保存 | 按标准模板保存 |
| 实验内容 | 首批12个实验 | 覆盖基础发布与高频故障 |

### 6.2 P1：MVP稳定后

- 确定性故障变体和综合实验；
- 专题学习路径与症状索引；
- 实验作者CLI工具链；
- 复盘导出Markdown；
- 薄弱知识点统计；
- Helm专项实验；
- Prometheus/Grafana专项实验；
- kind集群支持；
- 一键安装脚本和离线安装说明。

### 6.3 P2：暂缓

- 浏览器终端；
- 多用户及权限体系；
- 云端部署和共享实验集群；
- AI辅助排障；
- 多集群与远程集群；
- 自定义Controller/Operator；
- 社区排行榜。

---

## 7. 信息架构

### 7.1 首页/环境页

展示：

- Docker状态；
- minikube状态；
- kubectl连接状态；
- 当前Context；
- Kubernetes版本；
- 节点数量与状态；
- CPU和内存可用情况；
- Helm、Ingress、metrics-server状态；
- 最近练习和总体进度。

### 7.2 实验目录页

支持按照以下维度浏览：

- 分类：基础、发布、网络、配置、资源、存储、综合；
- 难度：入门、初级、进阶；
- 状态：未开始、进行中、已完成；
- 预计耗时；
- 涉及知识点。

MVP可以只实现分类和状态筛选。

### 7.3 实验详情页

包含：

1. 实验背景与故障现象；
2. 当前任务；
3. 完成条件；
4. 资源状态摘要；
5. Events；
6. 当前日志和Previous日志；
7. 分级提示；
8. 验证按钮；
9. 重置与结束按钮；
10. 复盘输入区。

### 7.4 学习记录页

展示：

- 已完成实验；
- 最近练习时间；
- 每次耗时；
- 验证次数；
- 提示使用次数；
- 复盘内容；
- 按分类汇总的完成情况。

---

## 8. 核心用户流程

```text
打开平台
  ↓
执行环境检查
  ├─ 失败：显示原因和修复建议
  └─ 通过：进入实验目录
              ↓
           选择实验
              ↓
           开始实验
              ↓
     创建Namespace与故障资源
              ↓
     用户在本地终端排查和修改
              ↓
           点击验证
       ├─ 未通过：显示未满足的结果，不泄露根因
       └─ 通过：保存完成时间与结果
                       ↓
                    填写复盘
                       ↓
                结束或重置实验
```

---

## 9. 功能需求

### FR-001 环境检查

#### 描述

平台启动后检查本地练习环境是否满足实验要求。

#### 检查项

- Docker进程可用；
- `minikube status`返回可用状态；
- kubeconfig可读取；
- 当前Context属于允许列表；
- Kubernetes API可访问；
- 节点处于Ready；
- 集群资源满足实验最低要求；
- 实验需要的可选组件是否存在。

#### 验收标准

- 任一检查失败时，明确显示检查项、实际状态和建议操作；
- 不使用“环境异常”等无上下文错误信息；
- 环境检查失败不会创建任何Kubernetes资源；
- 检查结果可以手动重新执行。

### FR-002 实验目录

#### 描述

平台从`labs/`目录加载合法实验并显示实验元数据。

#### 验收标准

- 非法或缺少必填字段的实验不会进入可运行列表；
- 加载失败时显示具体文件及校验原因；
- 新增合规实验不需要修改平台核心代码；
- 用户可查看实验难度、时间、分类、知识点和状态。

### FR-003 开始实验

#### 描述

用户开始实验后，平台创建隔离Namespace并应用初始化资源。

#### 验收标准

- Namespace名称必须使用`kubelab-`前缀；
- 重复点击开始不会重复创建冲突资源；
- 创建过程部分失败时执行清理或进入可恢复状态；
- 初始化完成后显示实验已就绪；
- 初始化超时显示具体失败步骤。

### FR-004 实验状态

实验状态定义：

```text
not_started
provisioning
ready
in_progress
passed
resetting
error
```

状态变化必须保存到本地数据库。异常退出后，平台应通过实际集群状态进行校准，而不是只相信数据库记录。

### FR-005 资源观察

#### 支持资源

- Namespace；
- Pod；
- Deployment；
- ReplicaSet；
- Service；
- Endpoint/EndpointSlice；
- ConfigMap；
- Secret元数据；
- Ingress；
- PVC。

#### 安全要求

- Secret默认不显示明文值；
- 只展示当前实验Namespace中的资源；
- 不提供修改`kube-system`或其他Namespace的入口。

### FR-006 Events与Logs

#### 验收标准

- 支持查看按时间排序的Events；
- 支持查看容器当前日志；
- 支持查看上一次退出容器的Previous日志；
- 多容器Pod需要选择容器；
- 日志读取失败必须显示Pod、容器和错误原因；
- 默认限制日志行数，避免加载超大日志。

### FR-007 自动验证引擎

#### P0检查类型

```text
resource_exists
resource_not_exists
pod_phase
pod_ready
deployment_available
service_has_endpoints
container_image
config_value
pvc_phase
http_status
```

#### 验收标准

- 一个实验可以组合多个检查；
- 所有必需检查通过后才能判定实验成功；
- 验证失败只说明未满足的目标，不直接泄露故障根因；
- 每次验证保存时间和结果；
- HTTP探测必须设置超时；
- 检查异常与“用户尚未修复”必须区分。

### FR-008 分级提示

#### 规则

- 每个实验最多三级提示；
- 提示按照顺序解锁；
- 平台记录提示使用次数和时间；
- 第一级提供方向，第二级定位资源，第三级提供建议命令；
- 提示不能直接给出完整修复YAML。

### FR-009 重置实验

#### 描述

重置操作删除实验Namespace，并重新创建初始故障环境。

#### 验收标准

- 删除目标必须经过Namespace前缀和实验归属双重校验；
- 禁止删除`default`、`kube-system`等非实验Namespace；
- 删除或重建超时必须明确报告；
- 重置后验证应重新处于未通过状态；
- 重复重置应保持幂等。

### FR-010 结束实验

结束实验时，用户选择：

- 保留环境用于继续观察；或
- 清理实验Namespace。

MVP默认推荐清理。清理前必须显示明确目标Namespace。

### FR-011 学习记录

平台记录：

- 实验ID；
- 开始和完成时间；
- 总耗时；
- 验证次数；
- 提示使用次数；
- 是否成功；
- 重置次数；
- 复盘内容。

数据仅保存本地，不上传外部服务。

### FR-012 故障复盘

复盘模板：

```text
故障现象：
影响范围：
排查路径：
根本原因：
解决方案：
防复发措施：
面试口述版本：
```

MVP支持保存和编辑；P1支持导出Markdown。

---

## 10. 首批实验清单

| ID | 实验 | 难度 | 核心检查 |
|---|---|---:|---|
| LAB-001 | Deployment扩缩容 | 入门 | 可用副本数 |
| LAB-002 | 滚动更新与回滚 | 入门 | 镜像版本、可用副本 |
| LAB-003 | ConfigMap注入 | 入门 | 配置值、Pod Ready |
| LAB-004 | 探针基础 | 入门 | Readiness/Liveness状态 |
| LAB-005 | ImagePullBackOff | 初级 | 正确镜像、Pod Ready |
| LAB-006 | CrashLoopBackOff | 初级 | 重启停止、Pod Ready |
| LAB-007 | Service Selector错误 | 初级 | Endpoint数量、HTTP 200 |
| LAB-008 | ConfigMap缺失 | 初级 | ConfigMap存在、Pod Ready |
| LAB-009 | Readiness路径错误 | 初级 | Pod Ready、Endpoint恢复 |
| LAB-010 | OOMKilled | 初级 | 资源限制合理、Pod稳定 |
| LAB-011 | Ingress后端端口错误 | 初级 | Ingress访问HTTP 200 |
| LAB-012 | PVC无法绑定 | 初级 | PVC Bound、Pod Ready |

首个开发切片只实现LAB-005、LAB-006、LAB-007，用于验证核心实验引擎。

---

## 11. 实验定义规范

### 11.1 示例

```yaml
apiVersion: kubelab.io/v1
kind: Lab

metadata:
  id: service-selector
  name: Service无法访问Pod
  difficulty: beginner
  duration: 20m
  category: networking
  tags:
    - service
    - endpoint
    - label

requirements:
  kubernetes: ">=1.28"
  addons: []
  minimumCpu: 2
  minimumMemory: 2Gi

environment:
  namespace: kubelab-service-selector
  manifests:
    - manifests/deployment.yaml
    - manifests/service.yaml
  readyTimeout: 120s

task:
  description: |
    Web Pod正在正常运行，但通过Service无法访问。
    请定位故障并恢复服务。
  successMessage: |
    Service已经正确匹配Pod，应用访问恢复。

checks:
  - type: deployment_available
    name: web
    minimumReplicas: 2

  - type: service_has_endpoints
    name: web-service
    minimumEndpoints: 2

  - type: http_status
    service: web-service
    port: 80
    path: /
    expectedStatus: 200
    timeout: 3s

hints:
  - level: 1
    content: 检查流量是否已经找到后端实例。

  - level: 2
    content: 比较Service Selector和Pod Label。

  - level: 3
    content: 使用kubectl get endpoints和kubectl get pods --show-labels。

cleanup:
  deleteNamespace: true

interview:
  questions:
    - Service如何发现后端Pod？
    - Service存在但Endpoint为空可能有哪些原因？
    - Pod显示Running是否代表应用一定可用？
```

### 11.2 实验包结构

```text
labs/
└── service-selector/
    ├── lab.yaml
    ├── manifests/
    │   ├── deployment.yaml
    │   └── service.yaml
    ├── tests/
    │   ├── initial-state.yaml
    │   └── solved-state.yaml
    └── README.md
```

### 11.3 规范要求

- 实验ID全局唯一；
- Namespace必须以`kubelab-`开头；
- 必须定义初始失败状态和修复成功状态；
- 必须设置资源requests和limits；
- 必须定义初始化超时；
- 必须支持重复开始、验证和重置；
- 不允许默认创建特权容器或HostPath；
- 不允许修改非实验Namespace资源；
- 每个实验必须包含自动化测试。

---

## 12. 非功能需求

### 12.1 安全

1. 默认只允许连接名称匹配`minikube`的Context。
2. 默认拒绝未知、远程或疑似生产Context。
3. 所有实验资源必须限制在`kubelab-`前缀Namespace内。
4. 删除前必须校验Namespace前缀和实验记录。
5. 禁止创建特权容器、HostNetwork、HostPID、HostIPC和HostPath。
6. Secret值默认隐藏。
7. 外部命令参数不得直接拼接未经校验的用户输入。

### 12.2 可靠性

- 实验创建和重置必须幂等；
- 平台异常退出后可以根据集群实际状态恢复；
- 每个外部调用必须设置超时；
- 失败信息必须包含实验ID、资源、操作和原始错误；
- 不允许吞掉异常；
- 清理部分失败时列出残留资源。

### 12.3 性能

- 页面首次加载目标小于2秒，不包含集群启动时间；
- 普通资源状态查询目标小于2秒；
- 单实验创建目标小于60秒；
- 单次验证目标小于10秒；
- 默认日志查询最多500行。

### 12.4 兼容性

MVP正式支持：

- Windows 11上的WSL2；
- WSL2 Ubuntu；
- Ubuntu内运行的Docker Engine；
- minikube；
- Kubernetes 1.28及以上；
- Windows侧Chrome、Edge最新版，通过localhost访问Web。

Windows原生进程、WSL2中的非Ubuntu发行版、独立Linux主机和kind支持作为P1目标。具体Ubuntu、Docker、minikube和Kubernetes版本范围只根据真实环境自动化测试结果声明。

### 12.5 隐私

- 所有数据默认保存在本地；
- 不采集用户命令历史；
- 不上传kubeconfig、日志、Secret或学习记录；
- MVP不包含遥测。

### 12.6 可维护性

- 实验内容与核心代码分离；
- 新增普通实验不需要修改验证引擎；
- 核心模块具备单元测试；
- 实验具备初始状态和修复状态的集成测试；
- 错误信息包含可操作上下文。

---

## 13. 技术边界与建议架构

本节只定义产品约束，详细实现由后续技术设计文档确定。

### 13.1 建议技术栈

- 后端：WSL2 Ubuntu中的Python 3.11 + FastAPI；
- CLI：Typer；
- Kubernetes：官方Python Kubernetes Client；
- 数据库：SQLite；
- 实验定义：YAML + Schema校验；
- Web：服务端模板和少量JavaScript；
- 测试：pytest；
- 代码检查：ruff；
- 本地启动：WSL Linux文件系统中的uv虚拟环境；后续再评估容器化平台进程。

### 13.2 核心模块

```text
EnvironmentDoctor
  └── 检查本地依赖、Context和集群状态

LabRegistry
  └── 加载并校验实验定义

LabManager
  ├── start
  ├── status
  ├── reset
  └── cleanup

ValidationEngine
  └── 执行组合检查并返回结构化结果

KubernetesGateway
  └── 封装Kubernetes API访问

ProgressRepository
  └── 保存练习、验证、提示和复盘记录
```

### 13.3 CLI基线

核心能力必须先通过CLI验证：

```bash
kubelab doctor
kubelab list
kubelab start service-selector
kubelab status service-selector
kubelab verify service-selector
kubelab hint service-selector
kubelab reset service-selector
kubelab cleanup service-selector
```

Web界面只能调用已经验证的应用服务，不应复制一套独立业务逻辑。

---

## 14. 数据需求

### 14.1 实验记录

```text
id
lab_id
namespace
status
started_at
completed_at
duration_seconds
verification_count
hint_level_used
reset_count
last_error
```

### 14.2 验证记录

```text
id
session_id
checked_at
passed
check_type
target
actual_result
expected_result
error_context
```

### 14.3 复盘记录

```text
session_id
symptom
impact
investigation
root_cause
resolution
prevention
interview_summary
updated_at
```

---

## 15. 错误与边界条件

必须覆盖：

- Docker未启动；
- minikube不存在或停止；
- kubeconfig缺失或损坏；
- 当前Context不是minikube；
- Kubernetes API超时；
- 节点NotReady；
- CPU或内存不足；
- Namespace已存在但数据库无记录；
- 数据库记录存在但Namespace已被手动删除；
- Manifest格式错误；
- 实验定义缺少字段；
- 资源创建部分成功；
- 验证过程API超时；
- 用户在平台外修改或删除实验资源；
- 重置时Namespace长期Terminating；
- 日志过大；
- 多容器Pod未指定容器；
- 应用HTTP探测超时；
- 平台运行中minikube被停止。

---

## 16. MVP验收标准

MVP完成必须同时满足：

1. `doctor`能够正确识别可用和不可用环境。
2. 平台只允许在明确的本地练习Context上运行实验。
3. LAB-005、LAB-006、LAB-007三个核心实验端到端通过。
4. 每个实验初始状态验证必须失败，正确修复后验证必须通过。
5. 开始、状态、验证、提示、重置和清理操作可重复执行。
6. 重置实验后恢复同一初始故障状态。
7. 平台不能删除或修改非`kubelab-`Namespace。
8. Events、当前日志和Previous日志能够正常查看。
9. SQLite能够保存实验进度、验证次数、提示和复盘。
10. Web页面能够完成实验目录、详情、验证和重置主流程。
11. 至少12个实验内容完成，其中前三个具备完整自动化集成测试。
12. README包含WSL2 Ubuntu安装、启动、首个实验、Windows浏览器访问和卸载说明。
13. 实际执行所有自动化测试并通过后，才可标记MVP完成。

---

## 17. 测试与质量要求

### 17.1 测试层级

- 单元测试：Schema、状态机、验证器、Namespace安全校验；
- 集成测试：对minikube实际创建和清理资源；
- 实验契约测试：初始状态失败、解法状态通过；
- 回归测试：每次Bug修复增加对应场景；
- 手工验收：在WSL2 Ubuntu中完整走通首个实验，并从Windows浏览器访问Web。

### 17.2 实验契约

每个实验必须满足：

```text
创建实验成功
→ 初始验证失败
→ 应用标准修复
→ 验证通过
→ 重置实验
→ 再次验证失败
→ 清理后无残留资源
```

### 17.3 发布条件

- 所有P0测试通过；
- 不存在已知的Namespace越界修改风险；
- 不存在未处理的高风险删除操作；
- 不声称支持未经实际测试的平台或版本；
- 文档中的命令经过实际执行验证。

---

## 18. 里程碑与渐进交付

### M0：需求与设计基线

产出：

- PRD；
- 实验Schema；
- 状态机设计；
- 安全边界；
- 首批三个实验用例。

完成标准：所有核心输入、输出、边界条件和验收标准明确。

### M1：CLI垂直切片

范围：

- `doctor/list/start/status/verify/reset/cleanup`；
- LAB-005 ImagePullBackOff；
- LAB-006 CrashLoopBackOff；
- LAB-007 Service Selector错误；
- SQLite基础记录。

完成标准：三个实验在本地minikube端到端执行并通过自动化测试。

### M2：Web MVP

范围：

- 首页环境状态；
- 实验目录；
- 实验详情；
- Events与Logs；
- 验证、提示、重置；
- 学习进度与复盘。

完成标准：用户无需调用KubeLab CLI即可在浏览器管理实验，但排障操作仍在本地终端进行。

### M3：实验库完善

范围：

- 扩展到12个实验；
- 所有实验契约校验；
- 分类与进度统计；
- 面试题关联。

完成标准：12个实验均可重复创建、验证和清理。

### M4：简历与开源包装

范围：

- 项目截图；
- 架构图；
- 安装与贡献文档；
- License；
- Issue和PR模板；
- 示例复盘；
- 发布第一个版本。

完成标准：新用户按照README可以在已具备依赖的本地环境中完成首个实验。

---

## 19. 成功指标

### 使用指标

- 环境正常时，首次实验启动时间不超过5分钟；
- 单个实验重置成功率达到95%以上；
- 至少80%的实验可以不依赖平台内答案完成；
- 用户能够从已完成实验中整理出至少3个面试故障案例。

### 工程指标

- P0核心模块具备自动化测试；
- 首批实验契约测试通过率100%；
- Namespace越界操作测试通过率100%；
- 新增普通实验不需要修改核心业务代码；
- 清理后不存在已知实验资源残留。

### 展示指标

- README能够清楚说明问题、架构和使用流程；
- 至少提供3个实验的截图或演示；
- 项目能够明确说明故障注入、自动验证、安全隔离三个技术亮点。

---

## 20. 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| 开发平台挤占K8s学习时间 | 偏离80%自练目标 | 先CLI，后Web；每个功能必须对应一个学习场景 |
| 验证器误判 | 用户形成错误认知 | 组合检查资源状态和业务探测，增加契约测试 |
| 删除错误Namespace | 可能破坏用户资源 | Context白名单、前缀校验、归属校验、测试覆盖 |
| 实验状态不可重复 | 无法稳定练习 | 所有实验要求幂等、重置和初始状态测试 |
| WSL发行版或Windows/WSL路径混用 | 工具不可发现、SQLite锁异常或虚拟环境损坏 | MVP固定WSL2 Ubuntu；配置、数据库、日志和虚拟环境位于Linux文件系统 |
| 实验直接暴露答案 | 降低训练价值 | 任务只描述现象，验证和提示不直接给完整修复 |
| 过早做复杂前端 | 延迟核心价值 | 使用服务端模板，P1再评估前后端分离 |
| 版本兼容性变化 | 实验失效 | 定义版本范围，CI中逐步增加受支持版本测试 |

---

## 21. 待后续设计确认

以下内容不阻塞PRD基线，但必须在技术设计阶段确定：

1. 实验Schema使用JSON Schema还是Pydantic模型作为唯一真源；
2. HTTP验证使用临时Pod、端口转发还是Kubernetes API代理；
3. WSL2 localhost转发在受支持Windows版本上的验收矩阵；
4. minikube Context白名单和用户显式授权机制；
5. Namespace长期Terminating时的恢复策略；
6. Helm实验由CLI子进程还是SDK封装；
7. WSL2 Ubuntu开源安装方式采用`uv tool install`还是安装脚本。

---

## 22. 下一份文档

PRD确认后进入技术设计阶段，下一份文档应为：

```text
KubeLab技术设计文档
├── 系统架构
├── 模块职责
├── 实验Schema
├── 状态机
├── 验证器接口
├── 数据库模型
├── Kubernetes安全边界
├── 异常与回滚策略
├── 测试策略
└── M1任务拆分
```

后续产品阶段的详细需求见：

- [M7专题学习路径PRD](docs/PRD-M7-LEARNING-PATHS.md)
- [M8实验作者工具链PRD](docs/PRD-M8-AUTHOR-TOOLCHAIN.md)
