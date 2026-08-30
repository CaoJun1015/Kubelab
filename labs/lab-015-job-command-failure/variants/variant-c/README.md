# Job临时写入路径不可用

先删除不可变Job，再用`emptyDir`为`/work`提供受限临时空间。不要关闭`readOnlyRootFilesystem`。

