"""页面收敛只认最终校验结果；历史无证据的 Trace 不推断为通过。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Convergence:
    total: int = 0
    first_pass: int = 0
    repaired: int = 0
    repaired_passed: int = 0
    missing: int = 0
    repaired_missing: int = 0

    @property
    def first_pass_rate(self) -> float | None:
        return self.first_pass / self.total if self.total and not self.missing else None

    @property
    def repair_success_rate(self) -> float | None:
        return (
            self.repaired_passed / self.repaired
            if self.repaired and not self.repaired_missing
            else None
        )


def summarize_convergence(spans) -> Convergence:
    spans = list(spans)
    children = {}
    for span in spans:
        children.setdefault(span.parent_span_id, []).append(span)
    total = first = repaired = passed = missing = repaired_missing = 0
    for task in spans:
        if (
            task.span_kind != "task"
            or not task.name.startswith("slide[")
            or task.status not in {"succeeded", "failed"}
        ):
            continue
        total += 1
        nodes = children.get(task.id, [])
        had_repair = any(node.name == "slide.repair" for node in nodes)
        checks = [node for node in nodes if node.name == "slide.check"]
        final = max(checks, key=lambda node: node.id, default=None)
        attributes = final.attributes if final and isinstance(final.attributes, dict) else {}
        quality = attributes.get("quality_passed")
        known = task.status == "failed" or isinstance(quality, bool)
        valid = task.status == "succeeded" and quality is True
        missing += not known
        first += valid and not had_repair
        repaired += had_repair
        passed += had_repair and valid
        repaired_missing += had_repair and not known
    return Convergence(total, first, repaired, passed, missing, repaired_missing)
