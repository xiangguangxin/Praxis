"""Bounded scheduler worker; execution is injected and never runs arbitrary payload code."""
class Worker:
    def __init__(self,scheduler,execute):
        if not callable(execute): raise ValueError("execute callback required")
        self.scheduler,self.execute=scheduler,execute
    def run_once(self):
        job=self.scheduler.claim()
        if job is None: return {"status":"idle"}
        try:
            result=self.execute(job)
            if not isinstance(result,dict): raise TypeError("Job executor must return an object")
            if result.get("status")=="completed":
                self.scheduler.finish(job["id"],result)
                return {"status":"completed","job_id":job["id"],"result":result}
            self.scheduler.fail(job["id"],"Agent goal ended with status "+str(result.get("status","unknown")))
            return {"status":"failed","job_id":job["id"],"result":result}
        except Exception as exc:
            self.scheduler.fail(job["id"],type(exc).__name__+": "+str(exc)[:300])
            return {"status":"failed","job_id":job["id"],"error":type(exc).__name__}
    def run(self,*,max_jobs=1):
        if type(max_jobs) is not int or not 1 <= max_jobs <= 100: raise ValueError("max_jobs must be 1..100")
        results=[]
        for _ in range(max_jobs):
            item=self.run_once()
            if item["status"]=="idle": break
            results.append(item)
        return results
