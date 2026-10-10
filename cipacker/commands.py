"""各子命令的实现。

与 CLI 参数解析分离，便于单独测试与复用。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import doctor as doctor_mod
from . import logui, structured
from .archive import read_archive
from .discovery import iter_files, total_bytes
from .doctor import DoctorOptions, redact_file, redacted_copy
from .doctor import run as run_doctor
from .errors import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_PRECONDITION,
    CancelledError,
    DoctorError,
    DownloadError,
    PackError,
    UnpackError,
    UsageError,
    VerifyError,
)
from .layers import (
    layer_label,
    parse_layer_names,
    resolve_selection,
)
from .layout import LayoutKind, detect_ci_version, detect_layout
from .pack import (
    build_manifest,
    collect_pack_items,
    deterministic_manifest,
    write_package,
)
from .plugins import diff_plugins, scan_plugins
from .unpack import build_plan, execute_plan, flatten_if_nested
from .verify import verify_archive

# --------------------------------------------------------------------------- #
# pack
# --------------------------------------------------------------------------- #


def pack_command(args) -> int:
    structured.stage("pack")

    source = Path(args.dir).expanduser()
    if not source.is_dir():
        raise PackError(f"目录不存在: {source}")

    layout = detect_layout(source)
    if layout.kind is LayoutKind.UNKNOWN:
        logui.warn(
            "未能识别 ClassIsland 结构，将按给定目录直接打包；"
            "请确认路径指向数据目录（CI 2.x 通常是 …/data）"
        )

    output = Path(args.output).expanduser()
    if args.cidata and output.suffix.lower() != ".cidata":
        output = output.with_suffix(".cidata")

    logui.out(f"数据目录: {layout.app_root}")
    logui.out(f"结构: {layout.kind.value}   版本: {detect_ci_version(layout.app_root) or '未知'}")

    # --- 层选择 ---
    try:
        selected = parse_layer_names(args.layers)
    except ValueError as exc:
        raise PackError(str(exc)) from exc
    selection = resolve_selection(selected)

    for warning in selection.warnings:
        logui.warn(warning)

    # --- 文件收集 ---
    files = iter_files(layout, include_backups=args.include_backups)
    if not files:
        raise PackError("没有找到可打包的配置内容")

    items, _redacted, skipped = collect_pack_items(layout, selection, files)
    for warning in skipped:
        logui.warn(warning)

    if not items:
        raise PackError(
            "所选迁移层内没有可打包的文件",
            hint="请检查 --layers 选择，或改用 --layers all",
        )

    logui.out(
        f"共 {len(items)} 个文件，"
        f"原始大小 {logui.human_size(total_bytes(files))}"
    )

    # --- 可选：打包前体检 ---
    report = None
    if args.doctor or args.json_report:
        structured.stage("doctor")
        report = run_doctor(
            layout,
            files=files,
            selection=selection,
            options=DoctorOptions(strict=args.strict),
        )
        _print_report(report)

        if args.json_report:
            target = Path(args.json_report).expanduser()
            target.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logui.out(f"体检报告已写入: {target}")

        # 门禁语义：只有显式要求「先体检再打包」的 --doctor 才阻断打包。
        # --json-report 是纯粹的副作用开关（顺带导出一份报告），不该让打包失败；
        # 只传 --json-report 时一律继续打包，与「体检报告已写入」的提示一致。
        # 阻断条件与 doctor 子命令的 --strict 语义保持一致：
        #   默认（--doctor）      → 仅 error 阻断
        #   --doctor --strict     → error 与 warning 都阻断
        if args.doctor:
            blocking = report.has_errors or (
                args.strict and report.counts.get("warning")
            )
            if blocking:
                raise PackError(
                    f"体检未通过（健康分 {report.score}）",
                    hint="修复上述问题后重试，或去掉 --strict 以忽略警告",
                )

    # --- 可选：脱敏 ---
    # 关键：绝不能就地改写用户的真实配置。这里把需要脱敏的 JSON 复制到临时
    # 目录、在副本上脱敏，然后把 pack item 的来源重定向到副本。
    redacted_fields: list[str] = []
    redact_tmp: Path | None = None
    if args.redact:
        redact_tmp = Path(tempfile.mkdtemp(prefix="cipacker-redact-"))
        try:
            redacted_fields = _redact_items(items, redact_tmp)
        except BaseException:
            shutil.rmtree(redact_tmp, ignore_errors=True)
            raise
        logui.out(
            f"已脱敏 {len(redacted_fields)} 处敏感字段"
            "（仅影响包内容，源目录未被改动）"
        )

    # --- 写包 ---
    deterministic = not args.preserve_mtime
    manifest = build_manifest(
        layout,
        selection,
        include_backups=args.include_backups,
        deterministic=deterministic,
        redacted=redacted_fields,
    )
    if deterministic:
        deterministic_manifest(manifest)

    def on_file(rel: str) -> None:
        logui.out(f"  + {rel}")

    entries, fingerprint, sidecar = write_package(
        items,
        output,
        manifest,
        deterministic=deterministic,
        include_manifest=not args.no_manifest,
        progress_callback=on_file,
    )

    # 脱敏用的临时副本已写完，立即清理
    if redact_tmp is not None:
        shutil.rmtree(redact_tmp, ignore_errors=True)

    logui.out()
    logui.out(f"打包完成: {output}")
    logui.out(f"文件数: {len(entries)}   包大小: {logui.human_size(output.stat().st_size)}")
    logui.out(f"内容指纹: {fingerprint.content_sha256}")
    if sidecar is not None:
        logui.out(f"归档哈希: {fingerprint.archive_sha256}")
        logui.out(f"校验文件: {sidecar.name}")

    if deterministic:
        logui.out("（确定性模式：相同配置在任何机器上打包结果一致）")

    plugins = scan_plugins(layout, quiet=True)
    if plugins:
        logui.section(f"包含 {len(plugins)} 个插件")
        for plugin in plugins:
            state = "" if plugin.enabled else "  [已禁用]"
            logui.out(f"  {plugin.name} ({plugin.version}){state}")

    structured.stage("pack", file_count=len(entries))
    structured.success()
    return EXIT_OK


def _redact_items(items, staging: Path) -> list[str]:
    """把待打包的 JSON 复制到 ``staging`` 并脱敏副本。

    返回被脱敏的字段路径列表。**源文件永不被修改**——``items`` 中每一条的
    ``source`` 会被重定向到脱敏后的副本，供后续 :func:`write_package` 使用。
    """
    redacted: list[str] = []
    for item in items:
        if not item.rel_posix.lower().endswith(".json"):
            continue

        staged = staging / Path(item.rel_posix)
        try:
            fields = redacted_copy(item.source, staged)
        except Exception:  # noqa: BLE001 - 单个文件失败不影响整体打包
            logui.warn(f"脱敏失败，将按原样打包: {item.rel_posix}")
            continue

        if fields:
            # 重定向来源到副本；同时刷新 size，避免清单与实际内容不一致
            item.source = staged
            try:
                item.size = staged.stat().st_size
            except OSError:
                pass
            for field in fields:
                redacted.append(f"{item.rel_posix}:{field}")

    return redacted


# --------------------------------------------------------------------------- #
# unpack
# --------------------------------------------------------------------------- #


def unpack_command(args) -> int:
    archive_path = Path(args.input).expanduser()
    if not archive_path.is_file():
        raise UnpackError(f"迁移包不存在: {archive_path}")

    target_dir = Path(args.dir).expanduser()
    layout = detect_layout(target_dir)

    # --- 读取包信息 ---
    from .unpack import detect_format_summary

    logui.out(f"迁移包: {archive_path.name}")
    logui.out(f"格式: {detect_format_summary(archive_path)}")

    info = read_archive(archive_path)
    if info.manifest is not None:
        manifest = info.manifest
        logui.section("包信息")
        logui.out(f"  创建时间: {manifest.created_at or '未知'}")
        logui.out(f"  打包层级: {', '.join(layer_label(layer_name) for layer_name in manifest.layers) or '未知'}")
        logui.out(f"  文件数: {manifest.file_count}")
        if manifest.fingerprint:
            logui.out(f"  内容指纹: {manifest.fingerprint.content_sha256}")

    # --- 结构兼容性 ---
    _check_structure(info.ci_structure, layout)

    from .logui import UserPrompt

    prompt = UserPrompt(assume_yes=args.yes)

    # --- 确保 CI 存在（缺失时可下载；--force 可跳过）---
    if not layout.is_valid_install():
        if args.force:
            logui.warn(
                f"目标目录不是有效的 ClassIsland 安装: {layout.app_root}，"
                "已按 --force 继续解包"
            )
            structured.warning("目标不是有效 ClassIsland 安装，已按 --force 继续")
        else:
            if not _ensure_installation(args, target_dir, info):
                return EXIT_PRECONDITION
            layout = detect_layout(target_dir)

    # --- 构造计划 ---
    plan = build_plan(archive_path, layout)

    # --- --dry-run：纯只读预览，不做任何交互确认 ---
    if args.dry_run:
        _report_plugin_diff(info, layout)
        logui.section("解包计划")
        logui.out(f"  将写入 {plan.count} 个文件到 {layout.app_root}")
        if plan.skipped:
            logui.warn(f"  跳过 {len(plan.skipped)} 个不安全条目")
            for name, reason in plan.skipped[:5]:
                logui.out(f"    - {name} ({reason})")
        logui.out("\n（--dry-run 模式，未写入任何文件）")
        for _member, rel in plan.items:
            logui.out(f"  -> {rel}")
        structured.stage("unpack_complete", file_count=0)
        structured.success()
        return EXIT_OK

    # --- 运行中检测 ---
    from .install import is_ci_running

    if is_ci_running():
        logui.warn("检测到 ClassIsland 正在运行，配置写入可能失败或损坏")
        structured.warning("检测到 ClassIsland 正在运行")
        if not prompt.confirm("是否仍要继续?", default=False):
            raise CancelledError("已取消，请先关闭 ClassIsland")

    # --- 已有配置保护 ---
    if layout.has_existing_config():
        logui.warn(f"目标目录已有配置: {layout.app_root}")
        structured.warning(f"目标目录已有 ClassIsland 配置: {layout.app_root}")
        if not prompt.confirm("解包将覆盖现有配置文件，是否继续?", default=False):
            raise CancelledError("已取消")

    # --- 插件差异提示 ---
    _report_plugin_diff(info, layout)

    logui.section("解包计划")
    logui.out(f"  将写入 {plan.count} 个文件到 {layout.app_root}")
    if plan.skipped:
        logui.warn(f"  跳过 {len(plan.skipped)} 个不安全条目")
        for name, reason in plan.skipped[:5]:
            logui.out(f"    - {name} ({reason})")

    if not prompt.confirm(f"即将解包到 {layout.app_root}，是否继续?", default=False):
        raise CancelledError("已取消")

    # --- 执行 ---
    structured.stage("unpack")
    result = execute_plan(
        archive_path,
        layout,
        plan,
        make_backup=not args.no_backup,
    )
    flatten_if_nested(layout)

    logui.out()
    logui.out(f"解包完成: 共 {result.extracted} 个文件")
    if result.overwritten:
        logui.out(f"其中覆盖已有文件 {result.overwritten} 个")
    if result.skipped:
        logui.out(f"跳过不安全条目 {result.skipped} 个")
    if result.errors:
        logui.warn(f"{len(result.errors)} 个文件解压失败")
    if result.backup_dir:
        logui.out(f"原文件已备份到: {result.backup_dir}")
    logui.out("请确保 ClassIsland 已关闭后再启动。")

    structured.stage("unpack_complete", file_count=result.extracted)
    structured.success()
    return EXIT_OK


def _check_structure(source_structure: str, layout) -> None:
    """结构兼容性检查。"""
    if source_structure in ("unknown", "") or layout.kind is LayoutKind.UNKNOWN:
        return
    if source_structure == layout.kind.value:
        return

    logui.warn(
        f"版本结构不一致：包内结构 {source_structure}，目标结构 {layout.kind.value}"
    )
    structured.warning(
        f"版本结构不兼容: 配置包={source_structure}, 目标={layout.kind.value}"
    )
    logui.warn("跨结构迁移可能导致配置无法被正确加载")


def _report_plugin_diff(info, layout) -> None:
    """对比包内插件与目标已装插件。"""
    if info.manifest is None or not info.manifest.plugins:
        return

    target_plugins = scan_plugins(layout, quiet=True)
    diff = diff_plugins(info.manifest.plugins, target_plugins)

    if not any(diff.values()):
        return

    logui.section("插件差异")
    for name in diff["missing"]:
        logui.out(f"  [!] 包内有但目标未装: {name}")
    for name in diff["version_mismatch"]:
        logui.out(f"  [~] 版本不一致: {name}")
    for name in diff["extra"]:
        logui.out(f"  [+] 目标有但包内无: {name}")


def _ensure_installation(args, target_dir: Path, info) -> bool:
    """目标目录不是有效 CI 安装时的处理。"""
    from .logui import UserPrompt

    logui.out("未检测到 ClassIsland 安装。")

    prompt = UserPrompt(assume_yes=args.yes or args.yes_download)
    if not prompt.confirm("是否下载 ClassIsland?", default=False):
        logui.err(
            "请先安装 ClassIsland 后重试；"
            "若只想把配置释放到任意目录，请加 --force"
        )
        return False

    from .download import install_ci
    from .download.installer import InstallRequest

    major = None
    if info.ci_structure == "v1":
        major = 1
    elif info.ci_structure == "v2":
        major = 2

    version = args.ci_version or info.ci_version

    structured.stage("check_install")
    structured.stage("download")

    def on_progress(percent: float, current: int, total: int) -> None:
        structured.progress(percent, current, total)

    try:
        install_ci(
            InstallRequest(
                target=target_dir,
                version=version,
                major_version=major,
                source_key=args.source,
                spoof_ua=args.spoof_ua,
                progress=on_progress,
            ),
            progress_callback=on_progress,
        )
    except DownloadError as exc:
        logui.err(str(exc))
        return False

    structured.success()
    return True


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def list_command(args) -> int:
    archive_path = Path(args.input).expanduser()
    if not archive_path.is_file():
        raise VerifyError(f"迁移包不存在: {archive_path}")

    info = read_archive(archive_path)
    manifest = info.manifest

    if args.as_json:
        payload = {
            "path": str(archive_path),
            "format": info.format.value,
            "ci_structure": info.ci_structure,
            "ci_version": info.ci_version,
            "manifest": manifest.to_dict() if manifest else None,
            "files": info.file_names(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        structured.success()
        return EXIT_OK

    logui.out(f"迁移包: {archive_path.name}")
    logui.out(f"格式: {info.format.value}")

    if manifest is not None:
        logui.section("打包信息")
        logui.out(f"  工具: {manifest.tool} {manifest.tool_version}")
        logui.out(f"  创建时间: {manifest.created_at or '未知'}")
        logui.out(f"  结构: {manifest.ci_structure}   版本: {manifest.ci_version or '未知'}")
        logui.out(f"  来源平台: {manifest.source_platform or '未记录'}")
        logui.out(
            f"  打包层级: {', '.join(layer_label(layer_name) for layer_name in manifest.layers) or '全部'}"
        )
        logui.out(f"  包含备份: {'是' if manifest.include_backups else '否'}")
        logui.out(f"  文件数: {manifest.file_count}")
        if manifest.fingerprint:
            logui.out(f"  内容指纹: {manifest.fingerprint.content_sha256}")
            logui.out(f"  总字节数: {logui.human_size(manifest.fingerprint.total_bytes)}")
        if manifest.redacted:
            logui.out(f"  已脱敏字段: {len(manifest.redacted)} 处")

        if manifest.plugins:
            logui.section(f"打包时的插件（{len(manifest.plugins)}）")
            for plugin in manifest.plugins:
                state = "" if plugin.enabled else "  [已禁用]"
                logui.out(f"  {plugin.name} ({plugin.version})  api={plugin.api_version}{state}")

    declared = {entry.path: entry for entry in (manifest.files if manifest else [])}

    logui.section("文件列表")
    for name in sorted(info.file_names()):
        entry = declared.get(name)
        if args.hashes and entry is not None:
            logui.out(f"  {name}  ({logui.human_size(entry.size)})  {entry.sha256[:16]}…")
        elif entry is not None:
            logui.out(f"  {name}  ({logui.human_size(entry.size)})")
        else:
            logui.out(f"  {name}")

    structured.success()
    return EXIT_OK


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def verify_command(args) -> int:
    archive_path = Path(args.input).expanduser()
    if not archive_path.is_file():
        raise VerifyError(f"迁移包不存在: {archive_path}")

    structured.stage("verify")
    result = verify_archive(archive_path, deep=args.deep)

    if args.as_json:
        payload = {
            "path": str(archive_path),
            "ok": result.ok,
            "format": result.format.value,
            "checked_files": result.checked_files,
            "expected_fingerprint": result.expected_fingerprint,
            "actual_fingerprint": result.actual_fingerprint,
            "archive_hash_ok": result.archive_hash_ok,
            "issues": [
                {"kind": i.kind, "path": i.path, "detail": i.detail}
                for i in result.issues
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if not result.ok:
            structured.error("包校验未通过")
            return EXIT_FAILURE
        structured.success()
        return EXIT_OK

    logui.out(f"校验: {archive_path.name}")
    logui.out(f"格式: {result.format.value}")

    if not result.has_manifest:
        logui.out("该包不含清单，已仅校验 ZIP 结构完整性。")

    logui.out(f"已校验文件: {result.checked_files}")
    if result.expected_fingerprint:
        logui.out(f"期望指纹: {result.expected_fingerprint}")
        logui.out(f"实际指纹: {result.actual_fingerprint}")
    if result.archive_hash_ok is not None:
        logui.out(f"归档哈希: {'一致' if result.archive_hash_ok else '不一致'}")

    if result.ok:
        logui.out("\n校验通过：包内容与清单完全一致。")
        structured.success()
        return EXIT_OK

    logui.section(f"发现 {len(result.issues)} 个问题")
    for issue in result.issues:
        logui.out(f"  [{issue.kind}] {issue.path}")
        logui.out(f"        {issue.detail}")

    structured.error(f"包校验未通过（{len(result.issues)} 个问题）")
    raise VerifyError(
        f"包校验未通过（{len(result.issues)} 个问题）",
        hint="该包可能已损坏或被改动，建议从原始来源重新获取",
    )


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def doctor_command(args) -> int:
    target = Path(args.target).expanduser()
    if not target.exists():
        raise DoctorError(f"路径不存在: {target}")

    structured.stage("doctor")
    options = DoctorOptions(
        strict=args.strict,
        check_credentials=not args.no_credentials,
        check_paths=not args.no_paths,
        check_plugins=not args.no_plugins,
        target_ci_version=args.target_version,
    )

    is_archive = target.is_file()
    if is_archive:
        report = doctor_mod.run_on_archive(target, options=options)
    else:
        layout = detect_layout(target)
        report = run_doctor(layout, options=options)

    if args.redact and not is_archive:
        layout = detect_layout(target)
        from .discovery import iter_files as _iter

        total = 0
        for item in _iter(layout):
            if item.rel.lower().endswith(".json"):
                total += len(redact_file(layout.app_root / item.rel))
        logui.out(f"已就地脱敏 {total} 处敏感字段")

    if args.as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_report(report, verbose=True)

    # 默认是「纯报告」——发现错误也返回 0，便于在脚本中无条件调用。
    # 传 --strict 时才把「有错误」升级为非零退出码，供 CI/批处理门禁使用。
    if args.strict and (report.has_errors or report.counts.get("warning")):
        structured.error(
            f"体检未通过（错误 {report.counts.get('error', 0)} · "
            f"警告 {report.counts.get('warning', 0)}）"
        )
        return EXIT_FAILURE

    structured.success()
    return EXIT_OK


#: 带图形符号的严重度标记（可读性更好）。
_FANCY_MARKS = {
    "error": "✗",
    "warning": "!",
    "info": "i",
    "pass": "✓",
}

#: ASCII 降级标记：当输出流编码无法表示 ✓/✗ 时使用。
_ASCII_MARKS = {
    "error": "x",
    "warning": "!",
    "info": "i",
    "pass": "v",
}


def _severity_marks() -> dict[str, str]:
    """按标准输出流的编码能力选择符号标记。

    ``✓``(U+2713) 与 ``✗``(U+2717) 不在 GBK/CP936 字符集内，直接输出会抛
    ``UnicodeEncodeError``。CLI 入口已把标准流重配置为 UTF-8，这里再做一层
    兜底：若重配置未生效（例如宿主替换了 ``sys.stdout``），则降级为 ASCII，
    保证不会因编码问题崩溃，也不会显示成乱码方框。
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "".join(_FANCY_MARKS.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return _ASCII_MARKS
    return _FANCY_MARKS


def _print_report(report, verbose: bool = False) -> None:
    marks = _severity_marks()
    logui.section(f"体检报告（健康分 {report.score}/100）")
    logui.out(f"  目录: {report.root}")
    logui.out(f"  结构: {report.layout}   版本: {report.ci_version or '未知'}")

    counts = report.counts
    logui.out(
        f"  错误 {counts.get('error', 0)} · "
        f"警告 {counts.get('warning', 0)} · "
        f"提示 {counts.get('info', 0)}"
    )

    for finding in report.findings:
        marker = marks.get(finding.severity.value, "-")
        structured.finding(
            finding.rule_id, finding.severity.value, finding.message, finding.location
        )

        if not verbose and finding.severity.value in ("info",):
            continue
        location = f"  ({finding.location})" if finding.location else ""
        logui.out(f"  {marker} {finding.message}{location}")
        if finding.suggestion:
            logui.out(f"      → {finding.suggestion}")

    if not report.findings:
        logui.out("  未发现问题，配置状态良好。")


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #


def download_command(args) -> int:
    from .download.installer import InstallRequest, install_ci, list_available_versions

    structured.stage("check_install")

    if args.list_versions:
        major_filter: int | None = None
        if args.ci1x:
            major_filter = 1
        elif args.ci2x:
            major_filter = 2
        elif args.ci_version:
            major_filter = (
                1 if str(args.ci_version).lstrip("vV").startswith("1.") else 2
            )

        logui.out("正在汇总可用版本…")
        versions = list_available_versions(major_filter)
        if not versions:
            logui.err("无法获取版本列表（网络不可达？）")
            return EXIT_FAILURE

        structured.option_list(len(versions), "select_version")
        for index, (ver, source_key) in enumerate(versions, 1):
            structured.option(index, ver, source=source_key)
            logui.out(f"  {index}. {ver}  ({source_key})")
        structured.success()
        return EXIT_OK

    if not args.dir:
        raise UsageError("请用 -d/--dir 指定安装目录（仅在 --list-versions 时可省略）")

    target = Path(args.dir).expanduser()

    major: int | None = None
    if args.ci1x:
        major = 1
    elif args.ci2x:
        major = 2
    elif args.ci_version:
        major = 1 if str(args.ci_version).lstrip("vV").startswith("1.") else 2

    version: str | None = args.ci_version

    if version is None:
        logui.out("正在汇总可用版本…")
        versions = list_available_versions()
        if not versions:
            logui.err("无法获取版本列表，请用 --ci-version 指定版本")
            return EXIT_FAILURE

        candidates = [
            (v, s) for v, s in versions
            if major is None or v.startswith(f"{major}.")
        ] or versions

        structured.option_list(len(candidates), "select_version")
        structured.stage("select_version")
        for index, (v, s) in enumerate(candidates, 1):
            structured.option(index, v, source=s)

        from .logui import UserPrompt

        prompt = UserPrompt(assume_yes=args.yes)
        options = [(v, f"{v}  ({s})") for v, s in candidates]
        version = prompt.choose("请选择要安装的版本", options, default_index=0)
        if version is None:
            raise CancelledError("未选择版本")

    request = InstallRequest(
        target=target,
        version=version,
        major_version=major,
        source_key=args.source,
        spoof_ua=args.spoof_ua,
        verify=not args.no_verify,
    )

    structured.stage("download")

    def on_progress(percent: float, current: int, total: int) -> None:
        structured.progress(percent, current, total)

    install_ci(request, progress_callback=on_progress)

    logui.out(f"\nClassIsland 已安装到: {target}")
    structured.stage("complete", result="success")
    structured.success()
    return EXIT_OK
