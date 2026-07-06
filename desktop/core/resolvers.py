"""
答题解析器 - 移植自 OCS (ocsjs) 的 core/worker/resolvers/

纯函数实现，不接触 DOM，便于测试和复用。

包含:
- split_answer: 智能答案拆分（JSON数组 → 分隔符拆分）
- is_plain_answer / resolve_plain_answer: 纯ABCD答案识别
- is_program_answer: 代码题检测
- resolve_single: 单选题4阶段自适应匹配
- resolve_multiple: 多选题自适应匹配+领先度消歧
- resolve_judgement: 判断题关键词匹配
- resolve_completion: 填空题匹配
- disambiguate_similar_options: 领先度消歧
- default_work_type_resolver: 根据DOM元素判断题型
"""

import re
import json
import logging
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass, field

from .string_utils import (
    clear_string, normalize_string, remove_redundant,
    answer_similar, answer_similar_with_gap,
    answer_normalized_match, answer_exact_match,
    find_best_match,
)

logger = logging.getLogger(__name__)


# ============================================================
# 答案处理工具
# ============================================================

def is_plain_answer(answer: str) -> bool:
    """
    判断答案是否为A-Z的文本，字符序号依次递增，每个字符只出现一次

    支持最多26个选项（A-Z），移植自 OCS isPlainAnswer
    """
    answer = answer.strip()
    if len(answer) > 26 or not re.search(r"[A-Z]", answer):
        return False
    counter: Dict[int, int] = {}
    min_code = 0
    for ch in answer:
        code = ord(ch)
        # 只允许大写字母 A-Z
        if code < 65 or code > 90:
            return False
        if code < min_code:
            return False
        min_code = code
        counter[code] = counter.get(code, 0) + 1
    return all(v == 1 for v in counter.values())


def resolve_plain_answer(answer: str) -> Optional[str]:
    """
    判断是否为纯ABCD多选答案，但中间存在分隔符

    移植自 OCS resolvePlainAnswer
    """
    resolved = re.sub(r"[,，.。．、 #]", "", answer.strip()).strip()
    if is_plain_answer(resolved):
        return resolved
    return None


def is_program_answer(answer: str) -> bool:
    """
    检测答案是否为一段程序代码

    程序题的题库答案是一段代码，其中的 ; | ； 是代码语法，
    而非答案分隔符。本函数通过代码关键字、运算符等特征综合判定。

    移植自 OCS isProgramAnswer
    """
    code = (answer or "").strip()
    if not code:
        return False
    if not re.search(r"[;|；]", code):
        return False

    score = 0

    # 强特征：单独命中即可判定为程序
    strong_patterns = [
        r"#include\s*[<\"]",
        r"#define\s+\w",
        r"#!/",
        r"\b(?:int|void|float|double|char|long|short)\s+main\s*\(",
        r"\bfunction\s+\w+\s*\(",
        r"\bdef\s+\w+\s*\(",
        r"\bpublic\s+(?:class|static|void|int|float|double|String|final)\b",
        r"\bprivate\s+(?:class|static|void|int|float|double|String|final)\b",
        r"\bprotected\s+(?:class|static|void|int|float|double|String|final)\b",
        r"\bprintf\s*\(",
        r"\bscanf\s*\(",
        r"\bcout\s*<<",
        r"\bcin\s*>>",
        r"System\.out\.",
        r"console\.log\s*\(",
        r"\bpackage\s+[\w.]+\s*;",
        r"\bimport\s+[\w.*]+\s*;",
        r"\bclass\s+\w+\s*\{",
        r"\bstruct\s+\w+\s*\{",
    ]
    for pattern in strong_patterns:
        if re.search(pattern, code):
            score += 2

    # 中等特征
    medium_patterns = [
        r"=>", r"->", r"::", r"\+\+", r"--", r"[+\-*/%]=",
        r"\bnew\s+\w+\s*\(", r"\bfor\s*\(", r"\bwhile\s*\(",
        r"\bif\s*\(", r"\bswitch\s*\(", r"\btypedef\s+",
        r"\b(?:int|float|double|char|void|bool|boolean|long|short|String|auto|const|let|var)\s+\w+",
        r"\bsizeof\s*\(", r"\bmalloc\s*\(", r"\bfree\s*\(",
        r"\bcout\b", r"\bcin\b",
    ]
    for pattern in medium_patterns:
        if re.search(pattern, code):
            score += 1

    # 结构特征
    if re.search(r"\{", code) and re.search(r"\}", code):
        score += 1
    if re.search(r";", code):
        score += 1

    return score >= 2


def split_answer(
    answer: str,
    separators: Optional[List[str]] = None,
) -> List[str]:
    """
    分割答案

    智能拆分：先尝试 JSON.parse（数组），失败则按分隔符拆分。
    程序题答案中 ; | ； 是代码语法而非分隔符，检测到程序时取消这些分隔符。

    移植自 OCS splitAnswer
    """
    if separators is None:
        separators = ["===", "#", "---", "###", "|", ";", "；"]

    answer = (answer or "").strip()
    if not answer:
        return []

    separators = [s for s in separators if s.strip()]
    if not separators:
        separators = ["===", "#", "---", "###", "|", ";", "；"]

    # 程序题答案：; | ； 是代码语法
    if is_program_answer(answer):
        separators = [s for s in separators if s not in (";", "|", "；")]

    # 尝试 JSON 数组解析
    try:
        parsed = json.loads(answer)
        if isinstance(parsed, list):
            result = [str(el).strip() for el in parsed if str(el).strip()]
            if result:
                return result
    except (json.JSONDecodeError, TypeError):
        pass

    # 按分隔符拆分
    for sep in separators:
        parts = answer.split(sep)
        if len(parts) > 1:
            return [p.strip() for p in parts if p.strip()]

    return [answer]


# ============================================================
# 题型判断
# ============================================================

def default_work_type_resolver(
    radio_count: int = 0,
    checkbox_count: int = 0,
    textarea_count: int = 0,
) -> Optional[str]:
    """
    根据DOM元素数量判断题型

    移植自 OCS defaultWorkTypeResolver
    返回: 'judgement' | 'single' | 'multiple' | 'completion' | None
    """
    if radio_count == 2:
        return "judgement"
    if radio_count > 2:
        return "single"
    if checkbox_count > 2:
        return "multiple"
    if textarea_count >= 1:
        return "completion"
    return None


# ============================================================
# 单选题解析器
# ============================================================

@dataclass
class SingleResolveResult:
    """单选题匹配结果"""
    finish: bool = False
    option: Optional[str] = None
    ratings: Optional[List[float]] = None
    all_answer: Optional[List[str]] = None
    options: Optional[List[str]] = None


def resolve_single(
    answers: List[str],
    options: List[str],
    separators: Optional[List[str]] = None,
) -> SingleResolveResult:
    """
    单选题匹配算法（自适应）

    四阶段自动匹配：
    1. 归一化精确匹配 — 去除标点/空格/全角半角差异后精确比对
    2. 相似匹配 — 取所有选项中相似度最高且超过阈值的
    3. 纯ABCD答案兜底
    4. 多片段答案适配 — 合并后重新匹配

    移植自 OCS resolveSingle
    """
    all_answer: List[str] = []
    for a in answers:
        all_answer.extend(split_answer(a, separators))
    option_strings = [remove_redundant(o) for o in options]

    # ========== 阶段1: 归一化精确匹配 ==========
    normalized_result = answer_normalized_match(all_answer, option_strings)
    if normalized_result:
        try:
            index = option_strings.index(normalized_result[0])
            return SingleResolveResult(finish=True, option=options[index])
        except ValueError:
            pass

    # ========== 阶段2: 相似匹配（取最优） ==========
    ratings = answer_similar(all_answer, option_strings)

    best_index = -1
    best_rating = 0.0
    for i, r in enumerate(ratings):
        if r["rating"] > best_rating:
            best_rating = r["rating"]
            best_index = i

    if best_index != -1 and best_rating > 0.6:
        return SingleResolveResult(
            finish=True,
            option=options[best_index],
            ratings=[r["rating"] for r in ratings],
        )

    # ========== 阶段3: 纯ABCD答案兜底 ==========
    for answer in all_answer:
        ans = resolve_plain_answer(answer.strip())
        if ans and len(ans) == 1:
            index = ord(ans) - 65
            if 0 <= index < len(option_strings):
                return SingleResolveResult(finish=True, option=options[index])

    # ========== 阶段4: 多片段答案适配 ==========
    if len(all_answer) > 1:
        merged = "".join(all_answer)
        r = resolve_single([merged], options, separators)
        if r.finish:
            return r

    return SingleResolveResult(finish=False, all_answer=all_answer, options=option_strings)


# ============================================================
# 多选题解析器
# ============================================================

@dataclass
class MultipleMatchGroup:
    """多选题单个题库结果的匹配数据"""
    options: List[str] = field(default_factory=list)
    answers: List[str] = field(default_factory=list)
    ratings: List[float] = field(default_factory=list)
    similar_sum: float = 0.0
    similar_count: int = 0


@dataclass
class MultipleResolveResult:
    """多选题匹配结果"""
    finish: bool = False
    options: Optional[List[str]] = None
    plain_options: Optional[List[str]] = None
    groups: Optional[List[MultipleMatchGroup]] = None


def disambiguate_similar_options(
    options: List[str],
    ratings: List[float],
    threshold: float = 0.6,
) -> List[str]:
    """
    领先度消歧：候选选项两两比较文本相似度，如果非常相似则只保留与答案匹配度更高的

    解决多选题"一字之差的选项全选"问题

    移植自 OCS disambiguateSimilarOptions
    """
    if len(options) <= 1:
        return options

    normalized = [normalize_string(remove_redundant(o)) for o in options]
    eliminated = set()

    for i in range(len(options)):
        if i in eliminated:
            continue
        for j in range(i + 1, len(options)):
            if j in eliminated:
                continue
            pairwise = find_best_match(normalized[i], [normalized[j]])["rating"]
            if pairwise > threshold:
                if ratings[i] >= ratings[j]:
                    eliminated.add(j)
                else:
                    eliminated.add(i)
                    break

    return [options[i] for i in range(len(options)) if i not in eliminated]


def resolve_multiple(
    result_answers: List[str],
    options: List[str],
    separators: Optional[List[str]] = None,
) -> MultipleResolveResult:
    """
    多选题匹配算法（自适应）

    两阶段自动匹配：
    1. 归一化匹配（单向 答案⊇选项）— 仅当答案包含选项才命中
    2. 相似匹配 + 领先度消歧 — 候选选项两两比较，文本相似时只保留匹配度更高的
    3. 纯ABCD答案兜底

    移植自 OCS resolveMultiple
    """
    option_strings = [remove_redundant(o) for o in options]
    groups: List[MultipleMatchGroup] = []

    for i in range(len(result_answers)):
        answers = split_answer(result_answers[i].strip(), separators)

        # 阶段1: 归一化匹配（包含式）
        normalized_group = MultipleMatchGroup()
        normalized_options = answer_normalized_match(answers, option_strings)
        for opt in normalized_options:
            try:
                idx = option_strings.index(opt)
            except ValueError:
                continue
            # 严格方向：仅当 答案⊇选项（或归一化相等）时才视为命中
            matched_ans = None
            for a in answers:
                na = normalize_string(remove_redundant(a))
                no = normalize_string(remove_redundant(opt))
                if na == no or no in na:
                    matched_ans = a
                    break
            if not matched_ans:
                continue
            normalized_group.options.append(options[idx])
            normalized_group.answers.append(matched_ans)
            na = normalize_string(remove_redundant(matched_ans))
            no = normalize_string(remove_redundant(opt))
            rating = 1.0 if na == no else 0.8
            normalized_group.ratings.append(rating)
            normalized_group.similar_sum += rating
            normalized_group.similar_count += 1

        # 归一化匹配结果消歧
        normalized_disambiguated = disambiguate_similar_options(
            normalized_group.options, normalized_group.ratings
        )
        normalized_kept_indices = [normalized_group.options.index(opt) for opt in normalized_disambiguated]
        normalized_group.options = normalized_disambiguated
        normalized_group.answers = [normalized_group.answers[idx] for idx in normalized_kept_indices]
        normalized_group.ratings = [normalized_group.ratings[idx] for idx in normalized_kept_indices]
        normalized_group.similar_count = len(normalized_group.options)
        normalized_group.similar_sum = sum(normalized_group.ratings)

        # 阶段2: 相似度匹配
        rating_group = MultipleMatchGroup()
        ratings = answer_similar_with_gap(answers, option_strings)
        for j in range(len(ratings)):
            if ratings[j]["rating"] > 0.6:
                rating_group.options.append(options[j])
                rating_group.answers.append(ratings[j]["target"])
                rating_group.ratings.append(ratings[j]["rating"])
                rating_group.similar_sum += ratings[j]["rating"]
                rating_group.similar_count += 1

        # 领先度消歧
        disambiguated = disambiguate_similar_options(rating_group.options, rating_group.ratings)
        kept_indices = [rating_group.options.index(opt) for opt in disambiguated]
        rating_group.options = disambiguated
        rating_group.answers = [rating_group.answers[idx] for idx in kept_indices]
        rating_group.ratings = [rating_group.ratings[idx] for idx in kept_indices]
        rating_group.similar_count = len(rating_group.options)
        rating_group.similar_sum = sum(rating_group.ratings)

        # 选匹配度最高的
        best = max(
            [normalized_group, rating_group],
            key=lambda g: g.similar_count * 100 + g.similar_sum,
        )
        if i < len(groups):
            groups[i] = best
        else:
            groups.append(best)

    # 排序选择最优结果
    sorted_groups = sorted(
        [g for g in groups if g.similar_count != 0],
        key=lambda g: g.similar_count * 100 + g.similar_sum,
        reverse=True,
    )

    if sorted_groups:
        return MultipleResolveResult(
            finish=True,
            options=sorted_groups[0].options,
            groups=sorted_groups,
        )

    # 纯ABCD答案兜底
    plain_options: List[str] = []
    for answer in result_answers:
        ans = answer.strip()
        resolved = resolve_plain_answer(ans)
        if resolved:
            for ch in resolved:
                index = ord(ch) - 65
                if 0 <= index < len(options):
                    plain_options.append(options[index])

    if plain_options:
        # 去重保序
        seen = set()
        unique = []
        for opt in plain_options:
            if opt not in seen:
                seen.add(opt)
                unique.append(opt)
        return MultipleResolveResult(finish=True, plain_options=unique)

    return MultipleResolveResult(finish=False)


# ============================================================
# 判断题解析器
# ============================================================

CORRECT_WORDS = ["是", "对", "正确", "确定", "√", "对的", "是的", "正确的", "true", "True", "T", "yes", "1"]
INCORRECT_WORDS = ["非", "否", "错", "错误", "×", "X", "错的", "不对", "不正确的", "不正确", "不是", "不是的", "false", "False", "F", "no", "0"]


def _matches_judgement(target: str, words: List[str]) -> bool:
    """判断目标文本是否包含判断词"""
    for word in words:
        if clear_string(remove_redundant(word), "√", "×") == clear_string(remove_redundant(target), "√", "×"):
            return True
    return False


@dataclass
class JudgementResolveResult:
    """判断题匹配结果"""
    finish: bool = False
    option: Optional[str] = None


def resolve_judgement(
    answer_groups: List[List[str]],
    options: List[str],
) -> JudgementResolveResult:
    """
    判断题匹配算法

    遍历题库答案，判断答案文本是否包含正确/错误关键词，
    然后在选项中匹配对应关键词的选项。

    移植自 OCS resolveJudgement
    """
    for answers in answer_groups:
        answer_show_correct = None
        answer_show_incorrect = None
        for a in answers:
            if _matches_judgement(a, CORRECT_WORDS):
                answer_show_correct = a
            if _matches_judgement(a, INCORRECT_WORDS):
                answer_show_incorrect = a

        if answer_show_correct or answer_show_incorrect:
            for option in options:
                text_show_correct = _matches_judgement(option, CORRECT_WORDS)
                text_show_incorrect = _matches_judgement(option, INCORRECT_WORDS)

                if answer_show_correct and text_show_correct:
                    return JudgementResolveResult(finish=True, option=option)
                if answer_show_incorrect and text_show_incorrect:
                    return JudgementResolveResult(finish=True, option=option)

            return JudgementResolveResult(finish=False)

    return JudgementResolveResult(finish=False)


def is_judgement_options(options: List[str]) -> bool:
    """
    检测选项是否为判断题性质（仅有两个选项，且分别为"对"和"错"性质）

    移植自 OCS isJudgementOptions
    """
    if len(options) != 2:
        return False
    has_correct = any(_matches_judgement(opt, CORRECT_WORDS) for opt in options)
    has_incorrect = any(_matches_judgement(opt, INCORRECT_WORDS) for opt in options)
    return has_correct and has_incorrect


# ============================================================
# 填空题解析器
# ============================================================

@dataclass
class CompletionResolveResult:
    """填空题匹配结果"""
    finish: bool = False
    answers: Optional[List[str]] = None


def resolve_completion(
    answer_groups: List[List[str]],
    blank_count: int,
    separators: Optional[List[str]] = None,
) -> CompletionResolveResult:
    """
    填空题匹配算法

    遍历题库答案，找到答案数量与填空框数量一致的答案组，
    或者填空框只有一个时将所有答案合并。

    移植自 OCS resolveCompletion
    """
    for answers in answer_groups:
        ans = [a for a in answers if a]
        if len(ans) == 1:
            ans = split_answer(ans[0], separators)

        if ans and (len(ans) == blank_count or blank_count == 1):
            if len(ans) == blank_count:
                return CompletionResolveResult(finish=True, answers=ans)
            elif blank_count == 1:
                return CompletionResolveResult(finish=True, answers=[" ".join(ans)])

    return CompletionResolveResult(finish=False)
