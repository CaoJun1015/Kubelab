# LAB-012 PVC无法绑定

PVC指定不存在的StorageClass并保持Pending，依赖该卷的Pod无法调度。开始实验前必须由Doctor确认存在默认StorageClass；当前minikube环境的provisioner异常时不得执行真实实验。

`storageClassName`不可原地修改，标准操作是删除故障PVC后按`solutions/fix.yaml`重新创建。
