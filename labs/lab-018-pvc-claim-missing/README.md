# LAB-018 PVC依赖缺失

`consumer` Deployment引用名为`app-data`的PVC，但初始环境没有创建该对象。Pod会持续Pending，Events会指出未找到声明，而不是StorageClass绑定失败。

标准修复只创建一个使用`standard` StorageClass、`ReadWriteOnce`和128Mi容量的PVC。实验级readiness会在默认StorageClass不可用时阻止启动。
