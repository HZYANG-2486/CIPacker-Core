# CIPacker

> 纯 Python 实现的 **ClassIsland 配置迁移与体检工具**。
> 确定性打包 · 分层迁移 · 智能体检 · 插件依赖闭包。

CIPacker 把 ClassIsland 的配置迁移从「凭感觉复制一堆文件夹」升级为
**可复现、可校验、可回滚**的工程化流程。

一次典型迁移：

```bash
cipacker pack D:\ClassIsland -o backup.zip   # 打包（输出可复现 + 内容指纹）
cipacker verify backup.zip --deep           # 校验（篡改 / 损坏即可发现）
cipacker unpack backup.zip -d E:\ClassIsland  # 解包（自动备份，可回滚）
```

## 四大特色能力

### 1. 确定性打包 + 包指纹

同一份配置，在任何机器、任何时刻打包，**输出字节完全一致**。

* 固定时间戳 `1980-01-01`（zip 格式下限）、条目排序、固定权限位与压缩级别
* 绕开「指纹自引用悖论」：指纹基于**内容清单**（`path\0size\0sha256`）而非归档字节，
  因此与打包时刻无关；归档整体哈希另存为 `.sha256` 旁文件

```bash
$ cipacker pack D:\ClassIsland -o a.zip    # 内容指纹: 23e2d835…
$ sleep 5 && cipacker pack D:\ClassIsland -o b.zip
$ cmp a.zip b.zip && echo "字节一致"
```

需要保留真实修改时间时加 `--preserve-mtime`（此时放弃字节级可复现性）。

### 2. 分层 / 选择性迁移

迁移层之间有真实依赖关系，CIPacker 会**在打包前就提示**：

| 层 (`--layers`) | 内容 | 依赖 |
|---|---|---|
| `settings` | 应用全局设置（`Settings.json`） | — |
| `profiles` | 课表、时间表、科目等档案（`Profiles/`） | — |
| `plugins` | 插件本体与启用状态（`Plugins/`） | — |
| `plugin-configs` | 各插件自身的设置（`Config/Plugins/`） | `plugins` |
| `automation` | 自动化规则（`Config/Automation/`） | `profiles` |

```bash
# 只带走课表与应用设置，包体积从几十兆降到几 KB
cipacker pack D:\ClassIsland -o slim.zip --layers settings,profiles

# 只选了「插件设置」却没选「插件」——会给出软依赖警告
cipacker pack D:\ClassIsland -o part.zip --layers plugin-configs
# ⚠ 警告: 已选择「插件设置」但未选择其依赖的「插件」——目标环境可能需要额外处理
```

### 3. 智能体检 + 精准脱敏

`doctor` 在打包前/解包前发现 12 类问题，并可**就地脱敏**：

```bash
cipacker doctor D:\ClassIsland              # 纯报告
cipacker doctor D:\ClassIsland --strict     # 有问题时以非零码退出（CI 门禁）
cipacker doctor D:\ClassIsland --redact     # 就地掩码口令/令牌
cipacker pack D:\ClassIsland --doctor --redact   # 打包前体检 + 脱敏写入包内
```

体检规则涵盖：JSON 损坏、绝对路径泄漏、口令/令牌暴露、插件 manifest 非法、
插件目录名与 `id` 不一致、孤儿插件配置（有设置无插件）、缺失依赖、大文件等。

> 脱敏是**外科手术式**的：只替换敏感字段的值，保留 JSON 结构与键集合，
> 且报告本身**不回显明文**。
>
> **`pack --redact` 绝不改动源目录** —— 它在临时副本上脱敏后打包装入。
> 只有显式的 `doctor --redact` 才会就地修改，且写入是原子的
> （先写 `.ciptmp` 再替换），中断不会把配置截断。

### 安全边界

迁移包可能来自他人，因此解包路径按「不可信输入」处理：

| 威胁 | 处置 |
|------|------|
| **Zip Slip**（`../`、绝对路径、盘符、NUL） | `build_plan` 阶段拦截，记入 `skipped` 并展示原因 |
| **符号链接逃逸** | 打包时跳过符号链接（防止把 `/etc/shadow` 打进包）；解包时若落点是符号链接先解除 |
| **zip 内符号链接条目** | 一律落为普通文件，不还原为链接 |
| **路径重复静默覆盖** | 检测同名条目并跳过，避免内容与清单不符 |
| **半截文件** | 所有写入先落 `.ciptmp` 再原子替换，失败即清理 |

### 4. 插件依赖闭包

插件之间通过 `manifest.yml` 的 `dependencies` 互相引用。CIPacker 会：

* 解析完整依赖图，检出**缺失的必装依赖**
* 交叉比对包内插件与目标安装的**版本差异**，迁移前预警
* 识别 `.disabled`（禁用）/ `.uninstall`（待卸载）状态标记并原样迁移

```bash
$ cipacker doctor D:\ClassIsland
✗ 插件 'X' 依赖的必装插件 'Y' 未安装  (Plugins/X/manifest.yml)
    → 请安装插件 Y 后重试
```

## 完整命令参考

### pack — 打包配置

```
cipacker pack <目录> [-o 输出文件] [选项]
```

| 选项 | 说明 |
|------|------|
| `-o, --output` | 输出包路径（默认 `ci_config.zip`） |
| `--layers <列表>` | 迁移层，逗号分隔；`all` 表示全部（默认） |
| `--include-backups` | 包含 `Backups/` 目录 |
| `--doctor` | 打包前先执行体检；**发现 error 时中止打包** |
| `--strict` | 与 `--doctor` 联用：警告也视为错误并中止 |
| `--redact` | 脱敏敏感字段（写入包内，不改动源目录） |
| `--json-report <路径>` | 把体检报告写入 JSON 文件（**不阻断打包**，纯副作用开关） |
| `--no-manifest` | 不写包清单（导出为官方风格） |
| `--cidata` | 使用 `.cidata` 扩展名 |
| `--preserve-mtime` | 保留真实修改时间（放弃字节级可复现性） |
| `--structured` | 结构化事件流（见 [GUI 集成文档](GUI_INTEGRATION.md)） |

### unpack — 解包配置

```
cipacker unpack <zip文件> -d <目标目录> [选项]
```

| 选项 | 说明 |
|------|------|
| `-d, --dir` | 目标 ClassIsland 目录（必填） |
| `-y, --yes` | 跳过所有确认 |
| `--dry-run` | 只展示计划，不写入任何文件 |
| `--force` | 目标非有效 CI 安装时也强制解包 |
| `--no-backup` | 不备份将被覆盖的文件 |
| `--ci-version` | 缺失 CI 时下载的版本 |
| `--source` | 指定下载数据源 |
| `--structured` | 结构化事件流 |

> **比官方更安全的一点**：解包覆盖已有文件前会先备份到
> `Backups/PreCIPacker_<时间戳>/`，因此解包**总是可回滚**的。
> 此外内置 **Zip Slip 防护**：拒绝绝对路径、盘符、`..` 穿越与 NUL 字节。

### verify — 校验完整性

```
cipacker verify <zip文件> [--deep] [--json] [--structured]
```

默认逐文件比对内容哈希与内容指纹；`--deep` 额外比对归档整体哈希。
能区分「文件被改」「文件被删」「文件被加」与「指纹不符」四类问题。

### doctor — 体检

```
cipacker doctor <目录或包> [--strict] [--redact] [--json] [--structured]
```

| 选项 | 说明 |
|------|------|
| `--strict` | 有问题时以非零码退出 |
| `--redact` | 就地脱敏检出的敏感字段（**这是显式就地改写**，与 `pack --redact` 不同） |
| `--no-credentials` / `--no-paths` / `--no-plugins` | 跳过对应检查 |
| `--target-version` | 用于插件 API 兼容性对比的目标 CI 版本 |
| `--json` | 以 JSON 输出报告 |

> **退出码语义**：`doctor` 默认是**纯报告**模式，即便发现 error 也返回 `0`，便于在脚本中
> 无条件调用；只有加 `--strict` 才把「有 error 或 warning」升级为退出码 `1`。
> `pack --doctor` 则相反——它作为**打包前置门禁**，默认遇 error 即中止（`--strict` 再叠加 warning）。
> 两者的差异是刻意的：前者是「报告」，后者是「门禁」。

### list — 查看包内容

```
cipacker list <zip文件> [--json] [--structured]
```

### download — 下载 ClassIsland 本体

```
cipacker download -d <目录> [选项]
```

| 选项 | 说明 |
|------|------|
| `--list-versions` | 仅列出可用版本（无需 `-d`） |
| `--ci-version` | 指定版本（如 `2.1.0.1`） |
| `--ci1x` / `--ci2x` | 强制 1.x / 2.x 数据源（互斥） |
| `--source` | 指定数据源 |
| `--spoof-ua` | 使用浏览器 User-Agent |
| `-y, --yes` | 跳过确认 |

## 安装

```bash
# 方式一：从源码安装（推荐）
pip install -e .

# 方式二：仅装依赖后直接用模块
pip install -r requirements.txt
python -m cipacker pack D:\ClassIsland -o backup.zip
```

核心运行时**仅依赖 `tqdm`**。`PyYAML` 为可选增强：

```bash
pip install -e ".[yaml]"   # 插件 manifest 解析走 C 实现，更快更稳
```

> 不装 PyYAML 也完全可用——内置了零依赖的 YAML 子集解析器，
> 支持列表、嵌套映射与块标量，足以覆盖 `manifest.yml` 的全部字段。

## CI 1.x 与 2.x 支持

| 特性 | CI 1.x | CI 2.x |
|------|--------|--------|
| 目录结构 | `Settings.json` 在根目录 | `Settings.json` 在 `data/` 下 |
| 程序目录 | 单文件 `ClassIsland.exe` | `app-<version>-<n>/` 版本化目录 |
| 数据根 | 安装根 | **`data/`**（`app-*` 目录会被自动排除） |
| 下载源 | disturb / disturb-net6 / GitHub | Distribution API / GitHub |

工具会自动识别结构；若把路径直接指到 `data/`，也能正确解析。
`Cache/`、`Logs/`、`Temp/`、`Config/PluginsIndex/` 与程序二进制**永不迁移**。

## 下载源

| 源 | 适用版本 | 说明 |
|----|----------|------|
| Distribution API | CI 2.x | 官方分发 API，支持 SHA512 校验 |
| GitHub Releases | 1.x / 2.x | 全版本覆盖 |
| disturb | CI 1.x (1.5.0.4 ~ 1.7.0.1) | 社区分发源 |
| disturb-net6 | CI 1.x (1.5.0.4 ~ 1.6.0.5) | .NET 6 版本分发源 |

支持断点续传（`.part` + `Range`）、重试退避、ZIP 头校验与哈希校验。

## 项目结构

```
cipacker/
├── cli.py          # 命令行入口（argparse）
├── commands.py     # 各子命令的业务逻辑
├── layout.py       # ★ 布局抽象：CI 1.x / 2.x 结构识别
├── layers.py       # ★ 迁移层定义与选择校验
├── pack.py         # ★ 确定性 zip 写入器
├── fingerprint.py  # ★ 内容指纹
├── unpack.py       # 解包计划与执行（含备份）
├── verify.py       # 完整性校验
├── doctor.py       # ★ 体检规则引擎
├── plugins.py      # 插件 manifest / 依赖闭包
├── yamlish.py      # 零依赖 YAML 子集解析器
├── archive.py      # 包格式识别 + Zip Slip 防护
├── structured.py   # ★ GUI 事件流契约（唯一实现点）
├── download/       # 多源下载（transfer / github / pdc / disturb）
└── install/        # CI 运行中检测
```

## 测试

```bash
python run_tests.py            # 全部（110 个用例）
python run_tests.py -v         # 详细输出
python run_tests.py determinism  # 按名称过滤
```

覆盖：布局探测、文件收集与排除、YAML 解析、插件依赖闭包、确定性打包、
指纹一致性、分层选择、pack→unpack→pack 逐字节往返、Zip Slip 防护、
篡改检测、备份回滚、体检 12 规则、脱敏、下载（含续传与哈希校验）、
以及 `--structured` 契约（每命令恰好一次 `result=`）。

## GUI 集成

CIPacker 提供 `--structured` 模式，向 stderr 输出机器可读事件流，
便于包装为图形界面应用。**事件格式向后兼容旧版**，并新增
`stage=verify` / `stage=doctor` / `level=finding` 三类事件。
详见 **[GUI 集成文档](GUI_INTEGRATION.md)**。

## 许可证

MIT License

---

HZYANG ~~(+AI)~~ 2026
