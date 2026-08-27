# LAB-010 OOMKilled

低内存上限与无界分配共同制造OOM终止。排障时需结合当前`CrashLoopBackOff`、Last State中的`OOMKilled`和资源声明判断根因。

标准修复移除无界分配并设置合理内存边界；验证要求新Pod零重启并稳定Ready。
