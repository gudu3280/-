"""
Zendriver浏览器管理器 - 浏览器生命周期管理和页面操作封装

使用 zendriver (基于 CDP) 替代 Playwright，提供更好的反检测能力。
"""

import asyncio
import json
import logging
import os
from typing import Optional, List, Dict, Any
from pathlib import Path

import zendriver as zd
from zendriver import cdp

from .config import Config

logger = logging.getLogger(__name__)


class FrameCtx:
    """
    iframe 执行上下文封装

    通过 CDP 的 execution context 机制在 iframe 内执行 JS。
    替代 Playwright 的 Frame 对象。
    """

    def __init__(self, tab, context_id: int, url: str = ""):
        self.tab = tab
        self.context_id = context_id
        self.url = url


class BrowserManager:
    """
    Zendriver 浏览器生命周期管理

    使用本地 Chrome + CDP 协议实现:
    - 无 webdriver 特征，不易被检测
    - 首次登录后保存 cookies/session 到磁盘
    - 后续启动自动恢复登录状态
    - 支持多账号会话隔离
    """

    def __init__(self, config: Config = None):
        self._config = config or Config()
        self._browser: Optional[zd.Browser] = None
        self._tab: Optional[zd.Tab] = None
        self._started = False
        self._account_id: Optional[str] = None  # 当前使用的账号 ID
        # iframe 执行上下文缓存: {partial_url_or_index: context_id}
        self._ctx_cache: Dict[str, int] = {}
        self._on_navigation_callback = None  # 页面导航回调

    @property
    def tab(self) -> Optional[zd.Tab]:
        return self._tab

    @property
    def page(self) -> Optional[zd.Tab]:
        """兼容旧接口"""
        return self._tab

    @property
    def is_started(self) -> bool:
        """
        浏览器是否已启动。
        增加存活检测：如果 Chrome 进程已退出，自动重置状态。
        """
        if not self._started:
            return False
        # 检查浏览器进程是否还在运行
        if self._browser and not self._is_browser_process_alive():
            logger.warning("浏览器进程已退出，自动重置状态")
            self.force_reset()
            return False
        return True

    def _is_browser_process_alive(self) -> bool:
        """检查 Chrome 浏览器进程是否仍在运行"""
        if not self._browser:
            return False
        try:
            # zendriver 的 Browser 对象内部有 process 属性
            proc = getattr(self._browser, 'process', None)
            if proc:
                return proc.poll() is None  # poll() 返回 None 表示进程仍在运行
            # 如果没有 process 属性，尝试通过 tab 检查
            if self._tab:
                return True  # 有 tab 对象，假定浏览器还在
            return False
        except Exception:
            return False

    @property
    def account_id(self) -> Optional[str]:
        return self._account_id

    def set_account(self, account_id: Optional[str]):
        """设置当前使用的账号 ID（须在 start() 之前调用）"""
        self._account_id = account_id

    def set_on_navigation_callback(self, callback):
        """
        设置页面导航回调
        
        callback: async def callback(url: str) - 页面导航时触发
        """
        self._on_navigation_callback = callback

    def _get_user_data_dir(self) -> str:
        """根据当前账号获取用户数据目录"""
        if self._account_id:
            from .config import AccountManager
            return str(AccountManager().get_account_data_dir(self._account_id))
        return str(self._config.get_user_data_dir())

    @staticmethod
    def _find_local_chrome() -> Optional[str]:
        """查找本地 Chrome 浏览器路径"""
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        return None

    async def start(self, headless: bool = False) -> zd.Tab:
        """
        启动浏览器 (zendriver + 本地 Chrome)

        Args:
            headless: 是否无头模式

        Returns:
            主标签页实例
        """
        if self._started and self._is_browser_process_alive():
            logger.warning("浏览器已在运行中")
            return self._tab

        # 启动前清理残留进程（解决“关闭浏览器后再次启动卡死”的问题）
        if self._browser:
            logger.info("启动前检测到残留浏览器对象，清理中...")
            self._kill_browser_process()
            self._browser = None
            self._tab = None
            self._started = False

        chrome_path = self._find_local_chrome()
        user_data_dir = self._get_user_data_dir()
        logger.info(f"用户数据目录: {user_data_dir}")

        if chrome_path:
            logger.info(f"使用本地 Chrome: {chrome_path}")
        else:
            logger.info("未找到本地 Chrome，使用 zendriver 内置 Chromium")

        try:
            self._browser = await zd.start(
                headless=headless,
                user_data_dir=user_data_dir,
                browser_executable_path=chrome_path,
                browser_args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--disable-extensions",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-popup-blocking",
                    "--disable-default-apps",
                    "--lang=zh-CN",
                    "--restore-last-session",
                    # 禁止后台限制：窗口最小化时视频仍可正常加载和播放
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                ],
            )
        except Exception as e:
            raise RuntimeError(f"浏览器启动失败: {e}") from e

        # 获取主标签页
        tabs = self._browser.tabs if hasattr(self._browser, 'tabs') else []
        if tabs:
            self._tab = tabs[0]
        else:
            self._tab = await self._browser.get("about:blank")

        # 设置 User-Agent
        try:
            await self._tab.send(cdp.network.set_user_agent(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                )
            ))
        except Exception as e:
            logger.debug(f"设置 User-Agent 失败: {e}")

        # ======== 注入反检测 + Flash 伪装脚本 (所有新页面生效) ========
        init_script = """
// 隐藏 webdriver 标志
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// 伪造 chrome runtime
window.chrome = { runtime: {} };
// 隐藏自动化相关属性
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en'],
});

// ======== Flash 检测伪装 ========
window.swfobject = window.swfobject || {};
window.swfobject.hasFlashPlayerVersion = function() { return true; };
window.swfobject.getFlashPlayerVersion = function() {
    return { major: 32, minor: 0, release: 0 };
};
try {
    Object.defineProperty(window, 'FlashPlayerVersion', {
        value: '32.0.0', writable: false
    });
} catch(e) {}
if (!window.ActiveXObject) {
    window.ActiveXObject = function(name) {
        if (name && name.toLowerCase().includes('shockwave')) {
            return { GetVariable: function() { return '32,0,0'; } };
        }
        return null;
    };
}
try {
    const origMimeTypes = navigator.mimeTypes;
    Object.defineProperty(navigator, 'mimeTypes', {
        get: function() {
            const mt = origMimeTypes || [];
            if (!mt['application/x-shockwave-flash']) {
                mt['application/x-shockwave-flash'] = {
                    type: 'application/x-shockwave-flash',
                    description: 'Shockwave Flash',
                    suffixes: 'swf'
                };
            }
            return mt;
        }
    });
} catch(e) {}

// ======== 视频模块 Flash 拦截 ========
var _swf = window.swfobject || {};
_swf.createEmbed = function() { return document.createElement('div'); };
_swf.embedSWF = function() {};
window.swfobject = _swf;
var origWrite = document.write;
var origWriteln = document.writeln;
document.write = function(s) {
    if (s && (s.indexOf('flashplayer') !== -1 || s.indexOf('Flash') !== -1 ||
        s.indexOf('.swf') !== -1 || s.indexOf('shockwave') !== -1)) {
        return;
    }
    return origWrite.apply(document, arguments);
};
document.writeln = function(s) {
    if (s && (s.indexOf('flashplayer') !== -1 || s.indexOf('Flash') !== -1 ||
        s.indexOf('.swf') !== -1 || s.indexOf('shockwave') !== -1)) {
        return;
    }
    return origWriteln.apply(document, arguments);
};
// DOM 加载后隐藏 Flash 错误提示
document.addEventListener('DOMContentLoaded', function() {
    setTimeout(function() {
        var all = document.querySelectorAll('div, p, span, td, font');
        for (var i = 0; i < all.length; i++) {
            var t = (all[i].textContent || '').toLowerCase();
            if (t.indexOf('flashplayer') !== -1 || t.indexOf('安装flash') !== -1) {
                all[i].style.display = 'none';
                var p = all[i].parentElement;
                for (var j = 0; j < 3; j++) {
                    if (p && p.tagName !== 'BODY') { p.style.display = 'none'; p = p.parentElement; }
                }
            }
        }
    }, 500);
});

// ======== 禁止后台暂停: 全面封死所有可见性检测 ========
// 让页面始终认为自己可见且有焦点，彻底防止视频播放器在最小化时暂停
Object.defineProperty(document, 'hidden', { get: function() { return false; } });
Object.defineProperty(document, 'visibilityState', { get: function() { return 'visible'; } });
Object.defineProperty(document, 'webkitHidden', { get: function() { return false; } });
Object.defineProperty(document, 'webkitVisibilityState', { get: function() { return 'visible'; } });
Object.defineProperty(document, 'mozHidden', { get: function() { return false; } });
Object.defineProperty(document, 'msHidden', { get: function() { return false; } });
// hasFocus() 始终返回 true，防止播放器通过焦点检测暂停
Document.prototype.hasFocus = function() { return true; };
// 拦截 visibilitychange / blur / focus 事件注册
var _origAddEventListener = EventTarget.prototype.addEventListener;
EventTarget.prototype.addEventListener = function(type, listener, options) {
    if (type === 'visibilitychange' || type === 'webkitvisibilitychange'
        || type === 'mozvisibilitychange' || type === 'msvisibilitychange') {
        return;
    }
    return _origAddEventListener.call(this, type, listener, options);
};
// 防止通过 onvisibilitychange 属性设置监听
Object.defineProperty(document, 'onvisibilitychange', { get: function() { return null; }, set: function() {} });
// window blur/focus 事件伪装
Object.defineProperty(window, 'onblur', { get: function() { return null; }, set: function() {} });
Object.defineProperty(window, 'onfocus', { get: function() { return null; }, set: function() {} });
"""
        try:
            await self._tab.send(cdp.page.add_script_to_evaluate_on_new_document(
                source=init_script
            ))
            logger.info("反检测脚本注入成功")
        except Exception as e:
            logger.warning(f"反检测脚本注入失败: {e}")

        # ======== CDP 浏览器级反限制: 从根本上防止最小化时卡死 ========
        try:
            # 模拟焦点: 让浏览器认为页面始终有焦点
            await self._tab.send(cdp.emulation.set_focus_emulation_enabled(enabled=True))
            logger.info("CDP 焦点模拟已启用")
        except Exception as e:
            logger.debug(f"CDP 焦点模拟设置失败: {e}")
        try:
            # Web 生命周期状态: 强制页面保持 active，浏览器底层不冻结
            await self._tab.send(cdp.page.set_web_lifecycle_state(state="active"))
            logger.info("CDP Web生命周期已锁定为 active")
        except Exception as e:
            logger.debug(f"CDP 生命周期状态设置失败: {e}")

        # ======== 自动关闭 JS 弹窗 ========
        try:
            await self._tab.send(cdp.page.enable())

            async def _auto_dismiss_dialog(event):
                try:
                    await self._tab.send(cdp.page.handle_java_script_dialog(accept=True))
                except Exception:
                    pass

            self._tab.add_handler(
                cdp.page.JavaScriptDialogOpening, _auto_dismiss_dialog
            )
        except Exception as e:
            logger.debug(f"弹窗处理设置失败: {e}")

        # ======== 页面导航监听（用于快速检测登录状态） ========
        try:
            async def _on_frame_navigated(event):
                """页面导航事件回调"""
                if not self._on_navigation_callback:
                    return
                try:
                    # 只监听主框架导航
                    if event.frame and event.frame.parent_id is None:
                        url = event.frame.url
                        if "chaoxing.com" in url:
                            logger.debug(f"检测到页面导航: {url[:80]}")
                            # 异步触发回调（不阻塞事件处理）
                            asyncio.create_task(self._on_navigation_callback(url))
                except Exception as e:
                    logger.debug(f"导航回调处理异常: {e}")

            self._tab.add_handler(
                cdp.page.FrameNavigated, _on_frame_navigated
            )
            logger.debug("页面导航监听已启用")
        except Exception as e:
            logger.debug(f"页面导航监听设置失败: {e}")

        self._started = True
        logger.info("浏览器启动成功 (zendriver)")
        return self._tab

    async def stop(self):
        """关闭浏览器并清理资源"""
        if not self._started:
            return
        logger.info("正在关闭浏览器...")
        try:
            # 关闭前确保持久化存储（cookies/localStorage/sessionStorage）
            await self._persist_session_data()
        except Exception as e:
            logger.debug(f"持久化会话数据异常: {e}")
        try:
            if self._browser:
                await self._browser.stop()
                # 等待 Chrome 进程完全退出并刷盘
                await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"关闭浏览器异常: {e}")
        finally:
            self._browser = None
            self._tab = None
            self._ctx_cache.clear()
            self._started = False
            logger.info("浏览器已关闭")

    def force_reset(self):
        """
        强制重置浏览器状态（用于浏览器被外部关闭后重新启动）。
        不清理会话数据，以便下次启动时能复用已保存的 cookies。
        会尝试终止残留的 Chrome 进程，防止僵尸进程占用资源。
        """
        logger.info("强制重置浏览器状态...")
        # 尝试终止残留的 Chrome 进程
        self._kill_browser_process()
        self._browser = None
        self._tab = None
        self._ctx_cache.clear()
        self._started = False
        self._on_navigation_callback = None

    def _kill_browser_process(self):
        """强制终止 Chrome 浏览器进程（如果仍在运行）"""
        if not self._browser:
            return
        try:
            proc = getattr(self._browser, 'process', None)
            if proc and proc.poll() is None:
                logger.info("检测到残留 Chrome 进程，正在终止...")
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
                logger.info("Chrome 进程已终止")
        except Exception as e:
            logger.debug(f"终止 Chrome 进程异常(可忽略): {e}")

    async def stop_for_restart(self):
        """
        正确关闭浏览器进程以便重新启动。
        与 stop() 不同，不导航到其他页面，直接关闭 Chrome 进程。
        保留会话数据以便下次启动复用 cookies。
        """
        if not self._started:
            self.force_reset()
            return
        logger.info("正在关闭浏览器（为重启准备）...")
        try:
            if self._browser:
                await self._browser.stop()
                await asyncio.sleep(1.5)
        except Exception as e:
            logger.debug(f"关闭浏览器异常: {e}")
        finally:
            self._browser = None
            self._tab = None
            self._ctx_cache.clear()
            self._started = False
            self._on_navigation_callback = None  # 清除导航回调
            logger.info("浏览器已关闭，可重新启动")

    async def _persist_session_data(self):
        """
        关闭浏览器前，主动触发存储持久化。

        通过 CDP Storage.trackUsageAndQuotaForOrigin 和页面导航，
        确保 Chrome 将内存中的 cookies/localStorage/sessionStorage
        写入 user_data_dir 的磁盘文件。
        """
        if not self._tab or not self._browser:
            return
        try:
            # 访问学习通主页，确保 Chrome 将相关存储写入磁盘
            await self._browser.get("https://i.chaoxing.com/base")
            await asyncio.sleep(2)
            # 同时备份 cookies 到 JSON 文件（兆底方案）
            await self._save_cookies_backup()
            logger.debug("会话数据持久化完成")
        except Exception as e:
            logger.debug(f"持久化导航异常: {e}")

    def _get_cookies_backup_path(self) -> Path:
        """Cookies 备份文件路径（根据账号隔离）"""
        if self._account_id:
            from .config import AccountManager
            return AccountManager().get_account_cookies_path(self._account_id)
        return self._config.get_user_data_dir() / "cookies_backup.json"

    async def _save_cookies_backup(self):
        """
        通过 CDP 获取超星相关 cookies 并备份到 JSON 文件。
        兆底方案：当 Chrome 自带的 Cookies SQLite 未正确持久化时，
        可通过此备份手动恢复。
        """
        if not self._tab:
            return
        try:
            all_cookies = await self._tab.send(cdp.network.get_all_cookies())
            # 只保存超星相关域名的 cookies
            chaoxing_domains = {'.chaoxing.com', 'chaoxing.com',
                                '.chaoxing.com.cn', 'chaoxing.com.cn'}
            backup = []
            for c in all_cookies:
                domain = getattr(c, 'domain', '')
                if any(d in domain for d in chaoxing_domains):
                    backup.append({
                        'name': c.name,
                        'value': c.value,
                        'domain': c.domain,
                        'path': c.path,
                        'secure': c.secure,
                        'httpOnly': c.http_only,
                        'expires': c.expires,
                        'sameSite': str(getattr(c, 'same_site', '')),
                    })
            if backup:
                path = self._get_cookies_backup_path()
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(backup, f, ensure_ascii=False, indent=2)
                logger.debug(f"已备份 {len(backup)} 个超星 cookies")
        except Exception as e:
            logger.debug(f"备份 cookies 失败: {e}")

    async def _restore_cookies_backup(self) -> bool:
        """
        从备份文件恢复超星 cookies。

        Returns:
            是否成功恢复
        """
        path = self._get_cookies_backup_path()
        if not path.exists():
            logger.debug(f"cookies 备份文件不存在: {path}")
            return False
        try:
            with open(path, 'r', encoding='utf-8') as f:
                backup = json.load(f)
            if not backup:
                logger.debug(f"cookies 备份文件为空: {path}")
                return False
            logger.info(f"尝试从备份恢复 {len(backup)} 个 cookies...")
            restored = 0
            for c in backup:
                try:
                    # 使用 url 参数让 cookies 更可靠地设置
                    domain = c.get('domain', '')
                    secure = c.get('secure', False)
                    scheme = 'https' if secure else 'http'
                    url = f"{scheme}://{domain.lstrip('.')}{c.get('path', '/')}"
                    await self._tab.send(cdp.network.set_cookie(
                        name=c['name'],
                        value=c['value'],
                        url=url,
                        domain=domain,
                        path=c.get('path', '/'),
                        secure=secure,
                        http_only=c.get('httpOnly', False),
                        expires=c.get('expires', -1),
                    ))
                    restored += 1
                except Exception as e:
                    logger.debug(f"恢复 cookie {c.get('name', '?')} 失败: {e}")
            logger.info(f"从备份恢复了 {restored}/{len(backup)} 个 cookies")
            return restored > 0
        except Exception as e:
            logger.debug(f"恢复 cookies 备份失败: {e}")
            return False

    async def navigate(self, url: str) -> zd.Tab:
        """导航到指定URL"""
        if not self._tab:
            raise RuntimeError("浏览器未启动")
        logger.info(f"导航到: {url}")
        self._tab = await self._browser.get(url)
        self._ctx_cache.clear()
        await self._tab.wait(1)
        return self._tab

    async def navigate_to_chaoxing(self) -> bool:
        """导航到学习通新版互动课程页"""
        if not self._tab:
            raise RuntimeError("浏览器未启动")
        # 会话已在 is_logged_in 中建立，直接导航到新版互动课程页
        url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction"
        try:
            logger.info(f"导航到新版互动课程页: {url}")
            self._tab = await self._browser.get(url)
            self._ctx_cache.clear()
            await self._tab.wait(1)
            return True
        except Exception as e:
            logger.error(f"导航失败: {e}")
            await asyncio.sleep(0.5)
            try:
                self._tab = await self._browser.get(url)
                self._ctx_cache.clear()
                await self._tab.wait(1)
                return True
            except Exception as e2:
                logger.error(f"重试导航也失败: {e2}")
                return False

    async def is_logged_in(self, skip_navigate: bool = False) -> bool:
        """
        检查是否已登录学习通
        
        Args:
            skip_navigate: 如果为 True，不导航到 i.chaoxing.com，
                          仅在当前页面检测登录状态（用于轮询检测时避免打断用户操作）
        """
        if not self._tab:
            return False
        try:
            if skip_navigate:
                # 轻量检测：不导航，只检查当前页面 URL 和元素
                current_url = self._tab.url
                if "chaoxing.com" not in current_url:
                    return False
                try:
                    result = await self._tab.evaluate(
                        "!!document.querySelector('.user-info, .user-name, .head-img, .userName')"
                    )
                    return bool(result)
                except Exception:
                    return False

            # i.chaoxing.com/base 是稳定的通用入口，不会返回 405
            self._tab = await self._browser.get("https://i.chaoxing.com/base")
            self._ctx_cache.clear()
            await self._tab.wait(0.5)

            current_url = self._tab.url
            if "passport" in current_url or "login" in current_url:
                # 未登录 → 尝试从备份文件恢复 cookies
                logger.info(f"页面被重定向到登录页，尝试恢复 cookies...")
                if await self._restore_cookies_backup():
                    logger.info("已从备份恢复 cookies，重新检查登录状态")
                    # 恢复后必须刷新页面，让 cookies 生效
                    self._tab = await self._browser.get("https://i.chaoxing.com/base")
                    self._ctx_cache.clear()
                    await self._tab.wait(1)  # 恢复 cookies 后等待加载
                    current_url = self._tab.url
                    if "passport" not in current_url and "login" not in current_url:
                        logger.info("恢复 cookies 后登录成功")
                        # 恢复成功后立即再次备份（更新过期时间）
                        await self._save_cookies_backup()
                        return True
                    else:
                        logger.info(f"恢复 cookies 后仍被重定向到: {current_url[:60]}")
                else:
                    logger.info("没有可用的 cookies 备份文件")
                logger.info("未检测到登录状态")
                return False

            try:
                result = await self._tab.evaluate(
                    "!!document.querySelector('.user-info, .user-name, .head-img, .userName')"
                )
                if result:
                    logger.info("检测到已登录状态")
                    # 登录成功 → 备份当前 cookies
                    await self._save_cookies_backup()
                    return True
            except Exception:
                pass

            return False
        except Exception as e:
            logger.error(f"检查登录状态异常: {e}")
            return False

    async def get_logged_in_username(self) -> Optional[str]:
        """
        获取当前已登录用户的账号名。
        优先从当前页面提取（快速），仅在必要时才导航到个人中心。

        Returns:
            用户名，如果未登录则返回 None
        """
        if not self._tab:
            return None
        try:
            # 策略1: 先尝试从当前页面直接提取（最快，无导航开销）
            current_url = self._tab.url or ""
            if "chaoxing.com" in current_url and "passport" not in current_url:
                username = await self._extract_username_from_current_page()
                if username:
                    logger.info(f"当前页面检测到已登录用户: {username}")
                    return username

            # 策略2: 需要导航到个人中心页面
            if "i.chaoxing.com/base" not in current_url:
                self._tab = await self._browser.get("https://i.chaoxing.com/base")
                self._ctx_cache.clear()
                await self._tab.wait(0.3)  # 缩短等待时间

            username = await self._extract_username_from_current_page()
            if username:
                logger.info(f"个人中心检测到已登录用户: {username}")
                return username
            return None
        except Exception as e:
            logger.warning(f"获取用户名失败: {e}")
            return None

    async def _extract_username_from_current_page(self) -> Optional[str]:
        """从当前页面提取用户名（不导航）"""
        try:
            js_code = """
                (function() {
                    var selectors = [
                        '.user-name', '.userName', '.user-info .name',
                        '.head-img-box .name', '.user_info .name',
                        '#user-name', '.nickname'
                    ];
                    for (var i = 0; i < selectors.length; i++) {
                        var el = document.querySelector(selectors[i]);
                        if (el && el.textContent.trim()) return el.textContent.trim();
                    }
                    var userEl = document.querySelector('.user-info, .userName');
                    if (userEl) {
                        var title = userEl.getAttribute('title') || userEl.textContent;
                        if (title && title.trim()) return title.trim();
                    }
                    return null;
                })()
            """
            return await self._tab.evaluate(js_code)
        except Exception:
            return None

    async def close_extra_pages(self):
        """关闭除主页之外的所有页面"""
        if not self._browser:
            return
        try:
            tabs = self._browser.tabs if hasattr(self._browser, 'tabs') else []
            for tab in tabs:
                if tab != self._tab:
                    try:
                        await tab.close()
                    except Exception:
                        pass
        except Exception:
            pass

    # ======================== iframe 执行上下文 ========================

    async def _refresh_contexts(self):
        """刷新执行上下文缓存"""
        try:
            result = await self._tab.send(cdp.runtime.enable())
            # 获取所有 execution contexts
            # CDP 的 Runtime.executionContextCreated 事件会在 enable 后发送所有已有上下文
            # 但更可靠的方式是通过 evaluate 间接获取
        except Exception:
            pass

    async def find_frame_context(self, url_fragment: str) -> Optional[FrameCtx]:
        """
        根据 URL 片段查找 iframe 的执行上下文

        Args:
            url_fragment: iframe URL 的一部分（如 'studentcourse' 或 'modules/video'）

        Returns:
            FrameCtx 实例，或 None
        """
        if not self._tab:
            return None

        try:
            # 通过 JS 查找 iframe 并获取其 src
            # 然后用 CDP 获取对应的 execution context
            result = await self._tab.evaluate(f"""
                (() => {{
                    const iframes = document.querySelectorAll('iframe');
                    for (const iframe of iframes) {{
                        const src = iframe.src || iframe.getAttribute('src') || '';
                        if (src.includes('{url_fragment}')) {{
                            return src;
                        }}
                    }}
                    return '';
                }})()
            """)

            if not result:
                return None

            # 尝试获取 iframe 对应的 contentDocument
            # 对于 same-origin iframe，可以直接通过 JS 操作
            # 对于 cross-origin，需要通过 CDP execution context
            return FrameCtx(self._tab, -1, result)

        except Exception as e:
            logger.debug(f"查找 frame context 失败: {e}")
            return None

    async def eval_in_iframe(
        self, iframe_url_fragment: str, js_expression: str
    ) -> Any:
        """
        在 iframe 中执行 JS (same-origin 模式)

        通过主页面的 JS 访问 iframe.contentDocument 来执行操作。
        仅适用于 same-origin iframe。

        Args:
            iframe_url_fragment: iframe URL 的一部分
            js_expression: 接受 document 参数的 JS 表达式，
                          使用 _doc 代替 document

        Returns:
            执行结果
        """
        if not self._tab:
            return None

        # 包装 JS: 在 iframe 的 contentDocument 上下文中执行
        wrapped_js = f"""
        (() => {{
            const iframes = document.querySelectorAll('iframe');
            for (const iframe of iframes) {{
                const src = iframe.src || iframe.getAttribute('src') || '';
                if (src.includes('{iframe_url_fragment}')) {{
                    try {{
                        const _doc = iframe.contentDocument || iframe.contentWindow.document;
                        if (_doc) {{
                            return (function() {{ {js_expression} }}).call(null);
                        }}
                    }} catch(e) {{
                        return {{__error__: e.message}};
                    }}
                }}
            }}
            return null;
        }})()
        """
        try:
            result = await self._tab.evaluate(wrapped_js)
            if isinstance(result, dict) and '__error__' in result:
                logger.debug(f"iframe 执行错误: {result['__error__']}")
                return None
            return result
        except Exception as e:
            logger.debug(f"iframe evaluate 失败: {e}")
            return None

    async def eval_in_nested_iframe(
        self,
        parent_iframe_fragment: str,
        child_iframe_fragment: str,
        js_expression: str,
    ) -> Any:
        """
        在嵌套 iframe (两层) 中执行 JS

        用于 work 任务: cards_frame → work module iframe → quiz iframe
        """
        if not self._tab:
            return None

        wrapped_js = f"""
        (() => {{
            const iframes = document.querySelectorAll('iframe');
            for (const iframe of iframes) {{
                const src = iframe.src || iframe.getAttribute('src') || '';
                if (src.includes('{parent_iframe_fragment}')) {{
                    try {{
                        const _doc1 = iframe.contentDocument || iframe.contentWindow.document;
                        const childIframes = _doc1.querySelectorAll('iframe');
                        for (const child of childIframes) {{
                            const childSrc = child.src || child.getAttribute('src') || '';
                            if (childSrc.includes('{child_iframe_fragment}')) {{
                                const _doc = child.contentDocument || child.contentWindow.document;
                                if (_doc) {{
                                    return (function() {{ {js_expression} }}).call(null);
                                }}
                            }}
                        }}
                    }} catch(e) {{
                        return {{__error__: e.message}};
                    }}
                }}
            }}
            return null;
        }})()
        """
        try:
            result = await self._tab.evaluate(wrapped_js)
            if isinstance(result, dict) and '__error__' in result:
                logger.debug(f"嵌套 iframe 执行错误: {result['__error__']}")
                return None
            return result
        except Exception as e:
            logger.debug(f"嵌套 iframe evaluate 失败: {e}")
            return None
