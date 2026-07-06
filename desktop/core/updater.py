"""
自动更新模块 - 通过 GitHub / GitCode Releases 检查并下载更新

流程:
    启动 -> 后台检查版本 -> 有新版本则弹窗提示 -> 用户点击更新 ->
    下载 zip -> 启动 updater_helper.py -> 主进程退出 ->
    helper 替换文件 -> 重启应用

支持平台:
    - GitHub: GITHUB_REPO = "owner/repo"
    - GitCode: GITHUB_REPO = "gitcode:owner/repo"
"""

import os
import sys
import json
import logging
import tempfile
import subprocess
from dataclasses import dataclass
from typing import Optional, Callable
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)

DEFAULT_REPO = ""  # 需要在 config 中设置


def _detect_platform(repo: str) -> tuple:
    """检测平台，返回 (api_base, owner_repo, is_github)
    
    支持格式:
        "owner/repo"              -> GitHub
        "github:owner/repo"       -> GitHub
        "gitcode:owner/repo"      -> GitCode
    """
    if repo.startswith("gitcode:"):
        return "https://gitcode.com/api/v5", repo[8:], False
    elif repo.startswith("github:"):
        return "https://api.github.com", repo[7:], True
    else:
        # 默认 GitHub
        return "https://api.github.com", repo, True


@dataclass
class ReleaseInfo:
    """版本发布信息"""
    tag_name: str
    version: str
    body: str  # changelog
    download_url: str
    asset_name: str
    asset_size: int  # bytes


def _parse_version(tag: str) -> tuple:
    """将 'v1.2.3' 或 '1.2.3' 解析为 (1, 2, 3)"""
    tag = tag.lstrip("vV").strip()
    parts = tag.split(".")
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            result.append(0)
    while len(result) < 3:
        result.append(0)
    return tuple(result[:3])


def get_local_version() -> str:
    """获取本地版本号"""
    try:
        # PyInstaller 打包后，version.py 在 _MEIPASS 目录
        if getattr(sys, 'frozen', False):
            base = sys._MEIPASS
        else:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        version_file = os.path.join(base, "version.py")
        if os.path.isfile(version_file):
            with open(version_file, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("__version__"):
                        return line.split("=")[1].strip().strip("\"'")
    except Exception as e:
        logger.debug(f"读取本地版本失败: {e}")
    return "0.0.0"


def check_update(repo: str = DEFAULT_REPO) -> Optional[ReleaseInfo]:
    """
    检查是否有新版本（支持 GitHub 和 GitCode）。
    返回 ReleaseInfo 如果有新版本，否则返回 None。
    """
    if not repo:
        logger.debug("未配置 GITHUB_REPO，跳过更新检查")
        return None

    try:
        local_ver = get_local_version()
        logger.info(f"当前版本: {local_ver}")

        api_base, owner_repo, is_github = _detect_platform(repo)
        logger.info(f"更新平台: {'GitHub' if is_github else 'GitCode'}, 仓库: {owner_repo}")

        # 获取最新 release
        url = f"{api_base}/repos/{owner_repo}/releases/latest"
        headers = {"Accept": "application/vnd.github.v3+json"} if is_github else {}
        req = Request(url, headers=headers)
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        tag = data.get("tag_name", "")
        remote_ver_tuple = _parse_version(tag)
        local_ver_tuple = _parse_version(local_ver)

        logger.info(f"远程版本: {tag} -> {remote_ver_tuple}, 本地: {local_ver} -> {local_ver_tuple}")

        if remote_ver_tuple <= local_ver_tuple:
            logger.info("已是最新版本")
            return None

        # 找到 zip 资产
        assets = data.get("assets", [])
        zip_asset = None
        for asset in assets:
            name = asset.get("name", "")
            if name.endswith(".zip"):
                zip_asset = asset
                break

        if not zip_asset:
            logger.warning("Release 中没有找到 zip 文件")
            return None

        # 下载 URL: GitHub 用 browser_download_url, GitCode 也用 browser_download_url
        download_url = zip_asset.get("browser_download_url", "")
        if not download_url:
            # GitCode 备选字段
            download_url = zip_asset.get("url", "")

        return ReleaseInfo(
            tag_name=tag,
            version=tag.lstrip("vV"),
            body=data.get("body", ""),
            download_url=download_url,
            asset_name=zip_asset.get("name", ""),
            asset_size=zip_asset.get("size", 0),
        )

    except (URLError, HTTPError) as e:
        logger.warning(f"检查更新网络请求失败: {e}")
        return None
    except Exception as e:
        logger.warning(f"检查更新失败: {e}")
        return None


def download_update(
    release: ReleaseInfo,
    progress_callback: Optional[Callable[[int, int], None]] = None
) -> Optional[str]:
    """
    下载更新 zip 到临时文件。
    progress_callback(downloaded, total) 用于更新进度。
    返回 zip 文件路径，失败返回 None。
    """
    if not release.download_url:
        logger.error("没有下载 URL")
        return None

    try:
        tmp_dir = tempfile.gettempdir()
        zip_path = os.path.join(tmp_dir, release.asset_name or "update.zip")

        logger.info(f"下载更新: {release.download_url}")
        req = Request(release.download_url)
        with urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", release.asset_size))
            downloaded = 0
            chunk_size = 64 * 1024

            with open(zip_path, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)

        logger.info(f"下载完成: {zip_path} ({downloaded} bytes)")
        return zip_path

    except Exception as e:
        logger.error(f"下载更新失败: {e}")
        return None


def apply_update(zip_path: str, exe_name: str = "超星助手.exe"):
    """
    启动 updater_helper 脚本进行文件替换。
    调用后主进程应退出。
    """
    if not os.path.isfile(zip_path):
        logger.error(f"zip 文件不存在: {zip_path}")
        return False

    # 确定安装目录
    if getattr(sys, 'frozen', False):
        install_dir = os.path.dirname(sys.executable)
    else:
        install_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 找到 updater_helper.py
    if getattr(sys, 'frozen', False):
        helper_path = os.path.join(sys._MEIPASS, "updater_helper.py")
    else:
        helper_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "updater_helper.py"
        )

    if not os.path.isfile(helper_path):
        logger.error(f"updater_helper.py 不存在: {helper_path}")
        return False

    # 当前进程 PID
    pid = os.getpid()

    # 启动 helper（独立进程，不阻塞当前进程）
    cmd = [sys.executable, helper_path, str(pid), zip_path, install_dir, exe_name]
    logger.info(f"启动更新助手: {' '.join(cmd)}")

    try:
        # 使用 CREATE_NEW_PROCESS_GROUP 在 Windows 上确保独立运行
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        subprocess.Popen(cmd, **kwargs)
        return True

    except Exception as e:
        logger.error(f"启动更新助手失败: {e}")
        return False
