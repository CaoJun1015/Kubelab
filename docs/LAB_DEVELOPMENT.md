# KubeLab 实验开发指南

KubeLab 实验是声明式、安全扫描且可重复验证的故障场景。学习运行时只读取`lab.yaml`、`variant.yaml`和`manifests/`；作者工具额外读取`authoring.yaml`与`solutions/`，不会把标准修复应用到学习者Session。

## 目录约定

```text
labs/lab-NNN-short-name/
├── lab.yaml
├── authoring.yaml             # 作者Fake观测与允许修复范围
├── README.md
├── manifests/
│   └── resources.yaml
├── solutions/
│   ├── fix.yaml
│   └── fix-stage-1.yaml       # 仅综合实验
└── variants/                  # 可选；仅固定复练场景
    └── variant-b/
        ├── variant.yaml
        ├── authoring.yaml
        ├── README.md
        ├── manifests/
        │   └── resources.yaml
        └── solutions/
            └── fix.yaml
```

实验 ID、目录名和 Namespace 后缀应稳定、可读且唯一。Manifest 必须显式写入实验 Namespace，不能包含 Namespace、Node、ClusterRole、ClusterRoleBinding、CRD 等集群级资源，也不能引用宿主机路径、任意 URL 或外部命令。

## 作者命令工作流

```bash
kubelab lab init TARGET --type baseline|variant|composite
kubelab lab lint TARGET
kubelab lab test TARGET
kubelab lab inspect TARGET
kubelab lab package TARGET
```

`init`只写入空的新目录，先在同级临时目录完整生成，再原子重命名；拒绝符号链接、越界路径和冲突文件。非交互环境必须显式提供ID、标题、分类、难度和说明。`--dry-run`只返回相对文件清单。

`lint`复用正式Lab/Variant Schema、Registry和Manifest安全扫描，并执行资源级结构diff。`test`使用声明式Fake Gateway与正式ValidationEngine，既不创建数据库，也不连接集群。`inspect`只显示脱敏摘要、允许修改路径及通过前/后公开预览。`package`只接受完整实验族，输出可复现归档和SHA-256，不负责安装。

所有命令支持`--json`，问题结构固定为`code/severity/relativePath/fieldPath/message/docsAnchor`。退出码为：`0`通过或仅警告、`2`输入/目录/Schema错误、`3`安全或公开边界错误、`4`Fake契约失败、`5`集成环境不满足、`10`脱敏内部错误。

## `authoring.yaml`契约

权威Schema位于`schemas/lab-authoring-v1alpha1.schema.json`，固定为`kubelab.io/v1alpha1 kind: LabAuthoringContract`。`scenarioType`只能是`baseline`、`variant`或`composite`；状态必须包含`faulted/repaired/reset`，综合实验还必须包含`firstRepair`，且`reset`固定引用`faulted`。

普通场景声明`repairs.full`，综合场景同时声明`repairs.first`和`repairs.full`。每项修复只能引用声明式Manifest，并逐资源声明`modify/create/recreate`及允许变化的JSON Pointer。不能嵌入命令、脚本或插件；`recreate`只用于需要安全重建的资源，删除目标必须是作者契约中精确列出的、属于实验Namespace的资源。

Fake observation以已有check ID为键，只保存九类验证器需要的最小实际观测。不得保存日志、HTTP/DNS原始输出、Secret值、异常正文或Kubernetes原始对象；共用同一次Gateway查询的多个check必须给出一致观测。

## 定义实验

`lab.yaml` 必须符合 `kubelab.io/v1alpha1`，权威 JSON Schema 位于 `schemas/lab-v1alpha1.schema.json`。开发时参考现有实验，至少定义：

- 标题、分类、难度、预计用时和学习目标；
- 环境要求及可选 addon；
- 对学习者公开的任务与完成条件；
- 用于证明故障确实存在的 `initialChecks`；
- 修复后必须全部通过的 `successChecks`；
- 从低到高逐步解锁的提示；
- 可重复 reset 的声明式资源，以及只删除受控 Namespace 的清理配置。

验证必须观察资源状态或业务结果，不能只判断 Pod Phase。不要把 Secret 值、凭证或内部 expected/actual 写入公开描述。需要 HTTP 验证时，只能使用结构化的实验内 Service 或受控 Ingress 引用。

## 定义固定变体

`variant.yaml`必须符合`kubelab.io/v1alpha1 kind: LabVariant`，权威Schema位于`schemas/lab-variant-v1alpha1.schema.json`。目录决定父实验，ID只能与当前`variant-*`目录一致，不能引用其他实验。变体定义独立任务、Manifest、初始/成功检查、恰好三层提示，以及通过后才公开的关键证据、根因、修复和预防措施。

变体继承父实验的Namespace、requirements、cleanup和复盘问题，不复制业务状态。名称、说明和揭示字段在通过前不得出现在公开API、HTML、时间线、错误或Markdown中。对已有实验增加变体时，需要同时覆盖以下选择规则：首次基线、按序轮换、中断续练、全部完成后的最久未练，以及活动变体文件丢失时禁止回退。

稳定DNS场景只能使用`dns_resolution`，输入限于Service名和可选Pod名；不得提供hostname、命令或Shell。平台固定构造`<pod>.<service>.<namespace>.svc.cluster.local`并清理探针。

## 安全与可重复性

- 使用固定镜像标签，不使用 `latest`。
- 设置合理的 requests/limits，避免影响本机。
- 初始故障必须确定、可诊断且不会依赖互联网时序。
- 标准修复只修改完成任务所需字段，并能由受限 workspace 权限执行。
- reset 后必须重新满足全部初始检查，cleanup 后不得留下 Namespace、RBAC、PVC/PV 或临时 kubeconfig。
- 禁止提交真实 Secret、kubeconfig、令牌、证书、私钥、数据库、日志和本机绝对路径。

## 测试流程

日常作者验收优先使用五个命令；仓库级回归再运行pytest：

```bash
kubelab lab lint labs
kubelab lab test labs
kubelab lab inspect labs/lab-019-configuration-service-chain
uv run pytest
```

基线和每个变体都必须证明`initialChecks通过 → successChecks预检未通过 → 标准修复后通过 → reset恢复故障`。综合场景还必须证明修复第一个根因后第二个根因仍可观测。只有维护者在明确受信任的本机minikube中，才可启用真实实验集成测试；普通PR与CI不启用该变量。

新实验验收后还要构建 wheel 和 sdist，并运行 `scripts/verify_distribution.py`，确认安装产物能在源码仓库外加载完整目录。

可选真实入口为`kubelab lab test TARGET --integration --junit PATH`，默认关闭。它要求显式设置`KUBELAB_RUN_LAB_INTEGRATION=1`、WSL2 Ubuntu、本机Docker驱动minikube、未漂移的可信Context和四个固定缓存镜像。入口使用临时数据库和唯一`kubelab-author-*` Namespace，通过受限Solution Applier应用声明式修复，并审计Namespace、Workspace RBAC、Probe、PVC/PV和临时Workspace；不得用于远程或生产集群。
