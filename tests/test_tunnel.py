"""The phone address: emailed once, and again when a new one replaces an expired one."""

from __future__ import annotations

import unittest
from unittest import mock


class AddressTests(unittest.TestCase):
    def test_the_address_is_read_from_the_tunnel_log(self):
        from futuresfund.tunnel import address_from

        log = "Requesting new quick Tunnel\nINF https://old-name.trycloudflare.com\nINF https://new-name.trycloudflare.com\n"
        self.assertEqual(address_from(log), "https://new-name.trycloudflare.com")
        self.assertEqual(address_from("no address yet"), "")

    def test_a_new_address_is_emailed_once_and_a_replacement_is_emailed_again(self):
        from futuresfund import tunnel

        sent = []
        state = {}

        def send(body, subject):
            sent.append((subject, body))
            return {"sent": True}

        with mock.patch.object(tunnel, "_load", lambda: dict(state)), \
                mock.patch.object(tunnel, "_save", lambda item: state.update(item)), \
                mock.patch.object(tunnel, "alert_link_ok", lambda url: True), \
                mock.patch.object(tunnel, "_announce", lambda board, url, renewed: send(
                    f"View the desk on your phone: {url}",
                    "Futures desk address changed" if renewed else "Futures desk address",
                )):
            self.assertTrue(tunnel.note_url("https://first.trycloudflare.com", pid=10))
            self.assertFalse(tunnel.note_url("https://first.trycloudflare.com", pid=10))
            self.assertTrue(tunnel.note_url("https://second.trycloudflare.com", pid=11))
        self.assertEqual([item[0] for item in sent], ["Futures desk address", "Futures desk address changed"])
        self.assertIn("https://second.trycloudflare.com", sent[-1][1])
        self.assertEqual(state["emailed"], "https://second.trycloudflare.com")
        self.assertGreater(state["opened"], 0)

    def test_an_address_that_does_not_answer_is_not_emailed(self):
        from futuresfund import tunnel

        state = {}
        sent = []
        with mock.patch.object(tunnel, "_load", lambda: dict(state)), \
                mock.patch.object(tunnel, "_save", lambda item: state.update(item)), \
                mock.patch.object(tunnel, "alert_link_ok", lambda url: False), \
                mock.patch.object(tunnel, "_announce", lambda board, url, renewed: sent.append(url)), \
                mock.patch.object(tunnel, "_complain", lambda board, text: None):
            self.assertTrue(tunnel.note_url("https://silent.trycloudflare.com", pid=12))
        self.assertEqual(sent, [])
        self.assertNotIn("emailed", state)
        self.assertEqual(state["url"], "https://silent.trycloudflare.com")

    def test_an_address_is_replaced_at_530pm_new_york(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from futuresfund.tunnel import expired, issue_due

        et = ZoneInfo("America/New_York")
        opened = datetime(2026, 10, 13, 5, 13, tzinfo=et).timestamp()
        before = datetime(2026, 10, 13, 17, 29, tzinfo=et).timestamp()
        at = datetime(2026, 10, 13, 17, 30, tzinfo=et).timestamp()
        after_close = datetime(2026, 10, 13, 18, 0, tzinfo=et).timestamp()
        state = {"url": "https://desk.trycloudflare.com", "opened": opened}
        self.assertFalse(expired(state, before))
        self.assertFalse(issue_due(state, before))
        self.assertTrue(expired(state, at))
        self.assertTrue(issue_due(state, at))
        self.assertTrue(expired(state, after_close))
        self.assertFalse(issue_due(state, after_close))
        issued = datetime(2026, 10, 13, 17, 30, tzinfo=et).timestamp()
        morning = datetime(2026, 10, 14, 9, 0, tzinfo=et).timestamp()
        nxt = datetime(2026, 10, 14, 17, 30, tzinfo=et).timestamp()
        held = {"url": "https://desk.trycloudflare.com", "opened": issued}
        self.assertFalse(expired(held, morning))
        self.assertFalse(issue_due(held, morning))
        five_am = datetime(2026, 10, 14, 5, 12, tzinfo=et).timestamp()
        self.assertFalse(expired(held, five_am))
        self.assertFalse(issue_due(held, five_am))
        self.assertTrue(expired(held, nxt))
        self.assertTrue(expired({"url": "https://desk.trycloudflare.com"}, at))
        self.assertFalse(expired({}, at))

    def test_a_dead_address_is_not_kept_by_another_tunnel_process(self):
        from futuresfund import tunnel

        state = {
            "url": "https://old.trycloudflare.com",
            "opened": 1_700_000_000,
            "pid": 111,
            "emailed": "https://old.trycloudflare.com",
        }
        stopped = []
        with mock.patch.object(tunnel, "_load", lambda: dict(state)), \
                mock.patch.object(tunnel, "_alive", lambda pid: False), \
                mock.patch.object(tunnel, "_other_cloudflared", lambda: 222), \
                mock.patch.object(tunnel, "_stop", lambda pid: stopped.append(pid)), \
                mock.patch.object(tunnel, "issue_due", lambda state=None, now=None: False):
            tunnel._dead_checks = 0
            self.assertTrue(tunnel._holding(None))
            self.assertTrue(tunnel._holding(None))
            self.assertFalse(tunnel._holding(None))
        self.assertEqual(stopped, [])

    def test_a_report_carries_the_phone_address(self):
        from email.message import EmailMessage

        from futuresfund import mailer

        captured = {}

        class FakeSMTP:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def ehlo(self):
                return None

            def starttls(self):
                return None

            def login(self, user, password):
                return None

            def send_message(self, message: EmailMessage):
                captured["body"] = message.get_content()
                return {}

        with mock.patch("futuresfund.tunnel.current_url", return_value="https://desk.trycloudflare.com"), \
                mock.patch.object(mailer, "_smtp_settings", return_value=("smtp.example", 587, "user", "secret", "from@example.com")), \
                mock.patch.object(mailer, "report_recipients", return_value=["thefutureoffuturestrading@gmail.com"]), \
                mock.patch("smtplib.SMTP", return_value=FakeSMTP()):
            result = mailer._send_report("Profit 100.", "Futures desk report")
        self.assertTrue(result["sent"])
        self.assertIn("Profit 100.", captured["body"])
        self.assertIn("View the desk on your phone: https://desk.trycloudflare.com", captured["body"])
        self.assertEqual(captured["body"].count("https://desk.trycloudflare.com"), 1)

    def test_a_report_that_already_names_the_address_is_not_repeated(self):
        from futuresfund.mailer import _with_phone

        body = "View the desk on your phone: https://desk.trycloudflare.com"
        with mock.patch("futuresfund.tunnel.current_url", return_value="https://desk.trycloudflare.com"):
            self.assertEqual(_with_phone(body), body)

    def test_the_portfolio_manager_gives_the_phone_address(self):
        from futuresfund.desk_chat import answer

        with mock.patch("futuresfund.tunnel.current_url", return_value="https://desk.trycloudflare.com"):
            text = answer(None, "Portfolio Manager", "what is the web address for my phone?")
        self.assertIn("https://desk.trycloudflare.com", text)
        self.assertIn("expires", text)


if __name__ == "__main__":
    unittest.main()
