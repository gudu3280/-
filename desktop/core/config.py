"""
配置管理模块 - 基于 python-dotenv 的环境变量管理
"""

import os
import json
import threading
import uuid
import shutil
from datetime import datetime
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv, set_key

# 获取 desktop 目录的绝对路径
DESKTOP_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = DESKTOP_DIR / ".env"
DATA_DIR = DESKTOP_DIR / "data"

# 确保数据目录存在
DATA_DIR.mkdir(exist_ok=True)

# 加载环境变量
load_dotenv(ENV_FILE)


class Config:
    """应用配置单例类（线程安全）"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._loaded = False
        return cls._instance

    def __init__(self):
        if self._loaded:
            return
        self._loaded = True
        self.reload()

    def reload(self):
        """重新加载配置"""
        load_dotenv(ENV_FILE, override=True)

        # DeepSeek API
        self.deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "")
        self.deepseek_api_url = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions")
        self.deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

        # 题库服务器（单题库，兼容旧版）
        self.tiku_api_url = os.getenv("TIKU_API_URL", "")
        self.tiku_api_key = os.getenv("TIKU_API_KEY", "")

        # 多题库配置（JSON数组，OCS 风格）
        tiku_servers_str = os.getenv("TIKU_SERVERS", "")
        self.tiku_servers: list = []
        if tiku_servers_str:
            try:
                self.tiku_servers = json.loads(tiku_servers_str)
            except json.JSONDecodeError:
                self.tiku_servers = []
        # 如果没有配置多题库但配置了单题库，自动转换为多题库格式
        if not self.tiku_servers and self.tiku_api_url:
            self.tiku_servers = [{
                "name": "默认题库",
                "url": self.tiku_api_url,
                "method": "POST",
                "key": self.tiku_api_key,
            }]

        # 学习通账号
        self.chaoxing_username = os.getenv("CHAOXING_USERNAME", "")
        self.chaoxing_password = os.getenv("CHAOXING_PASSWORD", "")

        # 自动化参数
        self.answer_interval_min = int(os.getenv("ANSWER_INTERVAL_MIN", "2"))
        self.answer_interval_max = int(os.getenv("ANSWER_INTERVAL_MAX", "5"))
        self.submit_delay_min = int(os.getenv("SUBMIT_DELAY_MIN", "3"))
        self.submit_delay_max = int(os.getenv("SUBMIT_DELAY_MAX", "8"))
        self.min_accuracy = float(os.getenv("MIN_ACCURACY", "0.8"))
        self.auto_submit = os.getenv("AUTO_SUBMIT", "true").lower() == "true"
        self.auto_video = os.getenv("AUTO_VIDEO", "true").lower() == "true"
        self.video_speed = float(os.getenv("VIDEO_SPEED", "1"))
        self.auto_jump = os.getenv("AUTO_JUMP", "true").lower() == "true"
        self.auto_homework = os.getenv("AUTO_HOMEWORK", "true").lower() == "true"
        self.auto_exam = os.getenv("AUTO_EXAM", "false").lower() == "true"

        # 答题引擎配置（OCS 迁移）
        sep_str = os.getenv("ANSWER_SEPARATORS", "===,#,---,###,|,;,；")
        self.answer_separators = [s for s in sep_str.split(",") if s.strip()]
        upload_type = os.getenv("UPLOAD_TYPE", "80")
        try:
            self.upload_type = int(upload_type)
        except ValueError:
            self.upload_type = upload_type  # "save" | "nomove" | "force"
        self.worker_threads = max(1, int(os.getenv("WORKER_THREADS", "1")))
        self.random_answer = os.getenv("RANDOM_ANSWER", "false").lower() == "true"

        # 答案缓存
        self.cache_enabled = os.getenv("CACHE_ENABLED", "true").lower() == "true"
        self.cache_max_size = int(os.getenv("CACHE_MAX_SIZE", "200"))

        # OCS 迁移：学习增强配置
        self.video_volume = int(os.getenv("VIDEO_VOLUME", "0"))  # 0-100, 0=静音
        self.review_mode = os.getenv("REVIEW_MODE", "false").lower() == "true"  # 复习模式
        self.force_study = os.getenv("FORCE_STUDY", "false").lower() == "true"  # 强制学习非任务点
        self.search_timeout = int(os.getenv("SEARCH_TIMEOUT", "30"))  # 搜题最大耗时（秒）
        self.answer_pause = int(os.getenv("ANSWER_PAUSE", "0"))  # 答题结束后暂停（秒）

        # 多平台支持
        self.active_platform = os.getenv("ACTIVE_PLATFORM", "chaoxing")

        # 悬浮球
        self.floating_ball_enabled = os.getenv("FLOATING_BALL_ENABLED", "true").lower() == "true"
        self.floating_ball_size = int(os.getenv("FLOATING_BALL_SIZE", "60"))
        self.floating_ball_opacity = float(os.getenv("FLOATING_BALL_OPACITY", "0.9"))

        # 系统通知
        self.notification_enabled = os.getenv("NOTIFICATION_ENABLED", "true").lower() == "true"
        self.webhook_url = os.getenv("WEBHOOK_URL", "")

        # 更新配置
        self.github_repo = os.getenv("GITHUB_REPO", "")
        self.auto_check_update = os.getenv("AUTO_CHECK_UPDATE", "true").lower() == "true"

    def save(self, **kwargs):
        """保存配置到 .env 文件"""
        for key, value in kwargs.items():
            env_key = key.upper()
            if isinstance(value, bool):
                value = "true" if value else "false"
            set_key(str(ENV_FILE), env_key, str(value))
        self.reload()

    def save_credentials(self, username: str, password: str):
        """保存学习通凭据"""
        self.save(chaoxing_username=username, chaoxing_password=password)

    def save_deepseek_config(self, api_key: str, model: str = None, api_url: str = None):
        """保存 DeepSeek 配置"""
        updates = {"DEEPSEEK_API_KEY": api_key}
        if model:
            updates["DEEPSEEK_MODEL"] = model
        if api_url:
            updates["DEEPSEEK_API_URL"] = api_url
        self.save(**updates)

    def get_user_data_dir(self) -> Path:
        """获取浏览器用户数据目录(用于保存session)"""
        path = DATA_DIR / "browser_data"
        path.mkdir(exist_ok=True)
        return path

    def get_ttf_table_path(self) -> Path:
        """获取字体映射表路径"""
        return DESKTOP_DIR / "assets" / "ttf_table.json"

    def save_tiku_config(self, tiku_url: str, tiku_key: str = ""):
        """保存题库服务器配置"""
        self.save(tiku_api_url=tiku_url, tiku_api_key=tiku_key)

    def save_tiku_servers(self, servers: list):
        """保存多题库配置（JSON格式）"""
        self.save(tiku_servers=json.dumps(servers, ensure_ascii=False))

    @property
    def has_deepseek_config(self) -> bool:
        return bool(self.deepseek_api_key)

    @property
    def has_tiku_config(self) -> bool:
        return bool(self.tiku_api_url or self.tiku_servers)

    @property
    def has_tiku_servers(self) -> bool:
        """是否有配置多题库"""
        return bool(self.tiku_servers)

    @property
    def has_credentials(self) -> bool:
        return bool(self.chaoxing_username and self.chaoxing_password)

    @property
    def has_saved_session(self) -> bool:
        """检查是否有保存的浏览器session"""
        session_file = self.get_user_data_dir() / "Default" / "Cookies"
        return session_file.exists()


# ============================================================
# 多账号管理器
# ============================================================

class AccountManager:
    """
    多账号管理器 - 管理账号元数据和会话目录

    每个账号拥有独立的 Chrome user_data_dir，实现会话隔离。
    不保存明文密码，仅依赖浏览器 cookies/session 恢复登录态。
    """

    ACCOUNTS_FILE = DATA_DIR / "accounts.json"
    ACCOUNTS_DIR = DATA_DIR / "accounts"

    def __init__(self):
        self.ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        """从 accounts.json 加载数据"""
        if self.ACCOUNTS_FILE.exists():
            try:
                with open(self.ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                import logging
                logging.getLogger(__name__).warning(f"加载账号数据失败: {e}")
        return {"active_account": None, "accounts": []}

    def _save(self):
        """保存数据到 accounts.json"""
        try:
            with open(self.ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            import logging
            logging.getLogger(__name__).error(f"保存账号数据失败: {e}")

    def list_accounts(self) -> list:
        """返回账号列表（按最后登录时间倒序）"""
        accounts = self._data.get("accounts", [])
        return sorted(accounts, key=lambda a: a.get("last_login", ""), reverse=True)

    def get_active_account_id(self) -> Optional[str]:
        """获取当前激活的账号 ID"""
        return self._data.get("active_account")

    def get_account(self, account_id: str) -> Optional[dict]:
        """根据 ID 获取账号"""
        for acc in self._data.get("accounts", []):
            if acc["id"] == account_id:
                return acc
        return None

    def add_account(self, display_name: str, login_method: str = "password") -> str:
        """
        创建新账号

        Args:
            display_name: 脱敏显示名 (如 138***1234)
            login_method: 登录方式 (password/qrcode/cookie)

        Returns:
            新账号的 UUID
        """
        account_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat(timespec="seconds")
        account = {
            "id": account_id,
            "display_name": display_name,
            "created_at": now,
            "last_login": now,
            "login_method": login_method,
            "has_session": False,
        }
        self._data.setdefault("accounts", []).append(account)

        # 创建账号数据目录
        account_dir = self.ACCOUNTS_DIR / account_id
        (account_dir / "browser_data").mkdir(parents=True, exist_ok=True)

        # 设为激活账号
        self._data["active_account"] = account_id
        self._save()
        return account_id

    def update_account(self, account_id: str, **kwargs):
        """更新账号属性"""
        for acc in self._data.get("accounts", []):
            if acc["id"] == account_id:
                acc.update(kwargs)
                self._save()
                return

    def update_last_login(self, account_id: str):
        """更新最后登录时间"""
        now = datetime.now().isoformat(timespec="seconds")
        self.update_account(account_id, last_login=now)

    def update_display_name(self, account_id: str, display_name: str):
        """更新账号显示名称"""
        self.update_account(account_id, display_name=display_name)

    def set_active_account(self, account_id: str):
        """设置当前激活的账号"""
        self._data["active_account"] = account_id
        self._save()

    def remove_account(self, account_id: str):
        """删除账号及其数据目录"""
        accounts = self._data.get("accounts", [])
        self._data["accounts"] = [a for a in accounts if a["id"] != account_id]

        # 清除激活状态
        if self._data.get("active_account") == account_id:
            remaining = self._data["accounts"]
            self._data["active_account"] = remaining[0]["id"] if remaining else None

        self._save()

        # 删除数据目录
        account_dir = self.ACCOUNTS_DIR / account_id
        if account_dir.exists():
            try:
                shutil.rmtree(account_dir)
            except OSError:
                pass

    def get_account_data_dir(self, account_id: str) -> Path:
        """获取账号的浏览器数据目录"""
        path = self.ACCOUNTS_DIR / account_id / "browser_data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_account_cookies_path(self, account_id: str) -> Path:
        """获取账号的 cookies 备份路径"""
        return self.ACCOUNTS_DIR / account_id / "cookies_backup.json"

    def has_session(self, account_id: str) -> bool:
        """检查账号是否有有效的 session"""
        acc = self.get_account(account_id)
        if acc and acc.get("has_session"):
            return True
        # 检查 Chrome Cookies 文件是否存在（新旧版本位置）
        data_dir = self.get_account_data_dir(account_id)
        cookies_paths = [
            data_dir / "Default" / "Cookies",
            data_dir / "Default" / "Network" / "Cookies",
        ]
        return any(p.exists() for p in cookies_paths)

    def mark_session(self, account_id: str, has_session: bool = True):
        """标记账号的 session 状态"""
        self.update_account(account_id, has_session=has_session)

    @staticmethod
    def mask_username(username: str) -> str:
        """将用户名脱敏为显示名"""
        if not username:
            return "未知账号"
        
        # 手机号处理：138****8888
        if username.isdigit() and len(username) == 11:
            return username[:3] + "****" + username[-4:]
        
        # 短字符串
        if len(username) <= 4:
            return username[:1] + "***"
        
        # 邮箱处理：ab***@qq.com
        if "@" in username:
            local, domain = username.split("@", 1)
            if len(local) <= 2:
                return local[0] + "***@" + domain
            return local[:2] + "***@" + domain
        
        # 普通用户名：abc***xy
        return username[:3] + "***" + username[-2:]

    def migrate_legacy_data(self):
        """
        向后兼容：检测旧的 data/browser_data/ 目录，
        若有有效 cookies 则自动迁移为默认账号。
        """
        legacy_dir = DATA_DIR / "browser_data"
        # 检查多个可能的 Cookies 位置（Chrome 新旧版本）
        legacy_cookies_paths = [
            legacy_dir / "Default" / "Cookies",
            legacy_dir / "Default" / "Network" / "Cookies",
        ]
        has_cookies = any(p.exists() for p in legacy_cookies_paths)
        if not has_cookies:
            return

        # 检查是否已经有账号（避免重复迁移）
        if self._data.get("accounts"):
            return

        import logging
        logger = logging.getLogger(__name__)
        logger.info("检测到旧的浏览器数据，自动迁移为默认账号")

        account_id = self.add_account("默认账号", login_method="legacy")
        target_dir = self.get_account_data_dir(account_id)

        # 移动旧数据到新账号目录
        try:
            if target_dir.exists():
                shutil.rmtree(target_dir)
            shutil.copytree(str(legacy_dir), str(target_dir))
            self.mark_session(account_id, True)
            logger.info(f"已迁移旧数据到账号 {account_id}")
        except OSError as e:
            logger.warning(f"迁移旧数据失败: {e}")
