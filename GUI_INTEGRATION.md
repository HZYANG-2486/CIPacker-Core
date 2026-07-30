# CiPack GUI 集成文档

本文档面向希望为 CiPack 开发图形界面（GUI）外壳的开发者。

通过 `--structured` 参数，CiPack 会向 **stderr** 输出机器可读的事件流，GUI 外壳可解析这些事件来渲染进度条、阶段提示、选项列表和交互对话框。

## 启用方式

在任意子命令后添加 `--structured`：

```bash
python CiPack.py pack D:\ClassIsland -o ci_config.zip --structured
python CiPack.py unpack ci_config.zip -d D:\ClassIsland --structured
python CiPack.py download -d D:\ClassIsland --structured
python CiPack.py list ci_config.zip --structured
```

## 输出格式

所有结构化事件以 `[STRUCTURED]` 前缀输出到 **stderr**，每行一条，格式为空格分隔的 `key=value` 对：

```
[STRUCTURED] key1=value1 key2=value2 key3=value3
```

> stdout 仍输出人类可读的日志，stderr 专供结构化事件。两者互不干扰，可同时消费。

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

### 级别事件

| level | 说明 | 典型字段 | 示例 |
|-------|------|----------|------|
| `info` | 信息通知 | `stage`, `auto`, `selected` | `level=info stage=select_version auto=true selected=1.7.0.1` |
| `warning` | 警告 | `message` | `level=warning message=检测到 ClassIsland 正在运行` |
| `error` | 错误 | `message` | `level=error message=解压后的 CI 安装无效` |
| `success` | 成功完成 | — | `level=success` |
| `ask` | 需要用户输入 | `message`, `default`, `choices` | 见下文 |

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
$ python CiPack.py unpack ci_config.zip -d D:\ClassIsland --structured

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
$ python CiPack.py download -d D:\ClassIsland --structured

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
    ["python", "CiPack.py", "unpack", "ci_config.zip", "-d", target, "--structured"],
    stdin=subprocess.PIPE,   # 用于回答 ask 事件
    stdout=subprocess.PIPE,  # 人类可读日志（可选展示）
    stderr=subprocess.PIPE,  # 结构化事件
    text=True,
)
```

### 事件解析

逐行读取 stderr，过滤 `[STRUCTURED]` 前缀，按空格拆分 `key=value`：

```python
for line in proc.stderr:
    if not line.startswith("[STRUCTURED] "):
        continue
    payload = line[len("[STRUCTURED] "):].strip()
    fields = {}
    for token in payload.split(" "):
        if "=" in token:
            k, v = token.split("=", 1)
            fields[k] = v

    level = fields.get("level")
    stage = fields.get("stage")

    if level == "ask":
        # 弹出对话框，将用户回答写入 proc.stdin
        answer = show_dialog(fields["message"], fields.get("default"))
        proc.stdin.write(answer + "\n")
        proc.stdin.flush()

    elif level == "options":
        # 开始接收选项列表
        options = []
        option_count = int(fields.get("count", 0))

    elif level == "option":
        # 收集单个选项
        options.append({
            "index": int(fields.get("index", 0)),
            "value": fields.get("value", ""),
            "source": fields.get("source", ""),
            "label": fields.get("label", ""),
            "ping": fields.get("ping", ""),
        })

    elif "progress" in fields:
        # 更新进度条
        update_progress(float(fields["progress"]))

    elif level == "success":
        show_success()

    elif level == "error":
        show_error(fields.get("message", "未知错误"))

    elif "result" in fields:
        # 流程结束
        result = fields["result"]
        break
```

### 无交互自动化

如果 GUI 外壳希望完全自动化（不弹窗），可以通过 `--yes` 和 `--yes-download` 参数跳过所有确认：

```bash
python CiPack.py unpack ci_config.zip -d D:\ClassIsland --structured --yes --yes-download
```

此模式下不会产生 `level=ask` 事件，但仍会输出进度、阶段和结果事件。

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

## 许可证

MIT License
