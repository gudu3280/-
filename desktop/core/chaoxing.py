"""
超星学习通页面操作封装 (zendriver 版本)

移植自 学习通脚本.js 中的核心操作逻辑:
- Cx 类 (L1775-L2007): 视频/音频/测验/作业/考试处理
- getQuestion (L1591-L1608): 题目提取
- setAnswer/fillAnswer (L1665-L1708): 答案填写
- startWork (L2036-L2086): 章节任务执行流程

使用 zendriver (CDP) 替代 Playwright，提供更好的反检测能力。
"""

import asyncio
import json
import logging
import random
import re
from typing import List, Dict, Optional, Any, Callable, Union

import zendriver as zd

from .answer_engine import get_answer, get_answer_enhanced, AnswerResult, QUESTION_TYPE_NAMES
from .font_decrypt import FontDecryptor
from .config import Config

logger = logging.getLogger(__name__)

# Tab 类型别名
Tab = zd.Tab


def _remove_html(html: str) -> str:
    """去除HTML标签，保留纯文本"""
    if not html:
        return ""
    # 先处理 <br> 转换为换行，再清除其他标签（否则 <br> 会被第一条 regex 吞掉）
    text = re.sub(r'<br\s*/?>',  '\n', html)
    text = re.sub(r'<((?!img|sub|sup)[^>]+)>', '', text)
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&quot;', '"')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _clean_question_text(text: str) -> str:
    """清理题目文本"""
    text = re.sub(r'^【.*?】\s*', '', text)
    text = re.sub(r'\s*（\d+(?:\.\d+)?分）$', '', text)  # 同时匹配整数和小数分数
    return text.strip()


def _clean_ai_answer(text: str) -> str:
    """清理 AI 返回答案中的 Markdown 格式，保留纯文本内容"""
    if not text:
        return text
    # 移除代码块 ```...```
    text = re.sub(r'```[\w]*\n?', '', text)
    # 移除行内代码 `
    text = text.replace('`', '')
    # 移除加粗 **text** 和 __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    # 移除斜体 *text* 和 _text_（不匹配数字后的.避免误删）
    text = re.sub(r'(?<!\w)\*([^*]+?)\*(?!\w)', r'\1', text)
    # 移除 Markdown 标题前缀 # ## ###
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # 移除多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


class QuestionData:
    """题目数据"""
    def __init__(
        self,
        question: str,
        question_type: str,
        options: List[str] = None,
        raw_html: str = "",
        container_selector: str = ".TiMu",
    ):
        self.question = question
        self.question_type = question_type
        self.options = options or []
        self.raw_html = raw_html
        self.container_selector = container_selector  # 题目容器CSS选择器
        self.answer: Optional[AnswerResult] = None
        self.status: str = "pending"


class ChaoxingOperator:
    """学习通页面操作器 (zendriver 版本)"""

    def __init__(self, config: Config = None):
        self._config = config or Config()
        self._font_decryptor = FontDecryptor(self._config.get_ttf_table_path())
        self._chapters_lock = asyncio.Lock()
        # 任务处理全局锁：防止同一 work 任务被重复处理
        self._processed_works: set = set()  # 存储 "chapter_url:work_index"
        # 已完成章节 key 集合（从 SQLite 加载，用于跳过已完成的章节）
        self._completed_chapter_keys: set = set()

    def reset_task_locks(self):
        """清空任务处理锁（新一次任务执行前调用）"""
        self._processed_works.clear()

    def set_completed_chapter_keys(self, keys: set):
        """设置已完成章节 key 集合（从 SQLite 加载）"""
        self._completed_chapter_keys = set(keys) if keys else set()

    # ======================== 工具方法 ========================

    async def _navigate(self, browser: zd.Browser, tab: Tab, url: str) -> Tab:
        """导航到 URL"""
        try:
            new_tab = await browser.get(url)
            return new_tab
        except Exception as e:
            err_msg = str(e)
            if 'ERR_ABORTED' in err_msg or 'ERR_FAILED' in err_msg:
                logger.info(f"导航被重定向(预期行为): {err_msg[:60]}")
                await asyncio.sleep(1)
                return tab
            raise

    async def _check_detect_redirect(
        self, tab: Tab, browser: Optional[zd.Browser], original_url: str,
        log: Callable = None
    ) -> Optional[Tab]:
        """
        检测页面是否被超星反作弊系统重定向到 detect.chaoxing.com，
        如果是则自动恢复导航到原始 URL。

        Returns:
            恢复后的 tab (可能是新 tab)，如果检测失败/无法恢复则返回 None
        """
        _log = log or (lambda msg, level="info": logger.info(msg))
        try:
            current_url = tab.url or ""
        except Exception:
            current_url = ""

        if "detect.chaoxing.com" not in current_url and "monitor_temp" not in current_url:
            return tab  # 页面正常

        _log("检测到超星反作弊重定向 (detect.chaoxing.com)，正在恢复...", "warning")
        logger.warning(f"页面被重定向到反作弊检测页: {current_url[:100]}")

        if not browser or not original_url:
            _log("无法自动恢复（缺少 browser 或原始 URL）", "warning")
            return None

        # 等待一段时间让页面稳定
        await asyncio.sleep(3)

        # 尝试重新导航到原始 URL
        try:
            new_tab = await browser.get(original_url)
            await asyncio.sleep(3)
            new_url = new_tab.url or ""
            if "detect.chaoxing.com" not in new_url:
                _log("反作弊重定向已恢复", "success")
                return new_tab
            else:
                _log("恢复后仍被重定向，等待更久后重试...", "warning")
                await asyncio.sleep(8)
                retry_tab = await browser.get(original_url)
                await asyncio.sleep(3)
                return retry_tab
        except Exception as e:
            _log(f"恢复导航失败: {e}", "warning")
            return None

    async def _wait_for_selector(
        self, tab: Tab, selector: str, timeout_ms: int = 10000
    ) -> bool:
        """等待选择器匹配元素 (轮询实现)"""
        timeout_s = timeout_ms / 1000
        elapsed = 0.0
        interval = 0.5
        while elapsed < timeout_s:
            try:
                elems = await tab.select_all(selector)
                if elems:
                    return True
            except Exception:
                pass
            await asyncio.sleep(interval)
            elapsed += interval
        return False

    async def _eval_in_iframe(
        self, tab: Tab, iframe_fragment: str, js_body: str
    ) -> Any:
        """
        在 iframe 中执行 JS (same-origin)

        iframe_fragment: iframe src URL 的一部分
        js_body: JS 代码体，可使用 _doc 代替 document，_win 代替 window
        """
        escaped = iframe_fragment.replace("'", "\\'")
        wrapped = f"""
        (() => {{
            const iframes = document.querySelectorAll('iframe');
            for (const iframe of iframes) {{
                const src = iframe.src || iframe.getAttribute('src') || '';
                if (src.includes('{escaped}')) {{
                    try {{
                        const _doc = iframe.contentDocument || iframe.contentWindow.document;
                        const _win = iframe.contentWindow;
                        if (_doc) {{
                            return (function() {{ {js_body} }}).call(null);
                        }}
                    }} catch(e) {{
                        return {{__iframe_error__: e.message}};
                    }}
                }}
            }}
            return null;
        }})()
        """
        try:
            result = await tab.evaluate(wrapped)
            if isinstance(result, dict) and '__iframe_error__' in result:
                logger.debug(f"iframe 执行错误({iframe_fragment}): {result['__iframe_error__']}")
                return None
            return result
        except Exception as e:
            logger.debug(f"iframe evaluate 失败({iframe_fragment}): {e}")
            return None

    async def _eval_in_nested_iframe(
        self, tab: Tab, parent_fragment: str, child_fragment: str, js_body: str
    ) -> Any:
        """在两层嵌套 iframe 中执行 JS，找不到父 iframe 时直接在主页面找子 iframe"""
        pf = parent_fragment.replace("'", "\\'")
        cf = child_fragment.replace("'", "\\'")
        wrapped = f"""
        (() => {{
            // 策略1: 在父 iframe 内找子 iframe
            const iframes = document.querySelectorAll('iframe');
            for (const iframe of iframes) {{
                const src = iframe.src || iframe.getAttribute('src') || '';
                if (src.includes('{pf}')) {{
                    try {{
                        const _doc1 = iframe.contentDocument || iframe.contentWindow.document;
                        const childIframes = _doc1.querySelectorAll('iframe');
                        for (const child of childIframes) {{
                            const childSrc = child.src || child.getAttribute('src') || '';
                            if (childSrc.includes('{cf}')) {{
                                const _doc = child.contentDocument || child.contentWindow.document;
                                const _win = child.contentWindow;
                                if (_doc) {{
                                    return (function() {{ {js_body} }}).call(null);
                                }}
                            }}
                        }}
                    }} catch(e) {{
                        return {{__iframe_error__: e.message}};
                    }}
                }}
            }}
            // 策略2: 直接在主页面找子 iframe (当 tab 本身就是 studentcourse 页面时)
            for (const iframe of iframes) {{
                const src = iframe.src || iframe.getAttribute('src') || '';
                if (src.includes('{cf}')) {{
                    try {{
                        const _doc = iframe.contentDocument || iframe.contentWindow.document;
                        const _win = iframe.contentWindow;
                        if (_doc) {{
                            return (function() {{ {js_body} }}).call(null);
                        }}
                    }} catch(e) {{
                        return {{__iframe_error__: e.message}};
                    }}
                }}
            }}
            return null;
        }})()
        """
        try:
            result = await tab.evaluate(wrapped)
            if isinstance(result, dict) and '__iframe_error__' in result:
                return None
            return result
        except Exception as e:
            logger.debug(f"nested iframe evaluate 失败: {e}")
            return None

    async def _wait_in_iframe(
        self, tab: Tab, iframe_fragment: str, selector: str, timeout_ms: int = 10000
    ) -> bool:
        """在 iframe 中等待选择器"""
        timeout_s = timeout_ms / 1000
        elapsed = 0.0
        while elapsed < timeout_s:
            result = await self._eval_in_iframe(
                tab, iframe_fragment,
                f"return !!_doc.querySelector('{selector}');"
            )
            if result:
                return True
            await asyncio.sleep(0.5)
            elapsed += 0.5
        return False

    # ======================== 登录相关 ========================

    async def login(self, tab: Tab, username: str, password: str, browser: zd.Browser = None) -> bool:
        """自动登录学习通"""
        logger.info(f"开始登录，账号: {username[:3]}****")

        try:
            if browser:
                tab = await browser.get("https://passport2.chaoxing.com/login")
            await asyncio.sleep(1)

            # 切换到账号密码登录
            try:
                pwd_tab = await tab.find("账号密码登录", best_match=True)
                if pwd_tab:
                    await pwd_tab.click()
                    await asyncio.sleep(0.5)
            except Exception:
                pass

            # 填入账号密码
            phone_input = await tab.select("#phone")
            if phone_input:
                await phone_input.clear_input()
                await phone_input.send_keys(username)
            await asyncio.sleep(0.3)

            pwd_input = await tab.select("#pwd")
            if pwd_input:
                await pwd_input.clear_input()
                await pwd_input.send_keys(password)
            await asyncio.sleep(0.3)

            # 点击登录按钮
            try:
                login_btn = await tab.select("#loginBtn")
                if not login_btn:
                    login_btn = await tab.select("button[type='submit']")
                if login_btn:
                    await login_btn.click()
            except Exception as e:
                logger.warning(f"点击登录按钮失败: {e}")

            # 等待页面跳转(最多15秒)
            for _ in range(30):
                await asyncio.sleep(0.5)
                current = tab.url
                if "mycourse" in current or "interaction" in current or "mooc2-ans" in current:
                    logger.info("登录成功")
                    return True
                if "passport" not in current and "login" not in current:
                    logger.info("已跳转到其他页面，视为登录成功")
                    return True

            logger.warning("登录超时，可能需要验证码或二次确认")
            current = tab.url
            if "passport" not in current and "login" not in current:
                return True
            return False

        except Exception as e:
            logger.error(f"登录异常: {e}")
            return False

    # ======================== 缓存管理 ========================

    def clear_caches(self):
        """
        清除所有课程/章节缓存 + 任务处理锁。
        切换账号时必须调用，避免旧账号的课程数据泄漏到新账号。
        """
        # 清除课程列表缓存
        if hasattr(self, '_courses_cache'):
            delattr(self, '_courses_cache')
        # 清除所有章节缓存（_chapters_cache_<hash>）
        keys_to_remove = [k for k in dir(self) if k.startswith('_chapters_cache_')]
        for k in keys_to_remove:
            try:
                delattr(self, k)
            except Exception:
                pass
        # 清除任务处理锁和已完成章节标记（避免新账号跳过旧账号已处理的任务）
        self._processed_works.clear()
        self._completed_chapter_keys.clear()
        logger.info(f"已清除 operator 缓存（课程 + {len(keys_to_remove)} 个章节缓存 + 任务锁）")

    # ======================== 课程/章节获取 ========================

    async def get_courses(self, tab: Tab, browser: zd.Browser = None, chaoxing_browser=None, skip_session_refresh=False) -> List[Dict[str, str]]:
        """获取用户的课程列表"""
        logger.info("获取课程列表...")

        # 课程列表缓存（5分钟有效期，避免重复导航加载）
        cache_key = "_courses_cache"
        cached = getattr(self, cache_key, None)
        if cached and (asyncio.get_event_loop().time() - cached[0] < 300):
            logger.info(f"使用课程列表缓存 ({len(cached[1])} 门课程)")
            return cached[1]

        try:
            # 先访问主页确保 session 活跃（登录后首次加载可跳过）
            if browser and not skip_session_refresh:
                try:
                    logger.debug("刷新 session: 访问 i.chaoxing.com/base")
                    tab = await browser.get("https://i.chaoxing.com/base")
                    await asyncio.sleep(0.3)
                    # session 刷新后也可能触发验证码
                    tab = await self._ensure_no_captcha(tab, browser)
                except Exception as e:
                    logger.debug(f"刷新 session 失败: {e}")
            
            # 导航到新版互动课程页
            interaction_url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction"
            if browser:
                try:
                    tab = await browser.get(interaction_url)
                except Exception:
                    await asyncio.sleep(0.5)
                    try:
                        tab = await browser.get(interaction_url)
                    except Exception:
                        pass

            # 等待页面加载（含重定向）
            try:
                await tab.wait_for_ready_state()
            except Exception:
                pass
            prev_url = ""
            for _ in range(2):  # 最多 0.6 秒
                await asyncio.sleep(0.3)
                current = tab.url
                if current == prev_url:
                    break
                prev_url = current
            
            # ---- 反爬虫验证码检测 ----
            tab = await self._ensure_no_captcha(tab, browser)

            # 检查是否被重定向到登录页
            current_url = tab.url
            if "passport" in current_url or "login" in current_url:
                logger.warning(f"课程页被重定向到登录页: {current_url[:60]}")
                # 尝试恢复 cookies
                if chaoxing_browser:
                    try:
                        restored = await chaoxing_browser._restore_cookies_backup()
                        if restored:
                            logger.info("cookies 已恢复，重新导航到课程页")
                            await asyncio.sleep(1)
                            tab = await browser.get(interaction_url)
                            await tab.wait_for_ready_state()
                            # 再次检查
                            current_url = tab.url
                            if "passport" not in current_url and "login" not in current_url:
                                logger.info("cookies 恢复成功，继续加载课程")
                            else:
                                logger.warning("cookies 恢复后仍被重定向到登录页")
                                return []
                    except Exception as e:
                        logger.debug(f"恢复 cookies 失败: {e}")
                return []
            # 等待课程元素（5s→3s）
            await self._wait_for_selector(tab, "a[href*='courseId'], a[href*='courseid'], a[href*='course'], .course-info, [class*='course']", 3000)

            extract_courses_js = """
                (() => {
                    const result = [], seen = new Set();
                    function extractCourseId(url) { const m = url.match(/courseId[=:]([\\d]+)/i) || url.match(/courseid[=:]([\\d]+)/i); return m ? m[1] : ''; }
                    function buildUrl(cid, orig) { if (orig.startsWith('http')) return orig; return window.location.origin + (orig.startsWith('/')?'':'/') + orig; }
                    function findName(link) {
                        let el = link;
                        for (let i = 0; i < 4; i++) { el = el.parentElement; if (!el) break;
                            const ne = el.querySelector('.course-name, .Mcon1pTit, [class*="name"], [class*="title"], h3, h4, .course_title');
                            if (ne) { const t = ne.textContent.trim(); if (t && t.length >= 2 && t.length < 100) return t; }
                        }
                        const pt = link.parentElement ? link.parentElement.textContent.trim() : '';
                        if (pt && pt.length >= 2 && pt.length < 100) return pt;
                        const lt = link.textContent.trim(); return (lt && lt.length >= 2) ? lt : '';
                    }
                    // 策略1: 直接查找课程链接
                    document.querySelectorAll('a[href*="courseId"], a[href*="courseid"], a[href*="mycourse"], a[href*="clazzid"], a[data-courseid], a[onclick*="courseId"], a[onclick*="courseid"]').forEach(a => {
                        const href = a.href || a.getAttribute('href') || '';
                        let cid = extractCourseId(href) || a.getAttribute('data-courseid') || extractCourseId(a.getAttribute('onclick')||'');
                        if (!cid || seen.has(cid)) return; seen.add(cid);
                        const name = findName(a); if (!name || name.length < 2) return;
                        result.push({ name: name.substring(0,100).replace(/\\s+/g,' ').trim(), url: buildUrl(cid, href), id: cid });
                    });
                    // 策略2: 查找课程卡片容器
                    if (result.length === 0) {
                        document.querySelectorAll('[class*="course"], [class*="Course"], .list-item, [class*="card"], .course-info').forEach(card => {
                            const link = card.querySelector('a[href]'); if (!link) return;
                            const href = link.href || link.getAttribute('href') || '';
                            let cid = extractCourseId(href) || card.getAttribute('data-courseid') || '';
                            if (!cid || seen.has(cid)) return; seen.add(cid);
                            const ne = card.querySelector('[class*="name"], [class*="title"], .course_title');
                            let name = ne ? ne.textContent.trim() : card.textContent.trim().substring(0, 60);
                            if (!name || name.length < 2) return;
                            result.push({ name: name.substring(0,100), url: buildUrl(cid, href), id: cid });
                        });
                    }
                    return result;
                })()
            """
            courses = await tab.evaluate(extract_courses_js)

            # 也检查 iframe 中的课程
            if not courses:
                iframe_r = await self._eval_in_iframe(tab, "interaction", extract_courses_js)
                if iframe_r: courses = iframe_r

            # 如果新页面未找到课程，检测是否为 404/405 错误页
            if not courses and browser:
                try:
                    is_error = await tab.evaluate("""
                        (document.title.includes('404') || document.title.includes('405') ||
                         document.body.innerText.includes('页面不存在') ||
                         document.body.innerText.includes('暂时不能访问'))
                    """)
                    if is_error:
                        # 新页面加载失败，回退到 mooc1 旧版课程列表页
                        logger.warning("新版互动课程页加载失败(404/405)，回退到 mooc1 课程列表")
                        tab = await browser.get("https://mooc1.chaoxing.com/visit/courses")
                        await asyncio.sleep(1)
                        await self._wait_for_selector(tab, "a[href*='courseId'], a[href*='courseid'], [class*='course']", 3000)
                        courses = await tab.evaluate(extract_courses_js)
                    else:
                        # 页面正常但课程未加载，快速重试
                        logger.info("新页面课程未加载，等待重试...")
                        await asyncio.sleep(1)
                        courses = await tab.evaluate(extract_courses_js)
                except Exception: pass

            # 写入缓存
            if courses:
                setattr(self, cache_key, (asyncio.get_event_loop().time(), courses))
            logger.info(f"获取到 {len(courses) if courses else 0} 门课程")
            return courses or []
        except Exception as e:
            logger.error(f"获取课程列表失败: {e}")
            return []

    async def get_chapters(self, tab: Tab, course_url: str, browser: zd.Browser = None) -> List[Dict[str, Any]]:
        async with self._chapters_lock:
            return await self._get_chapters_impl(tab, course_url, browser)

    async def _get_chapters_impl(self, tab: Tab, course_url: str, browser: zd.Browser = None) -> List[Dict[str, Any]]:
        logger.info(f"获取章节列表: {course_url[:80]}...")

        # 章节缓存（10分钟有效期）
        ch_cache_key = f"_chapters_cache_{hash(course_url)}"
        cached = getattr(self, ch_cache_key, None)
        if cached and (asyncio.get_event_loop().time() - cached[0] < 600):
            logger.info(f"使用章节列表缓存 ({len(cached[1])} 项)")
            return cached[1]

        try:
            if browser:
                try: tab = await browser.get(course_url)
                except Exception as e:
                    if 'ERR_ABORTED' in str(e) or 'ERR_FAILED' in str(e): await asyncio.sleep(0.5)
                    else: raise
            # URL 稳定检测（缩短为 4 次轮询，最多 2 秒）
            prev_url = ""
            for _ in range(4):
                await asyncio.sleep(0.5); current = tab.url
                if current == prev_url: break
                prev_url = current
            current_url = tab.url

            # ---- 反爬虫验证码检测 ----
            tab = await self._ensure_no_captcha(tab, browser)
            current_url = tab.url

            # 403 / 权限失效检测 → 自动恢复：先回主页刷新 session，再重试
            if await self._is_page_blocked(tab):
                logger.info("课程页面被拦截(403/登录过期)，尝试回主页刷新 session...")
                if browser:
                    try:
                        tab = await browser.get("https://i.chaoxing.com/base")
                        await asyncio.sleep(1)
                    except Exception:
                        await asyncio.sleep(0.5)
                # 重试导航
                if browser:
                    try: tab = await browser.get(course_url)
                    except Exception as e:
                        if 'ERR_ABORTED' in str(e) or 'ERR_FAILED' in str(e): await asyncio.sleep(0.5)
                        else: raise
                prev_url = ""
                for _ in range(4):
                    await asyncio.sleep(0.5); current = tab.url
                    if current == prev_url: break
                    prev_url = current
                current_url = tab.url

            # 再次检测
            if any(kw in current_url.lower() for kw in ["login", "passport", "403", "forbidden"]):
                logger.warning(f"课程页面被重定向到: {current_url[:80]}，登录可能已过期")
                return []
            try:
                body_check = await tab.evaluate("document.body ? document.body.innerText.substring(0, 300) : ''")
                if body_check and isinstance(body_check, str):
                    if "403" in body_check or "禁止访问" in body_check or "无权访问" in body_check:
                        logger.warning(f"课程页面返回 403: {body_check[:80]}")
                        return []
            except Exception:
                pass

            m1 = re.search(r'courseid[=:](\d+)', current_url, re.I)
            m2 = re.search(r'courseid[=:](\d+)', course_url, re.I)
            course_id = (m1 or m2 or [None, ''])[1]
            m3 = re.search(r'clazzid[=:](\d+)', current_url, re.I)
            m4 = re.search(r'clazzid[=:](\d+)', course_url, re.I)
            clazz_id = (m3 or m4 or [None, ''])[1]
            logger.info(f"courseId={course_id}, clazzId={clazz_id}")
            try:
                ct = await tab.find("章节", best_match=True)
                if ct: await ct.click(); await asyncio.sleep(1)
            except Exception: pass
            iframe_src = await tab.evaluate("((()=>{const ifs=document.querySelectorAll('iframe');for(const f of ifs){const s=f.src||f.getAttribute('src')||'';if(s.includes('studentcourse')||s.includes('chapterlist'))return s;}for(const f of ifs){const s=f.src||f.getAttribute('src')||'';if(s.includes('courseid')&&!s.includes('stuActiveList'))return s;}return '';})())")
            if iframe_src:
                if not iframe_src.startswith('http'):
                    base_url = self._get_base_url(tab.url)
                    iframe_src = f"{base_url}{iframe_src}"
                try:
                    if browser: tab = await browser.get(iframe_src)
                    await asyncio.sleep(1.5)
                except Exception: pass
            else:
                cpi = (re.search(r'cpi=(\d+)', current_url) or [None,''])[1]
                enc = (re.search(r'enc=([a-f0-9]+)', current_url) or [None,''])[1]
                # 使用当前页面 URL 提取基础路径（动态适配用户子域名）
                base_url = self._get_base_url(current_url)
                sc_url = f"{base_url}/mycourse/studentcourse?courseid={course_id}&clazzid={clazz_id}&cpi={cpi}&ut=s&stuenc={enc}"
                try:
                    if browser: tab = await browser.get(sc_url)
                    await asyncio.sleep(1.5)
                except Exception: pass
            extract_js = "((()=>{const result=[],allLi=document.querySelectorAll('li');for(const li of allLi){const d=li.querySelector('div.chapter_item');if(!d)continue;const id=d.id||'',onclick=d.getAttribute('onclick')||'',title=d.getAttribute('title')||'';const nameEl=d.querySelector('span.catalog_sbar'),textEl=d.querySelector('.catalog_name,.newCatalog_name');let number=nameEl?nameEl.textContent.trim():'',name=textEl?textEl.textContent.trim():'';if(name&&number)name=name.replace(/^[ ]*[0-9]+[.][0-9]+[ ]*/,'').trim();const dn=number?number+' '+(name||title):(name||title);if(!dn||dn.length<2)continue;const m=onclick.match(/toOld\\s*\\(\\s*'([^']+)'\\s*,\\s*'([^']+)'\\s*,\\s*'([^']+)'/);let cId='',chId='',clId='';if(m){cId=m[1];chId=m[2];clId=m[3];}if(!chId&&id.startsWith('cur'))chId=id.substring(3);result.push({name:dn.substring(0,100),url:'',courseId:cId,chapterId:chId,clazzId:clId,onclick,taskCount:'',index:result.length});}return result;})())"
            chapters = []
            for attempt in range(3):
                try: chapters = await tab.evaluate(extract_js)
                except Exception: chapters = []
                if chapters: break
                await asyncio.sleep(2 * (attempt + 1))

            # 诊断信息：提取失败时记录页面状态
            if not chapters:
                try:
                    diag = await tab.evaluate("""
                        (() => {
                            const url = location.href;
                            const title = document.title;
                            const iframes = Array.from(document.querySelectorAll('iframe')).map(f => f.src || f.getAttribute('src') || '');
                            const lis = document.querySelectorAll('li').length;
                            const chapterItems = document.querySelectorAll('div.chapter_item').length;
                            const bodyLen = document.body ? document.body.innerText.length : 0;
                            const bodyPreview = document.body ? document.body.innerText.substring(0, 200) : '';
                            return { url, title, iframeCount: iframes.length, iframeSrcs: iframes.slice(0, 5), liCount: lis, chapterItemCount: chapterItems, bodyLen, bodyPreview };
                        })()
                    """)
                    logger.warning(f"章节提取失败，页面诊断: {diag}")
                except Exception as diag_err:
                    logger.warning(f"章节提取失败，诊断也失败: {diag_err}")
                # 尝试备用提取方案：通过 a 标签的 onclick 提取
                fallback_js = "((()=>{const result=[],links=document.querySelectorAll('a[onclick]');for(const a of links){const onclick=a.getAttribute('onclick')||'';const m=onclick.match(/toOld\\s*\\(\\s*'([^']+)'\\s*,\\s*'([^']+)'\\s*,\\s*'([^']+)'/);if(!m)continue;const name=a.textContent.trim();if(!name||name.length<2)continue;result.push({name:name.substring(0,100),url:'',courseId:m[1],chapterId:m[2],clazzId:m[3],onclick,taskCount:'',index:result.length});}return result;})())"
                try:
                    fb = await tab.evaluate(fallback_js)
                    if fb:
                        chapters = fb
                        logger.info(f"备用方案提取到 {len(chapters)} 个章节")
                except Exception:
                    pass
            if chapters:
                valid = []
                for ch in chapters:
                    chid = ch.get('chapterId', '')
                    cid = ch.get('courseId', course_id)
                    zid = ch.get('clazzId', clazz_id)
                    if not chid: continue
                    cpi_val = (re.search(r'cpi=(\d+)', current_url) or [None, ''])[1]
                    # 动态提取基础路径构造 knowledgestu URL
                    base_url = self._get_base_url(current_url)
                    ch['url'] = f"{base_url}/knowledge/knowledgestu?courseid={cid}&knowledgeid={chid}&clazzid={zid}&cpi={cpi_val}"
                    valid.append(ch)
                chapters = valid
                logger.info(f"章节分类: {len(chapters)} 个有效章节")
            logger.info(f"获取到 {len(chapters)} 个章节")
            # 写入章节缓存
            if chapters:
                setattr(self, ch_cache_key, (asyncio.get_event_loop().time(), chapters))
            return chapters
        except Exception as e:
            logger.error(f"获取章节列表失败: {e}")
            return []

    # ======================== URL 工具 ========================

    @staticmethod
    def _get_base_url(current_url: str) -> str:
        """
        从当前页面 URL 提取基础 URL（origin + 路径前缀）

        例如:
        - https://mooc2-xxx.chaoxing.com/mooc2-ans/visit/interaction → https://mooc2-xxx.chaoxing.com/mooc2-ans
        - https://mooc2-xxx.chaoxing.com/visit/interaction → https://mooc2-xxx.chaoxing.com
        - https://mooc1.chaoxing.com/visit/courses → https://mooc1.chaoxing.com
        """
        if not current_url or '/' not in current_url:
            return "https://mooc1.chaoxing.com"
        parts = current_url.split('/')
        origin = f"https://{parts[2]}"
        # 检测路径前缀（如 /mooc2-ans）
        if len(parts) > 3 and parts[3] in ('mooc2-ans', 'mooc2', 'mooc'):
            return f"{origin}/{parts[3]}"
        return origin

    @staticmethod
    def _normalize_chapter_url(url: str) -> str:
        """统一章节 URL，仅对旧版 studentcourse 添加 mooc2=1，不修改 knowledgestu 路径"""
        if not url or not url.startswith('http'):
            return url
        # knowledgestu 是新版 URL，不修改它
        if 'knowledgestu' in url:
            return url
        # 旧版 studentcourse → studentstudy + mooc2=1
        url = url.replace('/mycourse/studentcourse', '/mycourse/studentstudy')
        if 'mooc2=1' not in url:
            sep = '&' if '?' in url else '?'
            url = url + sep + 'mooc2=1'
        return url

    # ======================== 任务点识别 ========================

    async def get_task_points(self, tab: Tab, browser: zd.Browser = None) -> List[Dict[str, Any]]:
        """
        获取当前章节页面的任务点列表

        Returns:
            [{"type": "video|work|audio|pdf", "iframe_src": "...",
              "finished": bool, "job_index": int, ...}, ...]
        """
        try:
            # 等待页面/iframe 加载
            await asyncio.sleep(1.5)

            # 提取任务点信息的 JS (用 _doc 代替 document，_win 代替 window)
            # 重要: 当一个任务点包含多个 iframe 时，展开为多个独立任务
            # 同时检测无 .ans-job-icon 的 PDF/PPT 任务点 (超星新版页面 PDF 不带任务图标)
            # 核心改进: 读取 _win.attachments API 数据，获取服务端真实的任务完成状态
            extract_js = """
                const result = [];
                let globalIndex = 0;
                // 记录已处理的 iframe，避免重复
                const processedIframes = new Set();

                // 读取超星 attachments API（服务端真实状态）
                // _win.attachments 是超星注入的全局变量，包含每个任务点的 job/isPassed 状态
                const attachmentsMap = {};
                try {
                    const attachments = _win.attachments || [];
                    for (const att of attachments) {
                        const jid = String(att.jobid || (att.property && att.property._jobid) || '');
                        if (jid) {
                            attachmentsMap[jid] = {
                                is_job: !!att.job,
                                is_passed: !!att.isPassed,
                                mid: (att.property && att.property.mid) || '',
                                name: (att.property && (att.property.name || att.property.title)) || ''
                            };
                        }
                    }
                } catch(e) {}

                // 辅助函数: 从 iframe 提取任务信息
                function extractIframeTask(iframe, jobIndex, subIdx, domFinished) {
                    const iframeSrc = iframe.src || '';
                    const dataAttr = iframe.getAttribute('data') || '{}';
                    let type = 'unknown';
                    if (iframeSrc.includes('/modules/video/')) type = 'video';
                    else if (iframeSrc.includes('/modules/work/')) type = 'work';
                    else if (iframeSrc.includes('/modules/audio/')) type = 'audio';
                    else if (iframeSrc.includes('/modules/pdf/')) type = 'pdf';
                    else if (iframeSrc.includes('/readsvr/book/')) type = 'reading';
                    else if (iframeSrc.includes('/modules/ppt/') || iframeSrc.includes('swiper')) type = 'ppt';
                    else if (iframeSrc.includes('hyperlink')) type = 'link';
                    let name = '';
                    let jobId = '';
                    let hasJobId = false;
                    try {
                        const data = JSON.parse(dataAttr);
                        name = data.name || data.title || '';
                        jobId = String(data._jobid || data.jobid || '');
                        hasJobId = !!data._jobid;
                    } catch(e) {}

                    // 核心: 用 attachments API 判断完成状态，比 DOM 类名更可靠
                    let apiFinished = null;
                    let isJob = null;
                    if (jobId && attachmentsMap[jobId]) {
                        const att = attachmentsMap[jobId];
                        isJob = att.is_job;
                        // attachment.job=true 且 isPassed=true → 已完成
                        // attachment.job=false 且 isPassed=true → 已完成（非任务点但已通过）
                        apiFinished = att.is_passed;
                        if (!name && att.name) name = att.name;
                    }

                    // 最终完成状态: API数据优先，DOM类名作为回退
                    let finished = apiFinished !== null ? apiFinished : domFinished;

                    return { type, iframe_src: iframeSrc, finished: finished,
                             name, data_attr: dataAttr, job_index: jobIndex,
                             sub_index: subIdx, global_index: globalIndex++,
                             has_jobid: hasJobId, is_job: isJob,
                             api_finished: apiFinished,
                             dom_finished: domFinished };
                }

                // 第一轮: 通过 .ans-job-icon 查找 (视频/测验/音频等)
                const jobIcons = _doc.querySelectorAll('.ans-job-icon');
                let jobIndex = 0;
                jobIcons.forEach((icon) => {
                    const parent = icon.parentElement;
                    // 仅检查 ans-job-finished（超星专用完成标记），不使用宽泛的 finish/completed
                    let isFinished = false;
                    if (parent) {
                        isFinished = parent.classList.contains('ans-job-finished') ||
                                     parent.querySelector('.ans-job-finished') !== null;
                    }
                    if (!isFinished && icon.classList.contains('ans-job-finished')) {
                        isFinished = true;
                    }
                    const iframes = parent ? parent.querySelectorAll('iframe') : [];
                    if (iframes.length === 0) { jobIndex++; return; }
                    let subIdx = 0;
                    iframes.forEach((iframe) => {
                        const task = extractIframeTask(iframe, jobIndex, subIdx, isFinished);
                        processedIframes.add(iframe);
                        result.push(task);
                        subIdx++;
                    });
                    jobIndex++;
                });

                // 第二轮: 查找无 .ans-job-icon 的 .ans-attach-ct 容器 (PDF/PPT 任务点)
                // 超星新版页面的 PDF/PPT 任务点不带任务图标，但 iframe 的 data 中有 _jobid
                const attachContainers = _doc.querySelectorAll('.ans-attach-ct');
                attachContainers.forEach((container) => {
                    // 跳过已有 .ans-job-icon 的容器 (第一轮已处理)
                    if (container.querySelector('.ans-job-icon')) return;
                    const iframes = container.querySelectorAll('iframe');
                    iframes.forEach((iframe, subIdx) => {
                        // 跳过已处理的 iframe
                        if (processedIframes.has(iframe)) return;
                        // 检查是否有 _jobid (表示是可追踪的任务)
                        const dataAttr = iframe.getAttribute('data') || '{}';
                        let hasJobId = false;
                        try { hasJobId = !!JSON.parse(dataAttr)._jobid; } catch(e) {}
                        if (!hasJobId) return;  // 无可追踪的 jobid，跳过
                        // PDF 任务点没有 finished 标记，依赖 attachments API
                        const task = extractIframeTask(iframe, jobIndex, subIdx, false);
                        processedIframes.add(iframe);
                        result.push(task);
                        jobIndex++;
                    });
                });

                return result;
            """

            # 策略1: 在嵌套 iframe 中查找 (studentstudy 页面场景, iframe src 包含 cards)
            # 尝试多种 iframe 匹配关键词
            iframe_frags = ["cards", "knowledge", "studentcourse", "studentstudy"]
            for frag in iframe_frags:
                has_iframe = await self._eval_in_iframe(tab, frag, "return !!_doc;")
                if has_iframe:
                    tasks = await self._eval_in_iframe(tab, frag, extract_js)
                    if tasks:
                        logger.info(f"检测到 {len(tasks)} 个任务点 (iframe:{frag})")
                        # 补充检测：从外层页面检查 jobUnfinishCount（超星侧边栏未完成计数器）
                        # 当 ans-job-finished 类未出现但 jobUnfinishCount=0 时，也标记为已完成
                        try:
                            chapter_all_done = await tab.evaluate("""
                                (() => {
                                    const inputs = document.querySelectorAll('.jobUnfinishCount, input.jobUnfinishCount');
                                    let total = 0;
                                    inputs.forEach(inp => { total += parseInt(inp.value) || 0; });
                                    // jobUnfinishCount=0 表示全部完成
                                    return inputs.length > 0 && total === 0;
                                })()
                            """)
                            if chapter_all_done:
                                logger.info("jobUnfinishCount=0，全部任务点标记为已完成")
                                for t in tasks:
                                    t["finished"] = True
                        except Exception:
                            pass
                        return tasks

            # 策略1.5: 通过 iframe id 查找
            try:
                id_result = await tab.evaluate("""
                    (() => {
                        const iframe = document.getElementById('iframe');
                        if (!iframe) return null;
                        const _doc = iframe.contentDocument || iframe.contentWindow.document;
                        if (!_doc) return null;
                        const result = [];
                        let globalIndex = 0;
                        const processedIframes = new Set();
                        function extractTask(ifr, jIdx, sIdx, fin) {
                            const src = ifr.src || '';
                            const dAttr = ifr.getAttribute('data') || '{}';
                            let t = 'unknown';
                            if (src.includes('/modules/video/')) t = 'video';
                            else if (src.includes('/modules/work/')) t = 'work';
                            else if (src.includes('/modules/audio/')) t = 'audio';
                            else if (src.includes('/modules/pdf/')) t = 'pdf';
                            let n = '';
                            try { n = JSON.parse(dAttr).name || ''; } catch(e) {}
                            return { type: t, iframe_src: src, finished: fin, name: n, data_attr: dAttr, job_index: jIdx, sub_index: sIdx, global_index: globalIndex++ };
                        }
                        let jobIndex = 0;
                        _doc.querySelectorAll('.ans-job-icon').forEach((icon) => {
                            const parent = icon.parentElement;
                            const isFinished = parent && parent.classList.contains('ans-job-finished');
                            const iframes = parent ? parent.querySelectorAll('iframe') : [];
                            if (iframes.length === 0) { jobIndex++; return; }
                            let subIdx = 0;
                            iframes.forEach((f) => { result.push(extractTask(f, jobIndex, subIdx, isFinished)); processedIframes.add(f); subIdx++; });
                            jobIndex++;
                        });
                        _doc.querySelectorAll('.ans-attach-ct').forEach((container) => {
                            if (container.querySelector('.ans-job-icon')) return;
                            container.querySelectorAll('iframe').forEach((f, sIdx) => {
                                if (processedIframes.has(f)) return;
                                const dAttr = f.getAttribute('data') || '{}';
                                let hasJob = false;
                                try { hasJob = !!JSON.parse(dAttr)._jobid; } catch(e) {}
                                if (!hasJob) return;
                                result.push(extractTask(f, jobIndex, sIdx, false));
                                processedIframes.add(f);
                                jobIndex++;
                            });
                        });
                        return result;
                    })()
                """)
                if id_result and len(id_result) > 0:
                    logger.info(f"检测到 {len(id_result)} 个任务点 (iframe#id)")
                    # 补充检测 jobUnfinishCount
                    try:
                        chapter_all_done = await tab.evaluate("""
                            (() => {
                                const inputs = document.querySelectorAll('.jobUnfinishCount, input.jobUnfinishCount');
                                let total = 0;
                                inputs.forEach(inp => { total += parseInt(inp.value) || 0; });
                                return inputs.length > 0 && total === 0;
                            })()
                        """)
                        if chapter_all_done:
                            logger.info("jobUnfinishCount=0，全部任务点标记为已完成")
                            for t in id_result:
                                t["finished"] = True
                    except Exception:
                        pass
                    return id_result
            except Exception:
                pass

            # 策略2: 直接在主页面查找 (studentcourse 页面本身就是 iframe 内容)
            main_js = extract_js.replace('_doc', 'document')
            main_wrapped = f"(() => {{ const _doc = document; {main_js} }})()"
            try:
                tasks = await tab.evaluate(main_wrapped)
                if tasks and len(tasks) > 0:
                    logger.info(f"检测到 {len(tasks)} 个任务点 (主页面)")
                    return tasks
            except Exception as e:
                logger.debug(f"主页面查找任务点失败: {e}")

            return []

        except Exception as e:
            logger.error(f"获取任务点列表失败: {e}")
            return []

    # ======================== 任务点完成状态实时校验 ========================

    async def _recheck_single_task_finished(
        self, tab: Tab, task: Dict[str, Any], on_log: callable = None
    ) -> bool:
        """
        实时检查单个任务点是否已完成

        多策略检测（优先级从高到低）:
        0. attachments API（超星服务端真实状态，最可靠）
        1. 在 cards iframe 中按 job_index 查找 ans-job-finished 类
        2. 检查 cards iframe 中的 jobUnfinishCount
        3. 检查主文档中的 jobUnfinishCount
        """
        job_index = task.get("job_index", -1)

        # 策略0: 通过 attachments API 检查（最可靠）
        data_attr = task.get("data_attr", "{}")
        job_id = ""
        try:
            import json as _json
            data = _json.loads(data_attr)
            job_id = str(data.get("_jobid") or data.get("jobid") or "")
        except Exception:
            pass

        if job_id:
            for frag in ["cards", "knowledge"]:
                try:
                    is_passed = await self._eval_in_iframe(tab, frag, f"""
                        return (() => {{
                            try {{
                                const attachments = _win.attachments || [];
                                for (const att of attachments) {{
                                    const jid = String(att.jobid || (att.property && att.property._jobid) || '');
                                    if (jid === '{job_id}') return !!att.isPassed;
                                }}
                            }} catch(e) {{}}
                            return null;
                        }})();
                    """)
                    if is_passed is True:
                        if on_log:
                            on_log(f"任务点[{job_index}] 实时检测: attachments API 确认已完成", "success")
                        return True
                except Exception:
                    pass

        if job_index < 0:
            return False

        # 策略1: 在 cards iframe 中按 job_index 精准查找 ans-job-finished
        recheck_js = f"""
            (() => {{
                const icons = _doc.querySelectorAll('.ans-job-icon');
                let idx = 0;
                for (const icon of icons) {{
                    if (idx === {job_index}) {{
                        const parent = icon.parentElement;
                        if (parent && parent.classList.contains('ans-job-finished')) return true;
                        if (parent && parent.querySelector('.ans-job-finished')) return true;
                        if (icon.classList.contains('ans-job-finished')) return true;
                        return false;
                    }}
                    idx++;
                }}
                // 未找到对应 job_index，回退查找第 {job_index} 个带 iframe 的任务容器
                const containers = _doc.querySelectorAll('[class*="ans-job"]');
                return null;
            }})()
        """
        for frag in ["cards", "knowledge"]:
            try:
                result = await self._eval_in_iframe(tab, frag, f"return {recheck_js};")
                if result is True:
                    if on_log:
                        on_log(f"任务点[{job_index}] 实时检测: ans-job-finished 确认完成", "info")
                    return True
            except Exception:
                pass

        # 策略2: 检查 cards iframe 内的 jobUnfinishCount
        for frag in ["cards", "knowledge"]:
            try:
                count = await self._eval_in_iframe(tab, frag, """
                    return (() => {
                        const inputs = _doc.querySelectorAll('.jobUnfinishCount, input.jobUnfinishCount');
                        if (inputs.length === 0) return null;
                        let total = 0;
                        inputs.forEach(inp => { total += parseInt(inp.value) || 0; });
                        return total;
                    })();
                """)
                if count is not None and isinstance(count, (int, float)) and count == 0:
                    if on_log:
                        on_log(f"任务点[{job_index}] 实时检测: iframe jobUnfinishCount=0 全部完成", "info")
                    return True
            except Exception:
                pass

        # 策略3: 检查主文档的 jobUnfinishCount（侧边栏计数）
        try:
            main_count = await tab.evaluate("""
                (() => {
                    const inputs = document.querySelectorAll('.jobUnfinishCount, input.jobUnfinishCount');
                    if (inputs.length === 0) return null;
                    let total = 0;
                    inputs.forEach(inp => { total += parseInt(inp.value) || 0; });
                    return total;
                })()
            """)
            if main_count is not None and isinstance(main_count, (int, float)) and main_count == 0:
                if on_log:
                    on_log(f"任务点[{job_index}] 实时检测: 主文档 jobUnfinishCount=0 全部完成", "info")
                return True
        except Exception:
            pass

        return False

    async def _batch_recheck_all_tasks(
        self, tab: Tab, tasks: List[Dict[str, Any]], on_log: callable = None
    ) -> List[Dict[str, Any]]:
        """
        批量重新检查所有任务点的完成状态

        优先级:
        1. attachments API（超星服务端真实状态，最可靠）
        2. ans-job-finished DOM 类名
        3. jobUnfinishCount 计数器
        """
        # 策略0: 通过 attachments API 批量检查（最可靠）
        attachments_js = """
            (() => {
                const map = {};
                try {
                    const attachments = _win.attachments || [];
                    for (const att of attachments) {
                        const jid = String(att.jobid || (att.property && att.property._jobid) || '');
                        if (jid) {
                            map[jid] = {
                                is_job: !!att.job,
                                is_passed: !!att.isPassed
                            };
                        }
                    }
                } catch(e) {}
                return map;
            })()
        """
        attachments_checked = False
        for frag in ["cards", "knowledge"]:
            try:
                att_map = await self._eval_in_iframe(tab, frag, f"return {attachments_js};")
                if att_map and isinstance(att_map, dict) and len(att_map) > 0:
                    updated = 0
                    for task in tasks:
                        # 从 task 的 data_attr 中提取 jobid
                        data_attr = task.get("data_attr", "{}")
                        job_id = ""
                        try:
                            data = __import__("json").loads(data_attr)
                            job_id = str(data.get("_jobid") or data.get("jobid") or "")
                        except Exception:
                            pass
                        if job_id and job_id in att_map:
                            att = att_map[job_id]
                            if att.get("is_passed"):
                                task["finished"] = True
                                updated += 1
                            # 同时更新 is_job 字段
                            task["is_job"] = att.get("is_job")
                    if updated > 0:
                        if on_log:
                            on_log(f"批量重检: attachments API 确认 {updated}/{len(tasks)} 个任务点已完成", "success")
                    attachments_checked = True
                    break
            except Exception:
                pass

        # 策略1: DOM 类名批量检查
        batch_js = """
            (() => {
                const icons = _doc.querySelectorAll('.ans-job-icon');
                const results = [];
                let idx = 0;
                icons.forEach((icon) => {
                    const parent = icon.parentElement;
                    let finished = false;
                    if (parent && parent.classList.contains('ans-job-finished')) finished = true;
                    else if (parent && parent.querySelector('.ans-job-finished')) finished = true;
                    else if (icon.classList.contains('ans-job-finished')) finished = true;
                    results.push({job_index: idx, finished: finished});
                    idx++;
                });
                // 同时获取 jobUnfinishCount
                const inputs = _doc.querySelectorAll('.jobUnfinishCount, input.jobUnfinishCount');
                let unfinishCount = null;
                if (inputs.length > 0) {
                    unfinishCount = 0;
                    inputs.forEach(inp => { unfinishCount += parseInt(inp.value) || 0; });
                }
                return {task_states: results, unfinish_count: unfinishCount};
            })()
        """

        all_done_by_count = False

        # 在 cards/knowledge iframe 中批量检查
        for frag in ["cards", "knowledge"]:
            try:
                result = await self._eval_in_iframe(tab, frag, f"return {batch_js};")
                if result and isinstance(result, dict):
                    task_states = result.get("task_states", [])
                    unfinish_count = result.get("unfinish_count")

                    # 如果 jobUnfinishCount=0，全部标记完成
                    if unfinish_count is not None and unfinish_count == 0:
                        all_done_by_count = True
                        if on_log:
                            on_log(f"批量重检: iframe jobUnfinishCount=0，全部标记完成", "success")

                    # 按 job_index 更新每个任务的 finished 状态
                    state_map = {s["job_index"]: s["finished"] for s in task_states if isinstance(s, dict)}
                    for task in tasks:
                        ji = task.get("job_index", -1)
                        if all_done_by_count:
                            task["finished"] = True
                        elif ji in state_map and state_map[ji] and not task.get("finished"):
                            task["finished"] = True

                    if task_states:
                        finished_count = sum(1 for t in tasks if t.get("finished"))
                        if on_log:
                            on_log(f"批量重检: DOM检查 {finished_count}/{len(tasks)} 个任务点已完成", "info")
                    break
            except Exception:
                pass

        # 补充: 检查主文档的 jobUnfinishCount
        if not all_done_by_count:
            try:
                main_count = await tab.evaluate("""
                    (() => {
                        const inputs = document.querySelectorAll('.jobUnfinishCount, input.jobUnfinishCount');
                        if (inputs.length === 0) return null;
                        let total = 0;
                        inputs.forEach(inp => { total += parseInt(inp.value) || 0; });
                        return total;
                    })()
                """)
                if main_count is not None and isinstance(main_count, (int, float)) and main_count == 0:
                    if on_log:
                        on_log(f"批量重检: 主文档 jobUnfinishCount=0，全部标记完成", "success")
                    for task in tasks:
                        task["finished"] = True
            except Exception:
                pass

        return tasks

    # ======================== 答题操作 ========================

    async def extract_questions(
        self, tab: Tab, quiz_type: str = "1",
        iframe_frag: str = "", browser: zd.Browser = None,
        deep: bool = False, iframe_index: int = 0,
    ) -> List[QuestionData]:
        """
        从页面提取题目列表

        Args:
            tab: 标签页
            quiz_type: "1"=章节测验, "2"=作业, "3"=考试
            iframe_frag: iframe URL 片段 (空则直接在 tab 上操作)
        """
        try:
            if quiz_type == "1":
                selector = '.TiMu'
            elif quiz_type == "2":
                selector = '.questionLi'
            elif quiz_type == "3":
                selector = 'body'
            else:
                selector = '.TiMu'

            # ---- 7.2 判断题繁体字/图片选项预处理 ----
            # 超星旧版判断题选项可能是图片或图标(.ri class)，而非文字
            # 需要在提取前将 True/False/對/錯 和 .ri 图标转换为 √/× 文字
            preprocess_judgment_js = """
                (() => {
                    const typeInputs = _doc.querySelectorAll("input[id^='answertype']");
                    let count = 0;
                    typeInputs.forEach(input => {
                        if (input.value !== '3') return;  // type=3 是判断题
                        const container = input.closest('.TiMu, .questionLi, .Py-mian1, [class*="question"], [class*="timu"]');
                        if (!container) return;
                        const options = container.querySelectorAll('ul li .after, ul.answerList li, .answer_p, .option-item, ul li');
                        options.forEach(opt => {
                            const text = (opt.textContent || '').trim();
                            // 已有中文对错文字，不需要转换
                            if (text.includes('对') || text.includes('错') || text.includes('√') || text.includes('×')) return;
                            // 英文 True/False
                            if (text === 'True') { opt.textContent = '√'; count++; return; }
                            if (text === 'False') { opt.textContent = '×'; count++; return; }
                            // 繁体字 對/錯
                            if (text === '對') { opt.textContent = '√'; count++; return; }
                            if (text === '錯') { opt.textContent = '×'; count++; return; }
                            // .ri class 图标（ri = right icon = √，无 ri = ×）
                            const ri = opt.querySelector('.ri');
                            if (ri) {
                                const span = _doc.createElement('span');
                                span.innerText = '√';
                                opt.innerHTML = '';
                                opt.appendChild(span);
                                count++;
                            } else if (opt.querySelector('img') && !text) {
                                // 有图片但无文字 → 根据位置判断（第一个=对，第二个=错）
                                const allOpts = Array.from(opt.parentElement.children);
                                const idx = allOpts.indexOf(opt);
                                const span = _doc.createElement('span');
                                span.innerText = idx === 0 ? '√' : '×';
                                opt.appendChild(span);
                                count++;
                            }
                        });
                    });
                    return count;
                })()
            """

            # 执行判断题预处理
            try:
                if iframe_frag:
                    pre_count = await self._eval_in_iframe(tab, iframe_frag, preprocess_judgment_js)
                else:
                    preprocess_js_run = preprocess_judgment_js.replace('_doc', 'document')
                    pre_count = await tab.evaluate(f"(() => {{ {preprocess_js_run} }})()")
                if pre_count and pre_count > 0:
                    logger.info(f"判断题选项预处理: 转换了 {pre_count} 个选项")
            except Exception as pre_err:
                logger.debug(f"判断题预处理异常(忽略): {pre_err}")

            # 通过 JS 提取题目数据（一次性完成，无元素时自然返回空数组）
            if quiz_type == "1":
                extract_js = """
                    const timus = _doc.querySelectorAll('.TiMu');
                    const results = [];
                    timus.forEach(timu => {
                        const titleEl = timu.querySelector('.clearfix .fontLabel');
                        if (!titleEl) return;
                        const titleHtml = titleEl.innerHTML;
                        // OCS对齐: 使用id选择器并验证name匹配answertype+数字
                        const allTypes = timu.querySelectorAll('input[id^="answertype"]');
                        let qType = '0';
                        for (const inp of allTypes) {
                            if ((inp.name || '').match(/answertype\\d+/)) { qType = inp.value || '0'; break; }
                        }
                        if (qType === '0' && allTypes.length > 0) qType = allTypes[0].value || '0';
                        const options = [];
                        timu.querySelectorAll('ul li .after').forEach(el => {
                            options.push(el.innerHTML);
                        });
                        results.push({
                            question: titleHtml,
                            type: qType,
                            options: options,
                            html: timu.innerHTML.substring(0, 2000)
                        });
                    });
                    return results;
                """
            elif quiz_type == "2":
                extract_js = """
                    const items = _doc.querySelectorAll('.questionLi');
                    const results = [];
                    items.forEach(elem => {
                        const nameEl = elem.querySelector('.mark_name');
                        if (!nameEl) return;
                        let nameHtml = nameEl.innerHTML;
                        const idx = nameHtml.indexOf('</span>');
                        if (idx >= 0) nameHtml = nameHtml.substring(idx + 7);
                        // OCS对齐: 使用id选择器并验证name匹配
                        const allTypes = elem.querySelectorAll('input[id^="answertype"]');
                        let qType = '0';
                        for (const inp of allTypes) {
                            if ((inp.name || '').match(/answertype\\d+/)) { qType = inp.value || '0'; break; }
                        }
                        if (qType === '0' && allTypes.length > 0) qType = allTypes[0].value || '0';
                        const options = [];
                        elem.querySelectorAll('.answer_p').forEach(el => {
                            options.push(el.innerHTML);
                        });
                        results.push({
                            question: nameHtml,
                            type: qType,
                            options: options,
                            html: elem.innerHTML.substring(0, 2000)
                        });
                    });
                    return results;
                """
            else:
                extract_js = """
                    const body = _doc.body;
                    return [{
                        question: body ? body.textContent.substring(0, 500) : '',
                        type: '0',
                        options: [],
                        html: ''
                    }];
                """

            if iframe_frag:
                if 'modules/' in iframe_frag:
                    raw_questions = await self._eval_in_nested_module_iframe(
                        tab, iframe_frag, extract_js, deep=deep, iframe_index=iframe_index
                    )
                else:
                    raw_questions = await self._eval_in_iframe(tab, iframe_frag, extract_js)
                if raw_questions is None:
                    # iframe 找不到，回退到主页面
                    extract_js_main = extract_js.replace('_doc', 'document')
                    raw_questions = await tab.evaluate(f"(() => {{ {extract_js_main} }})()")
            else:
                # 替换 _doc 为 document
                extract_js_main = extract_js.replace('_doc', 'document')
                raw_questions = await tab.evaluate(f"(() => {{ {extract_js_main} }})()")

            questions = []
            # 根据 quiz_type 确定容器选择器
            container_sel = {"1": ".TiMu", "2": ".questionLi"}.get(quiz_type, ".TiMu")
            for rq in (raw_questions or []):
                q_text = _clean_question_text(_remove_html(rq.get('question', '')))
                if not q_text or len(q_text) < 2:
                    continue
                questions.append(QuestionData(
                    question=q_text,
                    question_type=rq.get('type', '0'),
                    options=[_remove_html(o) for o in rq.get('options', [])],
                    raw_html=rq.get('html', ''),
                    container_selector=container_sel,
                ))

            logger.info(f"提取到 {len(questions)} 道题目")
            return questions

        except Exception as e:
            logger.error(f"提取题目失败: {e}")
            return []

    async def _extract_questions_generic(
        self, tab: Tab, iframe_frag: str = "",
        deep: bool = False, iframe_index: int = 0,
    ) -> List[QuestionData]:
        """通用题目提取 - 当标准选择器都失败时使用"""
        try:
            extract_js = """
                const results = [];
                const typeInputs = _doc.querySelectorAll("input[id^='answertype']");
                typeInputs.forEach((input, idx) => {
                    let container = input.closest('.TiMu, .questionLi, .Py-mian1, [class*="question"], [class*="timu"]');
                    if (!container) container = input.parentElement;
                    if (!container) return;
                    let qText = '';
                    const titleEl = container.querySelector(
                        '.clearfix .fontLabel, .mark_name, .Py-m1-title, ' +
                        '[class*="title"], [class*="name"], .question-title'
                    );
                    if (titleEl) qText = titleEl.textContent.trim();
                    if (!qText) qText = container.textContent.trim().substring(0, 200);
                    const options = [];
                    container.querySelectorAll(
                        'ul li .after, ul.answerList li, .answer_p, .option-item'
                    ).forEach(el => options.push(el.textContent.trim()));
                    results.push({
                        question: qText,
                        type: input.value || '0',
                        options: options,
                        html: container.innerHTML.substring(0, 1000)
                    });
                });
                if (results.length === 0) {
                    const panels = _doc.querySelectorAll('.Py-mian1');
                    panels.forEach((panel, idx) => {
                        if (idx === 0) return;
                        const titleEl = panel.querySelector('.Py-m1-title');
                        if (!titleEl) return;
                        const qText = titleEl.textContent.trim();
                        const options = [];
                        panel.querySelectorAll('ul.answerList li.clearfix').forEach(
                            el => options.push(el.textContent.trim())
                        );
                        results.push({
                            question: qText, type: '0', options: options,
                            html: panel.innerHTML.substring(0, 1000)
                        });
                    });
                }
                return results;
            """
            if iframe_frag:
                if 'modules/' in iframe_frag:
                    raw = await self._eval_in_nested_module_iframe(
                        tab, iframe_frag, extract_js, deep=deep, iframe_index=iframe_index
                    )
                else:
                    raw = await self._eval_in_iframe(tab, iframe_frag, extract_js)
                if raw is None:
                    extract_js_main = extract_js.replace('_doc', 'document')
                    raw = await tab.evaluate(f"(() => {{ {extract_js_main} }})()")
            else:
                extract_js_main = extract_js.replace('_doc', 'document')
                raw = await tab.evaluate(f"(() => {{ {extract_js_main} }})()")

            questions = []
            for rq in (raw or []):
                q_text = _clean_question_text(_remove_html(rq.get('question', '')))
                if not q_text or len(q_text) < 2:
                    continue
                questions.append(QuestionData(
                    question=q_text,
                    question_type=rq.get('type', '0'),
                    options=rq.get('options', []),
                    raw_html=rq.get('html', ''),
                ))
            if questions:
                logger.info(f"通用提取得到 {len(questions)} 道题目")
            return questions
        except Exception as e:
            logger.error(f"通用题目提取失败: {e}")
            return []

    async def _extract_from_deep_work_iframe(
        self, tab: Tab, log: Callable = None, iframe_index: int = 0
    ) -> List[QuestionData]:
        """
        从 work iframe 内的子 iframe (第三层) 中提取题目。
        当题目位于 cards → work → quiz 三层 iframe 结构时使用。
        """
        _log = log or (lambda msg, level="info": logger.info(msg))
        try:
            # 在 work iframe 内查找子 iframe，并在子 iframe 中提取题目
            deep_extract_js = """
                const results = [];
                const iframes = _doc.querySelectorAll('iframe');
                for (const f of iframes) {
                    try {
                        const d = f.contentDocument || f.contentWindow.document;
                        // 策略1: .TiMu (章节测验)
                        const timus = d.querySelectorAll('.TiMu');
                        if (timus.length > 0) {
                            timus.forEach(timu => {
                                const titleEl = timu.querySelector('.clearfix .fontLabel')
                                    || timu.querySelector('[class*="fontLabel"]')
                                    || timu.querySelector('[class*="title"]');
                                if (!titleEl) return;
                                // OCS对齐: id选择器 + name验证
                                const allTypes = timu.querySelectorAll('input[id^="answertype"]');
                                let qType = '0';
                                for (const inp of allTypes) {
                                    if ((inp.name || '').match(/answertype\\d+/)) { qType = inp.value || '0'; break; }
                                }
                                if (qType === '0' && allTypes.length > 0) qType = allTypes[0].value || '0';
                                const options = [];
                                timu.querySelectorAll('ul li .after').forEach(el => {
                                    options.push(el.innerHTML);
                                });
                                results.push({
                                    question: titleEl.innerHTML,
                                    type: qType,
                                    options: options,
                                    html: timu.innerHTML.substring(0, 2000)
                                });
                            });
                            if (results.length > 0) break;
                        }
                        // 策略2: .questionLi (作业)
                        const qLis = d.querySelectorAll('.questionLi');
                        if (qLis.length > 0) {
                            qLis.forEach(elem => {
                                const nameEl = elem.querySelector('.mark_name');
                                if (!nameEl) return;
                                let nameHtml = nameEl.innerHTML;
                                const idx = nameHtml.indexOf('</span>');
                                if (idx >= 0) nameHtml = nameHtml.substring(idx + 7);
                                const allTypes = elem.querySelectorAll('input[id^="answertype"]');
                                let qType = '0';
                                for (const inp of allTypes) {
                                    if ((inp.name || '').match(/answertype\\d+/)) { qType = inp.value || '0'; break; }
                                }
                                if (qType === '0' && allTypes.length > 0) qType = allTypes[0].value || '0';
                                const options = [];
                                elem.querySelectorAll('.answer_p').forEach(el => {
                                    options.push(el.innerHTML);
                                });
                                results.push({
                                    question: nameHtml, type: qType,
                                    options: options,
                                    html: elem.innerHTML.substring(0, 2000)
                                });
                            });
                            if (results.length > 0) break;
                        }
                        // 策略3: 通用 answertype (id选择器)
                        const typeInputs = d.querySelectorAll("input[id^='answertype']");
                        if (typeInputs.length > 0) {
                            typeInputs.forEach(input => {
                                let container = input.closest('.TiMu, .questionLi, .Py-mian1, [class*="question"]');
                                if (!container) container = input.parentElement;
                                if (!container) return;
                                let qText = '';
                                const tEl = container.querySelector('.fontLabel, .mark_name, [class*="title"]');
                                if (tEl) qText = tEl.textContent.trim();
                                if (!qText) qText = container.textContent.trim().substring(0, 200);
                                const opts = [];
                                container.querySelectorAll('ul li .after, .answer_p').forEach(
                                    el => opts.push(el.textContent.trim())
                                );
                                results.push({
                                    question: qText, type: input.value || '0',
                                    options: opts, html: container.innerHTML.substring(0, 1000)
                                });
                            });
                            if (results.length > 0) break;
                        }
                    } catch(e) { /* cross-origin or access denied */ }
                }
                return results;
            """
            raw = await self._eval_in_nested_module_iframe(
                tab, "modules/work", deep_extract_js, iframe_index=iframe_index
            )
            if not raw:
                return []

            questions = []
            for rq in (raw or []):
                q_text = _clean_question_text(_remove_html(rq.get('question', '')))
                if not q_text or len(q_text) < 2:
                    continue
                questions.append(QuestionData(
                    question=q_text,
                    question_type=rq.get('type', '0'),
                    options=[_remove_html(o) for o in rq.get('options', [])],
                    raw_html=rq.get('html', ''),
                ))
            if questions:
                _log(f"深层iframe提取得到 {len(questions)} 道题目", "success")
                logger.info(f"深层 work iframe 提取得到 {len(questions)} 道题目")
            return questions
        except Exception as e:
            logger.error(f"深层 work iframe 提取失败: {e}")
            return []

    async def fill_answer(
        self, tab: Tab, question: QuestionData,
        answer: AnswerResult, elem_index: int,
        iframe_frag: str = "", deep: bool = False, iframe_index: int = 0,
    ) -> bool:
        """填写答案到页面"""
        try:
            answer_list = answer.answer_list
            if not answer_list:
                return False
            # 清理 AI 返回的 Markdown 格式
            answer_list = [_clean_ai_answer(a) for a in answer_list]
            q_type = question.question_type
            sel = question.container_selector
            if q_type in ("0", "1"):
                return await self._fill_choice_answer(
                    tab, question, answer_list, elem_index, iframe_frag, deep=deep, iframe_index=iframe_index)
            elif q_type == "3":
                return await self._fill_judge_answer(
                    tab, answer_list, elem_index, iframe_frag, deep=deep, iframe_index=iframe_index,
                    container_selector=sel)
            elif q_type in ("2", "4", "5", "6", "7", "9"):
                return await self._fill_text_answer(
                    tab, answer_list, elem_index, iframe_frag, deep=deep, iframe_index=iframe_index,
                    container_selector=sel)
            return False
        except Exception as e:
            logger.error(f"填写答案失败: {e}")
            return False

    async def _fill_random_answer(
        self, tab: Tab, question: QuestionData, elem_index: int,
        iframe_frag: str = "", deep: bool = False, iframe_index: int = 0,
    ) -> bool:
        """
        4.2 答题失败时随机作答，避免卡住

        策略:
        - 选择题: 随机点击一个选项
        - 判断题: 随机选对或错
        - 填空/简答题: 从预设文案库随机选一个填入
        """
        q_type = question.question_type
        sel = question.container_selector

        # 预设文案库 (填空题用)
        fallback_texts = [
            "正确", "对", "是", "以上都对",
            "不确定", "需要进一步分析",
            "根据所学知识，答案如上所述",
        ]

        try:
            if q_type in ("0", "1"):
                # 选择题: 随机点击一个选项 (OCS风格: 优先级点击)
                random_js = f"""
                    (() => {{
                        const items = _doc.querySelectorAll('{sel}');
                        if ({elem_index} >= items.length) return false;
                        const container = items[{elem_index}];
                        const lis = Array.from(container.querySelectorAll('ul li'));
                        if (lis.length === 0) return false;
                        const li = lis[Math.floor(Math.random() * lis.length)];
                        // OCS风格优先级点击
                        const target = li.querySelector('.answer_p') || li.querySelector('.after') ||
                            li.querySelector('a') || li.querySelector('label') ||
                            li.querySelector('.num_option') || li;
                        try {{ target.click(); }} catch(e) {{}}
                        return true;
                    }})()
                """
            elif q_type == "3":
                # 判断题: 随机选对或错
                is_true = random.choice([True, False])
                is_true_js = 'true' if is_true else 'false'
                random_js = f"""
                    (() => {{
                        const items = _doc.querySelectorAll('{sel}');
                        if ({elem_index} >= items.length) return false;
                        const container = items[{elem_index}];
                        const lis = container.querySelectorAll('ul li, .answerList li');
                        if (lis.length < 2) return false;
                        const target = {is_true_js} ? lis[0] : lis[1];
                        target.click();
                        return true;
                    }})()
                """
            else:
                # 填空/简答: 填入预设文案
                text = random.choice(fallback_texts)
                import json as _json
                text_json = _json.dumps(text)
                random_js = f"""
                    (() => {{
                        const items = _doc.querySelectorAll('{sel}');
                        if ({elem_index} >= items.length) return false;
                        const container = items[{elem_index}];
                        const textarea = container.querySelector('textarea, .ql-editor, [contenteditable="true"]');
                        if (textarea) {{
                            textarea.innerHTML = {text_json};
                            textarea.dispatchEvent(new Event('input', {{bubbles: true}}));
                            return true;
                        }}
                        return false;
                    }})()
                """

            if iframe_frag and 'modules/' in iframe_frag:
                result = await self._eval_in_nested_module_iframe(
                    tab, iframe_frag, random_js, deep=deep, iframe_index=iframe_index
                )
            elif iframe_frag:
                result = await self._eval_in_iframe(tab, iframe_frag, random_js)
            else:
                random_js_main = random_js.replace('_doc', 'document')
                result = await tab.evaluate(f"(() => {{ {random_js_main} }})()")

            return bool(result)

        except Exception as e:
            logger.debug(f"随机作答异常: {e}")
            return False

    async def _eval_in_context(self, tab: Tab, iframe_frag: str, js: str, deep: bool = False, iframe_index: int = 0) -> Any:
        """在 iframe 或主页面中执行 JS, iframe 找不到时自动回退到主页面"""
        if iframe_frag:
            # 对 modules/* 路径使用嵌套 iframe 查找
            if 'modules/' in iframe_frag:
                result = await self._eval_in_nested_module_iframe(tab, iframe_frag, js, deep=deep, iframe_index=iframe_index)
                if result is not None:
                    return result
            else:
                result = await self._eval_in_iframe(tab, iframe_frag, js)
                if result is not None:
                    return result
            # iframe 没找到，回退到主页面
            logger.debug(f"_eval_in_context: iframe({iframe_frag}) 未找到，回退到主页面")
        main_js = js.replace('_doc', 'document').replace('_win', 'window')
        return await tab.evaluate(f"(() => {{ {main_js} }})()")

    async def _eval_in_video_player(self, tab: Tab, js: str, iframe_index: int = 0) -> Any:
        """在视频播放器 iframe 中执行 JS (主页面 → cards iframe → video iframe → <video>)"""
        return await self._eval_in_nested_module_iframe(tab, "modules/video", js, iframe_index=iframe_index)

    async def _eval_in_nested_module_iframe(self, tab: Tab, module_frag: str, js: str, deep: bool = False, iframe_index: int = 0) -> Any:
        """
        在嵌套的模块 iframe 中执行 JS。
        遍历路径: 主页面 → cards/knowledge iframe → module iframe (如 modules/video, modules/audio)
        _doc 指向最内层 module iframe 的 document
        
        deep=True: 如果模块 iframe 内有子 iframe，优先在子 iframe 中查找 .TiMu/.questionLi 元素。
                   如果子 iframe 中未找到目标元素，回退到模块 iframe 本身。
        iframe_index: 当页面有多个同类模块 iframe 时，指定第几个(从0开始)。
        """
        mf = module_frag.replace("'", "\\'")
        target_idx = iframe_index

        # 关键修复: 如果 JS 是 IIFE (()=>{...})()，剥离包装使其成为普通函数体
        # 这样代码中的 return 语句就能被外层 (function(){...}).call(null) 正确捕获
        # 否则 IIFE 在 function body 内执行时返回值会丢失（变成 undefined）
        _effective_js = js.strip()
        _iife_markers = ['(() => {', '(() =>{', '(()=> {', '(()=>{']
        for _marker in _iife_markers:
            if _effective_js.startswith(_marker) and _effective_js.endswith('})()'):
                _effective_js = _effective_js[len(_marker):].rstrip()
                if _effective_js.endswith('})()'):
                    _effective_js = _effective_js[:-4].rstrip()
                break
        # deep 模式: 在模块 iframe 的子 iframe 中查找，如果子 iframe 有目标元素则优先使用子 iframe
        if deep:
            deep_wrapped = f"""
            (() => {{
                const allIframes = document.querySelectorAll('iframe');
                // 找到模块 iframe (按 index 选择第 {target_idx} 个匹配的)
                let moduleIframe = null;
                let matchCount = 0;
                for (const cardsIframe of allIframes) {{
                    const src = cardsIframe.src || cardsIframe.getAttribute('src') || '';
                    if (src.includes('cards') || src.includes('knowledge')) {{
                        try {{
                            const cardsDoc = cardsIframe.contentDocument || cardsIframe.contentWindow.document;
                            for (const child of cardsDoc.querySelectorAll('iframe')) {{
                                const cs = child.src || child.getAttribute('src') || '';
                                if (cs.includes('{mf}')) {{
                                    if (matchCount === {target_idx}) {{ moduleIframe = child; break; }}
                                    matchCount++;
                                }}
                            }}
                        }} catch(e) {{}}
                        if (moduleIframe) break;
                    }}
                }}
                if (!moduleIframe) {{
                    matchCount = 0;
                    for (const f of allIframes) {{
                        const s = f.src || f.getAttribute('src') || '';
                        if (s.includes('{mf}')) {{
                            if (matchCount === {target_idx}) {{ moduleIframe = f; break; }}
                            matchCount++;
                        }}
                    }}
                    // 如果通过 src 匹配未找到，尝试搜索所有 iframe 查找题目
                    if (!moduleIframe && '{mf}' === 'modules/work') {{
                        function deepSearchFrames(doc, depth) {{
                            if (depth > 4) return null;
                            const frames = doc.querySelectorAll('iframe');
                            for (const f of frames) {{
                                try {{
                                    const d = f.contentDocument || f.contentWindow.document;
                                    if (d && (d.querySelector('.TiMu') || d.querySelector('input[name^="answertype"]'))) return f;
                                    const found = deepSearchFrames(d, depth + 1);
                                    if (found) return found;
                                }} catch(e) {{}}
                            }}
                            return null;
                        }}
                        const quizFrame = deepSearchFrames(document, 0);
                        if (quizFrame) {{
                            moduleIframe = quizFrame;
                        }}
                    }}
                }}
                if (!moduleIframe) return null;
                try {{
                    const modDoc = moduleIframe.contentDocument || moduleIframe.contentWindow.document;

                    // 递归搜索函数: 在 iframe 树中查找题目元素 (最多 4 层)
                    function findQuizDoc(doc, depth) {{
                        if (depth > 4) return null;
                        if (!doc) return null;
                        // 检查当前文档是否有题目
                        if (doc.querySelector('.TiMu') || doc.querySelector('.questionLi') ||
                            doc.querySelector('input[name^="answertype"]')) {{
                            return doc;
                        }}
                        // 递归搜索子 iframe
                        const iframes = doc.querySelectorAll('iframe');
                        for (const f of iframes) {{
                            try {{
                                const subDoc = f.contentDocument || f.contentWindow.document;
                                if (subDoc && subDoc.body) {{
                                    const found = findQuizDoc(subDoc, depth + 1);
                                    if (found) return found;
                                }}
                            }} catch(e) {{}}
                        }}
                        return null;
                    }}

                    // 尝试查找 quiz document
                    let quizDoc = findQuizDoc(modDoc, 0);

                    // 如果未找到，也尝试从主页面的所有 iframe 搜索
                    if (!quizDoc) {{
                        quizDoc = findQuizDoc(document, 0);
                    }}

                    if (quizDoc) {{
                        const _doc = quizDoc;
                        const _win = quizDoc.defaultView || quizDoc.parentWindow;
                        return (function() {{ {_effective_js} }}).call(null);
                    }}

                    // 未找到题目元素: 尝试在每个可访问的子 iframe 中执行
                    const allFrames = modDoc.querySelectorAll('iframe');
                    for (const frame of allFrames) {{
                        try {{
                            const fDoc = frame.contentDocument || frame.contentWindow.document;
                            if (fDoc && fDoc.body) {{
                                const _doc = fDoc;
                                const _win = frame.contentWindow;
                                const result = (function() {{ {_effective_js} }}).call(null);
                                // 如果返回了有效结果 (非 null/undefined 且未失败)，则使用它
                                if (result !== null && result !== undefined) {{
                                    if (typeof result !== 'object' || result.ok !== false) {{
                                        return result;
                                    }}
                                }}
                            }}
                        }} catch(e) {{}}
                    }}

                    // 最终回退: 模块 iframe 本身
                    const _doc = modDoc;
                    const _win = moduleIframe.contentWindow;
                    return (function() {{ {_effective_js} }}).call(null);
                }} catch(e) {{
                    return {{__iframe_error__: e.message}};
                }}
            }})()
            """
            try:
                result = await tab.evaluate(deep_wrapped)
                if isinstance(result, dict) and '__iframe_error__' in result:
                    logger.debug(f"deep nested module iframe 执行错误({module_frag}): {result['__iframe_error__']}")
                    return None
                return result
            except Exception as e:
                logger.debug(f"deep nested module iframe 失败({module_frag}): {e}")
                return None
        wrapped = f"""
        (() => {{
            // 策略1: 主页面 → cards iframe → module iframe (按 index 选择第 {target_idx} 个)
            const allIframes = document.querySelectorAll('iframe');
            let matchCount = 0;
            for (const cardsIframe of allIframes) {{
                const src = cardsIframe.src || cardsIframe.getAttribute('src') || '';
                if (src.includes('cards') || src.includes('knowledge')) {{
                    try {{
                        const cardsDoc = cardsIframe.contentDocument || cardsIframe.contentWindow.document;
                        const childIframes = cardsDoc.querySelectorAll('iframe');
                        for (const child of childIframes) {{
                            const childSrc = child.src || child.getAttribute('src') || '';
                            if (childSrc.includes('{mf}')) {{
                                if (matchCount === {target_idx}) {{
                                    const _doc = child.contentDocument || child.contentWindow.document;
                                    const _win = child.contentWindow;
                                    if (_doc) {{
                                        return (function() {{ {_effective_js} }}).call(null);
                                    }}
                                }}
                                matchCount++;
                            }}
                        }}
                    }} catch(e) {{
                        return {{__iframe_error__: e.message}};
                    }}
                }}
            }}
            // 策略2: 直接在主页面找 module iframe
            matchCount = 0;
            for (const iframe of allIframes) {{
                const src = iframe.src || iframe.getAttribute('src') || '';
                if (src.includes('{mf}')) {{
                    if (matchCount === {target_idx}) {{
                        try {{
                            const _doc = iframe.contentDocument || iframe.contentWindow.document;
                            const _win = iframe.contentWindow;
                            if (_doc) {{
                                return (function() {{ {_effective_js} }}).call(null);
                            }}
                        }} catch(e) {{
                            return {{__iframe_error__: e.message}};
                        }}
                    }}
                    matchCount++;
                }}
            }}
            return null;
        }})()
        """
        try:
            result = await tab.evaluate(wrapped)
            if isinstance(result, dict) and '__iframe_error__' in result:
                logger.debug(f"nested module iframe 执行错误({module_frag}): {result['__iframe_error__']}")
                return None
            return result
        except Exception as e:
            logger.debug(f"nested module iframe evaluate 失败({module_frag}): {e}")
            return None

    async def _fill_choice_answer(
        self, tab: Tab, question: QuestionData,
        answer_list: List[str], elem_index: int,
        iframe_frag: str = "", deep: bool = False, iframe_index: int = 0,
    ) -> bool:
        """填写选择/多选题答案 (与原脚本 matchAnswer + setAnswer 行为对齐)"""
        try:
            sel = question.container_selector

            # === 匹配答案到选项 (与原脚本 matchAnswer 一致: 精确匹配优先) ===
            matched_indices = []
            for ans in answer_list:
                ans_clean = _remove_html(ans).strip().lower()
                found = False
                # 第一轮: 精确匹配
                for i, opt in enumerate(question.options):
                    opt_clean = _remove_html(opt).strip().lower()
                    if ans_clean == opt_clean:
                        matched_indices.append(i)
                        found = True
                        break
                if found:
                    continue
                # 第二轮: 子串匹配 (双方都 >= 4 字符)
                for i, opt in enumerate(question.options):
                    opt_clean = _remove_html(opt).strip().lower()
                    if len(ans_clean) >= 4 and len(opt_clean) >= 4:
                        if ans_clean in opt_clean or opt_clean in ans_clean:
                            matched_indices.append(i)
                            found = True
                            break
                if found:
                    continue
                # 第三轮: 字母答案回退 (支持 A-Z，DeepSeek 返回字母但 _parse 映射失败)
                letter_match = re.match(r'^([A-Za-z])$', ans.strip())
                if letter_match:
                    idx = ord(letter_match.group(1).upper()) - 65
                    if 0 <= idx < len(question.options):
                        matched_indices.append(idx)
                        logger.debug(f"选择题字母回退: {ans.strip()} -> index {idx}")
                        continue
                # 第四轮: 多字母答案回退 (支持 A-Z，如 "ACEJ" 或 "A,C,E,J" 未被解析)
                letters = re.findall(r'[A-Za-z]', ans.strip())
                if len(letters) >= 2:
                    for ch in letters:
                        idx = ord(ch.upper()) - 65
                        if 0 <= idx < len(question.options) and idx not in matched_indices:
                            matched_indices.append(idx)
                    logger.debug(f"选择题多字母回退: {ans.strip()} -> indices {letters}")

            if not matched_indices:
                logger.warning(
                    f"选择题答案匹配失败: answer={answer_list}, "
                    f"options={[o[:30] for o in question.options[:4]]}"
                )
                return False

            logger.info(
                f"选择题匹配: indices={matched_indices}, "
                f"answers={[a[:30] for a in answer_list]}, opts={len(question.options)}"
            )
            if question.question_type == "1" and len(matched_indices) < 2:
                logger.warning(
                    f"多选题只匹配到 {len(matched_indices)} 个选项！"
                    f"answer_list={[a[:40] for a in answer_list]}, "
                    f"question.options={[o[:40] for o in question.options]}"
                )

            # === 填写答案: 点击 + 直接设置 hidden input 双保险 ===
            indices_json = json.dumps(matched_indices)
            fill_and_verify_js = f"""
                (() => {{
                    const items = _doc.querySelectorAll('{sel}');
                    if ({elem_index} >= items.length) return {{
                        ok:false, reason:'elem_index out of range',
                        elemIndex:{elem_index}, itemsLen:items.length
                    }};
                    const item = items[{elem_index}];
                    const targetIndices = {indices_json};

                    // --- 获取 LI 列表 ---
                    const firstUl = item.querySelector('ul');
                    if (!firstUl) return {{ok:false, reason:'no ul found'}};
                    const lis = Array.from(firstUl.querySelectorAll('li'));
                    if (lis.length === 0) return {{ok:false, reason:'no li found'}};

                    // --- 诊断: 收集 DOM 结构信息 ---
                    const diag = {{
                        lisCount: lis.length,
                        firstLiHtml: (lis[0] ? lis[0].innerHTML.substring(0, 300) : ''),
                        allInputs: Array.from(item.querySelectorAll('input')).map(i =>
                            `${{i.type}}|${{i.name}}|${{i.value.substring(0,30)}}`
                        ),
                        allClasses: Array.from(new Set(
                            Array.from(item.querySelectorAll('*')).flatMap(e =>
                                Array.from(e.classList)
                            ).filter(c => c.includes('answer') || c.includes('check') || c.includes('option') || c.includes('num'))
                        )).slice(0, 20)
                    }};

                    // --- 1. 点击目标选项元素 ---
                    let clicked = 0;
                    const clickResults = [];
                    for (const idx of targetIndices) {{
                        if (idx >= lis.length) {{
                            clickResults.push({{idx, status:'out-of-range'}});
                            continue;
                        }}
                        const li = lis[idx];
                        const target =
                            li.querySelector('.answer_p') ||
                            li.querySelector('.after') ||
                            li.querySelector('a') ||
                            li.querySelector('label') ||
                            li.querySelector('.num_option') ||
                            li;
                        const tag = target ? target.tagName +
                            (target.className ? '.' + target.className.split(' ')[0] : '') : 'null';
                        try {{ target.click(); }} catch(e) {{}}
                        clicked++;
                        clickResults.push({{idx, status:'clicked', tag}});
                    }}

                    // --- 2. 直接设置 hidden input (兜底) ---
                    // 从 num_option 的 data 属性或选项字母构建值
                    let choiceLetters = '';
                    for (const idx of targetIndices) {{
                        if (idx < lis.length) {{
                            const numOpt = lis[idx].querySelector('.num_option');
                            if (numOpt) {{
                                const d = numOpt.getAttribute('data') || '';
                                choiceLetters += d;
                            }} else {{
                                choiceLetters += String.fromCharCode(65 + idx);
                            }}
                        }}
                    }}
                    const hiddenInput = item.querySelector('input[type="hidden"][name^="answer"]');
                    if (hiddenInput && choiceLetters) {{
                        hiddenInput.value = choiceLetters;
                        try {{
                            hiddenInput.dispatchEvent(new Event('change', {{bubbles:true}}));
                            hiddenInput.dispatchEvent(new Event('input', {{bubbles:true}}));
                        }} catch(e) {{}}
                    }}

                    // --- 3. 验证 ---
                    const checked = item.querySelectorAll('[class*="check_answer"]');
                    const ariaChecked = item.querySelectorAll('[aria-checked="true"]');
                    const finalHidden = hiddenInput ? hiddenInput.value : '';
                    // 宽松验证: 点击成功 或 有任何选中指示 或 hidden 有值
                    const ok = clicked > 0 || checked.length > 0 ||
                               ariaChecked.length > 0 || finalHidden.length > 0;

                    return {{
                        ok: ok,
                        clicked: clicked,
                        checkedCount: checked.length,
                        ariaCount: ariaChecked.length,
                        hiddenValue: hiddenInput ? finalHidden : 'no-hidden-input',
                        choiceLetters: choiceLetters,
                        diag: diag,
                        details: clickResults
                    }};
                }})()
            """
            result = await self._eval_in_context(
                tab, iframe_frag, fill_and_verify_js, deep=deep, iframe_index=iframe_index
            )
            if result and isinstance(result, dict):
                diag = result.get('diag', {})
                details = result.get('details', [])
                if result.get('ok'):
                    logger.info(
                        f"选择题填写成功: clicked={result.get('clicked')}, "
                        f"checked={result.get('checkedCount')}, aria={result.get('ariaCount')}, "
                        f"hidden={str(result.get('hiddenValue',''))[:30]}, "
                        f"letters={result.get('choiceLetters','')}, "
                        f"classes={diag.get('allClasses', [])[:10]}, "
                        f"details={details}"
                    )
                    await asyncio.sleep(0.2)
                    return True
                else:
                    reason = result.get('reason', 'unknown')
                    if 'itemsLen' in result:
                        logger.warning(
                            f"选择题填写失败: reason={reason}, "
                            f"idx={result.get('elemIndex')}, items={result.get('itemsLen')}"
                        )
                    else:
                        logger.warning(
                            f"选择题填写失败: reason={reason}, "
                            f"clicked={result.get('clicked')}, "
                            f"lisCount={diag.get('lisCount')}, "
                            f"firstLiHtml={diag.get('firstLiHtml','')[:200]}, "
                            f"inputs={diag.get('allInputs', [])}, "
                            f"classes={diag.get('allClasses', [])[:15]}, "
                            f"details={details}"
                        )
                    return False
            else:
                # _eval_in_context 返回 None/undefined (可能在错误的上下文执行)
                logger.warning(f"选择题填写返回空结果: {result!r}")
                return False
        except Exception as e:
            logger.error(f"填写选择题答案失败: {e}")
            return False

    async def _fill_judge_answer(
        self, tab: Tab, answer_list: List[str],
        elem_index: int, iframe_frag: str = "", deep: bool = False, iframe_index: int = 0,
        container_selector: str = ".TiMu",
    ) -> bool:
        """填写判断题答案 (与原脚本 setAnswer case "3" 行为对齐)"""
        try:
            sel = container_selector
            answer_text = answer_list[0] if answer_list else ""
            is_true = bool(re.search(
                r'正确|对|是|√|true|T|ri', answer_text, re.IGNORECASE
            ))
            is_true_js = 'true' if is_true else 'false'

            # 新版超星DOM: 判断题无input[type=radio/checkbox]
            # 结构: <li onclick="addChoice(this)"> + <span class="num_option" data="true/false">
            fill_js = f"""
                (() => {{
                    const items = _doc.querySelectorAll('{sel}');
                    if ({elem_index} >= items.length) return {{
                        ok:false, reason:'elem_index out of range',
                        elemIndex:{elem_index}, itemsLen:items.length,
                        url:(_doc.location||{{}}).href||'unknown',
                        bodyLen:_doc.body?_doc.body.innerHTML.length:0,
                        hasTiMu:!!_doc.querySelector('.TiMu'),
                        hasAnsType:!!_doc.querySelector('input[name^="answertype"]'),
                        title:(_doc.title||'').substring(0,50)
                    }};
                    const item = items[{elem_index}];
                    const isTrue = {is_true_js};

                    // --- 获取 li 列表 ---
                    const firstUl = item.querySelector('ul');
                    const lis = firstUl
                        ? Array.from(firstUl.querySelectorAll('li'))
                        : Array.from(item.querySelectorAll('ul li'));

                    // --- 找到目标 li (通过 num_option data 属性) ---
                    let targetLi = null;
                    let targetNumOpt = null;
                    for (const li of lis) {{
                        const numOpt = li.querySelector('.num_option');
                        if (!numOpt) continue;
                        const data = (numOpt.getAttribute('data') || '').toLowerCase();
                        if (isTrue && data === 'true') {{ targetLi = li; targetNumOpt = numOpt; break; }}
                        if (!isTrue && (data === 'false' || data === 'wr')) {{ targetLi = li; targetNumOpt = numOpt; break; }}
                    }}
                    // 回退: 文本匹配
                    if (!targetLi) {{
                        for (const li of lis) {{
                            const text = li.textContent.trim();
                            if (isTrue && (text.includes('对') || text.includes('正确') || text.includes('√'))) {{
                                targetLi = li; targetNumOpt = li.querySelector('.num_option'); break;
                            }}
                            if (!isTrue && (text.includes('错') || text.includes('错误') || text.includes('×'))) {{
                                targetLi = li; targetNumOpt = li.querySelector('.num_option'); break;
                            }}
                        }}
                    }}
                    // 回退: 索引 (0=对, 1=错)
                    if (!targetLi && lis.length >= 2) {{
                        targetLi = lis[isTrue ? 0 : 1];
                        targetNumOpt = targetLi.querySelector('.num_option');
                    }}
                    if (!targetLi) return {{ok:false, reason:'no target li found', lisCount:lis.length}};

                    // --- 直接操作 DOM (绕过 addChoice, 最可靠) ---
                    // 1. 清除所有选项的 check_answer
                    item.querySelectorAll('.num_option.check_answer, .num_option.check_answer_dx').forEach(el => {{
                        el.classList.remove('check_answer', 'check_answer_dx');
                    }});
                    // 2. 清除所有 li 的 aria-checked
                    lis.forEach(li => {{
                        li.setAttribute('aria-checked', 'false');
                        li.setAttribute('aria-pressed', 'false');
                    }});
                    // 3. 设置目标为选中
                    if (targetNumOpt) {{
                        targetNumOpt.classList.add('check_answer');
                    }}
                    targetLi.setAttribute('aria-checked', 'true');
                    targetLi.setAttribute('aria-pressed', 'true');
                    // 4. 更新 hidden input (关键! 提交时服务器读取此值)
                    const hiddenInput = item.querySelector('input[type="hidden"][name^="answer"]');
                    if (hiddenInput) {{
                        hiddenInput.value = isTrue ? 'true' : 'false';
                    }}
                    // 5. 不再调用 targetLi.click() - addChoice 会 toggle 反向取消选择!

                    // --- 最终验证 ---
                    const hasCheck = item.querySelector('.check_answer, .check_answer_dx');
                    const targetSelected = targetLi.getAttribute('aria-checked') === 'true';
                    const hiddenVal = hiddenInput ? hiddenInput.value : 'no-hidden';
                    return {{
                        ok: hasCheck !== null && targetSelected,
                        targetSelected: targetSelected,
                        hasCheckClass: hasCheck !== null,
                        hiddenValue: hiddenVal,
                        lisCount: lis.length
                    }};
                }})()
            """
            result = await self._eval_in_context(
                tab, iframe_frag, fill_js, deep=deep, iframe_index=iframe_index
            )
            if result and isinstance(result, dict):
                if result.get('ok'):
                    logger.debug(
                        f"判断题填写验证通过: isTrue={is_true}, "
                        f"lis={result.get('lisCount')}"
                    )
                    await asyncio.sleep(0.3)
                    return True
                else:
                    reason = result.get('reason', 'not checked')
                    diag = ''
                    if 'itemsLen' in result:
                        diag = (f", idx={result.get('elemIndex')}, items={result.get('itemsLen')}, "
                                f"hasTiMu={result.get('hasTiMu')}, hasAnsType={result.get('hasAnsType')}, "
                                f"url={result.get('url', '?')[:80]}, bodyLen={result.get('bodyLen')}")
                    logger.warning(
                        f"判断题填写验证失败: isTrue={is_true}, reason={reason}, "
                        f"clicked={result.get('clicked')}, targetSelected={result.get('targetSelected')}, "
                        f"hasCheckClass={result.get('hasCheckClass')}, hasAriaChecked={result.get('hasAriaChecked')}, "
                        f"lisCount={result.get('lisCount')}{diag}"
                    )
                    return False
            else:
                logger.warning(f"判断题填写返回空结果: {result!r}")
                return False
        except Exception as e:
            logger.error(f"填写判断题答案失败: {e}")
            return False

    async def _fill_text_answer(
        self, tab: Tab, answer_list: List[str],
        elem_index: int, iframe_frag: str = "", deep: bool = False, iframe_index: int = 0,
        container_selector: str = ".TiMu",
    ) -> bool:
        """填写文本类题目答案"""
        try:
            sel = container_selector
            import json as _json
            answers_json = _json.dumps(answer_list)
            fill_js = f"""
                const answers = {answers_json};
                const items = _doc.querySelectorAll('{sel}');
                if ({elem_index} >= items.length) return;
                const item = items[{elem_index}];
                const textareas = item.querySelectorAll('textarea');
                textareas.forEach((ta, i) => {{
                    if (i < answers.length) {{
                        const ans = answers[i].replace(/第.空:/g, '');
                        if (typeof UE !== 'undefined' && UE.getEditor) {{
                            try {{
                                const editor = UE.getEditor(ta.name);
                                editor.ready(function() {{ this.setContent(ans); }});
                                return;
                            }} catch(e) {{}}
                        }}
                        ta.value = ans;
                        ta.dispatchEvent(new Event('input', {{bubbles: true}}));
                    }}
                }});
                return true;
            """
            result = await self._eval_in_context(tab, iframe_frag, fill_js, deep=deep, iframe_index=iframe_index)
            return result is not None  # 根据 JS 执行结果判断是否成功
        except Exception as e:
            logger.error(f"填写文本答案失败: {e}")
            return False

    async def submit_quiz(self, tab: Tab, iframe_frag: str = "", deep: bool = False, iframe_index: int = 0) -> bool:
        """提交测验答案 - 用 zendriver Element.click() 模拟真实用户点击
            
        超星测验提交链路：
          btnBlueSubmit() → validateTimeNew() (异步AJAX验证) → 弹窗
          → submitCheckTimes() → form1submit() → confirmSubmitWork() (真正AJAX提交)
            
        使用 zendriver 的 DOM 树搜索 + Element.click()（CDP user_gesture=True）。
        浏览器已配置 CDP 级别的弹窗自动关闭（accept=True）。
        """
        try:
            # 第一步：前置准备 —— 重置暂存标记 + 诊断
            prep_result = await tab.evaluate("""
                (() => {
                    function findQuizDoc(doc, depth) {
                        if (depth > 5 || !doc) return null;
                        if (doc.querySelector('#form1')) return doc;
                        var iframes = doc.querySelectorAll('iframe');
                        for (var i = 0; i < iframes.length; i++) {
                            try {
                                var subDoc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                                if (subDoc && subDoc.body) {
                                    var found = findQuizDoc(subDoc, depth + 1);
                                    if (found) return found;
                                }
                            } catch(e) {}
                        }
                        return null;
                    }
                    var quizDoc = findQuizDoc(document, 0);
                    if (!quizDoc) return {ok: false, reason: 'quiz form #form1 not found'};
                    var quizWin = quizDoc.defaultView || quizDoc.parentWindow;
                    try { if (typeof quizWin.submitLock !== 'undefined') quizWin.submitLock = 0; } catch(e) {}
                    try { if (typeof quizWin.tempSave !== 'undefined') quizWin.tempSave = false; } catch(e) {}
                    var pyFlag = quizDoc.querySelector('#pyFlag');
                    if (pyFlag) pyFlag.value = '0';
                    return { ok: true, pyFlag: pyFlag ? pyFlag.value : 'N/A' };
                })()
            """)
            logger.info(f"测验提交前置: {prep_result}")
            if not prep_result or not prep_result.get('ok'):
                logger.error(f"前置准备失败: {prep_result}")
                return False
    
            # 第二步：用 zendriver DOM 搜索找到提交按钮并 Element.click()
            # 超星测验提交按钮: <a class="btnSubmit workBtnIndex" onclick="btnBlueSubmit()">提交</a>
            submit_btn = None
            # 优先用 .btnSubmit（真正的提交按钮）
            try:
                btns = await tab.query_selector_all('.btnSubmit')
                if btns:
                    submit_btn = btns[0]
                    logger.info("找到 .btnSubmit")
            except Exception as e:
                logger.debug(f"query .btnSubmit: {e}")
            # 在 iframe 内搜索
            if not submit_btn:
                try:
                    iframes = await tab.query_selector_all('iframe')
                    for iframe_el in iframes:
                        try:
                            if iframe_el.node_name == "IFRAME" and iframe_el.content_document:
                                for sel in ['.btnSubmit', '#submitBtn', '.btnBlue']:
                                    inner = await tab.query_selector_all(sel, iframe_el)
                                    if inner:
                                        submit_btn = inner[0]
                                        logger.info(f"在 iframe 中找到 {sel}")
                                        break
                                if submit_btn:
                                    break
                        except Exception:
                            continue
                except Exception as e:
                    logger.debug(f"iframe 搜索: {e}")
            # 最后尝试按文字找
            if not submit_btn:
                try:
                    elem = await tab.find("提交", best_match=True, timeout=3)
                    if elem:
                        submit_btn = elem
                        logger.info("通过文字找到提交按钮")
                except Exception:
                    pass
    
            if submit_btn:
                # 用 zendriver 的真实 CDP 点击（user_gesture=True）
                try:
                    await submit_btn.click()
                    logger.info("已通过 Element.click() 点击提交按钮 (CDP user_gesture)")
                except Exception as e:
                    logger.warning(f"Element.click() 失败: {e}，降级为 JS click")
                    await tab.evaluate("""
                        (() => {
                            function findQuizDoc(doc, depth) {
                                if (depth > 5 || !doc) return null;
                                if (doc.querySelector('#form1')) return doc;
                                var iframes = doc.querySelectorAll('iframe');
                                for (var i = 0; i < iframes.length; i++) {
                                    try {
                                        var subDoc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                                        if (subDoc && subDoc.body) {
                                            var found = findQuizDoc(subDoc, depth + 1);
                                            if (found) return found;
                                        }
                                    } catch(e) {}
                                }
                                return null;
                            }
                            var quizDoc = findQuizDoc(document, 0);
                            if (!quizDoc) return;
                            var quizWin = quizDoc.defaultView || quizDoc.parentWindow;
                            if (typeof quizWin.btnBlueSubmit === 'function') quizWin.btnBlueSubmit();
                        })()
                    """)
            else:
                # 找不到按钮，直接调用 btnBlueSubmit
                logger.warning("未找到提交按钮元素，降级调用 btnBlueSubmit")
                await tab.evaluate("""
                    (() => {
                        function findQuizDoc(doc, depth) {
                            if (depth > 5 || !doc) return null;
                            if (doc.querySelector('#form1')) return doc;
                            var iframes = doc.querySelectorAll('iframe');
                            for (var i = 0; i < iframes.length; i++) {
                                try {
                                    var subDoc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                                    if (subDoc && subDoc.body) {
                                        var found = findQuizDoc(subDoc, depth + 1);
                                        if (found) return found;
                                    }
                                } catch(e) {}
                            }
                            return null;
                        }
                        var quizDoc = findQuizDoc(document, 0);
                        if (!quizDoc) return;
                        var quizWin = quizDoc.defaultView || quizDoc.parentWindow;
                        if (typeof quizWin.btnBlueSubmit === 'function') quizWin.btnBlueSubmit();
                    })()
                """)
    
            # 第三步：等待确认弹窗出现并点击"确定"按钮
            # 超星确认弹窗是预渲染 HTML，不是 layui：
            #   <a class="bluebtn" onclick="submitCheckTimes();">确定</a>
            #   <a class="btnGray_1 marleft10" onclick="hideWindow();">取消</a>
            confirmed = False
            for wait_round in range(20):
                await asyncio.sleep(0.5)
                # 在所有 frame 中搜索确认弹窗的提交/确定按钮
                find_and_click = await tab.evaluate("""
                    (() => {
                        function isVisible(el, doc) {
                            var p = el;
                            while (p && p !== doc.body && p !== doc.documentElement) {
                                try {
                                    if (p.style && p.style.display === 'none') return false;
                                    if (p.style && p.style.visibility === 'hidden') return false;
                                } catch(e) {}
                                p = p.parentElement;
                            }
                            return true;
                        }
                        function searchAndClick(doc, depth) {
                            if (depth > 5 || !doc) return null;
                            // 策略1: .bluebtn + submitCheckTimes
                            var bluebtns = doc.querySelectorAll('.bluebtn');
                            for (var i = 0; i < bluebtns.length; i++) {
                                var onclick = bluebtns[i].getAttribute('onclick') || '';
                                var txt = (bluebtns[i].textContent || '').trim();
                                if (isVisible(bluebtns[i], doc) && onclick.indexOf('submitCheckTimes') >= 0) {
                                    bluebtns[i].click();
                                    return {clicked: true, text: txt, method: 'bluebtn+submitCheckTimes', depth: depth};
                                }
                            }
                            // 策略2: layui-layer-btn 中的按钮
                            var layerBtns = doc.querySelectorAll('.layui-layer-btn a, .layui-layer-btn button');
                            for (var i = 0; i < layerBtns.length; i++) {
                                var txt = (layerBtns[i].textContent || '').trim();
                                if (isVisible(layerBtns[i], doc) && (txt === '提交' || txt === '确定' || txt === '确认')) {
                                    layerBtns[i].click();
                                    return {clicked: true, text: txt, method: 'layui-layer-btn', depth: depth};
                                }
                            }
                            // 策略3: 任何可见的弹窗中的"提交"/"确定"按钮（排除原始 .btnSubmit）
                            var allLinks = doc.querySelectorAll('a, button');
                            for (var i = 0; i < allLinks.length; i++) {
                                var el = allLinks[i];
                                var txt = (el.textContent || '').trim();
                                var cls = el.className || '';
                                var onclick = el.getAttribute('onclick') || '';
                                // 跳过原始提交按钮
                                if (cls.indexOf('btnSubmit') >= 0) continue;
                                if (cls.indexOf('workBtnIndex') >= 0) continue;
                                // 匹配弹窗中的确认按钮
                                if (isVisible(el, doc) && (txt === '提交' || txt === '确定') && el.children.length <= 1) {
                                    // 确保不在 .btnSubmit 中
                                    var inSubmit = false;
                                    var p = el.parentElement;
                                    while (p) {
                                        if (p.className && p.className.indexOf && p.className.indexOf('btnSubmit') >= 0) { inSubmit = true; break; }
                                        p = p.parentElement;
                                    }
                                    if (!inSubmit) {
                                        el.click();
                                        return {clicked: true, text: txt, cls: cls.substring(0, 40), method: 'generic-match', depth: depth};
                                    }
                                }
                            }
                            // 递归 iframe
                            var iframes = doc.querySelectorAll('iframe');
                            for (var i = 0; i < iframes.length; i++) {
                                try {
                                    var subDoc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                                    if (subDoc && subDoc.body) {
                                        var r = searchAndClick(subDoc, depth + 1);
                                        if (r) return r;
                                    }
                                } catch(e) {}
                            }
                            return null;
                        }
                        return searchAndClick(document, 0);
                    })()
                """)
                if find_and_click:
                    logger.info(f"弹窗检测 (round {wait_round}): {find_and_click}")
                    if find_and_click.get('clicked'):
                        confirmed = True
                        break
            
                # 检查 submitLock
                if wait_round % 6 == 5:
                    lock_check = await tab.evaluate("""
                        (() => {
                            function findQuizWin(doc, depth) {
                                if (depth > 5 || !doc) return null;
                                if (doc.querySelector('#form1')) return doc.defaultView || doc.parentWindow;
                                var iframes = doc.querySelectorAll('iframe');
                                for (var i = 0; i < iframes.length; i++) {
                                    try {
                                        var subDoc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                                        if (subDoc && subDoc.body) {
                                            var found = findQuizWin(subDoc, depth + 1);
                                            if (found) return found;
                                        }
                                    } catch(e) {}
                                }
                                return null;
                            }
                            var win = findQuizWin(document, 0);
                            if (!win) return null;
                            return (typeof win.submitLock !== 'undefined') ? win.submitLock : null;
                        })()
                    """)
                    if lock_check == 1:
                        logger.info("submitLock=1，提交已完成")
                        confirmed = True
                        break
    
            # 第四步：验证
            await asyncio.sleep(3)
            post_check = await tab.evaluate("""
                (() => {
                    function findQuizWin(doc, depth) {
                        if (depth > 5 || !doc) return null;
                        if (doc.querySelector('#form1')) return doc.defaultView || doc.parentWindow;
                        var iframes = doc.querySelectorAll('iframe');
                        for (var i = 0; i < iframes.length; i++) {
                            try {
                                var subDoc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                                if (subDoc && subDoc.body) {
                                    var found = findQuizWin(subDoc, depth + 1);
                                    if (found) return found;
                                }
                            } catch(e) {}
                        }
                        return null;
                    }
                    var win = findQuizWin(document, 0);
                    if (!win) return {found: false, reason: 'iframe context lost'};
                    return {
                        found: true,
                        submitLock: (typeof win.submitLock !== 'undefined') ? win.submitLock : -999,
                        url: win.location.href.substring(0, 100)
                    };
                })()
            """)
            logger.info(f"提交后检查: {post_check}")
    
            if confirmed:
                logger.info("测验已通过 Element.click() 提交")
                return True
    
            if post_check and isinstance(post_check, dict):
                if post_check.get('submitLock') == 1:
                    logger.info("submitLock=1，已提交")
                    return True
                if not post_check.get('found'):
                    logger.info("iframe 已导航，视为提交成功")
                    return True
    
            # 降级：XHR 直接提交
            logger.warning("尝试 XHR 降级提交")
            xhr_result = await tab.evaluate("""
                (() => {
                    function findQuizDoc(doc, depth) {
                        if (depth > 5 || !doc) return null;
                        if (doc.querySelector('#form1')) return doc;
                        var iframes = doc.querySelectorAll('iframe');
                        for (var i = 0; i < iframes.length; i++) {
                            try {
                                var subDoc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                                if (subDoc && subDoc.body) {
                                    var found = findQuizDoc(subDoc, depth + 1);
                                    if (found) return found;
                                }
                            } catch(e) {}
                        }
                        return null;
                    }
                    var quizDoc = findQuizDoc(document, 0);
                    if (!quizDoc) return {submitted: false, reason: 'no quizDoc'};
                    var quizWin = quizDoc.defaultView || quizDoc.parentWindow;
                    try { if (typeof quizWin.submitLock !== 'undefined') quizWin.submitLock = 0; } catch(e) {}
                    var pyFlag = quizDoc.querySelector('#pyFlag');
                    if (pyFlag) pyFlag.value = '0';
                    var form = quizDoc.getElementById('form1');
                    if (!form) return {submitted: false, reason: 'no form'};
                    var formData = new FormData(form);
                    formData.set('pyFlag', '0');
                    var ajaxUrl = '';
                    try { ajaxUrl = (typeof quizWin.version === 'function') ? quizWin.version(form.action) : form.action; } catch(e) { ajaxUrl = form.action; }
                    var xhr = new quizWin.XMLHttpRequest();
                    xhr.open('POST', ajaxUrl, false);
                    xhr.send(formData);
                    var result = {submitted: true, method: 'XHR', status: xhr.status};
                    try { result.response = JSON.parse(xhr.responseText); } catch(e) { result.responseText = (xhr.responseText || '').substring(0, 200); }
                    return result;
                })()
            """)
            logger.info(f"XHR 降级: {xhr_result}")
            if xhr_result and xhr_result.get('submitted'):
                resp = xhr_result.get('response')
                if resp and isinstance(resp, dict) and resp.get('status') == True:
                    logger.info("XHR 提交成功")
                    return True
                elif resp:
                    logger.warning(f"XHR 服务器拒绝: {resp.get('msg', '')}")
                else:
                    return True
            return False
    
        except Exception as e:
            logger.error(f"提交测验失败: {e}")
            return False

    async def _find_and_click_modal_submit(
        self, tab: Tab, iframe_frag: str = "", deep: bool = False, iframe_index: int = 0
    ) -> dict:
        """在多个 frame 上下文中查找并点击确认弹窗的“提交”按钮"""
        # JS: 在给定 document 中搜索弹窗提交按钮
        search_js = """
            const docs = [_doc];
            // 也搜索父级 frame (work iframe 的弹窗可能在这里)
            try {
                if (_win && _win.parent && _win.parent.document) {
                    docs.push(_win.parent.document);
                }
            } catch(e) {}
            // 搜索顶层
            try {
                if (_win && _win.top && _win.top.document) {
                    docs.push(_win.top.document);
                }
            } catch(e) {}

            for (const doc of docs) {
                // 策略1: layui/jQuery UI/自定义弹窗
                const btns = doc.querySelectorAll(
                    '.layui-layer-btn a, .layui-layer-btn button, ' +
                    '.layui-layer-btn0, ' +
                    '.dialog-btn a, .dialog-btn button, ' +
                    '.ui-dialog-buttonset button, .ui-dialog-buttonset a, ' +
                    '.btnBlueSubmit, a.bluebtn, button.bluebtn, ' +
                    '.submit-btn, .confirm-btn'
                );
                for (const btn of btns) {
                    const text = (btn.textContent || btn.innerText || btn.value || '').trim();
                    if (text === '提交' || text === '确认提交' || text === '确定') {
                        btn.click();
                        return {clicked: true, text: text, frame: doc.title || 'unknown'};
                    }
                }
                // 策略2: 搜索所有可见按钮
                const all = doc.querySelectorAll('a, button, input[type="button"]');
                for (const btn of all) {
                    const text = (btn.textContent || btn.innerText || btn.value || '').trim();
                    if ((text === '提交' || text === '确认提交') && btn.offsetParent !== null) {
                        btn.click();
                        return {clicked: true, text: text, frame: doc.title || 'unknown', fallback: true};
                    }
                }
            }
            return {clicked: false};
        """
        # 尝试在 quiz iframe 上下文中搜索
        result = await self._eval_in_context(tab, iframe_frag, search_js, deep=deep, iframe_index=iframe_index)
        if result and isinstance(result, dict) and result.get('clicked'):
            return result

        # 回退: 直接在主页面搜索（弹窗可能在最外层）
        main_search = """
            (() => {
                const docs = [document];
                // 搜索所有可访问的 iframe
                document.querySelectorAll('iframe').forEach(f => {
                    try { if (f.contentDocument) docs.push(f.contentDocument); } catch(e) {}
                });
                for (const doc of docs) {
                    const btns = doc.querySelectorAll(
                        '.layui-layer-btn a, .layui-layer-btn button, .layui-layer-btn0, ' +
                        '.dialog-btn a, .dialog-btn button, ' +
                        '.ui-dialog-buttonset button, .ui-dialog-buttonset a, ' +
                        'a.bluebtn, button.bluebtn, .submit-btn, .confirm-btn'
                    );
                    for (const btn of btns) {
                        const text = (btn.textContent || btn.innerText || btn.value || '').trim();
                        if (text === '提交' || text === '确认提交' || text === '确定') {
                            btn.click();
                            return {clicked: true, text: text, mainPage: true};
                        }
                    }
                    const all = doc.querySelectorAll('a, button, input[type="button"]');
                    for (const btn of all) {
                        const text = (btn.textContent || btn.innerText || btn.value || '').trim();
                        if ((text === '提交' || text === '确认提交') && btn.offsetParent !== null) {
                            btn.click();
                            return {clicked: true, text: text, mainPage: true, fallback: true};
                        }
                    }
                }
                return {clicked: false, mainPage: true};
            })()
        """
        try:
            main_result = await tab.evaluate(main_search)
            if main_result and isinstance(main_result, dict) and main_result.get('clicked'):
                return main_result
        except Exception as e:
            logger.debug(f"主页面弹窗搜索失败: {e}")

        return result or {'clicked': False}

    async def save_draft(self, tab: Tab, iframe_frag: str = "", deep: bool = False, iframe_index: int = 0) -> bool:
        """暂存测验答案（不提交，不刷新页面）"""
        try:
            # 抑制 alert 弹窗
            await self._eval_in_context(tab, iframe_frag,
                "window.alert = function(msg) { console.log('alert:', msg); }; "
                "window.confirm = function(msg) { console.log('confirm:', msg); return true; }; "
                "true;",
                deep=deep, iframe_index=iframe_index
            )
            saved = await self._eval_in_context(tab, iframe_frag, """
                // 策略1: 调用 noSubmit 函数（最可靠的方式）
                if (typeof noSubmit === 'function') {
                    noSubmit();
                    return {saved: true, method: 'noSubmit'};
                }
                // 策略2: 查找暂存按钮（扩大搜索范围）
                const allBtns = _doc.querySelectorAll(
                    'input[type="button"], button, a.btnGray, a.graybtn, ' +
                    '[class*="save"], [class*="draft"], ' +
                    'input.btnGray, .btn-gray, [onclick*="noSubmit"], [onclick*="save"]'
                );
                for (const btn of allBtns) {
                    const text = (btn.value || btn.textContent || btn.innerText || '').trim();
                    const onclick = (btn.getAttribute('onclick') || '').toLowerCase();
                    if (text.includes('暂') || text.includes('保存') || text.includes('存') ||
                        onclick.includes('nosubmit') || onclick.includes('save')) {
                        btn.click();
                        return {saved: true, method: 'button:' + text.substring(0, 20)};
                    }
                }
                // 策略3: 通过 window 全局函数查找
                const globalFuncs = ['noSubmit', 'saveWork', 'saveAnswer', 'tempSave'];
                for (const fn of globalFuncs) {
                    if (typeof window[fn] === 'function') {
                        try { window[fn](); return {saved: true, method: fn}; } catch(e) {}
                    }
                }
                return {saved: false, btnCount: allBtns.length};
            """, deep=deep, iframe_index=iframe_index)

            if saved and isinstance(saved, dict) and saved.get('saved'):
                await asyncio.sleep(1.5)
                logger.info(f"测验已暂存（{saved.get('method', '')}）")
                return True
            else:
                btn_count = saved.get('btnCount', 0) if isinstance(saved, dict) else 0
                logger.warning(f"未找到暂存按钮(已扫描{btn_count}个按钮)，答案可能未保存")
                return False
        except Exception as e:
            logger.error(f"暂存测验失败: {e}")
            return False

    async def dismiss_flash_prompt(self, tab: Tab, iframe_frag: str = "", deep: bool = False, iframe_index: int = 0):
        """处理 Flash Player 提示，隐藏相关弹窗"""
        try:
            dismiss_js = """
                const flashKeywords = ['flashplayer', 'flash player', 'adobe.com', '安装flash', '启用flash', 'flash插件'];
                const allElements = _doc.querySelectorAll('div, p, span, td, li, section');
                for (const el of allElements) {
                    const text = (el.textContent || '').toLowerCase();
                    if (flashKeywords.some(kw => text.includes(kw))) {
                        let container = el;
                        for (let i = 0; i < 5; i++) {
                            if (container.parentElement &&
                                container.parentElement.tagName !== 'BODY' &&
                                container.parentElement.tagName !== 'HTML') {
                                container = container.parentElement;
                            }
                            const cls = (container.className || '').toLowerCase();
                            const id = (container.id || '').toLowerCase();
                            if (cls.includes('layer') || cls.includes('popup') || cls.includes('modal') ||
                                cls.includes('dialog') || cls.includes('mask') || cls.includes('overlay') ||
                                id.includes('layer') || id.includes('popup')) {
                                container.style.display = 'none'; break;
                            }
                        }
                        el.style.display = 'none';
                    }
                }
                const selectors = [
                    '[class*="flash"]', '[id*="flash"]',
                    'object[type="application/x-shockwave-flash"]',
                    'embed[type="application/x-shockwave-flash"]'
                ];
                for (const sel of selectors) {
                    _doc.querySelectorAll(sel).forEach(el => {
                        if (el.offsetHeight < 300) el.style.display = 'none';
                    });
                }
                const overlays = _doc.querySelectorAll(
                    '.layui-layer-shade, .layui-layer, .layui-layer-btn, ' +
                    '[class*="mask"], [class*="overlay"], [class*="popup"], .modal-backdrop'
                );
                overlays.forEach(el => {
                    const text = (el.textContent || '').toLowerCase();
                    if (flashKeywords.some(kw => text.includes(kw)) ||
                        el.classList.contains('layui-layer-shade')) {
                        el.style.display = 'none';
                    }
                });
                const closeBtns = _doc.querySelectorAll(
                    '.layui-layer-btn a, .layui-layer-close, .popup-close, .modal-close'
                );
                for (const btn of closeBtns) {
                    const text = (btn.textContent || btn.value || '').trim();
                    if (text.includes('知道') || text.includes('关闭') ||
                        text.includes('确定') || text === '×' || text === 'X') {
                        let parent = btn.parentElement;
                        for (let i = 0; i < 5; i++) {
                            if (!parent) break;
                            const pText = (parent.textContent || '').toLowerCase();
                            if (flashKeywords.some(kw => pText.includes(kw))) {
                                btn.click(); return;
                            }
                            parent = parent.parentElement;
                        }
                    }
                }
            """
            await self._eval_in_context(tab, iframe_frag, dismiss_js, deep=deep, iframe_index=iframe_index)
        except Exception:
            pass  # Flash 处理非关键，静默失败

    # ======================== 视频/音频播放 ========================

    async def play_video(self, tab: Tab, iframe_frag: str = "",
                         on_log: Callable = None, iframe_index: int = 0,
                         pause_event: asyncio.Event = None,
                         stop_check: Callable[[], bool] = None) -> bool:
        """
        自动播放视频

        包含: 自动静音 + 可配置倍速播放 + 随机暂停 + 鼠标模拟
        视频元素在嵌套 iframe 中: 主页面 → cards iframe → video iframe → <video>
        倍速通过 self._config.video_speed 配置，默认 1 倍速
        iframe_index: 当有多个视频时，指定第几个(从0开始)
        """
        log = on_log or (lambda msg, level="info": logger.info(msg))
        try:
            log(f"正在处理视频任务(索引:{iframe_index})...")
            await self.dismiss_flash_prompt(tab, iframe_frag)

            # 二次 Flash 伪装
            try:
                await self._eval_in_context(tab, iframe_frag, """
                    _doc.querySelectorAll('div, p, span, td').forEach(el => {
                        const text = (el.textContent || '').toLowerCase();
                        if (text.includes('flashplayer') || text.includes('安装flash')) {
                            el.style.display = 'none';
                        }
                    });
                    if (window.swfobject) {
                        window.swfobject.hasFlashPlayerVersion = function() { return true; };
                        window.swfobject.getFlashPlayerVersion = function() {
                            return { major: 32, minor: 0, release: 0 };
                        };
                    }
                """)
            except Exception:
                pass

            # 等待视频元素加载 (在嵌套 video iframe 中查找)
            # 多视频场景: 第二个视频的 iframe 可能需要额外时间加载
            video_found = False
            for attempt in range(8):  # 增加到 8 次，给多视频场景更多时间
                check = await self._eval_in_video_player(tab,
                    "return !!(_doc.getElementById('video_html5_api') || "
                    "_doc.querySelector('video[id]') || "
                    "_doc.querySelector('.video-js') || "
                    "_doc.querySelector('video'));",
                    iframe_index=iframe_index
                )
                if check:
                    video_found = True
                    break
                if attempt == 1:
                    await self.dismiss_flash_prompt(tab, iframe_frag)
                await asyncio.sleep(2)

            if not video_found:
                # 调试信息
                debug_info = await self._eval_in_iframe(tab, "cards", """
                    const videoIframes = _doc.querySelectorAll('iframe');
                    const info = [];
                    videoIframes.forEach(f => {
                        info.push({src: (f.src || '').substring(0, 100), loaded: !!f.contentDocument});
                    });
                    return {iframeCount: videoIframes.length, iframes: info, bodyLen: _doc.body ? _doc.body.innerHTML.length : 0};
                """)
                log(f"视频元素未加载，cards iframe 状态: {debug_info}", "warning")
                return False

            # ---- 检测视频是否已完成（避免重复播放）----
            already_done = await self._eval_in_video_player(tab, """
                const v = _doc.getElementById('video_html5_api')
                    || _doc.querySelector('video[id]')
                    || _doc.querySelector('video');
                if (!v) return false;
                // 视频 ended 属性 = 已播放完毕
                if (v.ended) return true;
                // currentTime 接近 duration = 已观看完
                if (v.duration && isFinite(v.duration) && v.currentTime >= v.duration - 1) return true;
                // 检查页面上的完成标记
                const body = _doc.body ? _doc.body.innerText : '';
                if (body.includes('已完成') && !body.includes('未完成')) return true;
                return false;
            """, iframe_index=iframe_index)
            if already_done:
                log(f"视频已完成，跳过播放", "success")
                return True

            # ---- 尝试拖动(seek)到末尾快速完成 ----
            seek_result = await self._eval_in_video_player(tab, """
                const v = _doc.getElementById('video_html5_api')
                    || _doc.querySelector('video[id]')
                    || _doc.querySelector('video');
                if (!v) return { ok: false };
                const dur = v.duration;
                if (typeof dur !== 'number' || !isFinite(dur) || dur <= 0)
                    return { ok: false, reason: 'no_duration' };
                // 尝试 seek 到末尾前 1 秒
                v.muted = """ + ("true" if self._config.video_volume == 0 else "false") + """;
                v.volume = """ + str(self._config.video_volume / 100.0) + """;
                v.currentTime = Math.max(0, dur - 1);
                return { ok: true, seekTo: dur - 1, duration: dur };
            """, iframe_index=iframe_index)
            if seek_result and seek_result.get('ok'):
                await asyncio.sleep(4)
                after_seek = await self._eval_in_video_player(tab, """
                    const v = _doc.getElementById('video_html5_api')
                        || _doc.querySelector('video[id]')
                        || _doc.querySelector('video');
                    if (!v) return { found: false };
                    const dur = v.duration;
                    const durValid = (typeof dur === 'number' && isFinite(dur) && dur > 0);
                    return {
                        found: true,
                        currentTime: v.currentTime || 0,
                        duration: durValid ? dur : 0,
                        ended: v.ended,
                        paused: v.paused
                    };
                """, iframe_index=iframe_index)
                if after_seek and after_seek.get('found'):
                    s_ct = after_seek.get('currentTime', 0)
                    s_dur = after_seek.get('duration', 0)
                    if s_dur > 0 and (s_ct >= s_dur - 2 or after_seek.get('ended')):
                        log(f"拖动到末尾成功({s_ct:.0f}s/{s_dur:.0f}s)，等待服务器确认...", "success")
                        await asyncio.sleep(3)
                        return True
                    else:
                        log(f"拖动被平台重置({s_ct:.0f}s/{s_dur:.0f}s)，回退到正常播放", "info")

            # 初始化视频播放器 + 倍速探测 (在 video iframe 中执行)
            speed = self._config.video_speed
            init_result = await self._eval_in_video_player(tab, f"""
                const videoEl = _doc.getElementById('video_html5_api')
                    || _doc.querySelector('video[id]')
                    || _doc.querySelector('.video-js video')
                    || _doc.querySelector('video');
                if (!videoEl) return {{ok: false, reason: 'no_video'}};

                // 通过 window 链查找 videojs
                var vjs = null;
                var videoId = videoEl.id || 'video_html5_api';
                try {{ vjs = _win.videojs; }} catch(e) {{}}
                if (!vjs) try {{ vjs = _win.parent.videojs; }} catch(e) {{}}
                if (!vjs) try {{ vjs = _win.top.videojs; }} catch(e) {{}}

                var player = null;
                if (vjs) {{
                    try {{ player = vjs(videoId); }} catch(e) {{}}
                }}

                // 先静音 + 开始播放
                videoEl.muted = true;
                if (player) {{
                    player.muted(true);
                    if (player.paused()) player.play();
                }}
                videoEl.play().catch(() => {{}});

                // 倍速设置策略:
                // 1. 先通过 DOM 直接设置 (最底层，不受 videojs 限制)
                // 2. 再通过 player API 设置 (如果 videojs 可用)
                // 3. Monkey-patch player.playbackRate 防止 videojs 内部重置
                var targetSpeed = {speed};
                var effectiveSpeed = 1;
                var method = 'none';

                // 步骤1: 直接 DOM 设置
                videoEl.playbackRate = targetSpeed;
                videoEl.defaultPlaybackRate = targetSpeed;
                effectiveSpeed = videoEl.playbackRate;
                if (Math.abs(effectiveSpeed - targetSpeed) < 0.2) {{
                    method = 'dom';
                }}

                // 步骤2: 如果 DOM 设置被拒绝，尝试 player API
                if (method === 'none' && player) {{
                    try {{
                        player.playbackRate(targetSpeed);
                        effectiveSpeed = videoEl.playbackRate;
                        if (Math.abs(effectiveSpeed - targetSpeed) < 0.2) {{
                            method = 'player';
                        }}
                    }} catch(e) {{}}
                }}

                // 步骤3: 如果两种方法都被拒绝，探测平台支持的最大速度
                if (method === 'none') {{
                    var testSpeeds = [{speed}, 4, 3, 2, 1.5];
                    for (var i = 0; i < testSpeeds.length; i++) {{
                        videoEl.playbackRate = testSpeeds[i];
                        if (Math.abs(videoEl.playbackRate - testSpeeds[i]) < 0.2) {{
                            effectiveSpeed = testSpeeds[i];
                            targetSpeed = testSpeeds[i];
                            method = 'probed_' + testSpeeds[i];
                            break;
                        }}
                    }}
                    if (method === 'none') {{
                        effectiveSpeed = videoEl.playbackRate;
                        targetSpeed = effectiveSpeed;
                        method = 'fallback_' + effectiveSpeed;
                    }}
                }}

                // 步骤4: Monkey-patch player.playbackRate 防止 videojs 内部重置
                if (player && targetSpeed > 1) {{
                    try {{
                        var _origPR = player.playbackRate.bind(player);
                        var _lockedSpeed = targetSpeed;
                        player.playbackRate = function(rate) {{
                            if (arguments.length === 0) return _origPR();
                            // 只允许设置我们想要的速度，拒绝其他值
                            if (Math.abs(rate - _lockedSpeed) < 0.5) {{
                                return _origPR(rate);
                            }}
                            // 拒绝 videojs 内部的重置调用
                            return _origPR(_lockedSpeed);
                        }};
                    }} catch(e) {{}}
                }}

                // 同样锁定 tech 层的 setPlaybackRate
                if (player && targetSpeed > 1) {{
                    try {{
                        var tech = player.tech({{}});
                        if (tech && typeof tech.setPlaybackRate === 'function') {{
                            var _origTechPR = tech.setPlaybackRate.bind(tech);
                            var _lockedSpeed2 = targetSpeed;
                            tech.setPlaybackRate = function(rate) {{
                                if (Math.abs(rate - _lockedSpeed2) > 0.5) {{
                                    return _origTechPR(_lockedSpeed2);
                                }}
                                return _origTechPR(rate);
                            }};
                        }}
                    }} catch(e) {{}}
                }}

                return {{ok: true, rate: effectiveSpeed, target: targetSpeed, hasVjs: !!vjs, method: method}};
            """, iframe_index=iframe_index)
            speed_label = f"{speed}倍速" if speed != 1 else "正常速度"
            effective_speed = speed  # 实际生效的速度，可能被降级
            if init_result and isinstance(init_result, dict):
                if not init_result.get('ok'):
                    log(f"视频初始化失败: {init_result.get('reason', 'unknown')}", "warning")
                else:
                    has_vjs = init_result.get('hasVjs', False)
                    actual_rate = init_result.get('rate', speed)
                    method = init_result.get('method', '?')
                    effective_speed = actual_rate
                    if actual_rate != speed:
                        log(f"视频已开始播放(目标{speed}x, 实际生效{actual_rate}x, vjs:{'✓' if has_vjs else '✗'}, 方式:{method})", "warning")
                    else:
                        log(f"视频已开始播放({speed_label}, vjs:{'✓' if has_vjs else '✗'}, 方式:{method})", "success")
            else:
                log(f"视频已开始播放({speed_label})", "success")

            # 延迟5秒检查视频是否真正在播放
            await asyncio.sleep(5)
            verify = await self._eval_in_video_player(tab, f"""
                const v = _doc.getElementById('video_html5_api') || _doc.querySelector('video');
                if (!v) return {{ found: false }};
                return {{
                    found: true,
                    paused: v.paused,
                    currentTime: v.currentTime,
                    duration: v.duration,
                    playbackRate: v.playbackRate,
                    readyState: v.readyState,
                    networkState: v.networkState
                }};
            """, iframe_index=iframe_index)
            if verify and isinstance(verify, dict) and verify.get('found'):
                v_paused = verify.get('paused', True)
                v_rate = verify.get('playbackRate', 1)
                v_ct = verify.get('currentTime', 0)
                v_dur = verify.get('duration', 0)
                v_ready = verify.get('readyState', 0)
                if v_paused and v_ct == 0:
                    log(f"视频可能未自动播放(暂停中, readyState:{v_ready})，尝试点击播放按钮...", "warning")
                    await self._eval_in_video_player(tab, f"""
                        const v = _doc.getElementById('video_html5_api') || _doc.querySelector('video');
                        if (!v) return;
                        v.muted = {"true" if self._config.video_volume == 0 else "false"};
                        v.volume = {self._config.video_volume / 100.0};
                        const btn = _doc.querySelector('.vjs-big-play-button');
                        if (btn) btn.click();
                        v.play().catch(() => {{}});
                        // 恢复倍速: 直接 DOM 设置
                        v.playbackRate = {effective_speed};
                        v.defaultPlaybackRate = {effective_speed};
                    """, iframe_index=iframe_index)
                elif abs(v_rate - effective_speed) > 0.5:
                    log(f"倍速偏差: 目标{effective_speed}x, 实际{v_rate}x，重新设置...", "warning")
                    await self._eval_in_video_player(tab, f"""
                        const v = _doc.getElementById('video_html5_api') || _doc.querySelector('video');
                        if (v) {{
                            v.playbackRate = {effective_speed};
                            v.defaultPlaybackRate = {effective_speed};
                        }}
                    """, iframe_index=iframe_index)
                else:
                    log(f"视频播放正常: {v_ct:.0f}s/{v_dur if isinstance(v_dur, (int,float)) and v_dur > 0 else '?'}s, 倍速:{v_rate}x, readyState:{v_ready}", "info")

            # 等待视频完成（设置最大超时时间，防止无限循环）
            max_wait_s = 3600  # 默认最大1小时
            start_time = asyncio.get_event_loop().time()
            consecutive_errors = 0
            loop_count = 0
            prev_ct = -1  # 上次 currentTime，用于检测卡死
            stall_count = 0  # 连续卡死次数
            stall_recovered = 0  # 卡死恢复次数
            while True:
                await asyncio.sleep(3)

                # 暂停/停止检查（每轮循环都响应，确保秒级响应）
                if stop_check and stop_check():
                    log("收到停止指令，中止视频播放", "warning")
                    return False
                if pause_event and not pause_event.is_set():
                    log("收到暂停指令，暂停视频等待...", "info")
                    # 暂停视频播放
                    await self._eval_in_video_player(tab, """
                        const videoEl = _doc.getElementById('video_html5_api')
                            || _doc.querySelector('video[id]')
                            || _doc.querySelector('video');
                        if (videoEl && !videoEl.paused) videoEl.pause();
                    """, iframe_index=iframe_index)
                    await pause_event.wait()
                    log("已继续执行，恢复视频播放", "success")
                    # 恢复播放
                    await self._eval_in_video_player(tab, f"""
                        const videoEl = _doc.getElementById('video_html5_api')
                            || _doc.querySelector('video[id]')
                            || _doc.querySelector('video');
                        if (videoEl && videoEl.paused) {{
                            videoEl.playbackRate = {effective_speed};
                            videoEl.play().catch(() => {{}});
                        }}
                    """, iframe_index=iframe_index)

                # 超时保护
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > max_wait_s:
                    log(f"视频播放超时(已等待{elapsed:.0f}秒)，跳过", "warning")
                    return False

                # 每次监控都强制检查并重新应用倍速
                # 同时检测: 视频错误 / 人脸识别 / 视频内弹出题目
                status = await self._eval_in_video_player(tab, f"""
                    const findVideo = () => _doc.getElementById('video_html5_api')
                        || _doc.querySelector('video[id]')
                        || _doc.querySelector('video');
                    const videoEl = findVideo();
                    if (!videoEl) return {{ error: true, paused: true, ended: false }};

                    // 强制重新应用倍速
                    const targetSpeed = {effective_speed};
                    if (Math.abs(videoEl.playbackRate - targetSpeed) > 0.3) {{
                        videoEl.playbackRate = targetSpeed;
                        videoEl.defaultPlaybackRate = targetSpeed;
                    }}

                    const ct = videoEl.currentTime || 0;
                    const dur = videoEl.duration;
                    const durValid = (typeof dur === 'number' && isFinite(dur) && dur > 0);
                    const realEnded = videoEl.ended || (durValid && ct >= dur - 1);

                    // --- 视频加载错误检测 ---
                    let videoErrorMsg = '';
                    const errDiv = _doc.querySelector('.vjs-modal-dialog-content');
                    if (errDiv) {{
                        const errText = errDiv.innerText || '';
                        const errKeywords = ['视频文件损坏', '网络错误导致视频下载中途失败',
                            '视频因格式不支持', '网络的问题无法加载'];
                        for (const kw of errKeywords) {{
                            if (errText.includes(kw)) {{ videoErrorMsg = kw; break; }}
                        }}
                    }}

                    // --- 人脸识别检测 (旧版+新版) ---
                    let hasFace = false;
                    try {{
                        const oldFaces = _doc.querySelectorAll('#fcqrimg');
                        for (const f of oldFaces) {{
                            if (f.getAttribute('src')) {{ hasFace = true; break; }}
                        }}
                        if (!hasFace) {{
                            const newFaces = _doc.querySelectorAll('.chapterVideoFaceMaskDiv');
                            for (const f of newFaces) {{
                                if (f.style.display !== 'none') {{ hasFace = true; break; }}
                            }}
                        }}
                    }} catch(e) {{}}

                    // --- 视频内弹出题目检测 ---
                    let hasQuiz = false;
                    try {{
                        hasQuiz = !!_doc.querySelector('#videoquiz-submit');
                    }} catch(e) {{}}

                    return {{
                        paused: videoEl.paused,
                        ended: realEnded,
                        currentTime: ct,
                        duration: durValid ? dur : 0,
                        speed: videoEl.playbackRate,
                        readyState: videoEl.readyState,
                        videoError: videoErrorMsg,
                        hasFace: hasFace,
                        hasQuiz: hasQuiz
                    }};
                """, iframe_index=iframe_index)
                if not status or status.get('error'):
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        # 前3次失败可能是 iframe 还在加载，打印诊断信息
                        log(f"视频状态获取连续失败({consecutive_errors}次)，等待重试...", "warning")
                    if consecutive_errors >= 30:  # 90秒无响应才视为异常
                        log("视频状态长时间无法获取(90秒)，跳过", "warning")
                        return False  # 返回 False(未完成)而不是 True
                    continue  # 跳过本次循环，等待下次重试
                else:
                    consecutive_errors = 0
                    # 动态调整超时时间
                    duration = status.get("duration", 0)
                    if duration and duration > 0 and max_wait_s == 3600:
                        max_wait_s = duration * 1.5 / effective_speed + 60
                        logger.debug(f"视频超时时间调整为 {max_wait_s:.0f}s (时长={duration}s, 倍速={effective_speed})")
                    # 记录实际倍速 (调试用)
                    actual_speed = status.get("speed", effective_speed)
                    if abs(actual_speed - effective_speed) > 0.5:
                        logger.debug(f"倍速偏差: 目标={effective_speed}, 实际={actual_speed}, 重新设置")
                    # 每15秒打印一次播放进度
                    loop_count += 1
                    if loop_count % 5 == 0:
                        ct = status.get("currentTime", 0)
                        dur = status.get("duration", 0)
                        pct = (ct / dur * 100) if dur > 0 else 0
                        log(f"视频进度: {ct:.0f}s/{dur:.0f}s ({pct:.0f}%)", "info")

                    # --- 视频卡死检测与恢复 ---
                    cur_ct = status.get("currentTime", 0)
                    cur_ready = status.get("readyState", 0)
                    cur_dur = status.get("duration", 0)
                    cur_paused = status.get("paused", False)

                    # 判定卡死: 两种模式，不同阈值和恢复策略
                    # 模式A: 视频未加载 (readyState==0 && duration==0) — 严重，快速检测
                    # 模式B: 播放卡住 (duration有效但进度不动) — 可能是缓冲，慢速检测
                    is_stalled = False
                    stall_mode = ""  # "A" = 未加载, "B" = 播放卡住
                    if cur_ready == 0 and cur_dur == 0:
                        is_stalled = True
                        stall_mode = "A"
                    elif prev_ct >= 0 and abs(cur_ct - prev_ct) < 0.5 and cur_dur == 0:
                        is_stalled = True
                        stall_mode = "A"
                    elif prev_ct >= 0 and cur_dur > 0 and not cur_paused:
                        if abs(cur_ct - prev_ct) < 0.5:
                            is_stalled = True
                            stall_mode = "B"

                    if is_stalled:
                        stall_count += 1
                        # 模式A: 3次(~9秒)触发; 模式B: 7次(~21秒)触发
                        threshold = 3 if stall_mode == "A" else 7
                        if stall_count == threshold:
                            stall_recovered += 1
                            # 根据恢复次数升级策略，永不放弃视频
                            if stall_recovered <= 3:
                                # 策略1: 常规恢复 (seek回退+play / 仅 play)
                                if stall_mode == "A":
                                    log(f"视频未加载[第{stall_recovered}次恢复, readyState:{cur_ready}, ct:{cur_ct:.0f}s]，seek回退重新加载...", "warning")
                                    await self._eval_in_video_player(tab, f"""
                                        (() => {{
                                            const v = _doc.getElementById('video_html5_api')
                                                || _doc.querySelector('video[id]')
                                                || _doc.querySelector('video');
                                            if (!v) return 'no_video';
                                            try {{ v.currentTime = Math.max(0, (v.currentTime || 0) - 2); }} catch(e) {{}}
                                            v.playbackRate = {effective_speed};
                                            v.defaultPlaybackRate = {effective_speed};
                                            v.play().catch(() => {{}});
                                            const btn = _doc.querySelector('.vjs-big-play-button');
                                            if (btn) btn.click();
                                            return 'recovered_A';
                                        }})()
                                    """, iframe_index=iframe_index)
                                else:
                                    log(f"视频播放卡住[第{stall_recovered}次恢复, ct:{cur_ct:.0f}s/{cur_dur:.0f}s]，尝试恢复播放(不seek)...", "warning")
                                    await self._eval_in_video_player(tab, f"""
                                        (() => {{
                                            const v = _doc.getElementById('video_html5_api')
                                                || _doc.querySelector('video[id]')
                                                || _doc.querySelector('video');
                                            if (!v) return 'no_video';
                                            v.playbackRate = {effective_speed};
                                            v.defaultPlaybackRate = {effective_speed};
                                            v.play().catch(() => {{}});
                                            const btn = _doc.querySelector('.vjs-big-play-button');
                                            if (btn) btn.click();
                                            if (v.readyState < 2) {{ try {{ v.load(); }} catch(e) {{}} }}
                                            return 'recovered_B';
                                        }})()
                                    """, iframe_index=iframe_index)
                            else:
                                # 策略2(第4次+): 强制重载视频源
                                log(f"视频多次卡住[第{stall_recovered}次恢复]，强制重载视频源...", "warning")
                                await self._eval_in_video_player(tab, f"""
                                    (() => {{
                                        const v = _doc.getElementById('video_html5_api')
                                            || _doc.querySelector('video[id]')
                                            || _doc.querySelector('video');
                                        if (!v) return 'no_video';
                                        // 保存当前位置，强制重载
                                        const pos = v.currentTime || 0;
                                        try {{
                                            const src = v.currentSrc || v.src;
                                            if (src) {{
                                                v.src = '';
                                                v.src = src;
                                                v.load();
                                                v.currentTime = Math.max(0, pos - 3);
                                            }}
                                        }} catch(e) {{}}
                                        v.playbackRate = {effective_speed};
                                        v.defaultPlaybackRate = {effective_speed};
                                        v.play().catch(() => {{}});
                                        const btn = _doc.querySelector('.vjs-big-play-button');
                                        if (btn) btn.click();
                                        return 'force_reload';
                                    }})()
                                """, iframe_index=iframe_index)
                                await asyncio.sleep(5)  # 给重载更多时间
                            await asyncio.sleep(3)
                            stall_count = 0
                    else:
                        stall_count = 0

                    prev_ct = cur_ct

                # --- 2.1 视频加载错误检测: 检测到错误则跳过 ---
                video_error = status.get("videoError", "")
                if video_error:
                    log(f"检测到视频加载错误: {video_error}，跳过该视频", "warning")
                    await asyncio.sleep(2)
                    return False

                # --- 2.2 人脸识别检测: 暂停播放，等待用户手动完成 ---
                if status.get("hasFace"):
                    log("检测到人脸识别，请手动完成识别...", "warning")
                    face_start = asyncio.get_event_loop().time()
                    while True:
                        await asyncio.sleep(3)
                        if stop_check and stop_check():
                            log("收到停止指令，中止人脸识别等待", "warning")
                            return False
                        face_check = await self._eval_in_video_player(tab, """
                            let has = false;
                            try {
                                const oldFaces = _doc.querySelectorAll('#fcqrimg');
                                for (const f of oldFaces) { if (f.getAttribute('src')) { has = true; break; } }
                                if (!has) {
                                    const newFaces = _doc.querySelectorAll('.chapterVideoFaceMaskDiv');
                                    for (const f of newFaces) { if (f.style.display !== 'none') { has = true; break; } }
                                }
                            } catch(e) {}
                            return has;
                        """, iframe_index=iframe_index)
                        if not face_check:
                            face_elapsed = asyncio.get_event_loop().time() - face_start
                            log(f"人脸识别已完成(等待{face_elapsed:.0f}秒)，恢复播放", "success")
                            # 恢复播放
                            await self._eval_in_video_player(tab, f"""
                                const videoEl = _doc.getElementById('video_html5_api')
                                    || _doc.querySelector('video[id]')
                                    || _doc.querySelector('video');
                                if (videoEl && videoEl.paused) {{
                                    videoEl.muted = true;
                                    videoEl.playbackRate = {effective_speed};
                                    videoEl.defaultPlaybackRate = {effective_speed};
                                    videoEl.play().catch(() => {{}});
                                }}
                            """, iframe_index=iframe_index)
                            break
                        # 人脸识别超时保护 (最多等待10分钟)
                        if asyncio.get_event_loop().time() - face_start > 600:
                            log("人脸识别等待超时(600秒)，跳过", "warning")
                            break

                # --- 2.3 视频内弹出题目自动作答 ---
                if status.get("hasQuiz"):
                    log("检测到视频内弹出题目，自动随机作答...", "info")
                    quiz_result = await self._eval_in_video_player(tab, """
                        const submitBtn = _doc.querySelector('#videoquiz-submit');
                        if (!submitBtn) return { answered: false };
                        const opts = Array.from(_doc.querySelectorAll('.ans-videoquiz-opt label'));
                        if (opts.length === 0) return { answered: false };
                        // 随机选择一个选项
                        const chosen = opts[Math.floor(Math.random() * opts.length)];
                        chosen.click();
                        // 点击提交
                        submitBtn.click();
                        return { answered: true, total: opts.length };
                    """, iframe_index=iframe_index)
                    if quiz_result and quiz_result.get("answered"):
                        log(f"视频内题目已随机作答({quiz_result.get('total', '?')}个选项)", "success")
                        await asyncio.sleep(3)
                        # 隐藏题目元素
                        await self._eval_in_video_player(tab, """
                            try {
                                const container = _doc.querySelector('#video .ans-videoquiz');
                                if (container) container.remove();
                                const comps = _doc.querySelectorAll('.x-component-default');
                                comps.forEach(c => { c.style.display = 'none'; });
                            } catch(e) {}
                        """, iframe_index=iframe_index)

                if status.get("ended"):
                    # 视频播放到末尾，额外等待让超星服务器记录观看时长
                    log(f"视频播放到末尾({status.get('currentTime',0):.0f}s/{status.get('duration',0):.0f}s)，等待服务器确认...", "info")
                    await asyncio.sleep(3)
                    log("视频播放完成", "success")
                    return True

                if status.get("paused") and not status.get("ended"):
                    await self._eval_in_video_player(tab, f"""
                        const videoEl = _doc.getElementById('video_html5_api')
                            || _doc.querySelector('video[id]')
                            || _doc.querySelector('video');
                        if (!videoEl) return;
                        videoEl.muted = true;
                        const btn = _doc.querySelector('.vjs-big-play-button');
                        if (btn) btn.click();
                        videoEl.playbackRate = {effective_speed};
                        videoEl.defaultPlaybackRate = {effective_speed};
                        videoEl.play().catch(() => {{}});
                    """, iframe_index=iframe_index)

                # 模拟鼠标移动
                await self._eval_in_video_player(tab, """
                    const video = _doc.getElementById('video_html5_api') || _doc.body;
                    if (!video) return;
                    const rect = video.getBoundingClientRect();
                    const x = Math.floor(Math.random() * rect.width) + rect.left;
                    const y = Math.floor(Math.random() * rect.height) + rect.top;
                    ['mousemove', 'mouseover'].forEach(type => {
                        video.dispatchEvent(new MouseEvent(type, {
                            bubbles: true, cancelable: true,
                            clientX: x, clientY: y
                        }));
                    });
                """, iframe_index=iframe_index)

        except Exception as e:
            logger.error(f"视频播放异常: {e}")
            return False

    async def play_audio(self, tab: Tab, iframe_frag: str = "",
                         on_log: Callable = None, iframe_index: int = 0,
                         pause_event: asyncio.Event = None,
                         stop_check: Callable[[], bool] = None) -> bool:
        """自动播放音频，音频在嵌套 iframe 中
        iframe_index: 当有多个音频时，指定第几个(从0开始)
        """
        log = on_log or (lambda msg, level="info": logger.info(msg))
        try:
            log("正在处理音频任务...")
            # 等待音频元素 (在嵌套 audio iframe 中查找)
            for _ in range(20):
                check = await self._eval_in_nested_module_iframe(tab, "modules/audio",
                    "return !!_doc.getElementById('audio_html5_api');",
                    iframe_index=iframe_index
                )
                if check:
                    break
                await asyncio.sleep(0.5)

            # 静音并自动播放
            await self._eval_in_nested_module_iframe(tab, "modules/audio", """
                const audio = _doc.getElementById('audio_html5_api');
                if (audio) {
                    audio.muted = true;
                    audio.autoplay = true;
                    audio.volume = 0;
                    audio.play().catch(() => {});
                }
            """, iframe_index=iframe_index)

            # 等待音频播放完成（设置最大超时时间）
            audio_start = asyncio.get_event_loop().time()
            audio_max_wait = 600  # 最大10分钟
            while True:
                await asyncio.sleep(2)
                # 暂停/停止检查
                if stop_check and stop_check():
                    log("收到停止指令，中止音频播放", "warning")
                    return False
                if pause_event and not pause_event.is_set():
                    log("收到暂停指令，等待继续...", "info")
                    await pause_event.wait()
                    log("已继续执行", "success")
                elapsed = asyncio.get_event_loop().time() - audio_start
                if elapsed > audio_max_wait:
                    log(f"音频播放超时(已等待{elapsed:.0f}秒)，跳过", "warning")
                    return False
                ended = await self._eval_in_nested_module_iframe(tab, "modules/audio", """
                    const audio = _doc.getElementById('audio_html5_api');
                    if (!audio) return true;
                    if (audio.paused) audio.play().catch(() => {});
                    return audio.ended;
                """, iframe_index=iframe_index)
                if ended:
                    log("音频播放完成", "success")
                    return True

        except Exception as e:
            logger.error(f"音频播放异常: {e}")
            return False

    # ======================== 侧边栏导航 ========================

    async def _click_chapter_in_sidebar(
        self, tab: Tab, chapter_url: str, browser: zd.Browser, log: Callable
    ) -> bool:
        """通过点击侧边栏链接进入章节，支持从 studentcourse 或 studentstudy 页面点击"""
        # 提取目标章节 ID (knowledgeid 或 chapterid)
        target_id = ""
        for pattern in [r'knowledgeid[=:](\d+)', r'chapterid[=:](\d+)']:
            m = re.search(pattern, chapter_url, re.IGNORECASE)
            if m:
                target_id = m.group(1)
                break
        if not target_id:
            log("无法从 URL 提取章节 ID", "warning")
            return False

        old_url = tab.url
        already_on_studentstudy = ('studentstudy' in old_url or 'knowledgestu' in old_url)

        # ---- 策略A: studentstudy 页面侧边栏 (posCatalog_select + posCatalog_name) ----
        if already_on_studentstudy:
            click_js_study = f"""
                (() => {{
                    const targetId = '{target_id}';
                    // 通过 id="cur{{knowledgeid}}" 精确匹配
                    const target = document.getElementById('cur' + targetId);
                    if (target) {{
                        const nameSpan = target.querySelector('.posCatalog_name');
                        if (nameSpan) {{
                            nameSpan.click();
                            return {{clicked: true, method: 'posCatalog_id', text: nameSpan.textContent.trim().substring(0, 40)}};
                        }}
                        // 直接点击 target div
                        target.click();
                        return {{clicked: true, method: 'posCatalog_div', text: target.textContent.trim().substring(0, 40)}};
                    }}
                    // 回退: 遍历 posCatalog_name 查找匹配 knowledgeid 的 onclick
                    const names = document.querySelectorAll('.posCatalog_name');
                    for (const span of names) {{
                        const onclick = span.getAttribute('onclick') || '';
                        if (onclick.includes("'" + targetId + "'")) {{
                            span.click();
                            return {{clicked: true, method: 'posCatalog_onclick', text: span.textContent.trim().substring(0, 40)}};
                        }}
                    }}
                    return {{clicked: false, posCatalogCount: names.length}};
                }})()
            """
            try:
                result = await tab.evaluate(click_js_study)
                if result and result.get('clicked'):
                    log(f"点击侧边栏: {result.get('text', '')[:30]} ({result.get('method', '')})", "success")
                    await asyncio.sleep(3)
                    if self._is_chapter_switched(tab.url, old_url, target_id, already_on_studentstudy):
                        log("章节切换成功", "success")
                        return True
                    for _ in range(4):
                        await asyncio.sleep(1)
                        if self._is_chapter_switched(tab.url, old_url, target_id, already_on_studentstudy):
                            log("章节切换成功 (延迟)", "success")
                            return True
            except Exception as e:
                log(f"JS 点击 studentstudy 侧边栏失败: {e}", "warning")

        # ---- 策略B: studentcourse 页面目录 (a.clicktitle + aria-labelledby) ----
        click_js_course = f"""
            (() => {{
                const targetId = '{target_id}';
                // 通过 aria-labelledby 精确匹配
                const containers = document.querySelectorAll('[aria-labelledby*="' + targetId + '"]');
                for (const container of containers) {{
                    const link = container.querySelector('a.clicktitle');
                    if (link) {{
                        link.click();
                        return {{clicked: true, method: 'aria-labelledby', text: link.textContent.trim().substring(0, 40)}};
                    }}
                }}
                // 遍历 clicktitle 链接查找匹配
                const links = document.querySelectorAll('a.clicktitle');
                for (const link of links) {{
                    const gp = link.parentElement ? link.parentElement.parentElement : null;
                    if (gp) {{
                        const labelled = gp.getAttribute('aria-labelledby') || '';
                        if (labelled.includes(targetId)) {{
                            link.click();
                            return {{clicked: true, method: 'gp_aria', text: link.textContent.trim().substring(0, 40)}};
                        }}
                    }}
                }}
                return {{clicked: false, linkCount: links.length}};
            }})()
        """
        try:
            result = await tab.evaluate(click_js_course)
            if result and result.get('clicked'):
                log(f"点击目录页: {result.get('text', '')[:30]} ({result.get('method', '')})", "success")
                await asyncio.sleep(3)
                if self._is_chapter_switched(tab.url, old_url, target_id, already_on_studentstudy):
                    log("章节切换成功", "success")
                    return True
                for _ in range(4):
                    await asyncio.sleep(1)
                    if self._is_chapter_switched(tab.url, old_url, target_id, already_on_studentstudy):
                        log("章节切换成功 (延迟)", "success")
                        return True
        except Exception as e:
            log(f"JS 点击目录页章节失败: {e}", "warning")

        # ---- Fallback: 用 zendriver find 点击 ----
        try:
            # 先尝试 studentstudy 的 posCatalog_name
            if already_on_studentstudy:
                items = await tab.evaluate(f"""
                    (() => {{
                        const items = document.querySelectorAll('.posCatalog_name');
                        return Array.from(items).map(el => ({{
                            text: el.textContent.trim().substring(0, 60),
                            onclick: (el.getAttribute('onclick') || '').substring(0, 120)
                        }}));
                    }})()
                """)
                if items:
                    for item in items:
                        onclick = item.get('onclick', '')
                        text = item.get('text', '')
                        if target_id in onclick:
                            clean = re.sub(r'\s+', ' ', text).strip()
                            if clean and len(clean) > 2:
                                try:
                                    el = await tab.find(clean, best_match=True)
                                    if el:
                                        await el.click()
                                        await asyncio.sleep(3)
                                        if self._is_chapter_switched(tab.url, old_url, target_id, already_on_studentstudy):
                                            log(f"find 点击侧边栏成功: {clean[:30]}", "success")
                                            return True
                                except Exception:
                                    continue

            # 再尝试 studentcourse 的 clicktitle
            links = await tab.evaluate("""
                (() => {
                    const links = document.querySelectorAll('a.clicktitle');
                    return Array.from(links).map(l => l.textContent.trim().substring(0, 60));
                })()
            """)
            if links:
                for text in links:
                    clean = re.sub(r'\s+', ' ', text).strip()
                    if clean and len(clean) > 2:
                        try:
                            el = await tab.find(clean, best_match=True)
                            if el:
                                await el.click()
                                await asyncio.sleep(3)
                                if self._is_chapter_switched(tab.url, old_url, target_id, already_on_studentstudy):
                                    log(f"find 点击目录页成功: {clean[:30]}", "success")
                                    return True
                        except Exception:
                            continue
        except Exception:
            pass

        # ---- 10.5 PCount.next 补充策略: 直接调用超星内部 API 切换章节 ----
        try:
            # 从章节 URL 中提取 courseId, chapterId, clazzId
            cid_m = re.search(r'courseid=(\d+)', chapter_url, re.I)
            kid_m = re.search(r'knowledgeid=(\d+)', chapter_url, re.I)
            clid_m = re.search(r'clazzid=(\d+)', chapter_url, re.I)
            if cid_m and kid_m:
                course_id = cid_m.group(1)
                chapter_id = kid_m.group(1)
                clazz_id = clid_m.group(1) if clid_m else ''
                pcount_result = await tab.evaluate(f"""
                    (() => {{
                        try {{
                            if (typeof top.PCount !== 'undefined' && top.PCount.next) {{
                                top.PCount.next(1, '{chapter_id}', '{course_id}', '{clazz_id}', '');
                                return 'called';
                            }}
                            return 'no_PCount';
                        }} catch(e) {{ return 'error:' + e.message; }}
                    }})()
                """)
                if pcount_result == 'called':
                    await asyncio.sleep(3)
                    if self._is_chapter_switched(tab.url, old_url, target_id, already_on_studentstudy):
                        log(f"PCount.next 切换章节成功", "success")
                        return True
                    for _ in range(4):
                        await asyncio.sleep(1)
                        if self._is_chapter_switched(tab.url, old_url, target_id, already_on_studentstudy):
                            log(f"PCount.next 切换章节成功 (延迟)", "success")
                            return True
                logger.debug(f"PCount.next 结果: {pcount_result}")
        except Exception as e:
            logger.debug(f"PCount.next 异常(忽略): {e}")

        log(f"未能点击章节链接 (target_id={target_id})", "warning")
        return False

    def _is_chapter_switched(self, new_url: str, old_url: str, target_id: str, was_on_studentstudy: bool) -> bool:
        """判断章节是否成功切换"""
        if 'studentstudy' not in new_url and 'knowledgestu' not in new_url:
            return False
        if was_on_studentstudy:
            # 从 studentstudy 点击：URL 变了就是切换成功
            # 或者 URL 包含目标 knowledgeid/chapterid
            if new_url != old_url:
                return True
            if target_id and target_id in new_url:
                return True
            return False
        else:
            # 从 studentcourse 点击：到达 studentstudy 就是成功
            return True

    def _is_on_same_course_page(self, current_url: str, chapter_url: str) -> bool:
        """检查当前页面是否是同一课程的页面 (studentcourse / studentstudy / knowledgestu)"""
        if ("studentcourse" not in current_url
                and "studentstudy" not in current_url
                and "knowledgestu" not in current_url):
            return False
        cur_course = re.search(r'courseid[=:](\d+)', current_url, re.IGNORECASE)
        tgt_course = re.search(r'courseid[=:](\d+)', chapter_url, re.IGNORECASE)
        if cur_course and tgt_course:
            return cur_course.group(1) == tgt_course.group(1)
        cur_clazz = re.search(r'clazzid[=:](\d+)', current_url, re.IGNORECASE)
        tgt_clazz = re.search(r'clazzid[=:](\d+)', chapter_url, re.IGNORECASE)
        if cur_clazz and tgt_clazz:
            return cur_clazz.group(1) == tgt_clazz.group(1)
        return False

    async def _random_sleep(self, min_sec: float, max_sec: float):
        """随机等待"""
        delay = random.uniform(min_sec, max_sec)
        await asyncio.sleep(delay)

    # ======================== 完整章节处理流程 ========================

    async def _is_page_blocked(self, tab: Tab) -> bool:
        """轻量检测：页面是否被 403/登录过期拦截"""
        try:
            cur_url = (tab.url or "").lower()
            if any(kw in cur_url for kw in ["login", "passport", "403", "forbidden"]):
                return True
            body_text = await tab.evaluate(
                "document.body ? document.body.innerText.substring(0, 300) : ''"
            )
            if body_text and isinstance(body_text, str):
                if "403" in body_text or "禁止访问" in body_text or "无权访问" in body_text or "登录已过期" in body_text:
                    return True
        except Exception:
            pass
        return False

    async def _check_page_403(self, tab: Tab, log: Callable) -> bool:
        """检测页面是否为 403/权限失效，返回 True 表示页面不可用"""
        try:
            cur_url = (tab.url or "").lower()
            # URL 被重定向到登录页或错误页
            # 用路径段匹配避免子串误匹配 (如 "error" 不能匹配到 "courseId")
            from urllib.parse import urlparse
            parsed = urlparse(cur_url)
            path_segments = parsed.path.split('/')
            is_login_page = (
                any(kw in path_segments for kw in ['login', 'passport', 'error', 'forbidden']) or
                (parsed.hostname and 'passport' in parsed.hostname) or
                parsed.path.strip('/') in ['login', 'passport'] or
                '403' in path_segments
            )
            if is_login_page:
                logger.debug(f"_check_page_403 URL触发: path={parsed.path}, hostname={parsed.hostname}")
                log(f"页面被重定向到: {tab.url[:80]}，可能登录已过期", "error")
                log("请重新扫码登录后再试", "warning")
                return True
            # 检查页面内容是否含 403 提示 (严格匹配，避免课程 ID 中数字误报)
            body_text = await tab.evaluate(
                "document.body ? document.body.innerText.substring(0, 500) : ''"
            )
            if body_text and isinstance(body_text, str):
                lower = body_text.lower()
                if "禁止访问" in body_text or "无权访问" in body_text or "登录已过期" in body_text:
                    log(f"页面返回 403 或权限失效，内容: {body_text[:120]}", "error")
                    log("请重新扫码登录后再试", "warning")
                    return True
                # "403" 只匹配独立出现 (如 "403 Forbidden"、"403错误"、"HTTP 403")
                import re as _re
                if _re.search(r'\b403\b', lower) and (
                    'forbidden' in lower or '错误' in body_text or '禁止' in body_text or '权限' in body_text
                ):
                    log(f"页面返回 403，内容: {body_text[:120]}", "error")
                    log("请重新扫码登录后再试", "warning")
                    return True
        except Exception as e:
            logger.debug(f"403检测异常: {e}")
        return False

    async def _check_page_404(self, tab: Tab, log: Callable) -> bool:
        """检测页面是否为 404/页面不存在，返回 True 表示页面不可用"""
        try:
            cur_url = (tab.url or "").lower()
            # URL 包含 404 关键词
            if "404" in cur_url or "notfound" in cur_url:
                log(f"页面 URL 包含 404: {tab.url[:80]}", "error")
                return True
            # 检查页面内容是否含 404 提示
            body_text = await tab.evaluate(
                "document.body ? document.body.innerText.substring(0, 500) : ''"
            )
            if body_text and isinstance(body_text, str):
                if ("页面不存在" in body_text
                        or "404" in body_text
                        or "很抱歉" in body_text and "不存在" in body_text):
                    log(f"检测到 404 页面: {body_text[:100]}", "error")
                    return True
            # 检查页面标题
            title = await tab.evaluate("document.title || ''")
            if title and isinstance(title, str) and ("404" in title or "页面不存在" in title):
                log(f"页面标题显示 404: {title}", "error")
                return True
        except Exception as e:
            logger.debug(f"404检测异常: {e}")
        return False

    async def _check_captcha_page(self, tab: Tab) -> bool:
        """检测当前页面是否为反爬虫验证码页面"""
        try:
            cur_url = (tab.url or "").lower()
            if "antispider" in cur_url or "showverify" in cur_url:
                return True
            # 检查页面特征
            body = await tab.evaluate(
                "document.body ? document.body.innerHTML.substring(0, 500) : ''"
            )
            if body and isinstance(body, str):
                if "processVerify" in body or "antispider" in body.lower():
                    return True
            title = await tab.evaluate("document.title || ''")
            if title and isinstance(title, str) and "提示页面" in title:
                # 检查是否有验证码输入框
                has_input = await tab.evaluate(
                    "!!document.getElementById('ucode')"
                )
                if has_input:
                    return True
        except Exception as e:
            logger.debug(f"验证码检测异常: {e}")
        return False

    async def _solve_captcha(self, tab: Tab, browser: zd.Browser, log: Callable, max_retries: int = 3) -> bool:
        """
        自动识别并解决反爬虫验证码
        
        Returns:
            True: 验证码解决成功，页面已跳转回正常页面
            False: 验证码解决失败
        """
        import base64
        
        # 延迟导入 ddddocr，避免未安装时影响其他功能
        try:
            import ddddocr
        except ImportError:
            log("ddddocr 未安装，无法自动识别验证码，请手动处理", "error")
            log("安装命令: pip install ddddocr", "error")
            return False
        
        for attempt in range(1, max_retries + 1):
            try:
                log(f"正在自动解决验证码 (第 {attempt}/{max_retries} 次)...", "info")
                
                # 等待验证码图片加载
                await asyncio.sleep(1.5)
                
                # 通过 canvas 提取验证码图片的 base64 数据
                img_base64 = await tab.evaluate("""
                    (() => {
                        const img = document.getElementById('ccc');
                        if (!img || !img.complete || !img.naturalWidth) return null;
                        const canvas = document.createElement('canvas');
                        canvas.width = img.naturalWidth;
                        canvas.height = img.naturalHeight;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0);
                        const dataUrl = canvas.toDataURL('image/png');
                        return dataUrl.replace(/^data:image\\/\\w+;base64,/, '');
                    })()
                """)
                
                if not img_base64 or not isinstance(img_base64, str):
                    log("无法提取验证码图片，尝试刷新验证码...", "warning")
                    # 点击验证码图片刷新
                    await tab.evaluate("document.getElementById('ccc').click()")
                    await asyncio.sleep(1.5)
                    continue
                
                # 使用 ddddocr 识别验证码
                img_bytes = base64.b64decode(img_base64)
                ocr = ddddocr.DdddOcr(show_ad=False)
                captcha_text = ocr.classification(img_bytes)
                
                if not captcha_text:
                    log("OCR 识别结果为空，刷新验证码重试...", "warning")
                    await tab.evaluate("document.getElementById('ccc').click()")
                    await asyncio.sleep(1.5)
                    continue
                
                # 只保留字母数字，最多4位
                captcha_text = re.sub(r'[^0-9a-zA-Z]', '', captcha_text)[:4]
                log(f"OCR 识别验证码: {captcha_text}", "info")
                
                if len(captcha_text) < 4:
                    log(f"识别结果不足4位({len(captcha_text)}位)，刷新重试...", "warning")
                    await tab.evaluate("document.getElementById('ccc').click()")
                    await asyncio.sleep(1.5)
                    continue
                
                # 填写验证码并提交
                await tab.evaluate(f"""
                    (() => {{
                        const input = document.getElementById('ucode');
                        if (!input) return false;
                        input.value = '{captcha_text}';
                        input.dispatchEvent(new Event('input', {{bubbles: true}}));
                        // 提交表单
                        const form = input.closest('form');
                        if (form) {{
                            form.submit();
                            return true;
                        }}
                        // 备用：找提交按钮点击
                        const btn = document.querySelector('.submit, input[type=submit]');
                        if (btn) {{ btn.click(); return true; }}
                        return false;
                    }})()
                """)
                
                # 等待页面跳转
                old_url = tab.url
                for _ in range(10):
                    await asyncio.sleep(0.5)
                    if tab.url != old_url:
                        break
                
                # 检查是否成功（不再在验证码页面）
                if not await self._check_captcha_page(tab):
                    log(f"验证码 {captcha_text} 提交成功，已跳转回正常页面", "success")
                    await asyncio.sleep(1)
                    return True
                else:
                    log(f"验证码 {captcha_text} 可能不正确，页面仍在验证码页，重试...", "warning")
                    # 等待一下让页面稳定（验证码可能已刷新）
                    await asyncio.sleep(1)
                    
            except Exception as e:
                log(f"验证码处理第 {attempt} 次异常: {e}", "warning")
                await asyncio.sleep(1)
        
        log(f"验证码自动解决失败（已尝试 {max_retries} 次），请手动处理", "error")
        return False

    async def _ensure_no_captcha(self, tab: Tab, browser: zd.Browser = None, log: Callable = None) -> Tab:
        """
        确保当前页面不是验证码拦截页，如果是则自动解决
        
        Returns:
            处理后的 tab（可能是跳转后的新 tab）
        """
        _log = log or (lambda msg, level="info": logger.info(msg))
        
        if not browser:
            return tab
            
        try:
            if await self._check_captcha_page(tab):
                _log("检测到反爬虫验证码拦截，正在自动处理...", "warning")
                success = await self._solve_captcha(tab, browser, _log)
                if success:
                    _log("验证码已自动解决，继续执行...", "success")
                    # 返回当前 tab（页面已跳转）
                    return tab
                else:
                    _log("验证码自动解决失败，任务可能受阻", "error")
        except Exception as e:
            logger.debug(f"验证码检查异常: {e}")
        
        return tab

    async def process_chapter(
        self,
        tab: Tab,
        chapter_url: str,
        browser: zd.Browser = None,
        on_progress: Callable = None,
        on_log: Callable = None,
        pause_event: asyncio.Event = None,
        stop_check: Callable[[], bool] = None,
        force_all: bool = False,
    ) -> Dict[str, Any]:
        """
        处理单个章节的所有任务点

        Args:
            force_all: True 时忽略 finished 标记，全部任务点重新执行

        Returns:
            {"success": bool, "quizzes": int, "videos": int, "errors": int}
        """
        progress = on_progress or (lambda *a: None)
        log = on_log or (lambda msg, level="info": logger.info(msg))
        result = {"success": True, "quizzes": 0, "videos": 0, "errors": 0, "skipped": False}

        # 检查 SQLite 完成数据库：如果章节已标记完成且非 force_all，直接跳过
        if not force_all and self._completed_chapter_keys:
            import re as _re
            cid_m = _re.search(r'courseid=(\d+)', chapter_url, _re.IGNORECASE)
            kid_m = _re.search(r'knowledgeid=(\d+)', chapter_url, _re.IGNORECASE)
            if cid_m and kid_m:
                ch_key = f"{cid_m.group(1)}:{kid_m.group(1)}"
                if ch_key in self._completed_chapter_keys:
                    log(f"章节已在完成记录中（{ch_key}），跳过", "success")
                    result["skipped"] = True
                    return result

        try:
            # knowledgestu URL 可直接导航，不做路径转换
            navigate_url = chapter_url

            current_url = tab.url
            on_same_course = self._is_on_same_course_page(current_url, chapter_url)
            on_studentstudy = ('studentstudy' in current_url or 'knowledgestu' in current_url)
            # 旧版 studentcourse 也视为目录页状态（兼容已加载的旧页面）
            on_studentcourse = 'studentcourse' in current_url

            # ---- 智能导航：优先在侧边栏直接切换章节 ----
            if on_same_course and on_studentstudy:
                # 已在同课程的 studentstudy 页面，直接在侧边栏点击下一个章节
                log(f"在侧边栏直接切换章节 (无需返回目录页)")
                old_url = current_url
                clicked = await self._click_chapter_in_sidebar(tab, chapter_url, browser, log)
                if clicked:
                    # 等待页面切换（URL 变化或内容刷新）
                    for _ in range(6):
                        await asyncio.sleep(0.5)
                        if tab.url != old_url or 'studentstudy' in tab.url or 'knowledgestu' in tab.url:
                            break
                    await asyncio.sleep(2)
                    log("侧边栏切换章节成功", "success")
                    # 侧边栏切换后也检查验证码
                    tab = await self._ensure_no_captcha(tab, browser, log)
                else:
                    # 侧边栏点击失败，回退到目录页导航
                    log("侧边栏切换失败，回退到目录页导航", "warning")
                    if browser:
                        try:
                            tab = await browser.get(navigate_url)
                        except Exception as e:
                            if 'ERR_ABORTED' in str(e) or 'ERR_FAILED' in str(e):
                                await asyncio.sleep(1)
                            else:
                                raise
                    await asyncio.sleep(3)
                    clicked = await self._click_chapter_in_sidebar(tab, chapter_url, browser, log)
                    if not clicked:
                        log("章节链接点击失败，跳过", "warning")
                        result["skipped"] = True
                        return result
                    for _ in range(6):
                        await asyncio.sleep(0.5)
                        if 'studentstudy' in tab.url or 'knowledgestu' in tab.url:
                            break
                    await asyncio.sleep(2)

            elif on_same_course and on_studentcourse:
                # 已在同课程的目录页，直接点击章节链接
                log(f"在目录页点击章节: {chapter_url[:60]}")
                clicked = await self._click_chapter_in_sidebar(tab, chapter_url, browser, log)
                if not clicked:
                    log("章节链接点击失败，跳过", "warning")
                    result["skipped"] = True
                    return result
                for _ in range(6):
                    await asyncio.sleep(0.5)
                    if 'studentstudy' in tab.url or 'knowledgestu' in tab.url:
                        break
                await asyncio.sleep(2)

            else:
                # 不在同课程页面，需要先导航到 studentcourse 目录页，再通过侧边栏进入章节
                # knowledgestu 不能直接访问，必须从 studentcourse 点击进去
                course_id_m = re.search(r'courseid[=:](\d+)', chapter_url, re.I)
                clazz_id_m = re.search(r'clazzid[=:](\d+)', chapter_url, re.I)
                cpi_m = re.search(r'cpi=(\d+)', chapter_url)
                course_id = course_id_m.group(1) if course_id_m else ''
                clazz_id = clazz_id_m.group(1) if clazz_id_m else ''
                cpi_val = cpi_m.group(1) if cpi_m else ''

                if course_id and clazz_id:
                    # 动态提取基础路径（适配用户子域名，避免 404）
                    base_url = self._get_base_url(current_url)
                    sc_url = f"{base_url}/mycourse/studentcourse?courseid={course_id}&clazzid={clazz_id}&mooc2=1"
                    if cpi_val:
                        sc_url += f"&cpi={cpi_val}"
                    # 备用 URL：用通用域名，避免子域名不匹配导致 404
                    sc_url_fallback = f"https://mooc2-ans.chaoxing.com/mycourse/studentcourse?courseid={course_id}&clazzid={clazz_id}&mooc2=1"
                    if cpi_val:
                        sc_url_fallback += f"&cpi={cpi_val}"
                    log(f"导航到目录页(studentcourse): {sc_url[:70]}")
                else:
                    # 无法构造 studentcourse URL，回退到直接访问
                    sc_url = ""
                    sc_url_fallback = ""
                    log(f"导航到目录页: {navigate_url[:70]}")

                target_url = sc_url or navigate_url
                if browser:
                    try:
                        tab = await browser.get(target_url)
                    except Exception as e:
                        if 'ERR_ABORTED' in str(e) or 'ERR_FAILED' in str(e):
                            await asyncio.sleep(1)
                        else:
                            raise

                # 等待页面加载完成
                prev_url = ""
                for _ in range(8):
                    await asyncio.sleep(0.5)
                    current = tab.url
                    if current == prev_url:
                        break
                    prev_url = current
                await asyncio.sleep(2)

                # ---- 反爬虫验证码检测 ----
                tab = await self._ensure_no_captcha(tab, browser, log)

                # 检测 404：如果子域名不匹配导致 404，用通用域名重试
                if sc_url and sc_url_fallback and await self._check_page_404(tab, log):
                    log("目录页 404，尝试用通用域名重试...", "warning")
                    if browser:
                        try:
                            tab = await browser.get(sc_url_fallback)
                        except Exception:
                            await asyncio.sleep(1)
                        prev_url = ""
                        for _ in range(6):
                            await asyncio.sleep(0.5)
                            if tab.url == prev_url: break
                            prev_url = tab.url
                        await asyncio.sleep(2)

                # 检测 403：如果 studentcourse 也返回 403，尝试回退到 knowledgestu
                if sc_url and await self._check_page_403(tab, log):
                    log("studentcourse 页面不可用，尝试直接访问章节页", "warning")
                    if browser:
                        try:
                            tab = await browser.get(navigate_url)
                        except Exception as e:
                            if 'ERR_ABORTED' in str(e) or 'ERR_FAILED' in str(e):
                                await asyncio.sleep(1)
                            else:
                                raise
                    prev_url = ""
                    for _ in range(8):
                        await asyncio.sleep(0.5)
                        current = tab.url
                        if current == prev_url: break
                        prev_url = current
                    await asyncio.sleep(2)
                    # 直接访问时不需要侧边栏点击，页面已经是章节页
                else:
                    # 从 studentcourse 目录页点击章节链接进入 studentstudy
                    log(f"点击章节链接: {chapter_url[:60]}")
                    clicked = await self._click_chapter_in_sidebar(tab, chapter_url, browser, log)
                    if not clicked:
                        log("章节链接点击失败，跳过", "warning")
                        result["skipped"] = True
                        return result

                    # 等待 studentstudy 页面加载完成
                    for _ in range(6):
                        await asyncio.sleep(0.5)
                        if 'studentstudy' in tab.url or 'knowledgestu' in tab.url:
                            break
                    await asyncio.sleep(2)

            log("页面就绪")

            # ---- 3.1 闯关模式检测 ----
            # 检测侧边栏是否有闯关/解锁模式标记
            try:
                breaking_mode = await tab.evaluate("""
                    (() => {
                        const els = document.querySelectorAll('.catalog_points_sa, .catalog_points_er');
                        return els.length > 0;
                    })()
                """)
                if breaking_mode:
                    log("检测到闯关/解锁模式，部分章节可能需要手动解锁", "warning")
            except Exception:
                pass

            # ---- 3.2 闯关卡死检测: 同一章节重复进入3次则告警 ----
            chapter_id = ""
            try:
                cid_match = re.search(r'(?:knowledgeid|chapterid)[=:](\d+)', chapter_url, re.I)
                if cid_match:
                    chapter_id = cid_match.group(1)
            except Exception:
                pass
            if chapter_id:
                if not hasattr(self, '_chapter_entry_counts'):
                    self._chapter_entry_counts = {}
                self._chapter_entry_counts[chapter_id] = self._chapter_entry_counts.get(chapter_id, 0) + 1
                entry_count = self._chapter_entry_counts[chapter_id]
                if entry_count >= 3:
                    log(f"章节[{chapter_id}]已重复进入{entry_count}次，可能是闯关模式卡住或章节测试未完成，请手动检查", "warning")
                    self._chapter_entry_counts[chapter_id] = 1  # 重置计数器

            # ---- 3.3 当前章节完成状态检测: 检测侧边栏完成图标 ----
            try:
                chapter_done = await tab.evaluate("""
                    (() => {
                        const active = document.querySelector('.posCatalog_active');
                        if (active && active.querySelector('.icon_Completed')) return true;
                        return false;
                    })()
                """)
                if chapter_done:
                    log("侧边栏显示当前章节已完成(icon_Completed)，跳过", "success")
                    result["skipped"] = True
                    return result
            except Exception:
                pass

            # ---- 403 / 权限失效检测 ----
            is_403 = await self._check_page_403(tab, log)
            if is_403:
                result["errors"] += 1
                result["skipped"] = True
                return result

            # ---- 404 / 页面不存在检测 + 回退重试 ----
            is_404 = await self._check_page_404(tab, log)
            if is_404:
                log("检测到 404 页面，尝试回退到目录页重新进入...", "warning")
                # 回退策略：导航到 studentcourse 目录页，再通过侧边栏点击进入
                course_id_m = re.search(r'courseid[=:](\d+)', chapter_url, re.I)
                clazz_id_m = re.search(r'clazzid[=:](\d+)', chapter_url, re.I)
                cpi_m = re.search(r'cpi=(\d+)', chapter_url)
                course_id = course_id_m.group(1) if course_id_m else ''
                clazz_id = clazz_id_m.group(1) if clazz_id_m else ''
                cpi_val = cpi_m.group(1) if cpi_m else ''

                if course_id and clazz_id and browser:
                    # 用通用域名构造 URL，避免子域名错误
                    sc_url = f"https://mooc2-ans.chaoxing.com/mycourse/studentcourse?courseid={course_id}&clazzid={clazz_id}&mooc2=1"
                    if cpi_val:
                        sc_url += f"&cpi={cpi_val}"
                    log(f"回退到目录页(通用域名): {sc_url[:70]}")
                    try:
                        tab = await browser.get(sc_url)
                    except Exception:
                        await asyncio.sleep(1)

                    # 等待目录页加载
                    for _ in range(6):
                        await asyncio.sleep(0.5)
                        if 'studentcourse' in (tab.url or '').lower():
                            break
                    await asyncio.sleep(2)

                    # 检查目录页是否也是 404
                    dir_404 = await self._check_page_404(tab, log)
                    if dir_404:
                        log("目录页也是 404，可能 session 已失效，跳过此章节", "error")
                        result["errors"] += 1
                        result["skipped"] = True
                        return result

                    # 从目录页点击章节链接进入
                    log(f"从目录页重新点击章节链接: {chapter_url[:60]}")
                    clicked = await self._click_chapter_in_sidebar(tab, chapter_url, browser, log)
                    if clicked:
                        for _ in range(6):
                            await asyncio.sleep(0.5)
                            if 'studentstudy' in tab.url or 'knowledgestu' in tab.url:
                                break
                        await asyncio.sleep(2)

                        # 再次检查 404
                        retry_404 = await self._check_page_404(tab, log)
                        if retry_404:
                            log("重试后仍然是 404，跳过此章节", "error")
                            result["errors"] += 1
                            result["skipped"] = True
                            return result
                        log("404 回退重试成功", "success")
                    else:
                        log("回退后点击章节链接失败，跳过", "warning")
                        result["errors"] += 1
                        result["skipped"] = True
                        return result
                else:
                    log("无法构造目录页 URL，跳过此章节", "error")
                    result["errors"] += 1
                    result["skipped"] = True
                    return result

            # ---- 8.1 旧版→新版重定向 ----
            # 超星旧版页面需要切换到新版，否则某些功能不可用
            try:
                page_url = (tab.url or '').lower()
                need_redirect = False
                if 'mooc2=0' in page_url or 'studentcourse' in page_url:
                    need_redirect = True
                if need_redirect:
                    redirect_result = await tab.evaluate("""
                        (() => {
                            const url = new URL(window.location.href);
                            let changed = false;
                            if (url.searchParams.get('mooc2') !== '1') { url.searchParams.set('mooc2', '1'); changed = true; }
                            if (url.searchParams.get('newMooc') !== 'true') { url.searchParams.set('newMooc', 'true'); changed = true; }
                            if (changed) { window.location.replace(url.toString()); return true; }
                            return false;
                        })()
                    """)
                    if redirect_result:
                        log("检测到旧版超星页面，已自动切换到新版", "info")
                        await asyncio.sleep(3)
            except Exception:
                pass

            # ---- 8.2 任务页面→章节页面重定向 ----
            # 如果当前页面是任务页面而非章节页面，自动点击“章节”链接
            try:
                page_url = (tab.url or '').lower()
                if 'pageheader=0' in page_url:
                    chapter_link = await tab.evaluate("""
                        (() => {
                            const a = document.querySelector('a[title="章节"]');
                            if (a) { a.click(); return true; }
                            return false;
                        })()
                    """)
                    if chapter_link:
                        log("已自动切换到章节列表页面", "info")
                        await asyncio.sleep(2)
            except Exception:
                pass

            # 检查是否是章节分组页
            # studentcourse / studentstudy 页面本身是有效的章节容器，不跳过
            page_url_lower = tab.url.lower()
            is_group_page = (
                "studentcourse" not in page_url_lower
                and "studentstudy" not in page_url_lower
                and "knowledgestu" not in page_url_lower
                and "chapterid" not in page_url_lower
                and "knowledgeid" not in page_url_lower
            )
            if is_group_page:
                log("章节分组页（无章节ID），跳过", "info")
                result["skipped"] = True
                return result

            # ---- 12.1 自动滚动到当前活跃章节 ----
            try:
                await tab.evaluate("""
                    (() => {
                        const active = document.querySelector('.posCatalog_active');
                        if (active) active.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    })()
                """)
            except Exception:
                pass

            # 获取任务点列表
            tasks = await self.get_task_points(tab, browser)
            total = len(tasks)
            log(f"检测到 {total} 个任务点")

            if total == 0:
                log("无任务点", "info")
                result["skipped"] = True
                return result

            # ---- 批量重检所有任务点完成状态 ----
            # 初始检测可能因为 DOM 渲染延迟导致误判，批量重检确保准确
            if not force_all:
                tasks = await self._batch_recheck_all_tasks(tab, tasks, on_log=log)

            all_finished = not force_all and all(t.get("finished") for t in tasks)
            if all_finished:
                # 记录每个任务的完成状态，便于排查误判
                for i, t in enumerate(tasks):
                    log(f"  任务[{i+1}] {t.get('name','')} finished={t.get('finished')} type={t.get('type','')}", "info")
                log(f"全部 {total} 个任务点已完成，跳过", "success")
                result["skipped"] = True
                return result

            # 处理每个任务
            work_idx = 0   # work 任务计数器
            pdf_idx = 0    # pdf 任务计数器
            video_idx = 0  # video 任务计数器
            audio_idx = 0  # audio 任务计数器
            ppt_idx = 0    # ppt 任务计数器
            reading_idx = 0  # reading 任务计数器
            link_idx = 0   # link 任务计数器
            processed_mids = set()  # 10.1 任务去重: 通过 mid 唯一标识避免重复处理
            for i, task in enumerate(tasks):
                # 暂停/停止检查
                if stop_check and stop_check():
                    log("收到停止指令", "warning")
                    break
                if pause_event and not pause_event.is_set():
                    log("收到暂停指令，等待继续...", "info")
                    await pause_event.wait()
                    log("已继续执行", "success")

                # 检测反作弊重定向
                recovered = await self._check_detect_redirect(tab, browser, chapter_url, log)
                if recovered is None:
                    log("页面被反作弊系统拦截，无法恢复", "warning")
                    result["errors"] += 1
                    break
                elif recovered is not tab:
                    tab = recovered
                    log("页面已恢复，继续处理", "info")
                    await asyncio.sleep(2)

                task_name = task.get("name") or task.get("type", "未知")
                task_type = task.get("type", "unknown")
                progress(task_name, i + 1, total)

                # 无论任务是否已完成，都必须递增对应类型的索引
                # 因为 iframe_index 对应 DOM 中所有同类 iframe 的位置（包括已完成的）
                # 如果跳过已完成任务不递增，后续未完成任务的 iframe_index 就会错位
                if task_type == "video":
                    current_video_idx = video_idx
                    video_idx += 1
                elif task_type == "audio":
                    current_audio_idx = audio_idx
                    audio_idx += 1
                elif task_type == "pdf":
                    current_pdf_idx = pdf_idx
                    pdf_idx += 1
                elif task_type == "work":
                    current_work_idx = work_idx
                    work_idx += 1
                elif task_type == "ppt":
                    current_ppt_idx = ppt_idx
                    ppt_idx += 1
                elif task_type == "reading":
                    current_reading_idx = reading_idx
                    reading_idx += 1
                elif task_type == "link":
                    current_link_idx = link_idx
                    link_idx += 1

                # ---- 10.1 任务去重: 通过 mid 唯一标识避免重复处理 ----
                import json as _json
                try:
                    _data = _json.loads(task.get("data_attr", "{}"))
                    _mid = str(_data.get("mid") or "")
                except Exception:
                    _mid = ""
                if _mid and _mid in processed_mids:
                    log(f"[{i+1}/{total}] {task_name} - mid重复({_mid})，跳过", "info")
                    continue
                if _mid:
                    processed_mids.add(_mid)

                # ---- 实时重检当前任务点完成状态 ----
                # 防止侧边栏已标记完成但初始检测遗漏的情况
                if not force_all and not task.get("finished"):
                    try:
                        is_done = await self._recheck_single_task_finished(tab, task, on_log=log)
                        if is_done:
                            task["finished"] = True
                            log(f"[{i+1}/{total}] {task_name} - 实时检测确认已完成，跳过播放", "success")
                    except Exception as recheck_err:
                        logger.debug(f"实时重检异常(忽略): {recheck_err}")

                if not force_all and task.get("finished"):
                    log(f"[{i+1}/{total}] {task_name} - 已完成(finished=true)，跳过", "info")
                    continue

                log(f"[{i+1}/{total}] 正在处理: {task_name} ({task_type})")

                iframe_src = task.get("iframe_src", "")

                if task_type == "video" and self._config.auto_video:
                    success = await self.play_video(tab, "cards", on_log=log, iframe_index=current_video_idx,
                                                    pause_event=pause_event, stop_check=stop_check)
                    if success:
                        result["videos"] += 1
                    else:
                        result["errors"] += 1

                elif task_type == "audio" and self._config.auto_video:
                    success = await self.play_audio(tab, "cards", on_log=log, iframe_index=current_audio_idx,
                                                    pause_event=pause_event, stop_check=stop_check)
                    if success:
                        result["videos"] += 1
                    else:
                        result["errors"] += 1

                elif task_type == "work":
                    # ---- 4.1 章节测试已完成检测: 检测 .testTit_status_complete ----
                    test_already_done = False
                    try:
                        test_check_js = f"""
                            (() => {{
                                // 在 cards iframe 中查找章节测试状态
                                const statusEls = _doc.querySelectorAll('.testTit_status');
                                for (const el of statusEls) {{
                                    if (el.classList.contains('testTit_status_complete')) return true;
                                }}
                                return false;
                            }})()
                        """
                        for frag in ["cards", "knowledge"]:
                            done = await self._eval_in_iframe(tab, frag, f"return {test_check_js};")
                            if done is True:
                                test_already_done = True
                                break
                    except Exception:
                        pass

                    if test_already_done:
                        log(f"[{i+1}/{total}] {task_name} - 章节测试已完成(testTit_status_complete)，跳过答题", "success")
                        continue

                    # work 任务: cards iframe → work module → quiz iframe
                    work_result = await self._process_work_task(
                        tab, browser=browser, chapter_url=chapter_url,
                        on_log=log,
                        pause_event=pause_event, stop_check=stop_check,
                        work_index=current_work_idx,
                    )
                    if work_result.get("success"):
                        result["quizzes"] += 1
                    else:
                        result["errors"] += 1

                elif task_type == "pdf":
                    # PDF 任务: 主页面 → cards iframe → pdf module iframe
                    # 使用 current_pdf_idx 指定当前 PDF 的 iframe 索引，支持多个 PDF
                    log(f"[{i+1}/{total}] 处理PDF (索引:{current_pdf_idx})")

                    # 渐进式滚动: 模拟真实阅读行为，从上到下逐步滚动
                    # 超星 PDF 任务需要滚动触发完成状态
                    scroll_steps = 5
                    pdf_scroll_ok = False
                    for step in range(scroll_steps + 1):
                        progress_pct = int(step / scroll_steps * 100)
                        scroll_js = f"""
                            const pv = _doc.querySelector('#panView');
                            var scrollTarget = null;
                            if (pv && pv.contentWindow) {{
                                scrollTarget = pv.contentWindow;
                            }} else if (_doc.documentElement.scrollHeight > _doc.documentElement.clientHeight) {{
                                scrollTarget = _doc.documentElement;
                            }} else if (_doc.body) {{
                                scrollTarget = _doc.body;
                            }}
                            if (scrollTarget) {{
                                var maxScroll = (scrollTarget.scrollHeight || 99999) - (scrollTarget.clientHeight || 800);
                                var scrollTo = Math.min(maxScroll, Math.round(maxScroll * {step} / {scroll_steps}));
                                scrollTarget.scrollTo(0, scrollTo);
                                return {{scrolled: true, step: {step}, scrollTo: scrollTo, maxScroll: maxScroll}};
                            }}
                            return {{scrolled: false, step: {step}}};
                        """
                        result_scroll = await self._eval_in_nested_module_iframe(
                            tab, "modules/pdf", scroll_js,
                            iframe_index=current_pdf_idx
                        )
                        if result_scroll and isinstance(result_scroll, dict):
                            if result_scroll.get('scrolled'):
                                pdf_scroll_ok = True
                            elif step == 0:
                                log(f"[{i+1}/{total}] PDF滚动目标未找到，尝试备用方案", "warning")
                        if step == 0:
                            log(f"[{i+1}/{total}] PDF滚动进度: {progress_pct}%")
                        await asyncio.sleep(0.6)

                    # 滚动 PDF 内嵌的 iframe (如有)
                    await self._eval_in_nested_module_iframe(
                        tab, "modules/pdf", """
                            const innerIframes = _doc.querySelectorAll('iframe');
                            var scrolled = 0;
                            for (const f of innerIframes) {
                                try {
                                    const innerDoc = f.contentDocument || f.contentWindow.document;
                                    const innerWin = f.contentWindow;
                                    var maxScroll = (innerDoc.documentElement.scrollHeight || 99999) - (innerDoc.documentElement.clientHeight || 800);
                                    innerWin.scrollTo(0, maxScroll);
                                    scrolled++;
                                } catch(e) {}
                            }
                            return {scrolled: scrolled};
                        """,
                        iframe_index=current_pdf_idx
                    )
                    # ---- 5.1 finishJob: 补充完成策略 ----
                    # 超星某些 PDF/PPT 任务点需要调用 win.finishJob() 才能完成
                    try:
                        finish_result = await self._eval_in_nested_module_iframe(
                            tab, "modules/pdf", """
                                (() => {
                                    try {
                                        if (typeof _win.finishJob === 'function') {
                                            _win.finishJob();
                                            return {called: true};
                                        }
                                        // 尝试在 iframe 的 window 中查找
                                        const frames = _doc.querySelectorAll('iframe');
                                        for (const f of frames) {
                                            try {
                                                if (typeof f.contentWindow.finishJob === 'function') {
                                                    f.contentWindow.finishJob();
                                                    return {called: true, via: 'inner_iframe'};
                                                }
                                            } catch(e) {}
                                        }
                                        return {called: false, reason: 'no finishJob function'};
                                    } catch(e) { return {called: false, error: e.message}; }
                                })()
                            """,
                            iframe_index=current_pdf_idx
                        )
                        if finish_result and isinstance(finish_result, dict) and finish_result.get('called'):
                            log(f"[{i+1}/{total}] PDF finishJob 调用成功", "success")
                            pdf_scroll_ok = True
                    except Exception as fj_err:
                        logger.debug(f"PDF finishJob 异常(忽略): {fj_err}")

                    # 等待超星服务器标记完成
                    await asyncio.sleep(2)
                    if pdf_scroll_ok:
                        log(f"[{i+1}/{total}] PDF任务完成", "success")
                    else:
                        log(f"[{i+1}/{total}] PDF滚动可能未生效，任务标记为失败", "error")
                        result["errors"] += 1

                elif task_type == "ppt":
                    # ---- 5.2 PPT 任务: swiperNext 翻页 + finishJob ----
                    log(f"[{i+1}/{total}] 处理PPT (索引:{current_ppt_idx})")
                    ppt_ok = False
                    # 检测 swiper-container 并逐页翻阅
                    swiper_result = await self._eval_in_nested_module_iframe(
                        tab, "modules/ppt", """
                            (() => {
                                const slides = _doc.querySelectorAll('.swiper-container .swiper-slide');
                                if (slides.length > 0) {
                                    return {has_swiper: true, count: slides.length};
                                }
                                return {has_swiper: false};
                            })()
                        """,
                        iframe_index=current_ppt_idx
                    )
                    if swiper_result and swiper_result.get('has_swiper'):
                        slide_count = swiper_result.get('count', 1)
                        log(f"[{i+1}/{total}] PPT 检测到 {slide_count} 页 swiper，开始翻阅")
                        # 静音音频
                        await self._eval_in_nested_module_iframe(
                            tab, "modules/ppt", """
                                _doc.querySelectorAll('audio, video').forEach(el => { el.muted = true; });
                            """,
                            iframe_index=current_ppt_idx
                        )
                        # 逐页翻阅
                        for page in range(slide_count):
                            await self._eval_in_nested_module_iframe(
                                tab, "modules/ppt", """
                                    if (typeof _win.swiperNext === 'function') _win.swiperNext();
                                """,
                                iframe_index=current_ppt_idx
                            )
                            await asyncio.sleep(1)
                        await asyncio.sleep(3)
                        ppt_ok = True
                        log(f"[{i+1}/{total}] PPT swiper 翻阅完成", "success")
                    else:
                        # 无 swiper，尝试 finishJob
                        finish_result = await self._eval_in_nested_module_iframe(
                            tab, "modules/ppt", """
                                (() => {
                                    if (typeof _win.finishJob === 'function') { _win.finishJob(); return true; }
                                    return false;
                                })()
                            """,
                            iframe_index=current_ppt_idx
                        )
                        if finish_result:
                            ppt_ok = True
                            log(f"[{i+1}/{total}] PPT finishJob 调用成功", "success")
                        else:
                            log(f"[{i+1}/{total}] PPT 无 swiper 且无 finishJob", "warning")
                    if not ppt_ok:
                        result["errors"] += 1

                elif task_type == "reading":
                    # ---- 5.3 定时阅读任务 (2026新版 /readsvr/book/mooc) ----
                    log(f"[{i+1}/{total}] 处理阅读任务 (索引:{current_reading_idx})")
                    reading_ok = False
                    # 检查是否是新版定时阅读 (有 #reader 元素)
                    is_timed = await self._eval_in_nested_module_iframe(
                        tab, "readsvr", """
                            (() => {
                                if (_doc.querySelector('#reader')) return 'timed';
                                if (_doc.querySelector('#ReadWeb')) return 'readweb';
                                return 'unknown';
                            })()
                        """,
                        iframe_index=current_reading_idx
                    )
                    if is_timed == 'timed':
                        # 定时阅读: 获取 timing 参数，等待后翻页
                        timing_sec = 60  # 默认60秒
                        try:
                            t_val = await self._eval_in_nested_module_iframe(
                                tab, "readsvr",
                                "return new URL(_doc.location.href).searchParams.get('timing') || '60';",
                                iframe_index=current_reading_idx
                            )
                            if t_val:
                                timing_sec = int(t_val) + 3
                        except Exception:
                            pass
                        log(f"[{i+1}/{total}] 定时阅读: 需等待 {timing_sec} 秒")
                        # 等待并跳转正文页
                        await asyncio.sleep(timing_sec)
                        await self._eval_in_nested_module_iframe(
                            tab, "readsvr", """
                                (() => {
                                    const jumper = _doc.querySelector('#pagejump');
                                    if (jumper) { jumper.value = '5'; jumper.dispatchEvent(new Event('change')); }
                                })()
                            """,
                            iframe_index=current_reading_idx
                        )
                        log(f"[{i+1}/{total}] 已跳转正文页，等待 {timing_sec} 秒", "info")
                        await asyncio.sleep(timing_sec)
                        # 跳转封底页
                        await self._eval_in_nested_module_iframe(
                            tab, "readsvr", """
                                (() => {
                                    const jumper = _doc.querySelector('#pagejump');
                                    if (jumper) { jumper.value = '7'; jumper.dispatchEvent(new Event('change')); }
                                })()
                            """,
                            iframe_index=current_reading_idx
                        )
                        log(f"[{i+1}/{total}] 已跳转封底页", "info")
                        await asyncio.sleep(3)
                        # 点击完成
                        await self._eval_in_nested_module_iframe(
                            tab, "readsvr", """
                                (() => {
                                    const pagers = _doc.querySelectorAll('.readerPager');
                                    for (const el of pagers) {
                                        if (el.style.zIndex === '101') { el.click(); return true; }
                                    }
                                    if (pagers.length > 0) { pagers[0].click(); return true; }
                                    return false;
                                })()
                            """,
                            iframe_index=current_reading_idx
                        )
                        reading_ok = True
                        log(f"[{i+1}/{total}] 定时阅读完成", "success")
                    elif is_timed == 'readweb':
                        # 普通书籍: 跳末页
                        await asyncio.sleep(5)
                        await self._eval_in_nested_module_iframe(
                            tab, "readsvr", """
                                (() => {
                                    try {
                                        if (typeof readweb !== 'undefined' && readweb.goto) {
                                            readweb.goto(typeof epage !== 'undefined' ? epage : 9999);
                                            return true;
                                        }
                                        return false;
                                    } catch(e) { return false; }
                                })()
                            """,
                            iframe_index=current_reading_idx
                        )
                        reading_ok = True
                        log(f"[{i+1}/{total}] 书籍阅读跳末页完成", "success")
                    else:
                        log(f"[{i+1}/{total}] 阅读任务类型未知", "warning")
                    if not reading_ok:
                        result["errors"] += 1

                elif task_type == "link":
                    # ---- 6.1 链接任务: 自动点击完成，不弹窗 ----
                    log(f"[{i+1}/{total}] 处理链接任务 (索引:{current_link_idx})")
                    link_ok = await self._eval_in_nested_module_iframe(
                        tab, "hyperlink", """
                            (() => {
                                const a = _doc.querySelector('#hyperlink');
                                if (!a) return false;
                                const _click = a.onclick;
                                a.onclick = () => false;  // 阻止弹窗
                                a.click();
                                a.onclick = _click;  // 还原
                                return true;
                            })()
                        """,
                        iframe_index=current_link_idx
                    )
                    if link_ok:
                        await asyncio.sleep(3)
                        log(f"[{i+1}/{total}] 链接任务完成", "success")
                    else:
                        log(f"[{i+1}/{total}] 链接任务未找到 #hyperlink 元素", "warning")
                        result["errors"] += 1

                else:
                    log(f"[{i+1}/{total}] 未知任务类型: {task_type}，跳过")

                await self._random_sleep(1, 2.5)

            log(f"章节处理完成: 测验{result['quizzes']}个, 视频{result['videos']}个")

            # ---- 后处理：重新检测超星服务端完成状态 ----
            # 以服务端的 ans-job-finished / jobUnfinishCount 标记为准
            await asyncio.sleep(1)  # 等待服务端更新状态
            try:
                post_tasks = await self.get_task_points(tab, browser)
                if post_tasks:
                    all_done_now = all(t.get("finished") for t in post_tasks)
                    if all_done_now:
                        log(f"超星服务端确认: 全部 {len(post_tasks)} 个任务点已完成", "success")
                        result["success"] = True
                        result["errors"] = 0
                    elif result["errors"] == 0:
                        # 服务端标记未更新（ans-job-finished 可能延迟），但处理过程无错误
                        # 说明任务已被跳过（already_submitted）或已成功处理，不覆盖 success
                        log(f"处理无错误，任务点完成状态以本地结果为准", "info")
                        result["success"] = True
                    else:
                        unfinished = [t for t in post_tasks if not t.get("finished")]
                        log(f"超星服务端: 仍有 {len(unfinished)} 个任务点未完成", "info")
                        result["success"] = False
            except Exception as e:
                logger.debug(f"后处理完成检测异常: {e}")
                # 检测失败时保留原结果
                result["success"] = result["errors"] == 0

            return result

        except Exception as e:
            logger.error(f"处理章节异常: {e}")
            result["success"] = False
            return result

    async def _process_work_task(
        self,
        tab: Tab,
        browser: zd.Browser = None,
        chapter_url: str = "",
        on_log: Callable = None,
        pause_event: asyncio.Event = None,
        stop_check: Callable[[], bool] = None,
        work_index: int = 0,
    ) -> Dict[str, Any]:
        """
        处理章节测验/作业任务

        work 任务有多层 iframe:
        cards_frame (studentcourse) → work module iframe → quiz iframe
        
        work_index: 当前 work 任务在所有 work 任务中的索引(从0开始)

        Returns:
            {"success": bool, "page_refreshed": bool}
        """
        log = on_log or (lambda msg, level="info": logger.info(msg))

        # 全局锁：同一 work 任务仅允许处理一次（防止重复执行）
        work_lock_key = f"{chapter_url}:{work_index}"
        if work_lock_key in self._processed_works:
            log(f"任务已在本次会话中处理过，跳过（锁: {work_lock_key[:60]}）", "info")
            return {"success": True, "skipped": True, "duplicate": True}
        self._processed_works.add(work_lock_key)

        try:
            # Flash 提示处理
            await self.dismiss_flash_prompt(tab, "cards")

            # 等待题目加载 (work module + quiz iframe 都需要时间)
            await asyncio.sleep(2)

            # ---- 检测作业是否已提交/已完成（避免重复答题）----
            # 第一层：在 cards iframe 中检查 ans-job-finished 标记
            try:
                cards_finished = await self._eval_in_iframe(tab, "cards", f"""
                    const icons = _doc.querySelectorAll('.ans-job-icon');
                    let finished = false;
                    icons.forEach((icon, i) => {{
                        if (i === {work_index}) {{
                            const parent = icon.parentElement;
                            if (parent && parent.classList.contains('ans-job-finished')) {{
                                finished = true;
                            }}
                        }}
                    }});
                    return finished;
                """)
                if cards_finished:
                    log("任务点已标记完成(ans-job-finished)，跳过答题", "success")
                    return {"success": True, "skipped": True, "already_submitted": True}
            except Exception as e:
                logger.debug(f"cards 层完成检测异常: {e}")

            # 第二层：在 work module iframe 中检查已提交标记（仅检查明确的提交标记）
            try:
                already_submitted = await self._eval_in_nested_module_iframe(
                    tab, "modules/work", """
                    // 0. 检查 testTit_status_complete 类（超星作业完成状态标记）
                    if (_doc.querySelector('.testTit_status_complete')) return true;
                    const body = _doc.body ? _doc.body.innerText : '';
                    // 仅检查明确的"已提交"文字
                    if (body.includes('已提交') || body.includes('已经提交')) return true;
                    // 检查"已完成"文字
                    if (body.includes('已完成') && !body.includes('未完成')) return true;
                    // 检查"查看答案"按钮（提交后才出现）
                    if (_doc.querySelector('a[onclick*="viewAnswer"], .btnLookAnswer, #btnLookAnswer')) return true;
                    // 检查 TiMu 完成标记（成绩已显示 = 已提交）
                    const timu = _doc.querySelector('.TiMu');
                    if (timu && timu.querySelector('.Py_for_end, .sum_score')) return true;
                    // 检查 quiz 子 iframe 中的完成标记
                    const quizIframe = _doc.querySelector('iframe[src*="quiz"], iframe[id*="quiz"]');
                    if (quizIframe && quizIframe.contentDocument) {
                        const qDoc = quizIframe.contentDocument;
                        // 检查 quiz iframe 内的 testTit_status_complete
                        if (qDoc.querySelector('.testTit_status_complete')) return true;
                        const qBody = qDoc.body ? qDoc.body.innerText : '';
                        if (qBody.includes('已提交') || qBody.includes('已经提交') || qBody.includes('已完成')) return true;
                    }
                    return false;
                    """, deep=True, iframe_index=work_index
                )
                if already_submitted:
                    log("作业已提交/已完成，跳过答题", "success")
                    return {"success": True, "skipped": True, "already_submitted": True}
            except Exception as e:
                logger.debug(f"work 层完成检测异常: {e}")

            # 滚动页面确保题目加载 (在 work iframe 中)
            scroll_js = """
                const body = _doc.body || _doc.documentElement;
                let h = 0;
                const step = _win.innerHeight || 600;
                const timer = setInterval(() => {
                    h += step;
                    _win.scrollTo(0, h);
                    if (h >= body.scrollHeight) {
                        clearInterval(timer);
                        _win.scrollTo(0, 0);
                    }
                }, 100);
            """
            await self._eval_in_nested_module_iframe(
                tab, "modules/work", scroll_js, deep=True, iframe_index=work_index
            )
            await asyncio.sleep(0.8)

            # === 字体解密：在提取题目前解密字体加密字符 ===
            if self._font_decryptor.available:
                async def _eval_for_decrypt(js: str):
                    return await self._eval_in_nested_module_iframe(
                        tab, "modules/work", js, deep=True, iframe_index=work_index
                    )
                decrypted = await self._font_decryptor.decrypt_in_iframe(tab, _eval_for_decrypt)
                if decrypted:
                    log("字体解密完成，题目文本已还原", "success")
                    await asyncio.sleep(0.5)  # 等待 DOM 更新

            # === 一次性多策略合并提取题目（单次 JS eval，依次尝试多种选择器） ===
            combined_extract_js = """
                var results = [];
                var matched_type = '';

                // 策略1: 章节测验选择器 (.TiMu)
                var timus = _doc.querySelectorAll('.TiMu');
                if (timus.length > 0) {
                    matched_type = '1';
                    timus.forEach(function(timu) {
                        var titleEl = timu.querySelector('.clearfix .fontLabel');
                        if (!titleEl) return;
                        var typeInput = timu.querySelector('input[name^="answertype"]');
                        var qType = typeInput ? (typeInput.value || '0') : '0';
                        var options = [];
                        timu.querySelectorAll('ul li .after').forEach(function(el) { options.push(el.innerHTML); });
                        results.push({ question: titleEl.innerHTML, type: qType, options: options, html: timu.innerHTML.substring(0, 2000) });
                    });
                }

                // 策略2: 作业选择器 (.questionLi)
                if (results.length === 0) {
                    var items = _doc.querySelectorAll('.questionLi');
                    if (items.length > 0) {
                        matched_type = '2';
                        items.forEach(function(elem) {
                            var nameEl = elem.querySelector('.mark_name');
                            if (!nameEl) return;
                            var nameHtml = nameEl.innerHTML;
                            var idx = nameHtml.indexOf('</span>');
                            if (idx >= 0) nameHtml = nameHtml.substring(idx + 7);
                            var typeInput = elem.querySelector('input[name^="answertype"]');
                            var qType = typeInput ? (typeInput.value || '0') : '0';
                            var options = [];
                            elem.querySelectorAll('.answer_p').forEach(function(el) { options.push(el.innerHTML); });
                            results.push({ question: nameHtml, type: qType, options: options, html: elem.innerHTML.substring(0, 2000) });
                        });
                    }
                }

                // 策略3: 通用提取 (answertype input)
                if (results.length === 0) {
                    var typeInputs = _doc.querySelectorAll("input[name^='answertype']");
                    if (typeInputs.length > 0) {
                        matched_type = '3';
                        typeInputs.forEach(function(input) {
                            var container = input.closest('.TiMu, .questionLi, .Py-mian1, [class*="question"], [class*="timu"]');
                            if (!container) container = input.parentElement;
                            if (!container) return;
                            var qText = '';
                            var titleEl = container.querySelector('.clearfix .fontLabel, .mark_name, [class*="title"], [class*="name"], .question-title');
                            if (titleEl) qText = titleEl.textContent.trim();
                            if (!qText) qText = container.textContent.trim().substring(0, 200);
                            var options = [];
                            container.querySelectorAll('ul li .after, ul.answerList li, .answer_p, .option-item').forEach(function(el) { options.push(el.textContent.trim()); });
                            results.push({ question: qText, type: input.value || '0', options: options, html: container.innerHTML.substring(0, 1000) });
                        });
                    }
                }

                return { results: results, type: matched_type, count: results.length,
                         hasSubIframes: _doc.querySelectorAll('iframe').length > 0 };
            """
            combined = await self._eval_in_nested_module_iframe(
                tab, "modules/work", combined_extract_js, deep=True, iframe_index=work_index
            )

            questions = []
            quiz_type_matched = ''
            if combined and isinstance(combined, dict) and combined.get('count', 0) > 0:
                quiz_type_matched = combined.get('type', '1')
                container_sel = {"1": ".TiMu", "2": ".questionLi"}.get(quiz_type_matched, ".TiMu")
                for rq in combined.get('results', []):
                    q_text = _clean_question_text(_remove_html(rq.get('question', '')))
                    if not q_text or len(q_text) < 2:
                        continue
                    questions.append(QuestionData(
                        question=q_text,
                        question_type=rq.get('type', '0'),
                        options=[_remove_html(o) for o in rq.get('options', [])],
                        raw_html=rq.get('html', ''),
                        container_selector=container_sel,
                    ))

            # 策略4: 深层 iframe 探测（当 deep 模式未成功时）
            if not questions and combined and isinstance(combined, dict) and combined.get('hasSubIframes'):
                log("尝试在 work 子iframe 中查找题目...", "info")
                deep_questions = await self._extract_from_deep_work_iframe(
                    tab, log, iframe_index=work_index
                )
                if deep_questions:
                    questions = deep_questions

            if not questions:
                log("未检测到题目", "warning")
                return {"success": False, "page_refreshed": False}

            log(f"检测到 {len(questions)} 道题目，开始答题...")
            log("使用 deep 模式（题目在 quiz 子 iframe 中）", "info")

            # 逐题填写答案
            success_count = 0
            for i, q in enumerate(questions):
                if stop_check and stop_check():
                    log("收到停止指令，中止答题", "warning")
                    break
                if pause_event and not pause_event.is_set():
                    log("收到暂停指令，先暂存当前进度...", "info")
                    await self.save_draft(
                        tab, iframe_frag="modules/work", deep=True, iframe_index=work_index
                    )
                    await pause_event.wait()
                    log("已继续执行", "success")

                # 检测反作弊重定向
                recovered = await self._check_detect_redirect(tab, browser, chapter_url, log)
                if recovered is None:
                    log("页面被反作弊系统拦截，中止答题", "warning")
                    break
                elif recovered is not tab:
                    tab = recovered
                    log("页面已恢复，但需重新开始答题", "warning")
                    break

                log(f"  题目 {i+1}/{len(questions)}: [{QUESTION_TYPE_NAMES.get(q.question_type, '未知')}] {q.question[:50]}...")

                enhanced = await get_answer_enhanced(q.question, q.question_type, q.options)
                # 构建 AnswerResult
                answer = None
                if enhanced.get("finish"):
                    type_name = QUESTION_TYPE_NAMES.get(q.question_type, '未知')
                    log(f"  -> [{enhanced['source']}] 匹配成功({type_name}): {enhanced.get('options') or enhanced.get('answers')}", "success")
                    if q.question_type in ("0", "1", "3"):
                        answer = AnswerResult(source=enhanced["source"], answer_text="#".join(enhanced.get("raw_answers", [])), answer_list=enhanced["options"], success=True)
                    else:
                        answer = AnswerResult(source=enhanced["source"], answer_text="#".join(enhanced.get("answers", [])), answer_list=enhanced["answers"], success=True)
                else:
                    # OCS匹配失败，回退到原始解析
                    raw_answers = enhanced.get("raw_answers", [])
                    source = enhanced.get("source", "none")
                    if raw_answers:
                        answer = AnswerResult(source=source, answer_text="#".join(raw_answers), answer_list=raw_answers, success=True)
                        log(f"  -> [{source}] OCS未匹配，回退原始: {raw_answers}", "warning")
                    else:
                        answer = AnswerResult(source=source, success=False, error_reason="未匹配到答案")
                q.answer = answer

                if answer and answer.answer_list:
                    filled = await self.fill_answer(
                        tab, q, answer, i, iframe_frag="modules/work",
                        deep=True, iframe_index=work_index
                    )
                    if filled:
                        q.status = "success"
                        success_count += 1
                    else:
                        q.status = "failed"
                        if q.question_type in ("0", "1"):
                            # 选择/多选题: 不随机，保留 DeepSeek 答案(hidden input 可能已设置)
                            log("  -> 选择题填写失败，已设置hidden input，跳过随机", "warning")
                        else:
                            log("  -> 答案填写失败，尝试随机作答", "warning")
                            if await self._fill_random_answer(tab, q, i, iframe_frag="modules/work", deep=True, iframe_index=work_index):
                                q.status = "random"
                                success_count += 1
                                log("  -> 随机作答成功", "info")
                else:
                    q.status = "failed"
                    reason = answer.error_reason if answer and answer.error_reason else "未匹配到答案"
                    if q.question_type in ("0", "1"):
                        log(f"  -> 未获取到答案（{reason}），选择题跳过随机", "warning")
                    else:
                        log(f"  -> 未获取到答案（{reason}），尝试随机作答", "warning")
                        if await self._fill_random_answer(tab, q, i, iframe_frag="modules/work", deep=True, iframe_index=work_index):
                            q.status = "random"
                            success_count += 1
                            log("  -> 随机作答成功", "info")

                if i < len(questions) - 1:
                    await asyncio.sleep(random.uniform(0.15, 0.4))

            # 答题完成: 先暂存
            accuracy = success_count / len(questions) if questions else 0
            log(f"答题完成，正确率: {success_count}/{len(questions)} ({accuracy:.0%})")

            await asyncio.sleep(random.uniform(0.3, 0.5))
            saved = await self.save_draft(
                tab, iframe_frag="modules/work", deep=True, iframe_index=work_index
            )
            if saved:
                log("答案已暂存保存", "success")
            else:
                log("暂存失败，请注意手动保存", "warning")

            # 根据配置决定是否自动提交
            page_refreshed = False
            if self._config.auto_submit and accuracy >= self._config.min_accuracy:
                await asyncio.sleep(random.uniform(0.5, 1.0))
                submitted = await self.submit_quiz(
                    tab, iframe_frag="modules/work", deep=True, iframe_index=work_index
                )
                if submitted:
                    log("正确率达标，测验已自动提交", "success")
                    await asyncio.sleep(1)
                    page_refreshed = True
                else:
                    log("提交失败，答案已暂存，请手动提交", "warning")
            elif self._config.auto_submit and accuracy < self._config.min_accuracy:
                log(
                    f"正确率不足{self._config.min_accuracy:.0%}，已暂存答案，请手动检查后提交",
                    "warning",
                )
            else:
                log("未开启自动提交，答案已暂存，请手动提交", "info")

            return {"success": success_count > 0, "page_refreshed": page_refreshed}

        except Exception as e:
            logger.error(f"处理测验任务异常: {e}")
            return {"success": False, "page_refreshed": False}

    # ======================== 作业/考试处理 ========================

    async def _detect_quiz_iframe(self, tab: Tab) -> str:
        """
        自动检测页面中题目所在的 iframe，返回 iframe URL 片段。
        如果题目直接在主页面，返回空字符串。
        """
        try:
            # 检查主页面是否有题目元素
            has_main = await tab.evaluate(
                "!!(document.querySelector('.TiMu') || "
                "document.querySelector('.questionLi') || "
                "document.querySelector('input[name^=\"answertype\"]'))"
            )
            if has_main:
                return ""

            # 查找包含题目元素的 iframe
            iframe_src = await tab.evaluate("""
                (() => {
                    const iframes = document.querySelectorAll('iframe');
                    for (const f of iframes) {
                        try {
                            const d = f.contentDocument || f.contentWindow.document;
                            if (d.querySelector('.TiMu') || d.querySelector('.questionLi') ||
                                d.querySelector('input[name^="answertype"]')) {
                                return f.src || f.getAttribute('src') || '';
                            }
                        } catch(e) {}
                    }
                    // 回退: 返回第一个看起来像作业/考试的 iframe
                    for (const f of iframes) {
                        const src = f.src || f.getAttribute('src') || '';
                        if (src.includes('work') || src.includes('exam') || src.includes('mooc-ans')) {
                            return src;
                        }
                    }
                    return '';
                })()
            """)
            if iframe_src:
                # 提取有用的片段
                for frag in ['modules/work', 'mooc-ans', 'work', 'exam']:
                    if frag in iframe_src:
                        logger.debug(f"自动检测到题目 iframe: {iframe_src[:80]}")
                        return frag
                return iframe_src[:50]  # 回退用前50字符
            return ""
        except Exception as e:
            logger.debug(f"iframe 自动检测失败: {e}")
            return ""

    async def process_homework(
        self,
        tab: Tab,
        homework_url: str,
        browser: zd.Browser = None,
        on_log: Callable = None,
        pause_event: asyncio.Event = None,
        stop_check: Callable[[], bool] = None,
    ) -> Dict[str, Any]:
        """处理作业"""
        log = on_log or (lambda msg, level="info": logger.info(msg))
        result = {"success": True, "quizzes": 0, "videos": 0, "errors": 0}
        try:
            if browser:
                try:
                    tab = await browser.get(homework_url)
                except Exception as e:
                    if 'ERR_ABORTED' in str(e):
                        await asyncio.sleep(1)
                    else:
                        raise
            await asyncio.sleep(2)

            # 字体解密（主页面 + iframe）
            if self._font_decryptor.available:
                await self._font_decryptor.decrypt_page(tab)

            # ---- 9.1 复制粘贴限制解除 ----
            try:
                await tab.evaluate("""
                    (() => {
                        try {
                            const instants = window.UE?.instants || [];
                            for (const key in instants) {
                                const ue = instants[key];
                                if (ue?.textarea) {
                                    if (window.editorPaste) ue.removeListener('beforepaste', window.editorPaste);
                                    if (window.myEditor_paste) ue.removeListener('beforepaste', window.myEditor_paste);
                                }
                            }
                        } catch(e) {}
                    })()
                """)
            except Exception:
                pass

            # 自动检测 iframe 上下文（作业页可能在 iframe 中）
            iframe_frag = await self._detect_quiz_iframe(tab)

            # 提取并答题
            questions = await self.extract_questions(tab, quiz_type="2", iframe_frag=iframe_frag)
            if not questions:
                questions = await self.extract_questions(tab, quiz_type="1", iframe_frag=iframe_frag)
            if not questions:
                questions = await self._extract_questions_generic(tab, iframe_frag=iframe_frag)
            if not questions:
                log("作业中未检测到题目", "warning")
                return result

            log(f"作业检测到 {len(questions)} 道题目")
            success_count = 0
            for i, q in enumerate(questions):
                if stop_check and stop_check():
                    break
                if pause_event and not pause_event.is_set():
                    log("收到暂停指令，等待继续...", "info")
                    await pause_event.wait()
                    log("已继续执行", "success")
                type_name = QUESTION_TYPE_NAMES.get(q.question_type, '未知')
                log(f"  [{type_name}] {q.question[:50]}...")
                enhanced = await get_answer_enhanced(q.question, q.question_type, q.options)
                answer = None
                if enhanced.get("finish"):
                    if q.question_type in ("0", "1", "3"):
                        answer = AnswerResult(source=enhanced["source"], answer_text="#".join(enhanced.get("raw_answers", [])), answer_list=enhanced["options"], success=True)
                    else:
                        answer = AnswerResult(source=enhanced["source"], answer_text="#".join(enhanced.get("answers", [])), answer_list=enhanced["answers"], success=True)
                else:
                    raw_answers = enhanced.get("raw_answers", [])
                    if raw_answers:
                        answer = AnswerResult(source=enhanced.get("source", "none"), answer_text="#".join(raw_answers), answer_list=raw_answers, success=True)
                    else:
                        answer = AnswerResult(source=enhanced.get("source", "none"), success=False, error_reason="未匹配到答案")
                if answer and answer.answer_list:
                    filled = await self.fill_answer(tab, q, answer, i, iframe_frag=iframe_frag)
                    if filled:
                        success_count += 1
                    elif q.question_type not in ("0", "1"):
                        # 非选择题: 答题失败时随机作答
                        if await self._fill_random_answer(tab, q, i, iframe_frag=iframe_frag):
                            success_count += 1
                else:
                    if q.question_type not in ("0", "1"):
                        # 非选择题: 未找到答案时随机作答
                        if await self._fill_random_answer(tab, q, i, iframe_frag=iframe_frag):
                            success_count += 1
                if i < len(questions) - 1:
                    await asyncio.sleep(random.uniform(0.15, 0.4))

            await self.save_draft(tab, iframe_frag=iframe_frag)
            if self._config.auto_submit:
                accuracy = success_count / len(questions) if questions else 0
                if accuracy >= self._config.min_accuracy:
                    await self.submit_quiz(tab, iframe_frag=iframe_frag, deep=True)

            result["quizzes"] = success_count
            result["success"] = success_count > 0
            log(f"作业处理完成: {success_count}/{len(questions)}")
            return result
        except Exception as e:
            logger.error(f"处理作业失败: {e}")
            result["errors"] += 1
            return result

    async def process_exam(
        self,
        tab: Tab,
        exam_url: str,
        browser: zd.Browser = None,
        on_log: Callable = None,
        pause_event: asyncio.Event = None,
        stop_check: Callable[[], bool] = None,
    ) -> Dict[str, Any]:
        """处理考试"""
        log = on_log or (lambda msg, level="info": logger.info(msg))
        result = {"success": True, "quizzes": 0, "videos": 0, "errors": 0}
        try:
            if browser:
                try:
                    tab = await browser.get(exam_url)
                except Exception as e:
                    if 'ERR_ABORTED' in str(e):
                        await asyncio.sleep(1)
                    else:
                        raise
            await asyncio.sleep(2)

            # 字体解密（主页面 + iframe）
            if self._font_decryptor.available:
                await self._font_decryptor.decrypt_page(tab)

            # ---- 11.1 考试整卷预览重定向 ----
            # 如果考试页面支持整卷预览，自动跳转
            try:
                page_url = (tab.url or '').lower()
                if 'reVersionTestStartNew' in page_url:
                    # 检查是否禁止整卷预览
                    no_preview = await tab.evaluate("""
                        (() => {
                            const info = document.querySelector('.mark_info');
                            return info && info.textContent.includes('不允许整卷预览');
                        })()
                    """)
                    if no_preview:
                        log("当前考试禁止整卷预览，将采用逐题模式", "warning")
                    else:
                        # 尝试跳转整卷预览
                        preview_result = await tab.evaluate("""
                            (() => {
                                if (typeof topreview === 'function') { topreview(); return true; }
                                return false;
                            })()
                        """)
                        if preview_result:
                            log("考试已跳转到整卷预览页面", "success")
                            await asyncio.sleep(3)
            except Exception:
                pass

            # ---- 9.1 复制粘贴限制解除 ----
            try:
                await tab.evaluate("""
                    (() => {
                        try {
                            const instants = window.UE?.instants || [];
                            for (const key in instants) {
                                const ue = instants[key];
                                if (ue?.textarea) {
                                    if (window.editorPaste) ue.removeListener('beforepaste', window.editorPaste);
                                    if (window.myEditor_paste) ue.removeListener('beforepaste', window.myEditor_paste);
                                }
                            }
                        } catch(e) {}
                    })()
                """)
            except Exception:
                pass

            # 自动检测 iframe 上下文
            iframe_frag = await self._detect_quiz_iframe(tab)

            questions = await self.extract_questions(tab, quiz_type="3", iframe_frag=iframe_frag)
            if not questions:
                questions = await self.extract_questions(tab, quiz_type="1", iframe_frag=iframe_frag)
            if not questions:
                log("考试中未检测到题目", "warning")
                return result

            log(f"考试检测到 {len(questions)} 道题目")
            success_count = 0
            for i, q in enumerate(questions):
                if stop_check and stop_check():
                    break
                if pause_event and not pause_event.is_set():
                    log("收到暂停指令，等待继续...", "info")
                    await pause_event.wait()
                    log("已继续执行", "success")
                type_name = QUESTION_TYPE_NAMES.get(q.question_type, '未知')
                log(f"  题目 {i+1}/{len(questions)}: [{type_name}] {q.question[:50]}...")
                enhanced = await get_answer_enhanced(q.question, q.question_type, q.options)
                answer = None
                if enhanced.get("finish"):
                    log(f"  -> [{enhanced['source']}] 匹配成功({type_name}): {enhanced.get('options') or enhanced.get('answers')}", "success")
                    if q.question_type in ("0", "1", "3"):
                        answer = AnswerResult(source=enhanced["source"], answer_text="#".join(enhanced.get("raw_answers", [])), answer_list=enhanced["options"], success=True)
                    else:
                        answer = AnswerResult(source=enhanced["source"], answer_text="#".join(enhanced.get("answers", [])), answer_list=enhanced["answers"], success=True)
                else:
                    raw_answers = enhanced.get("raw_answers", [])
                    if raw_answers:
                        answer = AnswerResult(source=enhanced.get("source", "none"), answer_text="#".join(raw_answers), answer_list=raw_answers, success=True)
                        log(f"  -> [{enhanced.get('source')}] OCS未匹配，回退原始: {raw_answers}", "warning")
                    else:
                        answer = AnswerResult(source=enhanced.get("source", "none"), success=False, error_reason="未匹配到答案")
                if answer and answer.answer_list:
                    filled = await self.fill_answer(tab, q, answer, i, iframe_frag=iframe_frag)
                    if filled:
                        success_count += 1
                    else:
                        # ---- 4.2 答题失败时随机作答 ----
                        if await self._fill_random_answer(tab, q, i, iframe_frag=iframe_frag):
                            success_count += 1
                else:
                    # ---- 4.2 未找到答案时随机作答 ----
                    if await self._fill_random_answer(tab, q, i, iframe_frag=iframe_frag):
                        success_count += 1
                if i < len(questions) - 1:
                    await asyncio.sleep(random.uniform(0.15, 0.4))

            await self.save_draft(tab, iframe_frag=iframe_frag)
            if self._config.auto_submit:
                accuracy = success_count / len(questions) if questions else 0
                if accuracy >= self._config.min_accuracy:
                    await self.submit_quiz(tab, iframe_frag=iframe_frag, deep=True)

            result["quizzes"] = success_count
            result["success"] = success_count > 0
            log(f"考试处理完成: {success_count}/{len(questions)}")
            return result
        except Exception as e:
            logger.error(f"处理考试失败: {e}")
            result["errors"] += 1
            return result
