# LAB-004 探针基础

错误Liveness路径会让本来能够提供服务的nginx反复重启。修复后同时配置正确的Liveness和Readiness路径。

验证要求新Pod稳定Ready且重启数为零，避免把短暂Running误判为恢复。
