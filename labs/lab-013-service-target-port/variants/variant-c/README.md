# Service暴露端口契约错误

调查Service公开端口与调用约定。标准修复保留Selector和`targetPort`，仅把公开端口恢复为`port: 80`。

由于Kubernetes Server-Side Apply把Service端口值作为列表键，直接从`8080`改成`80`会保留旧列表项。参考修复需要先执行`kubectl delete service web`，再应用`solutions/fix.yaml`声明式重建Service；Deployment和Pod不需要删除。
