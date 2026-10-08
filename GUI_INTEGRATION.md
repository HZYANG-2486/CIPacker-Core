# CIPacker GUI 集成文档

本文档面向希望为 CIPacker 开发图形界面（GUI）外壳的开发者。

通过 `--structured` 参数，CIPacker 会向 **stderr** 输出机器可读的事件流，GUI 外壳可解析这些事件来渲染进度条、阶段提示、选项列表和交互对话框。

> **v3.0 兼容性承诺**：事件格式与旧版**完全向后兼容**。v3 只**追加**了新的
> `stage` 值与 `level=finding` 级别，未修改任何既有字段语义。
> 旧版外壳（忽略未知字段）无需改动即可继续工作。

## 启用方式

在任意子命令后添加 `--structured`：

```bash
cipacker pack D:\ClassIsland -o ci_config.zip --structured
cipacker unpack ci_config.zip -d D:\ClassIsland --structured
cipacker download -d D:\ClassIsland --structured
cipacker list ci_config.zip --structured
cipacker verify ci_config.zip --structured      # v3 新增
cipacker doctor D:\ClassIsland --structured     # v3 新增
```

> 旧版调用方式 `python CiPack.py <子命令> ... --structured` 仍然可用
> （`CiPack.py` 已改为指向 `cipacker.cli.main` 的瘦 shim）。

## 输出格式

所有结构化事件以 `[STRUCTURED]` 前缀输出到 **stderr**，每行一条，格式为空格分隔的 `key=value` 对：

```
[STRUCTURED] key1=value1 key2=value2 key3=value3
```

> stdout 仍输出人类可读的日志，stderr 专供结构化事件。两者互不干扰，可同时消费。

### ⚠️ 值的引号规则（v3 起强化）

**值中若包含空格、制表符、换行、引号或反斜杠，会被双引号包裹并转义。**

这一点非常重要：ClassIsland 是中文应用，绝大多数 `message` 都含空格或标点。
旧版直接拼接 `key=value`，导致按空格拆分的解析器会**丢弃消息的后半部分**。
v3 修正为：

```
[STRUCTURED] level=error message="体检未通过（健康分 0）"
[STRUCTURED] level=warning message="JSON 解析失败：第 1 行第 2 列: Expecting value"
```

无特殊字符时**不加引号**，保持旧输出形态：

```
[STRUCTURED] stage=pack file_count=18
```

因此解析器必须实现「最小 CSV 式」的词法：`key=` 后若是 `"`，则读到下一个
未转义的 `"` 为止。参考实现见 `cipacker.structured.parse_line()`，
**强烈建议直接复用或对照移植**。

转义序列：`\\` → `\`、`\"` → `"`、`\n` → 换行、`\r` → 回车、`\t` → 制表符。

## 事件类型

### 阶段切换

```
[STRUCTURED] stage=<stage_name>
```

| stage 值 | 说明 |
|-----------|------|
| `pack` | 打包阶段 |
| `unpack` | 解包阶段 |
| `unpack_complete` | 解包完成 |
| `download` | 下载阶段 |
| `check_install` | 检查安装状态 |
| `complete` | 整体流程完成 |
| `select_version` | 版本选择 |
| `select_source` | 下载源选择 |
| `select_ci1x_source` | CI 1.x 下载源选择 |
| `verify` | 校验阶段（**v3 新增**） |
| `doctor` | 体检阶段（**v3 新增**） |

### 级别事件

| level | 说明 | 典型字段 | 示例 |
|-------|------|----------|------|
| `info` | 信息通知 | `stage`, `auto`, `selected` | `level=info stage=select_version auto=true selected=1.7.0.1` |
| `warning` | 警告 | `message` | `level=warning message="检测到 ClassIsland 正在运行"` |
| `error` | 错误 | `message` | `level=error message="解压后的 CI 安装无效"` |
| `success` | 成功完成 | — | `level=success` |
| `ask` | 需要用户输入 | `message`, `default`, `choices` | 见下文 |
| `finding` | 单条体检发现（**v3 新增**） | `rule`, `severity`, `message`, `location` | 见下文 |

### 体检发现事件（level=finding，v3 新增）

`verify` / `doctor` / `pack --doctor` 会为**每一条**发现输出一个独立事件，
便于外壳逐条渲染为列表项：

```
[STRUCTURED] level=finding rule=broken-json severity=error message="JSON 解析失败：第 1 行第 2 列: Expecting value" location=Profiles/broken.json
[STRUCTURED] level=finding rule=credential-exposure severity=warning message="检测到 5 处可能的敏感信息" location=Config/Plugins/x/settings.json
[STRUCTURED] level=finding rule=plugin-disabled severity=info message="插件 'X' 处于禁用状态，迁移后将保持禁用" location=Plugins/X/.disabled
```

| 字段 | 说明 |
|------|------|
| `rule` | 规则标识符（稳定，可用于映射到 UI 文案与图标） |
| `severity` | `error` / `warning` / `info` |
| `message` | 人类可读描述（**可能含空格，务必按引号规则解析**） |
| `location` | 相对路径；无具体位置时该字段省略 |

v3 内置规则标识符：

| `rule` | severity | 含义 |
|--------|----------|------|
| `missing-settings` | error | 缺少 `Settings.json` |
| `broken-json` | error | JSON 无法解析（文件损坏） |
| `abs-path-leak` | warning | 含本机绝对路径，跨机迁移可能失效 |
| `credential-exposure` | warning | 含疑似口令/令牌等敏感信息 |
| `plugin-manifest-invalid` | error | 插件 `manifest.yml` 解析失败 |
| `plugin-dir-name-mismatch` | warning | 插件目录名与 `manifest.id` 不一致 |
| `plugin-manifest-warning` | warning | manifest 含未知平台等可疑值 |
| `plugin-disabled` | info | 插件处于禁用状态 |
| `plugin-pending-uninstall` | info | 插件带有 `.uninstall` 标记 |
| `plugin-dep-missing` | error | 依赖的必装插件缺失 |
| `orphan-plugin-config` | warning | 存在设置但对应插件未安装 |
| `large-file` | info | 单文件体积异常偏大 |

### 用户询问（level=ask）

当工具需要用户交互时，会先输出 `level=ask` 事件，然后等待 stdin 输入：

```
[STRUCTURED] level=ask message=是否下载 ClassIsland? (y/N)  default=N
[STRUCTURED] level=ask message=请选择版本 (0-26):  default=0 choices=1.7.106.2,1.7.106.1,1.7.106.0,...
```

| 字段 | 说明 |
|------|------|
| `message` | 提示文本 |
| `default` | 默认值（用户直接回车时采用） |
| `choices` | 逗号分隔的可选值列表（仅列表选择场景） |

GUI 外壳应监听此事件，弹出对话框或下拉列表，将用户选择写入子进程 stdin。

### 选项列表（level=options + level=option）

列表选择场景下，工具会先输出一个 `level=options` 事件标记列表开始，随后逐条输出 `level=option` 事件：

```
[STRUCTURED] level=options stage=select_version count=26
[STRUCTURED] level=option index=1 value=1.7.106.2 source=github
[STRUCTURED] level=option index=2 value=1.7.106.1 source=github
[STRUCTURED] level=option index=3 value=1.7.106.0 source=github
...
[STRUCTURED] level=ask message=请选择版本 (0-26):  default=0 choices=1.7.106.2,1.7.106.1,...
```

`level=option` 的字段因场景而异：

| 场景 | stage | 额外字段 |
|------|-------|----------|
| 版本选择 | `select_version` | `index`, `value`(版本号), `source`(来源) |
| 下载源选择 | `select_source` | `index`, `value`(源类型), `label`(显示名), `ping`(延迟秒数) |
| CI 1.x 源选择 | `select_ci1x_source` | `index`, `value`(源类型), `label`(显示名) |

### 下载进度

```
[STRUCTURED] stage=download progress=0.0 current=0 total=46000000
[STRUCTURED] progress=45.2 current=20800000 total=46000000
[STRUCTURED] progress=100.0
```

| 字段 | 说明 |
|------|------|
| `progress` | 百分比（0.0 ~ 100.0） |
| `current` | 已下载字节数 |
| `total` | 总字节数（0 表示未知大小） |

### 最终结果

```
[STRUCTURED] result=success exit_code=0
[STRUCTURED] result=failed exit_code=1 message=错误描述
[STRUCTURED] result=cancelled exit_code=130
```

| result 值 | 说明 | exit_code |
|-----------|------|-----------|
| `success` | 正常完成 | 0 |
| `failed` | 执行失败 | 1 |
| `cancelled` | 用户中断（Ctrl+C） | 130 |

## 完整示例

### 解包流程

```
$ cipacker unpack ci_config.zip -d D:\ClassIsland --structured

[STRUCTURED] stage=check_install
[STRUCTURED] stage=download
[STRUCTURED] stage=download progress=0.0 current=0 total=46000000
[STRUCTURED] progress=12.5 current=5760000 total=46000000
[STRUCTURED] progress=67.8 current=31200000 total=46000000
[STRUCTURED] progress=100.0
[STRUCTURED] level=success
[STRUCTURED] stage=unpack
[STRUCTURED] level=warning message=目标目录已有 ClassIsland 配置: D:\ClassIsland
[STRUCTURED] level=ask message=是否覆盖? (y/N)  default=N
[STRUCTURED] level=warning message=版本不一致: 打包时=1.7.0.1, 目标=1.7.106.2
[STRUCTURED] level=ask message=即将将配置解压到: D:\ClassIsland\n继续? (y/N)  default=N
[STRUCTURED] stage=unpack_complete file_count=37
[STRUCTURED] level=success
[STRUCTURED] result=success exit_code=0
```

### 下载流程（交互式版本选择）

```
$ cipacker download -d D:\ClassIsland --structured

[STRUCTURED] stage=check_install
[STRUCTURED] level=options stage=select_version count=26
[STRUCTURED] level=option index=1 value=1.7.106.2 source=github
[STRUCTURED] level=option index=2 value=1.7.106.1 source=github
...
[STRUCTURED] level=option index=10 value=1.7.0.1 source=disturb
...
[STRUCTURED] level=ask message=请选择版本 (0-26):  default=0 choices=1.7.106.2,1.7.106.1,...
[STRUCTURED] stage=download
[STRUCTURED] stage=download progress=0.0 current=0 total=46000000
[STRUCTURED] progress=50.0 current=23000000 total=46000000
[STRUCTURED] progress=100.0
[STRUCTURED] level=success
[STRUCTURED] stage=complete result=success
[STRUCTURED] level=success
[STRUCTURED] result=success exit_code=0
```

## GUI 外壳集成建议

### 子进程启动

```python
import subprocess

proc = subprocess.Popen(
    ["cipacker", "unpack", "ci_config.zip", "-d", target, "--structured"],
    stdin=subprocess.PIPE,   # 用于回答 ask 事件
    stdout=subprocess.PIPE,  # 人类可读日志（可选展示）
    stderr=subprocess.PIPE,  # 结构化事件
    text=True,
    encoding="utf-8",        # 重要：CI 为中文应用，必须显式使用 UTF-8
)
```

### 事件解析（v3 推荐做法）

**不要**用 `payload.split(" ")` 简单切分——含空格的消息会被截断。
请使用与 `cipacker.structured.parse_line()` 等价的引号感知解析：

```python
def parse_line(line: str) -> dict[str, str]:
    """解析一条 [STRUCTURED] 事件，正确处理引号与转义。"""
    PREFIX = "[STRUCTURED] "
    if not line.startswith(PREFIX):
        return {}
    payload = line[len(PREFIX):].strip()

    fields: dict[str, str] = {}
    i, n = 0, len(payload)
    while i < n:
        while i < n and payload[i] in " \t":
            i += 1
        if i >= n:
            break
        ks = i
        while i < n and payload[i] not in "= \t":
            i += 1
        key = payload[ks:i]
        if i >= n or payload[i] != "=":
            continue
        i += 1
        if i < n and payload[i] == '"':
            i += 1
            buf = []
            while i < n:
                ch = payload[i]
                if ch == "\\" and i + 1 < n:
                    buf.append({"n": "\n", "r": "\r", "t": "\t"}.get(payload[i+1], payload[i+1]))
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                buf.append(ch)
                i += 1
            value = "".join(buf)
        else:
            vs = i
            while i < n and payload[i] not in " \t":
                i += 1
            value = payload[vs:i]
        if key:
            fields[key] = value
    return fields


for line in proc.stderr:
    fields = parse_line(line)
    if not fields:
        continue

    level = fields.get("level")
    stage = fields.get("stage")

    if level == "ask":
        # 弹出对话框，将用户回答写入 proc.stdin
        answer = show_dialog(fields["message"], fields.get("default"))
        proc.stdin.write(answer + "\n")
        proc.stdin.flush()

    elif level == "options":
        options = []
        option_count = int(fields.get("count", 0))

    elif level == "option":
        options.append({
            "index": int(fields.get("index", 0)),
            "value": fields.get("value", ""),
            "source": fields.get("source", ""),
            "label": fields.get("label", ""),
            "ping": fields.get("ping", ""),
        })

    elif level == "finding":
        # v3：逐条体检发现，渲染为可点击的问题列表
        findings.append({
            "rule": fields.get("rule", ""),
            "severity": fields.get("severity", "info"),
            "message": fields.get("message", ""),      # 已正确还原空格
            "location": fields.get("location", ""),
        })

    elif "progress" in fields:
        update_progress(float(fields["progress"]))

    elif level == "success":
        show_success()

    elif level == "error":
        show_error(fields.get("message", "未知错误"))

    elif "result" in fields:
        # 流程结束——保证恰好出现一次
        result = fields["result"]
        break
```

### 无交互自动化

如果 GUI 外壳希望完全自动化（不弹窗），可以通过 `--yes` 和 `--yes-download` 参数跳过所有确认：

```bash
cipacker unpack ci_config.zip -d D:\ClassIsland --structured --yes --yes-download
```

此模式下不会产生 `level=ask` 事件，但仍会输出进度、阶段和结果事件。

若目标目录不是有效 ClassIsland 安装、又不想触发下载，可用 `--force`：
此时会输出一条 `level=warning`，然后照常解包（适用于预置配置 / 制作母盘）。

```bash
cipacker unpack base_config.zip -d D:\Staging --structured --force --yes
```

## 字段速查表

| 字段 | 出现场景 | 说明 |
|------|----------|------|
| `stage` | 阶段切换 / 级别事件 | 当前所处阶段 |
| `level` | 级别事件 | info / warning / error / success / ask / options / option |
| `message` | ask / warning / error | 人类可读消息文本 |
| `default` | ask | 默认值 |
| `choices` | ask | 逗号分隔的可选值 |
| `index` | option | 选项序号（从 1 开始） |
| `value` | option | 选项值（版本号 / 源类型） |
| `source` | option (版本选择) | 版本来源（github / disturb / distribution:xxx） |
| `label` | option (源选择) | 源显示名称 |
| `ping` | option (源选择) | 延迟秒数 |
| `count` | options | 选项总数 |
| `auto` | info | 是否自动选择（true/false） |
| `selected` | info | 自动选中的值 |
| `progress` | 下载 | 百分比 0.0 ~ 100.0 |
| `current` | 下载 | 已下载字节 |
| `total` | 下载 | 总字节 |
| `result` | 流程结束 | success / failed / cancelled |
| `exit_code` | 流程结束 | 退出码 |
| `file_count` | pack / unpack | 文件数量 |
| `rule` | finding (v3) | 体检规则标识符 |
| `severity` | finding (v3) | error / warning / info |
| `location` | finding (v3) | 相对路径（可能省略） |

## v3 契约保证

以下保证由测试套件 `tests/test_structured_contract.py` 逐条断言。
修改任何一条都会被 CI 捕获：

1. **前缀与原语不变** —— `[STRUCTURED] ` + 空格分隔的 `key=value`。
2. **`result=` 恰好一次** —— 无论成功、失败、取消还是参数错误，
   整个进程生命周期内有且仅有一条 `result=` 事件。
   外壳可以安全地以「收到 `result=`」作为流程终止信号。
3. **`result=` 携带 `exit_code`** —— 等于进程真实退出码，失败时附带 `message`。
4. **stdout 不掺入事件** —— 人类可读输出只在 stdout，事件只在 stderr。
5. **只增不改** —— 新增 `stage` 值、新增 `level` 值、新增字段，
   绝不修改既有字段的名称或语义。
6. **引号规则** —— 含特殊字符的值被双引号包裹（见上文），
   解析器必须按引号感知方式处理。

## 退出码约定

| 退出码 | 含义 |
|--------|------|
| `0` | 成功 |
| `1` | 一般性失败（I/O、校验失败、结构不兼容等） |
| `2` | 命令行用法错误（由 argparse 产生） |
| `3` | 前置条件不满足（CI 未安装且未用 `--force`） |
| `130` | 用户中断（Ctrl+C） |

> 注意：`doctor` 默认是「纯报告」模式，**即使发现问题也返回 0**；
> 需要以退出码作门禁时请加 `--strict`。

## 许可证

MIT License
