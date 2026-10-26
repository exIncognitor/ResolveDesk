"""Small labeled retrieval comparison. No LLM judge or paid model calls."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def evaluate(knowledge, cases, principal):
    report = {}
    for mode in ("bm25", "vector", "hybrid"):
        details = []
        for case in cases:
            result = knowledge.search(principal, case["query"], 5, mode)
            rank = next((i for i,item in enumerate(result["items"],1)
                         if item["document_id"] == case["document"] and case["section"] in item["section"]), None)
            details.append({"id":case["id"], "rank":rank, "degraded":result["degraded"],
                            "retrieved":[{"document":i["document_id"],"section":i["section"]} for i in result["items"]]})
        n = len(details) or 1
        report[mode] = {"recall_at_5":sum(d["rank"] is not None for d in details)/n,
                        "hit_at_1":sum(d["rank"] == 1 for d in details)/n,
                        "mrr_at_5":sum(1/d["rank"] for d in details if d["rank"])/n,
                        "cases":details}
    return report


def main():
    from resolvedesk.retrieval import HybridKnowledge, LocalEmbedding
    from resolvedesk.store import Principal, Store
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    cases = json.loads((ROOT / "evaluation" / "retrieval_cases.json").read_text(encoding="utf-8"))
    kb = HybridKnowledge(Store(ROOT/"data/resolvedesk.sqlite3"), LocalEmbedding(ROOT/"data/models/multilingual-e5-small"))
    report = {"created_utc":datetime.now(timezone.utc).isoformat(), "dataset":"8 hand-labeled synthetic IT questions; no general quality claim",
              "embedding":kb.status()["embedding"], "results":evaluate(kb,cases,Principal("demo","learner"))}
    output = Path(args.output) if args.output else ROOT / "reports" / ("retrieval-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+".json")
    output.parent.mkdir(exist_ok=True)
    with output.open("x",encoding="utf-8") as f:
        json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({mode:{k:v for k,v in data.items() if k!='cases'} for mode,data in report["results"].items()},ensure_ascii=False))
    print("Report:",output)


if __name__ == "__main__":
    main()
