# M8 实验作者契约

M8 在现有 `Lab`、`LabVariant`、`LabRegistry` 和验证引擎之上增加只供作者工具消费的 `LabAuthoringContract`。运行时不读取作者契约，学习 Session、进度和 Web API 仍以既有业务模型为唯一状态来源。

## 文件位置

- 基线或综合场景：`labs/<lab-id>/authoring.yaml`
- 固定变体：`labs/<lab-id>/variants/<variant-id>/authoring.yaml`

作者契约与场景目录绑定，不允许声明跨目录的 Manifest、修复文件或父实验。所有路径均为场景目录内的相对 POSIX 路径。

## 契约结构

作者契约固定使用：

```yaml
apiVersion: kubelab.io/v1alpha1
kind: LabAuthoringContract
scenarioType: baseline
states:
  faulted:
    observations: {}
  repaired:
    observations: {}
  reset: faulted
repairs:
  full:
    manifest: solutions/fix.yaml
    allowedChanges: []
```

`scenarioType` 只能为 `baseline`、`variant` 或 `composite`。综合场景必须增加 `firstRepair` 状态，以及 `first`、`full` 两个修复计划；其他场景禁止声明第一阶段。

每个状态按现有 check ID 提供实际观测。观测类型由对应的运行时 check 类型决定，不能在作者文件中重新定义验证条件。可声明的观测仅包括资源存在性、Pod 安全摘要、可用副本数、Endpoint 数量、镜像、配置匹配布尔结果、PVC Phase、HTTP 状态和 DNS 解析布尔结果。禁止原始日志、探针输出、Secret 值、异常正文和 Kubernetes 原始对象。

`reset` 只能引用 `faulted`，避免维护一份可能漂移的重复故障状态。每个状态必须覆盖 initial 和 success checks，引用未知 check、漏掉 check 或让共享 Gateway 查询产生冲突观测均视为契约错误。

## 修复边界

每项 `allowedChanges` 由资源身份、操作类型和 JSON Pointer 白名单组成：

- `modify`：资源已存在，只允许修改声明路径；
- `create`：初始环境不存在该资源，修复文件可创建它；
- `recreate`：仅用于 Kubernetes 不可变资源，允许删除精确资源后按修复 Manifest 重建。

修复文件继续接受现有 Manifest 安全扫描。工具只比较结构化资源，不执行 README 中的命令，也不自动修改作者内容。

## 作者命令边界

- `kubelab lab init` 使用固定模板生成安全、可运行样例，不覆盖现有文件。
- `kubelab lab lint` 复用运行时 Schema、Registry 和 Manifest 安全规则。
- `kubelab lab test` 默认使用声明式 Fake Gateway，不访问数据库或集群。
- `kubelab lab inspect` 区分作者视图、通过前公开预览和通过后公开预览。
- `kubelab lab package` 只生成确定性作者包，不安装或执行包内内容。

真实集成测试必须显式设置 `KUBELAB_RUN_LAB_INTEGRATION=1`，并通过 WSL2、本机 Docker 驱动 minikube、Context Trust 和 readiness 门禁。M8 不自动启动集群、不自动信任 Context，也不连接远程或生产集群。

## 稳定错误与退出码

问题对象固定包含 `code`、`severity`、`relativePath`、`fieldPath`、`message` 和 `docsAnchor`。输出统一脱敏，不包含绝对用户路径、完整 Manifest、Secret 或堆栈。

- `0`：通过或仅有警告
- `2`：输入、目录或 Schema 错误
- `3`：安全或公开边界错误
- `4`：Fake 契约失败
- `5`：集成环境不满足
- `10`：脱敏内部错误
