# 旧版实现存档

`CiPack_v2.py` 是 v2.x 的单文件实现（约 2100 行），仅作**历史参考**保留，
不再维护、不参与构建，也不被任何代码引用。

v3.0 已将其重构为 `cipacker/` 多模块包。若需要对照旧行为，可参阅此文件，
但请注意其中已知的缺陷（v3 已全部修复）：

1. `parse_yaml_simple` 无法解析列表与嵌套结构 —— 导致插件 `dependencies`
   与 `supportedOSPlatforms` 永远解析为空。
2. 未处理 `.disabled` / `.uninstall` 插件状态标记。
3. 使用 `ZipFile.write()`，携带磁盘 mtime，打包结果不可复现。
4. `except SystemExit` 分支先 `emit("result=…")` 再重新抛出，
   会导致 **`result=` 事件重复**，破坏 GUI 外壳契约。
5. `--official-format` 混淆了「不写清单」与「去掉 data/ 前缀」两种行为。
6. 误将 `.gz` 排除；V1 布局误收集 `ClassIsland.exe`。
7. 三处重复的发行版获取逻辑。

需要旧版时请用：`python legacy/CiPack_v2.py <子命令> ...`
