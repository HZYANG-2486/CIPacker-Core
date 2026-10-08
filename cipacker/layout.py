"""ClassIsland 目录结构的统一抽象。

本模块是**唯一**进行 V1 / V2 结构判定的地方。其余所有模块只消费
:class:`Layout` 提供的路径属性，内部不得再出现 ``if kind == "v1"`` 之类的分支。

结构依据（均来自 ClassIsland 官方源码核实）：

``ClassIsland.Core/CommonDirectories.cs``::

    AppPackageRoot     = "./"
    AppRootFolderPath  = "./"                              # ★ 数据根
    AppConfigPath      => AppRootFolderPath + "/Config"
    AppLogFolderPath   => AppRootFolderPath + "/Logs"
    AppCacheFolderPath => AppRootFolderPath + "/Cache"
    AppTempFolderPath  => AppRootFolderPath + "/Temp"

数据根布局::

    AppRootFolderPath/
    ├── Settings.json      ← 应用设置（含 LastAppVersion）
    ├── Profiles/          ← 档案：课表/时间表/科目
    ├── Plugins/           ← 插件二进制（每插件一目录，目录名 = manifest.id）
    ├── Backups/           ← 自动备份 zip
    ├── Config/            ← 应用其它配置
    │   ├── PluginsIndex/  ← 插件市场元数据缓存（无需迁移）
    │   ├── Plugins/       ← ★ 各插件自身的设置
    │   └── Automation/    ← 自动化规则
    ├── Logs/              ← 不迁移
    ├── Cache/             ← 不迁移
    └── Temp/              ← 不迁移

CI 2.x 为三层安装（issue #1449 诊断实锤）::

    AppPackageRoot/
    ├── ClassIsland(.exe)          ← launcher
    ├── app-1.7.106.0-0/           ← 版本化程序目录（打包必须排除）
    └── data/                      ← ★ AppRootFolderPath

CI 1.x 无 ``data/`` 层，``Settings.json`` 与 ``Config/`` 同在根目录。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import logui

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 数据根下恒不迁移的目录名。
EXCLUDED_DATA_DIRS = frozenset({"Cache", "Logs", "Temp"})

#: ``Config/`` 下无需迁移的子目录（市场元数据缓存）。
EXCLUDED_CONFIG_DIRS = frozenset({"PluginsIndex"})

#: 顶层恒不迁移的目录名前缀（版本化程序目录等）。
EXCLUDED_TOP_PREFIXES = ("app-", "old", "__ci_")

#: 不迁移的文件扩展名（调试符号、压缩副产物）。
EXCLUDED_FILE_SUFFIXES = frozenset({".pdb"})

#: 程序本体文件名（绝不进入配置包），大小写不敏感。
PROGRAM_BINARIES = frozenset(
    {
        "classisland.exe",
        "classisland",
        "classisland.dll",
        "classisland.launcher.exe",
        "classisland.launcher",
        "classisland.desktop.dll",
    }
)

#: 数据根内的关键文件名。
SETTINGS_FILENAME = "Settings.json"
FILES_MANIFEST_FILENAME = "files.json"
BACKUPS_DIRNAME = "Backups"

#: 版本化程序目录命名模式，例如 ``app-1.7.106.0-0``。
_VERSION_DIR_RE = re.compile(r"^app-.+-\d+$", re.IGNORECASE)

#: 版本号提取。
_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?\b")


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


class LayoutKind(str, Enum):
    """ClassIsland 目录结构类型。"""

    V1 = "v1"
    V2 = "v2"
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # pragma: no cover
        return self.value


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Layout:
    """ClassIsland 安装/数据目录的结构描述。

    :param kind: 结构类型。
    :param package_root: 用户传入的路径（程序包根）。
    :param app_root: **数据根**——``Settings.json`` 所在目录。
    :param version_dir: V2 的版本化程序目录（``app-<ver>-<n>``），V1 为 None。
    """

    kind: LayoutKind
    package_root: Path
    app_root: Path
    version_dir: Path | None = None

    # -- 关键路径 ---------------------------------------------------------- #

    @property
    def settings_file(self) -> Path:
        return self.app_root / SETTINGS_FILENAME

    @property
    def profiles_dir(self) -> Path:
        return self.app_root / "Profiles"

    @property
    def plugins_dir(self) -> Path:
        return self.app_root / "Plugins"

    @property
    def config_dir(self) -> Path:
        return self.app_root / "Config"

    @property
    def plugin_configs_dir(self) -> Path:
        """插件各自设置的目录（``Config/Plugins/``）。"""
        return self.config_dir / "Plugins"

    @property
    def automation_dir(self) -> Path:
        return self.config_dir / "Automation"

    @property
    def backups_dir(self) -> Path:
        return self.app_root / BACKUPS_DIRNAME

    @property
    def is_known(self) -> bool:
        return self.kind is not LayoutKind.UNKNOWN

    # -- 路径映射 ---------------------------------------------------------- #

    def to_archive_name(self, rel: Path | str) -> str:
        """把「相对数据根的路径」转换为包内 POSIX 路径。

        包内路径**始终以数据根为基准**，不含 V2 的 ``data/`` 前缀——
        前缀属于安装布局知识，不应固化进迁移包。
        """
        rel_posix = Path(rel).as_posix().lstrip("/")
        return rel_posix

    def to_target_path(self, archive_name: str, target: Layout) -> Path:
        """把包内路径映射到目标安装的磁盘路径。

        包内路径相对数据根，因此直接拼接到目标数据根即可——
        V1 与 V2 的差异被数据根吸收，无需任何分支。
        """
        rel = archive_name.replace("\\", "/").lstrip("/")
        return target.app_root / rel

    def matches(self, other: Layout) -> bool:
        """两个布局是否结构兼容（同类型，或任一方未知）。"""
        if self.kind is LayoutKind.UNKNOWN or other.kind is LayoutKind.UNKNOWN:
            return True
        return self.kind is other.kind

    def describe(self) -> str:
        if self.kind is LayoutKind.UNKNOWN:
            return "未知结构"
        return f"{self.kind.value} ({self.app_root})"

    # -- 有效性 ------------------------------------------------------------ #

    def is_valid_install(self) -> bool:
        """数据根是否像一个可用的 ClassIsland 安装。"""
        if not self.app_root.is_dir():
            return False
        # 有 Settings.json 是最强信号
        if self.settings_file.is_file():
            return True
        # 退一步：Profiles/ 与 Config/ 同时存在也认为有效
        return self.profiles_dir.is_dir() and self.config_dir.is_dir()

    def has_existing_config(self) -> bool:
        """数据根是否已有用户配置（用于解包前的覆盖确认）。"""
        return self.settings_file.is_file()

    def __str__(self) -> str:  # pragma: no cover
        return self.describe()


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #


def _read_settings_version(path: Path) -> str | None:
    """从 Settings.json 读取 ``LastAppVersion``。"""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        version = data.get("LastAppVersion")
        if version is not None:
            return str(version)
    return None


def _read_files_json_version(root: Path) -> str | None:
    """回退方案：从 ``files.json`` 推断版本。"""
    path = root / FILES_MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        for key in ("version", "Version"):
            if data.get(key) is not None:
                return str(data[key])

    match = _VERSION_RE.search(content)
    if match:
        return match.group(0)
    return None


def extract_major_version(version: str | None) -> int | None:
    """从版本字符串提取主版本号。"""
    if not version:
        return None
    match = _VERSION_RE.search(str(version))
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def detect_ci_version(app_root: Path) -> str | None:
    """探测数据根对应的 ClassIsland 版本。"""
    version = _read_settings_version(app_root / SETTINGS_FILENAME)
    if version:
        return version
    return _read_files_json_version(app_root)


def _looks_like_version_dir(path: Path) -> bool:
    name = path.name
    if _VERSION_DIR_RE.match(name):
        return True
    # 有的发行版直接叫 app
    return name.lower() == "app"


def _find_version_dir(package_root: Path) -> Path | None:
    """在程序包根下寻找版本化程序目录。"""
    try:
        children = sorted(package_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return None

    candidates = [p for p in children if p.is_dir() and _looks_like_version_dir(p)]
    if not candidates:
        return None
    # 多个时取名称最大的（版本号最新的通常排最后）
    return candidates[-1]


def _detect_under(package_root: Path) -> Layout | None:
    """在给定程序包根下尝试识别结构。"""
    data_dir = package_root / "data"

    # --- V2 形态一：包根下直接有 data/ ---
    if data_dir.is_dir():
        has_settings = (data_dir / SETTINGS_FILENAME).is_file()
        has_dirs = (data_dir / "Profiles").is_dir() or (data_dir / "Config").is_dir()
        if has_settings or has_dirs:
            return Layout(
                kind=LayoutKind.V2,
                package_root=package_root,
                app_root=data_dir,
                version_dir=_find_version_dir(package_root),
            )

    # --- V2 形态二：包根下有 app-*/data/ ---
    version_dir = _find_version_dir(package_root)
    if version_dir is not None:
        nested_data = version_dir / "data"
        if nested_data.is_dir() and (
            (nested_data / SETTINGS_FILENAME).is_file()
            or (nested_data / "Profiles").is_dir()
        ):
            return Layout(
                kind=LayoutKind.V2,
                package_root=package_root,
                app_root=nested_data,
                version_dir=version_dir,
            )

    # --- V2 形态三：用户直接指向 data/ 目录 ---
    # 特征是「本身叫 data」且内部有 Profiles/Config，同时父目录存在程序线索。
    if package_root.name.lower() == "data":
        has_settings = (package_root / SETTINGS_FILENAME).is_file()
        has_profiles = (package_root / "Profiles").is_dir()
        if has_settings and has_profiles:
            parent = package_root.parent
            return Layout(
                kind=LayoutKind.V2,
                package_root=parent,
                app_root=package_root,
                version_dir=_find_version_dir(parent),
            )

    # --- V1：根目录直接放 Settings.json / Config + Profiles ---
    has_settings = (package_root / SETTINGS_FILENAME).is_file()
    has_config = (package_root / "Config").is_dir()
    has_profiles = (package_root / "Profiles").is_dir()
    if has_settings or (has_config and has_profiles):
        return Layout(
            kind=LayoutKind.V1,
            package_root=package_root,
            app_root=package_root,
            version_dir=None,
        )

    return None


def _probe_candidates(root: Path) -> list[Path]:
    """构造结构探测的候选路径列表（按优先级）。

    顺序很重要：先按用户传入的路径判定，只有在无法识别时才回退到
    ``ClassIsland_PackageRoot`` 与环境线索。否则 ``<root>/data`` 会被
    优先识别成 V1（因为 data/ 里也有 Settings.json），从而掩盖 V2 语义。
    """
    candidates: list[Path] = [root]

    # 环境变量显式指定（issue #1449：从命令行启动时数据目录可能落在 cwd）
    override = os.environ.get("ClassIsland_PackageRoot")
    if override:
        override_path = Path(override).expanduser()
        if override_path not in candidates:
            candidates.append(override_path)

    # 当前工作目录（用户可能在任意目录执行 start）
    cwd = Path.cwd()
    if cwd not in candidates:
        candidates.append(cwd)

    return candidates


def detect_layout(root: Path | str) -> Layout:
    """探测给定路径的 ClassIsland 结构。

    这是**唯一**的结构判定入口。

    探测顺序：
      1. ``<root>/data/`` 且含有数据 → V2，数据根为 ``<root>/data``
      2. ``<root>/app-*/data/`` → V2，数据根为嵌套的 data
      3. ``<root>/Settings.json`` 或 ``<root>/Config`` + ``Profiles`` → V1
      4. 通过 ``ClassIsland_PackageRoot`` 环境变量与 cwd 兜底重试
      5. 读 ``Settings.json.LastAppVersion`` 交叉校验主版本号
      6. 均不匹配 → UNKNOWN（保留旧行为：不阻断，仅提示）
    """
    package_root = Path(root).expanduser()
    try:
        package_root = package_root.resolve()
    except OSError:
        package_root = package_root.absolute()

    for candidate in _probe_candidates(package_root):
        if not candidate.is_dir():
            continue
        layout = _detect_under(candidate)
        if layout is not None:
            return _cross_check(layout, root_hint=package_root)

    return Layout(
        kind=LayoutKind.UNKNOWN,
        package_root=package_root,
        app_root=package_root,
        version_dir=None,
    )


def _cross_check(layout: Layout, root_hint: Path) -> Layout:
    """用 ``Settings.json`` 中的版本号对结构判定做交叉校验。

    结构特征与版本号冲突时**以版本号为准**（版本号是应用自己写的，更权威），
    并在数据根不变的前提下修正 ``kind``。
    """
    version = detect_ci_version(layout.app_root)
    major = extract_major_version(version)
    if major is None:
        return layout

    expected = LayoutKind.V1 if major == 1 else LayoutKind.V2 if major >= 2 else None
    if expected is None or expected is layout.kind:
        return layout

    # 版本号为 1.x 但结构看起来像 V2（有 data/）时，说明是 1.7+ 的便携布局，
    # 此时数据根判定仍然正确，只是 kind 需要修正。
    logui.warn(
        f"结构特征与版本号不一致（结构={layout.kind.value}, "
        f"版本={version}），以版本号为准判定为 {expected.value}"
    )
    return Layout(
        kind=expected,
        package_root=layout.package_root,
        app_root=layout.app_root,
        version_dir=layout.version_dir,
    )


# --------------------------------------------------------------------------- #
# 排除规则
# --------------------------------------------------------------------------- #


def is_excluded_dir(rel_parts: tuple[str, ...], kind: LayoutKind) -> bool:
    """判断（相对数据根的）目录是否应被排除。

    这是**唯一**的目录排除判定入口；结果与 ``kind`` 无关的规则在前，
    与 ``kind`` 相关的规则集中在末尾，避免调用方重复分支。
    """
    if not rel_parts:
        return False

    top = rel_parts[0]

    # 版本化程序目录 / 旧版本目录 / 内部临时目录
    lowered = top.lower()
    for prefix in EXCLUDED_TOP_PREFIXES:
        if lowered.startswith(prefix):
            return True

    # 数据根下的易失目录
    if top in EXCLUDED_DATA_DIRS:
        return True

    # 仅 __init__ 等内部目录
    if top.startswith(".") and top not in (".config",):
        return True

    # Config/ 下的市场元数据缓存
    if top == "Config" and len(rel_parts) >= 2 and rel_parts[1] in EXCLUDED_CONFIG_DIRS:
        return True

    return False


def is_excluded_file(rel_parts: tuple[str, ...], name: str) -> bool:
    """判断文件是否应被排除（``rel_parts`` 含文件名）。"""
    suffix = Path(name).suffix.lower()
    if suffix in EXCLUDED_FILE_SUFFIXES:
        return True

    # 程序本体（仅当位于数据根顶层时排除；插件目录下的同名文件不在此列）
    if len(rel_parts) == 1 and name.lower() in PROGRAM_BINARIES:
        return True

    # 内部临时/锁文件
    if name.endswith((".tmp", ".part", ".lock")) and name != "Settings.json":
        return True

    return False


def should_include_backups(rel_parts: tuple[str, ...]) -> bool:
    """目录是否属于 ``Backups/``（受 ``--include-backups`` 控制）。"""
    return bool(rel_parts) and rel_parts[0] == BACKUPS_DIRNAME
