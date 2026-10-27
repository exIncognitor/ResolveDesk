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
            if "options" not in {r[1] for r in db.execute("PRAGMA table_info(eval_jobs)")}:
                db.execute("ALTER TABLE eval_jobs ADD COLUMN options TEXT NOT NULL DEFAULT '{}'")
            db.execute("CREATE TABLE IF NOT EXISTS quality_baselines(tenant TEXT,suite_id TEXT,job_id TEXT,report TEXT,created REAL,PRIMARY KEY(tenant,suite_id))")
            db.execute("UPDATE eval_jobs SET status='paused',error='process_interrupted' WHERE status IN ('queued','running')")

    def get(self,p,job_id):
        with self.store.db() as db:
            r = db.execute("SELECT * FROM eval_jobs WHERE id=? AND tenant=? AND owner=?",(job_id,p.tenant,p.user)).fetchone()
            if r is None:raise KeyError("Evaluation not found")
            return {"job_id":r["id"],"status":r["status"],"completed":r["completed"],"total":r["total"],
                    "result":json.loads(r["result"]) if r["result"] else None,"error":r["error"]}

    def submit(self,p,kind="retrieval",limit=6,use_judge=True):
        if p.role!="support":raise PermissionError()
        if kind not in {"retrieval","quality"} or type(limit) is not int or not 1 <= limit <= 6:
            raise ValueError("Invalid evaluation options")
        with self.store.db(True) as db:
            if db.execute("SELECT COUNT(*) FROM eval_jobs WHERE status IN ('running','queued')").fetchone()[0]>=2:
                raise Conflict("Evaluation queue full")
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO eval_jobs(id,tenant,owner,status,completed,total,result,error,created,options) VALUES(?,?,?,'queued',0,0,NULL,NULL,?,?)",
                       (job_id,p.tenant,p.user,time.time(),encode({"kind":kind,"limit":limit,"use_judge":use_judge})))
        self.schedule(p,job_id)
        return self.get(p,job_id)

    def schedule(self,p,job_id):
        if job_id in self.tasks:return
        task = asyncio.create_task(self.run(p,job_id))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _:self.tasks.pop(job_id,None))

    def resume(self,p,job_id):
        if p.role!="support":raise PermissionError()
        job = self.get(p,job_id)
        if job["status"] not in {"paused","failed"}:raise Conflict("Evaluation is not resumable")
        with self.store.db(True) as db:
            db.execute("UPDATE eval_jobs SET status='queued',error=NULL WHERE id=?",(job_id,))
        self.schedule(p,job_id)
        return self.get(p,job_id)

    def set_baseline(self,p,job_id):
        if p.role!="support":raise PermissionError()
        job = self.get(p,job_id)
        report = job["result"] or {}
        if job["status"]!="completed" or report.get("kind")!="active_service_quality" or not report.get("judge_enabled") or report.get("evaluation_errors") or report.get("passed")!=report.get("total") or report.get("intent",{}).get("accuracy")!=1:
            raise Conflict("仅完整通过且有有效 Judge 评分的质量报告可设为基线")
        with self.store.db(True) as db:
            db.execute("INSERT INTO quality_baselines VALUES(?,?,?,?,?) ON CONFLICT(tenant,suite_id) DO UPDATE SET job_id=excluded.job_id,report=excluded.report,created=excluded.created",(p.tenant,report["suite_id"],job_id,encode(report),time.time()))
        return {"saved":True,"suite_id":report["suite_id"],"job_id":job_id}

    def compare(self,p,report):
        with self.store.db() as db:
            row = db.execute("SELECT job_id,report FROM quality_baselines WHERE tenant=? AND suite_id=?",(p.tenant,report["suite_id"])).fetchone()
        if row is None:return report
        baseline = json.loads(row["report"])
        report["baseline_job_id"] = row["job_id"]
        report["regression_note"] = "与人工选定、同知识版本和评分配置的基线比较；相对下降超过5%触发回归。"
        metrics = {**report["avg_scores"],"pass_rate":report["pass_rate"]}
        previous = {**baseline["avg_scores"],"pass_rate":baseline["pass_rate"]}
        report["regressions"] = [{"test_id":"metric:"+key,"detail":"相对基线下降超过5%","previous":old,"current":metrics.get(key)} for key,old in previous.items() if old>0 and metrics.get(key,0)<old*.95]
        if report["evaluation_errors"]:report["regressions"].append({"test_id":"evaluation-errors","detail":"存在执行或评分错误，不能认定质量无退化"})
        return report

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
                options = json.loads(db.execute("SELECT options FROM eval_jobs WHERE id=?",(job_id,)).fetchone()[0])
                extra = [dict(r) for r in db.execute("SELECT id,question AS query,document,section FROM regression_cases WHERE tenant=?",(p.tenant,))]
            if options.get("kind")=="quality":
                from resolvedesk.quality import QualityRunner
                def progress(completed,total):
                    with self.store.db(True) as db:db.execute("UPDATE eval_jobs SET completed=?,total=? WHERE id=?",(completed,total,job_id))
                # A resumed job gets a new sandbox; previous evidence is retained, never overwritten.
                attempt_id = job_id+"-"+uuid.uuid4().hex[:8]
                report = self.compare(p,await QualityRunner(self.store,self.knowledge).run(p,attempt_id,options["limit"],options["use_judge"],progress))
                with self.store.db(True) as db:
                    db.execute("UPDATE eval_jobs SET status='completed',result=? WHERE id=?",(encode(report),job_id))
                return
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
