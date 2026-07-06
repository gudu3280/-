"""
PyInstaller 打包脚本

使用方法:
    python build.py          # 打包为目录（推荐）
    python build.py --onefile # 打包为单文件

打包后的 exe 位于: desktop/dist/超星助手/超星助手.exe

注意事项:
1. 打包前确保已安装所有依赖: pip install -r requirements.txt
2. 使用项目自带的 Chromium（不打包系统 Chromium）
3. data/ 和 .env 不打包，由应用首次运行时创建
"""

import os
import sys
import subprocess
import shutil
import zipfile

# 项目根目录
DESKTOP_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(DESKTOP_DIR, "dist")
BUILD_DIR = os.path.join(DESKTOP_DIR, "build")

# 应用配置
APP_NAME = "超星助手"
MAIN_SCRIPT = os.path.join(DESKTOP_DIR, "main.py")
ICON_FILE = os.path.join(DESKTOP_DIR, "assets", "icon.ico")

# 版本号
VERSION_FILE = os.path.join(DESKTOP_DIR, "version.py")
APP_VERSION = "1.0.0"
try:
    with open(VERSION_FILE, encoding="utf-8") as f:
        for line in f:
            if line.startswith("__version__"):
                APP_VERSION = line.split("=")[1].strip().strip("\"'")
                break
except FileNotFoundError:
    pass

# 数据文件（打包时需要包含，不含 .env）
DATA_FILES = [
    (os.path.join(DESKTOP_DIR, "assets"), "assets"),
    (VERSION_FILE, "."),
]


def clean_build():
    """清理构建目录"""
    for d in [DIST_DIR, BUILD_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d)
    print("[OK] 已清理构建目录")


def check_dependencies():
    """检查依赖是否已安装"""
    required = ["PyInstaller", "PyQt5", "zendriver", "qasync"]
    missing = []
    for pkg in required:
        import_name = pkg
        if pkg == "PyInstaller":
            import_name = "PyInstaller"
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(f"[ERROR] 缺少依赖: {', '.join(missing)}")
        print("请先运行: pip install -r requirements.txt && pip install pyinstaller")
        return False

    # 检查数据文件
    for src, _ in DATA_FILES:
        if not os.path.exists(src):
            print(f"[WARN] 数据文件不存在: {src}")

    return True


def build(onefile: bool = False):
    """执行打包"""
    mode = "单文件" if onefile else "目录"
    print(f"{'=' * 50}")
    print(f"  打包: {APP_NAME} v{APP_VERSION}")
    print(f"  模式: {mode}")
    print(f"{'=' * 50}")

    # 构建 PyInstaller 参数
    args = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--windowed",           # 不显示控制台窗口
        "--noconfirm",          # 不确认覆盖
        "--clean",              # 清理缓存
    ]

    if onefile:
        args.append("--onefile")
    else:
        args.append("--onedir")

    # 图标
    if os.path.exists(ICON_FILE):
        args.extend(["--icon", ICON_FILE])

    # 数据文件
    separator = ";" if sys.platform == "win32" else ":"
    for src, dst in DATA_FILES:
        if os.path.exists(src):
            args.extend(["--add-data", f"{src}{separator}{dst}"])

    # updater_helper.py 作为数据文件
    helper_file = os.path.join(DESKTOP_DIR, "updater_helper.py")
    if os.path.exists(helper_file):
        args.extend(["--add-data", f"{helper_file}{separator}."])

    # 隐藏导入
    hidden_imports = [
        # 核心模块
        "core", "core.config", "core.browser", "core.chaoxing",
        "core.answer_engine", "core.font_decrypt", "core.task_runner",
        "core.completion_db",
        # UI 模块
        "ui", "ui.login_window", "ui.main_window", "ui.styles", "ui.widgets",
        "ui.floating_ball",
        # 浏览器自动化
        "zendriver", "zendriver.cdp", "zendriver.core",
        "zendriver.core.connection", "zendriver.core.element",
        "zendriver.core.tab", "zendriver.core.browser",
        # 异步支持
        "qasync",
        # HTTP 与网络
        "httpx", "httpx._transports", "httpx._transports.default",
        "websockets", "websockets.client",
        # 工具库
        "dotenv", "aiofiles", "fonttools", "fontTools",
        # 验证码识别 (ddddocr)
        "ddddocr", "onnxruntime", "PIL", "PIL.Image",
        "cv2", "numpy", "flatbuffers", "protobuf",
        "google.protobuf",
        # PyQt5
        "PyQt5.QtCore", "PyQt5.QtGui", "PyQt5.QtWidgets",
        "PyQt5.QtNetwork",
        # 打包更新相关
        "zipfile", "urllib.request", "json",
    ]
    for imp in hidden_imports:
        args.extend(["--hidden-import", imp])

    # 排除不需要的模块（减小体积）
    excludes = [
        "tkinter", "matplotlib", "scipy", "pandas",
        "IPython", "jupyter", "pytest", "playwright",
        # 排除其他 Qt 绑定（项目使用 PyQt5）
        "PySide6", "PySide2", "PyQt4",
    ]
    for exc in excludes:
        args.extend(["--exclude-module", exc])

    # 输出目录
    args.extend(["--distpath", DIST_DIR])
    args.extend(["--workpath", BUILD_DIR])

    # spec 文件目录
    args.extend(["--specpath", DESKTOP_DIR])

    # 主脚本
    args.append(MAIN_SCRIPT)

    print(f"\n执行命令:")
    print(" ".join(args))
    print()

    # 执行打包
    result = subprocess.run(args, cwd=DESKTOP_DIR)

    if result.returncode == 0:
        print(f"\n{'=' * 50}")
        print(f"  打包成功!")
        if onefile:
            exe_path = os.path.join(DIST_DIR, f"{APP_NAME}.exe")
            print(f"  输出: {exe_path}")
            if os.path.exists(exe_path):
                size_mb = os.path.getsize(exe_path) / (1024 * 1024)
                print(f"  大小: {size_mb:.1f} MB")
        else:
            out_dir = os.path.join(DIST_DIR, APP_NAME)
            print(f"  输出目录: {out_dir}")

        # 打包后生成 release zip
        _create_release_zip()

        print(f"{'=' * 50}")
    else:
        print(f"\n[ERROR] 打包失败，返回码: {result.returncode}")


def _create_release_zip():
    """打包后生成 release zip"""
    out_dir = os.path.join(DIST_DIR, APP_NAME)
    if not os.path.isdir(out_dir):
        print("[WARN] 输出目录不存在，跳过 zip 生成")
        return

    zip_name = f"{APP_NAME}-v{APP_VERSION}.zip"
    zip_path = os.path.join(DIST_DIR, zip_name)

    print(f"\n正在生成发布包: {zip_name}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(out_dir):
            # 排除不需要的文件
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
            for f in files:
                if f.endswith(".pyc"):
                    continue
                file_path = os.path.join(root, f)
                arc_name = os.path.join(APP_NAME, os.path.relpath(file_path, out_dir))
                zf.write(file_path, arc_name)

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"  发布包: {zip_path}")
    print(f"  大小: {size_mb:.1f} MB")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="超星助手打包工具")
    parser.add_argument("--onefile", action="store_true", help="打包为单文件")
    parser.add_argument("--clean", action="store_true", help="仅清理构建目录")
    parser.add_argument("--check", action="store_true", help="仅检查依赖")
    parser.add_argument("--version", action="store_true", help="显示版本号")
    args = parser.parse_args()

    if args.version:
        print(f"{APP_NAME} v{APP_VERSION}")
        return

    if args.clean:
        clean_build()
        return

    if args.check:
        check_dependencies()
        return

    if not check_dependencies():
        return

    clean_build()
    build(onefile=args.onefile)


if __name__ == "__main__":
    main()
