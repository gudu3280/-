"""
悬浮球组件 - 独立悬浮球 + 小窗口交互

交互逻辑:
- 默认显示为小型圆形悬浮球（可拖拽）
- 单击悬浮球 → 唤起小型功能窗口（显示进度和日志）
- 双击悬浮球 → 快速返回系统主界面
- 小窗口可独立关闭，不影响悬浮球

设计: 基于 ui-ux-pro-max Dark Mode (OLED) 设计规范
"""

import datetime
import logging
from typing import Optional, List

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
    QApplication, QFrame, QPushButton, QSizePolicy,
)
from PyQt5.QtCore import (
    Qt, pyqtSignal, QTimer, QPoint, QPropertyAnimation,
    QEasingCurve, pyqtProperty, QRect, QSize,
)
from PyQt5.QtGui import (
    QColor, QPainter, QFont, QPen, QBrush, QConicalGradient,
    QRadialGradient, QPainterPath, QCursor, QLinearGradient, QTextCursor,
)

logger = logging.getLogger(__name__)


class FloatingBall(QWidget):
    """
    独立悬浮球 + 小窗口

    - 圆形悬浮球（48px），始终置顶，可拖拽
    - 单击 → 唤起小窗口（进度+日志）
    - 双击 → 返回主界面
    - 小窗口关闭后悬浮球仍显示
    """

    restore_requested = pyqtSignal()

    STATE_COLORS = {
        "idle": "#6366f1",
        "running": "#10b981",
        "paused": "#f59e0b",
        "error": "#f43f5e",
        "completed": "#06b6d4",
    }

    BALL_SIZE = 64

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        # 状态
        self._state = "idle"
        self._progress = 0.0
        self._status_text = "待机"
        self._log_lines: List[str] = []
        self._max_log_lines = 200

        # 拖拽
        self._dragging = False
        self._drag_offset = QPoint()
        self._click_pos = QPoint()
        self._opacity = 0.9

        # 动画
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(250)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._panel_fade_anim = None

        # 初始位置：右侧居中
        screen = QApplication.primaryScreen().geometry()
        self.move(
            screen.right() - self.BALL_SIZE - 12,
            screen.height() // 2 - self.BALL_SIZE // 2,
        )

        self.setFixedSize(self.BALL_SIZE, self.BALL_SIZE)
        self.setWindowOpacity(self._opacity)

        # 小窗口
        self._panel: Optional[FloatingPanel] = None

        # 单击/双击判定
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(200)  # 缩短双击判定等待，提升单击响应速度
        self._click_timer.timeout.connect(self._on_single_click)

        # 鼠标悬停效果
        self._hovered = False
        self.setMouseTracking(True)

    # ======================== 状态更新 ========================

    def set_state(self, state: str, status_text: str = ""):
        self._state = state
        if status_text:
            self._status_text = status_text
        self.update()
        if self._panel:
            self._panel.set_state(state, status_text)

    def set_progress(self, progress: float, status_text: str = ""):
        self._progress = max(0.0, min(1.0, progress))
        if status_text:
            self._status_text = status_text
        self.update()
        if self._panel:
            self._panel.set_progress(progress, status_text)

    def append_log(self, message: str, level: str = "info"):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        colors = {"info": "#3b82f6", "success": "#22c55e", "warning": "#f59e0b", "error": "#ef4444"}
        color = colors.get(level, "#94a3b8")
        icon = {"info": "ℹ", "success": "✓", "warning": "⚠", "error": "✗"}.get(level, "ℹ")
        line = f'<span style="color:#475569">{timestamp}</span> <span style="color:{color}">{icon} {message}</span>'
        self._log_lines.append(line)
        if len(self._log_lines) > self._max_log_lines:
            self._log_lines = self._log_lines[-self._max_log_lines:]
        if self._panel:
            self._panel.append_log(line)

    # ======================== 绘制悬浮球 ========================

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        ring_width = 5
        r = min(w, h) / 2 - ring_width - 2

        # 背景轨道（和主界面 CircularProgressRing 一致）
        track_pen = QPen(QColor("#1e293b"), ring_width)
        track_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(track_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPoint(int(cx), int(cy)), int(r), int(r))

        # 进度弧（渐变色 蓝→绿）
        if self._progress > 0.001:
            arc_angle = int(self._progress * 360 * 16)
            gradient = QConicalGradient(cx, cy, 90)
            gradient.setColorAt(0.0, QColor("#3b82f6"))
            gradient.setColorAt(1.0, QColor("#22c55e"))
            arc_pen = QPen(gradient, ring_width)
            arc_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(arc_pen)
            painter.drawArc(
                int(cx - r), int(cy - r), int(r * 2), int(r * 2),
                90 * 16, -arc_angle
            )

        # 中心百分比
        pct = int(self._progress * 100)
        if pct > 0:
            painter.setPen(QColor("#e2e8f0"))
            painter.setFont(QFont("Segoe UI", 12, QFont.Bold))
            painter.drawText(self.rect(), Qt.AlignCenter, f"{pct}%")
        else:
            # 待机状态显示“超”
            painter.setPen(QColor("#94a3b8"))
            painter.setFont(QFont("Microsoft YaHei UI", 13, QFont.Bold))
            painter.drawText(self.rect(), Qt.AlignCenter, "超")

    # ======================== 鼠标事件 ========================

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            logger.info(f"悬浮球按下: pos={event.pos()}")
            self._click_pos = event.pos()
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            self._dragging = False

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            # 移动超过 4px 才算拖拽
            if (event.pos() - self._click_pos).manhattanLength() > 4:
                self._dragging = True
                self.move(event.globalPos() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        logger.info(f"悬浮球释放: dragging={self._dragging}")
        if not self._dragging:
            self._click_timer.start()  # 等待双击判定
        self._dragging = False

    def mouseDoubleClickEvent(self, event):
        logger.info("悬浮球双击")
        self._click_timer.stop()
        self._dragging = True
        self.restore_requested.emit()

    def _on_single_click(self):
        """单击 → 唤起/切换小窗口"""
        logger.info(f"单击回调触发: panel_exists={self._panel is not None}, visible={self._panel.isVisible() if self._panel else None}")
        if self._panel and self._panel.isVisible():
            self._panel.close()
        else:
            self._show_panel()

    # ======================== 小窗口 ========================

    def _show_panel(self):
        """唤起小窗口"""
        if not self._panel:
            self._panel = FloatingPanel(self)
            self._panel.restore_requested.connect(self.restore_requested.emit)

        # 同步当前状态到面板
        self._panel.set_state(self._state, self._status_text)
        self._panel.set_progress(self._progress, self._status_text)

        # 定位：悬浮球上方
        ball_pos = self.pos()
        px = ball_pos.x() + (self.BALL_SIZE - self._panel.width()) // 2
        py = ball_pos.y() - self._panel.height() - 8
        screen = QApplication.primaryScreen().geometry()
        # 如果上方空间不够，显示在下方
        if py < 5:
            py = ball_pos.y() + self.BALL_SIZE + 8
        # 左右边界检查
        if px < 5:
            px = 5
        if px + self._panel.width() > screen.right() - 5:
            px = screen.right() - self._panel.width() - 5
        self._panel.move(px, py)

        # 清空并重新加载日志（避免重复）
        self._panel._log_view.clear()
        for line in self._log_lines[-60:]:
            self._panel.append_log(line)

        # 淡入（必须存储为实例变量，否则动画被垃圾回收，面板透明度停在0）
        self._panel.setWindowOpacity(0)
        self._panel.show()
        self._panel_fade_anim = QPropertyAnimation(self._panel, b"windowOpacity")
        self._panel_fade_anim.setDuration(200)
        self._panel_fade_anim.setStartValue(0.0)
        self._panel_fade_anim.setEndValue(0.95)
        self._panel_fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._panel_fade_anim.start()

    # ======================== 显示/隐藏 ========================

    def show_ball(self):
        # 断开 hide_ball 累积的 finished 连接
        try:
            self._fade_anim.finished.disconnect()
        except TypeError:
            pass
        self.show()
        self._fade_anim.stop()
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(self._opacity)
        self._fade_anim.start()

    def hide_ball(self):
        if self._panel:
            self._panel.close()
        self._fade_anim.stop()
        try:
            self._fade_anim.finished.disconnect()
        except TypeError:
            pass
        self._fade_anim.setStartValue(self.windowOpacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.finished.connect(self.hide)
        self._fade_anim.start()


class FloatingPanel(QFrame):
    """悬浮球的日志窗口 - 和主界面风格一致"""

    restore_requested = pyqtSignal()

    PANEL_WIDTH = 480
    PANEL_HEIGHT = 500

    STATE_COLORS = FloatingBall.STATE_COLORS

    def __init__(self, parent_ball=None):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(self.PANEL_WIDTH, self.PANEL_HEIGHT)

        self.setStyleSheet("""
            QFrame {
                background-color: rgba(26, 22, 16, 0.97);
                border: 1px solid #1e293b;
                border-radius: 12px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(6)

        # 标题栏
        header = QHBoxLayout()
        header.setSpacing(8)

        self._dot = QLabel("●")
        self._dot.setFixedWidth(14)
        self._dot.setStyleSheet("color: #64748b; font-size: 13px; background: transparent; border: none;")
        header.addWidget(self._dot)

        self._status_label = QLabel("待机")
        self._status_label.setStyleSheet(
            "color: #f8fafc; font-size: 12px; font-weight: bold; "
            "font-family: 'Microsoft YaHei UI', sans-serif; background: transparent; border: none;"
        )
        header.addWidget(self._status_label)

        self._pct_label = QLabel("")
        self._pct_label.setStyleSheet(
            "color: #22c55e; font-size: 12px; font-weight: bold; "
            "font-family: 'Segoe UI', sans-serif; background: transparent; border: none;"
        )
        header.addWidget(self._pct_label)

        header.addStretch()

        # 关闭按钮
        close_btn = QPushButton("×")
        close_btn.setFixedSize(22, 22)
        close_btn.setCursor(QCursor(Qt.PointingHandCursor))
        close_btn.setStyleSheet("""
            QPushButton { background: #1e293b; color: #94a3b8; border: none;
                           border-radius: 11px; font-size: 15px; font-weight: bold; }
            QPushButton:hover { background: #ef4444; color: white; }
        """)
        close_btn.clicked.connect(self.close)
        header.addWidget(close_btn)

        layout.addLayout(header)

        # 进度条
        from PyQt5.QtWidgets import QProgressBar
        self._bar = QProgressBar()
        self._bar.setFixedHeight(6)
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet("""
            QProgressBar { background: #1e293b; border: none; border-radius: 3px; }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #3b82f6, stop:1 #22c55e); border-radius: 3px; }
        """)
        layout.addWidget(self._bar)

        # 日志区（和主界面 LogWidget 一致）
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setLineWrapMode(QTextEdit.WidgetWidth)  # 长行自动换行，确保内容可见
        font = QFont("Consolas", 11)
        font.setStyleHint(QFont.Monospace)
        self._log_view.setFont(font)
        self._log_view.setStyleSheet("""
            QTextEdit {
                background-color: #1a1610;
                border: 1px solid #1e293b;
                border-radius: 8px;
                padding: 6px 8px;
                color: #e2e8f0;
            }
        """)
        layout.addWidget(self._log_view, 1)

        # 底部提示
        hint = QLabel("双击悬浮球返回主界面")
        hint.setStyleSheet("color: #475569; font-size: 10px; background: transparent; border: none;")
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)

    def set_state(self, state: str, status_text: str = ""):
        color = self.STATE_COLORS.get(state, "#64748b")
        self._dot.setStyleSheet(f"color: {color}; font-size: 13px; background: transparent; border: none;")
        self._status_label.setText(status_text or state)
        self._status_label.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: bold; "
            "font-family: 'Microsoft YaHei UI', sans-serif; background: transparent; border: none;"
        )

    def set_progress(self, progress: float, status_text: str = ""):
        pct = int(max(0.0, min(1.0, progress)) * 100)
        self._bar.setValue(pct)
        self._pct_label.setText(f"{pct}%" if pct > 0 else "")
        if status_text:
            self._status_label.setText(status_text)

    def append_log(self, line: str):
        self._log_view.append(line)
        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._log_view.setTextCursor(cursor)
