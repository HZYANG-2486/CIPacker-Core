"""插件元数据解析、状态标记与依赖闭环。

对应 ClassIsland 官方模型：

- ``PluginManifest.cs`` — ``manifest.yml`` 的字段定义
- ``PluginInfo.cs``    — ``.disabled`` / ``.uninstall`` 标记文件语义
- ``PluginDependency.cs`` — 依赖项的 ``{ id, isRequired }`` 结构

关键事实（源码核实）：

- 插件安装落点为 ``Plugins/<manifest.Id>``，即**目录名应等于 manifest.id**
- ``IsEnabled`` 的判定是 ``!File.Exists(PluginFolderPath/.disabled)``
- ``IsUninstalling`` 由 ``.uninstall`` 文件存在决定
- ``supportedOSPlatforms`` 有效值仅 ``Windows`` / ``Linux`` / ``OSX``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import logui
from .layout import Layout, extract_major_version
from .models import PluginDependency, PluginInfo
from .yamlish import YamlSubsetError, load_mapping

MANIFEST_FILENAME = "manifest.yml"
MANIFEST_FALLBACK = "Manifest.yml"
DISABLED_MARKER = ".disabled"
UNINSTALL_MARKER = ".uninstall"

#: 官方支持的平台标识。
VALID_PLATFORMS = frozenset({"Windows", "Linux", "OSX"})


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def _as_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def parse_dependencies(raw: object) -> list[PluginDependency]:
    """解析 ``dependencies`` 字段。

    支持官方 ``[{id, isRequired}]`` 形式，也容忍纯字符串简写
    （视为必选依赖），以便兼容社区中出现的非标准写法。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [PluginDependency(id=raw, is_required=True)] if raw else []
    if not isinstance(raw, (list, tuple)):
        return []

    result: list[PluginDependency] = []
    for item in raw:
        if isinstance(item, str):
            if item:
                result.append(PluginDependency(id=item, is_required=True))
        elif isinstance(item, dict):
            dep_id = item.get("id") or item.get("Id")
            if dep_id is None:
                continue
            required_raw = item.get("isRequired", item.get("IsRequired", True))
            result.append(
                PluginDependency(id=str(dep_id), is_required=bool(required_raw))
            )
    return result


def parse_supported_os(raw: object) -> list[str]:
    """解析 ``supportedOSPlatforms``，并标记非法值。"""
    values = _as_str_list(raw)
    # 官方默认支持所有平台
    if not values:
        return ["Windows", "OSX", "Linux"]
    return values


def _platform_warnings(values: list[str]) -> list[str]:
    warnings: list[str] = []
    for value in values:
        if value not in VALID_PLATFORMS:
            warnings.append(
                f"supportedOSPlatforms 含未知平台 {value!r}"
                f"（有效值：{'/'.join(sorted(VALID_PLATFORMS))}）"
            )
    return warnings


def load_plugin_manifest(path: Path) -> tuple[PluginInfo | None, list[str]]:
    """解析单个 ``manifest.yml``。

    :return: ``(PluginInfo, warnings)``；解析彻底失败时第一项为 None。
    """
    warnings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, [f"无法读取 manifest: {exc}"]

    try:
        data = load_mapping(text)
    except YamlSubsetError as exc:
        return None, [f"manifest 解析失败: {exc}"]

    if not data:
        return None, ["manifest 为空"]

    plugin_id = str(data.get("id") or data.get("Id") or "").strip()
    if not plugin_id:
        warnings.append("manifest 缺少 id 字段")

    supported_os = parse_supported_os(
        data.get("supportedOSPlatforms", data.get("supportedOSPlatform"))
    )
    warnings.extend(_platform_warnings(supported_os))

    dependencies = parse_dependencies(data.get("dependencies"))

    info = PluginInfo(
        id=plugin_id,
        name=str(data.get("name") or plugin_id or "未知插件"),
        version=str(data.get("version") or "unknown"),
        api_version=str(data.get("apiVersion") or "unknown"),
        description=str(data.get("description") or ""),
        author=str(data.get("author") or ""),
        entrance_assembly=str(data.get("entranceAssembly") or ""),
        supported_os=supported_os,
        dependencies=dependencies,
        parse_ok=True,
        parse_warnings=warnings,
    )

    if not info.entrance_assembly:
        warnings.append("manifest 缺少 entranceAssembly 字段")

    return info, warnings


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #


def scan_plugins(layout: Layout, *, quiet: bool = False) -> list[PluginInfo]:
    """扫描数据根 ``Plugins/`` 下所有插件。

    目录中不存在 ``manifest.yml`` 的条目会被跳过（可能是残留目录），
    但会在日志中提示，便于诊断。
    """
    plugins_dir = layout.plugins_dir
    if not plugins_dir.is_dir():
        return []

    result: list[PluginInfo] = []

    try:
        entries = sorted(plugins_dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        logui.warn(f"无法读取插件目录: {exc}")
        return []

    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue

        manifest_path = entry / MANIFEST_FILENAME
        if not manifest_path.is_file():
            alt = entry / MANIFEST_FALLBACK
            if alt.is_file():
                manifest_path = alt
            else:
                if not quiet:
                    logui.warn(f"插件目录缺少 manifest.yml，已跳过: {entry.name}")
                continue

        info, warnings = load_plugin_manifest(manifest_path)
        if info is None:
            if not quiet:
                logui.warn(f"插件 {entry.name} 元数据无效: {'; '.join(warnings)}")
            # 仍然记录，供体检报告使用
            info = PluginInfo(
                id=entry.name,
                name=entry.name,
                folder=entry.name,
                parse_ok=False,
                parse_warnings=warnings,
            )

        info.folder = entry.name
        info.enabled = not (entry / DISABLED_MARKER).exists()
        info.uninstalling = (entry / UNINSTALL_MARKER).exists()
        result.append(info)

    return result


# --------------------------------------------------------------------------- #
# 依赖闭环
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DependencyIssue:
    """一条插件依赖问题。"""

    severity: str  # "error" | "warning" | "info"
    rule_id: str
    message: str
    plugin_id: str
    suggestion: str = ""


def check_dependencies(plugins: list[PluginInfo]) -> list[DependencyIssue]:
    """检查插件之间的依赖闭合性。

    仅检查「本机已装插件集合」内部的一致性——跨机器的迁移完整性
    由 :mod:`cipacker.doctor` 在解包前对照目标环境完成。
    """
    issues: list[DependencyIssue] = []
    installed = {p.id: p for p in plugins if p.id}

    for plugin in plugins:
        for dep in plugin.dependencies:
            if dep.id in installed:
                continue
            if dep.is_required:
                issues.append(
                    DependencyIssue(
                        severity="error",
                        rule_id="plugin-dep-missing",
                        message=(
                            f"插件 '{plugin.name}' 依赖的必装插件 "
                            f"'{dep.id}' 未安装"
                        ),
                        plugin_id=plugin.id,
                        suggestion=f"请安装插件 {dep.id} 后重试",
                    )
                )
            else:
                issues.append(
                    DependencyIssue(
                        severity="info",
                        rule_id="plugin-dep-optional-missing",
                        message=(
                            f"插件 '{plugin.name}' 的可选依赖 "
                            f"'{dep.id}' 未安装（不影响使用）"
                        ),
                        plugin_id=plugin.id,
                    )
                )

    return issues


def check_api_compatibility(
    plugins: list[PluginInfo],
    target_ci_version: str | None,
) -> list[DependencyIssue]:
    """检查插件 ``apiVersion`` 与目标 ClassIsland 版本的兼容性。

    ``apiVersion`` 表示插件所针对的 ClassIsland 版本，主版本号不同即
    认为存在不兼容风险（插件 ABI 在 1.x → 2.x 之间发生过破坏性变更）。
    """
    issues: list[DependencyIssue] = []
    target_major = extract_major_version(target_ci_version)
    if target_major is None:
        return issues

    for plugin in plugins:
        plugin_major = extract_major_version(plugin.api_version)
        if plugin_major is None:
            continue
        if plugin_major != target_major:
            issues.append(
                DependencyIssue(
                    severity="warning",
                    rule_id="plugin-api-incompatible",
                    message=(
                        f"插件 '{plugin.name}' 面向 ClassIsland "
                        f"{plugin.api_version}，与目标版本 {target_ci_version} "
                        f"主版本不一致"
                    ),
                    plugin_id=plugin.id,
                    suggestion=(
                        f"请为插件 {plugin.id} 获取面向 "
                        f"ClassIsland {target_major}.x 的版本"
                    ),
                )
            )

    return issues


def build_dependency_index(plugins: list[PluginInfo]) -> dict[str, list[str]]:
    """构建 ``被依赖插件 -> [依赖它的插件]`` 的反向索引。"""
    index: dict[str, list[str]] = {}
    for plugin in plugins:
        for dep in plugin.dependencies:
            if dep.is_required:
                index.setdefault(dep.id, []).append(plugin.id)
    return index


def diff_plugins(
    source: list[PluginInfo],
    target: list[PluginInfo],
) -> dict[str, list[str]]:
    """对比源包与目标安装的插件差异（迁移前提示用）。

    :return: ``{"missing": [...], "version_mismatch": [...], "extra": [...]}``
    """
    source_map = {p.id: p for p in source if p.id}
    target_map = {p.id: p for p in target if p.id}

    missing: list[str] = []
    mismatch: list[str] = []
    extra: list[str] = []

    for pid, src in source_map.items():
        if pid not in target_map:
            missing.append(f"{src.name} ({src.version})")
            continue
        tgt = target_map[pid]
        if src.version != tgt.version:
            mismatch.append(f"{src.name}: 源 {src.version} → 目标 {tgt.version}")

    for pid, tgt in target_map.items():
        if pid not in source_map:
            extra.append(f"{tgt.name} ({tgt.version})")

    return {"missing": missing, "version_mismatch": mismatch, "extra": extra}
