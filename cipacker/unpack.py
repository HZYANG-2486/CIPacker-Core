"""解包：把迁移包内容还原到目标 ClassIsland 安装。

路径映射的核心洞见是：**包内路径始终相对数据根**，而数据根由目标
安装的 :class:`~cipacker.layout.Layout` 决定。因此 V1 与 V2 的差异
在 ``Layout`` 层就已被吸收，解包逻辑无需任何版本分支。
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import logui
from .archive import ArchiveFormat, is_safe_member, normalize_member, read_archive
from .layout import Layout
from .models import MANIFEST_NAME


@dataclass
class UnpackPlan:
    """一次解包的执行计划。"""

    items: list[tuple[str, str]] = field(default_factory=list)
    """``(包内路径, 目标相对路径)`` 列表。"""

    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(包内路径, 跳过原因)`` 列表。"""

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass
class UnpackResult:
    """解包结果。"""

    target: Path
    extracted: int = 0
    skipped: int = 0
    overwritten: int = 0
    backup_dir: Path | None = None
    errors: list[str] = field(default_factory=list)


def build_plan(archive_path: Path, target: Layout) -> UnpackPlan:
    """构造解包计划（不做任何写入）。

    这一步把所有「跳过」判断前置，使用户在确认前就能看到完整影响范围。
    """
    info = read_archive(archive_path)
    plan = UnpackPlan()

    seen: dict[str, str] = {}  # 归一路径 -> 首个出现时的原始成员名

    for name in info.names:
        if name == MANIFEST_NAME or name.endswith("/"):
            continue

        if info.data_prefix:
            rel = normalize_member(name)
        else:
            rel = name.replace("\\", "/").lstrip("/")

        # 安全检查针对最终落点
        safe, reason = is_safe_member(rel, target.app_root)
        if not safe:
            plan.skipped.append((name, reason))
            continue

        # 重复条目：后写会静默覆盖先写，是「内容与清单不符」的常见来源，
        # 也让校验结果不可预测。这里显式跳过，并把原因告知用户。
        if rel in seen:
            plan.skipped.append(
                (name, f"与包内 {seen[rel]} 路径重复，已跳过以避免静默覆盖")
            )
            continue

        seen[rel] = name
        plan.items.append((name, rel))

    return plan


def _atomic_write(archive: zipfile.ZipFile, member: str, dest: Path) -> None:
    """原子写入单个文件，避免中断产生半截文件。

    实现要点：

    * 先写同目录下的 ``.ciptmp`` 临时文件，再 ``replace`` 到目标
      （同目录保证 ``rename`` 是同一个文件系统内的原子操作）。
    * 任一步失败都清理临时文件，不留垃圾。
    * ``dest`` 若已是符号链接，先解除——否则会跟随链接写到链接目标，
      构成「zip 符号链接逃逸」。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 防止跟随已有符号链接写穿到目标之外
    if dest.is_symlink():
        dest.unlink()

    temp = dest.with_name(dest.name + ".ciptmp")
    try:
        with archive.open(member) as source, open(temp, "wb") as handle:
            shutil.copyfileobj(source, handle)
        temp.replace(dest)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def backup_existing(
    target: Layout,
    plan: UnpackPlan,
    backup_root: Path | None = None,
) -> Path | None:
    """把将被覆盖的现有文件备份到 ``Backups/`` 下。

    这是对官方「覆盖即丢失」行为的一处改进：解包总是可回退。
    """
    existing = [
        rel
        for _, rel in plan.items
        if (target.app_root / rel).is_file()
    ]
    if not existing:
        return None

    from .models import utc_now_iso

    stamp = (
        utc_now_iso().replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    )
    root = backup_root or target.backups_dir
    backup_dir = root / f"PreCIPacker_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    for rel in existing:
        source = target.app_root / rel
        dest = backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, dest)
        except OSError as exc:
            logui.warn(f"备份失败（继续解包）: {rel} - {exc}")

    return backup_dir


def execute_plan(
    archive_path: Path,
    target: Layout,
    plan: UnpackPlan,
    *,
    make_backup: bool = True,
    progress_callback: object | None = None,
) -> UnpackResult:
    """执行解包计划。"""
    result = UnpackResult(target=target.app_root)
    result.skipped = len(plan.skipped)

    if make_backup:
        result.backup_dir = backup_existing(target, plan)
        if result.backup_dir is not None:
            logui.out(f"已备份被覆盖文件到: {result.backup_dir}")

    overwritten_before = sum(
        1 for _, rel in plan.items if (target.app_root / rel).is_file()
    )

    with zipfile.ZipFile(archive_path, "r") as archive:
        for member, rel in plan.items:
            dest = target.app_root / rel
            try:
                _atomic_write(archive, member, dest)
            except (OSError, zipfile.BadZipFile, KeyError) as exc:
                message = f"解压失败: {rel} - {exc}"
                logui.warn(message)
                result.errors.append(message)
                continue
            result.extracted += 1
            if callable(progress_callback):
                progress_callback(rel)

    result.overwritten = overwritten_before
    return result


def flatten_if_nested(target: Layout) -> bool:
    """处理「包内顶层多了一层目录」的常见情况。

    部分社区打包工具会生成 ``<root>/ClassIsland/data/...`` 这类包。
    若数据根下只有单一目录且其内部才像数据根，则把它上提一层。
    """
    app_root = target.app_root
    if not app_root.is_dir():
        return False

    if (app_root / "Settings.json").is_file() or (app_root / "Profiles").is_dir():
        return True

    try:
        children = [c for c in app_root.iterdir() if c.is_dir() and not c.name.startswith(".")]
    except OSError:
        return False

    if len(children) != 1:
        return False

    inner = children[0]
    if not ((inner / "Settings.json").is_file() or (inner / "Profiles").is_dir()):
        return False

    for item in sorted(inner.iterdir(), key=lambda p: p.name):
        dest = app_root / item.name
        if dest.exists():
            continue
        shutil.move(str(item), str(dest))

    try:
        inner.rmdir()
    except OSError:
        pass

    logui.warn(f"检测到嵌套目录，已上提内容: {inner.name}")
    return True


def detect_format_summary(archive_path: Path) -> str:
    """生成包格式的人类可读描述。"""
    info = read_archive(archive_path)
    label = {
        ArchiveFormat.OWN_V3: "CIPacker v3 格式",
        ArchiveFormat.OWN_V2: "CIPacker 旧版格式（兼容读取）",
        ArchiveFormat.OFFICIAL: "官方 / 裸 zip 格式",
        ArchiveFormat.UNKNOWN: "未知格式",
    }[info.format]

    structure = info.ci_structure if info.ci_structure != "unknown" else "未识别"
    version = info.ci_version or "未知"
    return f"{label} | 结构: {structure} | 版本: {version}"
