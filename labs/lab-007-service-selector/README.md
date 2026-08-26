# LAB-007 Service Selector错误

## 练习目标

建立从Deployment、Pod Labels、Service Selector、EndpointSlice到集群内HTTP访问的完整排障链路。

## 初始现象

- Deployment `web`拥有可用副本；
- Pod正常Running和Ready；
- Service `web`存在，但Selector与Pod Label不匹配；
- Ready Endpoint数量为0，Service无法把流量转发给后端。

## 建议排查

```bash
kubectl -n kubelab-service-selector get deployment,pod,service
kubectl -n kubelab-service-selector get pod --show-labels
kubectl -n kubelab-service-selector describe service web
kubectl -n kubelab-service-selector get endpointslice -l kubernetes.io/service-name=web
```

任务要求保留Pod标签并修复Service Selector，以便练习定位Service发现链路。

## 完成条件

- Service至少拥有一个Ready Endpoint；
- KubeLab从实验Namespace中的临时curl Pod访问Service得到HTTP 200；
- Probe Pod在成功、失败或超时后均被清理。

## 维护者说明

声明式标准修复位于`solutions/fix.yaml`，只更新Service Selector。HTTP检查使用结构化Service目标，不接受外部URL。

## 面试复盘

1. Service Controller如何根据Selector生成EndpointSlice？
2. Pod Labels匹配但Pod未Ready时，Endpoint会有什么变化？
3. 为什么Deployment Available和Service可访问是两个不同的验证层次？
