# LAB-001 Deployment扩缩容

观察Deployment、ReplicaSet和Pod的副本关系，把单副本web工作负载扩容到三个可用副本。

标准修复位于`solutions/fix.yaml`。验证同时检查Deployment可用副本数和三个Pod的稳定Ready状态，避免只修改期望值便判定成功。
