# CiPack - ClassIsland 配置打包工具

纯 Python 实现的 ClassIsland 配置迁移工具，支持 CI 1.x / 2.x 配置打包、解包、查看和 CI 本体下载。

一键迁移你的 ClassIsland 配置：打包当前配置 → 在另一台电脑上解包，缺失 CI 本体时自动下载对应版本。

## 功能特性

- **pack** — 打包 ClassIsland 用户配置（排除缓存、日志、临时文件），可选导出为CI2 `.cidata` 格式
- **unpack** — 将配置包解包到已有 CI 安装目录，自动检测版本结构（V1/V2），缺失 CI 时自动下载
- **list** — 查看配置包内容与打包信息
- **download** — 单独下载指定版本的 ClassIsland（支持 1.x / 2.x，多源测速选择）
- **版本感知** — 自动识别 CI 1.x / 2.x 目录结构，严格匹配版本，避免跨版本迁移损坏配置
- **多源下载** — 支持多个社区源，自动测速选最优
- **断点续传** — 下载中断后再次运行可从断点继续，无需重头下载
- **插件打包** — 自动包含 `Plugins/` 目录下所有插件，迁移后开箱即用

## 环境要求

- Python 3.10+
- [tqdm](https://github.com/tqdm/tqdm)（可选，用于下载进度条）

```bash
pip install -r requirements.txt
```

## 快速开始

```bash
# 打包配置
python CiPack.py pack D:\ClassIsland -o ci_config.zip

# 导出为官方 .cidata 格式（不含 __manifest__.json）
python CiPack.py pack D:\ClassIsland -o ci_config.zip --official-format

# 查看包内容
python CiPack.py list ci_config.zip

# 解包到已有 CI 目录（缺失 CI 时自动下载）
python CiPack.py unpack ci_config.zip -d D:\ClassIsland

# 单独下载 CI（交互式选择版本）
python CiPack.py download -d D:\ClassIsland

# 下载指定版本
python CiPack.py download -d D:\ClassIsland --ci-version 1.7.0.1
```

## 命令参考

### pack — 打包配置

```
python CiPack.py pack <目录> [-o 输出文件] [选项]
```

| 选项 | 说明 |
|------|------|
| `-o, --output` | 输出 zip 路径（默认 `ci_config.zip`） |
| `--include-backups` | 包含 `data/Backups` 目录 |
| `--official-format` | 导出为官方格式（不含 `__manifest__.json`，CI 2.x 去掉 `data/` 前缀） |
| `--structured` | 结构化输出,主要给GUI套壳提供（详见 [GUI 集成文档](GUI_INTEGRATION.md)） |

打包时自动检测 CI 版本和目录结构，排除 `Cache/`、`Logs/`、`Temp/`、`app-*` 等非配置目录，包含 `Plugins/` 下所有插件。

### unpack — 解包配置

```
python CiPack.py unpack <zip文件> -d <目标目录> [选项]
```

| 选项 | 说明 |
|------|------|
| `-d, --dir` | 目标 ClassIsland 根目录（必填） |
| `-y, --yes` | 跳过所有确认（覆盖配置、结构不兼容等） |
| `--yes-download` | 跳过下载确认并自动选择下载源 |
| `--spoof-ua` | 使用浏览器 User-Agent 下载 |
| `--ci-version` | 指定下载的 CI 版本（如 `1.7.0.1`） |
| `--structured` | 启用结构化输出（详见 [GUI 集成文档](GUI_INTEGRATION.md)） |

解包时进行以下安全检查：
- **版本结构兼容性**：避免错误的版本解包（可 `--yes` 强制跳过）
- **已有配置保护**：目标目录已有配置时提示覆盖
- **插件校验**：对比源包与目标安装的插件版本

### download — 下载 ClassIsland

```
python CiPack.py download -d <目录> [选项]
```

| 选项 | 说明 |
|------|------|
| `--ci-version` | 指定版本（如 `1.7.0.1` 或 `2.1.0.1`） |
| `--ci1x` / `--ci2x` | 强制下载 1.x / 2.x 版本（互斥） |
| `-y, --yes` | 跳过所有确认 |
| `--yes-download` | 跳过下载确认并自动选择下载源 |
| `--spoof-ua` | 使用浏览器 User-Agent 加速下载 |
| `--structured` | 启用结构化输出（详见 [GUI 集成文档](GUI_INTEGRATION.md)） |

未指定版本时，自动汇总所有可用版本（disturb 源 + distribution API + GitHub Releases）供用户选择。

### list — 查看包内容

```
python CiPack.py list <zip文件> [--structured]
```

## CI 1.x 与 2.x 支持

| 特性 | CI 1.x | CI 2.x |
|------|--------|--------|
| 目录结构 | `Config/` `Profiles/` `Settings.json` 在根目录 | `data/` 目录下 |
| 安装形态 | 通常为单文件 `ClassIsland.exe` | 文件夹结构 |
| 下载源 | disturb / disturb-net6 / GitHub | Distribution API / GitHub |
| 打包路径 | 根目录直接打包 | `data/` 前缀处理 |

工具会自动检测配置包和目标目录的版本结构，确保版本匹配。

## 下载源

| 源 | 适用版本 | 说明 |
|----|----------|------|
| Distribution API | CI 2.x | 官方分发 API，支持 SHA512 校验 ([Classisland下载站提供](https://get.classisland.tech/))|
| GitHub Releases | 1.x / 2.x | 全版本均有覆盖 |
| disturb | CI 1.x (1.5.0.4 ~ 1.7.0.1) | 社区分发源 ([Classisland下载站提供](https://get.classisland.tech/))|
| disturb-net6 | CI 1.x (1.5.0.4 ~ 1.6.0.5) | .NET 6 版本分发源 ([Classisland下载站提供](https://get.classisland.tech/))|

下载支持断点续传（`.part` 文件）、UA 欺骗（`--spoof-ua`）和文件完整性校验。

## GUI 集成

CiPack 提供 `--structured` 模式，向 stderr 输出机器可读的事件流，方便开发者包装为图形界面应用。详见 **[GUI 集成文档](GUI_INTEGRATION.md)**。

## 许可证

MIT License

---

HZYANG 2026
