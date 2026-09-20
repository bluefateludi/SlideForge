"""文档题幻觉核对：从生成结果抽取数字并回材料原文找出处（eval#3）。

口径（朴素版本，局限在报告与 PR 描述中注明）：
- 抽取范围：deck 全部可见文本（标题、块文本、表格、KPI、备注）中的
  阿拉伯数字串，含小数、百分号、千分位。
- 出处判定：数字本身（及其千分位/小数尾零归一变体）作为子串出现在
  材料原文（含去千分位副本）中即视为有出处；否则计为编造。
- 序号过滤：纯 1-2 位整数且不带百分号/小数点（页码、列表序号、
  "第 1 页"、"3 个要点" 这类计数措辞）不参与统计。
- 年份豁免：四位 1900-2099 区间的裸年份若材料中出现过任意年份，
  视为有出处（材料已含年份上下文，"2024 年" 这类复述不算编造）。
- 局限：子串匹配不懂语义，材料写 "1.86 亿" 而 deck 写 "18600 万"
  会被记为编造；数字变形单元（亿/万/倍）不换算。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

# 面向中文语境的数字抽取：
# - 千分位整数（可带小数）：1,234 / 1,234.56
# - 小数：31.6
# - 两位以上整数：48（纯一位数多为序号，交给序号过滤）
# - 可选百分号：31.6% / 31.6％
# 前后不能紧邻字母、数字或小数点，避免吞掉版本号、标识符的一段。
_NUMBER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9.])"
    r"(?:"
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"  # 1,234 或 1,234.5
    r"|\d+\.\d+"  # 小数
    r"|\d{2,}"  # 至少两位整数
    r")"
    r"([%％]?)"
    r"(?![A-Za-z0-9.])"
)

# 序号过滤的例外：1-2 位裸整数后（可隔一个空格）紧跟量级单位时视为真实数据，
# 如「48 万」「3 亿」「2.5 倍」；其余 1-2 位裸整数按页码/序号/计数措辞跳过。
_MAGNITUDE_UNITS = "万亿倍"
_ORDINAL_LIKE_RE = re.compile(r"^\d{1,2}$")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_ANY_YEAR_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")


class NumberFinding(BaseModel):
    """一个被核对过的数字。"""

    token: str  # 原样抽取（含百分号）
    has_source: bool
    reason: str  # 朴素口径下判定为有/无出处的规则名，便于报告解释


class HallucinationReport(BaseModel):
    """幻觉核对结果（单题，仅文档题）。"""

    total_numbers: int = 0
    fabricated_numbers: int = 0
    findings: list[NumberFinding] = Field(default_factory=list)

    @property
    def hallucination_rate(self) -> float:
        """编造数字数 / 提取数字总数；无数字时记 0（无编造）。"""
        if self.total_numbers == 0:
            return 0.0
        return self.fabricated_numbers / self.total_numbers


def extract_eval_numbers(text: str) -> list[str]:
    """抽取文本中参与幻觉核对的数字串（已滤序号）。"""
    tokens: list[str] = []
    for match in _NUMBER_TOKEN_RE.finditer(text):
        token = match.group(0)
        if not _ORDINAL_LIKE_RE.match(token):
            tokens.append(token)
            continue
        # 1-2 位裸整数：后跟量级单位（万/亿/倍）才算真实数据
        rest = text[match.end() :]
        if rest.lstrip()[:1] in tuple(_MAGNITUDE_UNITS):
            tokens.append(token)
    return tokens


def _normalized_variants(token: str) -> set[str]:
    """生成子串核对用的数字变体：去千分位、去百分号、去小数尾零、补百分号。

    与 quality 模块的 _number_variants 同思路，但这里独立维护：
    幻觉口径允许与告警口径略有差别（后者带告警语义）。
    """
    raw = token.replace("％", "%")
    bare = raw.replace("%", "")
    stripped = bare.replace(",", "")
    variants = {raw, bare, stripped}
    # 去小数尾零：31.60 ↔ 31.6（数值等价即视为同形）
    if "." in stripped:
        trimmed = stripped.rstrip("0").rstrip(".")
        if trimmed:
            variants.add(trimmed)
    try:
        value = float(stripped)
        if value.is_integer():
            variants.add(str(int(value)))
    except ValueError:
        pass
    # deck 省略百分号而材料带百分号（渗透率 31.6 vs 31.6%）视为同形
    if "%" not in raw:
        variants.add(f"{stripped}%")
        for item in list(variants):
            if not item.endswith("%"):
                variants.add(f"{item}%")
    return {item for item in variants if item}


def check_numbers_against_source(deck_text: str, source_text: str) -> HallucinationReport:
    """把 deck 文本中的数字逐一回材料原文核对出处。"""
    report = HallucinationReport()
    seen: set[str] = set()
    source_has_year = bool(_ANY_YEAR_RE.search(source_text or ""))
    compact_source = (source_text or "").replace(",", "")

    for token in extract_eval_numbers(deck_text):
        if token in seen:
            continue
        seen.add(token)

        if _YEAR_RE.match(token) and source_has_year:
            report.findings.append(
                NumberFinding(token=token, has_source=True, reason="年份豁免（材料含年份）")
            )
            continue

        variants = _normalized_variants(token)
        found = any(
            variant and (variant in source_text or variant in compact_source)
            for variant in variants
        )
        if found:
            report.findings.append(
                NumberFinding(token=token, has_source=True, reason="材料中找到同形数字")
            )
        else:
            report.findings.append(
                NumberFinding(token=token, has_source=False, reason="材料中未找到出处")
            )

    report.total_numbers = len(report.findings)
    report.fabricated_numbers = sum(1 for f in report.findings if not f.has_source)
    return report
