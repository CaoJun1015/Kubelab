# 为 KubeLab 做贡献

感谢你帮助改进 KubeLab。项目当前只支持在 Windows 11 + WSL2 Ubuntu 中运行；Windows 原生环境用于编辑和无集群测试。提交代码前请先阅读[安全策略](SECURITY.md)和[实验开发指南](docs/LAB_DEVELOPMENT.md)。

## 开始之前

1. 在 GitHub Issue 中确认问题尚未被处理。新实验请使用“实验提案”表单。
2. 从最新 `main` 创建短生命周期分支，不要在提交中加入数据库、日志、kubeconfig、令牌或本机路径。
3. 使用 Python 3.11 和项目锁定的依赖：

```bash
uv python install 3.11
uv sync --locked --dev
```

## 提交排障案例

一个可接受的案例必须能稳定复现一个明确的 Kubernetes 故障，并形成“观察现象 → 收集证据 → 最小修复 → 自动验证 → reset 后重现”的闭环。不要提交依赖公网、随机时序、人工判定或生产集群的案例。

### 1. 先确定案例类型

- `baseline`：独立实验，适合一个主要根因。
- `variant`：现有实验族的固定复练场景，必须放在父实验的 `variants/variant-*` 下。
- `composite`：两个连续根因；第一次修复后必须仍能观察到第二个根因。

新增内置实验的编号、主题和分类先通过实验提案确认，避免与现有21个实验重复。独立分发的第三方实验使用自己的稳定 `publisherId`；向主仓库投稿时，不要自行冒用 `kubelab` 发布者身份。

### 2. 用作者工具生成目录

不要复制旧实验后大范围改名。使用安全模板开始：

```bash
uv run kubelab lab init labs/lab-022-example \
  --type baseline \
  --id lab-022-example \
  --title "示例故障" \
  --category troubleshooting \
  --difficulty intermediate \
  --description "练习通过证据定位并修复一个可重复故障"
```

基线和综合实验必须包含：

```text
lab.yaml
package.yaml
authoring.yaml
README.md
manifests/*.yaml
solutions/fix.yaml
```

综合实验还需要 `solutions/fix-stage-1.yaml`。变体使用 `variant.yaml`，并继承父实验的 `package.yaml`、Namespace、环境要求、cleanup和复盘问题。

### 3. 内容要求

- `lab.yaml`或`variant.yaml`必须通过对应的 `kubelab.io/v1alpha1` Schema。
- README使用“是什么、为什么、怎么做”说明知识点，但不能把盲练变体答案提前公开。
- 故障环境必须确定、可观察、可reset；不能只依赖Pod Phase判断成功。
- `initialChecks`证明故障已经正确注入；`successChecks`证明业务结果真正恢复。
- 提供恰好三层提示：观察方向、可在受限Workspace复制执行的建议命令、故障方向。
- 基线或综合实验提供三个复盘问题；修复应只改变导致故障的必要字段。
- 镜像必须固定版本，不能使用 `latest`；工作负载必须设置合理的requests和limits。
- `authoring.yaml`只声明最小Fake观测和允许修复的JSON Pointer。修复操作只能是`modify`、`create`或必要资源的`recreate`。
- `package.yaml`中的Lab ID必须与实验一致，版本使用无构建元数据的SemVer，`requiresKubelab`使用PEP 440范围。

详细字段以[实验开发指南](docs/LAB_DEVELOPMENT.md)和仓库`schemas/`目录为准，不以其他实验中的偶然写法为准。

### 4. 安全要求

- 所有资源必须显式位于该实验的 `kubelab-*` Namespace。
- 禁止Namespace、Node、ClusterRole、ClusterRoleBinding、CRD、hostPath和其他集群级资源。
- 禁止任意Shell、脚本、插件、用户提供的URL或外部下载。
- 禁止真实Secret、Token、私钥、证书、kubeconfig、完整日志、异常堆栈和本机绝对路径。
- HTTP和DNS检查只能使用KubeLab已有的结构化验证器，不得实现任意网络探测。
- reset和cleanup只能操作有明确KubeLab所有权的资源；标准修复必须能由受限Workspace权限完成。

### 5. 本地验收

先运行不访问数据库和集群的作者检查：

```bash
uv run kubelab lab lint labs/lab-022-example
uv run kubelab lab test labs/lab-022-example
uv run kubelab lab inspect labs/lab-022-example
uv run kubelab lab package labs/lab-022-example
```

必须满足：初始检查通过、成功检查预检失败、标准修复后成功检查通过、reset后恢复初始故障。综合实验还必须证明第一阶段修复后第二个根因仍然存在。生成的 `.kubelab-lab.tar.gz` 只用于本地验证，不要提交到Git。

真实minikube测试默认关闭。只有维护者明确授权，并确认WSL2 Ubuntu、本机Docker驱动minikube和可信Context后，才能运行 `--integration`；禁止连接远程或生产集群。

### 6. PR验收清单

- [ ] 实验提案已确认，没有重复现有知识点。
- [ ] 故障、证据、根因和最小修复关系清晰。
- [ ] `lab lint`、`lab test`和`lab inspect`通过。
- [ ] 没有Secret、宿主机路径、危险资源或盲练答案泄漏。
- [ ] reset可重复，cleanup不会留下Namespace、RBAC、Probe、PVC/PV或临时Workspace。
- [ ] PR只包含该实验及必要测试/文档，并说明是否运行过真实集群测试。

## 本地质量门

普通测试不会访问 Kubernetes。提交前在 Windows 和 WSL 的独立虚拟环境中运行：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
node --check src/kubelab/static/app.js
git diff --check
uv build
uv run python scripts/verify_distribution.py \
  --wheel dist/kubelab-0.1.0-py3-none-any.whl \
  --sdist dist/kubelab-0.1.0.tar.gz \
  --version 0.1.0
```

覆盖率必须不低于 90%。Windows 与 WSL 不得共享虚拟环境；真实 minikube 集成测试保持关闭，除非维护者明确安排本地验收。

## 代码和架构约束

- CLI 与 Web 必须复用 Application Service，不得在 Web 中启动 CLI 子进程。
- Web 路由不得直接使用 ORM Session 或 Kubernetes Client。
- 不增加任意 Shell、命令、路径或 URL 输入。
- Kubernetes 写操作必须经过 Context 信任、Namespace 作用域和所有权校验。
- 公共输出不得包含 Secret、凭证、完整 Manifest、验证 expected/actual 或异常堆栈。
- 新行为必须有 Fake 测试；默认测试不得依赖 minikube。

## Pull Request

PR 请保持范围单一，说明风险、验证结果和是否访问过本地集群。维护者会使用 merge commit 保留里程碑历史；不要提交构建产物。对安全问题请勿创建公开 Issue，按[安全策略](SECURITY.md)私下报告。
