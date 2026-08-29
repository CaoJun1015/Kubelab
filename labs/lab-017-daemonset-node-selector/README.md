# LAB-017 DaemonSet节点选择错误

`node-agent` DaemonSet声明了一个本地minikube中不存在的hostname，因此控制器没有符合条件的节点，也不会创建Pod。学习者只能修改命名空间内的DaemonSet，不能读取或修改Node。

标准修复显式清空错误的`nodeSelector`，其余Pod模板保持不变。成功验证使用稳定实验标签确认至少一个Pod Running且Ready。
