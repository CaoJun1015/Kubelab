# 示例复盘：Deployment 副本不足

> 这是脱敏的演示内容。实验名称、Namespace 和观察结果均为训练数据，不包含凭证、请求内部值或开发机路径。

## 现象

应用只有一个可用 Pod，但任务要求三个副本。Service 本身可发现后端，事件中没有调度或镜像错误。

## 调查路径

1. 查看 Deployment 与 ReplicaSet，确认期望副本数为 1。
2. 查看 Pod 状态和近期 Events，排除镜像、探针和资源不足。
3. 对照任务完成条件，将问题收敛为 Deployment 副本配置不足。

## 根因

Deployment 的 `spec.replicas` 被设置为 1，与目标容量 3 不一致。

## 修复与验证

在 Session 专属受限 workspace 中将 Deployment 扩容到三个副本。等待三个 Pod Ready 后回到 Web 点击验证，所有公开检查均通过。

## 后续改进

- 为关键工作负载建立期望副本和可用副本监控。
- 变更后同时检查 rollout 状态与 Ready 数量。
- 复盘中记录判断依据，不记录 Secret、凭证或完整 Manifest。
