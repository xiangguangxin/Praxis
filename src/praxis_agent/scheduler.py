"""Durable local SQLite job queue; claims due jobs atomically."""
import json, sqlite3, time, uuid
class Scheduler:
    def __init__(self,database):
        self.database=str(database)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, goal TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, run_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,run_at)")
    def _connect(self):
        db=sqlite3.connect(self.database,timeout=10); db.row_factory=sqlite3.Row; return db
    def enqueue(self,goal,payload=None,*,run_at=None):
        if not isinstance(goal,str) or not goal.strip(): raise ValueError("goal required")
        if payload is not None and not isinstance(payload,dict): raise ValueError("payload must be object")
        now=time.time(); jid=str(uuid.uuid4())
        with self._connect() as db:
            db.execute("INSERT INTO jobs(id,goal,payload,status,run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(jid,goal,json.dumps(payload or {},ensure_ascii=False),"queued",now if run_at is None else float(run_at),now,now))
        return self.get(jid)
    def get(self,jid):
        with self._connect() as db: row=db.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone()
        if row is None: raise KeyError(jid)
        item=dict(row); item["payload"]=json.loads(item["payload"])
        if item["result"] is not None: item["result"]=json.loads(item["result"])
        return item
    def claim(self,*,now=None):
        now=time.time() if now is None else now
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT * FROM jobs WHERE status='queued' AND run_at<=? ORDER BY run_at,created_at LIMIT 1",(now,)).fetchone()
            if row is None: return None
            db.execute("UPDATE jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=? AND status='queued'",(now,row["id"]))
            row=db.execute("SELECT * FROM jobs WHERE id=?",(row["id"],)).fetchone()
            item=dict(row); item["payload"]=json.loads(item["payload"]); return item
    def _transition(self,jid,status,**fields):
        now=time.time(); allowed={"completed":{"running"},"failed":{"running"},"queued":{"running"}}
        with self._connect() as db:
            row=db.execute("SELECT status FROM jobs WHERE id=?",(jid,)).fetchone()
            if row is None: raise KeyError(jid)
            if row["status"] not in allowed[status]: raise ValueError("Invalid job transition")
            updates={"status":status,"updated_at":now,**fields}; sql=", ".join(k+"=?" for k in updates)
            db.execute("UPDATE jobs SET "+sql+" WHERE id=?",(*updates.values(),jid))
    def finish(self,jid,result):
        if not isinstance(result,dict): raise ValueError("result must be object")
        self._transition(jid,"completed",result=json.dumps(result,ensure_ascii=False),error=None)
    def fail(self,jid,error):
        if not isinstance(error,str) or not error.strip(): raise ValueError("error required")
        self._transition(jid,"failed",error=error[:500])
    def retry(self,jid,*,run_at=None):
        self._transition(jid,"queued",run_at=time.time() if run_at is None else float(run_at),error=None)
    def list(self,*,status=None,limit=100):
        if type(limit) is not int or not 1<=limit<=1000: raise ValueError("limit must be 1..1000")
        with self._connect() as db:
            rows=db.execute("SELECT * FROM jobs "+("WHERE status=? " if status else "")+"ORDER BY created_at DESC LIMIT ?",((status,limit) if status else (limit,))).fetchall()
        output=[]
        for row in rows:
            item=dict(row); item["payload"]=json.loads(item["payload"])
            if item["result"] is not None: item["result"]=json.loads(item["result"])
            output.append(item)
        return output
