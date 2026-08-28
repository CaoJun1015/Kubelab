# 安全策略

## 支持范围

安全修复只面向最新的 GitHub Release。KubeLab 是本地训练工具，不应连接远程或生产 Kubernetes 集群；将其用于这些环境不属于支持范围。

## 私下报告漏洞

请通过 GitHub 仓库的 **Security → Advisories → Report a vulnerability** 创建 Private Security Advisory：

<https://github.com/CaoJun1015/Kubelab/security/advisories/new>

请提供受影响版本、复现步骤、实际影响和建议修复。不要在公开 Issue、讨论、日志或截图中披露漏洞细节、凭证、kubeconfig、Secret 或可识别的本机路径。维护者会在收到报告后尽快确认，并在评估完成后协调修复与披露。

## 安全边界

KubeLab 只信任用户明确确认的本机 minikube 身份。实验资源限制在平台创建且所有权完全匹配的 Namespace 中；受限 workspace 不允许读取 Secret、修改 RBAC 或访问集群级 Namespace。Web 固定监听 `127.0.0.1:8765`，使用同源、Origin、CSRF 和安全响应头保护。

如果怀疑凭证已经泄漏，请先撤销或轮换凭证，再提交私下报告。仓库不接受真实凭证作为复现材料。
