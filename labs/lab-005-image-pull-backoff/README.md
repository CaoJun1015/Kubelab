# LAB-005 ImagePullBackOff

## 练习目标

学会从Pod状态和Events区分`ErrImagePull`与`ImagePullBackOff`，确认问题来自镜像引用，并在修改Deployment后验证新Pod稳定Ready。

## 初始现象

- Deployment `web`已经创建；
- Pod保持`Pending`且容器未Ready；
- 容器waiting reason会出现`ErrImagePull`或`ImagePullBackOff`；
- 初始镜像使用保留域名`registry.invalid`，因此不依赖某个真实镜像仓库长期保留错误标签。

## 建议排查

```bash
kubectl -n kubelab-image-pull-backoff get deployment,pod
kubectl -n kubelab-image-pull-backoff describe pod
kubectl -n kubelab-image-pull-backoff get events --sort-by=.lastTimestamp
kubectl -n kubelab-image-pull-backoff get deployment web -o yaml
```

你需要直接修改实验Namespace中的Deployment。平台只验证最终状态，不执行README中的命令。

## 完成条件

- `web`容器镜像精确为`nginx:1.27-alpine`；
- 至少一个匹配Pod为`Running`和Ready；
- Ready状态连续保持5秒。

## 维护者说明

声明式标准修复位于`solutions/fix.yaml`，仅供自动化契约测试和维护者核对，不会由Registry自动应用。真实环境无法拉取固定镜像时应报告环境错误，而不是把实验判为失败。

## 面试复盘

1. `ErrImagePull`和`ImagePullBackOff`的关系是什么？
2. 如何从Events判断是镜像不存在、Registry认证失败还是网络故障？
3. 为什么只检查Deployment中的镜像字段不足以证明故障已经恢复？
