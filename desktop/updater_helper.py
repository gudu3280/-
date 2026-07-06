"""
updater_helper.py - 独立更新辅助脚本

在主进程退出后，替换安装目录文件并重启应用。

用法:
    python updater_helper.py <主进程PID> <zip路径> <安装目录> [exe名称]

流程:
1. 等待主进程 PID 退出
2. 解压 zip 覆盖安装目录（保留 data/, .env, browser_data/）
3. 启动新 exe
4. 自行退出
"""

import os
import sys
import time
import zipfile
import subprocess
import shutil
import tempfile

# 更新时需要保留的目录/文件（用户数据）
PRESERVE_PATTERNS = [
    "data",
    ".env",
    "browser_data",
    "chaoxing_debug.log",
    "completion.db",
]


def wait_for_process_exit(pid: int, timeout: int = 30):
    """等待指定 PID 的进程退出"""
    print(f"[updater] 等待进程 {pid} 退出...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            # Windows: 使用 tasklist 检查进程是否存在
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            if str(pid) not in result.stdout:
                print(f"[updater] 进程 {pid} 已退出")
                return True
        except Exception:
            pass
        time.sleep(0.5)

    print(f"[updater] 超时，尝试强制结束进程 {pid}")
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], timeout=10)
    except Exception:
        pass
    time.sleep(1)
    return True


def apply_update(zip_path: str, install_dir: str):
    """解压 zip 覆盖安装目录，保留用户数据"""
    print(f"[updater] 解压: {zip_path}")
    print(f"[updater] 目标: {install_dir}")

    if not os.path.isfile(zip_path):
        print(f"[updater] ERROR: zip 文件不存在: {zip_path}")
        return False

    # 创建临时解压目录
    tmp_dir = tempfile.mkdtemp(prefix="chaoxing_update_")
    try:
        # 解压
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_dir)

        # 找到解压后的根目录（可能是 zip 内的顶层文件夹）
        extracted_items = os.listdir(tmp_dir)
        if len(extracted_items) == 1 and os.path.isdir(os.path.join(tmp_dir, extracted_items[0])):
            source_dir = os.path.join(tmp_dir, extracted_items[0])
        else:
            source_dir = tmp_dir

        print(f"[updater] 源目录: {source_dir}")

        # 复制文件到安装目录（跳过保留的用户数据）
        _copy_tree(source_dir, install_dir)

        print("[updater] 更新文件已替换")
        return True

    except Exception as e:
        print(f"[updater] ERROR: 解压/复制失败: {e}")
        return False
    finally:
        # 清理临时目录
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def _copy_tree(src_dir: str, dst_dir: str):
    """递归复制目录树，跳过需要保留的用户数据"""
    for item in os.listdir(src_dir):
        src_path = os.path.join(src_dir, item)
        dst_path = os.path.join(dst_dir, item)

        # 检查是否是需要保留的用户数据
        if item.lower() in [p.lower() for p in PRESERVE_PATTERNS]:
            print(f"[updater] 保留用户数据: {item}")
            continue

        if os.path.isdir(src_path):
            # 目录：递归复制
            if not os.path.exists(dst_path):
                os.makedirs(dst_path)
            _copy_tree(src_path, dst_path)
        else:
            # 文件：覆盖复制
            shutil.copy2(src_path, dst_path)


def launch_app(install_dir: str, exe_name: str = "超星助手.exe"):
    """启动应用"""
    exe_path = os.path.join(install_dir, exe_name)
    if os.path.isfile(exe_path):
        print(f"[updater] 启动: {exe_path}")
        subprocess.Popen([exe_path], cwd=install_dir)
        return True
    else:
        print(f"[updater] ERROR: exe 不存在: {exe_path}")
        return False


def main():
    if len(sys.argv) < 4:
        print("用法: updater_helper.py <PID> <zip路径> <安装目录> [exe名称]")
        sys.exit(1)

    pid = int(sys.argv[1])
    zip_path = sys.argv[2]
    install_dir = sys.argv[3]
    exe_name = sys.argv[4] if len(sys.argv) > 4 else "超星助手.exe"

    print(f"[updater] PID={pid}, zip={zip_path}, dir={install_dir}")

    # 1. 等待主进程退出
    wait_for_process_exit(pid)

    # 2. 应用更新
    if not apply_update(zip_path, install_dir):
        print("[updater] 更新失败，退出")
        sys.exit(1)

    # 3. 重启应用
    launch_app(install_dir, exe_name)

    # 4. 清理 zip
    try:
        os.remove(zip_path)
        print(f"[updater] 已清理: {zip_path}")
    except Exception:
        pass

    print("[updater] 更新完成")
    sys.exit(0)


if __name__ == "__main__":
    main()
