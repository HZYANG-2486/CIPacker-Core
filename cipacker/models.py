"""数据模型：包清单、文件条目、分层、插件信息、体检报告。

全部使用冻结 dataclass，便于序列化与比较。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from . import __version__

# --------------------------------------------------------------------------- #
# 分层
# --------------------------------------------------------------------------- #


class LayerName(str, Enum):
    """迁移分层：按语义把数据根内容分组，支持选择性迁移。"""

    SETTINGS = "settings"
    PROFILES = "profiles"
    PLUGINS = "plugins"
    PLUGIN_CONFIGS = "plugin-configs"
    AUTOMATION = "automation"

    def __str__(self) -> str:  # pragma: no cover - 便于 argparse 显示
        return self.value


# --------------------------------------------------------------------------- #
# 文件条目
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FileEntry:
    """包内单个文件的元信息。"""

    path: str  # 包内 POSIX 路径（同时作为排序键）
    size: int
    sha256: str
    layer: LayerName
    mode: int = 0o644

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["layer"] = self.layer.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileEntry:
        payload = dict(data)
        payload["layer"] = LayerName(payload.get("layer", LayerName.SETTINGS.value))
        return cls(**payload)


@dataclass(frozen=True)
class PackageFingerprint:
    """包指纹。

    ``content_sha256`` 基于**内容清单**（路径+大小+内容哈希），与压缩参数和
    时间戳无关，因此同一份配置在任何机器、任何时间打包结果恒等——这使得
    「包去重」和「往返一致性校验」成为可能。

    ``archive_sha256`` 是 zip 文件本身的字节哈希，仅在确定性模式下有意义，
    存放于**旁挂** ``.sha256`` 文件而非包内（避免自指悖论）。
    """

    content_sha256: str
    archive_sha256: str | None = None
    file_count: int = 0
    total_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_sha256": self.content_sha256,
            "archive_sha256": self.archive_sha256,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PackageFingerprint:
        return cls(
            content_sha256=data.get("content_sha256", ""),
            archive_sha256=data.get("archive_sha256"),
            file_count=int(data.get("file_count", 0)),
            total_bytes=int(data.get("total_bytes", 0)),
        )


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PluginDependency:
    """插件依赖项（对应 PluginDependency.cs）。"""

    id: str
    is_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "isRequired": self.is_required}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginDependency:
        return cls(
            id=str(data.get("id", "")),
            is_required=bool(data.get("isRequired", data.get("is_required", True))),
        )


@dataclass
class PluginInfo:
    """已安装插件的信息（聚合 manifest + 本地状态标记）。"""

    id: str
    name: str = ""
    version: str = "unknown"
    api_version: str = "unknown"
    description: str = ""
    author: str = ""
    entrance_assembly: str = ""
    supported_os: list[str] = field(default_factory=list)
    dependencies: list[PluginDependency] = field(default_factory=list)
    folder: str = ""
    enabled: bool = True
    uninstalling: bool = False
    parse_ok: bool = True
    parse_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "apiVersion": self.api_version,
            "description": self.description,
            "author": self.author,
            "entranceAssembly": self.entrance_assembly,
            "supportedOSPlatforms": list(self.supported_os),
            "dependencies": [d.to_dict() for d in self.dependencies],
            "folder": self.folder,
            "enabled": self.enabled,
            "uninstalling": self.uninstalling,
            "parse_ok": self.parse_ok,
            "parse_warnings": list(self.parse_warnings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginInfo:
        deps_raw = data.get("dependencies") or []
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            version=str(data.get("version", "unknown")),
            api_version=str(data.get("apiVersion", data.get("api_version", "unknown"))),
            description=str(data.get("description", "")),
            author=str(data.get("author", "")),
            entrance_assembly=str(
                data.get("entranceAssembly", data.get("entrance_assembly", ""))
            ),
            supported_os=list(
                data.get("supportedOSPlatforms", data.get("supported_os", [])) or []
            ),
            dependencies=[
                PluginDependency.from_dict(d) for d in deps_raw if isinstance(d, dict)
            ],
            folder=str(data.get("folder", "")),
            enabled=bool(data.get("enabled", True)),
            uninstalling=bool(data.get("uninstalling", False)),
            parse_ok=bool(data.get("parse_ok", True)),
            parse_warnings=list(data.get("parse_warnings", []) or []),
        )


# --------------------------------------------------------------------------- #
# 体检
# --------------------------------------------------------------------------- #


class Severity(str, Enum):
    """体检发现级别。"""

    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    def __str__(self) -> str:  # pragma: no cover
        return self.value


_SEVERITY_ORDER = {
    Severity.PASS: 0,
    Severity.INFO: 1,
    Severity.WARNING: 2,
    Severity.ERROR: 3,
}


def severity_rank(severity: Severity) -> int:
    return _SEVERITY_ORDER.get(severity, 1)


@dataclass
class Finding:
    """一条体检发现。"""

    rule_id: str
    severity: Severity
    message: str
    location: str = ""
    suggestion: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        payload = dict(data)
        payload["severity"] = Severity(payload.get("severity", "info"))
        return cls(**payload)


@dataclass
class HealthReport:
    """体检报告。"""

    generated_at: str = ""
    root: str = ""
    layout: str = "unknown"
    ci_version: str | None = None
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    score: int = 100

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def sort(self) -> None:
        self.findings.sort(
            key=lambda f: (-severity_rank(f.severity), f.rule_id, f.location)
        )

    def compute(self) -> HealthReport:
        """重算计数与健康分（100 分制，error 扣 25、warning 扣 8、info 扣 1）。"""
        self.counts = {}
        for finding in self.findings:
            key = finding.severity.value
            self.counts[key] = self.counts.get(key, 0) + 1

        penalty = (
            self.counts.get(Severity.ERROR.value, 0) * 25
            + self.counts.get(Severity.WARNING.value, 0) * 8
            + self.counts.get(Severity.INFO.value, 0) * 1
        )
        self.score = max(0, 100 - penalty)
        return self

    @property
    def has_errors(self) -> bool:
        return any(f.severity is Severity.ERROR for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "root": self.root,
            "layout": self.layout,
            "ci_version": self.ci_version,
            "score": self.score,
            "counts": dict(self.counts),
            "findings": [f.to_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------- #
# 包清单
# --------------------------------------------------------------------------- #


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 表示（秒精度，便于确定性比较）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


MANIFEST_NAME = "__manifest__.json"
LEGACY_MANIFEST_NAME = "__manifest__.json"

#: 当前包格式版本。用于 unpack/verify 判断兼容性。
FORMAT_VERSION = "3.0"

#: 内部使用的确定性时间戳（ZIP 最早可表示时间）。
DETERMINISTIC_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


@dataclass
class Manifest:
    """包清单。"""

    format_version: str = FORMAT_VERSION
    tool: str = "CIPacker"
    tool_version: str = __version__
    created_at: str = ""
    ci_structure: str = "unknown"
    ci_version: str | None = None
    source_platform: str = ""
    layers: list[LayerName] = field(default_factory=list)
    include_backups: bool = False
    deterministic: bool = True
    redacted: list[str] = field(default_factory=list)
    file_count: int = 0
    total_bytes: int = 0
    files: list[FileEntry] = field(default_factory=list)
    plugins: list[PluginInfo] = field(default_factory=list)
    fingerprint: PackageFingerprint | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "created_at": self.created_at,
            "ci_structure": self.ci_structure,
            "ci_version": self.ci_version,
            "source_platform": self.source_platform,
            "layers": [layer.value for layer in self.layers],
            "include_backups": self.include_backups,
            "deterministic": self.deterministic,
            "redacted": list(self.redacted),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "files": [entry.to_dict() for entry in self.files],
            "plugins": [plugin.to_dict() for plugin in self.plugins],
            "fingerprint": self.fingerprint.to_dict() if self.fingerprint else None,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        files_raw = data.get("files") or []
        plugins_raw = data.get("plugins") or []
        layers_raw = data.get("layers") or []
        fp_raw = data.get("fingerprint")

        layers: list[LayerName] = []
        for item in layers_raw:
            try:
                layers.append(LayerName(item))
            except ValueError:
                continue

        return cls(
            format_version=str(data.get("format_version", data.get("version", "2.0"))),
            tool=str(data.get("tool", "CIPacker")),
            tool_version=str(data.get("tool_version", "unknown")),
            created_at=str(data.get("created_at", "")),
            ci_structure=str(data.get("ci_structure", "unknown")),
            ci_version=data.get("ci_version"),
            source_platform=str(data.get("source_platform", "")),
            layers=layers,
            include_backups=bool(data.get("include_backups", False)),
            deterministic=bool(data.get("deterministic", True)),
            redacted=list(data.get("redacted", []) or []),
            file_count=int(data.get("file_count", len(files_raw))),
            total_bytes=int(data.get("total_bytes", 0)),
            files=[
                FileEntry.from_dict(entry)
                for entry in files_raw
                if isinstance(entry, dict)
            ],
            plugins=[
                PluginInfo.from_dict(plugin)
                for plugin in plugins_raw
                if isinstance(plugin, dict)
            ],
            fingerprint=PackageFingerprint.from_dict(fp_raw) if fp_raw else None,
        )

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        return cls.from_dict(json.loads(text))
