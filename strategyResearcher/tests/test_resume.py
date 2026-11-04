import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from test_learning import fake_ask, good_days, pine

from research_department.department import Job
from research_department.mail import MailService
from research_department.meeting import Proposal
from research_department.service import ResearchDesk
from research_department.stages import Day, StageBook


def _rows(days: list[Day]) -> list[dict]:
    return [
        {"profit": day.profit, "worst_loss": day.worst_loss, "wins": day.wins, "trades": day.trades, "losses": day.losses}
        for day in days
    ]


class RewardScaleTests(unittest.TestCase):
    def test_a_higher_win_rate_moves_the_desk_toward_the_goal(self):
        desk = ResearchDesk()
        desk.settings["success_rate"] = 0.4
        desk._record_reward({"win_percentage": 0.25, "profit": 100})
        self.assertEqual(desk.reward["direction"], "waiting")
        desk._record_reward({"win_percentage": 0.33, "profit": 140})
        self.assertEqual(desk.reward["direction"], "toward")
        self.assertFalse(desk.reward["at_goal"])
        desk._record_reward({"win_percentage": 0.21, "profit": -30})
        self.assertEqual(desk.reward["direction"], "away")
        self.assertFalse(desk.reward["profitable"])
        self.assertEqual(desk.get_desk()[1]["reward"]["direction"], "away")
        desk._record_reward({"win_percentage": 0.44, "profit": 90})
        self.assertEqual(desk.reward["direction"], "toward")
        self.assertEqual(desk.reward["profit_direction"], "toward")
        self.assertEqual(desk.reward["profit_goal"], 1750)
        self.assertEqual(desk.reward["profit_total"], 3500)
        self.assertTrue(desk.reward["at_goal"])
        self.assertFalse(desk.reward["profit_at_goal"])
        self.assertTrue(desk.reward["profitable"])
        desk._record_reward({"win_percentage": 0.46, "profit": 3600})
        self.assertEqual(desk.reward["profit_direction"], "toward")
        self.assertTrue(desk.reward["profit_at_goal"])


class ResumeTests(unittest.TestCase):
    def test_a_shutdown_does_not_repeat_a_finished_backtest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            learning = root / "learning"
            learning.mkdir()
            (learning / "one_ES.pine").write_text(pine("One"), encoding="utf-8")
            library = root / "data" / "strategies.json"
            library.parent.mkdir()
            days = good_days(7)
            summary = {
                "success_rate": 1.0,
                "win_percentage": 1.0,
                "worst_day": 500,
                "trade_count": 7,
                "quiet_sessions": 0,
                "instrument": "ES",
            }
            run = {
                "engine_id": 1,
                "timeframe": "2m",
                "script": "One",
                "instrument": "ES",
                "change": "watch_bars 5",
                "days": 7,
                "success_rate": 1.0,
                "win_percentage": 1.0,
                "worst_day": 500,
                "trade_count": 7,
                "quiet_sessions": 0,
                "attempt": 1,
            }
            progress = {
                "file": "one_ES.pine",
                "source": pine("One"),
                "title": "One",
                "stage_index": 0,
                "attempt": 1,
                "closed": [["watch_bars", "up"]],
                "order": {
                    "variable": "watch_bars",
                    "value": 5,
                    "direction": "up",
                    "consistent_day": True,
                    "higher_win_rate": True,
                    "justification": "A longer watch keeps the $500 day and raises the win percentage.",
                },
                "finished": [{
                    "engine_id": 1,
                    "instrument": "ES",
                    "days": _rows(days),
                    "summary": summary,
                    "run": run,
                }],
            }
            (library.parent / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
            calls = []

            def backtest(_source, instrument, timeframe, count):
                calls.append((instrument, timeframe))
                return good_days(count)

            desk = ResearchDesk(folder=learning, library_path=library)
            self.assertTrue(desk.has_backtest())
            self.assertEqual(desk._file_index, 0)
            desk.jobs["job-1"] = Job("job-1", pine("One"), "ES", ["meeting"])
            desk.shown = "job-1"
            desk.current_file = "one_ES.pine"
            desk.backtest = backtest
            saved = desk._saved_attempt()
            desk._run_engines(
                "job-1",
                saved["source"],
                "ES",
                "One",
                StageBook(success_rate=0.4),
                Proposal(**saved["order"]),
                0.4,
                prior=saved["finished"],
                closed={("watch_bars", "up")},
            )
            self.assertNotIn(("ES", "2m"), calls)
            self.assertEqual(len(calls), 8)
            kept = json.loads((library.parent / "progress.json").read_text(encoding="utf-8"))
            self.assertEqual(len(kept["finished"]), 9)

    def test_a_table_row_is_emailed_with_the_application_url(self):
        sent = []

        def transport(message):
            sent.append(message)
            return {"sent": True, "to": message["to"], "reason": "sent"}

        desk = ResearchDesk()
        desk.mail = MailService(transport=transport, phone=lambda: "https://research.example/app")
        desk.ask = fake_ask
        desk.backtest = lambda _source, _instrument, _timeframe, count: good_days(count)
        desk.jobs["job-1"] = Job("job-1", pine("One"), "ES", ["meeting"])
        desk.shown = "job-1"
        desk.current_file = "one_ES.pine"
        desk._research("job-1")
        bodies = [message["body"] for message in sent]
        self.assertTrue(any("Added to the 7 days table." in body for body in bodies))
        self.assertTrue(any("Added to the scripts table." in body for body in bodies))
        self.assertTrue(all("https://research.example/app" in body for body in bodies))
        self.assertTrue(all("Open the application:" in body for body in bodies))
        self.assertTrue(any("Profit: $3,500" in body for body in bodies))
        self.assertTrue(any("Profit: $182,500" in body for body in bodies))
        self.assertTrue(all(message["to"] == "thefutureoffuturestrading@gmail.com" for message in sent))
