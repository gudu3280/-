"""
登录窗口 - 无边框自定义设计

灵感: 参考 Login.vue 的简洁居中 + 现代化无边框设计
- 自定义标题栏 (拖拽移动、关闭/最小化按钮)
- 圆角卡片式布局
- Unicode 图标输入框
- 自定义样式弹窗 (替代丑陋的 QMessageBox)
"""

import asyncio
import logging

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QFrame, QGraphicsDropShadowEffect,
    QSpacerItem, QSizePolicy, QApplication, QDialog,
)
from PyQt5.QtCore import Qt, pyqtSignal, QThread, QPoint, QRect, QRectF, QSize, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import (
    QFont, QColor, QPainter, QPainterPath, QLinearGradient,
    QCursor, QPen, QBrush,
)

from core.config import Config
from core.browser import BrowserManager
from core.chaoxing import ChaoxingOperator

logger = logging.getLogger(__name__)


# ============================================================
# 自定义弹窗 (替代丑陋的 QMessageBox)
# ============================================================

class StyledDialog(QDialog):
    """自定义样式弹窗 - 无边框 + 深色主题"""

    def __init__(self, parent=None, title="", message="", level="info", buttons=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self._drag_pos = None

        # 颜色方案
        colors = {
            "info": ("#409EFF", "💡"),
            "success": ("#67C23A", "✅"),
            "warning": ("#E6A23C", "⚠️"),
            "error": ("#F56C6C", "❌"),
            "question": ("#409EFF", "❓"),
        }
        accent, icon = colors.get(level, colors["info"])

        # 主容器
        container = QWidget(self)
        container.setObjectName("dialogContainer")
        container.setStyleSheet(f"""
            #dialogContainer {{
                background-color: #1e1e36;
                border-radius: 12px;
                border: 1px solid #2a2a4a;
            }}
        """)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(16)

        # 标题行
        header = QHBoxLayout()
        icon_label = QLabel(icon)
        icon_label.setStyleSheet(f"font-size: 20px; background: transparent;")
        header.addWidget(icon_label)

        title_label = QLabel(title)
        title_label.setStyleSheet(f"""
            font-size: 16px; font-weight: bold; color: {accent};
            background: transparent;
        """)
        header.addWidget(title_label)
        header.addStretch()
        layout.addLayout(header)

        # 消息文本
        msg_label = QLabel(message)
        msg_label.setWordWrap(True)
        msg_label.setStyleSheet("""
            font-size: 13px; color: #c0c0d0; background: transparent;
            line-height: 1.6;
        """)
        layout.addWidget(msg_label)

        # 按钮区
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        if buttons is None:
            buttons = [("确定", True)]

        self._result = None
        for text, is_accept in buttons:
            btn = QPushButton(text)
            btn.setFixedSize(90, 36)
            if is_accept:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {accent}; color: white;
                        border-radius: 8px; font-weight: bold; font-size: 13px;
                        border: none;
                    }}
                    QPushButton:hover {{ background-color: {accent}cc; }}
                    QPushButton:pressed {{ background-color: {accent}99; }}
                """)
                btn.clicked.connect(lambda checked, a=is_accept: self._accept(a))
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #2a2a4a; color: #a0a0b0;
                        border-radius: 8px; font-size: 13px;
                        border: 1px solid #3a3a5a;
                    }
                    QPushButton:hover { background-color: #3a3a5a; color: #e0e0e0; }
                """)
                btn.clicked.connect(self.reject)
            btn_layout.addWidget(btn)

        layout.addLayout(btn_layout)

        # 外层布局
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(container)

        # 阴影
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(0, 0, 0, 160))
        shadow.setOffset(0, 4)
        container.setGraphicsEffect(shadow)

        self.setFixedSize(380, 10)  # 高度自适应
        self.adjustSize()

    def _accept(self, result=True):
        self._result = result
        self.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.LeftButton:
            self.move(event.globalPos() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None


def show_styled_message(parent, title, message, level="info", buttons=None):
    """显示自定义样式弹窗并返回结果"""
    dlg = StyledDialog(parent, title, message, level, buttons)
    dlg.exec_()
    return dlg._result


# ============================================================
# 自定义输入框控件
# ============================================================

class IconLineEdit(QFrame):
    """带前导图标的输入框"""

    textChanged = pyqtSignal(str)
    returnPressed = pyqtSignal()

    def __init__(self, icon="📧", placeholder="", echo_mode=QLineEdit.Normal, parent=None):
        super().__init__(parent)
        self.setObjectName("iconLineEdit")
        self.setStyleSheet("""
            #iconLineEdit {
                background-color: #0f1a3a;
                border: 1.5px solid #2a2a5a;
                border-radius: 10px;
            }
        """)
        self.setFixedHeight(46)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(10)

        # 图标
        self._icon_label = QLabel(icon)
        self._icon_label.setFixedWidth(24)
        self._icon_label.setAlignment(Qt.AlignCenter)
        self._icon_label.setStyleSheet("font-size: 16px; background: transparent; border: none;")
        layout.addWidget(self._icon_label)

        # 输入框
        self._input = QLineEdit()
        self._input.setPlaceholderText(placeholder)
        self._input.setEchoMode(echo_mode)
        self._input.setStyleSheet("""
            QLineEdit {
                background: transparent; border: none; color: #e8e8e8;
                font-size: 14px; padding: 0;
            }
            QLineEdit::placeholder { color: #505070; }
        """)
        self._input.textChanged.connect(self.textChanged.emit)
        self._input.returnPressed.connect(self.returnPressed.emit)
        # 焦点事件绑定
        self._input.installEventFilter(self)
        layout.addWidget(self._input)

    def eventFilter(self, obj, event):
        if obj == self._input:
            if event.type() == 8:  # QEvent.FocusIn
                self.setStyleSheet("""
                    #iconLineEdit {
                        background-color: #0f1a3a;
                        border: 1.5px solid #409EFF;
                        border-radius: 10px;
                    }
                """)
            elif event.type() == 9:  # QEvent.FocusOut
                self.setStyleSheet("""
                    #iconLineEdit {
                        background-color: #0f1a3a;
                        border: 1.5px solid #2a2a5a;
                        border-radius: 10px;
                    }
                """)
        return False

    def text(self):
        return self._input.text()

    def setText(self, text):
        self._input.setText(text)

    def setFocus(self, reason=Qt.OtherFocusReason):
        self._input.setFocus(reason)


# ============================================================
# 登录工作线程
# ============================================================

class LoginWorker(QThread):
    """登录工作线程(避免阻塞GUI)"""

    login_success = pyqtSignal()
    login_failed = pyqtSignal(str)
    status_update = pyqtSignal(str)

    def __init__(self, browser: BrowserManager, username: str, password: str):
        super().__init__()
        self._browser = browser
        self._username = username
        self._password = password

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._do_login())
        except Exception as e:
            self.login_failed.emit(f"登录异常: {e}")
        finally:
            loop.close()

    async def _do_login(self):
        try:
            self.status_update.emit("正在启动浏览器...")
            if not self._browser.is_started:
                await self._browser.start(headless=False)

            page = self._browser.tab

            self.status_update.emit("检查登录状态...")
            if await self._browser.is_logged_in():
                self.status_update.emit("已检测到登录状态")
                self.login_success.emit()
                return

            if not self._username or not self._password:
                self.login_failed.emit("请输入账号和密码")
                return

            self.status_update.emit("正在登录...")
            operator = ChaoxingOperator()
            success = await operator.login(page, self._username, self._password, browser=self._browser._browser)

            if success:
                self.status_update.emit("登录成功")
                self.login_success.emit()
            else:
                self.login_failed.emit(
                    "登录失败，可能需要验证码或二次确认。\n"
                    "请在弹出的浏览器窗口中手动完成登录。"
                )

        except Exception as e:
            self.login_failed.emit(str(e))


# ============================================================
# 自定义标题栏
# ============================================================

class TitleBar(QWidget):
    """自定义标题栏 - 包含拖拽移动和关闭/最小化按钮"""

    close_clicked = pyqtSignal()
    minimize_clicked = pyqtSignal()

    def __init__(self, title="超星学习通助手", parent=None):
        super().__init__(parent)
        self._drag_pos = None
        self.setFixedHeight(40)
        self.setStyleSheet("background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 6, 0)

        # 标题
        title_label = QLabel(title)
        title_label.setStyleSheet("color: #606080; font-size: 12px; background: transparent; border: none;")
        layout.addWidget(title_label)

        layout.addStretch()

        # 最小化按钮
        min_btn = QPushButton("—")
        min_btn.setFixedSize(36, 30)
        min_btn.setCursor(QCursor(Qt.PointingHandCursor))
        min_btn.setStyleSheet("""
            QPushButton {
                color: #606080; background: transparent; border: none;
                font-size: 14px; font-weight: bold; border-radius: 4px;
            }
            QPushButton:hover { background: #2a2a4a; color: #e0e0e0; }
        """)
        min_btn.clicked.connect(self.minimize_clicked.emit)
        layout.addWidget(min_btn)

        # 关闭按钮
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 30)
        close_btn.setCursor(QCursor(Qt.PointingHandCursor))
        close_btn.setStyleSheet("""
            QPushButton {
                color: #606080; background: transparent; border: none;
                font-size: 13px; font-weight: bold; border-radius: 4px;
            }
            QPushButton:hover { background: #e81123; color: white; }
        """)
        close_btn.clicked.connect(self.close_clicked.emit)
        layout.addWidget(close_btn)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.LeftButton:
            window = self.window()
            window.move(window.pos() + event.globalPos() - self._drag_pos)
            self._drag_pos = event.globalPos()
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None


# ============================================================
# 登录窗口
# ============================================================

class LoginWindow(QWidget):
    """
    无边框登录窗口

    - 自定义标题栏 (拖拽/关闭/最小化)
    - 圆角深色卡片设计
    - 图标输入框
    - 自定义弹窗替代QMessageBox
    """

    login_completed = pyqtSignal(object)

    def __init__(self, shared_browser=None):
        super().__init__()
        self._config = Config()
        self._shared_browser = shared_browser
        self._browser = shared_browser or BrowserManager(self._config)
        self._owns_browser = shared_browser is None  # 只有本地创建的浏览器才负责关闭
        self._worker = None

        self._init_ui()
        self._load_saved_config()

    def _init_ui(self):
        # 无边框 + 透明背景
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(440, 580)
        self.setWindowTitle("超星学习通助手 - 登录")

        # 主卡片容器
        self._card = QFrame(self)
        self._card.setObjectName("loginCard")
        self._card.setGeometry(0, 0, 440, 580)
        self._card.setStyleSheet("""
            #loginCard {
                background-color: #12122a;
                border-radius: 16px;
                border: 1px solid #2a2a4a;
            }
        """)

        # 阴影
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(40)
        shadow.setColor(QColor(0, 0, 0, 140))
        shadow.setOffset(0, 6)
        self._card.setGraphicsEffect(shadow)

        # 卡片内布局
        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 自定义标题栏
        self._title_bar = TitleBar("超星学习通助手 - 登录")
        self._title_bar.close_clicked.connect(self.close)
        self._title_bar.minimize_clicked.connect(self.showMinimized)
        layout.addWidget(self._title_bar)

        # 内容区
        content = QVBoxLayout()
        content.setContentsMargins(44, 10, 44, 36)
        content.setSpacing(0)

        # === Logo / 标题 ===
        content.addSpacing(16)

        # 大标题
        title = QLabel("超星助手")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("""
            font-size: 32px; font-weight: bold; color: #409EFF;
            background: transparent; border: none;
            letter-spacing: 4px;
        """)
        content.addWidget(title)

        content.addSpacing(4)

        # 副标题
        subtitle = QLabel("CHAOXING AUTO TOOLKIT")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("""
            font-size: 11px; color: #505070;
            background: transparent; border: none;
            letter-spacing: 3px;
        """)
        content.addWidget(subtitle)

        content.addSpacing(12)

        # 分隔线 (渐变色)
        divider = QFrame()
        divider.setFixedHeight(2)
        divider.setStyleSheet("""
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 transparent, stop:0.2 #409EFF44,
                stop:0.5 #409EFF, stop:0.8 #409EFF44,
                stop:1 transparent
            );
            border: none;
        """)
        content.addWidget(divider)

        content.addSpacing(28)

        # === 输入区 ===
        # 学习通账号
        acc_label = QLabel("学习通账号")
        acc_label.setStyleSheet("font-size: 12px; color: #707090; background: transparent; border: none; margin-bottom: 4px;")
        content.addWidget(acc_label)
        content.addSpacing(6)

        self._username_input = IconLineEdit("👤", "邮箱 / 学号")
        content.addWidget(self._username_input)

        content.addSpacing(18)

        # 密码
        pwd_label = QLabel("密码")
        pwd_label.setStyleSheet("font-size: 12px; color: #707090; background: transparent; border: none; margin-bottom: 4px;")
        content.addWidget(pwd_label)
        content.addSpacing(6)

        self._password_input = IconLineEdit("🔒", "******", QLineEdit.Password)
        content.addWidget(self._password_input)

        content.addSpacing(18)

        # DeepSeek API Key
        api_label = QLabel("DeepSeek API Key")
        api_label.setStyleSheet("font-size: 12px; color: #707090; background: transparent; border: none; margin-bottom: 4px;")
        content.addWidget(api_label)
        content.addSpacing(6)

        self._api_key_input = IconLineEdit("🔑", "sk-... / AI密钥")
        content.addWidget(self._api_key_input)

        content.addSpacing(20)

        # 记住登录
        self._remember_cb = QCheckBox(" 记住登录信息")
        self._remember_cb.setStyleSheet("""
            QCheckBox {
                color: #808098; font-size: 12px;
                background: transparent; border: none;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px; height: 16px;
                border-radius: 4px;
                border: 1.5px solid #3a3a5a;
                background-color: #0f1a3a;
            }
            QCheckBox::indicator:checked {
                background-color: #409EFF;
                border-color: #409EFF;
                image: none;
            }
        """)
        content.addWidget(self._remember_cb)

        content.addSpacing(12)

        # 状态标签
        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setStyleSheet("""
            font-size: 12px; color: #707090;
            background: transparent; border: none;
            padding: 2px;
        """)
        self._status_label.setWordWrap(True)
        content.addWidget(self._status_label)

        content.addStretch()

        # === 按钮区 ===
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        self._cancel_btn = QPushButton("取消登录")
        self._cancel_btn.setFixedHeight(44)
        self._cancel_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e1e3a; color: #808098;
                border: 1px solid #2a2a4a; border-radius: 10px;
                font-size: 14px; font-weight: bold;
            }
            QPushButton:hover { background-color: #2a2a4a; color: #c0c0d0; }
            QPushButton:pressed { background-color: #151530; }
            QPushButton:disabled { color: #404060; }
        """)
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self._cancel_btn, 1)

        self._login_btn = QPushButton("登  录")
        self._login_btn.setFixedHeight(44)
        self._login_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._login_btn.setStyleSheet("""
            QPushButton {
                background-color: #409EFF; color: white;
                border: none; border-radius: 10px;
                font-size: 15px; font-weight: bold;
                letter-spacing: 2px;
            }
            QPushButton:hover { background-color: #5ab0ff; }
            QPushButton:pressed { background-color: #3080dd; }
            QPushButton:disabled {
                background-color: #2a2a4a; color: #505070;
            }
        """)
        self._login_btn.clicked.connect(self._on_login_click)
        btn_layout.addWidget(self._login_btn, 2)

        content.addLayout(btn_layout)

        layout.addLayout(content)

        # 快捷键
        self._password_input.returnPressed.connect(self._on_login_click)
        self._api_key_input.returnPressed.connect(self._on_login_click)

    def _load_saved_config(self):
        if self._config.chaoxing_username:
            self._username_input.setText(self._config.chaoxing_username)
        if self._config.chaoxing_password:
            self._password_input.setText(self._config.chaoxing_password)
        if self._config.deepseek_api_key:
            self._api_key_input.setText(self._config.deepseek_api_key)
        if self._config.has_credentials:
            self._remember_cb.setChecked(True)

    def _on_login_click(self):
        username = self._username_input.text().strip()
        password = self._password_input.text().strip()
        api_key = self._api_key_input.text().strip()

        if not username or not password:
            self._show_status("请输入账号和密码", "warning")
            return

        if not api_key:
            self._show_status("请输入 DeepSeek API Key", "warning")
            return

        self._config.save_deepseek_config(api_key=api_key)

        self._set_buttons_enabled(False)
        self._show_status("正在启动浏览器...", "info")

        self._worker = LoginWorker(self._browser, username, password)
        self._worker.status_update.connect(lambda msg: self._show_status(msg, "info"))
        self._worker.login_success.connect(self._on_login_success)
        self._worker.login_failed.connect(self._on_login_failed)
        self._worker.start()

    def _on_login_success(self):
        self._show_status("✓ 登录成功", "success")

        if self._remember_cb.isChecked():
            username = self._username_input.text().strip()
            password = self._password_input.text().strip()
            self._config.save_credentials(username, password)

        self._set_buttons_enabled(True)
        self.login_completed.emit(self._browser)

    def _on_login_failed(self, message: str):
        self._show_status(message, "error")
        self._set_buttons_enabled(True)

        if "验证码" in message or "手动" in message:
            show_styled_message(
                self, "手动登录",
                "请在弹出的浏览器窗口中手动完成登录，\n完成后返回此窗口点击「确定」。",
                level="warning",
                buttons=[("取消", False), ("确定", True)],
            )
            # 用户确认后尝试重新检测
            self._show_status("正在重新检测登录状态...", "info")
            self._worker = LoginWorker(self._browser, "", "")
            self._worker.status_update.connect(lambda msg: self._show_status(msg, "info"))
            self._worker.login_success.connect(self._on_login_success)
            self._worker.login_failed.connect(self._on_login_failed)
            self._worker.start()

    def _on_cancel(self):
        """取消登录"""
        result = show_styled_message(
            self, "退出确认",
            "确定要退出吗？",
            level="question",
            buttons=[("取消", False), ("退出", True)],
        )
        if result:
            self.close()

    def _set_buttons_enabled(self, enabled: bool):
        self._login_btn.setEnabled(enabled)
        self._cancel_btn.setEnabled(enabled)

    def _show_status(self, message: str, level: str = "info"):
        colors = {
            "info": "#707090",
            "success": "#67C23A",
            "warning": "#E6A23C",
            "error": "#F56C6C",
        }
        color = colors.get(level, colors["info"])
        self._status_label.setStyleSheet(f"""
            font-size: 12px; color: {color};
            background: transparent; border: none; padding: 2px;
        """)
        self._status_label.setText(message)

    def paintEvent(self, event):
        """绘制圆角背景"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 16, 16)
        painter.fillPath(path, QColor("#12122a"))
        painter.end()

    def closeEvent(self, event):
        # 只有本地创建的浏览器才在关闭时停止
        # 共享的浏览器由主窗口管理，关闭对话框不应影响它
        if self._owns_browser and self._browser.is_started:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._browser.stop())
            loop.close()
        event.accept()
