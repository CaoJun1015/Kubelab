# LAB-006 CrashLoopBackOff

## 练习目标

理解`CrashLoopBackOff`只是Kubelet对重复崩溃的退避表现，并使用容器状态、重启次数和previous日志定位实际退出原因。

## 初始现象

- Deployment `worker`已经创建；
- 容器启动后打印一行日志并以退出码1结束；
- Pod Phase通常仍是`Running`，但容器处于waiting且reason为`CrashLoopBackOff`；
- 重启次数至少为1。

## 建议排查

```bash
kubectl -n kubelab-crash-loop-backoff get pod
kubectl -n kubelab-crash-loop-backoff describe pod
kubectl -n kubelab-crash-loop-backoff logs deployment/worker
kubectl -n kubelab-crash-loop-backoff logs deployment/worker --previous
kubectl -n kubelab-crash-loop-backoff get deployment worker -o yaml
```

## 完成条件

- Deployment至少有一个可用副本；
- 修复后创建的Pod持续`Running`和Ready至少10秒；
- `worker`容器重启次数为0。

## 维护者说明

声明式标准修复位于`solutions/fix.yaml`。它保留`busybox:1.36.1`，只把会主动退出的命令改为持续运行循环，从而确保实验验证的是启动行为而不是换镜像绕过问题。

## 面试复盘

1. 为什么Pod Phase可能是`Running`，容器却显示`CrashLoopBackOff`？
2. `kubectl logs --previous`为什么对崩溃循环特别重要？
3. 应该用什么稳定性信号避免把一次短暂Running误判为修复成功？
