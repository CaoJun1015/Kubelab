# KubeLab M9 产品需求文档——可信本地实验包生命周期

> 文档版本：v1.0
> 文档状态：已批准实施
> 建议开发版本：`0.6.0a0`
> 前置条件：M8声明式作者工具链已完成
> 正式运行环境：WSL2 Ubuntu与本机Docker驱动minikube

## 1. 背景

M8能够生成经过Schema、安全、Fake生命周期和摘要验证的确定性
`.kubelab-lab.tar.gz`，但产物不能安装。运行时仍只读取随KubeLab发布的内置实验，
作者与学习者之间缺少可信的本地分发闭环。

M9提供离线、本地、显式的实验包生命周期。它验证内容完整性和KubeLab安全契约，
但不声称验证发布者身份。

## 2. 目标

- 定义带版本、发布者和KubeLab兼容范围的`LabPackage`契约。
- 支持本地包验证、暂存导入、显式启用、停用、版本切换、回滚和安全移除。
- 让新Session使用当前启用版本，同时让已有Session继续使用创建时的包摘要。
- 在包删除后保留脱敏的历史进度、场景揭示和复盘信息。
- 在CLI和Web中明确区分内置实验、本地包、完整性状态和未验证发布者。

## 3. 非目标

- 不从URL、GitHub Release、市场或其他远端下载包。
- 不实现发布者签名、公钥信任库或证书体系。
- 不提供Web上传、Web启停或Web卸载。
- 不允许外部包覆盖内置实验。
- 不允许包扩展M7学习路径、知识卡或症状索引。
- 不扩大Workspace RBAC，不连接远程或生产集群。

## 4. 包契约

每个实验族根目录必须包含`package.yaml`：

```yaml
apiVersion: kubelab.io/v1alpha1
kind: LabPackage
metadata:
  labId: lab-001-deployment-scaling
  version: 1.0.0
  publisherId: kubelab
  publisherName: KubeLab Project
spec:
  requiresKubelab: ">=0.6.0a0,<0.7.0"
```

`version`使用不含构建元数据的SemVer 2.0；`publisherId`使用KubeLab slug；
`requiresKubelab`使用PEP 440 Specifier。`labId`必须与`lab.yaml`一致。

作者包格式升级为v2，索引包含包元数据、场景、Schema版本以及所有文件的大小和
SHA-256。M8 v1包可以检查，但必须重新构建后才能导入。

## 5. 安全模型

- 只接受用户显式指定的本地普通文件，禁止符号链接。
- 禁止路径穿越、绝对路径、大小写冲突路径、链接、设备和FIFO成员。
- 压缩包不超过5 MiB，索引内容不超过4 MiB，最多257个成员，单文件不超过512 KiB。
- 总包存储不超过256 MiB，最多登记128个版本。
- 导入时重新执行Registry、安全扫描、authoring lint和完整Fake生命周期。
- 内容写入WSL原生状态目录，使用同目录临时路径和原子重命名。
- 不保存输入绝对路径、异常正文、完整Manifest、Secret或验证内部值。
- 完整性状态不等于发布者身份；第三方包始终显示“发布者未验证”。

## 6. 生命周期

包状态固定为`staged`、`enabled`、`disabled`、`pending_removal`和`removed`。

- import只产生staged版本。
- 同一Lab ID只允许一个enabled版本。
- 启用其他已安装版本构成升级或回滚。
- 内置Lab ID不可被外部包占用。
- 外部Lab ID首次导入后锁定publisherId。
- 同一digest重复导入幂等成功；相同Lab、发布者和版本的不同digest视为冲突。
- 活动Session按digest钉住，版本切换不影响它。
- 移除活动Session使用的版本时进入pending_removal，Session完成后再安全回收。
- 包缺失或被修改时，提示、验证和reset失败关闭；cleanup仍可执行。

## 7. 接口

CLI提供：

```text
kubelab package verify ARCHIVE
kubelab package import ARCHIVE
kubelab package list [--status STATUS]
kubelab package show LAB_ID [--version VERSION]
kubelab package enable LAB_ID --version VERSION
kubelab package disable LAB_ID
kubelab package remove LAB_ID --version VERSION [--yes]
```

所有命令支持`--json`。verify可在Windows和WSL执行；其余生命周期命令正式支持
WSL2 Ubuntu。remove在非交互或JSON模式必须显式使用`--yes`。

Web只新增：

- `GET /api/v1/packages`
- `GET /api/v1/packages/{lab_id}`

Lab、Session和Progress公开DTO增加来源、版本、发布者、发布者验证状态和可用状态。
页面只展示包清单与来源，不提供任何包写操作。

## 8. 数据与兼容

Alembic `0004_lab_packages`增加包、包事件和Session来源字段。旧Session回填为builtin。
外部Session保存脱敏实验及场景快照；活动操作读取钉住内容，完成后的历史读取快照。
迁移继续执行WAL checkpoint和升级前备份。

## 9. 验收

- 21个内置实验族、12个变体和33个场景保持可执行。
- 21份包契约与v2确定性产物通过Windows/WSL一致性检查。
- 完整覆盖导入、启停、多版本、回滚、Session钉住、待移除和历史快照。
- 恶意归档、危险Manifest、敏感内容和公开边界测试失败关闭。
- Windows与WSL的pytest覆盖率均不低于90%，并通过Ruff、format、strict mypy、
  JavaScript语法、`git diff --check`和wheel/sdist验证。
- 真实minikube验收保持显式关闭，除非另行授权。

