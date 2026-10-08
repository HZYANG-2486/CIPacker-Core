"""配置体检引擎。

在打包/迁移**之前**对配置做一次体检，把「迁移后才发现的问题」
提前暴露出来，并提供可操作的修复建议。

规则以注册表形式组织，每条规则是一个函数，便于单独测试与扩展。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import logui
from .discovery import DiscoveredFile, iter_files
from .layers import LayerSelection
from .layout import Layout, detect_ci_version
from .models import Finding, HealthReport, PluginInfo, Severity, utc_now_iso
from .plugins import (
    check_api_compatibility,
    check_dependencies,
    scan_plugins,
)

#: 单个文件超过该体积时给出提示。
LARGE_FILE_THRESHOLD = 50 * 1024 * 1024

#: 敏感字段名模式（值将被脱敏）。
CREDENTIAL_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|secret|token|apikey|api_key|accesskey|access_key"
    r"|credential|privatekey|private_key)",
    re.IGNORECASE,
)

#: 账号类字段模式。
ACCOUNT_KEY_PATTERN = re.compile(
    r"(username|user_name|account|loginname|login_name|idcard|id_card)",
    re.IGNORECASE,
)

#: 绝对路径泄漏模式。
ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:\\\\?[Uu]sers\\\\"),
    re.compile(r"[A-Za-z]:\\"),
    re.compile(r"/(?:Users|home|root|mnt|opt)/[^\s\"']*"),
)

#: 中国大陆手机号。
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

#: 身份证号（15/18 位）。
IDCARD_PATTERN = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)|(?<!\d)\d{15}(?!\d)")

#: 需要扫描文本内容的扩展名。
SCANNABLE_SUFFIXES = frozenset({".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".txt"})


@dataclass
class DoctorOptions:
    """体检选项。"""

    strict: bool = False
    """严格模式：把部分 warning 提升为 error。"""

    check_credentials: bool = True
    check_paths: bool = True
    check_plugins: bool = True
    target_ci_version: str | None = None


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _read_text(path: Path, limit: int = 4 * 1024 * 1024) -> str | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > limit:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _load_json(path: Path) -> tuple[object | None, str | None]:
    text = _read_text(path)
    if text is None:
        return None, "文件过大或不可读"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}"


def _walk_values(data: object, prefix: str = ""):
    """递归遍历 JSON 结构，产出 ``(路径, 键名, 值)``。"""
    if isinstance(data, dict):
        for key, value in data.items():
            current = f"{prefix}.{key}" if prefix else str(key)
            yield current, str(key), value
            yield from _walk_values(value, current)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            current = f"{prefix}[{index}]"
            yield from _walk_values(value, current)


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #


@dataclass
class RuleContext:
    """规则执行上下文。"""

    layout: Layout
    files: list[DiscoveredFile]
    selection: LayerSelection
    plugins: list[PluginInfo]
    options: DoctorOptions


Rule = Callable[[RuleContext, HealthReport], None]

_RULES: list[tuple[str, Rule]] = []


def rule(func: Rule) -> Rule:
    """注册一条体检规则。"""
    _RULES.append((func.__name__, func))
    return func


def registered_rules() -> list[str]:
    return [name for name, _ in _RULES]


@rule
def check_settings_present(ctx: RuleContext, report: HealthReport) -> None:
    """数据根必须有 Settings.json。"""
    if not ctx.layout.settings_file.is_file():
        report.add(
            Finding(
                rule_id="missing-settings",
                severity=Severity.ERROR,
                message="数据根缺少 Settings.json，这不像一个有效的 ClassIsland 配置目录",
                location=str(ctx.layout.settings_file),
                suggestion="请确认路径指向正确的 ClassIsland 数据目录（CI 2.x 通常是 data/）",
            )
        )


@rule
def check_json_validity(ctx: RuleContext, report: HealthReport) -> None:
    """JSON 配置文件必须可解析。"""
    for item in ctx.files:
        if not item.rel.lower().endswith(".json"):
            continue
        path = ctx.layout.app_root / item.rel

        # 备份目录里的损坏文件不影响迁移
        if item.rel.startswith("Backups/"):
            continue

        data, error = _load_json(path)
        if error is not None:
            report.add(
                Finding(
                    rule_id="broken-json",
                    severity=Severity.ERROR,
                    message=f"JSON 解析失败：{error}",
                    location=item.rel,
                    suggestion="该文件可能在写入过程中损坏；可从 Backups/ 中恢复",
                )
            )


@rule
def check_absolute_paths(ctx: RuleContext, report: HealthReport) -> None:
    """检测配置中泄漏的本机绝对路径。"""
    if not ctx.options.check_paths:
        return

    severity = Severity.ERROR if ctx.options.strict else Severity.WARNING

    for item in ctx.files:
        suffix = Path(item.rel).suffix.lower()
        if suffix not in SCANNABLE_SUFFIXES:
            continue
        if item.rel.startswith("Backups/"):
            continue
        if item.size > 2 * 1024 * 1024:
            continue

        text = _read_text(ctx.layout.app_root / item.rel)
        if not text:
            continue

        samples: list[str] = []
        for pattern in ABSOLUTE_PATH_PATTERNS:
            for match in pattern.finditer(text):
                samples.append(match.group(0))
                if len(samples) >= 3:
                    break
            if len(samples) >= 3:
                break

        if samples:
            report.add(
                Finding(
                    rule_id="abs-path-leak",
                    severity=severity,
                    message=f"检测到 {len(samples)} 处本机绝对路径，迁移到其它机器后可能失效",
                    location=item.rel,
                    suggestion="请在 ClassIsland 中把相关路径改为相对路径，或迁移后手动修正",
                    evidence={"samples": samples},
                )
            )


@rule
def check_credentials(ctx: RuleContext, report: HealthReport) -> None:
    """检测配置中的凭据类敏感字段。"""
    if not ctx.options.check_credentials:
        return

    for item in ctx.files:
        if not item.rel.lower().endswith(".json"):
            continue
        if item.rel.startswith("Backups/"):
            continue

        data, error = _load_json(ctx.layout.app_root / item.rel)
        if error is not None or data is None:
            continue

        hits: list[str] = []
        for path, key, value in _walk_values(data):
            if isinstance(value, str) and value.strip():
                if CREDENTIAL_KEY_PATTERN.search(key):
                    hits.append(f"{path}（凭据）")
                elif ACCOUNT_KEY_PATTERN.search(key):
                    hits.append(f"{path}（账号信息）")
                elif PHONE_PATTERN.search(value):
                    hits.append(f"{path}（手机号）")
                elif IDCARD_PATTERN.search(value):
                    hits.append(f"{path}（身份证号）")

        if hits:
            report.add(
                Finding(
                    rule_id="credential-exposure",
                    severity=Severity.WARNING,
                    message=f"检测到 {len(hits)} 处可能的敏感信息",
                    location=item.rel,
                    suggestion="分享该配置包前建议使用 --redact 脱敏",
                    evidence={"fields": hits[:10]},
                )
            )


@rule
def check_plugin_integrity(ctx: RuleContext, report: HealthReport) -> None:
    """插件目录结构与元数据完整性。"""
    if not ctx.options.check_plugins:
        return

    plugins_dir = ctx.layout.plugins_dir
    if not plugins_dir.is_dir():
        return

    for plugin in ctx.plugins:
        if not plugin.parse_ok:
            report.add(
                Finding(
                    rule_id="plugin-manifest-invalid",
                    severity=Severity.WARNING,
                    message=f"插件 '{plugin.folder}' 的 manifest.yml 无法解析："
                    + ("; ".join(plugin.parse_warnings) or "未知原因"),
                    location=f"Plugins/{plugin.folder}/manifest.yml",
                    suggestion="该插件在迁移后可能无法加载，建议在源环境重新安装",
                )
            )
            continue

        # 目录名应与 manifest.id 一致（官方安装逻辑即以此为准）
        if plugin.id and plugin.folder and plugin.id != plugin.folder:
            report.add(
                Finding(
                    rule_id="plugin-dir-name-mismatch",
                    severity=Severity.WARNING,
                    message=f"插件目录名 '{plugin.folder}' 与 manifest.id '{plugin.id}' 不一致",
                    location=f"Plugins/{plugin.folder}",
                    suggestion="ClassIsland 以 manifest.id 识别插件，目录名不一致可能导致重复安装",
                )
            )

        for warning in plugin.parse_warnings:
            report.add(
                Finding(
                    rule_id="plugin-manifest-warning",
                    severity=Severity.INFO,
                    message=f"插件 '{plugin.folder}': {warning}",
                    location=f"Plugins/{plugin.folder}/manifest.yml",
                )
            )

        if not plugin.enabled:
            report.add(
                Finding(
                    rule_id="plugin-disabled",
                    severity=Severity.INFO,
                    message=f"插件 '{plugin.name}' 处于禁用状态，迁移后将保持禁用",
                    location=f"Plugins/{plugin.folder}/.disabled",
                )
            )

        if plugin.uninstalling:
            report.add(
                Finding(
                    rule_id="plugin-pending-uninstall",
                    severity=Severity.INFO,
                    message=f"插件 '{plugin.name}' 已被标记为待卸载",
                    location=f"Plugins/{plugin.folder}/.uninstall",
                    suggestion="该插件目录会在下次启动时被删除，无需迁移",
                )
            )


@rule
def check_plugin_dependencies(ctx: RuleContext, report: HealthReport) -> None:
    """插件依赖闭环。"""
    if not ctx.options.check_plugins:
        return

    for issue in check_dependencies(ctx.plugins):
        severity = {
            "error": Severity.ERROR,
            "warning": Severity.WARNING,
            "info": Severity.INFO,
        }.get(issue.severity, Severity.INFO)

        report.add(
            Finding(
                rule_id=issue.rule_id,
                severity=severity,
                message=issue.message,
                location=f"Plugins/{issue.plugin_id}/manifest.yml",
                suggestion=issue.suggestion,
            )
        )

    target_version = ctx.options.target_ci_version or detect_ci_version(ctx.layout.app_root)
    for issue in check_api_compatibility(ctx.plugins, target_version):
        report.add(
            Finding(
                rule_id=issue.rule_id,
                severity=Severity.WARNING,
                message=issue.message,
                location=f"Plugins/{issue.plugin_id}/manifest.yml",
                suggestion=issue.suggestion,
            )
        )


@rule
def check_orphan_plugin_configs(ctx: RuleContext, report: HealthReport) -> None:
    """插件设置目录应对应已安装插件。"""
    if not ctx.options.check_plugins:
        return

    configs_dir = ctx.layout.plugin_configs_dir
    if not configs_dir.is_dir():
        return

    installed = {p.id for p in ctx.plugins if p.id}
    installed |= {p.folder for p in ctx.plugins if p.folder}

    try:
        entries = sorted(configs_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return

    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name not in installed:
            report.add(
                Finding(
                    rule_id="orphan-plugin-config",
                    severity=Severity.WARNING,
                    message=f"存在插件 '{entry.name}' 的设置，但该插件未安装",
                    location=f"Config/Plugins/{entry.name}",
                    suggestion="若该插件已不再使用，可删除此目录以减小包体积",
                )
            )


@rule
def check_file_sizes(ctx: RuleContext, report: HealthReport) -> None:
    """体积异常的文件提示。"""
    for item in ctx.files:
        if item.size > LARGE_FILE_THRESHOLD:
            report.add(
                Finding(
                    rule_id="large-file",
                    severity=Severity.INFO,
                    message=f"文件体积较大（{logui.human_size(item.size)}）",
                    location=item.rel,
                    suggestion="确认该文件确实需要迁移，否则可用 --layers 排除所在层",
                )
            )


@rule
def check_layer_selection(ctx: RuleContext, report: HealthReport) -> None:
    """层选择的一致性提示。"""
    from .layers import layer_label, layer_spec

    for name in ctx.selection.selected:
        spec = layer_spec(name)
        for required in spec.soft_requires:
            if required not in ctx.selection.selected:
                report.add(
                    Finding(
                        rule_id="layer-soft-dependency",
                        severity=Severity.WARNING,
                        message=(
                            f"已选择「{spec.label}」但未选择其依赖的"
                            f"「{layer_label(required)}」"
                        ),
                        location=f"layer:{name.value}",
                        suggestion=f"如目标环境需要该数据，请追加 --layers {required.value}",
                    )
                )


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #


def run(
    layout: Layout,
    *,
    files: list[DiscoveredFile] | None = None,
    selection: LayerSelection | None = None,
    options: DoctorOptions | None = None,
) -> HealthReport:
    """对给定布局执行体检。"""
    from .layers import DEFAULT_LAYERS, resolve_selection

    opts = options or DoctorOptions()
    if files is None:
        files = iter_files(layout, include_backups=False)
    if selection is None:
        selection = resolve_selection(list(DEFAULT_LAYERS))

    plugins = scan_plugins(layout, quiet=True) if opts.check_plugins else []

    ctx = RuleContext(
        layout=layout,
        files=files,
        selection=selection,
        plugins=plugins,
        options=opts,
    )

    report = HealthReport(
        generated_at=utc_now_iso(),
        root=str(layout.app_root),
        layout=layout.kind.value,
        ci_version=detect_ci_version(layout.app_root),
    )

    for _name, func in _RULES:
        try:
            func(ctx, report)
        except Exception as exc:  # noqa: BLE001 - 单条规则失败不应中断体检
            report.add(
                Finding(
                    rule_id="doctor-internal-error",
                    severity=Severity.INFO,
                    message=f"规则 {_name} 执行异常: {exc}",
                )
            )

    report.sort()
    return report.compute()


def run_on_archive(path: Path, *, options: DoctorOptions | None = None) -> HealthReport:
    """对迁移包做体检（解压到临时目录后跑规则）。"""
    import tempfile

    from .archive import read_archive
    from .layout import LayoutKind
    from .unpack import build_plan, execute_plan

    info = read_archive(path)

    with tempfile.TemporaryDirectory(prefix="cipacker-doctor-") as tmp:
        fake_root = Path(tmp) / "data"
        fake_root.mkdir(parents=True, exist_ok=True)

        scratch = Layout(
            kind=LayoutKind.V2,
            package_root=Path(tmp),
            app_root=fake_root,
            version_dir=None,
        )

        plan = build_plan(path, scratch)
        execute_plan(path, scratch, plan, make_backup=False)

        report = run(scratch, options=options)
        report.root = str(path)
        report.layout = info.ci_structure
        report.ci_version = info.ci_version

        if info.manifest is not None:
            declared = {p.id for p in info.manifest.plugins if p.id}
            actual = {p.id for p in scan_plugins(scratch, quiet=True) if p.id}
            for missing in sorted(declared - actual):
                report.add(
                    Finding(
                        rule_id="manifest-plugin-missing",
                        severity=Severity.WARNING,
                        message=f"清单声明插件 '{missing}'，但包内未找到其目录",
                        location="__manifest__.json",
                    )
                )
            report.sort()
            report.compute()

        return report


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #


def redact_data(data: object) -> list[str]:
    """在**内存中**脱敏 JSON 结构里的敏感字段值（纯函数，不触碰磁盘）。

    :return: 被脱敏的字段路径列表。
    """
    redacted: list[str] = []

    def scrub(node: object, prefix: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                current = f"{prefix}.{key}" if prefix else str(key)
                if isinstance(value, str) and value.strip():
                    if CREDENTIAL_KEY_PATTERN.search(str(key)) or ACCOUNT_KEY_PATTERN.search(
                        str(key)
                    ):
                        node[key] = "***"
                        redacted.append(current)
                        continue
                scrub(value, current)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                scrub(value, f"{prefix}[{index}]")

    scrub(data)
    return redacted


def redacted_copy(path: Path, dest: Path) -> list[str]:
    """把 ``path`` 脱敏后写入 ``dest``，**绝不修改原文件**。

    这是 ``pack --redact`` 应当使用的入口：源目录属于用户的真实安装，
    任何情况下都不应被就地改写。

    :return: 被脱敏的字段路径列表；文件不可解析或无需脱敏时返回空列表。
    """
    data, error = _load_json(path)
    if error is not None or not isinstance(data, (dict, list)):
        return []

    redacted = redact_data(data)

    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(dest.name + ".ciptmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        temp.write_text(payload, encoding="utf-8")
        temp.replace(dest)
    except OSError:
        temp.unlink(missing_ok=True)
        raise

    return redacted


def redact_file(path: Path) -> list[str]:
    """**就地**脱敏单个 JSON 文件（会修改 ``path`` 本身）。

    仅在用户显式要求就地脱敏时使用（``doctor --redact``）。
    写入是原子的：先写临时文件再替换，避免中断损坏原配置。

    :return: 被脱敏的字段路径列表。
    """
    data, error = _load_json(path)
    if error is not None or not isinstance(data, (dict, list)):
        return []

    redacted = redact_data(data)
    if not redacted:
        return []

    # 原子写入：防止中途崩溃把用户配置截断成半截文件
    temp = path.with_name(path.name + ".ciptmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise

    return redacted
