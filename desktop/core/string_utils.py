"""
字符串匹配工具 - 移植自 OCS (ocsjs) 的 core/utils/string.ts

包含:
- clear_string: 清除特殊字符，只保留中英文数字
- normalize_string: 归一化（全角转半角、去标点空格）
- remove_redundant: 去除选项开头的冗余标签 (A. / A、 / A选项)
- find_best_match / dice_coefficient: 字符串相似度 (Sorensen-Dice 系数)
- answer_similar: 答案相似度匹配
- answer_similar_with_gap: 带领先度消歧的相似度匹配
- answer_normalized_match: 归一化精确匹配
- answer_exact_match: 精确匹配
"""

import re
import unicodedata
from typing import List, Optional, Tuple, Dict, Any


# ============================================================
# 基础字符串处理
# ============================================================

def clear_string(s: str, *exclude: str) -> str:
    """
    删除特殊字符，全部转小写，只保留中英文、数字

    移植自 OCS clearString
    """
    if not s:
        return ""
    exclude_str = "".join(exclude) + "①②③④⑤⑥⑦⑧⑨"
    # 保留: CJK统一汉字(\u2E80-\u9FFF)、字母、数字、以及 exclude 中的字符
    pattern = f"[^\\u2E80-\\u9FFFA-Za-z0-9{re.escape(exclude_str)}]*"
    result = re.sub(pattern, "", s.strip().lower())
    return result


def normalize_string(s: str) -> str:
    """
    归一化字符串：去除标点、空格、全角转半角后再 clear_string

    移植自 OCS normalizeString
    """
    if not s:
        return ""
    # 去除中文标点和空白
    s = re.sub(r"[，。！？；：""''、（）【】《》\\s]", "", s)
    # 去除英文标点
    s = re.sub(r"[,.\-!?;:'\"()\[\]<>]", "", s)
    # 全角转半角
    s = _fullwidth_to_halfwidth(s)
    # ％ → %
    s = s.replace("％", "%")
    return clear_string(s)


def _fullwidth_to_halfwidth(s: str) -> str:
    """全角字符转半角"""
    result = []
    for c in s:
        code = ord(c)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif code == 0x3000:  # 全角空格
            result.append(" ")
        else:
            result.append(c)
    return "".join(result)


def remove_redundant(s: str) -> str:
    """
    删除题目选项中开头的冗余字符串

    两条规则依次执行：
    1. A. / A、 / A) 等带分隔符的选项标签：字母 + 分隔符 + 内容
    2. A选项 / C男女平等 等字母直接紧跟中文的标签：字母 + 中文内容
       （仅当字母后紧跟中文时剥离，故 "TCP协议"、"CPU" 等字母串不被误删）

    移植自 OCS removeRedundant
    """
    if not s:
        return ""
    s = s.strip()
    # 规则1: A. / A、 / A) 等带分隔符的标签
    s = re.sub(r"^[A-Z]{1}[^A-Za-z0-9⺀-鿿]+([A-Za-z0-9⺀-鿿]+)", r"\1", s)
    # 规则2: A选项 等字母直接紧跟中文
    s = re.sub(r"^[A-Z]{1}([⺀-鿿][A-Za-z0-9⺀-鿿]*)", r"\1", s)
    return s


# ============================================================
# 字符串相似度 (Sorensen-Dice 系数)
# ============================================================

def _get_bigrams(s: str) -> set:
    """获取字符串的所有二元组"""
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i+2] for i in range(len(s) - 1)}


def dice_coefficient(s1: str, s2: str) -> float:
    """
    计算两个字符串的 Sorensen-Dice 相似度系数

    与 npm string-similarity 包的算法一致
    返回 0.0 ~ 1.0
    """
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    bigrams1 = _get_bigrams(s1)
    bigrams2 = _get_bigrams(s2)

    if not bigrams1 or not bigrams2:
        return 0.0

    intersection = bigrams1 & bigrams2
    return (2.0 * len(intersection)) / (len(bigrams1) + len(bigrams2))


def find_best_match(target: str, candidates: List[str]) -> Dict[str, Any]:
    """
    在候选列表中找到与目标字符串最相似的

    返回: {"rating": float, "target": str}
    移植自 OCS findBestMatch
    """
    if not candidates:
        return {"rating": 0.0, "target": ""}

    best_rating = -1.0
    best_target = ""

    for candidate in candidates:
        rating = dice_coefficient(target, candidate)
        if rating > best_rating:
            best_rating = rating
            best_target = candidate

    return {"rating": best_rating, "target": best_target}


# ============================================================
# 答案匹配算法
# ============================================================

def answer_similar(answers: List[str], options: List[str]) -> List[Dict[str, Any]]:
    """
    答案相似度匹配，返回每个选项的相似度对象列表

    移植自 OCS answerSimilar
    返回: [{"rating": float, "target": str}, ...]
    """
    _answers = [clear_string(remove_redundant(a)) for a in answers]
    _options = [clear_string(remove_redundant(o)) for o in options]

    if not _answers:
        return [{"rating": 0.0, "target": ""} for _ in _options]

    similar = []
    for option in _options:
        if option.strip() == "":
            similar.append({"rating": 0.0, "target": ""})
        else:
            similar.append(find_best_match(option, _answers))

    return similar


def answer_similar_with_gap(answers: List[str], options: List[str]) -> List[Dict[str, Any]]:
    """
    带领先度消歧的相似度匹配

    返回每个选项的相似度 + 与次优选项的领先度
    用于解决"相似选项全选"的问题

    移植自 OCS answerSimilarWithGap
    返回: [{"rating": float, "target": str, "gap": float}, ...]
    """
    _answers = [clear_string(remove_redundant(a)) for a in answers]
    _options = [clear_string(remove_redundant(o)) for o in options]

    if not _answers:
        return [{"rating": 0.0, "target": "", "gap": 0.0} for _ in _options]

    ratings = []
    for option in _options:
        if option.strip() == "":
            ratings.append({"rating": 0.0, "target": ""})
        else:
            ratings.append(find_best_match(option, _answers))

    # 计算领先度
    sorted_ratings = sorted(ratings, key=lambda x: x["rating"], reverse=True)
    max_rating = sorted_ratings[0]["rating"] if sorted_ratings else 0.0
    second_rating = 0.0
    for i in range(1, len(sorted_ratings)):
        if sorted_ratings[i]["rating"] < max_rating:
            second_rating = sorted_ratings[i]["rating"]
            break

    result = []
    for r in ratings:
        gap = (max_rating - second_rating) if r["rating"] == max_rating else 0.0
        result.append({**r, "gap": gap})

    return result


def answer_normalized_match(answers: List[str], options: List[str]) -> List[str]:
    """
    归一化精确匹配模式

    对答案和选项进行归一化处理后精确比对
    仅取"答案⊇选项"或相等，不取"选项⊇答案"
    避免 "TCP" 误选 "TCP/IP协议" 等语义不一致的选项

    移植自 OCS answerNormalizedMatch
    返回: 匹配到的选项列表（按匹配级别降序排列）
    """
    _answers = [normalize_string(remove_redundant(a)) for a in answers]
    _options = [normalize_string(remove_redundant(o)) for o in options]

    if not _answers:
        return []

    matched = []
    for i, opt in enumerate(_options):
        level = 0
        if opt:
            for ans in _answers:
                if ans == opt:
                    level = 2
                    break
                elif opt in ans:
                    level = 1
        if level > 0:
            matched.append({"opt": opt, "original": options[i], "level": level})

    matched.sort(key=lambda x: x["level"], reverse=True)
    return [m["original"] for m in matched]


def answer_exact_match(answers: List[str], options: List[str]) -> List[str]:
    """
    精准匹配模式，返回符合的选项字符串列表

    移植自 OCS answerExactMatch
    """
    _answers = [remove_redundant(a) for a in answers]
    _options = [remove_redundant(o) for o in options]

    if not _answers:
        return []

    return [
        opt for opt in _options
        if any(ans.strip() == opt.strip() for ans in _answers)
    ]
