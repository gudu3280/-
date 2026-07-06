"""
字体解密模块 - 处理学习通的字体加密反爬机制

使用浏览器 JS 引擎执行 Typr.js 的原始解密算法，
确保字形哈希计算与原脚本 (学习通脚本.js) 完全一致。

哈希算法: md5(JSON.stringify(Typr.U.glyphToPath(font, gid))).slice(24)
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _load_ttf_table(ttf_table_path: Path) -> Dict[str, int]:
    """
    加载字体映射表 (ttf_table.json)

    该映射表来自原脚本的 @resource ttf 配置:
    https://www.forestpolice.org/ttf/2.0/table.json

    存储了字形轮廓哈希(8位十进制)到原始Unicode码位的映射。
    """
    if not ttf_table_path.exists():
        logger.warning(f"字体映射表不存在: {ttf_table_path}")
        return {}

    try:
        with open(ttf_table_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"加载字体映射表失败: {e}")
        return {}


def _load_js_engine(js_path: Path) -> str:
    """加载 JS 解密引擎 (Typr.js + MD5 + 解密函数)"""
    if not js_path.exists():
        logger.warning(f"JS 解密引擎不存在: {js_path}")
        return ""

    try:
        with open(js_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"加载 JS 解密引擎失败: {e}")
        return ""


class FontDecryptor:
    """
    字体解密器 - 使用浏览器 JS 引擎执行与油猴脚本相同的解密算法

    使用 Typr.js (从原脚本提取) + MD5 在浏览器上下文中执行:
    1. 解析 base64 TTF 字体
    2. 遍历 CJK 码位范围 (19968-40870)
    3. 对每个字形计算 md5(JSON.stringify(glyphToPath)).slice(24)
    4. 在 ttf_table.json 中查找映射
    5. 替换 DOM 中的加密字符
    """

    def __init__(self, ttf_table_path: Path = None, js_engine_path: Path = None):
        self._ttf_table: Dict[str, int] = {}
        self._js_engine: str = ""

        if ttf_table_path:
            self._ttf_table = _load_ttf_table(ttf_table_path)

        if js_engine_path:
            self._js_engine = _load_js_engine(js_engine_path)
        elif ttf_table_path:
            # 默认路径: 与 ttf_table.json 同目录下的 font_decrypt.js
            default_js = ttf_table_path.parent / "font_decrypt.js"
            self._js_engine = _load_js_engine(default_js)

    @property
    def available(self) -> bool:
        """字体解密功能是否可用 (需要 ttf_table + JS 引擎)"""
        return bool(self._ttf_table) and bool(self._js_engine)

    def _build_decrypt_js(self, font_base64: str, doc_var: str = "document") -> str:
        """
        构建完整的解密 JS 代码

        Args:
            font_base64: 字体 base64 数据
            doc_var: 文档变量名 ("document" 或 "_doc")

        Returns:
            可注入浏览器执行的完整 JS 代码
        """
        # 将 ttf_table 序列化为 JS 对象
        ttf_json = json.dumps(self._ttf_table, separators=(',', ':'))

        # 构建完整 JS: 引擎 + 解密调用
        return f"""
{self._js_engine}

(() => {{
    try {{
        var fontBase64 = {json.dumps(font_base64)};
        var ttfTable = {ttf_json};

        // 解码 base64 为 Uint8Array
        var decoded = atob(fontBase64);
        var fontData = new Uint8Array(decoded.length);
        for (var i = 0; i < decoded.length; i++) {{
            fontData[i] = decoded.charCodeAt(i);
        }}

        // 用 Typr.js 解析字体
        var font = Typr.parse(fontData.buffer);

        // 构建字符映射表
        var charMap = {{}};
        for (var code = 19968; code < 40870; code++) {{
            var gid = Typr.U.codeToGlyph(font, code);
            if (gid) {{
                var path = Typr.U.glyphToPath(font, gid);
                var hash = md5(JSON.stringify(path)).slice(24);
                if (ttfTable[hash] !== undefined) {{
                    charMap[code] = ttfTable[hash];
                }}
            }}
        }}

        var count = Object.keys(charMap).length;

        // 替换 DOM 中的加密字符
        {doc_var}.querySelectorAll(".font-cxsecret").forEach(function(el) {{
            var html = el.innerHTML;
            for (var encCode in charMap) {{
                var encChar = String.fromCharCode(parseInt(encCode));
                var origChar = String.fromCharCode(charMap[encCode]);
                html = html.split(encChar).join(origChar);
            }}
            el.innerHTML = html;
            el.classList.remove("font-cxsecret");
        }});

        return {{count: count, keys: Object.keys(charMap).slice(0, 10)}};
    }} catch(e) {{
        return {{error: e.message || String(e), count: 0}};
    }}
}})()
"""

    async def decrypt_page(self, page) -> bool:
        """
        检测并解密页面中的字体加密 (主页面)

        Args:
            page: zendriver Tab 实例
        """
        if not self.available:
            return False

        try:
            # 查找字体加密样式
            font_base64 = await page.evaluate("""
                (() => {
                    const styles = document.querySelectorAll('style');
                    for (const s of styles) {
                        const text = s.textContent || '';
                        if (text.includes('font-cxsecret')) {
                            const m = text.match(/base64,([\\w\\W]+?)'/);
                            if (m) return m[1];
                        }
                    }
                    return '';
                })()
            """)

            if not font_base64:
                return False

            logger.info("检测到字体加密，开始解密...")

            # 注入 JS 引擎并执行解密
            js_code = self._build_decrypt_js(font_base64, "document")
            result = await page.evaluate(js_code)

            if result and isinstance(result, dict):
                if result.get("error"):
                    logger.error(f"JS 解密错误: {result['error']}")
                    return False
                count = result.get("count", 0)
                logger.info(f"字体解密完成，共 {count} 个字符映射")
                return count > 0

            return False

        except Exception as e:
            logger.error(f"字体解密异常: {e}")
            return False

    async def decrypt_in_iframe(self, tab, iframe_eval_func) -> bool:
        """
        在 iframe 上下文中检测并解密字体加密

        Args:
            tab: zendriver Tab 实例
            iframe_eval_func: 异步函数，接受 JS 字符串并在 iframe 中执行，返回结果
                签名: async (js: str) -> Any
        """
        if not self.available:
            return False

        try:
            # 在 iframe 中查找字体加密样式
            font_base64 = await iframe_eval_func("""
                const styles = _doc.querySelectorAll('style');
                let result = '';
                for (const s of styles) {
                    const text = s.textContent || '';
                    if (text.includes('font-cxsecret')) {
                        const m = text.match(/base64,([\\w\\W]+?)'/);
                        if (m) { result = m[1]; break; }
                    }
                }
                return result;
            """)

            if not font_base64:
                return False

            logger.info("iframe 中检测到字体加密，开始解密...")

            # 注入 JS 引擎并执行解密 (使用 _doc 作为文档变量)
            js_code = self._build_decrypt_js(font_base64, "_doc")
            result = await iframe_eval_func(js_code)

            if result and isinstance(result, dict):
                # 检查是否为 CDP 异常对象 (浏览器 JS 解析/运行错误)
                if "exceptionId" in result or "exception" in result:
                    exc = result.get("exception", {})
                    desc = exc.get("description", result.get("text", "Unknown error"))
                    line = result.get("lineNumber", "?")
                    logger.error(f"iframe JS 执行异常: {desc} (line {line})")
                    return False
                if result.get("error"):
                    logger.error(f"iframe JS 解密错误: {result['error']}")
                    return False
                count = result.get("count", 0)
                logger.info(f"iframe 字体解密完成，共 {count} 个字符映射")
                return count > 0

            logger.warning(f"iframe 字体解密返回空结果: {result!r}")
            return False

        except Exception as e:
            logger.debug(f"iframe 字体解密异常: {e}")
            return False
