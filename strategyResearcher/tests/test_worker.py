import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from harness import PINE
from research_department.department import Department
from research_department.http_server import DepartmentServer
from research_department.worker import Worker


STAGES = [
    "meeting",
    "running",
    "passed_stage_1",
    "passed_stage_2",
    "passed_stage_3",
    "passed_all",
]


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.department = Department()
        self.server = DepartmentServer(self.department)
        self.worker = Worker(self.server.url)
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.server.close()
        self.directory.cleanup()

    def _file(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_loaded_file_is_accepted_and_a_second_script_is_not_sent(self):
        self.department.prepare(["meeting"], {})
        path = self._file("five.pine", PINE)
        accepted = self.worker.submit(str(path), "MNQ")
        self.assertEqual(accepted["job_id"], "job-1")
        self.assertFalse(accepted["refused"])
        with self.assertRaises(RuntimeError):
            self.worker.submit(str(path), "ES")
        posts = [call for call in self.worker.calls if call[0] == "POST"]
        self.assertEqual(posts, [("POST", "/scripts")])

    def test_an_empty_file_is_refused_and_the_next_file_can_load(self):
        empty = self._file("empty.pine", "   ")
        refused = self.worker.submit(str(empty), "ES")
        self.assertTrue(refused["refused"])
        self.assertIsNone(self.worker.job_id)
        self.department.prepare(["meeting"], {})
        accepted = self.worker.submit(str(self._file("five.pine", PINE)), "ES")
        self.assertEqual(accepted["job_id"], "job-1")

    def test_the_result_is_accepted_only_after_every_stage(self):
        script = "//@version=5\nstrategy(\"Passed\")\n"
        self.department.prepare(STAGES, {}, script)
        self.worker.submit(str(self._file("five.pine", PINE)), "ES")
        seen = []
        for _ in range(len(STAGES)):
            seen.append(self.worker.poll()["state"])
        self.assertEqual(seen, STAGES)
        result_calls = [call for call in self.worker.calls if call[1].endswith("/result")]
        self.assertEqual(result_calls, [("GET", "/jobs/job-1/result")])
        self.assertEqual(self.worker.calls[-1], ("GET", "/jobs/job-1/result"))
        self.assertEqual(self.worker.accepted_script, script)
        self.assertTrue(self.worker.finished)

    def test_a_stopped_job_is_finished_and_the_next_script_can_load(self):
        self.department.prepare(["stopped"], {}, best={"stage": 7})
        self.worker.submit(str(self._file("five.pine", PINE)), "NQ")
        body = self.worker.poll()
        self.assertEqual(body["state"], "stopped")
        self.assertEqual(self.worker.best, {"stage": 7})
        self.assertTrue(self.worker.finished)
        self.department.prepare(["meeting"], {})
        nxt = self.worker.submit(str(self._file("next.pine", PINE)), "NQ")
        self.assertEqual(nxt["job_id"], "job-2")

    def test_the_service_refuses_a_second_script_while_the_job_is_open(self):
        self.department.prepare(["meeting"], {})
        self.worker.submit(str(self._file("five.pine", PINE)), "GC")
        status, body = self.worker._send("POST", "/scripts", {"source": PINE, "instrument": "GC"})
        self.assertEqual(status, 409)
        self.assertTrue(body["refused"])
