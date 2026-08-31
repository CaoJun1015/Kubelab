# DaemonSet节点亲和性无法满足

保持Node不变，标准修复仅清除Pod模板中的错误`nodeAffinity`。

Server-Side Apply不能可靠删除由初始环境管理器持有的嵌套亲和性字段。参考修复先执行`kubectl delete daemonset node-agent`，再应用`solutions/fix.yaml`声明式重建DaemonSet；不要修改Node标签。
