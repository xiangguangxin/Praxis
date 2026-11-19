import tempfile
import threading
import time
import unittest
from pathlib import Path
from praxis_agent.planning import Task, TaskGraph, TaskOrchestrator, TaskLockManager, ResourceLockTimeout
from praxis_agent.planning.task_graph import TaskGraphError

class ParallelTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_independent_tasks_run_in_parallel_and_aggregate_results(self):
        barrier=threading.Barrier(2)
        started=[]
        def execute(task):
            started.append(task.id)
            barrier.wait(timeout=3)
            return {"artifact":task.id}
        graph=TaskGraph([Task("a","A"),Task("b","B")])
        runner=TaskOrchestrator(graph,execute,lambda task,result:{"passed":True,"evidence":{"artifact":result["artifact"]}},
                                self.root/"parallel.json",max_workers=2)
        result=runner.run()
        self.assertEqual(result["status"],"completed")
        self.assertEqual(set(started),{"a","b"})
        self.assertEqual(result["aggregate"]["counts"]["completed"],2)
        self.assertEqual(set(result["results"]),{"a","b"})

    def test_tasks_sharing_a_resource_are_serialized(self):
        lock=threading.Lock()
        active={"count":0,"peak":0}
        def execute(task):
            with lock:
                active["count"]+=1
                active["peak"]=max(active["peak"],active["count"])
            time.sleep(0.08)
            with lock: active["count"]-=1
            return {"ok":True}
        graph=TaskGraph([Task("a","A",(),(),("desktop:fixture",)),
                         Task("b","B",(),(),("desktop:fixture",))])
        result=TaskOrchestrator(graph,execute,lambda task,value:True,self.root/"same-resource.json",
                                max_workers=2).run()
        self.assertEqual(result["status"],"completed")
        self.assertEqual(active["peak"],1)

    def test_disjoint_resources_can_run_concurrently(self):
        barrier=threading.Barrier(2)
        def execute(task):
            barrier.wait(timeout=3)
            return {"ok":True}
        graph=TaskGraph([Task("a","A",(),(),("desktop:one",)),
                         Task("b","B",(),(),("desktop:two",))])
        result=TaskOrchestrator(graph,execute,lambda task,value:True,self.root/"different-resource.json",
                                max_workers=2).run()
        self.assertEqual(result["status"],"completed")

    def test_parallel_failure_blocks_only_dependents(self):
        def execute(task):
            if task.id=="bad": raise RuntimeError("injected")
            return {"ok":True}
        graph=TaskGraph([Task("bad","Bad"),Task("independent","Independent"),
                         Task("dependent","Dependent",("bad",))])
        result=TaskOrchestrator(graph,execute,lambda task,value:True,self.root/"failure.json",
                                max_workers=2,max_attempts=1).run()
        self.assertEqual(result["status"],"blocked")
        self.assertEqual(result["tasks"]["bad"],"failed")
        self.assertEqual(result["tasks"]["dependent"],"blocked")
        self.assertEqual(result["tasks"]["independent"],"completed")

    def test_parallel_checkpoint_resume_preserves_worker_limit(self):
        graph=TaskGraph([Task("a","A"),Task("b","B")])
        path=self.root/"resume.json"
        runner=TaskOrchestrator(graph,lambda task:{"ok":True},lambda task,value:True,path,max_workers=4)
        runner.status="paused"; runner._save()
        resumed=TaskOrchestrator.resume(path,lambda task:{"ok":True},lambda task,value:True)
        self.assertEqual(resumed.max_workers,4)
        self.assertEqual(resumed.run()["status"],"completed")

    def test_resource_lock_timeout_is_explicit(self):
        manager=TaskLockManager(self.root/"locks",timeout=0.1,poll_interval=0.01)
        with manager.acquire(["desktop:fixture"]):
            with self.assertRaises(ResourceLockTimeout):
                with manager.acquire(["desktop:fixture"]): pass

    def test_task_resource_names_round_trip_and_validate(self):
        graph=TaskGraph([Task("a","A",(),("done",),("desktop:fixture","output:/tmp"))])
        restored=TaskGraph.from_dict(graph.to_dict())
        self.assertEqual(restored.tasks["a"].resources,("desktop:fixture","output:/tmp"))
        with self.assertRaises(TaskGraphError):
            TaskGraph([Task("a","A",(),(),("same","same"))])
        with self.assertRaises(TaskGraphError):
            TaskGraph([Task("bad","Bad",(),(),([],))])
        with self.assertRaises(TaskGraphError):
            TaskGraph([Task("bad","Bad",([ "missing" ],))])

