"""Persistent, scoped retrieval evaluations and reviewed feedback regressions."""
import asyncio
import json
from pathlib import Path
import time
import uuid

from resolvedesk.store import encode, Conflict


class EvaluationService:
    def __init__(self,store,knowledge):
        self.store,self.knowledge,self.tasks = store,knowledge,{}
        with store.db(True) as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS eval_jobs(id TEXT PRIMARY KEY,tenant TEXT,owner TEXT,status TEXT,completed INTEGER,total INTEGER,result TEXT,error TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS regression_cases(id TEXT PRIMARY KEY,tenant TEXT,source_run TEXT,question TEXT,document TEXT,section TEXT,UNIQUE(tenant,source_run));""")
            db.execute("UPDATE eval_jobs SET status='paused',error='process_interrupted' WHERE status IN ('queued','running')")

    def get(self,p,job_id):
        with self.store.db() as db:
            r = db.execute("SELECT * FROM eval_jobs WHERE id=? AND tenant=? AND owner=?",(job_id,p.tenant,p.user)).fetchone()
            if r is None:raise KeyError("Evaluation not found")
            return {"job_id":r["id"],"status":r["status"],"completed":r["completed"],"total":r["total"],
                    "result":json.loads(r["result"]) if r["result"] else None,"error":r["error"]}

    def submit(self,p):
        if p.role!="support":raise PermissionError()
        with self.store.db(True) as db:
            if db.execute("SELECT COUNT(*) FROM eval_jobs WHERE status IN ('running','queued')").fetchone()[0]>=2:
                raise Conflict("Evaluation queue full")
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO eval_jobs VALUES(?,?,?,'queued',0,0,NULL,NULL,?)",(job_id,p.tenant,p.user,time.time()))
        self.schedule(p,job_id)
        return self.get(p,job_id)

    def schedule(self,p,job_id):
        if job_id in self.tasks:return
        task = asyncio.create_task(self.run(p,job_id))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _:self.tasks.pop(job_id,None))

    def resume(self,p,job_id):
        job = self.get(p,job_id)
        if job["status"] not in {"paused","failed"}:raise Conflict("Evaluation is not resumable")
        with self.store.db(True) as db:
            db.execute("UPDATE eval_jobs SET status='queued',error=NULL WHERE id=?",(job_id,))
        self.schedule(p,job_id)
        return self.get(p,job_id)

    def promote(self,p,run_id,document,section):
        if p.role!="support":raise PermissionError()
        with self.store.db(True) as db:
            row = db.execute("SELECT r.message FROM runs r JOIN feedback f ON r.id=f.run_id WHERE r.id=? AND f.tenant=? AND f.rating='bad'",(run_id,p.tenant)).fetchone()
            if row is None:raise KeyError("Negative feedback not found")
            if not db.execute("SELECT 1 FROM chunks WHERE tenant=? AND document_id=? AND section LIKE ? AND (visibility='tenant' OR owner=?)",(p.tenant,document,"%"+section+"%",p.user)).fetchone():
                raise ValueError("Expected reference is unavailable")
            db.execute("INSERT INTO regression_cases VALUES(?,?,?,?,?,?) ON CONFLICT(tenant,source_run) DO UPDATE SET question=excluded.question,document=excluded.document,section=excluded.section",(uuid.uuid4().hex,p.tenant,run_id,row["message"],document,section))
        return {"saved":True,"source_run":run_id}

    async def run(self,p,job_id):
        try:
            with self.store.db(True) as db:
                updated = db.execute("UPDATE eval_jobs SET status='running' WHERE id=? AND status='queued'",(job_id,))
                if not updated.rowcount:return
                extra = [dict(r) for r in db.execute("SELECT id,question AS query,document,section FROM regression_cases WHERE tenant=?",(p.tenant,))]
            default = json.loads((Path(__file__).resolve().parents[1]/"evaluation/retrieval_cases.json").read_text(encoding="utf-8")) if p.tenant=="demo" else []
            cases = default+extra
            details = []
            for index,case in enumerate(cases):
                result = await asyncio.to_thread(self.knowledge.search,p,case["query"],5,"hybrid")
                rank = next((i for i,item in enumerate(result["items"],1) if item["document_id"]==case["document"] and case["section"] in item["section"]),None)
                details.append({"test_id":case["id"],"question":case["query"],"rank":rank,"passed":rank is not None,"degraded":result["degraded"],"metadata":{}})
                with self.store.db(True) as db:
                    db.execute("UPDATE eval_jobs SET completed=?,total=? WHERE id=?",(index+1,len(cases),job_id))
            n = len(cases)
            passed = sum(d["passed"] for d in details)
            report = {"status":"complete","kind":"retrieval_and_reviewed_regression","total":n,"valid_total":n,"passed":passed,"pass_rate":passed/n if n else 0,
                      "evaluation_errors":0,"avg_scores":{"recall_at_5":passed/n if n else 0,"hit_at_1":sum(d["rank"]==1 for d in details)/n if n else 0,
                      "mrr_at_5":sum(1/d["rank"] for d in details if d["rank"])/n if n else 0},"results":details,
                      "regressions":[d for d in details if not d["passed"]],"regression_note":"固定示例题与人工审核的负反馈案例；不包含 LLM Judge 或生产质量评分。",
                      "recommendations":["小样本不能证明生产效果。","负反馈需人工指定期望文档和章节后才进入回归集。"]}
            with self.store.db(True) as db:
                db.execute("UPDATE eval_jobs SET status='completed',result=? WHERE id=?",(encode(report),job_id))
        except asyncio.CancelledError:
            with self.store.db(True) as db:
                db.execute("UPDATE eval_jobs SET status='paused',error='interrupted' WHERE id=?",(job_id,))
            raise
        except Exception as exc:
            with self.store.db(True) as db:
                db.execute("UPDATE eval_jobs SET status='failed',error=? WHERE id=?",(type(exc).__name__,job_id))

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
