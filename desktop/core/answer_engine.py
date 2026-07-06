"""
答题引擎模块 - 集成 OCS 答题算法 + DeepSeek API + 多题库服务器

功能:
- 多题库并发查询（OCS AnswererWrapper 风格）
- DeepSeek AI 辅助答题
- OCS 4阶段自适应答案匹配（归一化→相似度→纯ABCD→多片段）
- 答案缓存（LRU）
- 双通道并行查询（题库 + AI），取最快有效答案

移植自:
- 学习通脚本.js getAnswerFromAI / getAnswerFromCustomApi
- OCS core/worker/resolvers/ (4阶段匹配算法)
- OCS core/answer-wrapper/ (多题库并发)
"""

import asyncio
import re
import json
import logging
from typing import List, Optional, NamedTuple, Dict, Any, Tuple
from dataclasses import dataclass, field

import httpx

from .config import Config
from .resolvers import (
    resolve_single, resolve_multiple, resolve_judgement, resolve_completion,
    split_answer, resolve_plain_answer, is_plain_answer,
    default_work_type_resolver, is_judgement_options,
    SingleResolveResult, MultipleResolveResult, JudgementResolveResult, CompletionResolveResult,
)
from .answer_wrapper import (
    AnswererWrapper, parse_answer_wrapper, answer_wrapper_handler,
    get_answer_cache, AnswerCache,
)

logger = logging.getLogger(__name__)

# 模块级持久化 HTTP 客户端 (连接池复用)
# 首次调用时懒初始化，之后所有请求共享同一 TCP 连接
_http_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    """获取或创建持久化 HTTP 客户端（连接池复用）"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30,
            ),
            http2=False,  # HTTP/1.1 keep-alive 对短请求更稳定
        )
    return _http_client


_config_cache: Optional[Config] = None


def _get_config() -> Config:
    """获取配置（每次调用都重新加载 .env，确保 UI 保存的配置立即生效）"""
    global _config_cache
    if _config_cache is None:
        _config_cache = Config()
    else:
        _config_cache.reload()
    return _config_cache


def refresh_config():
    """清除配置缓存，强制下次重新创建（UI 保存配置后调用）"""
    global _config_cache
    _config_cache = None


# 题目类型映射
QUESTION_TYPE_NAMES = {
    "0": "单选题",
    "1": "多选题",
    "2": "填空题",
    "3": "判断题",
    "4": "简答题",
    "5": "名词解释",
    "6": "论述题",
    "7": "计算题",
    "9": "材料题",
}


@dataclass
class AnswerResult:
    """统一答案结果格式"""
    source: str           # 来源: "deepseek", "tiku", "none"
    answer_text: str = ""  # 原始答案文本
    answer_list: List[str] = field(default_factory=list)  # 解析后的答案列表
    success: bool = True
    error_reason: str = ""  # 失败原因（用于 UI 日志显示）

    def __bool__(self):
        return self.success and bool(self.answer_list or self.answer_text)


def _build_prompt(question: str, question_type: str, options: List[str]) -> str:
    """根据题型构建精简的 prompt，减少输入 token 数以加速响应"""

    if question_type == "3":
        # 判断题 - 极简 prompt
        return f"判断题，只回答\"正确\"或\"错误\"。\n\n{question}"

    elif question_type == "0":
        # 单选题 - 动态生成选项范围（支持 A-Z）
        opts = " ".join(f"{chr(65+i)}.{o}" for i, o in enumerate(options))
        last_letter = chr(65 + max(0, len(options) - 1))
        return f"单选题，只返回正确选项字母(A-{last_letter})。\n\n{question}\n{opts}"

    elif question_type == "1":
        # 多选题 - 动态生成选项范围（支持 A-Z）
        opts = " ".join(f"{chr(65+i)}.{o}" for i, o in enumerate(options))
        last_letter = chr(65 + max(0, len(options) - 1))
        return f"多选题，选择所有正确选项，返回所有正确选项字母(如AB、ACD，至少2个字母，范围A-{last_letter})。\n\n{question}\n{opts}"

    elif question_type == "2":
        # 填空题
        return f"填空题，直接返回答案，多个空用|分隔。\n\n{question}"

    elif question_type in ("4", "5"):
        # 简答题 / 名词解释
        return f"{QUESTION_TYPE_NAMES.get(question_type, '简答题')}，简洁回答。\n\n{question}"

    else:
        # 论述/计算/默认
        opts = ""
        if options:
            opts = "\n" + " ".join(f"{chr(65+i)}.{o}" for i, o in enumerate(options))
        type_name = QUESTION_TYPE_NAMES.get(question_type, "单选题")
        return f"{type_name}，直接给出答案。\n\n{question}{opts}"


def _extract_answer_from_reasoning(reasoning: str, question_type: str, options: List[str]) -> str:
    """
    当 deepseek-reasoner 的 content 为空时，从思考过程尾部提取最终答案。

    思考过程通常以"答案是..." / "所以选..." / "最终答案..." 结尾，
    提取尾部500字交给 _parse_ai_response 解析。
    """
    # 取思考过程尾部 500 字（答案通常在末尾）
    tail = reasoning[-500:] if len(reasoning) > 500 else reasoning

    # 优先找明确的答案标记
    answer_markers = [
        r'(?:答案|所以|因此|选|故选|最终|综上)[：:是为]?\s*([A-Z]{1,26}[、，,.\s]*)',
        r'(?:答案|所以|因此|选|故选|最终|综上)[：:是为]?\s*(正确|错误|对|错)',
    ]
    for pattern in answer_markers:
        matches = list(re.finditer(pattern, tail))
        if matches:
            # 取最后一个匹配（最接近结尾的）
            return matches[-1].group(0)

    # 没找到明确标记，返回尾部让 _parse_ai_response 解析
    return tail


def _parse_ai_response(content: str, question_type: str, options: List[str]) -> List[str]:
    """
    解析AI返回的内容为答案列表

    移植自脚本 L319-L379 的答案解析逻辑
    """
    content = content.strip()

    if not content:
        return []

    if question_type == "3":
        # 判断题: 识别正确/错误
        is_true = bool(re.search(r'正确|对|是|√|true|T', content, re.IGNORECASE))
        is_false = bool(re.search(r'错误|错|否|×|false|F|wr', content, re.IGNORECASE))
        if is_true and not is_false:
            return ["正确"]
        elif is_false:
            return ["错误"]
        return []

    elif question_type == "0":
        # 单选题: 提取选项字母（支持 A-Z）
        answer_match = re.search(r'[A-Z]', content)
        if answer_match:
            letter = answer_match.group(0)
            idx = ord(letter) - 65
            if 0 <= idx < len(options):
                return [options[idx]]
        return []

    elif question_type == "1":
        # 多选题: 提取多个字母（支持 A-Z，兼容 "AB", "A,B", "A、C", "ACDFJ" 等格式）
        # 先尝试连续字母匹配
        answer_match = re.search(r'[A-Z]{2,}', content)
        if answer_match:
            letters = answer_match.group(0)
        else:
            # 回退: 提取所有单独出现的大写字母（去重保持顺序）
            all_letters = re.findall(r'(?<![a-zA-Z])[A-Z](?![a-zA-Z])', content)
            seen = set()
            letters = ''
            for ch in all_letters:
                if ch not in seen:
                    seen.add(ch)
                    letters += ch
        if letters:
            result = []
            for letter in letters:
                idx = ord(letter) - 65
                if 0 <= idx < len(options):
                    result.append(options[idx])
            return result
        return []

    elif question_type == "2":
        # 填空题: 用 | 分隔多个空
        parts = [s.strip() for s in content.split("|") if s.strip()]
        return parts if parts else [content]

    else:
        # 简答/名词解释/论述/计算: 直接返回内容
        parts = [s.strip() for s in content.split("|") if s.strip()]
        return parts if parts else [content]


# 短答案题型的 max_tokens 限制
# deepseek-reasoner 的思考 tokens 和 content tokens 共享预算，
# 必须给足够余量，否则模型思考完后没 token 输出最终答案
_MAX_TOKENS_MAP = {
    "3": 200,  # 判断题: reasoner可能输出 "我认为...所以答案是正确"
    "0": 200,  # 单选题: reasoner可能输出 "分析...所以选A"
    "1": 200,  # 多选题: reasoner可能输出 "分析...所以选ABC"
    "2": 500,  # 填空题
}


async def ask_deepseek(
    question: str,
    question_type: str,
    options: List[str],
    config: Config = None,
) -> AnswerResult:
    """
    使用 DeepSeek API 获取答案（连接池复用，极速响应）
    """
    if config is None:
        config = _get_config()

    if not config.deepseek_api_key:
        logger.warning("DeepSeek API Key 未配置")
        return AnswerResult(source="deepseek", success=False, error_reason="API Key 未配置")

    # API Key 有效性检查
    if len(config.deepseek_api_key) < 10:
        return AnswerResult(source="deepseek", success=False, error_reason=f"API Key 无效({len(config.deepseek_api_key)}字符)，请在设置中输入有效的 sk- 开头的 Key")

    type_name = QUESTION_TYPE_NAMES.get(question_type, "单选题")

    prompt = _build_prompt(question, question_type, options)
    request_data = {
        "model": config.deepseek_model,
        "messages": [{"role": "user", "content": prompt}],
    }
    # 对短答案题型限制 max_tokens，减少服务端生成时间
    max_tok = _MAX_TOKENS_MAP.get(question_type)
    if max_tok:
        request_data["max_tokens"] = max_tok

    client = _get_client()
    try:
        # deepseek-reasoner 需要更长的超时时间（模型会先进行思考）
        timeout = httpx.Timeout(60.0, connect=10.0)
        response = await client.post(
            config.deepseek_api_url,
            json=request_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.deepseek_api_key}",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()

        # 提取AI回复内容
        choices = data.get("choices", [])
        if not choices:
            logger.warning("DeepSeek返回空choices")
            return AnswerResult(source="deepseek", success=False)

        message = choices[0].get("message", {})
        content = (message.get("content") or "").strip()

        # deepseek-reasoner: reasoning_content 包含思考过程，content 包含最终答案
        reasoning = (message.get("reasoning_content") or "").strip()
        if reasoning:
            logger.debug(f"DeepSeek思考过程: {reasoning[:100]}...")

        if not content and reasoning:
            # deepseek-reasoner: content 为空但思考过程有内容，尝试从思考中提取答案
            logger.info(f"DeepSeek content为空，尝试从思考过程中提取答案...")
            content = _extract_answer_from_reasoning(reasoning, question_type, options)
            logger.info(f"从思考过程提取: {content[:100]}")
        elif not content:
            logger.warning("DeepSeek返回空内容")
            return AnswerResult(source="deepseek", success=False)

        logger.info(f"DeepSeek返回({type_name}): {content[:100]}")

        # 解析答案
        answer_list = _parse_ai_response(content, question_type, options)

        if answer_list:
            logger.info(f"解析成功: {answer_list}")
            return AnswerResult(
                source="deepseek",
                answer_text=content,
                answer_list=answer_list,
                success=True,
            )
        else:
            logger.warning(f"返回内容未匹配: {content[:100]}")
            return AnswerResult(source="deepseek", answer_text=content, success=False)

    except httpx.TimeoutException:
        logger.error("DeepSeek API请求超时")
        return AnswerResult(source="deepseek", success=False, error_reason="API 请求超时")
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        reason = "认证失败(Key无效)" if status == 401 else f"HTTP {status}"
        logger.error(f"DeepSeek API HTTP错误: {status}")
        return AnswerResult(source="deepseek", success=False, error_reason=reason)
    except Exception as e:
        logger.error(f"DeepSeek API请求异常: {e}")
        return AnswerResult(source="deepseek", success=False, error_reason=f"请求异常: {str(e)[:50]}")


async def ask_tiku_server(
    question: str,
    question_type: str,
    options: List[str],
    config: Config = None,
) -> AnswerResult:
    """
    使用tiku题库服务器获取答案（支持多题库并发）

    移植自脚本 getAnswerFromCustomApi + OCS defaultAnswerWrapperHandler
    """
    if config is None:
        config = Config()

    # 优先使用多题库配置
    if config.tiku_servers:
        wrappers = parse_answer_wrapper(config.tiku_servers)
        if wrappers:
            answers = await answer_wrapper_handler(wrappers, question, options, question_type)
            if answers:
                # 按题型解析答案格式
                answer_list = _parse_tiku_answers(answers, question_type)
                logger.info(f"多题库查询成功: {answer_list}")
                return AnswerResult(
                    source="tiku",
                    answer_text="#".join(answers),
                    answer_list=answer_list,
                    success=True,
                )
            logger.info("多题库未找到答案")
            return AnswerResult(source="tiku", success=False)

    # 兼容旧版单题库
    if not config.tiku_api_url:
        return AnswerResult(source="tiku", success=False)

    logger.info(f"使用题库服务器查询: {config.tiku_api_url}")

    request_data = {
        "question": question,
        "type": question_type,
        "options": options or [],
        "key": config.tiku_api_key or "",
    }

    client = _get_client()
    try:
        response = await client.post(
            config.tiku_api_url,
            json=request_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.tiku_api_key}" if config.tiku_api_key else "",
            },
        )
        response.raise_for_status()
        data = response.json()

        logger.info(f"题库服务器返回: {data}")

        # 兼容多种返回格式 (与脚本 L207 一致)
        answer = ""
        if data.get("code") in (1, -1, 0) or data.get("success"):
            answer = data.get("answer") or (data.get("data", {}) or {}).get("answer", "")

        if answer:
            answer_list = _parse_tiku_answers([answer], question_type)
            logger.info(f"题库服务器解析成功: {answer_list}")
            return AnswerResult(
                source="tiku",
                answer_text=answer,
                answer_list=answer_list,
                success=True,
            )

        logger.info("题库服务器未找到答案")
        return AnswerResult(source="tiku", success=False)

    except Exception as e:
        logger.error(f"题库服务器请求异常: {e}")
        return AnswerResult(source="tiku", success=False)


def _parse_tiku_answers(answers: List[str], question_type: str) -> List[str]:
    """解析题库返回的答案，按题型处理分隔符"""
    result: List[str] = []
    for ans in answers:
        if question_type == "1":
            # 多选题: # 分隔
            result.extend(s.strip() for s in ans.split("#") if s.strip())
        elif question_type == "2":
            # 填空题: | 分隔
            result.extend(s.strip() for s in ans.split("|") if s.strip())
        else:
            result.append(ans.strip())
    return result if result else answers


async def get_answer(
    question: str,
    question_type: str,
    options: List[str] = None,
) -> AnswerResult:
    """
    统一答题接口: 双通道并行查询，取最快返回的有效答案。
    支持答案缓存（OCS 风格 LRU 缓存）。
    """
    if options is None:
        options = []

    config = _get_config()
    type_name = QUESTION_TYPE_NAMES.get(question_type, "未知类型")
    logger.info(f"答题 [{type_name}]: {question[:80]}...")

    # 答案缓存查询
    cache = get_answer_cache()
    cached = cache.get(question, question_type)
    if cached:
        logger.info(f"缓存命中: {cached}")
        return AnswerResult(
            source="cache",
            answer_text="#".join(cached),
            answer_list=cached,
            success=True,
        )

    has_tiku = config.has_tiku_config
    has_deepseek = config.has_deepseek_config

    # 首次调用时输出配置状态（帮助排查"未获取到答案"问题）
    if not getattr(get_answer, '_logged_config', False):
        ds_key = config.deepseek_api_key or ""
        ds_preview = ds_key[:8] + "***" if len(ds_key) > 8 else ds_key
        logger.info(f"答题引擎配置: tiku={'已配置' if has_tiku else '未配置'}, "
                    f"deepseek={'已配置' if has_deepseek else '未配置'} "
                    f"(key={ds_preview}), tiku_url={config.tiku_api_url or '空'}")
        if has_deepseek and len(ds_key) < 10:
            logger.warning(f"DeepSeek API Key 过短({len(ds_key)}字符)，可能无效")
        get_answer._logged_config = True

    if not has_tiku and not has_deepseek:
        logger.warning("未配置任何答题通道（题库和 DeepSeek 均未配置），请先在设置中配置 API Key")
        return AnswerResult(source="none", success=False)

    # 双通道并行: 同时发起请求，取最先成功的答案
    result: Optional[AnswerResult] = None
    if has_tiku and has_deepseek:
        tiku_task = asyncio.create_task(
            ask_tiku_server(question, question_type, options, config)
        )
        ds_task = asyncio.create_task(
            ask_deepseek(question, question_type, options, config)
        )

        # 等待任一完成
        done, pending = await asyncio.wait(
            {tiku_task, ds_task}, return_when=asyncio.FIRST_COMPLETED
        )
        # 优先取 tiku 结果（精确匹配）
        first_done = done.pop()
        first_result = first_done.result()
        if first_result and first_result.answer_list:
            # 取消未完成的任务
            for t in pending:
                t.cancel()
            logger.info(f"并行查询最快返回: {first_result.source}")
            result = first_result
        else:
            # 第一个未成功，等另一个
            if pending:
                second_task = pending.pop()
                try:
                    second_result = await second_task
                    if second_result and second_result.answer_list:
                        logger.info(f"并行查询第二个返回: {second_result.source}")
                        result = second_result
                except asyncio.CancelledError:
                    pass

        if result is None:
            logger.warning("双通道均未获取到有效答案")
            result = first_result if first_result else AnswerResult(source="none", success=False)

    # 单通道回退
    elif has_tiku:
        result = await ask_tiku_server(question, question_type, options, config)
    else:
        result = await ask_deepseek(question, question_type, options, config)

    # 写入缓存
    if result and result.success and result.answer_list:
        cache.set(question, result.answer_list, question_type)

    return result


# ============================================================
# OCS 解析器集成 - 智能答案匹配
# ============================================================

def resolve_answer(
    answers: List[str],
    options: List[str],
    question_type: str,
    blank_count: int = 0,
    separators: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    使用 OCS 4阶段自适应算法将原始答案匹配到选项

    移植自 OCS createDefaultQuestionResolver + resolvers/

    Args:
        answers: 题库/AI返回的原始答案列表
        options: 题目选项列表
        question_type: 题目类型 (0单选/1多选/2填空/3判断)
        blank_count: 填空题的空数
        separators: 答案分隔符

    Returns:
        {
            "finish": bool,         # 是否匹配成功
            "options": List[str],   # 选中的选项（选择题）
            "answers": List[str],   # 填空的答案（填空题）
            "option": str,          # 选中的单个选项（单选/判断）
        }
    """
    if separators is None:
        config = _get_config()
        separators = config.answer_separators

    result = {"finish": False, "options": [], "answers": [], "option": None}

    if question_type == "0" or question_type == "single":
        # 单选题
        r = resolve_single(answers, options, separators)
        if r.finish and r.option:
            return {"finish": True, "option": r.option, "options": [r.option], "answers": []}
        return result

    elif question_type == "1" or question_type == "multiple":
        # 多选题
        r = resolve_multiple(answers, options, separators)
        if r.finish:
            opts = r.options or r.plain_options or []
            if opts:
                return {"finish": True, "options": opts, "option": None, "answers": []}
        return result

    elif question_type == "3" or question_type == "judgement":
        # 判断题
        answer_groups = [[a] for a in answers]
        # 也尝试拆分答案
        for i, a in enumerate(answers):
            answer_groups[i] = split_answer(a, separators)
        r = resolve_judgement(answer_groups, options)
        if r.finish and r.option:
            return {"finish": True, "option": r.option, "options": [r.option], "answers": []}
        return result

    elif question_type == "2" or question_type == "completion":
        # 填空题
        answer_groups = [split_answer(a, separators) for a in answers]
        r = resolve_completion(answer_groups, blank_count or 1, separators)
        if r.finish and r.answers:
            return {"finish": True, "answers": r.answers, "options": [], "option": None}
        return result

    elif question_type in ("4", "5", "6", "7", "9"):
        # 简答题/名词解释/论述题/计算题/材料题: 直接返回AI生成的文本答案
        text_answers = []
        for a in answers:
            a = a.strip()
            if a:
                text_answers.append(a)
        if text_answers:
            return {"finish": True, "answers": text_answers, "options": [], "option": None}
        return result

    return result


async def get_answer_enhanced(
    question: str,
    question_type: str,
    options: List[str] = None,
    blank_count: int = 0,
) -> Dict[str, Any]:
    """
    增强版答题接口: 获取答案 + OCS 智能匹配

    流程:
    1. 查缓存 → 命中则直接用 OCS 解析器匹配
    2. 双通道查答案（多题库 + AI）
    3. 用 OCS 4阶段算法匹配到选项
    4. 写入缓存

    Returns:
        {
            "source": str,          # 答案来源
            "finish": bool,         # 是否匹配成功
            "options": List[str],   # 选中的选项
            "answers": List[str],   # 填空答案
            "option": str,          # 单选/判断的选项
            "raw_answers": List[str], # 原始答案
        }
    """
    if options is None:
        options = []

    config = _get_config()

    # 1. 获取原始答案
    answer_result = await get_answer(question, question_type, options)

    if not answer_result or not answer_result.success:
        return {
            "source": answer_result.source if answer_result else "none",
            "finish": False,
            "options": [],
            "answers": [],
            "option": None,
            "raw_answers": [],
        }

    raw_answers = answer_result.answer_list or ([answer_result.answer_text] if answer_result.answer_text else [])

    # 2. 使用 OCS 解析器匹配
    # 多选题关键修复: 将多个答案合并为单个源，否则 resolve_multiple 会把每个答案
    # 当成独立的"题库源"分别匹配，每组只匹配1个，然后只取最佳组 → 丢失选项
    if question_type == "1" and len(raw_answers) > 1:
        # 用 # 拼接多个答案，resolve_multiple 内部 split_answer 会按 # 拆分后一起匹配
        combined_answers = ["#".join(raw_answers)]
    else:
        combined_answers = raw_answers

    resolved = resolve_answer(
        combined_answers, options, question_type, blank_count, config.answer_separators
    )

    # 2.5 多选题特殊处理: 当 OCS 匹配不足时，用原始 AI 回复做纯ABCD兜底
    if question_type == "1" and answer_result.answer_text:
        matched_count = len(resolved.get("options", []))
        ai_text = answer_result.answer_text.strip()
        if matched_count < 2:
            # 支持多种格式: "AB", "A、B、C", "A,B,C", "ABCFJ" 等（A-Z 全范围）
            plain_letters = re.search(r'[A-Z]{2,}', ai_text)
            if not plain_letters:
                # 提取被非字母字符分隔的大写字母 (如 "A、B、E、J" → "ABEJ")
                all_abcd = re.findall(r'(?<![a-zA-Z])[A-Z](?![a-zA-Z])', ai_text)
                if len(all_abcd) >= 2:
                    plain_letters_text = "".join(dict.fromkeys(all_abcd))  # 去重保序
                    logger.info(f"多选题 OCS 匹配不足({matched_count}个)，提取字母兜底: {ai_text} -> {plain_letters_text}")
                    plain_result = resolve_answer(
                        [plain_letters_text], options, question_type, blank_count, config.answer_separators
                    )
                    if plain_result["finish"] and len(plain_result.get("options", [])) > matched_count:
                        logger.info(f"字母提取兜底成功: {plain_result['options']}")
                        resolved = plain_result
            else:
                logger.info(f"多选题 OCS 匹配不足({matched_count}个)，尝试纯ABCD兜底: {ai_text}")
                plain_result = resolve_answer(
                    [ai_text], options, question_type, blank_count, config.answer_separators
                )
                if plain_result["finish"] and len(plain_result.get("options", [])) > matched_count:
                    logger.info(f"纯ABCD兜底成功: {plain_result['options']}")
                    resolved = plain_result

    # 2.55 多选题最少2个选项校验: 如果仍然不足2个，标记为未完成
    if question_type == "1" and resolved["finish"]:
        if len(resolved.get("options", [])) < 2:
            logger.warning(
                f"多选题答案不足2个({len(resolved.get('options', []))}个): "
                f"{resolved.get('options', [])}, AI原文: {answer_result.answer_text[:100] if answer_result.answer_text else 'empty'}"
            )
            resolved["finish"] = False

    # 2.6 文本类题型回退: 当 OCS 未匹配时，直接用 AI 原始回答作为答案
    if not resolved["finish"] and question_type in ("2", "4", "5", "6", "7", "9"):
        if raw_answers:
            resolved = {"finish": True, "answers": raw_answers, "options": [], "option": None}
            logger.info(f"文本类题型({question_type}) OCS未匹配，回退AI原始回答: {raw_answers[:2]}")

    return {
        "source": answer_result.source,
        "finish": resolved["finish"],
        "options": resolved["options"],
        "answers": resolved["answers"],
        "option": resolved["option"],
        "raw_answers": raw_answers,
    }


async def cleanup():
    """关闭持久化 HTTP 客户端（应用退出时调用）"""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
    # 关闭题库模块的 HTTP 客户端
    from .answer_wrapper import cleanup_client
    await cleanup_client()
