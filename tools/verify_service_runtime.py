"""No paid model calls: verify real live service, retrieval corpus and budget stability."""
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    base=sys.argv[1] if len(sys.argv)>1 else "http://127.0.0.1:8080"
    if base not in {"http://127.0.0.1:8080","http://127.0.0.1:18080"}: raise ValueError("Local verification only")
    access=json.loads((ROOT/".codex-runtime/access.json").read_text(encoding="utf-8"))
    with httpx.Client(base_url=base,timeout=30,trust_env=False) as client:
        def request(method,path,body=None,actor="support"):
            result=client.request(method,path,json=body,headers={"Authorization":"Bearer "+access[actor]["token"]})
            result.raise_for_status();return result.json()
        health=request("GET","/health")
        assert health["database"]=="postgresql" and health["coordination"]=="redis" and health["vector_backend"]=="chroma"
        before=request("GET","/system")["budget"]
        corpus=json.loads((ROOT/"evaluation/retrieval_cases.json").read_text(encoding="utf-8"))
        results=[]
        for mode in ("bm25","vector","hybrid"):
            for case in corpus:
                result=request("POST","/knowledge/search",{"query":case["query"],"mode":mode,"top_k":5},"learner")
                passed=any(item["document_id"]==case["document"] and case["section"] in item["section"] for item in result["items"])
                results.append({"id":case["id"],"mode":mode,"passed":passed,"degraded":result["degraded"]})
        after=request("GET","/system")["budget"]
        assert before==after,"Verification must not spend model budget"
        report={"health":health,"paid_budget_changed":False,"budget":after,"retrieval":results,
            "passed":sum(r["passed"] and not r["degraded"] for r in results),"total":len(results),"mcp":request("GET","/mcp")}
        path=ROOT/"reports"/("services-runtime-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+".json")
        with path.open("x",encoding="utf-8") as dest: json.dump(report,dest,ensure_ascii=False,indent=2)
        print(json.dumps({"passed":report["passed"],"total":report["total"],"paid_budget_changed":False,"evidence":str(path.relative_to(ROOT))},ensure_ascii=False))
        if report["passed"]!=report["total"]: raise SystemExit(1)


if __name__=="__main__": main()
