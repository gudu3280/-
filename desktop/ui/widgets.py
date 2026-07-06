"""
自定义控件 - 仪表盘卡片、进度环、日志显示、章节树
"""

import datetime
import math
import re
from typing import Optional

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QProgressBar, QTextEdit, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QFrame, QSizePolicy, QMenu, QAction,
    QPushButton,
)
from PyQt5.QtCore import (
    Qt, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve,
    pyqtProperty, QPoint, QPointF,
)
from PyQt5.QtGui import (
    QColor, QTextCursor, QFont, QPainter, QPen, QRadialGradient,
    QConicalGradient, QPainterPath, QLinearGradient, QPolygonF,
)


# ============================================================
# 圆形进度环组件
# ============================================================

class CircularProgressRing(QWidget):
    """
    圆形进度环 - 自定义绘制

    外环: 渐变色进度弧
    内环: 灰色背景轨道
    中心: 百分比数字 + 状态文字
    """

    def __init__(self, parent=None, size: int = 120):
        super().__init__(parent)
        self._base_size = size
        self.setFixedSize(size, size)
        self._progress = 0.0  # 0.0 ~ 1.0
        self._animated_progress = 0.0
        self._status_text = "等待"
        self._ring_width = 8
        self._color_start = QColor("#3b82f6")
        self._color_end = QColor("#22c55e")

        # 进度动画
        self._anim = QPropertyAnimation(self, b"animatedProgress")
        self._anim.setDuration(600)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def setScaledSize(self, size: int):
        """根据容器大小自适应缩放"""
        size = max(60, min(size, 120))  # 限制在 60-120px 之间
        self._base_size = size
        self._ring_width = max(4, size // 15)  # 环宽随尺寸缩放
        self.setFixedSize(size, size)
        self.update()

    def _get_animated_progress(self):
        return self._animated_progress

    def _set_animated_progress(self, v):
        self._animated_progress = v
        self.update()

    animatedProgress = pyqtProperty(float, _get_animated_progress, _set_animated_progress)

    def set_progress(self, value: float, status: str = ""):
        """设置进度 (0.0~1.0) 和状态文字"""
        self._progress = max(0.0, min(1.0, value))
        if status:
            self._status_text = status
        self._anim.stop()
        self._anim.setStartValue(self._animated_progress)
        self._anim.setEndValue(self._progress)
        self._anim.start()

    def reset(self):
        """重置"""
        self._progress = 0.0
        self._animated_progress = 0.0
        self._status_text = "等待"
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2 - self._ring_width

        # 背景轨道
        track_pen = QPen(QColor("#1e293b"), self._ring_width)
        track_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(track_pen)
        painter.drawEllipse(QPoint(int(cx), int(cy)), int(r), int(r))

        # 进度弧
        if self._animated_progress > 0.001:
            arc_angle = int(self._animated_progress * 360 * 16)  # 16ths of degree
            start_angle = 90 * 16  # 从顶部开始

            # 渐变色
            gradient = QConicalGradient(cx, cy, 90)
            gradient.setColorAt(0.0, self._color_start)
            gradient.setColorAt(1.0, self._color_end)

            arc_pen = QPen(gradient, self._ring_width)
            arc_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(arc_pen)
            painter.drawArc(
                int(cx - r), int(cy - r), int(r * 2), int(r * 2),
                start_angle, -arc_angle
            )

        # 中心: 百分比（字体随尺寸缩放）
        pct = int(self._animated_progress * 100)
        painter.setPen(QColor("#e2e8f0"))
        font_size = max(10, self._base_size // 6)
        font = QFont("Segoe UI", font_size, QFont.Bold)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, f"{pct}%")

        # 状态文字（字体随尺寸缩放）
        painter.setPen(QColor("#94a3b8"))
        font2_size = max(7, self._base_size // 13)
        font2 = QFont("Microsoft YaHei", font2_size)
        painter.setFont(font2)
        text_rect = self.rect()
        text_rect.setTop(int(cy + 18))
        painter.drawText(text_rect, Qt.AlignCenter, self._status_text)

        painter.end()


class LogWidget(QWidget):
    """
    美化日志显示控件（带级别筛选）

    不同级别用左侧彩色竖条区分:
    - info: 蓝色
    - success: 绿色
    - warning: 橙色
    - error: 红色
    """

    LOG_COLORS = {
        "info": "#3b82f6",
        "success": "#22c55e",
        "warning": "#f59e0b",
        "error": "#ef4444",
    }

    LEVEL_ICONS = {
        "info": "\u2139",
        "success": "\u2713",
        "warning": "\u26a0",
        "error": "\u2717",
    }

    LEVEL_LABELS = {"all": "全部", "info": "信息", "success": "成功", "warning": "警告", "error": "错误"}

    def __init__(self, parent=None, max_lines: int = 200):
        super().__init__(parent)
        self._max_lines = max_lines
        self._logs: list = []  # 存储 (message, level) 元组
        self._filter_level = "all"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # 筛选栏
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(3)
        filter_bar.setContentsMargins(0, 0, 0, 0)

        self._filter_buttons = {}
        for level in ["all", "info", "success", "warning", "error"]:
            btn = QPushButton(self.LEVEL_LABELS[level])
            btn.setFixedHeight(20)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            color = self.LOG_COLORS.get(level, "#94a3b8")
            btn.setStyleSheet(f"""
                QPushButton {{
                    color: {color}; font-size: 10px;
                    background: #1e293b; border: 1px solid #334155;
                    border-radius: 4px; padding: 1px 8px;
                }}
                QPushButton:checked {{
                    background: {color}22; border-color: {color};
                }}
                QPushButton:hover {{ background: {color}33; }}
            """)
            btn.clicked.connect(lambda checked, lv=level: self._set_filter(lv))
            filter_bar.addWidget(btn)
            self._filter_buttons[level] = btn
        self._filter_buttons["all"].setChecked(True)
        filter_bar.addStretch()
        layout.addLayout(filter_bar)

        # 日志文本区
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QTextEdit.NoWrap)
        font = QFont("Consolas", 11)
        font.setStyleHint(QFont.Monospace)
        self._text.setFont(font)
        self._text.setStyleSheet("""
            QTextEdit {
                background-color: #1a1610;
                border: 1px solid #1e293b;
                border-radius: 8px;
                padding: 6px 8px;
                selection-background-color: #3b82f644;
            }
        """)
        layout.addWidget(self._text)

    def _set_filter(self, level: str):
        """设置筛选级别"""
        self._filter_level = level
        for lv, btn in self._filter_buttons.items():
            btn.setChecked(lv == level)
        self._refresh_display()

    def _refresh_display(self):
        """根据当前筛选级别重新渲染日志"""
        self._text.clear()
        for message, level in self._logs:
            if self._filter_level != "all" and level != self._filter_level:
                continue
            self._append_html(message, level)

    def _append_html(self, message: str, level: str):
        """渲染单条日志"""
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        bar_color = self.LOG_COLORS.get(level, self.LOG_COLORS["info"])
        text_color = self.LOG_COLORS.get(level, "#c0c0d0")
        icon = self.LEVEL_ICONS.get(level, "")

        html = (
            f'<div style="'
            f'border-left: 2px solid {bar_color}; '
            f'padding: 1px 6px; margin: 1px 0; '
            f'background: {bar_color}08; border-radius: 2px;'
            f'">'
            f'<span style="color:#555568; font-size:11px">{timestamp}</span> '
            f'<span style="color:{bar_color}; font-size:12px">{icon}</span> '
            f'<span style="color:{text_color}; font-size:12px">{self._escape_html(message)}</span>'
            f'</div>'
        )
        self._text.append(html)

    def append_log(self, message: str, level: str = "info"):
        """添加一条日志"""
        self._logs.append((message, level))
        if len(self._logs) > self._max_lines:
            self._logs = self._logs[-self._max_lines:]

        # 如果当前筛选不匹配，不渲染
        if self._filter_level != "all" and level != self._filter_level:
            return

        self._append_html(message, level)

        # 自动滚动到底部
        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._text.setTextCursor(cursor)

    def _escape_html(self, text: str) -> str:
        """转义HTML特殊字符"""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def clear_logs(self):
        """清空日志"""
        self._logs.clear()
        self._text.clear()


class ProgressPanel(QWidget):
    """
    进度面板控件 - 仪表盘卡片式设计

    左侧: CircularProgressRing 圆形进度环
    右侧: 当前任务名 + 增强进度条 + 统计文字
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            ProgressPanel {
                background-color: #111827;
                border: 1px solid #1e293b;
                border-radius: 12px;
            }
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(16)

        # 左侧: 圆形进度环
        self._ring = CircularProgressRing(size=110)
        main_layout.addWidget(self._ring)

        # 右侧: 信息区
        right_layout = QVBoxLayout()
        right_layout.setSpacing(6)

        # 任务名称
        self._task_label = QLabel("等待开始...")
        self._task_label.setWordWrap(True)
        self._task_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #e2e8f0; "
            "background: transparent; border: none;"
        )
        right_layout.addWidget(self._task_label)

        # 增强进度条
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%p%")
        self._progress_bar.setFixedHeight(22)
        self._progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 11px;
                text-align: center;
                color: white;
                font-weight: bold;
                font-size: 11px;
            }
            QProgressBar::chunk {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #3b82f6, stop:0.5 #22c55e, stop:1 #3b82f6
                );
                border-radius: 10px;
            }
        """)
        right_layout.addWidget(self._progress_bar)

        # 底部信息行
        bottom_layout = QHBoxLayout()

        self._count_label = QLabel("0 / 0")
        self._count_label.setStyleSheet(
            "font-size: 12px; color: #94a3b8; background: transparent; border: none;"
        )
        bottom_layout.addWidget(self._count_label)

        bottom_layout.addStretch()

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            "font-size: 12px; color: #94a3b8; background: transparent; border: none;"
        )
        bottom_layout.addWidget(self._status_label)

        right_layout.addLayout(bottom_layout)
        main_layout.addLayout(right_layout, 1)

    def resizeEvent(self, event):
        """窗口尺寸变化时自适应缩放圆形进度环"""
        h = self.height()
        # 圆环尺寸 = 面板高度 - 上下边距，限制在 60-110px
        ring_size = max(60, min(h - 24, 110))
        self._ring.setScaledSize(ring_size)
        super().resizeEvent(event)

    def update_progress(
        self,
        task_name: str,
        current: int,
        total: int,
        status: str = "",
    ):
        """更新进度显示"""
        self._task_label.setText(task_name)
        if total > 0:
            percent = int(current / total * 100)
            self._progress_bar.setValue(percent)
            self._count_label.setText(f"{current} / {total}")
            self._ring.set_progress(current / total)
        else:
            self._progress_bar.setValue(0)
            self._count_label.setText("0 / 0")
            self._ring.set_progress(0.0)
        self._status_label.setText(status)

    def reset(self):
        """重置进度"""
        self._task_label.setText("等待开始...")
        self._progress_bar.setValue(0)
        self._count_label.setText("0 / 0")
        self._status_label.setText("")
        self._ring.reset()

    def set_status(self, status: str):
        """设置状态文字"""
        self._status_label.setText(status)


class ChapterTree(QTreeWidget):
    """
    章节树形选择控件

    支持:
    - 课程 -> 章节的两级树形结构
    - 复选框选择要执行的章节
    - 课程级联选择：勾选课程 → 全选子章节，取消 → 全取消
    - 半选状态：部分子章节选中时课程显示半选
    - 全选/取消全选
    - 已完成状态标记：任务成功后自动标记，支持清除
    """

    chapter_selected = pyqtSignal(list)  # 发送选中的章节URL列表
    _COMPLETED_STYLE = "color: #64748b;"  # 已完成项：柔和灰蓝（WCAG AA 对比度）
    _NORMAL_STYLE = "color: #e2e8f0;"  # 正常项：亮白色（高对比度）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._updating = False  # 防止级联更新时的递归信号

        self.setHeaderLabels(["名称", "状态"])
        self.setColumnCount(2)
        self.setIndentation(16)
        self.setAnimated(True)

        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setMinimumSectionSize(60)

        self.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.setAlternatingRowColors(False)
        self.setWordWrap(True)

        # 文字清晰度优化：字号、字重、对比度
        self.setStyleSheet("""
            QTreeWidget {
                background-color: #1f1812;
                border: 1px solid #1e293b;
                border-radius: 8px;
                font-size: 13px;
                color: #e2e8f0;
                outline: none;
            }
            QTreeWidget::item {
                padding: 4px 6px;
                min-height: 22px;
            }
            QTreeWidget::item:selected {
                background-color: #3b82f633;
                color: #ffffff;
            }
            QTreeWidget::item:hover {
                background-color: #1e293b;
            }
            QHeaderView::section {
                background-color: #2a1f16;
                color: #94a3b8;
                border: none;
                border-bottom: 1px solid #1e293b;
                padding: 4px 8px;
                font-size: 12px;
                font-weight: bold;
            }
        """)
        # 使用自定义样式绘制展开/折叠箭头（深色主题下默认箭头不可见）
        from PyQt5.QtWidgets import QProxyStyle, QStyle
        class TreeBranchStyle(QProxyStyle):
            def drawPrimitive(self, element, option, painter, widget=None):
                if element == QStyle.PE_IndicatorBranch:
                    if option.state & QStyle.State_Children:
                        painter.setRenderHint(QPainter.Antialiasing)
                        color = QColor("#94a3b8")
                        painter.setBrush(color)
                        painter.setPen(QPen(color, 1))
                        rect = option.rect
                        cx = rect.center().x()
                        cy = rect.center().y()
                        if option.state & QStyle.State_Open:
                            # 展开：向下三角
                            pts = [QPointF(cx-4, cy-2), QPointF(cx+4, cy-2), QPointF(cx, cy+3)]
                        else:
                            # 折叠：向右三角
                            pts = [QPointF(cx-2, cy-4), QPointF(cx+3, cy), QPointF(cx-2, cy+4)]
                        poly = QPolygonF(pts)
                        painter.drawPolygon(poly)
                        return
                super().drawPrimitive(element, option, painter, widget)

        self.setStyle(TreeBranchStyle())
        # 设置字体加粗提高可读性
        font = self.font()
        font.setPointSize(9)
        font.setWeight(QFont.DemiBold)
        self.setFont(font)

        # 连接复选框变化信号
        self.itemChanged.connect(self._on_item_changed)

        # 已完成状态跟踪（key集合，key=courseid:knowledgeid）
        self._completed_keys: set = set()

        # 右键菜单
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def load_courses(self, courses: list):
        """
        加载课程列表

        Args:
            courses: [{"name": "...", "url": "...", "id": "..."}, ...]
        """
        self.clear()

        for course in courses:
            course_item = QTreeWidgetItem()
            course_item.setText(0, course.get("name", "未知课程"))
            course_item.setData(0, Qt.UserRole, course)
            course_item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
            )
            course_item.setCheckState(0, Qt.Unchecked)
            course_item.setText(1, "待加载")
            self.addTopLevelItem(course_item)

    def load_chapters(self, course_index: int, chapters: list):
        """
        为指定课程加载章节列表

        Args:
            course_index: 课程索引
            chapters: [{"name": "...", "url": "...", "index": 0, "task_type": "chapter|homework|exam"}, ...]
        """
        if course_index >= self.topLevelItemCount():
            return

        course_item = self.topLevelItem(course_index)
        # 清除已有子项
        while course_item.childCount() > 0:
            course_item.takeChild(0)

        self._updating = True
        for chapter in chapters:
            chapter_item = QTreeWidgetItem()
            task_type = chapter.get("task_type", "chapter")
            type_prefix = {"homework": "[作业] ", "exam": "[考试] "}.get(task_type, "")
            chapter_item.setText(0, type_prefix + chapter.get("name", "未知章节"))
            chapter_item.setData(0, Qt.UserRole, chapter)
            chapter_item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
            )
            chapter_item.setCheckState(0, Qt.Checked)  # 自动全选
            course_item.addChild(chapter_item)

        course_item.setText(1, f"{len(chapters)} 项")
        course_item.setExpanded(True)
        # 所有子章节都默认选中，课程也应显示为选中
        course_item.setCheckState(0, Qt.Checked)
        self._updating = False

    def get_selected_chapters(self) -> list:
        """
        获取所有选中的章节信息

        Returns:
            [{"url": "...", "name": "章节名", "course": "课程名", "task_type": "chapter"}, ...]
        """
        chapters = []

        for i in range(self.topLevelItemCount()):
            course_item = self.topLevelItem(i)
            course_name = course_item.text(0)
            for j in range(course_item.childCount()):
                chapter_item = course_item.child(j)
                if chapter_item.checkState(0) == Qt.Checked:
                    data = chapter_item.data(0, Qt.UserRole)
                    if data and "url" in data:
                        chapters.append({
                            "url": data.get("url", ""),
                            "name": data.get("name", chapter_item.text(0)),
                            "course": course_name,
                            "task_type": data.get("task_type", "chapter"),
                        })

        return chapters

    def get_selected_chapter_urls(self) -> list:
        """获取所有选中的章节URL（兼容旧接口）"""
        return [ch["url"] for ch in self.get_selected_chapters() if ch.get("url")]

    def select_all(self, checked: bool = True):
        """全选/取消全选"""
        state = Qt.Checked if checked else Qt.Unchecked
        self._updating = True
        for i in range(self.topLevelItemCount()):
            course_item = self.topLevelItem(i)
            course_item.setCheckState(0, state)
            for j in range(course_item.childCount()):
                course_item.child(j).setCheckState(0, state)
        self._updating = False

    def select_course(self, course_index: int, checked: bool = True):
        """选中/取消选中某个课程下的所有章节"""
        if course_index >= self.topLevelItemCount():
            return
        state = Qt.Checked if checked else Qt.Unchecked
        course_item = self.topLevelItem(course_index)
        self._updating = True
        course_item.setCheckState(0, state)
        for j in range(course_item.childCount()):
            course_item.child(j).setCheckState(0, state)
        self._updating = False

    def _on_item_changed(self, item, column):
        """复选框变化处理：课程↔章节级联"""
        if self._updating or column != 0:
            return

        parent = item.parent()

        if parent is None:
            # 这是课程项（顶层）→ 级联到所有子章节
            self._updating = True
            state = item.checkState(0)
            for j in range(item.childCount()):
                item.child(j).setCheckState(0, state)
            self._updating = False
        else:
            # 这是章节项（子层）→ 更新父课程的复选框状态
            self._update_parent_state(parent)

    def _update_parent_state(self, course_item):
        """根据子章节的选中状态更新课程的复选框"""
        self._updating = True
        total = course_item.childCount()
        if total == 0:
            self._updating = False
            return
        checked = sum(
            1 for j in range(total)
            if course_item.child(j).checkState(0) == Qt.Checked
        )
        if checked == 0:
            course_item.setCheckState(0, Qt.Unchecked)
        elif checked == total:
            course_item.setCheckState(0, Qt.Checked)
        else:
            course_item.setCheckState(0, Qt.PartiallyChecked)
        self._updating = False

    def set_chapter_status(self, course_idx: int, chapter_idx: int, status: str):
        """设置章节状态文字"""
        if course_idx < self.topLevelItemCount():
            course_item = self.topLevelItem(course_idx)
            if chapter_idx < course_item.childCount():
                course_item.child(chapter_idx).setText(1, status)

    # ======================== 已完成状态管理 ========================

    @staticmethod
    def _chapter_key(url: str) -> str:
        """提取稳定标识：courseid:knowledgeid，不依赖 clazzid/cpi/域名"""
        if not url:
            return ""
        # 大小写不敏感匹配 courseId/courseid/COURSEID 等
        cid = re.search(r'courseid=(\d+)', url, re.IGNORECASE)
        kid = re.search(r'knowledgeid=(\d+)', url, re.IGNORECASE)
        if cid and kid:
            return f"{cid.group(1)}:{kid.group(1)}"
        # 回退：尝试从 URL 路径中提取数字ID
        cid2 = re.search(r'course[=/:](\d+)', url, re.IGNORECASE)
        kid2 = re.search(r'knowledge[=/:](\d+)', url, re.IGNORECASE)
        if cid2 and kid2:
            return f"{cid2.group(1)}:{kid2.group(1)}"
        return url  # fallback: 用完整URL

    def load_completed_urls(self, urls: list):
        """加载已完成URL集合（从持久化存储），转换为 key 存储"""
        self._completed_keys = set()
        for url in urls:
            key = self._chapter_key(url)
            if key:
                self._completed_keys.add(key)
        self._refresh_all_completed_visuals()

    def get_completed_keys(self) -> set:
        """获取当前已完成 key 集合"""
        return set(self._completed_keys)

    def get_completed_urls(self) -> set:
        """获取当前已完成 key 集合（兼容旧接口）"""
        return set(self._completed_keys)

    def mark_completed(self, urls: list):
        """标记指定URL为已完成"""
        for url in urls:
            key = self._chapter_key(url)
            if key:
                self._completed_keys.add(key)
        self._refresh_all_completed_visuals()

    def mark_single_completed(self, url: str):
        """
        精确标记单个URL为已完成（高效，只更新目标项）

        用于实时回调场景，避免全量遍历刷新。
        返回 True 表示找到并更新了树中的项。
        """
        if not url:
            return False
        key = self._chapter_key(url)
        self._completed_keys.add(key)

        found = False
        for i in range(self.topLevelItemCount()):
            course_item = self.topLevelItem(i)
            for j in range(course_item.childCount()):
                child = course_item.child(j)
                data = child.data(0, Qt.UserRole)
                if data and self._chapter_key(data.get("url", "")) == key:
                    child.setForeground(0, QColor("#64748b"))
                    current_status = child.text(1)
                    if not current_status.startswith("✓"):
                        child.setText(1, "✓ 已完成")
                    found = True
                    break
            if found:
                # 更新课程级汇总
                self._update_course_completion_summary(course_item)
                break

        if found:
            self.viewport().update()
        return found

    def _update_course_completion_summary(self, course_item):
        """更新单个课程的完成汇总文字"""
        course_total = course_item.childCount()
        if course_total == 0:
            return
        course_completed = sum(
            1 for j in range(course_total)
            if course_item.child(j).data(0, Qt.UserRole)
            and self._chapter_key(
                course_item.child(j).data(0, Qt.UserRole).get("url", "")
            ) in self._completed_keys
        )
        if course_completed == course_total:
            course_item.setForeground(0, QColor("#64748b"))
            course_item.setText(1, f"{course_total} 项 ✓ 全部完成")
        elif course_completed > 0:
            course_item.setForeground(0, QColor("#e2e8f0"))
            course_item.setText(1, f"{course_total} 项 (已完成 {course_completed})")

    def clear_completed(self, urls: list = None):
        """清除已完成状态。urls=None 时清除全部"""
        if urls is None:
            self._completed_keys.clear()
        else:
            keys_to_remove = {self._chapter_key(u) for u in urls}
            self._completed_keys -= keys_to_remove
        self._refresh_all_completed_visuals()

    def is_completed(self, url: str) -> bool:
        return self._chapter_key(url) in self._completed_keys

    def get_uncompleted_chapters(self) -> list:
        """获取未完成的已选章节（用于“跳过已完成”执行）"""
        chapters = []
        for i in range(self.topLevelItemCount()):
            course_item = self.topLevelItem(i)
            course_name = course_item.text(0)
            for j in range(course_item.childCount()):
                chapter_item = course_item.child(j)
                if chapter_item.checkState(0) != Qt.Checked:
                    continue
                data = chapter_item.data(0, Qt.UserRole)
                if not data or "url" not in data:
                    continue
                url = data.get("url", "")
                if url and self._chapter_key(url) not in self._completed_keys:
                    chapters.append({
                        "url": url,
                        "name": data.get("name", chapter_item.text(0)),
                        "course": course_name,
                        "task_type": data.get("task_type", "chapter"),
                    })
        return chapters

    def select_chapters_by_urls(self, urls: list):
        """在树中只勾选指定 URL 的章节，取消其余勾选"""
        target_keys = {self._chapter_key(u) for u in urls}
        for i in range(self.topLevelItemCount()):
            course_item = self.topLevelItem(i)
            any_checked = False
            for j in range(course_item.childCount()):
                chapter_item = course_item.child(j)
                data = chapter_item.data(0, Qt.UserRole)
                if data and self._chapter_key(data.get("url", "")) in target_keys:
                    chapter_item.setCheckState(0, Qt.Checked)
                    any_checked = True
                else:
                    chapter_item.setCheckState(0, Qt.Unchecked)
            course_item.setCheckState(0, Qt.Checked if any_checked else Qt.Unchecked)
        self.viewport().update()

    def _refresh_all_completed_visuals(self):
        """刷新所有项的已完成视觉状态"""
        for i in range(self.topLevelItemCount()):
            course_item = self.topLevelItem(i)
            course_completed = 0
            course_total = course_item.childCount()
            for j in range(course_total):
                child = course_item.child(j)
                data = child.data(0, Qt.UserRole)
                url = data.get("url", "") if data else ""
                done = self._chapter_key(url) in self._completed_keys
                if done:
                    course_completed += 1
                    child.setForeground(0, QColor("#64748b"))
                    # 保留原有状态文字，追加完成标记
                    current_status = child.text(1)
                    if not current_status.startswith("✓"):
                        child.setText(1, "✓ 已完成")
                else:
                    child.setForeground(0, QColor("#e2e8f0"))  # 重置为正常亮色
                    current_status = child.text(1)
                    if current_status == "✓ 已完成":
                        child.setText(1, "")
            # 课程级汇总
            if course_total > 0:
                if course_completed == course_total:
                    course_item.setForeground(0, QColor("#64748b"))
                    course_item.setText(1, f"{course_total} 项 ✓ 全部完成")
                elif course_completed > 0:
                    course_item.setForeground(0, QColor("#e2e8f0"))
                    course_item.setText(1, f"{course_total} 项 (已完成 {course_completed})")
                # 否则保持原有文字
        self.viewport().update()

    def _show_context_menu(self, pos):
        """右键菜单：标记/清除已完成"""
        item = self.itemAt(pos)
        if not item:
            return

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: #1e293b; color: #e2e8f0;
                border: 1px solid #334155; padding: 4px;
            }
            QMenu::item:selected { background: #3b82f644; }
        """)

        parent = item.parent()
        if parent is None:
            # 课程项
            act_mark = menu.addAction("✓ 标记课程全部已完成")
            act_clear = menu.addAction("✗ 清除课程已完成状态")
            menu.addSeparator()
        else:
            # 章节项
            act_mark = menu.addAction("✓ 标记为已完成")
            act_clear = menu.addAction("✗ 清除已完成")
            menu.addSeparator()

        act_clear_all = menu.addAction("清除所有已完成状态")

        action = menu.exec_(self.viewport().mapToGlobal(pos))

        if action == act_mark:
            self._mark_item_completed(item)
        elif action == act_clear:
            self._clear_item_completed(item)
        elif action == act_clear_all:
            self.clear_completed()

    def _mark_item_completed(self, item):
        """标记项（课程或章节）为已完成"""
        urls = []
        if item.parent() is None:
            for j in range(item.childCount()):
                data = item.child(j).data(0, Qt.UserRole)
                if data and data.get("url"):
                    urls.append(data["url"])
        else:
            data = item.data(0, Qt.UserRole)
            if data and data.get("url"):
                urls.append(data["url"])
        if urls:
            self.mark_completed(urls)

    def _clear_item_completed(self, item):
        """清除项（课程或章节）的已完成状态"""
        urls = []
        if item.parent() is None:
            for j in range(item.childCount()):
                data = item.child(j).data(0, Qt.UserRole)
                if data and data.get("url"):
                    urls.append(data["url"])
        else:
            data = item.data(0, Qt.UserRole)
            if data and data.get("url"):
                urls.append(data["url"])
        if urls:
            self.clear_completed(urls)


class DashboardHeader(QFrame):
    """顶部标题栏 - 应用名 + 连接状态"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedHeight(48)
        self.setStyleSheet("""
            DashboardHeader {
                background-color: #1a1610;
                border-bottom: 1px solid #1e293b;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 4, 16, 4)
        layout.setSpacing(12)

        # 左侧: 应用名
        self._title = QLabel("⭐ 超星助手")
        self._title.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #3b82f6; "
            "background: transparent; border: none; letter-spacing: 1px;"
        )
        layout.addWidget(self._title)

        # 连接状态
        self._conn_label = QLabel("● 未连接")
        self._conn_label.setStyleSheet(
            "font-size: 10px; color: #ef4444; background: transparent; border: none;"
        )
        layout.addWidget(self._conn_label)

        layout.addStretch()

    def set_connected(self, connected: bool):
        """更新连接状态"""
        if connected:
            self._conn_label.setText("● 已连接")
            self._conn_label.setStyleSheet(
                "font-size: 10px; color: #22c55e; background: transparent; border: none;"
            )
        else:
            self._conn_label.setText("● 未连接")
            self._conn_label.setStyleSheet(
                "font-size: 10px; color: #ef4444; background: transparent; border: none;"
            )


# 向后兼容：StatsBar 别名指向 DashboardHeader
StatsBar = DashboardHeader
