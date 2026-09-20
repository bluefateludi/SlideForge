"""幻觉核对纯逻辑（eval#3）：数字抽取、年份豁免、序号过滤、出处核对。

全部为确定性纯函数测试，不触网不耗 API 额度。
材料样例对齐真实题集（q3_sales.md 风格的中文数字语境）。
"""

from __future__ import annotations

from app.eval.hallucination import (
    check_numbers_against_source,
    extract_eval_numbers,
)

# 与真实题集同风格的材料片段
Q3_SOURCE = """
Q3 总营收 1.86 亿元，同比增长 24%
毛利率 41.2%，同比提升 2.4 个百分点
华东 7200 万元，+31%，占比 38.7%
大客户数量 126 家，平均客单价 48 万元
2023 年为基期，2024 年 Q3 数据如下
"""


class TestExtractEvalNumbers:
    def test_extracts_plain_and_decimal_numbers(self) -> None:
        text = "营收 1.86 亿元，大客户 126 家，客单价 48 万元"
        assert extract_eval_numbers(text) == ["1.86", "126", "48"]

    def test_extracts_percent_with_full_width_sign(self) -> None:
        assert extract_eval_numbers("渗透率 31.6％，同比 +2.4%") == ["31.6％", "2.4%"]

    def test_extracts_thousands_separator(self) -> None:
        assert "1,234" in extract_eval_numbers("合同 1,234 份")

    def test_filters_ordinal_like_short_integers(self) -> None:
        # 1-2 位裸整数视为页码/序号/计数措辞，不参与幻觉统计
        # （"12" 是 2 位裸整数但紧跟"条"不属量级单位，同样被过滤）
        assert extract_eval_numbers("第 1 页，共 3 个要点，第 12 条") == []

    def test_keeps_three_digit_ordinal_range(self) -> None:
        # 三位数不在序号过滤范围（可能是真实数据），保留
        assert extract_eval_numbers("编号 128") == ["128"]

    def test_ignores_version_like_tokens(self) -> None:
        # 前后紧邻字母数字的片段不吞（版本号、标识符）
        assert extract_eval_numbers("v2.4.1 与 GPT4") == []


class TestCheckNumbersAgainstSource:
    def test_numbers_present_in_source_count_as_sourced(self) -> None:
        report = check_numbers_against_source(
            "总营收 1.86 亿元，毛利率 41.2%，大客户 126 家", Q3_SOURCE
        )
        assert report.total_numbers == 3
        assert report.fabricated_numbers == 0
        assert report.hallucination_rate == 0.0

    def test_fabricated_numbers_are_reported(self) -> None:
        report = check_numbers_against_source(
            "总营收 1.86 亿元，市占率 73.5%，客单价 99 万元", Q3_SOURCE
        )
        # 73.5 与 99 材料中没有 → 编造；1.86 有出处
        assert report.total_numbers == 3
        assert report.fabricated_numbers == 2
        assert report.hallucination_rate == 2 / 3
        fabricated = [f.token for f in report.findings if not f.has_source]
        assert fabricated == ["73.5%", "99"]

    def test_year_is_exempt_when_source_has_any_year(self) -> None:
        report = check_numbers_against_source("回顾 2023 年与 2024 年的表现", Q3_SOURCE)
        # 材料含年份 → 年份复述不算编造
        assert report.total_numbers == 2
        assert report.fabricated_numbers == 0

    def test_year_counts_when_source_has_no_year(self) -> None:
        report = check_numbers_against_source("成立于 2015 年", "没有任何年份的材料")
        assert report.fabricated_numbers == 1
        finding = report.findings[0]
        assert finding.token == "2015"
        assert not finding.has_source

    def test_thousands_separator_normalization(self) -> None:
        # deck 写千分位、材料写裸数：去千分位后可匹配
        report = check_numbers_against_source("新签约 7,200 万元", "新签约 7200 万元")
        assert report.fabricated_numbers == 0

    def test_trailing_zero_decimal_normalization(self) -> None:
        # deck 写 31.60、材料写 31.6：数值等价即有出处
        report = check_numbers_against_source("渗透率 31.60%", "渗透率 31.6%")
        assert report.fabricated_numbers == 0

    def test_unit_conversion_is_not_recognized_limitation(self) -> None:
        # 朴素口径的已知局限：亿/万换算不识别，材料 1.86 亿 vs deck 18600 万记编造
        report = check_numbers_against_source("总营收 18600 万元", "总营收 1.86 亿元")
        assert report.fabricated_numbers == 1

    def test_ordinal_numbers_are_excluded_from_rate(self) -> None:
        report = check_numbers_against_source("第 1 页讲 45% 的增速", "增速 45%")
        # "1" 被序号过滤，不进分母
        assert report.total_numbers == 1
        assert report.hallucination_rate == 0.0

    def test_empty_inputs_yield_zero_rate(self) -> None:
        report = check_numbers_against_source("", "")
        assert report.total_numbers == 0
        assert report.hallucination_rate == 0.0

    def test_duplicate_tokens_are_counted_once(self) -> None:
        report = check_numbers_against_source("占 38.7%，其中 38.7% 来自华东", Q3_SOURCE)
        assert report.total_numbers == 1

    def test_percent_vs_bare_variant(self) -> None:
        # deck 写 41.2%（带百分号）、材料写 41.2%：命中；
        # deck 省略百分号写 38.7、材料写 38.7%：补百分号变体后命中
        report = check_numbers_against_source("毛利率 41.2%；占比 38.7", Q3_SOURCE)
        assert report.fabricated_numbers == 0
