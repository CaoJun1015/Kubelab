# DaemonSet Pod亲和性缺少锚点

无需创建额外Pod。标准修复只清除错误的required `podAffinity`。

Server-Side Apply不能可靠删除由初始环境管理器持有的嵌套亲和性字段。参考修复先执行`kubectl delete daemonset node-agent`，再应用`solutions/fix.yaml`声明式重建DaemonSet；不要创建伪造锚点Pod。
