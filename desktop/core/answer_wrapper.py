"""
题库配置器 - 移植自 OCS (ocsjs) 的 core/answer-wrapper/

包含:
- AnswererWrapper: 题库配置数据结构
- AnswerCache: 答案缓存（LRU 策略）
- answer_wrapper_handler: 多题库并发查询处理器
- parse_answer_wrapper: 从 JSON/字符串解析题库配置
"""

import asyncio
import json
import time
import logging
import re
from typing import List, Optional, Dict, Any, Tuple, Callable
from dataclasses import dataclass, field
from collections import OrderedDict

import httpx

from .config import Config

logger = logging.getLogger(__name__)


# ============================================================
# 题库配置数据结构
# ============================================================

@dataclass
class AnswererWrapper:
    """
    题库配置器

    移植自 OCS AnswererWrapper
    """
    url: str = ""                           # 题库接口地址
    name: str = "题库"                       # 题库名称
    method: str = "POST"                     # 请求方法 GET/POST
    content_type: str = "json"               # 内容类型 json/form
    type: str = ""                           # 题目类型参数名（空则用默认）
    handler: Optional[str] = None            # 自定义响应处理函数（JS 风格字符串）
    data: Optional[Dict[str, Any]] = None    # 请求体/参数额外字段
    headers: Optional[Dict[str, str]] = None # 自定义请求头
    key: str = ""                            # API 密钥
    timeout: int = 60                        # 超时秒数


def parse_answer_wrapper(config_data: Any) -> List[AnswererWrapper]:
    """
    从 JSON/字典/字符串解析题库配置

    移植自 OCS AnswerWrapperParser
    """
    wrappers: List[AnswererWrapper] = []

    if isinstance(config_data, str):
        try:
            config_data = json.loads(config_data)
        except json.JSONDecodeError:
            # 尝试 base64 解码
            import base64
            try:
                decoded = base64.b64decode(config_data).decode("utf-8")
                config_data = json.loads(decoded)
            except Exception:
                logger.warning(f"无法解析题库配置: {config_data[:100]}")
                return []

    if isinstance(config_data, dict):
        config_data = [config_data]

    if isinstance(config_data, list):
        for item in config_data:
            if not isinstance(item, dict):
                continue
            wrapper = AnswererWrapper(
                url=item.get("url", ""),
                name=item.get("name", "题库"),
                method=item.get("method", "POST").upper(),
                content_type=item.get("contentType", item.get("content_type", "json")),
                type=item.get("type", ""),
                handler=item.get("handler"),
                data=item.get("data"),
                headers=item.get("headers"),
                key=item.get("key", ""),
                timeout=int(item.get("timeout", 60)),
            )
            if wrapper.url:
                wrappers.append(wrapper)

    return wrappers


# ============================================================
# 答案缓存
# ============================================================

class AnswerCache:
    """
    答案缓存 - LRU 策略

    移植自 OCS searchAnswerInCaches
    """

    def __init__(self, max_size: int = 200):
        self._cache: OrderedDict = OrderedDict()
        self._max_size = max_size
        self._enabled = True

    def get(self, question: str, question_type: str = "") -> Optional[List[str]]:
        """查询缓存"""
        if not self._enabled:
            return None
        key = self._make_key(question, question_type)
        if key in self._cache:
            # LRU: 移到末尾
            self._cache.move_to_end(key)
            logger.debug(f"缓存命中: {question[:30]}...")
            return self._cache[key]
        return None

    def set(self, question: str, answers: List[str], question_type: str = ""):
        """写入缓存"""
        if not self._enabled:
            return
        key = self._make_key(question, question_type)
        self._cache[key] = answers
        self._cache.move_to_end(key)
        # 超容量时删除最旧的
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def clear(self):
        """清空缓存"""
        self._cache.clear()

    def set_enabled(self, enabled: bool):
        """启用/禁用缓存"""
        self._enabled = enabled
        if not enabled:
            self.clear()

    @property
    def size(self) -> int:
        return len(self._cache)

    @staticmethod
    def _make_key(question: str, question_type: str) -> str:
        """生成缓存键"""
        return f"{question_type}:{question.strip().lower()}"


# 模块级缓存单例
_cache_instance: Optional[AnswerCache] = None


def get_answer_cache() -> AnswerCache:
    """获取答案缓存单例"""
    global _cache_instance
    if _cache_instance is None:
        config = Config()
        _cache_instance = AnswerCache(config.cache_max_size)
        _cache_instance.set_enabled(config.cache_enabled)
    return _cache_instance


# ============================================================
# 多题库并发查询处理器
# ============================================================

# 模块级持久化 HTTP 客户端
_http_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    """获取或创建持久化 HTTP 客户端"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
            http2=False,
        )
    return _http_client


def _replace_placeholders(obj: Any, context: Dict[str, Any]) -> Any:
    """
    递归替换占位符 ${title} ${options} ${type} 等

    移植自 OCS 占位符替换逻辑
    """
    if isinstance(obj, str):
        result = obj
        for key, value in context.items():
            placeholder = "${" + key + "}"
            result = result.replace(placeholder, str(value))
        return result
    elif isinstance(obj, dict):
        return {k: _replace_placeholders(v, context) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_replace_placeholders(item, context) for item in obj]
    return obj


def _parse_handler_response(response_data: Any, handler: str) -> Optional[Tuple[str, str]]:
    """
    解析题库响应（简化版 handler）

    OCS 使用 Function(handler)() 执行自定义 JS 处理函数。
    Python 版使用内置解析逻辑，兼容常见题库返回格式。

    返回: (题目, 答案) 或 None
    """
    if not isinstance(response_data, dict):
        return None

    # 兼容多种返回格式
    answer = ""

    # 格式1: {code: 1/-1/0, data: {answer: "..."}}
    if "code" in response_data:
        code = response_data.get("code")
        if code in (1, -1, 0) or response_data.get("success"):
            data = response_data.get("data", {}) or {}
            answer = data.get("answer", "") or response_data.get("answer", "")

    # 格式2: {success: true, answer: "..."}
    elif response_data.get("success"):
        answer = response_data.get("answer", "") or (response_data.get("data", {}) or {}).get("answer", "")

    # 格式3: 直接 {answer: "..."}
    elif "answer" in response_data:
        answer = response_data["answer"]

    # 格式4: {data: "答案文本"}
    elif "data" in response_data and isinstance(response_data["data"], str):
        answer = response_data["data"]

    if answer:
        question = response_data.get("question", "")
        return (question, str(answer))

    return None


async def answer_wrapper_handler(
    wrappers: List[AnswererWrapper],
    question: str,
    options: List[str],
    question_type: str = "",
) -> List[str]:
    """
    多题库并发查询处理器

    移植自 OCS defaultAnswerWrapperHandler
    - 多题库 Promise.all（asyncio.gather）并发请求
    - GET 拼接 URL 参数，POST 构造请求体
    - 占位符替换 ${title} ${options} ${type}
    - 超时控制
    - 返回所有题库的答案列表

    返回: 答案字符串列表（可能来自多个题库）
    """
    if not wrappers:
        return []

    # 构建占位符上下文
    context = {
        "title": question,
        "question": question,
        "type": question_type,
        "options": "#".join(options) if options else "",
    }

    client = _get_client()

    async def query_one(wrapper: AnswererWrapper) -> List[str]:
        """查询单个题库"""
        if not wrapper.url:
            return []

        try:
            # 替换 URL 中的占位符
            url = _replace_placeholders(wrapper.url, context)

            headers = {"Content-Type": "application/json"}
            if wrapper.headers:
                headers.update(_replace_placeholders(wrapper.headers, context))
            if wrapper.key:
                headers["Authorization"] = f"Bearer {wrapper.key}"

            # 构建请求数据
            request_data = {
                "question": question,
                "type": question_type,
                "options": options,
                "key": wrapper.key,
            }
            if wrapper.data:
                extra = _replace_placeholders(wrapper.data, context)
                if isinstance(extra, dict):
                    request_data.update(extra)

            # 发送请求
            timeout = httpx.Timeout(wrapper.timeout, connect=10.0)

            if wrapper.method == "GET":
                params = {k: v for k, v in request_data.items() if v}
                response = await client.get(url, params=params, headers=headers, timeout=timeout)
            else:
                if wrapper.content_type == "form":
                    response = await client.post(url, data=request_data, headers=headers, timeout=timeout)
                else:
                    response = await client.post(url, json=request_data, headers=headers, timeout=timeout)

            response.raise_for_status()
            data = response.json()

            # 解析响应
            parsed = _parse_handler_response(data, wrapper.handler or "")
            if parsed:
                _, answer = parsed
                logger.info(f"题库[{wrapper.name}]返回: {answer[:100]}")
                return [answer]
            else:
                logger.debug(f"题库[{wrapper.name}]未找到答案")
                return []

        except httpx.TimeoutException:
            logger.warning(f"题库[{wrapper.name}]请求超时")
            return []
        except Exception as e:
            logger.error(f"题库[{wrapper.name}]请求异常: {e}")
            return []

    # 并发查询所有题库
    results = await asyncio.gather(*[query_one(w) for w in wrappers])
    all_answers: List[str] = []
    for r in results:
        all_answers.extend(r)

    return all_answers


async def cleanup_client():
    """关闭持久化 HTTP 客户端"""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
