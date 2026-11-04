import os
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import mailer


class _FakeSMTP:
    sent = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, *args):
        pass

    def send_message(self, message):
        _FakeSMTP.sent.append(message)
        return {}


class MailLabelTests(unittest.TestCase):
    def setUp(self):
        _FakeSMTP.sent = []

    def test_a_test_run_email_says_plainly_that_it_is_a_test(self):
        env = {"SMTP_PASSWORD": "not-a-real-password", "SMTP_USER": "desk@example.com", "REPORT_TO": "owner@example.com"}
        with mock.patch.dict(os.environ, env), mock.patch.object(mailer.smtplib, "SMTP", _FakeSMTP):
            os.environ.pop("FUTURES_MAIL_OFF", None)
            result = mailer._send_report("Strategy: Kept Run.", "Futures fund library: Kept Run")
        self.assertTrue(result["sent"])
        message = _FakeSMTP.sent[0]
        self.assertTrue(message["Subject"].startswith("[TEST RUN - not a real strategy]"))
        self.assertTrue(message.get_content().startswith("THIS IS A TEST EMAIL."))

    def test_mail_off_still_sends_nothing(self):
        env = {"SMTP_PASSWORD": "not-a-real-password", "SMTP_USER": "desk@example.com", "FUTURES_MAIL_OFF": "1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(mailer.smtplib, "SMTP", side_effect=AssertionError("sent")):
            result = mailer._send_report("body", "subject")
        self.assertFalse(result["sent"])

    def test_the_library_email_carries_the_script_with_the_run_inputs(self):
        from futuresfund import learn

        source = 'strategy("Kept Run")\nstop_dollars = input.int(250, "Stop ($)")\n'
        with mock.patch("futuresfund.library._measured_script", return_value=source):
            text = learn._mail_script(Path("kept_run.pine"), {"source_sha": "abc", "params": {"stop_dollars": 300}})
        self.assertIn("----- Pine script start -----", text)
        self.assertIn('strategy("Kept Run")', text)
        self.assertIn("300", text)


if __name__ == "__main__":
    unittest.main()
