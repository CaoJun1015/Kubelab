# KubeLab M9 双平台验收记录

日期：2026-09-03
分支：`codex/m9-trusted-packages`
版本：`0.6.0a0`
冻结代码提交：`fbbf9a0`

## 验收范围

- LabPackage format v2、离线归档校验和21份内置包契约。
- 本地包暂存、启用、停用、版本切换、回滚、延期移除与失败重试。
- 内置实验保护、发布者锁定、Session包摘要钉住、公开快照与历史保留。
- 只读包API、Web来源展示、发布者未验证提示和公开DTO脱敏。
- Windows与WSL的测试、静态检查、发行构建、内容校验和安装烟测。

真实外部包集成入口已实现并由`KUBELAB_RUN_PACKAGE_INTEGRATION=1`门禁。本次变量保持关闭，没有执行真实实验start、repair、reset或cleanup，也没有修改集群资源。

## 自动化质量门

| 环境 | Python | pytest | 覆盖率 | Ruff | format | strict mypy | JavaScript | diff check |
|---|---:|---:|---:|---|---|---|---|---|
| Windows | 3.11.0 | 630 passed，39 skipped | 90.04% | 通过 | 通过 | 通过 | 通过 | 通过 |
| WSL2 Ubuntu | 3.11.16 | 633 passed，36 skipped | 90.15% | 通过 | 通过 | 通过 | 通过 | 通过 |

跳过项均为显式关闭的真实集成测试，或Windows账户不支持符号链接的安全测试分支；WSL已覆盖对应符号链接边界。

## 发行产物

Windows与WSL分别构建`0.6.0a0` wheel和sdist，统一内容校验均通过：

- 21个实验族；
- 12个固定变体；
- 33份作者契约；
- 21份包契约；
- 14个Web资源；
- `0001`至`0004`迁移及新增LabPackage Schema。

LAB-013在Windows与WSL生成的作者包SHA-256一致：

```text
f8e824958f5d9464200d5b1c91de35bf82c4974e5178fad36a3b930ea4be5855
```

## 安装烟测

- WSL wheel与sdist均在独立HOME、配置、状态、工具和缓存目录中安装。
- 两种产物均报告`KubeLab 0.6.0a0`、21个实验族、12个变体和33个场景。
- 安装后的`lab init/lint/test/inspect/package`与`package verify/import/enable/list/show/disable/remove`全部通过。
- Doctor在隔离环境中返回`unhealthy`（退出码3），因此Context信任按设计记录为`skipped-not-ready`；未自动修改用户信任状态。
- loopback Web在`127.0.0.1:8765`完成Dashboard、实验目录、包页面及静态资源烟测，并停止干净。
- Windows wheel从隔离目标目录导入，版本、21个内置实验、五个作者命令和离线`package verify`均通过；库存写命令仍按设计拒绝非WSL环境。

## 安全结论

- 归档与解压后的`content/`均按索引复验文件集合、大小和SHA-256。
- 5 MiB压缩包、4 MiB索引内容、257个成员、512 KiB单文件、256 MiB实际存储及128个累计登记版本上限均有自动化覆盖。
- 包清单与Web GET保持纯读取；cleanup完成后在Application Service锁外尝试回收待移除包，失败保留`pending_removal`并在后续写操作重试。
- 包内容缺失或篡改时拒绝新启动及hint/verify/reset，cleanup继续依赖已保存的Session与Namespace所有权信息。
- 没有保存Token、kubeconfig、用户绝对路径、完整Manifest、Secret、内部验证值或异常堆栈。

## 结论

M9 `0.6.0a0`的本地功能、双平台自动化门禁与发行产物验收完成。当前仅形成本地开发分支与本地提交；未推送、未合并、未创建标签或Release。
