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
