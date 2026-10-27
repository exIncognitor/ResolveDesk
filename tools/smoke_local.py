"""Real localhost API + Flash smoke; bounded scenarios and no secrets in reports."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import httpx
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    if args.url != "http://127.0.0.1:8080":
        raise ValueError("Smoke tests are restricted to the local service")
    access = json.loads((ROOT/".codex-runtime/access.json").read_text(encoding="utf-8"))
    results = []
    with httpx.Client(base_url=args.url, timeout=10, headers={"Authorization":"Bearer "+access["learner"]["token"]}) as client:
        def request(method,path,body=None):
            r = client.request(method,path,json=body)
            r.raise_for_status()
            return r.json()
        def wait(run):
            until = time.monotonic()+100
            while time.monotonic()<until:
                current = request("GET","/runs/"+run["id"])
                if current["status"] not in {"queued","running"}:
                    return current
                time.sleep(0.4)
            raise TimeoutError("Run did not finish")
        def new(message):
            return wait(request("POST","/runs",{"message":message,"request_key":"smoke-"+uuid.uuid4().hex}))
        started = time.monotonic()
        knowledge = new("VPN 提示错误 691，我应该如何排查？请引用知识库。")
        results.append({"case":"grounded_answer","run_id":knowledge["id"],"status":knowledge["status"],
                        "pass":knowledge["status"] in {"completed","awaiting_input"} and "[C-" in knowledge["answer"],"answer":knowledge["answer"]})
        before = {t["id"] for t in request("GET","/tickets")}
        missing = new("我需要人工处理，请帮我创建工单，目前还没提供具体问题，请先问我需要哪些信息。")
        results.append({"case":"missing_fields_no_write","run_id":missing["id"],"status":missing["status"],
                        "pass":missing["status"] == "awaiting_input" and {t["id"] for t in request("GET","/tickets")}==before,
                        "answer":missing["answer"]})
        full_message = "这是本地演示测试，请为我准备工单：标题 VPN 连接持续超时；具体描述：Windows 11 上 VPN 客户端连接超时，普通网页正常，已重启客户端并切换网络仍未解决；类别 network；优先级 normal。先给我草稿，我会核对确认。"
        if missing["status"] in {"awaiting_input","completed"}:
            full = wait(request("POST","/runs/"+missing["id"]+"/reply",{"message":full_message,"request_key":"smoke-"+uuid.uuid4().hex}))
        else:
            full = new(full_message)
        no_write = {t["id"] for t in request("GET","/tickets")} == before
        ticket, idempotent = None, False
        if full["status"] == "awaiting_confirmation":
            ticket = request("POST","/runs/"+full["id"]+"/confirm",{"approval":full["approval"]})
            repeated = request("POST","/runs/"+full["id"]+"/confirm",{"approval":full["approval"]})
            idempotent = repeated["id"] == ticket["id"]
        results.append({"case":"confirmed_idempotent_ticket","run_id":full["id"],"status":full["status"],
                        "pass":no_write and ticket is not None and idempotent and ticket["fields"]["category"]=="network",
                        "ticket_id":ticket["id"] if ticket else None,"draft":full["draft"],"error":full["error"]})
        with httpx.Client(base_url=args.url,timeout=10,headers={"Authorization":"Bearer "+access["support"]["token"]}) as support:
            system = support.get("/system")
            system.raise_for_status()
            budget = system.json()["budget"]
        report = {"created_utc":datetime.now(timezone.utc).isoformat(),"model":"deepseek-flash",
                  "elapsed_seconds":round(time.monotonic()-started,2),"cases":results,"budget":budget,
                  "all_passed":all(c["pass"] for c in results)}
    output = ROOT/"reports"/("real-smoke-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+".json")
    output.parent.mkdir(exist_ok=True)
    with output.open("x",encoding="utf-8") as f:
        json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    print("Report:",output)
    if not report["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
