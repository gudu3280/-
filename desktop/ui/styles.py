"""
QSS样式表 - 基于 ui-ux-pro-max Dark Mode (OLED) 设计规范

设计系统:
- Style: Dark Mode (OLED) - 深黑背景 + 高对比度 + 绿色CTA
- Colors: Primary #0F172A, Secondary #1E293B, CTA #22C55E, BG #020617, Text #F8FAFC
- Typography: Fira Code (等宽) / Fira Sans (正文) / Microsoft YaHei (中文)
- Effects: Minimal glow, smooth transitions (150-300ms), high readability
"""

# 主色调 (ui-ux-pro-max Design System)
PRIMARY_COLOR = "#0F172A"        # slate-900 主色调
SECONDARY_COLOR = "#1E293B"      # slate-800 次要色
SUCCESS_COLOR = "#22C55E"        # green-500 CTA/成功
WARNING_COLOR = "#F59E0B"        # amber-500 警告
DANGER_COLOR = "#EF4444"         # red-500 危险
INFO_COLOR = "#3B82F6"           # blue-500 信息
ACCENT_COLOR = "#3B82F6"         # blue-500 强调色
ACCENT_DIM = "#3B82F666"         # 半透明强调色

# 深色主题色板 (OLED Dark Mode)
BG_DARK = "#020617"              # 极深背景 (OLED黑)
BG_CARD = "#0F172A"              # 卡片背景 (slate-900)
BG_CARD_ALT = "#1E293B"          # hover/active (slate-800)
BG_INPUT = "#1E293B"             # 输入框背景 (slate-800)
TEXT_PRIMARY = "#F8FAFC"         # slate-50 主文字
TEXT_SECONDARY = "#94A3B8"       # slate-400 次文字
BORDER_COLOR = "#1E293B"         # slate-800 边框
BORDER_HIGHLIGHT = "#334155"     # slate-700 高亮边框


DARK_THEME = f"""
/* 全局 - Fira Code/Fira Sans + Microsoft YaHei */
QWidget {{
    background-color: {BG_DARK};
    color: {TEXT_PRIMARY};
    font-family: "Fira Sans", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}}

/* 窗口 */
QMainWindow, QDialog {{
    background-color: {BG_DARK};
}}

/* 输入框 - 现代风格 */
QLineEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER_HIGHLIGHT};
    border-radius: 8px;
    padding: 9px 14px;
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT_COLOR};
    font-size: 13px;
}}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {ACCENT_COLOR};
    background-color: #0f172a;
}}

QLineEdit:disabled {{
    background-color: {BG_CARD};
    color: {TEXT_SECONDARY};
}}

/* 按钮 - 渐变风格 (参考 CSDN PyQt5 QSS 实战) */
QPushButton {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_HIGHLIGHT};
    border-radius: 10px;
    padding: 8px 16px;
    color: {TEXT_PRIMARY};
    font-weight: bold;
    font-size: 13px;
    min-height: 16px;
    min-width: 40px;
}}

QPushButton:hover {{
    background-color: {BG_CARD_ALT};
    border-color: {ACCENT_COLOR}88;
}}

QPushButton:pressed {{
    background-color: {ACCENT_COLOR}22;
}}

QPushButton#primaryBtn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #3b82f6, stop:1 #2563eb);
    border: none;
    color: white;
}}

QPushButton#primaryBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #60a5fa, stop:1 #3b82f6);
}}

QPushButton#primaryBtn:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #2563eb, stop:1 #1d4ed8);
}}

QPushButton#dangerBtn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #ef4444, stop:1 #dc2626);
    border: none;
    color: white;
}}

QPushButton#dangerBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #f87171, stop:1 #ef4444);
}}

QPushButton#successBtn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #22c55e, stop:1 #16a34a);
    border: none;
    color: white;
}}

QPushButton#successBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4ade80, stop:1 #22c55e);
}}

QPushButton#warningBtn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #f59e0b, stop:1 #d97706);
    border: none;
    color: white;
}}

QPushButton#warningBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #fbbf24, stop:1 #f59e0b);
}}

QPushButton#skipBtn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #06b6d4, stop:1 #0891b2);
    border: none;
    color: white;
}}

QPushButton#skipBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #22d3ee, stop:1 #06b6d4);
}}

QPushButton#restartBtn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #f97316, stop:1 #ea580c);
    border: none;
    color: white;
}}

QPushButton#restartBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #fb923c, stop:1 #f97316);
}}

QPushButton:disabled {{
    background-color: #1e293b;
    color: #475569;
    border-color: #1e293b;
}}

/* 复选框 - iOS风格toggle (QSS模拟) */
QCheckBox {{
    spacing: 10px;
    color: {TEXT_PRIMARY};
    font-size: 13px;
    padding: 2px 0;
}}

QCheckBox::indicator {{
    width: 38px;
    height: 20px;
    border-radius: 10px;
    border: 1.5px solid {BORDER_HIGHLIGHT};
    background-color: {BG_INPUT};
}}

QCheckBox::indicator:checked {{
    background-color: {ACCENT_COLOR};
    border-color: {ACCENT_COLOR};
    image: none;
}}

QCheckBox::indicator:hover {{
    border-color: {ACCENT_COLOR}88;
}}

/* 标签 - 命名样式 */
QLabel#titleLabel {{
    font-size: 28px;
    font-weight: bold;
    color: {ACCENT_COLOR};
    letter-spacing: 2px;
}}

QLabel#subtitleLabel {{
    font-size: 12px;
    color: {TEXT_SECONDARY};
    letter-spacing: 2px;
}}

QLabel#sectionLabel {{
    font-size: 15px;
    font-weight: bold;
    color: {TEXT_PRIMARY};
}}

QLabel#statusLabel {{
    font-size: 13px;
    color: {TEXT_SECONDARY};
    padding: 4px;
}}

/* 树形控件 - 参考 PyQtClient Dark theme */
QTreeWidget {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_HIGHLIGHT};
    border-radius: 10px;
    padding: 4px;
    outline: none;
}}

QTreeWidget::item {{
    padding: 8px 12px;
    border-radius: 8px;
    min-height: 28px;
}}

QTreeWidget::item:hover {{
    background-color: {BG_CARD_ALT};
    color: {TEXT_PRIMARY};
}}

QTreeWidget::item:selected {{
    background-color: {ACCENT_COLOR}33;
    color: {ACCENT_COLOR};
    border: 1px solid {ACCENT_COLOR}44;
}}

QHeaderView::section {{
    background-color: {BG_CARD};
    border: none;
    padding: 6px;
    font-weight: bold;
    color: {TEXT_SECONDARY};
}}

/* 文本编辑 */
QTextEdit, QPlainTextEdit {{
    background-color: {BG_CARD};
    border: 1.5px solid {BORDER_COLOR};
    border-radius: 8px;
    padding: 10px;
    color: {TEXT_PRIMARY};
    font-family: "Consolas", "Source Code Pro", monospace;
    font-size: 12px;
}}

/* 进度条 - 现代渐变 */
QProgressBar {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER_HIGHLIGHT};
    border-radius: 11px;
    text-align: center;
    color: white;
    height: 22px;
    font-weight: bold;
    font-size: 11px;
}}

QProgressBar::chunk {{
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 #3b82f6, stop:0.5 #22c55e, stop:1 #3b82f6
    );
    border-radius: 10px;
}}

/* 选项卡 */
QTabWidget::pane {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_COLOR};
    border-radius: 6px;
    top: -1px;
}}

QTabBar::tab {{
    background-color: {BG_DARK};
    border: 1px solid {BORDER_COLOR};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 8px 16px;
    margin-right: 2px;
    color: {TEXT_SECONDARY};
}}

QTabBar::tab:selected {{
    background-color: {BG_CARD};
    color: {ACCENT_COLOR};
    border-bottom: 2px solid {ACCENT_COLOR};
}}

QTabBar::tab:hover {{
    color: {TEXT_PRIMARY};
}}

/* 滚动条 - 现代纤细风格 (参考 PyQtClient) */
QScrollBar:vertical {{
    background-color: transparent;
    width: 8px;
    border: none;
    border-radius: 4px;
    margin: 4px 0;
}}

QScrollBar::handle:vertical {{
    background-color: {BORDER_HIGHLIGHT};
    border-radius: 4px;
    min-height: 24px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {ACCENT_COLOR};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background-color: transparent;
    height: 8px;
    border: none;
    border-radius: 4px;
}}

QScrollBar::handle:horizontal {{
    background-color: {BORDER_HIGHLIGHT};
    border-radius: 4px;
}}

QScrollBar::handle:horizontal:hover {{
    background-color: {ACCENT_COLOR};
}}

/* 分割线 */
QFrame[frameShape="4"] {{
    color: {BORDER_COLOR};
    max-height: 1px;
}}

/* 工具提示 - 现代风格 */
QToolTip {{
    background-color: #1e293b;
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_HIGHLIGHT};
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 12px;
}}

/* 组合框 - 现代风格 */
QComboBox {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER_HIGHLIGHT};
    border-radius: 8px;
    padding: 8px 14px;
    color: {TEXT_PRIMARY};
    font-size: 13px;
    min-height: 16px;
}}

QComboBox:hover {{
    border-color: {ACCENT_COLOR}66;
}}

QComboBox:focus {{
    border-color: {ACCENT_COLOR};
}}

QComboBox::drop-down {{
    border: none;
    width: 28px;
    subcontrol-origin: padding;
    subcontrol-position: top right;
}}

QComboBox::down-arrow {{
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid {TEXT_SECONDARY};
    margin-right: 10px;
}}

QComboBox:hover::down-arrow {{
    border-top-color: {ACCENT_COLOR};
}}

QComboBox QAbstractItemView {{
    background-color: {BG_CARD};
    border: 1.5px solid {BORDER_COLOR};
    border-radius: 6px;
    selection-background-color: {ACCENT_COLOR};
    selection-color: white;
    outline: none;
    padding: 4px 0;
    color: {TEXT_PRIMARY};
}}

QComboBox QAbstractItemView::item {{
    padding: 6px 14px;
    min-height: 24px;
}}

QComboBox QAbstractItemView::item:hover {{
    background-color: {BG_INPUT};
}}

/* 分组框 - 卡片风格 */
QGroupBox {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_HIGHLIGHT};
    border-radius: 12px;
    margin-top: 14px;
    padding: 22px 14px 14px 14px;
    font-weight: bold;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 2px 12px;
    color: {ACCENT_COLOR};
    font-weight: bold;
    font-size: 13px;
    background-color: {BG_CARD};
    border-radius: 6px;
}}

/* 分割条 - 细腻风格 */
QSplitter::handle {{
    background-color: {BORDER_HIGHLIGHT};
    width: 2px;
    margin: 12px 0;
    border-radius: 1px;
}}

QSplitter::handle:hover {{
    background-color: {ACCENT_COLOR};
}}

/* 表单布局标签 */
QLabel {{
    color: {TEXT_PRIMARY};
    background: transparent;
    min-height: 16px;
}}

/* QFormLayout 标签自适应 */
QFormLayout QLabel {{
    font-size: 12px;
    white-space: nowrap;
}}
"""


LIGHT_THEME = f"""
/* 浅色主题 - 保留以备切换 */
QWidget {{
    background-color: #f5f7fa;
    color: #303133;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}}

QLineEdit {{
    background-color: white;
    border: 1px solid #dcdfe6;
    border-radius: 6px;
    padding: 8px 12px;
    color: #303133;
}}

QPushButton {{
    background-color: white;
    border: 1px solid #dcdfe6;
    border-radius: 6px;
    padding: 8px 20px;
    color: #606266;
}}

QPushButton#primaryBtn {{
    background-color: {ACCENT_COLOR};
    border-color: {ACCENT_COLOR};
    color: white;
}}
"""
