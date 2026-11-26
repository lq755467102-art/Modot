"""Recompute paired metrics from the published per-run rows (standard library)."""
import json
from collections import Counter, defaultdict
from pathlib import Path

data = json.loads((Path(__file__).resolve().parents[1] / "docs/benchmarks/modot-2026-11-23-results.json").read_text(encoding="utf-8"))
runs = data["comparison"]["runs"]
pairs = defaultdict(dict)
passed, delivered = Counter(), Counter()
for row in runs:
    product = row["product"]
    key = row["protocol"], row["caseId"]
    if product not in ("modot", "codex") or product in pairs[key]:
        raise ValueError(f"Unexpected or duplicate row for {key}")
    pairs[key][product] = row
    passed[product] += row["independentlyPassed"]
    delivered[product] += row["normalDelivery"]
if not pairs or any(set(x) != {"modot", "codex"} for x in pairs.values()):
    raise ValueError("Missing comparison partner")
matched = [pair for pair in pairs.values() if all(run["normalDelivery"] for run in pair.values())]
totals = {p: {field: sum(pair[p][field] for pair in matched)
              for field in ("wallMs", "inputTokens", "cachedInputTokens", "outputTokens", "totalTokens", "apiEquivalentUsd")}
          for p in ("modot", "codex")}
print(json.dumps({"scoringEntries": len(pairs), "passed": dict(passed), "delivered": dict(delivered),
                  "jointlyDelivered": len(matched), "fasterModot": sum(x["modot"]["wallMs"] < x["codex"]["wallMs"] for x in matched),
                  "timeReductionPercent": round(100 * (1 - totals["modot"]["wallMs"] / totals["codex"]["wallMs"]), 3),
                  "pairedTotals": totals}, indent=2))
