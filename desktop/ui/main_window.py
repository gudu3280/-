"""
主窗口 - 配置 → 浏览器启动 → 课程选择 → 任务执行

新流程:
1. 启动后直接显示主窗口(不弹浏览器)
2. 用户在右侧配置面板填写账号/API Key
3. 点击「启动浏览器」→ zendriver 打开 Chrome → 导航到 mooc2-ans 互动课程页
4. 浏览器就绪后，左侧课程树激活，右侧切换到任务控制面板
"""

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional, Any

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTabWidget, QFormLayout,
    QSpinBox, QDoubleSpinBox, QCheckBox, QSplitter, QFrame,
    QSizePolicy, QStackedWidget, QScrollArea, QComboBox, QLineEdit,
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QObject, QEvent
from PyQt5.QtGui import QFont, QCursor

from core.config import Config, ENV_FILE, DATA_DIR, AccountManager
from dotenv import set_key as _env_set_key
from core.browser import BrowserManager
from core.chaoxing import ChaoxingOperator
from core.task_runner import TaskRunner, TaskState
from core.completion_db import CompletionDB
from .widgets import LogWidget, ProgressPanel, ChapterTree, DashboardHeader
from .login_window import show_styled_message
from .floating_ball import FloatingBall
from core.answer_engine import refresh_config

logger = logging.getLogger(__name__)


# ============================================================
# 持久化 asyncio 事件循环工作器
# ============================================================

class AsyncWorker(QObject):
    """
    在单个后台线程中管理持久化的 asyncio 事件循环。

    zendriver 的所有异步对象 (Tab, Browser) 绑定在
    创建它们的事件循环上。如果在不同的循环中使用会出错。
    AsyncWorker 确保所有协程都在同一个循环中执行。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        """在后台线程中运行事件循环"""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run_coroutine(self, coro, callback=None, error_callback=None):
        """
        提交协程到持久化事件循环中执行。

        Args:
            coro: 要执行的协程
            callback: 成功回调 (result) -> None
            error_callback: 失败回调 (exception) -> None
        """
        async def _wrapper():
            try:
                result = await coro
                if callback:
                    callback(result)
            except Exception as e:
                if error_callback:
                    error_callback(e)
                else:
                    logger.error(f"AsyncWorker 协程异常: {e}")

        asyncio.run_coroutine_threadsafe(_wrapper(), self._loop)


# ============================================================
# 信号桥接
# ============================================================

class AppSignals(QObject):
    """线程安全的信号桥接"""
    log_signal = pyqtSignal(str, str)
    progress_signal = pyqtSignal(str, int, int, str)
    state_signal = pyqtSignal(object)
    courses_loaded = pyqtSignal(list)
    chapters_loaded = pyqtSignal(int, list)
    browser_ready = pyqtSignal()
    browser_failed = pyqtSignal(str)
    login_prompt = pyqtSignal(str)  # 登录提示（内联显示，不弹窗）
    login_detected = pyqtSignal(bool)
    chapter_done = pyqtSignal(str)  # 单个章节实时完成 (url)
    update_available = pyqtSignal(object)  # 发现新版本 (ReleaseInfo)


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):
    """
    主窗口 - 两阶段设计

    阶段1 (未连接): 右侧显示配置面板(账号/API Key) + 「启动浏览器」按钮
    阶段2 (已连接): 左侧课程树 + 右侧任务控制面板
    """

    def __init__(self):
        super().__init__()
        self._config = Config()
        self._account_mgr = AccountManager()
        self._browser = BrowserManager(self._config)
        self._operator = ChaoxingOperator(self._config)
        self._task_runner: Optional[TaskRunner] = None
        self._worker_thread = None
        self._launch_worker = None
        self._connected = False
        self._last_persist_time = 0.0  # .env 写入节流
        self._async_worker = AsyncWorker()  # 所有 zendriver 操作共享同一事件循环
        self._chapters_loaded_set = set()   # 已加载章节的课程索引集合
        self._saved_selection_urls = None     # "仅未完成"执行前的勾选状态快照
        self._completion_db = CompletionDB()  # SQLite 持久化
        self._user_selected_account = None  # 当前会话用户显式选择的账号ID

        # 信号
        self._signals = AppSignals()
        self._signals.log_signal.connect(self._on_log)
        self._signals.progress_signal.connect(self._on_progress)
        self._signals.state_signal.connect(self._on_state_change)
        self._signals.courses_loaded.connect(self._on_courses_loaded)
        self._signals.chapters_loaded.connect(self._on_chapters_loaded)
        self._signals.browser_ready.connect(self._on_browser_ready)
        self._signals.browser_failed.connect(self._on_browser_failed)
        self._signals.login_prompt.connect(self._on_login_prompt)
        self._signals.chapter_done.connect(self._on_chapter_done)
        self._signals.update_available.connect(self._on_update_available)

        self._init_ui()

        # ---- 浏览器看门狗定时器 ----
        # 每1.5秒检查浏览器进程是否还活着，如果浏览器被外部关闭则自动重置状态
        self._browser_watchdog = QTimer(self)
        self._browser_watchdog.setInterval(1500)
        self._browser_watchdog.timeout.connect(self._on_browser_watchdog)
        self._browser_watchdog.start()

        # 悬浮球（最小化时显示）
        self._floating_ball = FloatingBall()
        self._floating_ball.restore_requested.connect(self._restore_from_floating_ball)
        self._floating_ball.hide()

        # 向后兼容：迁移旧的浏览器数据为默认账号
        self._account_mgr.migrate_legacy_data()

        # 加载已完成状态（持久化）
        self._load_completion_state()

        # 刷新已保存账号列表
        self._refresh_account_list()

        # 后台检查更新
        if self._config.auto_check_update and self._config.github_repo:
            self._check_update_async()

    # ----------------------------------------------------------
    # UI 构建
    # ----------------------------------------------------------

    def _init_ui(self):
        self.setWindowTitle("超星学习通助手")
        self.setMinimumSize(860, 580)
        self.resize(1140, 740)

        central = QWidget()
        central.setStyleSheet("background-color: #1a1410;")  # 暖橙护眼底色
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ========== 顶部仪表盘 ==========
        self._stats_bar = DashboardHeader()
        main_layout.addWidget(self._stats_bar)

        # 重启浏览器按钮（隐藏在初始状态，连接后显示）
        self._relaunch_btn = QPushButton("⟳  重启")
        self._relaunch_btn.setFixedSize(80, 28)
        self._relaunch_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._relaunch_btn.setVisible(False)  # 初始隐藏，连接后才显示
        self._relaunch_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                color: #e2e8f0;
                border: 1px solid #3b82f6;
                border-radius: 6px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3b82f6;
                color: white;
                border-color: #60a5fa;
            }
            QPushButton:disabled {
                color: #475569;
                border-color: #1e293b;
                background-color: #2a1f16;
            }
        """)
        self._relaunch_btn.clicked.connect(self._on_relaunch_browser)
        self._stats_bar.layout().addWidget(self._relaunch_btn)

        # 当前账号显示标签（隐藏在初始状态，连接后显示）
        self._current_account_label = QLabel("")
        self._current_account_label.setVisible(False)
        self._current_account_label.setStyleSheet(
            "font-size: 11px; color: #3b82f6; background: transparent; "
            "border: none; padding: 0 8px;"
        )
        self._stats_bar.layout().addWidget(self._current_account_label)

        # 返回按钮（隐藏在初始状态，连接后显示）
        self._back_btn = QPushButton("← 返回")
        self._back_btn.setFixedSize(60, 28)
        self._back_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._back_btn.setVisible(False)  # 初始隐藏
        self._back_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #94a3b8;
                border: 1px solid #334155;
                border-radius: 6px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #334155;
                color: #e2e8f0;
            }
        """)
        self._back_btn.clicked.connect(self._on_back_to_setup)
        self._stats_bar.layout().addWidget(self._back_btn)

        # 退出按钮（添加到头部布局末尾）
        self._exit_btn = QPushButton("退出")
        self._exit_btn.setFixedSize(50, 28)
        self._exit_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._exit_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #94a3b8;
                border: 1px solid #334155;
                border-radius: 6px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #ef444422;
                color: #ef4444;
                border-color: #ef444444;
            }
        """)
        self._exit_btn.clicked.connect(self._on_exit)
        self._stats_bar.layout().addWidget(self._exit_btn)

        # ========== 内容区 ==========
        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setStyleSheet(
            "QSplitter::handle { background: #2a1f16; width: 1px; }"
            "QSplitter::handle:hover { background: #3b82f6; }"
        )
        main_layout.addWidget(self._splitter, 1)

        # --- 左侧: 课程面板 ---
        self._left_widget = QWidget()
        self._left_widget.setMinimumWidth(220)
        self._left_widget.setStyleSheet("background: transparent;")
        left_layout = QVBoxLayout(self._left_widget)
        left_layout.setContentsMargins(12, 10, 4, 10)
        left_layout.setSpacing(8)

        left_header = QHBoxLayout()
        left_header.setSpacing(8)
        left_label = QLabel("课程列表")
        left_label.setObjectName("sectionLabel")
        left_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #e2e8f0; "
            "background: transparent; border: none; letter-spacing: 1px;"
        )
        left_header.addWidget(left_label)
        left_header.addStretch()
        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setFixedSize(28, 28)
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._refresh_btn.setToolTip("双击刷新课程列表")
        self._refresh_btn.setStyleSheet("""
            QPushButton {
                background: #1e293b; color: #94a3b8;
                border: 1px solid #334155; border-radius: 6px;
                font-size: 14px;
            }
            QPushButton:hover { background: #334155; color: #3b82f6; border-color: #3b82f6; }
            QPushButton:disabled { color: #475569; border-color: #1e293b; }
        """)
        # 双击触发刷新（避免误触）
        self._refresh_click_count = 0
        self._refresh_timer = None
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        left_header.addWidget(self._refresh_btn)

        # 全局刷新按钮（重新加载课程列表）
        self._refresh_all_btn = QPushButton("⟳ 重载课程")
        self._refresh_all_btn.setFixedHeight(28)
        self._refresh_all_btn.setEnabled(False)
        self._refresh_all_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._refresh_all_btn.setToolTip("重新从超星加载所有课程列表\n（不会自动展开章节，需双击课程加载章节）")
        self._refresh_all_btn.setStyleSheet("""
            QPushButton {
                background: #3b82f6; color: white;
                border: none; border-radius: 6px;
                font-size: 12px; font-weight: bold;
                padding: 0 12px;
            }
            QPushButton:hover { background: #60a5fa; }
            QPushButton:pressed { background: #2563eb; }
            QPushButton:disabled { background: #1e293b; color: #475569; }
        """)
        self._refresh_all_btn.clicked.connect(self._load_courses)
        left_header.addWidget(self._refresh_all_btn)
        left_header.addStretch()
        left_layout.addLayout(left_header)

        # 占位提示(未连接时显示)
        self._left_placeholder = QLabel(
            "\u2b06 请先在右侧配置账号信息\n并点击「启动浏览器」开始使用"
        )
        self._left_placeholder.setAlignment(Qt.AlignCenter)
        self._left_placeholder.setStyleSheet(
            "color: #475569; font-size: 14px; background: transparent; "
            "border: none; line-height: 1.8;"
        )
        left_layout.addWidget(self._left_placeholder)

        # 章节树(连接后显示)
        self._chapter_tree = ChapterTree()
        self._chapter_tree.itemDoubleClicked.connect(self._on_tree_item_clicked)
        self._chapter_tree.setVisible(False)
        left_layout.addWidget(self._chapter_tree)

        # 完成进度统计条
        self._completion_label = QLabel("")
        self._completion_label.setVisible(False)
        self._completion_label.setStyleSheet(
            "font-size: 11px; color: #22c55e; background: #22c55e11; "
            "border: 1px solid #22c55e22; border-radius: 4px; "
            "padding: 2px 8px;"
        )
        left_layout.addWidget(self._completion_label)
        self._chapter_btn_widget = QWidget()
        self._chapter_btn_widget.setStyleSheet("background: transparent;")
        chapter_btn_layout = QHBoxLayout(self._chapter_btn_widget)
        chapter_btn_layout.setContentsMargins(0, 2, 4, 0)
        chapter_btn_layout.setSpacing(6)
        select_all_btn = QPushButton("\u2611 全选")
        select_all_btn.setFixedHeight(26)
        select_all_btn.setCursor(QCursor(Qt.PointingHandCursor))
        select_all_btn.setStyleSheet("""
            QPushButton {
                background: #3b82f622; color: #3b82f6;
                border: 1px solid #3b82f644; border-radius: 6px;
                font-size: 12px; padding: 2px 12px;
            }
            QPushButton:hover { background: #3b82f633; }
        """)
        select_all_btn.clicked.connect(lambda: self._chapter_tree.select_all(True))
        chapter_btn_layout.addWidget(select_all_btn)
        deselect_btn = QPushButton("\u2610 取消全选")
        deselect_btn.setFixedHeight(26)
        deselect_btn.setCursor(QCursor(Qt.PointingHandCursor))
        deselect_btn.setStyleSheet("""
            QPushButton {
                background: transparent; color: #a0a0b0;
                border: 1px solid #334155; border-radius: 6px;
                font-size: 12px; padding: 2px 12px;
            }
            QPushButton:hover { background: #334155; color: #e2e8f0; }
        """)
        deselect_btn.clicked.connect(lambda: self._chapter_tree.select_all(False))
        chapter_btn_layout.addWidget(deselect_btn)

        # 清除已完成按钮
        clear_done_btn = QPushButton("\u21bb 清除已完成")
        clear_done_btn.setFixedHeight(26)
        clear_done_btn.setCursor(QCursor(Qt.PointingHandCursor))
        clear_done_btn.setToolTip("清除所有章节的“已完成”标记，可重新执行")
        clear_done_btn.setStyleSheet("""
            QPushButton {
                background: transparent; color: #22c55e;
                border: 1px solid #22c55e44; border-radius: 6px;
                font-size: 12px; padding: 2px 12px;
            }
            QPushButton:hover { background: #22c55e22; }
        """)
        clear_done_btn.clicked.connect(self._on_clear_completed)
        chapter_btn_layout.addWidget(clear_done_btn)

        chapter_btn_layout.addStretch()
        self._chapter_btn_widget.setVisible(False)
        left_layout.addWidget(self._chapter_btn_widget)

        self._splitter.addWidget(self._left_widget)
        self._splitter.setStretchFactor(0, 35)  # 左侧 35%

        # --- 右侧: 使用 QStackedWidget 切换两个面板 ---
        self._right_stack = QStackedWidget()

        # 面板0: 设置面板 (未连接时) — 包裹在 QScrollArea 中
        self._setup_panel = self._build_setup_panel()
        setup_scroll = QScrollArea()
        setup_scroll.setWidget(self._setup_panel)
        setup_scroll.setWidgetResizable(True)
        setup_scroll.setFrameShape(QFrame.NoFrame)
        setup_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._right_stack.addWidget(setup_scroll)

        # 面板1: 任务控制面板 — 垂直三分区（参数+控制+日志）
        self._task_panel = self._build_task_panel()
        self._right_stack.addWidget(self._task_panel)

        self._splitter.addWidget(self._right_stack)
        self._splitter.setStretchFactor(1, 65)  # 右侧 65%
        self._splitter.setChildrenCollapsible(False)
        # 初始比例 35:65
        self._splitter.setSizes([350, 650])

        # 限制左侧拖拽范围在 30%-40% 之间
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

    # ----------------------------------------------------------
    # 账号管理（嵌入配置卡片）
    # ----------------------------------------------------------

    def _refresh_account_list(self):
        """刷新账号列表（嵌入在配置卡片内）"""
        # 清除现有条目
        while self._account_list_layout.count():
            item = self._account_list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        accounts = self._account_mgr.list_accounts()
        if not accounts:
            # 无账号时显示提示
            hint = QLabel("暂无已保存账号")
            hint.setAlignment(Qt.AlignCenter)
            hint.setStyleSheet(
                "font-size: 11px; color: #64748b; background: transparent; "
                "border: none; padding: 8px;"
            )
            self._account_list_layout.addWidget(hint)
            sub_hint = QLabel("点击「启动浏览器」登录后自动保存")
            sub_hint.setAlignment(Qt.AlignCenter)
            sub_hint.setStyleSheet(
                "font-size: 9px; color: #475569; background: transparent; "
                "border: none; padding-bottom: 4px;"
            )
            self._account_list_layout.addWidget(sub_hint)
            return

        for acc in accounts:
            row = self._create_account_row(acc)
            self._account_list_layout.addWidget(row)

    def _create_account_row(self, account: dict) -> QWidget:
        """创建单个账号卡片（美化版）"""
        # 外层容器
        container = QWidget()
        container.setStyleSheet("QWidget { background: transparent; border: none; }")
        outer_layout = QVBoxLayout(container)
        outer_layout.setContentsMargins(0, 2, 0, 2)
        outer_layout.setSpacing(0)

        # 主卡片
        card = QFrame()
        card.setFixedHeight(52)
        card.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #2a1f16, stop:1 #2f2418);
                border: 1px solid #3d2e1f;
                border-radius: 8px;
            }
            QFrame:hover {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #352a1f, stop:1 #3d3020);
                border-color: #3b82f644;
            }
        """)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        # 头像圆形（根据登录方式显示不同颜色）
        display = account.get("display_name", "?")
        initial = display[0].upper() if display else "?"
        login_method = account.get("login_method", "manual")
        
        # 根据登录方式设置头像颜色
        avatar_colors = {
            "qrcode": ("#10b981", "#059669"),   # 绿色 - 扫码登录
            "password": ("#3b82f6", "#2563eb"),  # 蓝色 - 密码登录
            "manual": ("#8b5cf6", "#7c3aed"),    # 紫色 - 手动登录
            "legacy": ("#f59e0b", "#d97706"),    # 橙色 - 迁移账号
        }
        color1, color2 = avatar_colors.get(login_method, ("#3b82f6", "#2563eb"))
        
        avatar = QLabel(initial)
        avatar.setFixedSize(32, 32)
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setStyleSheet(
            f"background: qlineargradient(x1:0,y1:0,x2:1,y2:1, "
            f"stop:0 {color1}33, stop:1 {color2}22); "
            f"border-radius: 16px; color: {color1}; "
            f"font-size: 14px; font-weight: bold;"
        )
        layout.addWidget(avatar)

        # 信息区域（名称 + 状态）
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        
        # 用户名
        name_lbl = QLabel(display)
        name_lbl.setStyleSheet(
            "font-size: 12px; font-weight: 600; color: #f1f5f9; "
            "background: transparent; border: none;"
        )
        info_layout.addWidget(name_lbl)

        # 状态行（登录方式 + 缓存状态）
        has_session = account.get("has_session", False)
        method_icons = {
            "qrcode": "📱",
            "password": "🔑",
            "manual": "✋",
            "legacy": "📦",
        }
        method_icon = method_icons.get(login_method, "👤")
        status_text = f"{method_icon} {'已缓存' if has_session else '未缓存'}"
        status_color = "#22c55e" if has_session else "#64748b"
        
        status_lbl = QLabel(status_text)
        status_lbl.setStyleSheet(
            f"font-size: 10px; color: {status_color}; "
            "background: transparent; border: none;"
        )
        info_layout.addWidget(status_lbl)
        layout.addLayout(info_layout, 1)

        # 操作按钮区
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)
        account_id = account["id"]

        # 选择按钮（更醒目的样式）
        select_btn = QPushButton("启动")
        select_btn.setFixedSize(52, 26)
        select_btn.setCursor(QCursor(Qt.PointingHandCursor))
        select_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 {color1}, stop:1 {color2});
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 11px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 {color2}, stop:1 {color1});
            }}
        """)
        select_btn.clicked.connect(lambda _, aid=account_id: self._on_select_account(aid))
        btn_layout.addWidget(select_btn)

        # 删除按钮
        del_btn = QPushButton("✕")
        del_btn.setFixedSize(26, 26)
        del_btn.setCursor(QCursor(Qt.PointingHandCursor))
        del_btn.setToolTip("删除此账号")
        del_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #64748b;
                border: 1px solid #334155;
                border-radius: 6px;
                font-size: 11px;
            }
            QPushButton:hover {
                background: #ef444422;
                color: #ef4444;
                border-color: #ef4444;
            }
        """)
        del_btn.clicked.connect(lambda _, aid=account_id, name=display: self._on_remove_account(aid, name))
        btn_layout.addWidget(del_btn)
        
        layout.addLayout(btn_layout)
        outer_layout.addWidget(card)

        return container

    def _on_select_account(self, account_id: str):
        """选择账号 -> 使用该账号的会话启动浏览器"""
        account = self._account_mgr.get_account(account_id)
        if not account:
            return

        # 标记用户已显式选择账号
        self._user_selected_account = account_id
        
        self._account_mgr.set_active_account(account_id)
        self._browser.set_account(account_id)

        # 切换账号时清除所有旧账号状态，确保新账号看到全新界面
        self._operator.clear_caches()
        self._chapters_loaded_set.clear()
        self._chapter_tree.clear()            # 清空课程/章节树
        self._progress_panel.reset()          # 重置任务进度面板
        self._completion_db.switch_account(account_id)  # 切换到新账号的完成状态库
        self._update_completion_label()       # 刷新完成进度统计

        self._launch_status.setText(f"正在使用 {account['display_name']} 启动...")
        self._launch_status.setStyleSheet(
            "font-size: 12px; color: #3b82f6; background: transparent; border: none; padding: 4px;"
        )
        self._launch_btn.setEnabled(False)

        # 检查 API Key
        api_key = self._config.deepseek_api_key
        if not api_key:
            show_styled_message(
                self, "提示", "请先配置 DeepSeek API Key", "warning"
            )
            self._launch_btn.setEnabled(True)
            return

        async def _launch():
            self._signals.log_signal.emit(f"正在使用 {account['display_name']} 的会话启动...", "info")
            try:
                if not self._browser.is_started:
                    # 启动浏览器加超时保护
                    try:
                        await asyncio.wait_for(self._browser.start(headless=False), timeout=30)
                    except asyncio.TimeoutError:
                        self._signals.browser_failed.emit("浏览器启动超时")
                        return
                page = self._browser.tab
                if not page:
                    self._signals.browser_failed.emit("浏览器启动后未获取到页面")
                    return
                await self._browser.close_extra_pages()

                self._signals.log_signal.emit("正在检查登录状态...", "info")
                # 登录状态检查加超时保护（防止浏览器关闭后挂起）
                try:
                    logged_in = await asyncio.wait_for(self._browser.is_logged_in(), timeout=15)
                except asyncio.TimeoutError:
                    logger.info("登录状态检查超时，可能浏览器已关闭")
                    self._signals.login_prompt.emit("浏览器已关闭或响应超时，请重新启动")
                    return

                if logged_in:
                    self._signals.log_signal.emit("已恢复登录状态", "success")
                    if self._browser.account_id:
                        self._account_mgr.mark_session(self._browser.account_id, True)
                        self._account_mgr.update_last_login(self._browser.account_id)
                    # 登录成功后立即备份 cookies，确保下次启动能复用
                    try:
                        await self._browser._save_cookies_backup()
                    except Exception:
                        pass
                    self._signals.browser_ready.emit()
                    return

                # 登录状态失效，引导用户重新登录
                self._signals.log_signal.emit("登录状态已失效，请在浏览器中重新登录", "warning")
                if self._browser.account_id:
                    self._account_mgr.mark_session(self._browser.account_id, False)

                # 导航到登录页
                try:
                    await asyncio.wait_for(self._browser.navigate("https://passport2.chaoxing.com/login"), timeout=15)
                except asyncio.TimeoutError:
                    self._signals.login_prompt.emit("浏览器响应超时，请重新启动")
                    return
                except Exception:
                    try:
                        await self._browser.navigate_to_chaoxing()
                    except Exception:
                        pass
                self._signals.log_signal.emit("请在浏览器窗口中完成登录", "info")

                # 设置导航回调：页面跳转时立即检测登录状态
                navigation_detected = asyncio.Event()
                
                async def on_navigation(url: str):
                    """页面导航回调 - 触发立即检测"""
                    if "chaoxing.com" in url and "passport" not in url:
                        logger.info(f"导航回调触发: {url[:60]}")
                        navigation_detected.set()
                
                self._browser.set_on_navigation_callback(on_navigation)

                # 轮询等待用户登录（1.5秒间隔 + 导航事件立即触发）
                max_wait = 300
                check_interval = 1.5  # 缩短为1.5秒，更快响应
                waited = 0
                login_detected = False
                
                while waited < max_wait and not login_detected:
                    # 等待导航事件或超时
                    try:
                        await asyncio.wait_for(navigation_detected.wait(), timeout=check_interval)
                        # 导航事件触发，立即检测
                        logger.info("导航事件触发，立即检测登录状态")
                        await asyncio.sleep(0.5)  # 等待页面加载
                        navigation_detected.clear()
                    except asyncio.TimeoutError:
                        # 定时器超时，正常轮询
                        waited += check_interval
                    except Exception:
                        # 其他异常（如浏览器关闭）
                        break
                    
                    # 轻量检查：不导航，避免打断用户操作
                    try:
                        logged_in = await asyncio.wait_for(
                            self._browser.is_logged_in(skip_navigate=True), timeout=5
                        )
                        if logged_in:
                            login_detected = True
                            self._signals.log_signal.emit("检测到登录成功！", "success")
                            if self._browser.account_id:
                                self._account_mgr.mark_session(self._browser.account_id, True)
                                self._account_mgr.update_last_login(self._browser.account_id)
                            username = await self._browser.get_logged_in_username()
                            if username:
                                self._signals.log_signal.emit(f"已登录用户: {username}", "success")
                                self._signals.state_signal.emit({"action": "save_account", "username": username})
                            # 登录成功后立即备份 cookies
                            try:
                                await self._browser._save_cookies_backup()
                            except Exception:
                                pass
                            # 清除导航回调
                            self._browser.set_on_navigation_callback(None)
                            self._signals.browser_ready.emit()
                            return
                    except asyncio.TimeoutError:
                        # 超时可能是瞬态问题，继续轮询而不退出
                        logger.debug("轻量检查超时，继续等待...")
                        continue
                    except Exception:
                        # 瞬态错误，跳过本次检查继续等待
                        logger.debug("轻量检查异常，继续等待...")
                        continue

                    # 每15秒完整检查一次
                    if waited % 15 == 0 and waited > 0:
                        try:
                            logged_in = await asyncio.wait_for(
                                self._browser.is_logged_in(skip_navigate=False), timeout=15
                            )
                            if logged_in:
                                login_detected = True
                                self._signals.log_signal.emit("检测到登录成功！", "success")
                                if self._browser.account_id:
                                    self._account_mgr.mark_session(self._browser.account_id, True)
                                    self._account_mgr.update_last_login(self._browser.account_id)
                                username = await self._browser.get_logged_in_username()
                                if username:
                                    self._signals.state_signal.emit({"action": "save_account", "username": username})
                                try:
                                    await self._browser._save_cookies_backup()
                                except Exception:
                                    pass
                                self._browser.set_on_navigation_callback(None)
                                self._signals.browser_ready.emit()
                                return
                        except asyncio.TimeoutError:
                            # 完整检查超时，可能是浏览器无响应
                            logger.warning("完整检查超时，浏览器可能无响应")
                            break
                        except Exception:
                            # 瞬态错误，继续等待
                            logger.debug("完整检查异常，继续等待...")
                            pass
                    elif waited % 10 == 0:
                        self._signals.log_signal.emit(f"等待登录中... ({waited}s)", "info")

                # 退出轮询（超时或浏览器关闭）- 检查浏览器状态再决定如何处理
                self._browser.set_on_navigation_callback(None)
                if not self._browser.is_started:
                    # 浏览器已停止，重置状态
                    self._browser.force_reset()
                    self._signals.log_signal.emit("浏览器已关闭", "warning")
                    self._signals.login_prompt.emit("请重新启动浏览器")
                else:
                    # 浏览器还在运行，不强制重置
                    self._signals.log_signal.emit("等待登录超时，请在浏览器中完成登录后重试", "warning")
                    self._signals.login_prompt.emit("等待登录超时，请重新点击启动")
                    
            except Exception as e:
                logger.error(f"账号启动异常: {e}", exc_info=True)
                self._browser.set_on_navigation_callback(None)
                self._browser.force_reset()  # 重置浏览器内部状态
                self._signals.log_signal.emit("浏览器已关闭或发生异常", "warning")
                self._signals.login_prompt.emit("请重新启动浏览器")

        def _on_err(e):
            logger.error(f"账号启动协程异常: {e}", exc_info=True)
            self._browser.set_on_navigation_callback(None)
            self._browser.force_reset()  # 同步重置浏览器内部状态
            self._signals.login_prompt.emit("请重新启动浏览器")

        self._async_worker.run_coroutine(_launch(), error_callback=_on_err)

    def _on_remove_account(self, account_id: str, display_name: str):
        """删除账号"""
        result = show_styled_message(
            self, "删除账号",
            f"确定要删除账号 \"{display_name}\" 吗？\n这将清除该账号的登录缓存。",
            level="question",
            buttons=[("取消", False), ("删除", True)],
        )
        if result:
            self._account_mgr.remove_account(account_id)
            self._refresh_account_list()

    def _build_setup_panel(self) -> QWidget:
        """构建设置面板(阶段1) - 一站式启动页，内嵌凭据输入"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)

        layout.addStretch(1)

        # 欢迎标题
        welcome_title = QLabel("欢迎使用超星助手")
        welcome_title.setAlignment(Qt.AlignCenter)
        welcome_title.setStyleSheet(
            "font-size: 24px; font-weight: bold; color: #3b82f6; "
            "background: transparent; border: none; letter-spacing: 2px;"
        )
        layout.addWidget(welcome_title)

        welcome_sub = QLabel("自动化学习工具 — 填写账号信息后一键启动")
        welcome_sub.setAlignment(Qt.AlignCenter)
        welcome_sub.setStyleSheet(
            "font-size: 13px; color: #94a3b8; background: transparent; "
            "border: none; padding-bottom: 4px;"
        )
        layout.addWidget(welcome_sub)

        # 分隔线
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 transparent, stop:0.2 #3b82f644, stop:0.5 #3b82f6, "
            "stop:0.8 #3b82f644, stop:1 transparent); border: none;"
        )
        layout.addWidget(divider)

        # === 快捷凭据输入卡片 ===
        cred_card = QFrame()
        cred_card.setStyleSheet("""
            QFrame {
                background-color: #1f1812;
                border: 1px solid #1e293b;
                border-radius: 12px;
            }
        """)
        cred_layout = QVBoxLayout(cred_card)
        cred_layout.setContentsMargins(18, 12, 18, 14)
        cred_layout.setSpacing(10)

        cred_title = QLabel("  账号配置")
        cred_title.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #64748b; "
            "background: transparent; border: none; letter-spacing: 1px;"
        )
        cred_layout.addWidget(cred_title)

        # === 已保存账号列表（嵌入在配置卡片内） ===
        self._account_section = QWidget()
        self._account_section.setStyleSheet(
            "QWidget { background: transparent; border: none; }"
        )
        acct_section_layout = QVBoxLayout(self._account_section)
        acct_section_layout.setContentsMargins(0, 0, 0, 0)
        acct_section_layout.setSpacing(4)

        acct_header = QHBoxLayout()
        acct_header_lbl = QLabel("已保存账号")
        acct_header_lbl.setStyleSheet(
            "font-size: 10px; color: #94a3b8; background: transparent; border: none;"
        )
        acct_header.addWidget(acct_header_lbl)
        acct_header.addStretch()

        refresh_acct_btn = QPushButton("↺ 刷新")
        refresh_acct_btn.setFixedSize(48, 18)
        refresh_acct_btn.setCursor(QCursor(Qt.PointingHandCursor))
        refresh_acct_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #3b82f6; border: none; "
            "font-size: 9px; } QPushButton:hover { color: #60a5fa; }"
        )
        refresh_acct_btn.clicked.connect(self._refresh_account_list)
        acct_header.addWidget(refresh_acct_btn)
        acct_section_layout.addLayout(acct_header)

        self._account_list_layout = QVBoxLayout()
        self._account_list_layout.setSpacing(3)
        acct_section_layout.addLayout(self._account_list_layout)

        cred_layout.addWidget(self._account_section)

        # 分隔线
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #1e293b; border: none;")
        cred_layout.addWidget(sep)

        input_style = (
            "QLineEdit { background: #2a1f16; border: 1px solid #1e293b; "
            "border-radius: 8px; padding: 7px 12px; color: #e8e8e8; font-size: 12px; }"
            "QLineEdit:focus { border-color: #3b82f6; background: #111827; }"
            "QLineEdit::placeholder { color: #475569; }"
        )

        # 第一行：API Key（独占一行）
        self._quick_api_key = QLineEdit()
        self._quick_api_key.setPlaceholderText("DeepSeek API Key (sk-...)")
        self._quick_api_key.setStyleSheet(input_style)
        self._quick_api_key.setFixedHeight(34)
        cred_layout.addWidget(self._quick_api_key)

        # 第二行：题库地址 + 密钥（并排，可折叠）
        tiku_row = QHBoxLayout()
        tiku_row.setSpacing(8)
        self._quick_tiku_url = QLineEdit()
        self._quick_tiku_url.setPlaceholderText("题库地址 (可选)")
        self._quick_tiku_url.setStyleSheet(input_style)
        self._quick_tiku_url.setFixedHeight(34)
        tiku_row.addWidget(self._quick_tiku_url)

        self._quick_tiku_key = QLineEdit()
        self._quick_tiku_key.setPlaceholderText("题库密钥")
        self._quick_tiku_key.setStyleSheet(input_style)
        self._quick_tiku_key.setFixedHeight(34)
        tiku_row.addWidget(self._quick_tiku_key)
        cred_layout.addLayout(tiku_row)

        layout.addWidget(cred_card)

        # 配置状态提示
        self._config_status = QLabel("")
        self._config_status.setAlignment(Qt.AlignCenter)
        self._config_status.setWordWrap(True)
        self._config_status.setStyleSheet(
            "font-size: 12px; color: #64748b; background: transparent; "
            "border: none; padding: 2px;"
        )
        layout.addWidget(self._config_status)
        self._update_config_status()

        # 启动浏览器按钮
        self._launch_btn = QPushButton("🚀  启动浏览器")
        self._launch_btn.setFixedHeight(44)
        self._launch_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._launch_btn.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6; color: white;
                border: none; border-radius: 10px;
                font-size: 14px; font-weight: bold;
                letter-spacing: 1px;
            }
            QPushButton:hover { background-color: #60a5fa; }
            QPushButton:pressed { background-color: #2563eb; }
            QPushButton:disabled { background-color: #1e293b; color: #475569; }
        """)
        self._launch_btn.clicked.connect(self._on_launch_browser)
        layout.addWidget(self._launch_btn)

        # 启动状态
        self._launch_status = QLabel("")
        self._launch_status.setAlignment(Qt.AlignCenter)
        self._launch_status.setWordWrap(True)
        self._launch_status.setStyleSheet(
            "font-size: 12px; color: #64748b; background: transparent; border: none; padding: 4px;"
        )
        layout.addWidget(self._launch_status)

        # 版本号 + 检查更新
        version_row = QHBoxLayout()
        version_row.addStretch()
        from core.updater import get_local_version
        self._version_label = QLabel(f"v{get_local_version()}")
        self._version_label.setStyleSheet(
            "font-size: 11px; color: #475569; background: transparent; border: none;"
        )
        version_row.addWidget(self._version_label)

        self._check_update_btn = QPushButton("检查更新")
        self._check_update_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._check_update_btn.setStyleSheet(
            "QPushButton { color: #3b82f6; font-size: 11px; background: transparent; "
            "border: none; padding: 2px 6px; }"
            "QPushButton:hover { color: #60a5fa; text-decoration: underline; }"
        )
        self._check_update_btn.clicked.connect(self._check_update_async)
        version_row.addWidget(self._check_update_btn)
        version_row.addStretch()
        layout.addLayout(version_row)

        layout.addStretch(1)

        # 快捷键: 在 API Key 输入框按回车直接启动
        self._quick_api_key.returnPressed.connect(self._on_launch_browser)
        self._quick_tiku_key.returnPressed.connect(self._on_launch_browser)

        # 加载已保存的配置
        self._load_quick_credentials()

        return panel

    def _build_task_panel(self) -> QWidget:
        """构建任务控制面板 - 紧凑布局，自适应宽度"""
        panel = QWidget()
        panel.setMinimumWidth(240)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 12, 8)
        layout.setSpacing(6)

        card_style = (
            "QFrame { background-color: #1f1812; border: 1px solid #1e293b; "
            "border-radius: 10px; }"
        )
        cb_style = (
            "QCheckBox { color: #c0c0d0; font-size: 12px; background: transparent; border: none; spacing: 4px; }"
            "QCheckBox::indicator { width: 7px; height: 7px; border-radius: 3px; "
            "border: 1px solid #475569; background: #2a1f16; }"
            "QCheckBox::indicator:checked { background: #3b82f6; border-color: #3b82f6; }"
        )
        lbl_style = "font-size: 11px; color: #94a3b8; background: transparent; border: none;"

        # ========== 卡片1: 学习设置 ==========
        settings_card = QFrame()
        settings_card.setStyleSheet(card_style)
        sl = QVBoxLayout(settings_card)
        sl.setContentsMargins(12, 8, 12, 8)
        sl.setSpacing(5)

        # 第一行：间隔 + 倍速（填满整行）
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        l1 = QLabel("间隔")
        l1.setStyleSheet(lbl_style)
        row1.addWidget(l1)
        self._interval_min = QSpinBox()
        self._interval_min.setRange(1, 120)
        self._interval_min.setValue(self._config.answer_interval_min)
        self._interval_min.setSuffix("s")
        self._interval_min.setMinimumWidth(45)
        row1.addWidget(self._interval_min)
        tilde = QLabel("~")
        tilde.setStyleSheet(lbl_style)
        row1.addWidget(tilde)
        self._interval_max = QSpinBox()
        self._interval_max.setRange(1, 300)
        self._interval_max.setValue(self._config.answer_interval_max)
        self._interval_max.setSuffix("s")
        self._interval_max.setMinimumWidth(45)
        row1.addWidget(self._interval_max)
        l2 = QLabel("倍速")
        l2.setStyleSheet(lbl_style)
        row1.addWidget(l2)
        self._video_speed_combo = QComboBox()
        for label, value in [("1x", 1), ("2x", 2), ("4x", 4), ("8x", 8), ("16x", 16)]:
            self._video_speed_combo.addItem(label, value)
        for i in range(self._video_speed_combo.count()):
            if self._video_speed_combo.itemData(i) == self._config.video_speed:
                self._video_speed_combo.setCurrentIndex(i)
                break
        self._video_speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        row1.addWidget(self._video_speed_combo)
        l_acc = QLabel("正确率")
        l_acc.setStyleSheet(lbl_style)
        row1.addWidget(l_acc)
        self._accuracy_spin = QDoubleSpinBox()
        self._accuracy_spin.setRange(0.0, 1.0)
        self._accuracy_spin.setSingleStep(0.1)
        self._accuracy_spin.setValue(self._config.min_accuracy)
        self._accuracy_spin.setMinimumWidth(40)
        row1.addWidget(self._accuracy_spin)
        sl.addLayout(row1)

        # 第二行：音量 + 提交策略 + 线程（填满整行）
        row2 = QHBoxLayout()
        row2.setSpacing(4)
        l3 = QLabel("音量")
        l3.setStyleSheet(lbl_style)
        row2.addWidget(l3)
        from PyQt5.QtWidgets import QSlider
        self._volume_slider = QSlider(Qt.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(self._config.video_volume)
        self._volume_slider.setStyleSheet(
            "QSlider { background: transparent; border: none; }"
            "QSlider::groove:horizontal { height: 3px; background: #1e293b; border-radius: 2px; }"
            "QSlider::handle:horizontal { width: 8px; height: 8px; background: #3b82f6; "
            "border-radius: 4px; margin: -3px 0; }"
            "QSlider::sub-page:horizontal { background: #3b82f6; border-radius: 2px; }"
        )
        row2.addWidget(self._volume_slider, 1)
        self._volume_label = QLabel(f"{self._config.video_volume}%")
        self._volume_label.setMinimumWidth(28)
        self._volume_label.setStyleSheet("font-size: 10px; color: #64748b; background: transparent; border: none;")
        row2.addWidget(self._volume_label)
        l_submit = QLabel("提交")
        l_submit.setStyleSheet(lbl_style)
        row2.addWidget(l_submit)
        self._upload_combo = QComboBox()
        self._upload_combo.addItem("仅保存", "save")
        self._upload_combo.addItem("不保存", "nomove")
        self._upload_combo.addItem("强制提交", "force")
        self._upload_combo.addItem("80%提交", 80)
        self._upload_combo.addItem("全对提交", 100)
        for i in range(self._upload_combo.count()):
            if self._upload_combo.itemData(i) == self._config.upload_type:
                self._upload_combo.setCurrentIndex(i)
                break
        row2.addWidget(self._upload_combo, 1)
        l_threads = QLabel("线程")
        l_threads.setStyleSheet(lbl_style)
        row2.addWidget(l_threads)
        self._threads_spin = QSpinBox()
        self._threads_spin.setRange(1, 5)
        self._threads_spin.setValue(self._config.worker_threads)
        self._threads_spin.setMinimumWidth(35)
        row2.addWidget(self._threads_spin)
        sl.addLayout(row2)

        # 第三行：自动提交 + 自动视频 + 自动跳转
        row3 = QHBoxLayout()
        row3.setSpacing(8)
        self._auto_submit_cb = QCheckBox("自动提交")
        self._auto_submit_cb.setChecked(self._config.auto_submit)
        self._auto_submit_cb.setStyleSheet(cb_style)
        row3.addWidget(self._auto_submit_cb)
        self._auto_video_cb = QCheckBox("自动视频")
        self._auto_video_cb.setChecked(self._config.auto_video)
        self._auto_video_cb.setStyleSheet(cb_style)
        row3.addWidget(self._auto_video_cb)
        self._auto_jump_cb = QCheckBox("自动跳转")
        self._auto_jump_cb.setChecked(self._config.auto_jump)
        self._auto_jump_cb.setStyleSheet(cb_style)
        row3.addWidget(self._auto_jump_cb)
        row3.addStretch()
        sl.addLayout(row3)

        # 第四行：自动作业 + 自动考试 + 复习 + 强制
        row4 = QHBoxLayout()
        row4.setSpacing(8)
        self._auto_homework_cb = QCheckBox("自动作业")
        self._auto_homework_cb.setChecked(self._config.auto_homework)
        self._auto_homework_cb.setStyleSheet(cb_style)
        row4.addWidget(self._auto_homework_cb)
        self._auto_exam_cb = QCheckBox("自动考试")
        self._auto_exam_cb.setChecked(self._config.auto_exam)
        self._auto_exam_cb.setToolTip("⚠️ 仅供技术学习")
        self._auto_exam_cb.setStyleSheet(
            "QCheckBox { color: #c0c0d0; font-size: 12px; background: transparent; border: none; spacing: 5px; }"
            "QCheckBox::indicator { width: 7px; height: 7px; border-radius: 3px; "
            "border: 1px solid #475569; background: #2a1f16; }"
            "QCheckBox::indicator:checked { background: #f59e0b; border-color: #f59e0b; }"
        )
        row4.addWidget(self._auto_exam_cb)
        self._review_mode_cb = QCheckBox("复习")
        self._review_mode_cb.setChecked(self._config.review_mode)
        self._review_mode_cb.setToolTip("已完成视频继续学习")
        self._review_mode_cb.setStyleSheet(
            "QCheckBox { color: #64748b; font-size: 11px; background: transparent; border: none; spacing: 5px; }"
            "QCheckBox::indicator { width: 7px; height: 7px; border-radius: 3px; "
            "border: 1px solid #334155; background: #2a1f16; }"
            "QCheckBox::indicator:checked { background: #8b5cf6; border-color: #8b5cf6; }"
        )
        row4.addWidget(self._review_mode_cb)
        self._force_study_cb = QCheckBox("强制")
        self._force_study_cb.setChecked(self._config.force_study)
        self._force_study_cb.setToolTip("非任务点视频也学习")
        self._force_study_cb.setStyleSheet(
            "QCheckBox { color: #64748b; font-size: 11px; background: transparent; border: none; spacing: 5px; }"
            "QCheckBox::indicator { width: 7px; height: 7px; border-radius: 3px; "
            "border: 1px solid #334155; background: #2a1f16; }"
            "QCheckBox::indicator:checked { background: #f59e0b; border-color: #f59e0b; }"
        )
        row4.addWidget(self._force_study_cb)
        row4.addStretch()
        sl.addLayout(row4)

        # 第五行：随机作答 + 缓存 + 超时 + 清除（填满整行）
        row5 = QHBoxLayout()
        row5.setSpacing(4)
        self._random_answer_cb = QCheckBox("随机")
        self._random_answer_cb.setChecked(self._config.random_answer)
        self._random_answer_cb.setStyleSheet(cb_style)
        row5.addWidget(self._random_answer_cb)
        self._cache_cb = QCheckBox("缓存")
        self._cache_cb.setChecked(self._config.cache_enabled)
        self._cache_cb.setStyleSheet(
            "QCheckBox { color: #c0c0d0; font-size: 12px; background: transparent; border: none; spacing: 5px; }"
            "QCheckBox::indicator { width: 7px; height: 7px; border-radius: 3px; "
            "border: 1px solid #475569; background: #2a1f16; }"
            "QCheckBox::indicator:checked { background: #22c55e; border-color: #22c55e; }"
        )
        row5.addWidget(self._cache_cb)
        l_timeout = QLabel("超时")
        l_timeout.setStyleSheet(lbl_style)
        row5.addWidget(l_timeout)
        self._search_timeout_spin = QSpinBox()
        self._search_timeout_spin.setRange(10, 180)
        self._search_timeout_spin.setValue(self._config.search_timeout)
        self._search_timeout_spin.setSuffix("s")
        self._search_timeout_spin.setMinimumWidth(50)
        row5.addWidget(self._search_timeout_spin)
        row5.addStretch()
        clear_cache_btn = QPushButton("清除")
        clear_cache_btn.setFixedHeight(22)
        clear_cache_btn.setCursor(QCursor(Qt.PointingHandCursor))
        clear_cache_btn.setStyleSheet(
            "QPushButton { color: #94a3b8; font-size: 10px; background: #1e293b; "
            "border: 1px solid #334155; border-radius: 4px; padding: 1px 8px; }"
            "QPushButton:hover { background: #ef4444; color: white; border-color: #ef4444; }"
        )
        clear_cache_btn.clicked.connect(self._on_clear_cache)
        row5.addWidget(clear_cache_btn)
        sl.addLayout(row5)

        layout.addWidget(settings_card)

        # ========== 参数+控制区 vs 日志区，可拖拽缩放 ==========
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)

        # 参数面板 + 控制按钮 + 进度（合并为一个区域）
        settings_card.setParent(None)
        param_inner = QWidget()
        param_inner_layout = QVBoxLayout(param_inner)
        param_inner_layout.setContentsMargins(8, 8, 12, 4)
        param_inner_layout.setSpacing(6)
        param_inner_layout.addWidget(settings_card)
        top_layout.addWidget(param_inner)

        # 连接所有控件信号
        self._interval_min.valueChanged.connect(self._apply_config)
        self._interval_max.valueChanged.connect(self._apply_config)
        self._auto_submit_cb.stateChanged.connect(self._apply_config)
        self._auto_video_cb.stateChanged.connect(self._apply_config)
        self._video_speed_combo.currentIndexChanged.connect(self._apply_config)
        self._auto_jump_cb.stateChanged.connect(self._apply_config)
        self._auto_homework_cb.stateChanged.connect(self._apply_config)
        self._auto_exam_cb.stateChanged.connect(self._apply_config)
        self._accuracy_spin.valueChanged.connect(self._apply_config)
        self._upload_combo.currentIndexChanged.connect(self._apply_config)
        self._threads_spin.valueChanged.connect(self._apply_config)
        self._random_answer_cb.stateChanged.connect(self._apply_config)
        self._cache_cb.stateChanged.connect(self._apply_config)
        self._search_timeout_spin.valueChanged.connect(self._apply_config)
        self._volume_slider.valueChanged.connect(self._apply_config)
        self._review_mode_cb.stateChanged.connect(self._apply_config)
        self._force_study_cb.stateChanged.connect(self._apply_config)

        # 控制按钮 — 两行布局：主按钮 + 次要按钮
        control_layout = QVBoxLayout()
        control_layout.setSpacing(4)

        # 第一行：开始执行（主按钮，独占一行）
        row1_layout = QHBoxLayout()
        row1_layout.setSpacing(4)

        self._start_btn = QPushButton("▶ 开始执行")
        self._start_btn.setMinimumHeight(36)
        self._start_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._start_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._start_btn.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6; color: white;
                border: none; border-radius: 8px;
                font-size: 13px; font-weight: bold;
            }
            QPushButton:hover { background-color: #60a5fa; }
            QPushButton:pressed { background-color: #2563eb; }
            QPushButton:disabled { background-color: #1e293b; color: #475569; }
        """)
        self._start_btn.clicked.connect(self._on_start)
        row1_layout.addWidget(self._start_btn)

        self._skip_btn = QPushButton("⏭ 仅未完成")
        self._skip_btn.setMinimumHeight(36)
        self._skip_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._skip_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._skip_btn.setToolTip("自动选中未完成的章节并开始执行")
        self._skip_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b; color: #94a3b8;
                border: 1px solid #334155; border-radius: 8px;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #334155; color: #e2e8f0; border-color: #3b82f6; }
            QPushButton:disabled { background-color: #2a1f16; color: #334155; }
        """)
        self._skip_btn.clicked.connect(self._on_skip_and_start)
        row1_layout.addWidget(self._skip_btn)
        control_layout.addLayout(row1_layout)

        # 第二行：从头开始 | 暂停 | 停止
        row2_layout = QHBoxLayout()
        row2_layout.setSpacing(4)

        self._restart_btn = QPushButton("↻ 从头开始")
        self._restart_btn.setMinimumHeight(32)
        self._restart_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._restart_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._restart_btn.setToolTip("直接执行所有已选章节，不跳过已完成")
        self._restart_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b; color: #f59e0b;
                border: 1px solid #f59e0b44; border-radius: 8px;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #f59e0b22; color: #fbbf24; border-color: #f59e0b; }
            QPushButton:disabled { background-color: #2a1f16; color: #334155; }
        """)
        self._restart_btn.clicked.connect(self._on_restart)
        row2_layout.addWidget(self._restart_btn)

        self._pause_btn = QPushButton("⏸ 暂停")
        self._pause_btn.setMinimumHeight(32)
        self._pause_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._pause_btn.setEnabled(False)
        self._pause_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._pause_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b; color: #eab308;
                border: 1px solid #eab30844; border-radius: 8px;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #eab30822; color: #facc15; border-color: #eab308; }
            QPushButton:disabled { background-color: #2a1f16; color: #334155; }
        """)
        self._pause_btn.clicked.connect(self._on_pause_resume)
        row2_layout.addWidget(self._pause_btn)

        self._stop_btn = QPushButton("⏹ 停止")
        self._stop_btn.setMinimumHeight(32)
        self._stop_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._stop_btn.setEnabled(False)
        self._stop_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._stop_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b; color: #ef4444;
                border: 1px solid #ef444444; border-radius: 8px;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #ef444422; color: #f87171; border-color: #ef4444; }
            QPushButton:disabled { background-color: #2a1f16; color: #334155; }
        """)
        self._stop_btn.clicked.connect(self._on_stop)
        row2_layout.addWidget(self._stop_btn)
        control_layout.addLayout(row2_layout)

        # 控制按钮区（直接添加到 top_layout，不再用独立的 ctrl_widget）
        ctrl_container = QWidget()
        ctrl_container.setLayout(control_layout)
        ctrl_container.layout().setContentsMargins(8, 2, 12, 4)
        top_layout.addWidget(ctrl_container)

        # 进度面板
        self._progress_panel = ProgressPanel()
        progress_container = QWidget()
        progress_layout = QHBoxLayout(progress_container)
        progress_layout.setContentsMargins(8, 0, 12, 4)
        progress_layout.addWidget(self._progress_panel)
        top_layout.addWidget(progress_container)

        # 日志区
        log_widget_section = QWidget()
        log_widget_section.setMinimumHeight(100)  # 确保日志区最小可见高度
        log_section_layout = QVBoxLayout(log_widget_section)
        log_section_layout.setContentsMargins(8, 4, 12, 8)
        log_section_layout.setSpacing(2)

        log_label = QLabel("📝 运行日志")
        log_label.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #e2e8f0; "
            "background: transparent; border: none; padding: 2px 0;"
        )
        log_section_layout.addWidget(log_label)

        self._log_widget = LogWidget()
        self._log_widget.setMinimumHeight(40)
        log_section_layout.addWidget(self._log_widget, 1)

        # QSplitter：上区（可滚动）vs 日志区，可拖拽缩放
        h_splitter = QSplitter(Qt.Vertical)
        h_splitter.setChildrenCollapsible(False)
        h_splitter.setStyleSheet(
            "QSplitter::handle { background: #1e293b; height: 3px; border-radius: 1px; }"
            "QSplitter::handle:hover { background: #3b82f6; }"
        )

        # 上区用 QScrollArea 包裹，窗口缩小时可滚动
        top_scroll = QScrollArea()
        top_scroll.setWidgetResizable(True)
        top_scroll.setFrameShape(QFrame.NoFrame)
        top_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        top_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        top_scroll.setWidget(top_widget)
        h_splitter.addWidget(top_scroll)
        h_splitter.addWidget(log_widget_section)
        h_splitter.setStretchFactor(0, 1)  # 上区弹性
        h_splitter.setStretchFactor(1, 1)  # 日志区也弹性（公平分配）
        h_splitter.setSizes([300, 200])

        layout.addWidget(h_splitter, 1)

        return panel

    # ----------------------------------------------------------
    # 配置加载
    # ----------------------------------------------------------

    def _update_config_status(self):
        """更新配置状态显示"""
        has_api = self._config.has_deepseek_config
        has_tiku = self._config.has_tiku_config
    
        parts = []
        if has_api:
            key = self._config.deepseek_api_key
            parts.append(f"AI: {key[:6]}***")
        if has_tiku:
            parts.append("题库: 已配置")
    
        if parts:
            self._config_status.setText("  ".join(parts))
            self._config_status.setStyleSheet(
                "font-size: 12px; color: #22c55e; background: transparent; "
                "border: none; padding: 2px;"
            )
        else:
            self._config_status.setText("请填写 API Key 后点击启动")
            self._config_status.setStyleSheet(
                "font-size: 12px; color: #94a3b8; background: transparent; "
                "border: none; padding: 2px;"
            )
    
    def _load_quick_credentials(self):
        """加载已保存的配置到输入框"""
        if self._config.deepseek_api_key:
            self._quick_api_key.setText(self._config.deepseek_api_key)
        if self._config.tiku_api_url:
            self._quick_tiku_url.setText(self._config.tiku_api_url)
        if self._config.tiku_api_key:
            self._quick_tiku_key.setText(self._config.tiku_api_key)
    
    def _save_quick_credentials(self):
        """保存输入框中的配置（仅 API Key 和题库配置）"""
        api_key = self._quick_api_key.text().strip()
        tiku_url = self._quick_tiku_url.text().strip()
        tiku_key = self._quick_tiku_key.text().strip()
    
        if api_key:
            self._config.save_deepseek_config(api_key=api_key)
    
        # 保存题库配置
        if tiku_url:
            self._config.save_tiku_config(tiku_url, tiku_key)
    
        # 刷新答题引擎配置缓存，确保新配置立即生效
        refresh_config()
    
        return api_key
    
    def _on_launch_browser(self):
        """点击「启动浏览器」按钮 - 启动浏览器并让用户手动登录
        
        底部按钮仅用于手动登录流程，不复用任何已保存账号。
        只有账号卡片上的「启动」按钮才会复用对应账号的会话。
        """
        # 保存快捷输入的配置
        self._save_quick_credentials()

        # 检查 API Key
        api_key = self._quick_api_key.text().strip() or self._config.deepseek_api_key
        if not api_key:
            show_styled_message(
                self, "提示",
                "请填写 DeepSeek API Key",
                "warning"
            )
            return

        # 底部按钮：不创建占位符账号，用默认数据目录启动浏览器
        # 登录成功后再由 _ensure_account_saved() 创建账号
        # （避免未登录时就在账号列表中出现“新账号”）
        
        # 禁用按钮
        self._launch_btn.setEnabled(False)
        self._launch_status.setStyleSheet(
            "font-size: 12px; color: #3b82f6; background: transparent; border: none; padding: 4px;"
        )
        self._launch_status.setText("正在启动浏览器...")

        # 通过 AsyncWorker 在持久化事件循环中启动浏览器
        async def _launch():
            self._signals.log_signal.emit("正在检查并启动浏览器...", "info")
            try:
                # 账号已在上层设置，直接启动浏览器
                if not self._browser.is_started:
                    try:
                        await asyncio.wait_for(self._browser.start(headless=False), timeout=30)
                    except asyncio.TimeoutError:
                        self._signals.browser_failed.emit("浏览器启动超时")
                        return

                page = self._browser.tab
                if not page:
                    self._signals.browser_failed.emit("浏览器启动后未获取到页面")
                    return

                # 关闭多余标签页
                self._signals.log_signal.emit("正在关闭多余标签页...", "info")
                await self._browser.close_extra_pages()

                # 检查登录状态
                self._signals.log_signal.emit("正在检查登录状态...", "info")
                try:
                    logged_in = await asyncio.wait_for(self._browser.is_logged_in(), timeout=15)
                except asyncio.TimeoutError:
                    logger.info("登录状态检查超时，可能浏览器已关闭")
                    self._browser.force_reset()
                    self._signals.login_prompt.emit("浏览器已关闭或响应超时，请重新启动")
                    return

                if logged_in:
                    self._signals.log_signal.emit("已检测到登录状态", "success")
                    # 检测到用户名并保存账号
                    username = await self._browser.get_logged_in_username()
                    if username:
                        self._signals.log_signal.emit(f"已登录用户: {username}", "success")
                        self._signals.state_signal.emit({"action": "save_account", "username": username})
                    # 登录成功后立即备份 cookies
                    try:
                        await self._browser._save_cookies_backup()
                    except Exception:
                        pass
                    self._signals.browser_ready.emit()
                    return

                # 未登录 → 导航到学习通登录页
                self._signals.log_signal.emit("正在导航到学习通登录页...", "info")
                try:
                    login_url = "https://passport2.chaoxing.com/login"
                    await asyncio.wait_for(self._browser.navigate(login_url), timeout=15)
                    self._signals.log_signal.emit("请在浏览器窗口中完成登录", "info")
                    self._signals.log_signal.emit("登录成功后将自动检测并保存账号", "info")
                except asyncio.TimeoutError:
                    self._browser.force_reset()
                    self._signals.login_prompt.emit("浏览器响应超时，请重新启动")
                    return
                except Exception as nav_err:
                    logger.warning(f"导航到登录页失败: {nav_err}")
                    nav_ok = await self._browser.navigate_to_chaoxing()
                    if not nav_ok:
                        self._signals.browser_failed.emit(
                            "无法访问学习通页面，请检查网络连接后重试。"
                        )
                        return
                    self._signals.log_signal.emit("请在浏览器窗口中完成登录", "info")

                # 设置导航回调：页面跳转时立即检测登录状态
                navigation_detected = asyncio.Event()
                
                async def on_navigation(url: str):
                    """页面导航回调 - 触发立即检测"""
                    if "chaoxing.com" in url and "passport" not in url:
                        logger.info(f"导航回调触发: {url[:60]}")
                        navigation_detected.set()
                
                self._browser.set_on_navigation_callback(on_navigation)

                # 等待用户登录（轮询检测 + 导航事件）
                max_wait = 300  # 最多等待 5 分钟
                check_interval = 1.5  # 每 1.5 秒检查一次（快速响应登录成功）
                waited = 0
                
                while waited < max_wait:
                    # 等待导航事件或超时
                    try:
                        await asyncio.wait_for(navigation_detected.wait(), timeout=check_interval)
                        # 导航事件触发，立即检测
                        logger.info("导航事件触发，立即检测登录状态")
                        await asyncio.sleep(0.5)  # 等待页面加载
                        navigation_detected.clear()
                    except asyncio.TimeoutError:
                        # 定时器超时，正常轮询
                        waited += check_interval
                    except Exception:
                        break
                    
                    # 轻量检查：不导航，避免打断用户操作
                    try:
                        logged_in = await asyncio.wait_for(
                            self._browser.is_logged_in(skip_navigate=True), timeout=5
                        )
                        if logged_in:
                            self._signals.log_signal.emit("检测到登录成功！", "success")
                            username = await self._browser.get_logged_in_username()
                            if username:
                                self._signals.log_signal.emit(f"已登录用户: {username}", "success")
                                self._signals.state_signal.emit({"action": "save_account", "username": username})
                            # 登录成功后立即备份 cookies
                            try:
                                await self._browser._save_cookies_backup()
                            except Exception:
                                pass
                            self._browser.set_on_navigation_callback(None)
                            self._signals.browser_ready.emit()
                            return
                    except asyncio.TimeoutError:
                        # 超时可能是瞬态问题，继续轮询
                        logger.debug("轻量检查超时，继续等待...")
                        continue
                    except Exception:
                        # 瞬态错误，跳过本次检查
                        logger.debug("轻量检查异常，继续等待...")
                        continue
                    
                    # 如果轻量检查未检测到，每 15 秒做一次完整检查
                    if waited % 15 == 0 and waited > 0:
                        try:
                            self._signals.log_signal.emit(f"等待登录中... 正在验证 ({waited}s)", "info")
                            logged_in = await asyncio.wait_for(
                                self._browser.is_logged_in(skip_navigate=False), timeout=15
                            )
                            if logged_in:
                                self._signals.log_signal.emit("检测到登录成功！", "success")
                                username = await self._browser.get_logged_in_username()
                                if username:
                                    self._signals.state_signal.emit({"action": "save_account", "username": username})
                                try:
                                    await self._browser._save_cookies_backup()
                                except Exception:
                                    pass
                                self._browser.set_on_navigation_callback(None)
                                self._signals.browser_ready.emit()
                                return
                        except asyncio.TimeoutError:
                            # 完整检查超时，可能是浏览器无响应
                            logger.warning("完整检查超时，浏览器可能无响应")
                            break
                        except Exception:
                            # 瞬态错误，继续等待
                            logger.debug("完整检查异常，继续等待...")
                            pass
                    elif waited % 10 == 0:
                        self._signals.log_signal.emit(f"等待登录中... ({waited}s)", "info")

                # 退出轮询 - 检查浏览器状态再决定如何处理
                self._browser.set_on_navigation_callback(None)
                if not self._browser.is_started:
                    # 浏览器已停止，重置状态
                    self._browser.force_reset()
                    self._signals.log_signal.emit("浏览器已关闭", "warning")
                    self._signals.login_prompt.emit("请重新启动浏览器")
                else:
                    # 浏览器还在运行，可能是检查超时，不强制重置
                    self._signals.log_signal.emit("等待登录超时，请在浏览器中完成登录后重试", "warning")
                    self._signals.login_prompt.emit("等待登录超时，请重新点击启动")

            except Exception as e:
                logger.error(f"浏览器启动异常: {e}", exc_info=True)
                self._browser.set_on_navigation_callback(None)
                self._browser.force_reset()
                self._signals.login_prompt.emit("请重新启动浏览器")

        def _on_launch_err(e):
            logger.error(f"浏览器启动协程异常: {e}", exc_info=True)
            self._browser.set_on_navigation_callback(None)
            self._browser.force_reset()
            self._signals.login_prompt.emit("请重新启动浏览器")

        self._async_worker.run_coroutine(_launch(), error_callback=_on_launch_err)

    def _ensure_account_saved(self, username: str, login_method: str = "password"):
        """
        确保账号已保存。如果已存在相同 display_name 的账号则更新，
        否则创建新账号并将浏览器绑定到该账号。
        同时更新"默认账号"为实际用户名。
        """
        if not username:
            return
        display_name = AccountManager.mask_username(username)
        accounts = self._account_mgr.list_accounts()

        # 查找已有的同名账号
        for acc in accounts:
            if acc["display_name"] == display_name:
                self._account_mgr.update_last_login(acc["id"])
                self._account_mgr.set_active_account(acc["id"])
                self._browser.set_account(acc["id"])
                logger.info(f"已更新账号: {display_name} ({acc['id']})")
                self._refresh_account_list()
                self._current_account_label.setText(f"👤 {display_name}")
                self._current_account_label.setVisible(True)
                return

        # 检查是否有"默认账号"或"新账号"需要更新为实际用户名
        legacy_names = ["默认账号", "Ĭ\u8ba4\u8d26\u53f7", "新账号"]  # 兼容编码问题和临时占位符
        for acc in accounts:
            if acc["display_name"] in legacy_names or acc.get("login_method") == "legacy":
                # 更新显示名称为实际用户名
                self._account_mgr.update_display_name(acc["id"], display_name)
                self._account_mgr.update_last_login(acc["id"])
                self._account_mgr.set_active_account(acc["id"])
                self._browser.set_account(acc["id"])
                logger.info(f"已更新默认账号为: {display_name} ({acc['id']})")
                self._refresh_account_list()
                self._current_account_label.setText(f"👤 {display_name}")
                self._current_account_label.setVisible(True)
                return

        # 创建新账号
        account_id = self._account_mgr.add_account(display_name, login_method)
        
        # 迁移 cookies 备份：从默认目录迁移到新账号目录
        # （底部启动按钮用默认目录启动，登录成功后才创建账号）
        try:
            old_cookies = self._browser._get_cookies_backup_path()
            self._browser.set_account(account_id)
            new_cookies = self._browser._get_cookies_backup_path()
            if old_cookies != new_cookies and old_cookies.exists():
                import shutil
                new_cookies.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(old_cookies), str(new_cookies))
                logger.info(f"已迁移 cookies 备份到新账号目录")
        except Exception as e:
            logger.debug(f"迁移 cookies 备份失败(可忽略): {e}")
        
        logger.info(f"已创建新账号: {display_name} ({account_id})")
        self._refresh_account_list()
        self._current_account_label.setText(f"👤 {display_name}")
        self._current_account_label.setVisible(True)

    def _on_browser_ready(self):
        """浏览器就绪 → 切换到任务面板"""
        self._connected = True

        # 如果已有 account_id，更新 session 状态并显示账号名
        if self._browser.account_id:
            self._account_mgr.mark_session(self._browser.account_id, True)
            self._account_mgr.update_last_login(self._browser.account_id)
            # 显示当前账号名
            account = self._account_mgr.get_account(self._browser.account_id)
            if account:
                self._current_account_label.setText(f"👤 {account['display_name']}")
                self._current_account_label.setVisible(True)
            # 确保 CompletionDB 使用当前账号的独立库
            self._completion_db.switch_account(self._browser.account_id)

        # 重置进度面板和任务锁，确保新账号看到干净状态
        self._progress_panel.reset()
        self._operator.reset_task_locks()

        # 更新头部连接状态
        self._stats_bar.set_connected(True)

        # 显示并启用重启浏览器按钮和返回按钮
        self._relaunch_btn.setVisible(True)
        self._relaunch_btn.setEnabled(True)
        self._back_btn.setVisible(True)

        # 切换右侧面板: 设置 → 任务
        self._right_stack.setCurrentIndex(1)

        # 左侧: 隐藏占位，显示课程树
        self._left_placeholder.setVisible(False)
        self._chapter_tree.setVisible(True)
        self._completion_label.setVisible(True)
        self._chapter_btn_widget.setVisible(True)
        self._refresh_btn.setEnabled(True)
        self._refresh_all_btn.setEnabled(True)
        self._update_completion_label()

        # 加载课程（登录后首次加载，跳过session刷新以加快速度）
        self._load_courses(skip_session_refresh=True)

    def _on_back_to_setup(self):
        """返回到设置/账号选择面板"""
        # 切换回设置面板
        self._right_stack.setCurrentIndex(0)
        self._connected = False
        self._stats_bar.set_connected(False)

        # 清除本次会话的账号选择标记
        self._user_selected_account = None

        # 清除 operator 缓存，避免切换账号后拿到旧账号的课程/章节数据
        self._operator.clear_caches()
        self._chapters_loaded_set.clear()
        self._chapter_tree.clear()
        self._progress_panel.reset()          # 重置任务进度面板
        self._completion_db.switch_account(None)  # 切回全局默认库

        # 隐藏返回按钮和账号标签
        self._back_btn.setVisible(False)
        self._current_account_label.setVisible(False)
        self._relaunch_btn.setVisible(False)

        # 隐藏课程树，显示占位
        self._left_placeholder.setVisible(True)
        self._chapter_tree.setVisible(False)
        self._completion_label.setVisible(False)
        self._chapter_btn_widget.setVisible(False)

        # 禁用启动按钮，等待浏览器完全关闭后再启用
        self._launch_btn.setEnabled(False)
        self._launch_status.setText("正在关闭浏览器...")
        self._launch_status.setStyleSheet(
            "font-size: 12px; color: #3b82f6; background: transparent; border: none; padding: 4px;"
        )

        async def _stop():
            await self._browser.stop_for_restart()
            self._browser.set_account(None)
            # 浏览器完全关闭后，重新启用启动按钮并刷新账号列表
            self._signals.login_prompt.emit("已返回，可选择其他账号或重新启动")

        def _on_stop_err(e):
            logger.warning(f"关闭浏览器异常: {e}")
            self._browser.force_reset()
            self._browser.set_account(None)
            self._signals.login_prompt.emit("已返回，可选择其他账号或重新启动")

        self._async_worker.run_coroutine(_stop(), error_callback=_on_stop_err)

        # 立即刷新账号列表（同步操作）
        self._refresh_account_list()

    def _on_browser_failed(self, error_msg: str):
        """浏览器启动失败"""
        self._launch_btn.setEnabled(True)
        self._relaunch_btn.setEnabled(True)  # 重启按钮也恢复可用
        self._launch_status.setStyleSheet(
            "font-size: 12px; color: #ef4444; background: transparent; border: none; padding: 4px;"
        )
        self._launch_status.setText(f"启动失败: {error_msg}")

        show_styled_message(
            self, "浏览器启动失败",
            f"{error_msg}\n\n请确保已安装 Chrome 浏览器",
            level="error",
        )

    def _on_browser_watchdog(self):
        """
        浏览器看门狗 - 定期检测浏览器进程是否存活。
            
        解决问题: 用户在外部关闭浏览器后，软件内部状态未重置，
        导致再次点击启动按钮时无响应。
        检测延迟: ~1.5秒（proc.poll 是确定性检查，无需多次确认）
        """
        # 仅在浏览器"认为已启动"时检查
        if not self._browser._started:
            return
    
        # 检查浏览器进程是否还在（proc.poll 是确定性的，不存在误判）
        if self._browser._is_browser_process_alive():
            return
    
        logger.warning("看门狗检测到浏览器进程已退出，自动重置状态")

        # 强制重置浏览器状态
        self._browser.set_on_navigation_callback(None)
        self._browser.force_reset()

        # 如果当前是已连接状态，切换到未连接
        if self._connected:
            self._connected = False
            self._stats_bar.set_connected(False)
            self._log_widget.append_log("浏览器已被关闭，请重新启动", "warning")
            # 清除旧账号数据状态
            self._operator.clear_caches()
            self._chapters_loaded_set.clear()
            self._chapter_tree.clear()
            self._progress_panel.reset()
            self._completion_db.switch_account(None)
            # 切换回设置面板
            self._right_stack.setCurrentIndex(0)
            self._back_btn.setVisible(False)
            self._current_account_label.setVisible(False)
            self._relaunch_btn.setVisible(False)
            self._left_placeholder.setVisible(True)
            self._chapter_tree.setVisible(False)
            self._completion_label.setVisible(False)
            self._chapter_btn_widget.setVisible(False)

        # 恢复启动按钮
        self._launch_btn.setEnabled(True)
        self._launch_status.setStyleSheet(
            "font-size: 12px; color: #f59e0b; background: transparent; border: none; padding: 4px;"
        )
        self._launch_status.setText("浏览器已关闭，可重新启动")

    def _on_login_prompt(self, msg: str):
        """登录提示（内联显示，不弹窗）"""
        self._launch_btn.setEnabled(True)
        self._launch_status.setStyleSheet(
            "font-size: 12px; color: #f59e0b; background: transparent; border: none; padding: 4px;"
        )
        self._launch_status.setText(f"⚠ {msg}")
        self._refresh_account_list()

    def _on_relaunch_browser(self):
        """重启浏览器（浏览器被关闭或卡死时）"""
        self._relaunch_btn.setEnabled(False)
        self._log_widget.append_log("正在重启浏览器...", "info")
        self._stats_bar.set_connected(False)

        async def _relaunch():
            try:
                # 1. 强制重置浏览器状态（无论进程是否还在）
                self._browser.force_reset()
                await asyncio.sleep(0.5)

                # 2. 重新启动浏览器
                self._signals.log_signal.emit("正在启动 Chrome...", "info")
                await self._browser.start(headless=False)

                page = self._browser.tab
                if not page:
                    self._signals.browser_failed.emit("浏览器启动后未获取到页面")
                    return

                await self._browser.close_extra_pages()

                # 3. 检查登录状态（cookies 会自动恢复）
                self._signals.log_signal.emit("正在检查登录状态...", "info")
                logged_in = await self._browser.is_logged_in()

                if logged_in:
                    self._signals.log_signal.emit("登录状态已恢复，无需重新登录", "success")
                    self._signals.browser_ready.emit()
                else:
                    # cookies 恢复失败，尝试自动登录
                    self._signals.log_signal.emit("登录状态已失效，尝试自动登录...", "warning")
                    username = self._config.chaoxing_username
                    password = self._config.chaoxing_password
                    if username and password:
                        try:
                            success = await self._operator.login(
                                page, username, password,
                                browser=self._browser._browser
                            )
                            if success:
                                self._signals.log_signal.emit("自动登录成功", "success")
                                await self._browser._save_cookies_backup()
                                self._signals.browser_ready.emit()
                                return
                        except Exception as e:
                            self._signals.log_signal.emit(f"自动登录失败: {e}", "warning")
                    # 回退到登录面板
                    self._signals.log_signal.emit("请回到登录面板重新启动浏览器", "warning")
                    self._signals.browser_failed.emit("登录状态已失效，请重新启动浏览器")

            except Exception as e:
                logger.error(f"重启浏览器异常: {e}", exc_info=True)
                self._signals.browser_failed.emit(str(e))

        def _on_err(e):
            logger.error(f"重启浏览器协程异常: {e}", exc_info=True)
            self._signals.browser_failed.emit(str(e))

        self._async_worker.run_coroutine(_relaunch(), error_callback=_on_err)

    # ----------------------------------------------------------
    # 课程加载
    # ----------------------------------------------------------

    def _load_courses(self, skip_session_refresh=False):
        if not self._connected:
            self._log_widget.append_log("请先启动浏览器", "warning")
            return
        if not self._browser.tab:
            self._log_widget.append_log("浏览器页面未就绪，请等待浏览器完全启动", "warning")
            return
        self._refresh_btn.setEnabled(False)
        self._refresh_all_btn.setEnabled(False)
        self._log_widget.append_log("正在加载课程列表...", "info")

        async def _do_load():
            courses = await self._operator.get_courses(
                self._browser.tab, 
                browser=self._browser._browser,
                chaoxing_browser=self._browser,  # 传递 ChaoxingBrowser 以便恢复 cookies
                skip_session_refresh=skip_session_refresh  # 登录后首次加载可跳过session刷新
            )
            # 课程加载成功后备份 cookies（覆盖扫码登录等场景）
            try:
                await self._browser._save_cookies_backup()
            except Exception:
                pass
            self._signals.courses_loaded.emit(courses)

        def _on_load_err(e):
            self._refresh_btn.setEnabled(True)
            self._refresh_all_btn.setEnabled(True)
            logger.error(f"加载课程失败: {e}", exc_info=True)
            self._signals.log_signal.emit(f"加载课程失败: {e}", "error")

        self._async_worker.run_coroutine(_do_load(), error_callback=_on_load_err)

    def _on_courses_loaded(self, courses: list):
        self._refresh_btn.setEnabled(True)
        self._refresh_all_btn.setEnabled(True)
        self._chapter_tree.load_courses(courses)
        self._chapters_loaded_set.clear()
        self._log_widget.append_log(
            f"已加载 {len(courses)} 门课程，点击课程名称加载章节", "success"
        )

    def _on_chapters_loaded(self, course_index: int, chapters: list):
        self._chapter_tree.load_chapters(course_index, chapters)
        # 加载后刷新已完成状态视觉（已完成的章节显示灰色标记）
        self._chapter_tree._refresh_all_completed_visuals()
        if not chapters:
            # 加载失败：不加入已加载集合，允许再次点击重试
            course_item = self._chapter_tree.topLevelItem(course_index)
            if course_item:
                course_item.setText(1, "加载失败")
            self._log_widget.append_log("章节加载失败，点击课程名称可重试", "error")
            return
        self._chapters_loaded_set.add(course_index)
        hw_count = sum(1 for c in chapters if c.get("task_type") == "homework")
        exam_count = sum(1 for c in chapters if c.get("task_type") == "exam")
        ch_count = sum(1 for c in chapters if c.get("task_type") == "chapter")
        summary = f"加载完成: {len(chapters)} 项"
        details = []
        if ch_count:
            details.append(f"{ch_count} 章节")
        if hw_count:
            details.append(f"{hw_count} 作业")
        if exam_count:
            details.append(f"{exam_count} 考试")
        if details:
            summary += " (" + ", ".join(details) + ")"
        self._log_widget.append_log(summary, "success")
        self._update_completion_label()

    def _on_tree_item_clicked(self, item, column):
        data = item.data(0, Qt.UserRole)
        if not data:
            return

        # 判断是课程项还是章节项
        is_course = "id" in data and "url" in data and "name" in data

        if is_course:
            course_name = data.get("name", "")
            # 查找课程在树中的索引
            course_index = -1
            for i in range(self._chapter_tree.topLevelItemCount()):
                if self._chapter_tree.topLevelItem(i) is item:
                    course_index = i
                    break

            if course_index >= 0:
                # 检查是否加载失败，失败状态允许重新点击加载
                status = item.text(1)
                is_failed = status.startswith("加载失败")
                if course_index not in self._chapters_loaded_set or is_failed:
                    self._load_chapters_for_course(course_index, data)
                else:
                    self._log_widget.append_log(f"选中课程: {course_name}", "info")
        elif "url" in data:
            self._log_widget.append_log(f"选中: {data.get('name', '')}", "info")

    def _load_chapters_for_course(self, course_index: int, course_data: dict):
        """按需加载指定课程的章节列表"""
        url = course_data.get("url", "")
        name = course_data.get("name", "未知课程")
        if not url:
            self._log_widget.append_log(f"课程 {name} 无有效URL", "warning")
            return

        # 显示加载状态
        course_item = self._chapter_tree.topLevelItem(course_index)
        if course_item:
            course_item.setText(1, "加载中...")

        self._log_widget.append_log(f"正在加载 [{name}] 的章节...", "info")

        async def _do_load():
            chapters = await self._operator.get_chapters(self._browser.tab, url, browser=self._browser._browser)
            # 为每个章节标记 task_type
            for ch in chapters:
                ch.setdefault("task_type", "chapter")
            self._signals.chapters_loaded.emit(course_index, chapters)

        def _on_err(e):
            logger.error(f"加载章节失败({name}): {e}", exc_info=True)
            self._signals.log_signal.emit(f"加载章节失败 ({name}): {e}", "error")
            if course_item:
                course_item.setText(1, "加载失败")

        self._async_worker.run_coroutine(_do_load(), error_callback=_on_err)

    # ----------------------------------------------------------
    # 任务执行
    # ----------------------------------------------------------

    def _apply_config(self):
        """将 UI 控件的值同步到 Config 单例（内存 + .env 持久化）"""
        mapping = {
            'answer_interval_min': self._interval_min.value(),
            'answer_interval_max': self._interval_max.value(),
            'auto_submit': self._auto_submit_cb.isChecked(),
            'auto_video': self._auto_video_cb.isChecked(),
            'video_speed': self._video_speed_combo.currentData(),
            'auto_jump': self._auto_jump_cb.isChecked(),
            'auto_homework': self._auto_homework_cb.isChecked(),
            'auto_exam': self._auto_exam_cb.isChecked(),
            'min_accuracy': self._accuracy_spin.value(),
            # OCS 答题引擎配置
            'upload_type': self._upload_combo.currentData(),
            'worker_threads': self._threads_spin.value(),
            'random_answer': self._random_answer_cb.isChecked(),
            'cache_enabled': self._cache_cb.isChecked(),
            'search_timeout': self._search_timeout_spin.value(),
            # OCS 学习增强配置
            'video_volume': self._volume_slider.value(),
            'review_mode': self._review_mode_cb.isChecked(),
            'force_study': self._force_study_cb.isChecked(),
        }
        # 更新内存中的 Config 属性
        for attr, val in mapping.items():
            setattr(self._config, attr, val)
        # 持久化到 .env（节流：距上次写入 <1s 则跳过）
        self._persist_config(mapping)
        # 刷新答题引擎配置缓存
        refresh_config()

    def _persist_config(self, mapping: dict, min_interval: float = 1.0):
        """将配置值写入 .env 文件，带节流保护避免频繁磁盘 I/O"""
        now = time.monotonic()
        if now - self._last_persist_time < min_interval:
            return
        self._last_persist_time = now
        env_path = str(ENV_FILE)
        for key, val in mapping.items():
            env_key = key.upper()
            if isinstance(val, bool):
                val = "true" if val else "false"
            _env_set_key(env_path, env_key, str(val))

    def _on_refresh_clicked(self):
        """双击刷新检测：400ms 内两次点击才触发刷新"""
        from PyQt5.QtCore import QTimer
        self._refresh_click_count += 1
        if self._refresh_click_count == 1:
            self._refresh_timer = QTimer(self)
            self._refresh_timer.setSingleShot(True)
            self._refresh_timer.timeout.connect(self._reset_refresh_count)
            self._refresh_timer.start(400)
        elif self._refresh_click_count >= 2:
            if self._refresh_timer:
                self._refresh_timer.stop()
            self._refresh_click_count = 0
            self._load_courses()

    def _reset_refresh_count(self):
        self._refresh_click_count = 0

    def _on_splitter_moved(self, pos, index):
        """限制左侧栏拖拽范围在 30%-40% 之间"""
        total = self._splitter.width()
        if total <= 0:
            return
        left_width = self._splitter.sizes()[0]
        pct = left_width / total
        if pct < 0.30:
            # 低于 30%，拉回 30%
            self._splitter.setSizes([int(total * 0.30), int(total * 0.70)])
        elif pct > 0.40:
            # 超过 40%，拉回 40%
            self._splitter.setSizes([int(total * 0.40), int(total * 0.60)])

    def _on_speed_changed(self, index=None):
        """倍速改变时更新 tooltip 提示"""
        speed = self._video_speed_combo.currentData()
        if speed and speed > 2:
            self._video_speed_combo.setToolTip(f"⚠ {speed}x 倍速可能被平台限制")
        elif speed and speed == 2:
            self._video_speed_combo.setToolTip("2x 是平台普遍支持的最高倍速")
        else:
            self._video_speed_combo.setToolTip("")

    def _on_skip_and_start(self):
        """一键跳过已完成：自动选中未完成章节并执行"""
        # 检查是否有课程已加载章节
        if not self._chapters_loaded_set:
            show_styled_message(self, "提示", "请先点击课程加载章节，再使用“仅未完成”功能", "warning")
            return

        # 保存当前勾选状态，执行完后恢复
        self._saved_selection_urls = self._get_current_selection_urls()

        uncompleted = self._chapter_tree.get_uncompleted_chapters()
        if not uncompleted:
            show_styled_message(
                self, "提示",
                "所有已选章节均已完成，无需执行\n如需重新执行请使用“从头开始”",
                "info"
            )
            self._saved_selection_urls = None
            return

        # 统计跳过的数量
        all_selected = self._chapter_tree.get_selected_chapters()
        skipped = len(all_selected) - len(uncompleted)

        # 在树中只勾选未完成的章节
        self._chapter_tree.select_chapters_by_urls([c["url"] for c in uncompleted])

        self._log_widget.append_log(
            f"仅未完成：筛选出 {len(uncompleted)} 个未完成章节"
            + (f"，跳过 {skipped} 个已完成" if skipped > 0 else ""),
            "info"
        )
        self._on_start()

    def _get_current_selection_urls(self) -> list:
        """获取当前树中勾选的章节URL列表（用于恢复）"""
        urls = []
        for i in range(self._chapter_tree.topLevelItemCount()):
            course_item = self._chapter_tree.topLevelItem(i)
            for j in range(course_item.childCount()):
                child = course_item.child(j)
                if child.checkState(0) == Qt.Checked:
                    data = child.data(0, Qt.UserRole)
                    if data and data.get("url"):
                        urls.append(data["url"])
        return urls

    def _on_restart(self):
        """选中课程的所有章节并执行（不跳过已完成）"""
        selected = self._chapter_tree.get_selected_chapters()
        if not selected:
            show_styled_message(self, "提示", "请先选择要执行的课程或章节", "warning")
            return
        self._log_widget.append_log(f"从头开始：执行已选 {len(selected)} 个章节，忽略已完成标记", "info")
        self._on_start(force_all=True)

    def _on_start(self, force_all: bool = False):
        selected_chapters = self._chapter_tree.get_selected_chapters()

        if not selected_chapters:
            show_styled_message(self, "提示", "请先选择要执行的章节", "warning")
            return

        # 保存凭据（确保 API Key 等配置在开始执行前已持久化）
        self._save_quick_credentials()
        self._apply_config()
        ch_count = sum(1 for c in selected_chapters if c.get("task_type", "chapter") == "chapter")
        hw_count = sum(1 for c in selected_chapters if c.get("task_type") == "homework")
        exam_count = sum(1 for c in selected_chapters if c.get("task_type") == "exam")
        parts = []
        if ch_count:
            parts.append(f"{ch_count}章节")
        if hw_count:
            parts.append(f"{hw_count}作业")
        if exam_count:
            parts.append(f"{exam_count}考试")
        task_desc = "、".join(parts) if parts else f"{len(selected_chapters)}个任务"
        self._log_widget.append_log(
            f"开始执行 {len(selected_chapters)} 个任务 ({task_desc})", "success"
        )

        # 清空任务处理锁，允许新一次执行
        if self._operator:
            self._operator.reset_task_locks()
            # 同步已完成章节 key 到 operator（用于跳过已完成的章节）
            if not force_all:
                self._operator.set_completed_chapter_keys(self._chapter_tree.get_completed_keys())

        self._task_runner = TaskRunner(self._browser, self._config, force_all=force_all)
        self._task_runner.set_callbacks(
            on_progress=lambda name, cur, total, status="": self._signals.progress_signal.emit(name, cur, total, status),
            on_log=lambda msg, level="info": self._signals.log_signal.emit(msg, level),
            on_state_change=lambda state: self._signals.state_signal.emit(state),
            on_complete=lambda result: self._on_task_complete(result),
            on_chapter_done=lambda url: self._signals.chapter_done.emit(url),
        )

        self._start_btn.setEnabled(False)
        self._skip_btn.setEnabled(False)
        self._restart_btn.setEnabled(False)
        self._pause_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)
        self._progress_panel.reset()

        async def _run():
            await self._task_runner.start(selected_chapters)

        def _on_task_err(e):
            logger.error(f"任务执行异常: {e}", exc_info=True)
            self._signals.log_signal.emit(f"任务执行异常: {e}", "error")
            self._start_btn.setEnabled(True)
            self._skip_btn.setEnabled(True)
            self._restart_btn.setEnabled(True)
            self._pause_btn.setEnabled(False)
            self._stop_btn.setEnabled(False)

        self._async_worker.run_coroutine(_run(), error_callback=_on_task_err)

    def _on_pause_resume(self):
        if not self._task_runner:
            return
        if self._task_runner.is_running:
            self._task_runner.pause()
            self._pause_btn.setText("▶ 继续")
        elif self._task_runner.is_paused:
            self._task_runner.resume()
            self._pause_btn.setText("⏸ 暂停")

    def _on_stop(self):
        if self._task_runner:
            self._task_runner.stop()
            self._log_widget.append_log("正在停止任务...", "warning")

    def _on_task_complete(self, result):
        self._start_btn.setEnabled(True)
        self._skip_btn.setEnabled(True)
        self._restart_btn.setEnabled(True)
        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._pause_btn.setText("⏸ 暂停")

        # 自动标记成功完成的章节
        if result.completed_urls:
            self._chapter_tree.mark_completed(result.completed_urls)
            self._save_completion_state()
            self._update_completion_label()
            self._log_widget.append_log(
                f"已标记 {len(result.completed_urls)} 个章节为已完成", "success"
            )

        # 恢复“仅未完成”执行前的勾选状态
        if self._saved_selection_urls:
            self._chapter_tree.select_chapters_by_urls(self._saved_selection_urls)
            self._saved_selection_urls = None
            self._log_widget.append_log("已恢复执行前的章节勾选状态", "info")

    # ----------------------------------------------------------
    # 回调
    # ----------------------------------------------------------

    def _on_log(self, message: str, level: str):
        self._log_widget.append_log(message, level)
        # 同步更新悬浮球日志
        if self._floating_ball:
            self._floating_ball.append_log(message, level)

    def _on_progress(self, task_name: str, current: int, total: int, status: str):
        self._progress_panel.update_progress(task_name, current, total, status)
        # 同步更新悬浮球进度
        if self._floating_ball:
            progress = current / total if total > 0 else 0.0
            self._floating_ball.set_progress(progress, task_name)

    def _on_chapter_done(self, url: str):
        """单个章节实时完成时立即刷新进度显示并持久化"""
        key = self._chapter_tree._chapter_key(url)
        logger.info(f"实时章节完成: key={key}, url={url[:60]}")
        # 精确更新单个章节项（高效，不遍历全树）
        found = self._chapter_tree.mark_single_completed(url)
        if not found:
            logger.warning(f"实时标记未找到匹配项: key={key}")
        # 强制 Qt 立即重绘树形控件
        self._chapter_tree.viewport().update()
        self._chapter_tree.repaint()
        self._update_completion_label()
        # 立即写入数据库，防止中途关闭丢失
        self._completion_db.add(key)

    def _on_state_change(self, state):
        # 处理自定义动作（如保存账号）
        if isinstance(state, dict):
            action = state.get("action")
            if action == "save_account":
                username = state.get("username")
                if username:
                    self._ensure_account_saved(username, login_method="manual")
                    logger.info(f"已自动保存账号: {username}")
            return

        if state == TaskState.RUNNING:
            self._start_btn.setEnabled(False)
            self._skip_btn.setEnabled(False)
            self._restart_btn.setEnabled(False)
            self._pause_btn.setEnabled(True)
            self._pause_btn.setText("⏸ 暂停")
            self._stop_btn.setEnabled(True)
            self._update_floating_ball(state="running", status="运行中")
        elif state == TaskState.PAUSED:
            self._update_floating_ball(state="paused", status="已暂停")
        elif state == TaskState.COMPLETED:
            self._start_btn.setEnabled(True)
            self._skip_btn.setEnabled(True)
            self._restart_btn.setEnabled(True)
            self._pause_btn.setEnabled(False)
            self._stop_btn.setEnabled(False)
            self._pause_btn.setText("⏸ 暂停")
            self._update_floating_ball(state="completed", progress=1.0, status="已完成")
        elif state in (TaskState.STOPPED, TaskState.ERROR):
            self._start_btn.setEnabled(True)
            self._skip_btn.setEnabled(True)
            self._restart_btn.setEnabled(True)
            self._pause_btn.setEnabled(False)
            self._stop_btn.setEnabled(False)
            self._pause_btn.setText("⏸ 暂停")
            err_state = "error" if state == TaskState.ERROR else "idle"
            err_status = "出错" if state == TaskState.ERROR else "已停止"
            self._update_floating_ball(state=err_state, status=err_status)

    # ----------------------------------------------------------
    # 已完成状态持久化 (SQLite)
    # ----------------------------------------------------------

    def _load_completion_state(self):
        """从 SQLite 数据库加载已完成章节集合，并自动迁移旧 JSON 数据"""
        try:
            keys = self._completion_db.get_all_keys()

            # 一次性迁移：如果 DB 为空但旧 JSON 文件存在，导入数据
            if not keys:
                legacy_file = DATA_DIR / "completed.json"
                if legacy_file.exists():
                    try:
                        data = json.loads(legacy_file.read_text(encoding="utf-8"))
                        if isinstance(data, list):
                            from .widgets import ChapterTree
                            legacy_keys = {
                                ChapterTree._chapter_key(item) for item in data
                            }
                            legacy_keys.discard("")
                            if legacy_keys:
                                self._completion_db.add_many(legacy_keys)
                                keys = legacy_keys
                                logger.info(f"已从旧 JSON 迁移 {len(keys)} 条记录到数据库")
                    except Exception as e:
                        logger.warning(f"迁移旧完成状态失败: {e}")

            if keys:
                self._chapter_tree._completed_keys = set(keys)
                self._chapter_tree._refresh_all_completed_visuals()
                logger.info(f"已加载 {len(keys)} 个已完成章节记录")
        except Exception as e:
            logger.warning(f"加载已完成状态失败: {e}")

    def _save_completion_state(self):
        """将已完成 key 集合同步到 SQLite 数据库"""
        try:
            current_keys = self._chapter_tree.get_completed_keys()
            db_keys = self._completion_db.get_all_keys()
            # 计算差异：新增的写入，删除的清理
            to_add = current_keys - db_keys
            to_remove = db_keys - current_keys
            if to_add:
                self._completion_db.add_many(to_add)
            if to_remove:
                self._completion_db.remove_many(to_remove)
        except Exception as e:
            logger.warning(f"保存已完成状态失败: {e}")

    def _update_completion_label(self):
        """刷新完成进度统计条文字"""
        completed_keys = self._chapter_tree.get_completed_keys()
        # 统计已加载的章节总数和其中已完成数
        total = 0
        completed = 0
        for i in range(self._chapter_tree.topLevelItemCount()):
            course_item = self._chapter_tree.topLevelItem(i)
            for j in range(course_item.childCount()):
                total += 1
                data = course_item.child(j).data(0, Qt.UserRole)
                if data:
                    key = self._chapter_tree._chapter_key(data.get("url", ""))
                    if key in completed_keys:
                        completed += 1
        if total == 0:
            self._completion_label.setText("尚未加载章节")
        elif completed == 0:
            self._completion_label.setText(f"进度: 0 / {total} 章节")
        else:
            pct = int(completed / total * 100)
            self._completion_label.setText(f"✓ 已完成 {completed} / {total} 章节 ({pct}%)")

    def _on_clear_completed(self):
        """清除所有已完成状态（带确认）"""
        count = len(self._chapter_tree.get_completed_keys())
        if count == 0:
            self._log_widget.append_log("没有已完成的章节", "info")
            return
        result = show_styled_message(
            self, "清除已完成",
            f"确定要清除 {count} 个章节的“已完成”标记吗？\n清除后可重新执行这些章节。",
            level="question",
            buttons=[("取消", False), ("清除", True)],
        )
        if result:
            self._chapter_tree.clear_completed()
            self._completion_db.clear_all()
            self._update_completion_label()
            self._log_widget.append_log(f"已清除 {count} 个章节的完成标记", "success")

    def _on_clear_cache(self):
        """清除答案缓存"""
        try:
            from core.answer_engine import get_answer_cache
            cache = get_answer_cache()
            count = cache.size()
            cache.clear()
            self._log_widget.append_log(f"已清除 {count} 条答案缓存", "success")
        except Exception as e:
            self._log_widget.append_log(f"清除缓存失败: {e}", "error")

    def _on_exit(self):
        result = show_styled_message(
            self, "退出确认", "确定要退出超星助手吗？",
            level="question",
            buttons=[("取消", False), ("退出", True)],
        )
        if result:
            self.close()

    # ----------------------------------------------------------
    # 自动更新
    # ----------------------------------------------------------

    def _check_update_async(self):
        """后台线程检查更新"""
        if not self._config.github_repo:
            return

        self._check_update_btn.setEnabled(False)
        self._check_update_btn.setText("检查中...")

        def _worker():
            from core.updater import check_update
            try:
                release = check_update(self._config.github_repo)
                if release:
                    self._signals.update_available.emit(release)
                else:
                    # 无更新，恢复按钮
                    QTimer.singleShot(0, lambda: self._check_update_btn.setText("已是最新"))
                    QTimer.singleShot(2000, lambda: self._check_update_btn.setText("检查更新"))
                    QTimer.singleShot(2000, lambda: self._check_update_btn.setEnabled(True))
            except Exception as e:
                logger.debug(f"更新检查失败: {e}")
                QTimer.singleShot(0, lambda: self._check_update_btn.setText("检查失败"))
                QTimer.singleShot(3000, lambda: self._check_update_btn.setText("检查更新"))
                QTimer.singleShot(3000, lambda: self._check_update_btn.setEnabled(True))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_update_available(self, release):
        """发现新版本时弹窗提示"""
        self._check_update_btn.setEnabled(True)
        self._check_update_btn.setText("检查更新")

        from PyQt5.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle("发现新版本")
        msg.setText(f"发现新版本 v{release.version}")
        msg.setInformativeText(
            f"更新内容:\n{release.body[:300] if release.body else '无'}"
            f"\n\n是否立即更新？"
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        msg.button(QMessageBox.StandardButton.Yes).setText("立即更新")
        msg.button(QMessageBox.StandardButton.No).setText("稍后")
        msg.setDefaultButton(QMessageBox.StandardButton.No)
        msg.setIcon(QMessageBox.Information)

        if msg.exec_() == QMessageBox.StandardButton.Yes:
            self._do_update(release)

    def _do_update(self, release):
        """下载并应用更新"""
        from core.updater import download_update, apply_update

        self._check_update_btn.setEnabled(False)
        self._check_update_btn.setText("下载中...")
        self._launch_status.setText("⬇ 正在下载更新...")

        def _on_progress(downloaded, total):
            if total > 0:
                pct = int(downloaded * 100 / total)
                mb = downloaded / (1024 * 1024)
                total_mb = total / (1024 * 1024)
                QTimer.singleShot(0, lambda: self._check_update_btn.setText(f"{pct}% ({mb:.1f}/{total_mb:.1f}MB)"))

        def _worker():
            zip_path = download_update(release, progress_callback=_on_progress)
            if zip_path:
                QTimer.singleShot(0, lambda: self._launch_status.setText("✅ 下载完成，正在应用更新..."))
                ok = apply_update(zip_path)
                if ok:
                    QTimer.singleShot(0, lambda: self._launch_status.setText("✅ 更新成功，即将重启..."))
                    QTimer.singleShot(1000, self.close)
                else:
                    QTimer.singleShot(0, lambda: self._launch_status.setText("❗ 更新应用失败"))
                    QTimer.singleShot(0, lambda: self._check_update_btn.setText("检查更新"))
                    QTimer.singleShot(0, lambda: self._check_update_btn.setEnabled(True))
            else:
                QTimer.singleShot(0, lambda: self._launch_status.setText("❗ 下载失败"))
                QTimer.singleShot(0, lambda: self._check_update_btn.setText("检查更新"))
                QTimer.singleShot(0, lambda: self._check_update_btn.setEnabled(True))

        threading.Thread(target=_worker, daemon=True).start()

    # ----------------------------------------------------------
    # 悬浮球 / 窗口最小化处理
    # ----------------------------------------------------------

    def changeEvent(self, event):
        """窗口状态变化事件 - 最小化时显示悬浮球，浏览器继续运行"""
        if event.type() == QEvent.WindowStateChange:
            if self.windowState() & Qt.WindowMinimized:
                # 窗口最小化 → 显示悬浮球
                if self._config.floating_ball_enabled:
                    self._floating_ball.show_ball()
                    # 浏览器和任务继续在后台运行，不中断
            else:
                # 窗口恢复 → 隐藏悬浮球
                if self._floating_ball.isVisible():
                    self._floating_ball.hide_ball()
        super().changeEvent(event)

    def _restore_from_floating_ball(self):
        """从悬浮球恢复主窗口"""
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.showNormal()
        self.activateWindow()
        self.raise_()
        self._floating_ball.hide_ball()

    def _update_floating_ball(self, state: str = "", progress: float = -1, status: str = "", log_msg: str = "", log_level: str = "info"):
        """更新悬浮球状态和日志"""
        if not self._floating_ball:
            return
        if state:
            self._floating_ball.set_state(state, status)
        if progress >= 0:
            self._floating_ball.set_progress(progress, status)
        if log_msg:
            self._floating_ball.append_log(log_msg, log_level)

    # ----------------------------------------------------------
    # 清理
    # ----------------------------------------------------------

    def closeEvent(self, event):
        if self._task_runner and self._task_runner.is_running:
            self._task_runner.stop()

        # 隐藏悬浮球
        if self._floating_ball:
            self._floating_ball.hide_ball()

        # 关闭前保存完成状态并关闭数据库
        self._save_completion_state()
        self._completion_db.close()

        if self._browser.is_started:
            try:
                # 使用 AsyncWorker 的事件循环来关闭浏览器和清理资源
                async def _shutdown():
                    from core.answer_engine import cleanup as cleanup_http
                    await cleanup_http()
                    await self._browser.stop()

                future = asyncio.run_coroutine_threadsafe(
                    _shutdown(), self._async_worker._loop
                )
                future.result(timeout=10)
            except Exception:
                pass

        event.accept()
