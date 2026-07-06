# 超星助手

超星学习通桌面自动化助手，基于 Python + PyQt5 + zendriver 开发，支持自动刷课、AI 答题、视频播放、多账号管理等功能。

基于 [ocsjs (OCS 网课助手)](https://github.com/ocsjs/ocsjs) v4.15.3 的核心逻辑移植开发。

## 功能特性

- **自动刷课**：自动遍历章节，完成视频、音频、文档等任务点
- **AI 答题**：接入 DeepSeek-reasoner 大模型，自动解析并填写选择题、填空题、判断题
- **视频播放**：自动静音播放，支持最小化窗口，内置卡死检测与自动恢复
- **多账号管理**：支持多账号会话隔离，一键切换，数据独立存储
- **验证码识别**：自动识别并提交超星验证码
- **悬浮球控制**：运行时可通过悬浮球暂停/继续/停止任务
- **自动更新**：支持从 GitHub 检查并下载新版本

## 技术栈

- **GUI**：PyQt5
- **浏览器自动化**：zendriver（基于 CDP 协议，无 webdriver 特征）
- **异步框架**：asyncio + qasync
- **AI 模型**：DeepSeek-reasoner
- **OCR**：ddddocr（验证码识别）
- **打包**：PyInstaller

## 快速开始

### 环境要求

- Python 3.10+
- Windows 10/11

### 安装

```bash
cd desktop

# 创建虚拟环境
python -m venv venv
venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 运行

```bash
python main.py
```

### 打包

```bash
python build.py          # 打包为目录
python build.py --onefile # 打包为单文件
```

打包产物位于 `desktop/dist/超星助手/`

## 使用方法

1. 启动程序，在配置面板填入 DeepSeek API Key
2. 点击「启动浏览器」，在弹出的 Chrome 中登录超星学习通
3. 登录成功后自动加载课程列表
4. 勾选需要完成的章节，点击「开始执行」
5. 程序将自动遍历章节并完成所有任务点

### 多账号

- 在账号管理区域可添加多个账号
- 每个账号的浏览器数据、完成状态独立存储
- 点击账号卡片上的「启动」按钮切换账号

## 项目结构

```
desktop/
├── core/                  # 核心逻辑
│   ├── browser.py         # 浏览器生命周期管理
│   ├── chaoxing.py        # 超星平台操作（导航、答题、视频）
│   ├── answer_engine.py   # AI 答题引擎
│   ├── task_runner.py     # 任务调度器
│   ├── completion_db.py   # 完成状态持久化（SQLite）
│   ├── config.py          # 配置管理
│   ├── font_decrypt.py    # 字体解密
│   └── updater.py         # 自动更新
├── ui/                    # 界面
│   ├── main_window.py     # 主窗口
│   ├── login_window.py    # 登录窗口
│   ├── floating_ball.py   # 悬浮球组件
│   ├── styles.py          # 样式定义
│   └── widgets.py         # 自定义控件
├── assets/                # 静态资源
├── main.py                # 程序入口
├── build.py               # 打包脚本
├── version.py             # 版本号
└── requirements.txt       # 依赖列表
```

## 致谢

本项目基于 [ocsjs (OCS 网课助手)](https://github.com/ocsjs/ocsjs) v4.15.3 的核心逻辑移植开发，感谢 ocsjs 团队的开源贡献。

## 免责声明

1. 本项目仅供**学习交流与技术研究**使用，严禁用于任何商业用途或违反平台服务条款的行为。
2. 使用本工具产生的一切后果由使用者自行承担，开发者不承担任何责任。
3. 本项目不保证功能的持续可用性，超星学习通平台可能更新反作弊机制导致工具失效。
4. 请遵守超星学习通平台的用户协议及相关法律法规，合理使用本工具。
