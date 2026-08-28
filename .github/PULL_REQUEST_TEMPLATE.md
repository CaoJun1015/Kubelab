## 变更内容

<!-- 说明问题、方案及为何属于本 PR 范围。 -->

## 安全与架构

- [ ] CLI 与 Web 继续复用 Application Service；Web 未直接访问 ORM 或 Kubernetes Client。
- [ ] 未增加任意命令、Shell、路径、URL 输入或扩大 workspace 权限。
- [ ] 公共输出不含 Secret、凭证、完整 Manifest、验证内部值或异常堆栈。
- [ ] 提交不含数据库、日志、缓存、构建产物或本机路径。

## 验证

- [ ] Windows pytest（覆盖率 ≥90%）、Ruff、格式、strict mypy、JavaScript 语法和 `git diff --check` 通过。
- [ ] WSL2 Ubuntu 使用独立环境完成相同质量门。
- [ ] wheel 与 sdist 已通过统一产物检查。
- [ ] 新行为有 Fake 测试，默认测试没有访问 minikube。

## 集群影响

<!-- 说明是否执行过本机 minikube 验收、创建了哪些临时资源以及清理结果。禁止使用远程或生产集群。 -->
