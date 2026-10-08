# 更新日志

本文件记录 CIPacker 的重要变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [Unreleased]

### 待办 / 计划中

- 支持自定义下载源地址（当前服务地址为模块级常量，需重启进程才能生效）
- 下载地址输出默认凭据脱敏

---

## [3.0.0] — 全面重构

v3.0 是一次**破坏性重构**：把 v2.x 的单文件实现（约 2100 行）拆分为模块化 Python 包，
并围绕「确定性、可校验、可回滚」重建了核心机制。

> 旧实现完整保留在 [`legacy/CiPack_v2.py`](legacy/CiPack_v2.py)，
> 仅供行为对照，不再维护、不参与构建。其已知缺陷见下方「修复」一节。

### 新增

**确定性打包**

- 打包输出**字节级可复现**：固定时间戳 `1980-01-01`、条目排序、固定权限位与压缩级别。
- 引入**内容指纹**机制，绕开「指纹自引用悖论」——指纹基于内容清单
  （`path\0size\0sha256` 排序后哈希）而非归档字节，因此与打包时刻无关；
  归档整体哈希另存为 `.sha256` 旁文件。
- 新增 `--preserve-mtime`：需要保留真实修改时间时启用（此时放弃字节级可复现性）。

**分层 / 选择性迁移**

- 新增 `--layers` 选项，支持按 `settings` / `profiles` / `plugins` /
  `plugin-configs` / `automation` 五个迁移层选择性打包。
- 层之间带**软依赖检查**：如只选「插件设置」而未选「插件」会在打包前给出警告。

**智能体检**

- 新增 `doctor` 子命令与 12 类体检规则：JSON 损坏、绝对路径泄漏、口令/令牌暴露、
  插件 manifest 非法、插件目录名与 `id` 不一致、孤儿插件配置、缺失依赖、大文件等。
- 新增**精准脱敏**：外科手术式替换敏感字段的值，保留 JSON 结构与键集合，
  且报告本身**不回显明文**。
- 新增 `--target-version`：针对目标 ClassIsland 版本做插件 API 兼容性对比。

**插件依赖闭包**

- 解析插件 `manifest.yml` 的 `dependencies` 字段，构建依赖闭包并检出缺失。
- 识别 `.disabled` / `.uninstall` 状态标记，正确反映插件启用状态。
- 插件 API 大版本不匹配时给出兼容性告警。

**CLI 与 GUI 集成**

- CLI 全面重设计：`pack` / `unpack` / `list` / `verify` / `doctor` / `download` 六个子命令。
- 新增 `--structured`：向 stderr 输出机器可读事件流，供 GUI 外壳集成。
- 新增统一退出码约定：`0` 成功 / `1` 失败 / `2` 用法错误 / `3` 前置条件不满足 / `130` 用户取消。
- 新增 `-d/--force/-y` 等选项，支持非交互与强制解包。

**下载与安装**

- 新增 `download` 子命令：从官方分发中心（PDC）/ GitHub Releases / Disturb 源获取
  ClassIsland，支持多源自动测速择优、断点续传与哈希校验。

**工程化**

- 拆分为 `cipacker` 多模块包，保留 `CiPack.py` 作为兼容瘦 shim。
- 建立 121 个测试的测试套件，覆盖确定性、布局识别、安全防护、GUI 契约等。
- 接入 `ruff` 与 `mypy`，均零告警。

### 修复

以下为 v2.x 中确认存在、v3.0 已修复的缺陷（详见 [`legacy/README.md`](legacy/README.md)）：

- **插件依赖永远解析为空** — `parse_yaml_simple` 无法解析列表与嵌套结构，
  导致 `dependencies` 与 `supportedOSPlatforms` 恒为空。
- **插件状态被忽略** — 未处理 `.disabled` / `.uninstall` 标记。
- **打包结果不可复现** — 使用 `ZipFile.write()` 携带磁盘 mtime。
- **`result=` 事件重复** — `except SystemExit` 分支先 `emit` 再重新抛出，
  破坏 GUI 外壳契约（违反「每次调用有且仅有一条 `result=`」）。
- **`--official-format` 语义混淆** — 把「不写清单」与「去掉 `data/` 前缀」两种行为混为一谈。
- **文件收集错误** — 误将 `.gz` 排除；V1 布局下误收集 `ClassIsland.exe`。
- **发行版获取逻辑三处重复** — 已统一。

### 安全

- **Zip Slip 防护**：拒绝 `..` 穿越、绝对路径、盘符、NUL 字节等恶意条目。
- **符号链接防护**：
  - 打包阶段跳过符号链接，避免把指向宿主机任意文件（如 `/etc/shadow`）的内容
    读入迁移包；
  - 解包阶段写入前先解除已存在的符号链接，防止穿透写入。
- **脱敏绝不污染源目录**：`pack --redact` 在临时副本上脱敏，
  源目录保持原样（此前实现会就地改写用户真实配置，属数据丢失级缺陷）。
- **原子写入**：解包与脱敏均先写 `.ciptmp` 再替换，中断不会截断文件；
  失败时清理临时文件。
- **重复成员检测**：包内路径重复时跳过并告警，避免静默覆盖。

### 变更

- 项目由单文件脚本转为可安装包，新增 `pyproject.toml`，支持 `pip install`。
- 命令行入口由 `python CiPack.py` 变为 `cipacker`（`CiPack.py` 保留兼容）。
- 运行依赖收敛为仅 `tqdm`；`PyYAML` 为可选增强，缺失时自动降级到内置 YAML 子集解析器。

---

## [2.x] — 单文件实现（已归档）

参见 [`legacy/README.md`](legacy/README.md) 与 [`legacy/CiPack_v2.py`](legacy/CiPack_v2.py)。

[Unreleased]: https://github.com/HZYANG-2486/CIPacker-Core/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/HZYANG-2486/CIPacker-Core/releases/tag/v3.0.0
