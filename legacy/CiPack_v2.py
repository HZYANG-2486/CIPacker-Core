#!/usr/bin/env python3
import argparse
import os
import sys
import zipfile
import json
import re
import urllib.request, urllib.error
import shutil
import uuid
import platform
import hashlib
import time
import subprocess
from pathlib import Path
from datetime import datetime

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

# 结构化输出模式（GUI 外壳解析用）
STRUCTURED_MODE = False


def emit(*pairs, **kwargs):
    """
    统一输出接口。
    人类模式（STRUCTURED_MODE=False）: 静默，不输出任何内容
    结构化模式（STRUCTURED_MODE=True）: 输出 [STRUCTURED] key=value 到 stderr

    用法:
        emit("stage=download")
        emit("stage=download", "progress=0.0")
        emit(stage="download", progress=0.0)
        emit("level=warning", message="文件不存在")
    """
    if not STRUCTURED_MODE:
        return
    parts = []
    for p in pairs:
        parts.append(str(p))
    for k, v in kwargs.items():
        if v is None:
            continue
        parts.append(f"{k}={v}")
    if not parts:
        return
    line = " ".join(parts)
    print(f"[STRUCTURED] {line}", file=sys.stderr, flush=True)


def ask(prompt: str, default: str | None = None, choices: list[str] | None = None) -> str:
    """
    询问包装函数。
    人类模式：直接调用 input()
    结构化模式：先 emit level=ask 事件，再调用 input()
    GUI 外壳可通过 stdin 注入回答，或提供 --yes 自动回答。
    """
    if STRUCTURED_MODE:
        kwargs = {"level": "ask", "message": prompt, "default": default}
        if choices:
            kwargs["choices"] = ",".join(choices)
        emit(**kwargs)
    try:
        return input(prompt)
    except EOFError:
        return default or ""

EXCLUDE_DIRS = {
    "app-",
    "old",
}

EXCLUDE_DATA_DIRS = {
    "Cache",
    "Logs",
    "Temp",
}

EXCLUDE_V1_TOP_DIRS = {
    "Cache",
    "Logs",
    "Temp",
    "Backups",
}

EXCLUDE_FILE_EXTENSIONS = {".pdb", ".gz"}

ROOT_ONLY_DATA = True


def parse_yaml_simple(content: str) -> dict:
    result = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip().strip("`'\"")
            if value:
                result[key] = value
    return result


def detect_ci_version(root: str) -> str | None:
    # 优先: 从 Settings.json 的 LastAppVersion 获取版本
    # CI 2.x: Settings.json 在 data/ 目录下
    # CI 1.x: Settings.json 在根目录
    settings_paths = [
        os.path.join(root, "data", "Settings.json"),
        os.path.join(root, "Settings.json"),
    ]
    for settings_path in settings_paths:
        if os.path.isfile(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                version = data.get("LastAppVersion")
                if version is not None:
                    return version
                print("警告: Settings.json 中未找到 LastAppVersion 字段", file=sys.stderr)
            except json.JSONDecodeError as e:
                print(f"警告: Settings.json 解析失败: {e}", file=sys.stderr)
            except OSError as e:
                print(f"警告: 读取 Settings.json 失败: {e}", file=sys.stderr)
            continue
    else:
        print("警告: 未找到 Settings.json (已搜索根目录和 data/ 目录)", file=sys.stderr)

    # 回退: 从 files.json 解析版本信息
    files_json_path = os.path.join(root, "files.json")
    if os.path.isfile(files_json_path):
        try:
            with open(files_json_path, "r", encoding="utf-8") as f:
                content = f.read()
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    version = data.get("version")
                    if version is None:
                        version = data.get("Version")
                    if version is not None:
                        return version
            except json.JSONDecodeError:
                pass
            # 尝试从文件内容中查找版本模式 (如 x.x.x.x)
            match = re.search(r"\b(\d+\.\d+\.\d+(?:\.\d+)?)\b", content)
            if match:
                return match.group(1)
        except OSError as e:
            print(f"警告: 读取 files.json 失败: {e}", file=sys.stderr)

    print("警告: 无法从 Settings.json 或 files.json 获取版本信息", file=sys.stderr)
    return None


def detect_ci_structure(root: str) -> str:
    has_data = os.path.isdir(os.path.join(root, "data"))
    if has_data:
        return "v2"
    has_config = os.path.isdir(os.path.join(root, "Config"))
    has_profiles = os.path.isdir(os.path.join(root, "Profiles"))
    if has_config and has_profiles:
        return "v1"
    return "unknown"


def detect_version_from_zip(zip_path: str) -> dict:
    """
    检测 zip 文件的格式类型和 CI 版本结构。
    返回: {"format": "own"|"official"|"unknown", "ci_structure": "v1"|"v2"|"unknown", "ci_version": str|None}
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            
            # 自有格式: 有 __manifest__.json
            if "__manifest__.json" in names:
                with zf.open("__manifest__.json") as f:
                    manifest = json.load(f)
                return {
                    "format": "own",
                    "ci_structure": manifest.get("ci_structure", "unknown"),
                    "ci_version": manifest.get("ci_version"),
                }
            
            # 官方格式: 无 manifest，通过文件结构推断
            has_data_dir = any(n.startswith("data/") for n in names)
            has_settings_json = any(n == "Settings.json" or n == "data/Settings.json" for n in names)
            has_config = any(n.startswith("Config/") for n in names)
            has_profiles = any(n.startswith("Profiles/") for n in names)

            if has_data_dir:
                return {"format": "official", "ci_structure": "v2"}

            if has_settings_json or (has_config and has_profiles):
                # 尝试读取 Settings.json 中的版本信息
                version = None
                if "Settings.json" in names:
                    try:
                        with zf.open("Settings.json") as f:
                            data = json.load(f)
                        version = data.get("LastAppVersion")
                    except Exception:
                        pass
                
                if version:
                    v_str = str(version)
                    if v_str.startswith("1."):
                        return {"format": "official", "ci_structure": "v1", "ci_version": version}
                    elif v_str.startswith("2."):
                        return {"format": "official", "ci_structure": "v2", "ci_version": version}
                
                # 无法从版本号判断时，默认 v2（更常见），但保留 v1 特征
                # 如果存在 v2 特有的文件/目录则推断为 v2
                has_v2_features = any(
                    n.startswith("Application") or n.startswith("ExactTimeServerConfigures.json")
                    for n in names
                )
                if has_v2_features:
                    return {"format": "official", "ci_structure": "v2", "ci_version": version}
                
                # 有 Config/ + Profiles/ 但无明显 v2 特征，默认返回 v2（官方格式主要在当前使用）
                # 但保留无版本的情况供后续判断
                return {"format": "official", "ci_structure": "v2", "ci_version": version}
            
            return {"format": "unknown", "ci_structure": "unknown", "ci_version": None}
    except Exception as e:
        print(f"警告: 检测 zip 格式失败: {e}", file=sys.stderr)
        return {"format": "unknown", "ci_structure": "unknown", "ci_version": None}


def collect_plugin_info(root: str) -> list[dict]:
    plugins = []
    structure = detect_ci_structure(root)
    if structure == "v1":
        plugins_dir = os.path.join(root, "Plugins")
    else:
        plugins_dir = os.path.join(root, "data", "Plugins")
    if not os.path.isdir(plugins_dir):
        return plugins

    for plugin_name in os.listdir(plugins_dir):
        plugin_path = os.path.join(plugins_dir, plugin_name)
        if not os.path.isdir(plugin_path):
            continue

        manifest_path = os.path.join(plugin_path, "manifest.yml")
        if not os.path.isfile(manifest_path):
            continue

        with open(manifest_path, "r", encoding="utf-8") as f:
            content = f.read()

        info = parse_yaml_simple(content)
        plugins.append({
            "id": info.get("id", plugin_name),
            "name": info.get("name", plugin_name),
            "version": info.get("version", "unknown"),
            "apiVersion": info.get("apiVersion", "unknown"),
            "path": plugin_name,
        })

    return plugins


def should_exclude_dir(dirpath: str, root: str, structure: str = "v2") -> bool:
    rel = os.path.relpath(dirpath, root)
    parts = Path(rel).parts

    if not parts:
        return False

    top = parts[0]
    if any(top.startswith(prefix) for prefix in EXCLUDE_DIRS):
        return True

    if structure == "v1":
        if top in EXCLUDE_V1_TOP_DIRS:
            return True
        return False

    if top == "data" and len(parts) >= 2:
        second = parts[1]
        if second in EXCLUDE_DATA_DIRS:
            return True

    return False


def should_exclude_file(filepath: str, root: str, structure: str = "v2") -> bool:
    rel = os.path.relpath(filepath, root)
    parts = Path(rel).parts
    name = os.path.basename(filepath)

    if structure == "v2":
        if ROOT_ONLY_DATA and len(parts) == 1:
            return True

    if os.path.splitext(name)[1] in EXCLUDE_FILE_EXTENSIONS:
        return True

    return False


def collect_files(root: str, include_backups: bool = False) -> list[str]:
    result = []
    root = os.path.abspath(root)
    structure = detect_ci_structure(root)

    for dirpath, dirnames, filenames in os.walk(root):
        if should_exclude_dir(dirpath, root, structure):
            dirnames[:] = []
            continue

        if not include_backups:
            rel = os.path.relpath(dirpath, root)
            parts = Path(rel).parts
            if structure == "v2" and len(parts) >= 2 and parts[0] == "data" and parts[1] == "Backups":
                dirnames[:] = []
                continue
            if structure == "v1" and len(parts) == 1 and parts[0] == "Backups":
                dirnames[:] = []
                continue

        for f in filenames:
            full = os.path.join(dirpath, f)
            if should_exclude_file(full, root, structure):
                continue
            result.append(os.path.relpath(full, root))

    return result





def has_existing_config(root: str) -> bool:
    """检测目标目录是否已有 CI 配置（Settings.json 等）"""
    structure = detect_ci_structure(root)
    if structure == "v2":
        return os.path.isfile(os.path.join(root, "data", "Settings.json"))
    elif structure == "v1":
        return os.path.isfile(os.path.join(root, "Settings.json"))
    return False


def check_ci_running(target: str) -> bool:
    """检测 ClassIsland 是否正在运行"""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq ClassIsland.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            return "ClassIsland.exe" in result.stdout
        except Exception:
            return False
    else:
        try:
            result = subprocess.run(
                ["pgrep", "-f", "ClassIsland"],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False


def is_valid_ci_install(root: str) -> bool:
    if os.path.isdir(os.path.join(root, "data")):
        is_windows = sys.platform == "win32"
        try:
            if is_windows:
                has_exe = any(f.endswith(".exe") for f in os.listdir(root))
                if not has_exe:
                    return False
            else:
                has_config = any(f.endswith((".json", ".yml", ".yaml", ".toml", ".ini", ".cfg")) for f in os.listdir(root))
                if not has_config:
                    return False
        except PermissionError:
            print("错误: 没有权限访问目录,请检查权限", file=sys.stderr)
            return False
        has_files = False
        for dirpath, dirnames, filenames in os.walk(root):
            if filenames:
                has_files = True
                break
        return has_files
    if os.path.isdir(os.path.join(root, "Config")) and os.path.isdir(os.path.join(root, "Profiles")):
        if not os.path.isfile(os.path.join(root, "Settings.json")):
            return False
        try:
            has_files = bool(os.listdir(root))
        except PermissionError:
            print("错误: 没有权限访问目录,请检查权限", file=sys.stderr)
            return False
        if not has_files:
            return False
        return True
    # CI 1.x 单文件安装: 根目录有 ClassIsland.exe，无 data/ 目录
    try:
        entries = os.listdir(root)
    except PermissionError:
        print("错误: 没有权限访问目录,请检查权限", file=sys.stderr)
        return False
    has_exe = ("ClassIsland.exe" in entries) or ("ClassIsland" in entries)
    if has_exe:
        return True
    return False


def check_and_prompt_download(target: str, yes_download: bool = False) -> bool:
    if not os.path.isdir(target):
        try:
            os.makedirs(target)
        except PermissionError:
            print("错误: 没有权限创建目标目录,请检查权限", file=sys.stderr)
            return False
    if is_valid_ci_install(target):
        return True
    print("未检测到 ClassIsland 安装。")
    if yes_download:
        return True
    resp = ask("是否下载 ClassIsland? (y/N) ", default="N").strip().lower()
    if resp in ("y", "yes"):
        return True
    print("已取消下载。请手动安装 ClassIsland 后重试。")
    return False


def fetch_latest_release() -> dict:
    url = "https://api.github.com/repos/ClassIsland/ClassIsland/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "CiPack/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("错误: GitHub API 访问频率受限,请稍后重试", file=sys.stderr)
        elif e.code == 404:
            print("错误: 未找到该版本的发布", file=sys.stderr)
        else:
            print(f"错误: GitHub API 请求失败 (HTTP {e.code})", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"错误: 网络连接失败 - {e.reason}", file=sys.stderr)
        return None
    except TimeoutError:
        print("错误: 请求超时,请检查网络连接", file=sys.stderr)
        return None
    except Exception as e:
        print(f"错误: 获取发布信息失败 - {e}", file=sys.stderr)
        return None


def fetch_release_by_version(tag: str) -> dict:
    url = f"https://api.github.com/repos/ClassIsland/ClassIsland/releases/tags/{tag}"
    req = urllib.request.Request(url, headers={"User-Agent": "CiPack/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("错误: GitHub API 访问频率受限,请稍后重试", file=sys.stderr)
        elif e.code == 404:
            print(f"错误: 未找到版本 {tag} 的发布", file=sys.stderr)
        else:
            print(f"错误: GitHub API 请求失败 (HTTP {e.code})", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"错误: 网络连接失败 - {e.reason}", file=sys.stderr)
        return None
    except TimeoutError:
        print("错误: 请求超时,请检查网络连接", file=sys.stderr)
        return None
    except Exception as e:
        print(f"错误: 获取发布信息失败 - {e}", file=sys.stderr)
        return None


def fetch_all_github_releases(per_page: int = 30) -> list[dict] | None:
    """获取 GitHub Releases 列表"""
    url = f"https://api.github.com/repos/ClassIsland/ClassIsland/releases?per_page={per_page}"
    req = urllib.request.Request(url, headers={"User-Agent": "CiPack/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("错误: GitHub API 访问频率受限,请稍后重试", file=sys.stderr)
        else:
            print(f"错误: GitHub API 请求失败 (HTTP {e.code})", file=sys.stderr)
        return None
    except Exception as e:
        print(f"错误: 获取版本列表失败 - {e}", file=sys.stderr)
        return None


def get_system_os() -> str:
    if sys.platform == "win32":
        return "windows"
    elif sys.platform == "darwin":
        return "macos"
    else:
        return "linux"


def get_system_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x64"
    elif machine in ("x86", "i386", "i686"):
        return "x86"
    elif machine in ("arm64", "aarch64"):
        return "arm64"
    return "x64"


def _score_asset(asset_name: str, target_os: str, target_arch: str) -> int:
    name = asset_name.lower()
    score = 0

    if target_os in name:
        score += 1000
    if target_arch in name:
        score += 500

    if "folder" in name:
        score += 100
    elif "singlefile" in name:
        score += 80

    if "selfcontained" in name:
        score += 50
    elif "full" in name:
        score += 40
    elif "trimmed" in name:
        score += 30

    if name.endswith(".zip"):
        score += 20
    elif name.endswith(".7z"):
        score += 10

    if "app" in name and target_os in name and target_arch in name:
        score += 200

    return score


def select_best_asset(release: dict) -> dict | None:
    assets = release.get("assets", [])
    if not assets:
        return None

    def _is_source_code(asset_name: str) -> bool:
        lower = asset_name.lower()
        return "source code" in lower or "source_code" in lower

    filtered = [a for a in assets if not _is_source_code(a.get("name", ""))]
    if not filtered:
        return None

    target_os = get_system_os()
    target_arch = get_system_arch()

    scored = []
    for asset in filtered:
        name = asset.get("name", "")
        score = _score_asset(name, target_os, target_arch)
        scored.append((score, asset))

    scored.sort(key=lambda x: x[0], reverse=True)

    if scored:
        for score, candidate in scored:
            name = candidate.get("name")
            url = candidate.get("browser_download_url")
            if not name or not url:
                continue
            return {
                "name": name,
                "browser_download_url": url,
                "size": candidate.get("size", 0),
            }

    return None


def _version_key(v: str):
    """将版本号字符串解析为数字元组，用于排序"""
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts)


def fetch_distribution_channels() -> dict | None:
    url = "https://distribution.classisland.tech/api/v1/public/distributions/web"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CiPack/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"错误: 获取分发渠道列表失败 - {e}", file=sys.stderr)
        return None

    if data.get("code", 0) != 0:
        print(f"错误: API 返回错误 - {data.get('message', '')}", file=sys.stderr)
        return None

    content = data.get("content", {})
    raw_channels = content.get("channels", {})
    if not isinstance(raw_channels, dict):
        raw_channels = {}
    default_channel = content.get("defaultChannel")

    channels = {}
    for channel_id, info in raw_channels.items():
        if not isinstance(info, dict):
            continue
        channels[channel_id] = {
            "latestVersionId": info.get("latestVersionId"),
            "latestVersion": info.get("latestVersion"),
            "channelName": info.get("channelName"),
            "channelDescription": info.get("channelDescription"),
        }

    return {"channels": channels, "default_channel": default_channel}


def fetch_distribution_asset(version_id: str, target_os: str, target_arch: str, variant: str = "selfContained_folder") -> dict | None:
    url = f"https://distribution.classisland.tech/api/v1/public/distributions/web/{version_id}/{target_os}_{target_arch}_{variant}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CiPack/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"错误: 获取分发下载链接失败 - {e}", file=sys.stderr)
        return None

    if data.get("code", 0) != 0:
        print(f"错误: API 返回错误 - {data.get('message', '')}", file=sys.stderr)
        return None

    content = data.get("content", {})
    archive_url = content.get("archiveUrl")
    if not archive_url:
        print("错误: 返回数据缺少 archiveUrl", file=sys.stderr)
        return None

    name = archive_url.split("/")[-1].split("?")[0]

    return {
        "url": archive_url,
        "sha512": content.get("archiveSHA512"),
        "version": content.get("version"),
        "name": name,
    }


def verify_sha512(filepath: str, expected_sha512: str) -> bool:
    try:
        h = hashlib.sha512()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        actual = h.hexdigest()
        return actual.lower() == (expected_sha512 or "").lower()
    except Exception as e:
        print(f"错误: SHA512 校验失败 - {e}", file=sys.stderr)
        return False


def ping_source(url: str, timeout: float = 5.0) -> float | None:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        start = time.time()
        urllib.request.urlopen(req, timeout=timeout)
        end = time.time()
        return end - start
    except Exception:
        return None


def select_download_source(yes: bool = False) -> str | None:
    sources = [
        {"name": "Distribution API (官方分发)", "type": "distribution", "ping_url": "https://distribution.classisland.tech/api/v1/public/distributions/web"},
        {"name": "GitHub Releases", "type": "github", "ping_url": "https://api.github.com/repos/ClassIsland/ClassIsland/releases/latest"},
    ]

    print("正在测速下载源...")
    pinged = []
    for source in sources:
        ping_time = ping_source(source["ping_url"])
        if ping_time is not None:
            pinged.append((ping_time, source))
            print(f"  - {source['name']}: {ping_time:.3f}s")
        else:
            print(f"  - {source['name']}: 不可达")

    if not pinged:
        print("错误: 所有下载源不可达", file=sys.stderr)
        return None

    pinged.sort(key=lambda x: x[0])

    if yes:
        print(f"自动选择: {pinged[0][1]['name']} ({pinged[0][0]:.3f}s)")
        emit("level=info", "stage=select_source", "auto=true", f"selected={pinged[0][1]['type']}")
        return pinged[0][1]["type"]

    if len(pinged) == 1:
        chosen = pinged[0][1]
        print(f"自动选择下载源: {chosen['name']} ({pinged[0][0]:.3f}s)")
        emit("level=info", "stage=select_source", "auto=true", f"selected={chosen['type']}")
        return chosen["type"]

    # 结构化输出: 下载源列表供 GUI 展示
    emit("level=options", "stage=select_source", f"count={len(pinged)}")
    for i, (ping_time, source) in enumerate(pinged, 1):
        emit("level=option", f"index={i}", f"value={source['type']}", f"label={source['name']}", f"ping={ping_time:.3f}")

    print("可用下载源（按响应时间排序）:")
    for i, (ping_time, source) in enumerate(pinged, 1):
        print(f"  {i}. {source['name']} ({ping_time:.3f}s)")

    while True:
        try:
            choice = ask(
                "请选择下载源 (输入数字): ",
                choices=[s["type"] for _, s in pinged],
            ).strip()
        except EOFError:
            chosen = pinged[0][1]
            print(f"\n无交互输入，自动选择: {chosen['name']}")
            emit("level=info", "stage=select_source", "auto=true", f"selected={chosen['type']}")
            return chosen["type"]
        try:
            idx = int(choice)
            if 1 <= idx <= len(pinged):
                return pinged[idx - 1][1]["type"]
        except ValueError:
            pass
        print("无效输入，请重试。")


def fetch_disturb_index(index_url: str) -> dict | None:
    for ua in [
        {"User-Agent": "CiPack/2.0", "Accept": "application/json"},
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"},
    ]:
        try:
            req = urllib.request.Request(index_url, headers=ua)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                if raw.lstrip().startswith("<"):
                    continue
                return json.loads(raw)
        except Exception:
            continue
    print(f"错误: 获取 disturb 索引失败: {index_url}", file=sys.stderr)
    return None


def fetch_disturb_versions() -> list[dict] | None:
    # 注意: 根路径 /classisland/disturb/index.json 返回 HTML (Alist 列表页)
    # 必须使用 /d/ 或 /p/ 前缀的分发路径才能获取真实 JSON 索引
    urls = [
        "https://get.classisland.tech/d/ClassIsland-Ningbo-S3/classisland/disturb/index.json",
        "https://get.classisland.tech/p/ClassIsland-Ningbo-S3/classisland/disturb/index.json",
        "https://get.classisland.tech/classisland/disturb/index.json",
    ]
    for url in urls:
        data = fetch_disturb_index(url)
        if data is not None:
            return data.get("Versions", [])
    return None


def fetch_disturb_net6_versions() -> list[dict] | None:
    urls = [
        "https://get.classisland.tech/d/ClassIsland-Ningbo-S3/classisland/disturb-net6/index.json",
        "https://get.classisland.tech/p/ClassIsland-Ningbo-S3/classisland/disturb-net6/index.json",
        "https://get.classisland.tech/classisland/disturb-net6/index.json",
    ]
    for url in urls:
        data = fetch_disturb_index(url)
        if data is not None:
            return data.get("Versions", [])
    return None


def download_ci1x(version: str, target_dir: str, spoof_ua: bool = False, yes_skip: bool = False) -> str | None:
    """从 disturb 或 disturb-net6 源下载 CI 1.x"""
    # disturb 源版本信息 URL（/d/ 优先，新版使用 /d/ 前缀）
    version_info_urls = [
        f"https://get.classisland.tech/d/ClassIsland-Ningbo-S3/classisland/disturb/{version}/index.json",
        f"https://get.classisland.tech/d/ClassIsland-Ningbo-S3/classisland/disturb-net6/{version}/index.json",
        f"https://get.classisland.tech/p/ClassIsland-Ningbo-S3/classisland/disturb/{version}/index.json",
        f"https://get.classisland.tech/p/ClassIsland-Ningbo-S3/classisland/disturb-net6/{version}/index.json",
    ]
    
    version_info = None
    for url in version_info_urls:
        for headers in [
            {"User-Agent": "CiPack/2.0", "Accept": "application/json"},
            {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"},
        ]:
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read().decode("utf-8")
                    if raw.lstrip().startswith("<"):
                        continue
                    version_info = json.loads(raw)
                    break
            except Exception:
                continue
        if version_info:
            break
    
    if version_info is None:
        print(f"错误: 无法获取版本 {version} 的下载信息", file=sys.stderr)
        return None
    
    download_infos = version_info.get("DownloadInfos", {})
    
    # 选择最佳变体: 优先 singleFile，然后 folder
    variant_order = [
        "windows_x64_full_singleFile",
        "windows_x64_trimmed_singleFile",
        "windows_x64_full_folder",
        "windows_x64_selfContained_folder",
    ]
    
    selected_variant = None
    for variant in variant_order:
        if variant in download_infos:
            selected_variant = download_infos[variant]
            break
    
    if selected_variant is None:
        # 尝试任意 windows_x64 变体
        for key, info in download_infos.items():
            if "windows_x64" in key:
                selected_variant = info
                break
    
    if selected_variant is None:
        print("错误: 未找到合适的下载资源", file=sys.stderr)
        return None
    
    archive_urls = selected_variant.get("ArchiveDownloadUrls", {})

    # 测速选择镜像
    available_mirrors = {k: v for k, v in archive_urls.items() if v and k != "DeployMethod"}
    if not available_mirrors:
        print("错误: 未找到下载地址", file=sys.stderr)
        return None

    # 有多个镜像时测速
    if len(available_mirrors) > 1:
        print("正在测速下载镜像...")
        pinged_mirrors = []
        for mirror_name, mirror_url in available_mirrors.items():
            # 用 HEAD 请求测速
            ping_url = mirror_url.split("/download/")[0] if "/download/" in mirror_url else mirror_url.rsplit("/", 1)[0]
            t = ping_source(ping_url, timeout=5.0)
            if t is not None:
                display = "get.classisland.tech" if "get.classisland.tech" in mirror_url else mirror_name
                pinged_mirrors.append((t, mirror_name, mirror_url, display))
                print(f"  - {display}: {t:.3f}s")
            else:
                display = "get.classisland.tech" if "get.classisland.tech" in mirror_url else mirror_name
                print(f"  - {display}: 不可达")

        if pinged_mirrors:
            pinged_mirrors.sort(key=lambda x: x[0])
            download_url = pinged_mirrors[0][2]
            print(f"选择镜像: {pinged_mirrors[0][3]} ({pinged_mirrors[0][0]:.3f}s)")
        else:
            download_url = list(available_mirrors.values())[0]
    else:
        download_url = list(available_mirrors.values())[0]

    name = download_url.split("/")[-1].split("?")[0]
    asset = {
        "name": name,
        "browser_download_url": download_url,
        "size": 0,
    }

    print(f"准备下载: {name}")
    print(f"版本: {version}")
    print(f"下载地址: {download_url}")

    downloaded = download_ci_package(asset, target_dir, spoof_ua=spoof_ua)
    if downloaded is None:
        return None

    # SHA256 校验
    expected_sha256 = selected_variant.get("ArchiveSHA256")
    if expected_sha256:
        print("正在校验文件完整性 (SHA256)...")
        h = hashlib.sha256()
        with open(downloaded, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        actual = h.hexdigest()
        if actual.lower() != expected_sha256.lower():
            print(f"SHA256 校验失败", file=sys.stderr)
            print(f"  期望: {expected_sha256}", file=sys.stderr)
            print(f"  实际: {actual}", file=sys.stderr)
            if yes_skip:
                print("跳过校验,继续解压")
            else:
                if os.path.exists(downloaded):
                    os.remove(downloaded)
                return None
        else:
            print("校验通过")

    return downloaded


def select_ci1x_download_source(yes: bool = False) -> tuple[str | None, str | None]:
    """
    选择 CI 1.x 下载源。返回 (source_type, version)。
    source_type: "disturb"|"disturb-net6"|"github"|None
    """
    disturb_versions = fetch_disturb_versions()
    net6_versions = fetch_disturb_net6_versions()
    
    options = []
    
    if disturb_versions:
        # 获取 disturb 最新稳定版
        stable_versions = [v for v in disturb_versions if isinstance(v, dict) and "stable" in v.get("Channels", [])]
        if stable_versions:
            latest = sorted(stable_versions, key=lambda x: _version_key(x.get("Version", "0")))[-1]
            options.append({
                "name": f"Disturb 源 (最新稳定版 {latest.get('Version', '未知')})",
                "type": "disturb",
                "version": latest.get("Version"),
            })
        # 也添加一个让用户选择特定版本的选项？暂时只提供最新稳定版

    if net6_versions:
        stable_versions = [v for v in net6_versions if isinstance(v, dict) and "stable" in v.get("Channels", [])]
        if stable_versions:
            latest = sorted(stable_versions, key=lambda x: _version_key(x.get("Version", "0")))[-1]
            options.append({
                "name": f"Disturb-net6 源 (最新稳定版 {latest.get('Version', '未知')})",
                "type": "disturb-net6",
                "version": latest.get("Version"),
            })
    
    # GitHub 回退
    options.append({
        "name": "GitHub Releases (回退)",
        "type": "github",
        "version": None,
    })
    
    if not options:
        print("错误: 无可用下载源", file=sys.stderr)
        return None, None
    
    if yes:
        print(f"自动选择: {options[0]['name']} (默认源)")
        emit("level=info", "stage=select_ci1x_source", "auto=true", f"selected={options[0]['type']}")
        return options[0]["type"], options[0]["version"]
    
    if len(options) == 1:
        chosen = options[0]
        print(f"自动选择: {chosen['name']}")
        emit("level=info", "stage=select_ci1x_source", "auto=true", f"selected={chosen['type']}")
        return chosen["type"], chosen["version"]
    
    # 结构化输出: CI 1.x 下载源列表供 GUI 展示
    emit("level=options", "stage=select_ci1x_source", f"count={len(options)}")
    for i, opt in enumerate(options, 1):
        emit("level=option", f"index={i}", f"value={opt['type']}", f"label={opt['name']}")

    print("可用 CI 1.x 下载源:")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt['name']}")
    
    while True:
        try:
            choice = ask(
                "请选择下载源 (输入数字): ",
                choices=[o["type"] for o in options],
            ).strip()
        except EOFError:
            chosen = options[0]
            print(f"\n无交互输入，自动选择: {chosen['name']}")
            emit("level=info", "stage=select_ci1x_source", "auto=true", f"selected={chosen['type']}")
            return chosen["type"], chosen["version"]
        try:
            idx = int(choice)
            if 1 <= idx <= len(options):
                chosen = options[idx - 1]
                return chosen["type"], chosen["version"]
        except ValueError:
            pass
        print("无效输入，请重试。")


def build_distribution_variant(target_os: str, target_arch: str) -> list[str]:
    os_lower = (target_os or "").lower()
    if os_lower == "windows":
        return ["selfContained_folder", "full_folder", "full_singleFile"]
    elif os_lower in ("linux", "macos", "darwin", "mac"):
        return ["selfContained_folder", "full_folder"]
    else:
        return ["selfContained_folder", "full_folder"]


def download_ci_package(asset: dict, save_dir: str, spoof_ua: bool = False) -> str | None:
    name = asset.get("name")
    url = asset.get("browser_download_url")
    if not name or not url:
        print("错误: 下载资源信息缺失 (name/url)", file=sys.stderr)
        emit("level=error", message="下载资源信息缺失 (name/url)")
        return None
    size = asset.get("size", 0)
    part_path = os.path.join(save_dir, f"{name}.part")
    final_path = os.path.join(save_dir, name)

    # 断点续传: 检查已有部分文件
    existing_size = 0
    if os.path.exists(part_path):
        existing_size = os.path.getsize(part_path)

    if size and size > 0:
        if existing_size > 0:
            print(f"断点续传: 已下载 {existing_size / (1024 * 1024):.1f} / {size / (1024 * 1024):.1f} MB")
        else:
            print(f"开始下载: {name} ({size / (1024 * 1024):.1f} MB)")
    else:
        if existing_size > 0:
            print(f"断点续传: 已下载 {existing_size / (1024 * 1024):.1f} MB")
        else:
            print(f"开始下载: {name} (大小未知)")

    emit("stage=download", "progress=0.0", "current=0", f"total={size}")

    # UA 选择
    if spoof_ua:
        download_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/octet-stream,*/*",
            "Referer": "https://www.classisland.tech/",
        }
    else:
        download_headers = {
            "User-Agent": "CiPack/2.0",
            "Accept": "application/octet-stream,*/*",
        }

    # 断点续传 Range header
    if existing_size > 0:
        download_headers["Range"] = f"bytes={existing_size}-"

    try:
        req = urllib.request.Request(url, headers=download_headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            # 206 = 支持断点续传, 200 = 不支持(从头开始)
            if resp.status == 200 and existing_size > 0:
                # 服务器不支持续传,从头开始
                existing_size = 0
                os.remove(part_path)

            # 从 Content-Length 获取总大小
            content_length = resp.headers.get("Content-Length")
            if content_length:
                cl = int(content_length)
                if resp.status == 206:
                    # 206 响应的 Content-Length 是剩余部分的大小
                    total_size = existing_size + cl
                    if size == 0:
                        size = total_size
                else:
                    if size == 0:
                        size = cl

            # 打开文件: 206 追加, 200 覆盖
            mode = "ab" if resp.status == 206 else "wb"

            # 首次下载时验证文件头 (ZIP 文件以 PK\x03\x04 开头)
            if existing_size == 0:
                first_chunk = resp.read(4)
                if first_chunk != b"PK\x03\x04":
                    print(f"错误: 下载文件不是有效的 ZIP (文件头: {first_chunk.hex()})", file=sys.stderr)
                    emit("level=error", message=f"下载文件不是有效的 ZIP (文件头: {first_chunk.hex()})")
                    print("可能是服务器端错误,请稍后重试或换用 GitHub 源", file=sys.stderr)
                    if os.path.exists(part_path):
                        os.remove(part_path)
                    return None

            with open(part_path, mode) as f:
                if existing_size == 0:
                    f.write(first_chunk)
                if TQDM_AVAILABLE:
                    total = size if size > 0 else None
                    pbar = tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024, desc=name)
                    if existing_size > 0:
                        pbar.update(existing_size)
                    downloaded = existing_size
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        pbar.update(len(chunk))
                        downloaded += len(chunk)
                        if size > 0:
                            pct = min(100.0, downloaded * 100.0 / size)
                            emit(f"progress={pct:.1f}", f"current={downloaded}", f"total={size}")
                    pbar.close()
                else:
                    downloaded = existing_size
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if size > 0:
                            print(f"\r  下载中: {downloaded / (1024 * 1024):.1f} / {size / (1024 * 1024):.1f} MB", end="", flush=True)
                            pct = min(100.0, downloaded * 100.0 / size)
                            emit(f"progress={pct:.1f}", f"current={downloaded}", f"total={size}")
                        else:
                            print(f"\r  下载中: {downloaded / (1024 * 1024):.1f} MB", end="", flush=True)
                            emit(f"current={downloaded}")
                    print()
    except KeyboardInterrupt:
        # 断点续传: 不删除 .part 文件,下次可继续
        print("\n下载中断,已保存部分文件,下次运行将断点续传")
        emit("level=warning", message="下载中断")
        return None
    except Exception as e:
        print(f"错误: 下载失败 - {e}", file=sys.stderr)
        emit("level=error", message=f"下载失败: {e}")
        return None

    emit("progress=100.0")
    if os.path.exists(final_path):
        os.remove(final_path)
    os.rename(part_path, final_path)
    return final_path


def _find_ci_root(dir_path: str) -> str | None:
    if os.path.isdir(os.path.join(dir_path, "data")):
        return dir_path
    if os.path.isdir(os.path.join(dir_path, "Config")) and os.path.isdir(os.path.join(dir_path, "Profiles")):
        return dir_path

    try:
        for item in os.listdir(dir_path):
            item_path = os.path.join(dir_path, item)
            if os.path.isdir(item_path) and not item.startswith(("app-", "old", "__")):
                sub_result = _find_ci_root(item_path)
                if sub_result:
                    return sub_result
    except PermissionError:
        pass
    return None


def _flatten_ci_structure(target_dir: str) -> bool:
    direct = _find_ci_root(target_dir)
    if direct == target_dir:
        return True

    try:
        top_items = os.listdir(target_dir)
    except PermissionError:
        return False

    candidates = []
    for item in top_items:
        item_path = os.path.join(target_dir, item)
        if os.path.isdir(item_path):
            has_data = os.path.isdir(os.path.join(item_path, "data"))
            has_config_profiles = (
                os.path.isdir(os.path.join(item_path, "Config"))
                and os.path.isdir(os.path.join(item_path, "Profiles"))
            )
            if has_data or has_config_profiles:
                candidates.append((item_path, has_data))

    if not candidates:
        return False

    candidates.sort(key=lambda x: x[1], reverse=True)
    nested_path = candidates[0][0]

    tmp_name = f"__ci_flatten_{uuid.uuid4().hex}__"
    tmp_path = os.path.join(target_dir, tmp_name)
    shutil.move(nested_path, tmp_path)

    for item in os.listdir(tmp_path):
        src = os.path.join(tmp_path, item)
        dst = os.path.join(target_dir, item)
        if not os.path.exists(dst):
            shutil.move(src, dst)
        else:
            print(f"警告: 跳过已存在的文件: {dst}", file=sys.stderr)
            if os.path.isdir(src) and os.path.isdir(dst):
                for sub_item in os.listdir(src):
                    sub_src = os.path.join(src, sub_item)
                    sub_dst = os.path.join(dst, sub_item)
                    if not os.path.exists(sub_dst):
                        shutil.move(sub_src, sub_dst)
                    else:
                        print(f"警告: 跳过已存在的文件: {sub_dst}", file=sys.stderr)
            elif os.path.isfile(src):
                os.remove(src)

    try:
        shutil.rmtree(tmp_path)
    except Exception:
        pass

    return True


def extract_ci_package(archive_path: str, target_dir: str) -> bool:
    existing_items = set()
    if os.path.isdir(target_dir):
        existing_items = set(os.listdir(target_dir))
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            members = zf.namelist()
            for member in members:
                if member.endswith('/'):
                    continue
                # 安全解析相对路径，避免路径穿越
                rel = member.lstrip('/')
                # 去除 Windows 盘符和绝对路径的前导 \
                if ':' in rel[:3] or rel.startswith('\\') or rel.startswith('/'):
                    print(f"警告: 跳过可疑路径: {member}", file=sys.stderr)
                    continue
                dest_path = os.path.join(target_dir, rel)
                dest_abs = os.path.abspath(dest_path)
                target_abs = os.path.abspath(target_dir)
                # Zip Slip 防护
                if not dest_abs.startswith(target_abs + os.sep) and dest_abs != target_abs:
                    print(f"警告: 路径穿越尝试已阻止: {member}", file=sys.stderr)
                    continue
                os.makedirs(os.path.dirname(dest_abs), exist_ok=True)
                try:
                    with zf.open(member) as src, open(dest_abs, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                except Exception as e:
                    print(f"警告: 解压 {member} 失败: {e}", file=sys.stderr)
                    continue
                size = zf.getinfo(member).file_size
                unit = "B"
                val = size
                if val >= 1024:
                    val = size / 1024
                    unit = "KB"
                if val >= 1024:
                    val = val / 1024
                    unit = "MB"
                print(f"  解压: {os.path.basename(member)} ({val:.1f} {unit})")

            top_items = set()
            for member in members:
                part = member.split('/')[0]
                if part:
                    top_items.add(part)

            if len(top_items) == 1:
                nested_dir = top_items.pop()
                nested_path = os.path.join(target_dir, nested_dir)
                if os.path.isdir(nested_path):
                    tmp_name = f"__ci_tmp_{uuid.uuid4().hex}__"
                    tmp_path = os.path.join(target_dir, tmp_name)
                    shutil.move(nested_path, tmp_path)
                    for item in os.listdir(tmp_path):
                        src = os.path.join(tmp_path, item)
                        dst = os.path.join(target_dir, item)
                        if not os.path.exists(dst):
                            shutil.move(src, dst)
                        else:
                            print(f"警告: 跳过已存在的文件: {dst}", file=sys.stderr)
                    shutil.rmtree(tmp_path)

            valid = is_valid_ci_install(target_dir)
            if not valid:
                if not _flatten_ci_structure(target_dir):
                    print("错误: 解压后的 CI 安装无效", file=sys.stderr)
                    emit("level=error", message="解压后的 CI 安装无效")
                    return False
                valid = is_valid_ci_install(target_dir)
            if valid:
                return True
            else:
                print("错误: 解压后的 CI 安装无效", file=sys.stderr)
                emit("level=error", message="解压后的 CI 安装无效")
                return False
    except Exception as e:
        if os.path.isdir(target_dir):
            current_items = set(os.listdir(target_dir))
            new_items = current_items - existing_items
            for item in new_items:
                item_path = os.path.join(target_dir, item)
                try:
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    else:
                        os.remove(item_path)
                except Exception:
                    pass
        print(f"错误: 解压 CI 包失败 - {e}", file=sys.stderr)
        emit("level=error", message=f"解压 CI 包失败: {e}")
        return False


def pack_command(args):
    global STRUCTURED_MODE
    STRUCTURED_MODE = getattr(args, "structured", False)
    emit("stage=pack")
    root = os.path.abspath(args.dir)
    output = os.path.abspath(args.output)

    if not os.path.isdir(root):
        print(f"错误: 目录不存在: {root}", file=sys.stderr)
        emit("level=error", message=f"目录不存在: {root}")
        sys.exit(1)

    files = collect_files(root, include_backups=args.include_backups)

    if not files:
        print("没有找到需要打包的配置文件。", file=sys.stderr)
        emit("level=error", message="没有找到需要打包的配置文件")
        sys.exit(1)

    plugins = collect_plugin_info(root)
    ci_version = detect_ci_version(root)
    ci_structure = detect_ci_structure(root)

    manifest = {
        "tool": "ClassIsland Config Packer",
        "version": "2.0",
        "created_at": datetime.now().isoformat(),
        "source_dir": root,
        "file_count": len(files),
        "include_backups": args.include_backups,
        "plugins": plugins,
        "ci_version": ci_version,
        "ci_structure": ci_structure,
    }

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        if not args.official_format:
            zf.writestr("__manifest__.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        arcnames = []
        for rel in files:
            full = os.path.join(root, rel)
            rel_posix = rel.replace(os.sep, "/")
            if ci_structure == "v2" and rel_posix.startswith("data/"):
                zip_path = rel_posix[5:]
            else:
                zip_path = rel_posix
            zf.write(full, zip_path)
            arcnames.append(zip_path)
            size = os.path.getsize(full)
            print(f"  + {zip_path}  ({size / 1024:.1f} KB)")

    total_size = os.path.getsize(output)
    print(f"\n打包完成: {output}")
    print(f"文件数: {len(files)}  总大小: {total_size / 1024:.1f} KB")
    emit("stage=pack", f"file_count={len(arcnames)}")
    emit("level=success")

    version_display = ci_version if ci_version is not None else "未知"
    structure_display = ci_structure if ci_structure != "unknown" else "未知"
    print(f"检测到 ClassIsland 版本: {version_display}")
    print(f"检测到目录结构: {structure_display}")

    if plugins:
        print(f"\n检测到 {len(plugins)} 个已安装插件:")
        for p in plugins:
            print(f"  {p['name']} ({p['version']})")


def ensure_ci_downloaded(
    target: str,
    need_ci1x: bool = False,
    download_version: str | None = None,
    yes_download: bool = False,
    spoof_ua: bool = False,
    yes_skip: bool = False,
) -> bool:
    """确保目标目录有有效的 CI 安装，若无可下载。返回是否成功。"""
    if is_valid_ci_install(target):
        return True

    if not check_and_prompt_download(target, yes_download=yes_download):
        return False

    if is_valid_ci_install(target):
        return True

    if need_ci1x:
        # === CI 1.x 下载逻辑 ===
        if download_version:
            print(f"将下载 ClassIsland {download_version} (CI 1.x)")
        else:
            print("将下载最新 ClassIsland 1.x 版本")

        downloaded_path = None

        # 优先: 有明确版本时直接从 disturb 源下载，跳过索引
        if download_version:
            print("尝试从 disturb 源直接下载...")
            downloaded_path = download_ci1x(download_version, target, spoof_ua=spoof_ua, yes_skip=yes_skip)

        # 回退: 通过源选择器（索引或 GitHub）
        if downloaded_path is None:
            print("disturb 直连失败，选择其他下载源...")
            source_type, default_version = select_ci1x_download_source(yes=yes_download)
            if source_type is None:
                print("错误: 无可用下载源", file=sys.stderr)
                return False

            if source_type in ("disturb", "disturb-net6"):
                version_to_download = download_version or default_version
                if not version_to_download:
                    print("错误: 无法确定下载版本", file=sys.stderr)
                    return False
                downloaded_path = download_ci1x(version_to_download, target, spoof_ua=spoof_ua, yes_skip=yes_skip)
            elif source_type == "github":
                if download_version:
                    release = fetch_release_by_version(download_version)
                    if release is None:
                        release = fetch_release_by_version(f"v{download_version}")
                else:
                    release = fetch_latest_release()
                if release is None:
                    return False
                asset = select_best_asset(release)
                if asset is None:
                    print("错误: 未找到合适的下载资源", file=sys.stderr)
                    return False
                size_mb = asset["size"] / (1024 * 1024)
                print(f"准备下载: {asset['name']} ({size_mb:.1f} MB)")
                print(f"下载地址: {asset['browser_download_url']}")
                for gh_retry in range(3):
                    downloaded_path = download_ci_package(asset, target, spoof_ua=spoof_ua)
                    if downloaded_path is not None:
                        break
                    if gh_retry < 2:
                        print(f"GitHub 下载失败,重试 ({gh_retry + 2}/3)...", file=sys.stderr)
                if downloaded_path is None:
                    print("错误: GitHub 下载失败", file=sys.stderr)
                    return False
                print(f"下载完成: {downloaded_path}")

        try:
            if downloaded_path and not extract_ci_package(downloaded_path, target):
                return False
            print("CI 安装完成")
        finally:
            if downloaded_path and os.path.exists(downloaded_path):
                os.remove(downloaded_path)
        return is_valid_ci_install(target)
    else:
        # === CI 2.x 下载逻辑 ===
        if download_version is None:
            print("警告: 未指定 CI 版本,将下载最新版,可能存在兼容性风险")

        # 2. 选择下载源
        source_type = select_download_source(yes=yes_download)
        if source_type is None:
            print("错误: 所有下载源均不可用", file=sys.stderr)
            return False

        downloaded_path = None
        try:
            if source_type == "distribution":
                channels_info = fetch_distribution_channels()
                if channels_info is None:
                    print("错误: 获取 distribution 渠道失败,回退到 GitHub", file=sys.stderr)
                    source_type = "github"

            if source_type == "distribution":
                default_ch_id = channels_info.get("default_channel")
                if not default_ch_id or default_ch_id not in channels_info.get("channels", {}):
                    print("错误: distribution 渠道信息不完整,回退到 GitHub", file=sys.stderr)
                    source_type = "github"
                else:
                    channel = channels_info["channels"][default_ch_id]
                    version_id = channel.get("latestVersionId")
                    latest_version = channel.get("latestVersion", "")
                    if not version_id or not latest_version:
                        source_type = "github"
                    elif download_version and download_version != latest_version:
                        print(f"指定版本 {download_version} 非最新版,回退到 GitHub 下载")
                        source_type = "github"

            if source_type == "distribution":
                target_os = get_system_os()
                target_arch = get_system_arch()
                variants = build_distribution_variant(target_os, target_arch)

                asset_info = None
                for variant in variants:
                    asset_info = fetch_distribution_asset(version_id, target_os, target_arch, variant)
                    if asset_info:
                        break

                if asset_info is None:
                    print("错误: 未找到合适的下载资源,回退到 GitHub", file=sys.stderr)
                    source_type = "github"
                else:
                    print(f"准备下载: {asset_info['name']}")
                    print(f"版本: {asset_info['version']}")
                    print(f"下载地址: {asset_info['url']}")

                    asset = {
                        "name": asset_info["name"],
                        "browser_download_url": asset_info["url"],
                        "size": 0,
                    }

                    downloaded_path = download_ci_package(asset, target, spoof_ua=spoof_ua)
                    if downloaded_path is None:
                        return False

                    actual_size = os.path.getsize(downloaded_path)
                    print(f"下载文件大小: {actual_size / (1024 * 1024):.1f} MB")

                    sha512_ok = False
                    if asset_info.get("sha512"):
                        print("正在校验文件完整性 (SHA512)...")
                        if verify_sha512(downloaded_path, asset_info["sha512"]):
                            sha512_ok = True
                        else:
                            actual_size = os.path.getsize(downloaded_path)
                            print(f"SHA512 校验失败 (文件大小: {actual_size / (1024 * 1024):.1f} MB)", file=sys.stderr)
                            print("可能是服务器端 SHA512 记录有误", file=sys.stderr)
                            if yes_skip or ask("是否跳过校验继续解压? (y/N) ", default="N").strip().lower() == "y":
                                sha512_ok = True
                                print("跳过校验,继续解压")
                            else:
                                print("回退到 GitHub 下载", file=sys.stderr)
                                if os.path.exists(downloaded_path):
                                    os.remove(downloaded_path)
                                source_type = "github"
                    else:
                        sha512_ok = True

                    if sha512_ok:
                        print("校验通过")
                        print(f"下载完成: {downloaded_path}")

            if source_type == "github":
                if download_version:
                    print(f"将下载版本: {download_version}")
                    release = fetch_release_by_version(download_version)
                    if release is None:
                        release = fetch_release_by_version(f"v{download_version}")
                else:
                    release = fetch_latest_release()
                if release is None:
                    return False
                asset = select_best_asset(release)
                if asset is None:
                    print("错误: 未找到合适的下载资源", file=sys.stderr)
                    return False
                size_mb = asset["size"] / (1024 * 1024)
                print(f"准备下载: {asset['name']} ({size_mb:.1f} MB)")
                print(f"下载地址: {asset['browser_download_url']}")
                for gh_retry in range(3):
                    downloaded_path = download_ci_package(asset, target, spoof_ua=spoof_ua)
                    if downloaded_path is not None:
                        break
                    if gh_retry < 2:
                        print(f"GitHub 下载失败,重试 ({gh_retry + 2}/3)...", file=sys.stderr)
                if downloaded_path is None:
                    print("错误: GitHub 下载失败,请检查网络或稍后重试", file=sys.stderr)
                    return False
                print(f"下载完成: {downloaded_path}")

            if downloaded_path and not extract_ci_package(downloaded_path, target):
                return False
            print("CI 安装完成")
            emit("level=success", message="CI 安装完成")
        finally:
            if downloaded_path and os.path.exists(downloaded_path):
                os.remove(downloaded_path)

        return is_valid_ci_install(target)


def unpack_command(args):
    global STRUCTURED_MODE
    STRUCTURED_MODE = getattr(args, "structured", False)
    archive = os.path.abspath(args.input)
    target = os.path.abspath(args.dir)

    if not os.path.isfile(archive):
        print(f"错误: 打包文件不存在: {archive}", file=sys.stderr)
        emit("level=error", message=f"打包文件不存在: {archive}")
        sys.exit(1)

    zip_info = detect_version_from_zip(archive)
    inferred_structure = zip_info.get("ci_structure", "unknown")
    inferred_version = zip_info.get("ci_version")

    source_ci_version = inferred_version
    early_manifest = None
    with zipfile.ZipFile(archive, "r") as zf:
        if "__manifest__.json" in zf.namelist():
            with zf.open("__manifest__.json") as f:
                early_manifest = json.load(f)
            if source_ci_version is None:
                source_ci_version = early_manifest.get("ci_version")

    if not check_and_prompt_download(target, yes_download=args.yes_download):
        return

    need_ci1x = False
    if inferred_structure == "v1":
        need_ci1x = True
    elif early_manifest and early_manifest.get("ci_structure") == "v1":
        need_ci1x = True

    if not ensure_ci_downloaded(
        target,
        need_ci1x=need_ci1x,
        download_version=args.ci_version or source_ci_version,
        yes_download=args.yes_download,
        spoof_ua=args.spoof_ua,
        yes_skip=args.yes,
    ):
        return
    emit("stage=unpack")
    print("开始解包配置...")

    if not os.path.isdir(target):
        print(f"错误: 目标目录不存在: {target}", file=sys.stderr)
        sys.exit(1)

    # 检测 ClassIsland 是否正在运行
    if check_ci_running(target):
        print("警告: 检测到 ClassIsland 正在运行!", file=sys.stderr)
        emit("level=warning", message="检测到 ClassIsland 正在运行")
        print("请先关闭 ClassIsland 再继续，否则配置可能写入失败或损坏。", file=sys.stderr)
        if not args.yes:
            resp = ask("是否继续? (y/N) ", default="N").strip().lower()
            if resp not in ("y", "yes"):
                print("已取消。请关闭 ClassIsland 后重试。")
                return
        else:
            print("(--yes 跳过确认，继续操作)", file=sys.stderr)

    # 已有配置保护: 目标目录已有 CI 配置时警告
    if has_existing_config(target):
        print(f"警告: 目标目录已有 ClassIsland 配置: {target}", file=sys.stderr)
        emit("level=warning", message=f"目标目录已有 ClassIsland 配置: {target}")
        print("解包将覆盖现有配置文件。", file=sys.stderr)
        if not args.yes:
            resp = ask("是否覆盖? (y/N) ", default="N").strip().lower()
            if resp not in ("y", "yes"):
                print("已取消。")
                return
        else:
            print("(--yes 跳过确认，将覆盖现有配置)", file=sys.stderr)

    # 结构兼容性检查: V1 配置包不能解包到 V2 安装，反之亦然
    target_ci_structure = detect_ci_structure(target)
    source_structure_for_check = inferred_structure
    if early_manifest:
        source_structure_for_check = early_manifest.get("ci_structure", source_structure_for_check)
    if source_structure_for_check == "unknown":
        source_structure_for_check = target_ci_structure  # 无法判断时不阻止

    if source_structure_for_check != "unknown" and target_ci_structure != "unknown" and source_structure_for_check != target_ci_structure:
        print(f"错误: 版本结构不兼容 - 配置包结构: {source_structure_for_check}, 目标 CI 结构: {target_ci_structure}", file=sys.stderr)
        emit("level=error", message=f"版本结构不兼容: 配置包={source_structure_for_check}, 目标={target_ci_structure}")
        print(f"V1 配置包只能解包到 V1 安装目录，V2 配置包只能解包到 V2 安装目录。", file=sys.stderr)
        if not args.yes:
            resp = ask("是否强行继续? (y/N) ", default="N").strip().lower()
            if resp not in ("y", "yes"):
                print("已取消。")
                return
            print("警告: 强行解包可能导致配置损坏", file=sys.stderr)
            emit("level=warning", message="强行解包可能导致配置损坏")
        else:
            print("警告: 强行解包可能导致配置损坏 (--yes 跳过确认)", file=sys.stderr)
            emit("level=warning", message="强行解包可能导致配置损坏")

    target_ci_version = detect_ci_version(target)
    if source_ci_version and target_ci_version and source_ci_version != target_ci_version:
        print(f"警告: 版本不一致 - 打包时版本: {source_ci_version}, 目标版本: {target_ci_version}")
        emit("level=warning", message=f"版本不一致: 打包时={source_ci_version}, 目标={target_ci_version}")
        print("配置可能无法完全兼容")

    with zipfile.ZipFile(archive, "r") as zf:
        names = zf.namelist()

        manifest = None
        if "__manifest__.json" in names:
            with zf.open("__manifest__.json") as f:
                manifest = json.load(f)

        target_structure = manifest.get("ci_structure", "unknown") if manifest else inferred_structure
        if target_structure == "unknown":
            target_structure = detect_ci_structure(target)

        if manifest:
            print("=== 打包信息 ===")
            print(f"  创建时间: {manifest.get('created_at', '未知')}")
            print(f"  文件数: {manifest.get('file_count', '未知')}")
            print(f"  来源目录: {manifest.get('source_dir', '未知')}")

        warnings = []
        if manifest and isinstance(manifest.get("plugins"), list):
            source_plugins = {
                p["id"]: p
                for p in manifest["plugins"]
                if isinstance(p, dict) and "id" in p
            }
            target_plugins = {}
            for p in collect_plugin_info(target):
                if isinstance(p, dict) and "id" in p:
                    target_plugins[p["id"]] = p

            print(f"\n=== 插件版本校验 ===")
            for pid, src in source_plugins.items():
                src_name = src.get("name", "未知插件")
                src_version = src.get("version", "未知版本")
                if pid not in target_plugins:
                    warnings.append(f"  [!] 源配置包含插件 '{src_name}'，但目标未安装")
                else:
                    tgt = target_plugins[pid]
                    tgt_name = tgt.get("name", "未知插件")
                    tgt_version = tgt.get("version", "未知版本")
                    if src_version != tgt_version:
                        warnings.append(f"  [~] 插件 '{src_name}' 版本不一致")
                        warnings.append(f"      源: {src_version}  |  目标: {tgt_version}")

            for pid, tgt in target_plugins.items():
                if pid not in source_plugins:
                    tgt_name = tgt.get("name", "未知插件")
                    tgt_version = tgt.get("version", "未知版本")
                    print(f"  [+] 目标有插件 '{tgt_name}' ({tgt_version})，但源配置未包含")

            if warnings:
                print("\n".join(warnings))

        if not args.yes:
            resp = ask(f"\n即将将配置解压到: {target}\n继续? (y/N) ", default="N").strip().lower()
            if resp not in ("y", "yes"):
                print("已取消。")
                return

        count = 0
        for name in names:
            if name == "__manifest__.json":
                continue
            if name.endswith("/"):
                continue

            if zip_info.get("format", "unknown") == "official":
                # 官方格式: zip 内文件无 data/ 前缀，根据目标结构决定解压路径
                if target_structure == "v2":
                    dest = os.path.join(target, "data", name)
                else:
                    dest = os.path.join(target, name)
            else:
                # 自有格式: 保持原有逻辑
                if target_structure == "v2" and not name.startswith("data/"):
                    dest = os.path.join(target, "data", name)
                else:
                    dest = os.path.join(target, name)
            # 安全校验：防止路径逃逸
            dest_abs = os.path.abspath(dest)
            target_abs = os.path.abspath(target)
            if not dest_abs.startswith(target_abs + os.sep) and dest_abs != target_abs:
                print(f"警告: 跳过可疑路径: {name}", file=sys.stderr)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)

            with zf.open(name) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            count += 1
            print(f"  -> {name}")

    print(f"\n解包完成: 共 {count} 个文件")
    emit("stage=unpack_complete", f"file_count={count}")
    emit("level=success")
    if warnings:
        print("注意: 存在版本不一致的插件，配置可能无法完全兼容")
    print("请确保 ClassIsland 已关闭后再启动。")


def list_command(args):
    global STRUCTURED_MODE
    STRUCTURED_MODE = getattr(args, "structured", False)
    archive = os.path.abspath(args.input)

    if not os.path.isfile(archive):
        print(f"错误: 打包文件不存在: {archive}", file=sys.stderr)
        sys.exit(1)

    with zipfile.ZipFile(archive, "r") as zf:
        names = zf.namelist()

        if "__manifest__.json" in names:
            with zf.open("__manifest__.json") as f:
                manifest = json.load(f)
            print("=== 打包信息 ===")
            print(f"  工具版本: {manifest.get('version', '未知')}")
            print(f"  创建时间: {manifest.get('created_at', '未知')}")
            print(f"  来源目录: {manifest.get('source_dir', '未知')}")
            print(f"  包含备份: {manifest.get('include_backups', False)}")
            print(f"  文件数: {manifest.get('file_count', '未知')}")

            plugins_raw = manifest.get("plugins", [])
            if isinstance(plugins_raw, list) and plugins_raw:
                print(f"\n=== 打包时已安装插件 ({len(plugins_raw)}) ===")
                for p in plugins_raw:
                    if not isinstance(p, dict):
                        continue
                    p_name = p.get("name", "未知插件")
                    p_version = p.get("version", "未知版本")
                    print(f"  {p_name} ({p_version})")
            print()

        print("=== 文件列表 ===")
        for name in sorted(names):
            if name == "__manifest__.json":
                continue
            info = zf.getinfo(name)
            size_str = f"{info.file_size / 1024:.1f} KB" if info.file_size > 1024 else f"{info.file_size} B"
            print(f"  {name}  ({size_str})")


def download_command(args):
    """仅下载 ClassIsland，不解包配置"""
    global STRUCTURED_MODE
    STRUCTURED_MODE = getattr(args, "structured", False)
    target = os.path.abspath(args.dir)

    need_ci1x = False

    if args.ci1x:
        need_ci1x = True
    elif args.ci2x:
        pass  # CI 2.x 是默认 (need_ci1x=False)
    elif args.ci_version:
        v = str(args.ci_version).lstrip("vV")
        if v.startswith("1."):
            need_ci1x = True

    emit("stage=check_install")
    # 若目标已有 CI，检测结构（仅作为倾向，不限制版本选择）
    if is_valid_ci_install(target):
        existing_structure = detect_ci_structure(target)
        print(f"目标目录已有 ClassIsland ({existing_structure})，将重新下载/覆盖")
        if not args.yes:
            resp = ask("是否继续? (y/N) ", default="N").strip().lower()
            if resp not in ("y", "yes"):
                print("已取消。")
                return

    if check_ci_running(target):
        print("警告: 检测到 ClassIsland 正在运行!", file=sys.stderr)
        emit("level=warning", message="检测到 ClassIsland 正在运行")
        print("请先关闭 ClassIsland 再继续。", file=sys.stderr)
        if not args.yes:
            resp = ask("是否继续? (y/N) ", default="N").strip().lower()
            if resp not in ("y", "yes"):
                print("已取消。请关闭 ClassIsland 后重试。")
                return

    # 无明确版本时，列出版本供用户选择（不限制 1.x/2.x）
    if not args.ci_version:
        selected_version = prompt_select_version(yes=args.yes)
        if selected_version is None:
            print("已取消。")
            return
        args.ci_version = selected_version
        if selected_version.startswith("1."):
            need_ci1x = True

    emit("stage=download")
    ok = ensure_ci_downloaded(
        target,
        need_ci1x=need_ci1x,
        download_version=args.ci_version,
        yes_download=args.yes_download,
        spoof_ua=args.spoof_ua,
        yes_skip=args.yes,
    )
    if ok:
        print(f"\nClassIsland 已安装到: {target}")
        emit("stage=complete", "result=success")
        emit("level=success")
    else:
        print("\n安装失败。", file=sys.stderr)
        emit("level=error", message="ClassIsland 安装失败")
        sys.exit(1)


def prompt_select_version(yes: bool = False) -> str | None:
    """列出版本供用户选择，返回选中的版本号（同时显示 1.x 和 2.x）"""
    versions: list[tuple[str, str]] = []  # (version, source)

    # CI 1.x: 从 disturb 索引获取版本列表
    print("正在获取 CI 1.x 可用版本...")
    disturb_versions = fetch_disturb_versions()
    if disturb_versions:
        for v in disturb_versions:
            if not isinstance(v, dict):
                continue
            ver = v.get("Version", "")
            if ver:
                versions.append((ver, "disturb"))

    # CI 2.x: 从 distribution API 获取
    print("正在获取 CI 2.x 可用版本...")
    channels_info = fetch_distribution_channels()
    if channels_info and isinstance(channels_info.get("channels"), dict):
        for ch_id, ch in channels_info["channels"].items():
            if not isinstance(ch, dict):
                continue
            ver = ch.get("latestVersion", "")
            if ver:
                versions.append((ver, f"distribution:{ch_id}"))

    # 从 GitHub 获取所有版本（1.x 和 2.x）
    gh_releases = fetch_all_github_releases(per_page=30)
    if gh_releases:
        for rel in gh_releases:
            tag = rel.get("tag_name", "")
            if tag:
                tag_stripped = tag.lstrip("vV")
                if not any(v == tag_stripped for v, _ in versions):
                    if tag_stripped.startswith("1.") or tag_stripped.startswith("2."):
                        versions.append((tag_stripped, "github"))

    if not versions:
        print("错误: 无法获取任何版本列表", file=sys.stderr)
        return None

    # 去重并排序（倒序，最新在前）
    seen = set()
    unique_versions = []
    for ver, src in versions:
        if ver not in seen:
            seen.add(ver)
            unique_versions.append((ver, src))
    unique_versions.sort(key=lambda x: _version_key(x[0]), reverse=True)

    if yes and unique_versions:
        print(f"自动选择最新版本: {unique_versions[0][0]} ({unique_versions[0][1]})")
        emit("level=info", "stage=select_version", "auto=true", f"selected={unique_versions[0][0]}")
        return unique_versions[0][0]

    # 结构化输出: 版本列表供 GUI 展示
    emit("level=options", "stage=select_version", f"count={len(unique_versions)}")
    for i, (ver, src) in enumerate(unique_versions, 1):
        emit("level=option", f"index={i}", f"value={ver}", f"source={src}")

    print("\n可用版本:")
    for i, (ver, src) in enumerate(unique_versions, 1):
        print(f"  {i}. {ver}  ({src})")

    print(f"  0. 取消")
    try:
        choice = ask(
            f"请选择版本 (0-{len(unique_versions)}): ",
            default="0",
            choices=[v for v, _ in unique_versions],
        ).strip()
        idx = int(choice)
        if idx == 0:
            return None
        if 1 <= idx <= len(unique_versions):
            return unique_versions[idx - 1][0]
        print("无效选择", file=sys.stderr)
        return None
    except (ValueError, EOFError):
        print("无效输入", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser(
        description="CIPacker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  打包配置:
    python CiPack.py pack D:\\ClassIsland -o ci_config.zip

  打包(含备份):
    python CiPack.py pack D:\\ClassIsland -o ci_config.zip --include-backups

  查看包内容:
    python CiPack.py list ci_config.zip

  解包到已有 CI 目录:
    python CiPack.py unpack ci_config.zip -d D:\\ClassIsland

  下载 ClassIsland:
    python CiPack.py download -d D:\\ClassIsland --ci-version 1.7.0.1
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pack = sub.add_parser("pack", help="打包配置")
    p_pack.add_argument("dir", help="ClassIsland 根目录")
    p_pack.add_argument("-o", "--output", default="ci_config.zip", help="输出 zip 路径 (默认: ci_config.zip)")
    p_pack.add_argument("--include-backups", action="store_true", help="包含 data/Backups 目录")
    p_pack.add_argument("--official-format", action="store_true", help="导出为官方 .cidata 格式（不含 __manifest__.json）")
    p_pack.add_argument("--structured", action="store_true", help="启用结构化输出（供 GUI 外壳解析）")
    p_pack.set_defaults(func=pack_command)

    p_unpack = sub.add_parser("unpack", help="解包配置到已有 CI 目录")
    p_unpack.add_argument("input", help="打包文件 (.zip)")
    p_unpack.add_argument("-d", "--dir", required=True, help="目标 ClassIsland 根目录")
    p_unpack.add_argument("-y", "--yes", action="store_true", help="跳过所有确认（覆盖配置、结构不兼容等）")
    p_unpack.add_argument("--yes-download", action="store_true", help="跳过下载确认提示并自动选择下载源")
    p_unpack.add_argument("--spoof-ua", action="store_true", help="使用浏览器 User-Agent 下载(可能提升某些源的下载速度)")
    p_unpack.add_argument("--ci-version", default=None, help="指定下载的 CI 版本 tag (如 v2.0.0),默认最新版")
    p_unpack.add_argument("--structured", action="store_true", help="启用结构化输出（供 GUI 外壳解析）")
    p_unpack.set_defaults(func=unpack_command)

    p_download = sub.add_parser("download", help="下载 ClassIsland 到指定目录")
    p_download.add_argument("-d", "--dir", required=True, help="目标安装目录")
    p_download.add_argument("--ci-version", default=None, help="指定版本 (如 1.7.0.1 或 2.1.0.1)")
    ci_group = p_download.add_mutually_exclusive_group()
    ci_group.add_argument("--ci1x", action="store_true", help="强制下载 CI 1.x 版本")
    ci_group.add_argument("--ci2x", action="store_true", help="强制下载 CI 2.x 版本")
    p_download.add_argument("-y", "--yes", action="store_true", help="跳过所有确认")
    p_download.add_argument("--yes-download", action="store_true", help="跳过下载确认提示并自动选择下载源")
    p_download.add_argument("--spoof-ua", action="store_true", help="使用浏览器 User-Agent 下载")
    p_download.add_argument("--structured", action="store_true", help="启用结构化输出（供 GUI 外壳解析）")
    p_download.set_defaults(func=download_command)

    p_list = sub.add_parser("list", help="列出打包文件内容")
    p_list.add_argument("input", help="打包文件 (.zip)")
    p_list.add_argument("--structured", action="store_true", help="启用结构化输出（供 GUI 外壳解析）")
    p_list.set_defaults(func=list_command)

    args = parser.parse_args()
    try:
        args.func(args)
        emit("result=success", "exit_code=0")
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        emit("result=cancelled", "exit_code=130")
        sys.exit(130)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if code != 0:
            emit("result=failed", f"exit_code={code}")
        raise
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        emit("result=failed", "exit_code=1", message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
