"""迁移分层定义。

把数据根内容按**语义**分组，使「只迁移某几部分」成为可能——
例如只带走课表档案而不带走插件二进制，或只带走设置。

层与层之间的依赖是**软依赖**：选择了依赖方但未选择被依赖方时
由 :mod:`cipacker.doctor` 给出提示，而不是硬性拒绝，
因为「只要设置、不要插件」本身是合法诉求。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .models import LayerName

# --------------------------------------------------------------------------- #
# 层规格
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LayerSpec:
    """单个迁移层的定义。"""

    name: LayerName
    label: str
    description: str
    #: 匹配规则：``(前缀, 是否精确匹配)``
    matchers: tuple[tuple[str, bool], ...]
    soft_requires: tuple[LayerName, ...] = field(default=())

    def matches(self, rel_posix: str) -> bool:
        path = PurePosixPath(rel_posix)
        parts = path.parts
        if not parts:
            return False

        for prefix, exact in self.matchers:
            prefix_parts = PurePosixPath(prefix).parts
            if exact:
                if parts == prefix_parts:
                    return True
            else:
                if len(parts) >= len(prefix_parts) and parts[: len(prefix_parts)] == prefix_parts:
                    return True
        return False


#: 层注册表。**顺序即匹配优先级**——越靠前越先匹配。
LAYER_REGISTRY: tuple[LayerSpec, ...] = (
    LayerSpec(
        name=LayerName.PLUGIN_CONFIGS,
        label="插件设置",
        description="各插件自身的配置（Config/Plugins/）",
        matchers=(("Config/Plugins", False),),
        soft_requires=(LayerName.PLUGINS,),
    ),
    LayerSpec(
        name=LayerName.AUTOMATION,
        label="自动化",
        description="自动化规则与触发器配置（Config/Automation/）",
        matchers=(
            ("Config/Automation", False),
            ("Config/Automation.json", True),
        ),
        soft_requires=(LayerName.PROFILES,),
    ),
    LayerSpec(
        name=LayerName.SETTINGS,
        label="应用设置",
        description="应用全局设置（Settings.json）",
        matchers=(("Settings.json", True),),
    ),
    LayerSpec(
        name=LayerName.PROFILES,
        label="档案",
        description="课表、时间表、科目等档案（Profiles/）",
        matchers=(("Profiles", False),),
    ),
    LayerSpec(
        name=LayerName.PLUGINS,
        label="插件",
        description="插件本体与启用状态（Plugins/）",
        matchers=(("Plugins", False),),
    ),
)

_LAYER_BY_NAME = {spec.name: spec for spec in LAYER_REGISTRY}

#: 未匹配到任何层的文件所归属的兜底层。
FALLBACK_LAYER = LayerName.SETTINGS

#: 默认选中的层（等价于旧版的「全量打包」行为）。
DEFAULT_LAYERS: tuple[LayerName, ...] = (
    LayerName.SETTINGS,
    LayerName.PROFILES,
    LayerName.PLUGINS,
    LayerName.PLUGIN_CONFIGS,
    LayerName.AUTOMATION,
)


def all_layer_names() -> list[LayerName]:
    return [spec.name for spec in LAYER_REGISTRY]


def layer_spec(name: LayerName) -> LayerSpec:
    return _LAYER_BY_NAME[name]


def layer_label(name: LayerName) -> str:
    spec = _LAYER_BY_NAME.get(name)
    return spec.label if spec else name.value


def assign_layer(rel_posix: str) -> LayerName:
    """把相对数据根的 POSIX 路径归入某个迁移层。

    按 :data:`LAYER_REGISTRY` 的声明顺序做前缀匹配，第一个命中的胜出。
    """
    for spec in LAYER_REGISTRY:
        if spec.matches(rel_posix):
            return spec.name
    return FALLBACK_LAYER


# --------------------------------------------------------------------------- #
# 选择解析
# --------------------------------------------------------------------------- #


@dataclass
class LayerSelection:
    """一次打包的层选择结果。"""

    selected: list[LayerName] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def includes(self, name: LayerName) -> bool:
        return name in self.selected


def parse_layer_names(raw: str | None) -> list[LayerName]:
    """解析逗号分隔的层名列表。

    :param raw: 例如 ``"settings,profiles"``；``"all"`` 或 None 表示全选。
    :raises ValueError: 含未知层名。
    """
    if raw is None:
        return list(DEFAULT_LAYERS)

    tokens = [token.strip() for token in raw.replace(" ", ",").split(",") if token.strip()]
    if not tokens:
        return list(DEFAULT_LAYERS)

    if any(token.lower() in ("all", "*") for token in tokens):
        return list(DEFAULT_LAYERS)

    resolved: list[LayerName] = []
    for token in tokens:
        try:
            name = LayerName(token)
        except ValueError:
            valid = ", ".join(layer.value for layer in all_layer_names())
            raise ValueError(
                f"未知的迁移层 {token!r}；可用层：{valid}，或 all"
            ) from None
        if name not in resolved:
            resolved.append(name)

    return resolved


def resolve_selection(selected: list[LayerName]) -> LayerSelection:
    """校验层选择并生成软依赖提示。"""
    result = LayerSelection(selected=list(selected))

    for name in result.selected:
        spec = _LAYER_BY_NAME.get(name)
        if spec is None:
            continue
        for required in spec.soft_requires:
            if required not in result.selected:
                result.warnings.append(
                    f"已选择「{spec.label}」但未选择其依赖的"
                    f"「{layer_label(required)}」——目标环境可能需要额外处理"
                )

    return result


def describe_layers() -> list[str]:
    """生成层说明文本（供 CLI ``--help`` 与日志展示）。"""
    lines: list[str] = []
    for spec in LAYER_REGISTRY:
        dep = ""
        if spec.soft_requires:
            dep = "（依赖: " + ", ".join(layer_label(n) for n in spec.soft_requires) + "）"
        lines.append(f"  {spec.name.value:<15} {spec.label} — {spec.description}{dep}")
    return lines
