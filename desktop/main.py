"""
超星学习通桌面助手 - 入口文件

启动流程:
1. 创建QApplication
2. 加载样式
3. 直接显示主窗口(设置面板 + 课程面板)
4. 用户配置参数后手动点击「启动浏览器」
5. 浏览器就绪后激活课程选择和任务执行
"""

import sys
import os
import logging

# 将 desktop 目录加入 Python 路径
DESKTOP_DIR = os.path.dirname(os.path.abspath(__file__))
if DESKTOP_DIR not in sys.path:
    sys.path.insert(0, DESKTOP_DIR)

# 修复 venv 中 Qt 平台插件路径问题
def _fix_qt_plugin_path():
    try:
        import PyQt5
        plugin_path = os.path.join(
            os.path.dirname(PyQt5.__file__), 'Qt5', 'plugins', 'platforms'
        )
        if os.path.exists(plugin_path):
            os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = plugin_path
    except ImportError:
        pass

_fix_qt_plugin_path()

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('chaoxing_debug.log', encoding='utf-8', mode='w'),
    ],
)
logger = logging.getLogger(__name__)


def main():
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import Qt

    # 高DPI支持
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("超星学习通助手")

    # 加载样式
    from ui.styles import DARK_THEME
    app.setStyleSheet(DARK_THEME)

    # 直接显示主窗口
    from ui.main_window import MainWindow

    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
