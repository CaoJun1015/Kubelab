# KubeLab 实验开发指南

KubeLab 实验是声明式、安全扫描且可重复验证的故障场景。运行时只读取 `lab.yaml` 和 `manifests/`；`solutions/fix.yaml` 只用于测试、维护者验收和文档，不会自动替学习者修复实验。

## 目录约定

```text
labs/lab-NNN-short-name/
├── lab.yaml
├── README.md
├── manifests/
│   └── resources.yaml
├── solutions/
│   └── fix.yaml
└── variants/                  # 可选；仅固定复练场景
    └── variant-b/
        ├── variant.yaml
        ├── README.md
        ├── manifests/
        │   └── resources.yaml
        └── solutions/
            └── fix.yaml
```

实验 ID、目录名和 Namespace 后缀应稳定、可读且唯一。Manifest 必须显式写入实验 Namespace，不能包含 Namespace、Node、ClusterRole、ClusterRoleBinding、CRD 等集群级资源，也不能引用宿主机路径、任意 URL 或外部命令。

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

先运行无需集群的 Schema、Registry 和 Fake Gateway 契约测试：

```bash
uv run pytest tests/test_first_labs.py
uv run pytest
```

基线和每个变体都必须证明`initialChecks通过 → successChecks预检未通过 → 标准修复后通过 → reset恢复故障`。综合场景还必须证明修复第一个根因后第二个根因仍可观测。只有维护者在明确受信任的本机minikube中，才可启用真实实验集成测试；普通PR与CI不启用该变量。

新实验验收后还要构建 wheel 和 sdist，并运行 `scripts/verify_distribution.py`，确认安装产物能在源码仓库外加载完整目录。
