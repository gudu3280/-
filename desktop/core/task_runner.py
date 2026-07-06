"""
任务调度器 - 异步任务管理，支持暂停/继续/进度上报

通过 asyncio.Event 实现暂停/继续控制，
通过回调函数向 GUI 层报告进度和日志。
"""

import asyncio
import logging
import random
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum

from .browser import BrowserManager
from .chaoxing import ChaoxingOperator
from .config import Config

logger = logging.getLogger(__name__)


class TaskState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class TaskResult:
    """任务执行结果汇总"""
    total_chapters: int = 0
    completed_chapters: int = 0
    total_quizzes: int = 0
    total_videos: int = 0
    total_errors: int = 0
    total_homeworks: int = 0
    total_exams: int = 0
    chapter_results: List[Dict[str, Any]] = field(default_factory=list)
    completed_urls: List[str] = field(default_factory=list)  # 成功完成的章节URL


class TaskRunner:
    """
    任务调度器

    管理整个自动化流程:
    1. 遍历选定的章节
    2. 每个章节调用 ChaoxingOperator.process_chapter()
    3. 支持暂停/继续/停止控制
    4. 通过回调向 GUI 报告进度
    """

    def __init__(
        self,
        browser: BrowserManager,
        config: Config = None,
        force_all: bool = False,
    ):
        self._browser = browser
        self._config = config or Config()
        self._operator = ChaoxingOperator(self._config)
        self._force_all = force_all  # True = 忽略 finished 标记，全部重新执行

        self._state = TaskState.IDLE
        self._pause_event: Optional[asyncio.Event] = None  # 延迟创建，绑定到运行时事件循环
        self._stop_requested = False

        # 回调函数
        self._on_progress: Optional[Callable] = None
        self._on_log: Optional[Callable] = None
        self._on_state_change: Optional[Callable] = None
        self._on_complete: Optional[Callable] = None
        self._on_chapter_done: Optional[Callable] = None  # 单个章节完成时实时回调 (url)

        self._current_task: Optional[asyncio.Task] = None

    @property
    def state(self) -> TaskState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state == TaskState.RUNNING

    @property
    def is_paused(self) -> bool:
        return self._state == TaskState.PAUSED

    def set_callbacks(
        self,
        on_progress: Callable = None,
        on_log: Callable = None,
        on_state_change: Callable = None,
        on_complete: Callable = None,
        on_chapter_done: Callable = None,
    ):
        """设置回调函数"""
        self._on_progress = on_progress
        self._on_log = on_log
        self._on_state_change = on_state_change
        self._on_complete = on_complete
        self._on_chapter_done = on_chapter_done

    def _emit_log(self, message: str, level: str = "info"):
        """发送日志"""
        if self._on_log:
            self._on_log(message, level)
        logger.info(f"[{level}] {message}")

    def _emit_progress(self, task_name: str, current: int, total: int, status: str = ""):
        """发送进度"""
        if self._on_progress:
            self._on_progress(task_name, current, total, status)

    def _set_state(self, state: TaskState):
        """更新状态"""
        self._state = state
        if self._on_state_change:
            self._on_state_change(state)

    async def _check_pause(self):
        """检查是否需要暂停"""
        if not self._pause_event.is_set():
            self._emit_log("任务已暂停，等待继续...", "info")
            await self._pause_event.wait()
            self._emit_log("任务继续执行", "success")

    def _check_stop(self) -> bool:
        """检查是否请求停止"""
        return self._stop_requested

    def _ensure_pause_event(self):
        """确保 pause_event 在当前运行的事件循环中创建"""
        loop = asyncio.get_running_loop()
        if self._pause_event is None:
            self._pause_event = asyncio.Event()
            self._pause_event.set()
        return self._pause_event

    # ======================== 控制接口 ========================

    async def start(self, chapter_data):
        """
        启动任务执行

        Args:
            chapter_data: 章节数据列表
                支持多种格式:
                - [{"url": "...", "name": "...", "course": "...", "task_type": "chapter|homework|exam"}, ...]
                - [{"url": "...", "name": "...", "course": "..."}, ...]  (默认为 chapter)
                - ["url1", "url2", ...] (兼容旧格式)
        """
        if self._state == TaskState.RUNNING:
            self._emit_log("任务已在运行中", "warning")
            return

        # 统一转换为标准格式
        normalized = []
        for item in chapter_data:
            if isinstance(item, str):
                normalized.append({"url": item, "name": item[:40], "course": "", "task_type": "chapter"})
            elif isinstance(item, dict):
                normalized.append({
                    "url": item.get("url", ""),
                    "name": item.get("name", "章节"),
                    "course": item.get("course", ""),
                    "task_type": item.get("task_type", "chapter"),
                })

        self._stop_requested = False
        # pause_event 在 _run_chapters 中创建，确保绑定到正确的事件循环
        self._pause_event = None
        self._current_task = asyncio.create_task(self._run_chapters(normalized))

    def pause(self):
        """暂停任务"""
        if self._state == TaskState.RUNNING and self._pause_event:
            self._pause_event.clear()
            self._set_state(TaskState.PAUSED)
            self._emit_log("收到暂停指令", "info")

    def resume(self):
        """继续任务"""
        if self._state == TaskState.PAUSED and self._pause_event:
            self._pause_event.set()
            self._set_state(TaskState.RUNNING)
            self._emit_log("收到继续指令", "info")

    def stop(self):
        """停止任务"""
        self._stop_requested = True
        if self._pause_event:
            self._pause_event.set()  # 解除暂停以允许退出
        self._emit_log("收到停止指令，正在中止...", "warning")

    # ======================== 核心执行逻辑 ========================

    async def _run_chapters(self, chapter_data: List[Dict[str, str]]):
        """批量执行章节/作业/考试任务"""
        total = len(chapter_data)
        result = TaskResult(total_chapters=total)
        self._set_state(TaskState.RUNNING)
        # 在当前运行的事件循环中创建 pause_event，避免跨循环问题
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._emit_log(f"开始执行 {total} 个任务")

        try:
            page = self._browser.tab  # zendriver Tab
            browser = self._browser._browser  # zendriver Browser
            if not page:
                self._emit_log("浏览器未就绪", "error")
                self._set_state(TaskState.ERROR)
                return

            skipped_count = 0
            last_course = ""

            for i, ch in enumerate(chapter_data):
                # 检查停止
                if self._check_stop():
                    self._emit_log("用户停止任务", "warning")
                    self._set_state(TaskState.STOPPED)
                    break

                # 检查暂停
                await self._check_pause()

                url = ch.get("url", "")
                name = ch.get("name", f"任务{i+1}")
                course = ch.get("course", "")
                task_type = ch.get("task_type", "chapter")
                display_name = name[:30]

                # 课程切换时显示课程名
                if course and course != last_course:
                    self._emit_log(f"--- 课程: {course} ---", "info")
                    last_course = course

                progress_pct = int((i + 1) / total * 100)
                type_label = {"chapter": "章节", "homework": "作业", "exam": "考试"}.get(task_type, "任务")
                self._emit_progress(
                    f"[{type_label}] {display_name}", i + 1, total,
                    f"{progress_pct}% ({i+1}/{total})"
                )
                self._emit_log(f"[{i+1}/{total}] [{type_label}] {display_name}")

                try:
                    if task_type == "homework" and self._config.auto_homework:
                        chapter_result = await self._operator.process_homework(
                            tab=page,
                            homework_url=url,
                            browser=browser,
                            on_log=self._emit_log,
                            pause_event=self._pause_event,
                            stop_check=self._check_stop,
                        )
                        result.total_homeworks += chapter_result.get("quizzes", 0)
                    elif task_type == "homework" and not self._config.auto_homework:
                        self._emit_log(f"[{i+1}/{total}] 作业自动处理未开启，已跳过", "warning")
                        chapter_result = {"skipped": True, "quizzes": 0, "videos": 0, "errors": 0}
                        skipped_count += 1
                    elif task_type == "exam" and self._config.auto_exam:
                        chapter_result = await self._operator.process_exam(
                            tab=page,
                            exam_url=url,
                            browser=browser,
                            on_log=self._emit_log,
                            pause_event=self._pause_event,
                            stop_check=self._check_stop,
                        )
                        result.total_exams += chapter_result.get("quizzes", 0)
                    elif task_type == "exam" and not self._config.auto_exam:
                        self._emit_log(f"[{i+1}/{total}] 考试自动处理未开启，已跳过", "warning")
                        chapter_result = {"skipped": True, "quizzes": 0, "videos": 0, "errors": 0}
                        skipped_count += 1
                    else:
                        # 默认: 章节处理 (视频+测验+PDF)
                        chapter_result = await self._operator.process_chapter(
                            tab=page,
                            chapter_url=url,
                            browser=browser,
                            on_progress=lambda tname, cur, tot: self._emit_progress(
                                f"{display_name} - {tname}", cur, tot
                            ),
                            on_log=self._emit_log,
                            pause_event=self._pause_event,
                            stop_check=self._check_stop,
                            force_all=self._force_all,
                        )

                    result.chapter_results.append(chapter_result)
                    result.completed_chapters += 1
                    # 仅对章节类型累加测验/视频数，避免与作业/考试重复计数
                    if task_type == "chapter":
                        result.total_quizzes += chapter_result.get("quizzes", 0)
                        result.total_videos += chapter_result.get("videos", 0)
                    result.total_errors += chapter_result.get("errors", 0)

                    if chapter_result.get("skipped"):
                        skipped_count += 1
                        # skipped + success = 全部任务点已完成，无需重复处理
                        if chapter_result.get("success"):
                            self._emit_log(
                                f"[{i+1}/{total}] {display_name} - 已全部完成，跳过", "success"
                            )
                            if url:
                                result.completed_urls.append(url)
                                if self._on_chapter_done:
                                    self._on_chapter_done(url)
                        else:
                            self._emit_log(f"[{i+1}/{total}] {display_name} - 已跳过", "info")
                    elif chapter_result.get("success"):
                        self._emit_log(
                            f"[{i+1}/{total}] {display_name} - 完成 "
                            f"(测验:{chapter_result.get('quizzes', 0)} "
                            f"视频:{chapter_result.get('videos', 0)})",
                            "success",
                        )
                        if url:
                            result.completed_urls.append(url)
                            # 实时通知UI：该章节已完成
                            if self._on_chapter_done:
                                self._on_chapter_done(url)
                    else:
                        self._emit_log(f"[{i+1}/{total}] {display_name} - 有错误", "error")

                except Exception as e:
                    self._emit_log(f"[{i+1}/{total}] {display_name} - 异常: {e}", "error")
                    result.total_errors += 1

                # 章节间延迟（侧边栏切换模式下可缩短）
                if i < total - 1 and not self._check_stop():
                    delay = random.uniform(0.5, 1.2)
                    await asyncio.sleep(delay)

            # 完成
            if not self._check_stop():
                self._set_state(TaskState.COMPLETED)
                summary = (
                    f"全部完成! "
                    f"处理 {result.completed_chapters}/{total} 个任务, "
                    f"跳过 {skipped_count} 个, "
                    f"章节测验 {result.total_quizzes} 个, "
                    f"视频 {result.total_videos} 个"
                )
                if result.total_homeworks:
                    summary += f", 作业 {result.total_homeworks} 个"
                if result.total_exams:
                    summary += f", 考试 {result.total_exams} 个"
                summary += f", 错误 {result.total_errors} 个"
                self._emit_log(summary, "success")
            else:
                self._set_state(TaskState.STOPPED)
                self._emit_log(
                    f"任务已停止。已完成 {result.completed_chapters}/{total} 个章节",
                    "warning",
                )

        except Exception as e:
            logger.error(f"任务执行器顶层异常: {e}", exc_info=True)
            self._emit_log(f"执行器异常退出: {e}", "error")
            self._set_state(TaskState.ERROR)

        finally:
            if self._on_complete:
                self._on_complete(result)
