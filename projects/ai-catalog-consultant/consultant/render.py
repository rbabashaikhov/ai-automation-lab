"""Deterministic debug rendering of an ``EvidenceBundle`` for humans, tests and evaluation.

Not a Consultant answer and not LLM input (see ``evidence.serialize_for_llm`` for that). It shows
everything, including diagnostic similarity and ranks, so reviewers can see why evidence exists.
"""

from __future__ import annotations

from .evidence import EvidenceBundle, ProductEvidence


def _product(p: ProductEvidence) -> list:
    facts = {f.fact_id.split(".", 1)[1]: f for f in p.facts}
    price = facts.get("col.effective_price")
    avail = facts.get("col.is_available")
    lines = [f"{p.handle}  {p.model_code}  {p.name}",
             f"    identity: ({p.source}, {p.external_id})  url: {p.url or '-'}",
             f"    price: {price.value if price else 'not listed'} RUB (live)   available: {avail.value if avail else '-'}"]
    if p.selection_reasons or p.ranking_debug:
        lines.append(f"    selection: {', '.join(p.selection_reasons)}"
                     + (f"   ranks: {p.ranking_debug}" if p.ranking_debug else ""))
    for c in p.constraints:
        mark = {True: "ok", False: "VIOLATED", None: "unknown"}[c.satisfied]
        lines.append(f"    constraint [{mark}] {c.requested} (actual {c.actual})")
    for f in p.features:
        v = f" = {f.value:g}" if f.value is not None else ""
        dq = f"  data_quality={f.data_quality}" if f.data_quality else ""
        lines.append(f"    feature {f.feature_id}: {f.state}{v}  <- {', '.join(f.fact_ids) or '-'}{dq}")
    for f in p.facts:
        unit = f" {f.unit}" if f.unit else ""
        lines.append(f"    fact {f.fact_id}: {f.label} = {f.value}{unit}")
    for x in p.passages:
        sim = f" sim={x.similarity:.3f}" if x.similarity is not None else ""
        stripped = f" stripped={x.stripped_lines}" if x.stripped_lines else ""
        lines.append(f"    passage #{x.chunk_id} [{x.section} via {x.retrieval}{sim}{stripped}]")
        lines.extend(f"      | {line}" for line in x.text.splitlines())
    return lines


def render_debug(b: EvidenceBundle) -> str:
    lines = [f"EVIDENCE {b.version}  intent={b.intent}  route={b.route} ({b.router_rule})",
             f"confidence: {b.retrieval_confidence}  ({', '.join(b.confidence_reasons)})",
             f"request: {b.plan_summary}",
             f"totals: {b.totals}",
             f"semantic: {b.semantic}",
             f"budget: {b.budget}", ""]
    for p in b.products:
        lines.extend(_product(p))
    if b.alternatives:
        lines.append("ALTERNATIVES (each violates at least one original constraint)")
        for p in b.alternatives:
            lines.extend(_product(p))
    lines.append("GAPS" if b.gaps else "GAPS: none")
    for g in b.gaps:
        who = f" [{', '.join(g.handles)}]" if g.handles else ""
        lines.append(f"  - {g.kind}{who}: {g.detail}")
    return "\n".join(lines)
