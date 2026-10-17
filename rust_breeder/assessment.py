"""Compare a six-gene observation with the user's selected target.

This module only compares user-entered text. It makes no prediction about
crossbreeding or whether a plant can reach a target in a future round.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .models import Target, normalize_genes


_REPORT_ORDER = "YGHWX"


@dataclass(frozen=True)
class Assessment:
    genes: str
    matched: bool
    matched_count: int
    missing: dict[str, int]
    excess: dict[str, int]
    wrong_positions: tuple[int, ...]
    headline: str
    detail: str


def assess_observed(genes: str, target: Target) -> Assessment:
    """Validate an observation and report its fit under the target's mode.

    ``matched_count`` measures matching gene quantities in counts mode and
    matching positions in exact mode. Quantity differences remain available
    in either mode. ``wrong_positions`` is empty when order does not matter.
    Invalid or incomplete observations raise ``ValueError`` rather than being
    treated as a failed target match.
    """
    observed = normalize_genes(genes)
    have = Counter(observed)
    wanted = Counter(target.genes)
    missing = {gene: wanted[gene] - have[gene] for gene in _REPORT_ORDER
               if wanted[gene] > have[gene]}
    excess = {gene: have[gene] - wanted[gene] for gene in _REPORT_ORDER
              if have[gene] > wanted[gene]}

    if target.mode == "exact":
        wrong_positions = tuple(index for index, (actual, desired)
                                in enumerate(zip(observed, target.genes), 1)
                                if actual != desired)
        matched_count = 6 - len(wrong_positions)
        if wrong_positions:
            corrections = "；".join(
                f"第 {index} 位：{observed[index - 1]}→{target.genes[index - 1]}"
                for index in wrong_positions
            )
            detail = f"目标按固定顺序逐位比较，已匹配 {matched_count}/6。差异：{corrections}。"
        else:
            detail = f"录入基因 {observed} 与目标 {target.genes} 的 6 个位置完全一致。"
    else:
        wrong_positions = ()
        matched_count = sum(min(have[gene], wanted[gene]) for gene in _REPORT_ORDER)
        if matched_count == 6:
            detail = f"目标只看数量，顺序不限。录入基因 {observed} 的数量与目标 {target.genes} 完全一致。"
        else:
            missing_text = "、".join(f"{count}{gene}" for gene, count in missing.items())
            excess_text = "、".join(f"{count}{gene}" for gene, count in excess.items())
            detail = (f"目标只看数量，顺序不限，已匹配 {matched_count}/6。"
                      f"还缺：{missing_text}；多出：{excess_text}。")

    matched = matched_count == 6
    headline = "这株符合当前目标" if matched else f"这株尚未达标（匹配 {matched_count}/6）"
    return Assessment(observed, matched, matched_count, missing, excess,
                      wrong_positions, headline, detail)
