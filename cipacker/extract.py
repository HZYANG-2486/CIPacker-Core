"""ClassIsland **本体**发行包的解压。

注意与 :mod:`cipacker.unpack` 的区别：

- ``unpack``：解**配置迁移包**到已有 CI 安装。
- ``extract``（本模块）：解 **CI 本体发行包**（从下载源拿到的 zip），
  把 launcher / app 目录铺到目标根，并做「扁平化」修正。

发行包常见两种形态：

1. 包内直接是文件（``ClassIsland.exe``、``app-*/``、``data/``）
2. 包内多一层顶层目录（``ClassIsland_app_windows_x64_.../...``）

情况 2 需要把顶层目录内容上提一层，否则检测不到有效安装。
"""

from __future__ import annotations

import shutil
import uuid
import zipfile
from pathlib import Path

from . import logui
from .archive import is_safe_member
from .layout import detect_layout


def extract_ci_package(archive_path: Path, target_dir: Path) -> bool:
    """把 CI 发行包解压到 ``target_dir``。

    :return: 解压后 ``target_dir`` 是否为有效 CI 安装。
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    before = set()
    try:
        before = set(p.name for p in target_dir.iterdir())
    except OSError:
        pass

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for member in archive.namelist():
                if member.endswith("/"):
                    continue

                safe, reason = is_safe_member(member, target_dir)
                if not safe:
                    logui.warn(f"跳过不安全的条目（{reason}）: {member}")
                    continue

                dest = target_dir / member.replace("\\", "/").lstrip("/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with archive.open(member) as source, open(dest, "wb") as handle:
                        shutil.copyfileobj(source, handle)
                except (OSError, zipfile.BadZipFile) as exc:
                    logui.warn(f"解压 {member} 失败: {exc}")
    except zipfile.BadZipFile as exc:
        logui.err(f"发行包不是有效的 zip: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - 失败时回滚本次新增内容
        _rollback(target_dir, before)
        logui.err(f"解压失败: {exc}")
        return False

    _flatten_single_top_dir(target_dir)
    _flatten_to_data_root(target_dir)

    return detect_layout(target_dir).is_valid_install()


def _rollback(target_dir: Path, before: set[str]) -> None:
    """删除本次解压新增的条目。"""
    try:
        current = set(p.name for p in target_dir.iterdir())
    except OSError:
        return

    for name in current - before:
        path = target_dir / name
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            pass


def _flatten_single_top_dir(target_dir: Path) -> None:
    """包内只有单一顶层目录时，把其内容上提一层。"""
    try:
        children = [p for p in target_dir.iterdir() if p.name != "__MACOSX"]
    except OSError:
        return

    if len(children) != 1 or not children[0].is_dir():
        return

    top = children[0]

    # 已经是有效结构就不动
    if (top / "data").is_dir() or (top / "ClassIsland.exe").is_file():
        pass
    elif not ((top / "Settings.json").is_file() or (top / "app").is_dir()):
        # 顶层目录内部也没有 CI 特征，不动
        has_app = any(child.name.startswith("app") for child in top.iterdir() if child.is_dir())
        if not has_app:
            return

    temp = target_dir / f"__ci_flatten_{uuid.uuid4().hex}__"
    shutil.move(str(top), str(temp))

    for item in sorted(temp.iterdir(), key=lambda p: p.name):
        dest = target_dir / item.name
        if dest.exists():
            logui.warn(f"跳过已存在的条目: {item.name}")
            continue
        shutil.move(str(item), str(dest))

    shutil.rmtree(temp, ignore_errors=True)


def _flatten_to_data_root(target_dir: Path) -> None:
    """处理 ``<root>/<x>/data/...`` 形态：把 ``data`` 上提到根。"""
    if (target_dir / "data").is_dir():
        return

    try:
        children = [p for p in target_dir.iterdir() if p.is_dir()]
    except OSError:
        return

    # 若某个子目录里有 app-* 与 data，说明发行包结构被多套了一层
    for child in children:
        if (child / "data").is_dir() and any(
            sub.name.startswith("app") for sub in child.iterdir() if sub.is_dir()
        ):
            logui.warn(f"检测到多余的嵌套层级，正在上提: {child.name}")
            for item in sorted(child.iterdir(), key=lambda p: p.name):
                dest = target_dir / item.name
                if dest.exists():
                    continue
                shutil.move(str(item), str(dest))
            shutil.rmtree(child, ignore_errors=True)
            return
