"""
章节完成状态持久化 — SQLite 存储

替代 JSON 文件，提供更可靠的持久化和查询能力。
"""

import logging
import sqlite3
from pathlib import Path
from typing import Set

from .config import DATA_DIR

logger = logging.getLogger(__name__)


class CompletionDB:
    """
    SQLite 持久化管理器

    表结构:
        completed_chapters (
            chapter_key  TEXT PRIMARY KEY,  -- "courseid:knowledgeid"
            completed_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """

    def __init__(self, db_path: Path = None):
        self._db_path = db_path or (DATA_DIR / "completion.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_table()

    def _init_table(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS completed_chapters (
                chapter_key  TEXT PRIMARY KEY,
                completed_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        self._conn.commit()

    # ----------------------------------------------------------
    # 写入
    # ----------------------------------------------------------

    def add(self, key: str):
        """添加/更新一条完成记录"""
        if not key:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO completed_chapters (chapter_key) VALUES (?)",
            (key,),
        )
        self._conn.commit()

    def add_many(self, keys):
        """批量添加"""
        valid = [k for k in keys if k]
        if not valid:
            return
        self._conn.executemany(
            "INSERT OR REPLACE INTO completed_chapters (chapter_key) VALUES (?)",
            [(k,) for k in valid],
        )
        self._conn.commit()

    # ----------------------------------------------------------
    # 删除
    # ----------------------------------------------------------

    def remove(self, key: str):
        """删除一条记录"""
        if not key:
            return
        self._conn.execute(
            "DELETE FROM completed_chapters WHERE chapter_key = ?", (key,)
        )
        self._conn.commit()

    def remove_many(self, keys):
        """批量删除"""
        placeholders = ",".join("?" for _ in keys)
        self._conn.execute(
            f"DELETE FROM completed_chapters WHERE chapter_key IN ({placeholders})",
            list(keys),
        )
        self._conn.commit()

    def clear_all(self):
        """清空全部记录"""
        self._conn.execute("DELETE FROM completed_chapters")
        self._conn.commit()

    # ----------------------------------------------------------
    # 查询
    # ----------------------------------------------------------

    def get_all_keys(self) -> Set[str]:
        """获取所有已完成的 key 集合"""
        cursor = self._conn.execute("SELECT chapter_key FROM completed_chapters")
        return {row[0] for row in cursor.fetchall()}

    def count(self) -> int:
        """获取已完成记录数"""
        cursor = self._conn.execute("SELECT COUNT(*) FROM completed_chapters")
        return cursor.fetchone()[0]

    def has(self, key: str) -> bool:
        """判断某个 key 是否已完成"""
        cursor = self._conn.execute(
            "SELECT 1 FROM completed_chapters WHERE chapter_key = ?", (key,)
        )
        return cursor.fetchone() is not None

    # ----------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------

    def close(self):
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None

    def switch_account(self, account_id: str = None):
        """
        切换到指定账号的完成状态数据库。
        每个账号使用独立的 SQLite 文件，实现数据隔离。
        
        Args:
            account_id: 账号ID，为 None 时使用全局默认库
        """
        if account_id:
            from .config import AccountManager
            acc_dir = AccountManager().get_account_data_dir(account_id)
            new_path = acc_dir.parent / "completion.db"  # accounts/{id}/completion.db
        else:
            new_path = DATA_DIR / "completion.db"

        if new_path == self._db_path:
            return  # 路径相同，无需切换

        # 关闭旧连接，打开新库
        self.close()
        self._db_path = new_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_table()
        logger.info(f"CompletionDB 已切换到: {new_path}")
