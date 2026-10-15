import contextlib
import copy
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from send_history import SendHistory, forwarding_lock, mailbox_scope


MAILBOX = {"host": "https://mail.example.test", "email": "soc@example.test"}
SCOPE = mailbox_scope(MAILBOX, "production")
CONFIG = {
    "folders": [{"folder_id": "10"}],
    "forward": {"to": ["customer@example.test"], "limit": 2},
    "test_forward": {"to": ["test@example.test"]},
    "error_to": ["ops@example.test"],
}


def message(source_id, unread=False):
    return SimpleNamespace(
        id=str(source_id), subject=f"Fwd: Alert {source_id}", unread=unread,
        body_text="Alert body", body_html="<p>Alert body</p>", attachments=(),
    )


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.config = SimpleNamespace(email=MAILBOX["email"])
        self.searches = []
        self.attempts = []
        self.fetch_error = None
        self.send_error = None
        self.receipt = "receipt-1"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def search_messages(self, *, query, folder_id, limit, offset, sort_by):
        if query != "" or sort_by != "dateAsc":
            raise AssertionError("Search must include both read and unread messages")
        self.searches.append((folder_id, offset))
        return SimpleNamespace(
            messages=self.messages[offset:offset + limit],
            more=offset + limit < len(self.messages),
        )

    def get_message(self, source_id):
        if self.fetch_error:
            raise self.fetch_error
        self.current_id = source_id
        return next(item for item in self.messages if item.id == source_id)

    def list_signatures(self):
        return ()

    def send_message(self, **kwargs):
        self.attempts.append((self.current_id, kwargs))
        if self.send_error:
            raise self.send_error
        return SimpleNamespace(message_id=self.receipt)

    def mark_read(self, *args, **kwargs):
        raise AssertionError("Forwarding must not modify read flags")


class ForwardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "forwarding_state.sqlite3"
        self.client = FakeClient([message("1")])
        self.cfg = copy.deepcopy(CONFIG)

    def run_job(self, **mode):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(main, "ROOT", self.root))
            stack.enter_context(patch.object(main, "load_env"))
            stack.enter_context(patch.object(main, "load_config", return_value=self.cfg))
            stack.enter_context(patch.object(main, "_zimbra_config_from_env", return_value=MAILBOX))
            stack.enter_context(patch.object(main, "ZimbraClient", return_value=self.client))
            stack.enter_context(patch.object(main, "prepare_send_bodies", return_value=("body", "<p>body</p>")))
            self.notify = stack.enter_context(patch.object(main, "notify_soc_on_error", return_value=True))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            main.main(**mode)

    def state(self, source_id, mode="production"):
        with SendHistory(self.db, mailbox_scope(MAILBOX, mode)) as history:
            return history.status(source_id)

    def test_read_message_is_sent_once_and_read_toggle_does_not_resend(self):
        self.run_job()
        self.assertFalse(self.client.messages[0].unread)
        self.assertEqual(self.state("1"), "sent")
        self.client.messages[0].unread = True
        self.run_job()
        self.assertTrue(self.client.messages[0].unread)
        self.assertEqual(len(self.client.attempts), 1)

    def test_unread_message_remains_unread_after_sending(self):
        self.client.messages[0].unread = True
        self.run_job()
        self.assertTrue(self.client.messages[0].unread)
        self.assertEqual(len(self.client.attempts), 1)

    def test_pagination_reaches_unsent_mail_beyond_a_page_of_sent_mail(self):
        self.client.messages = [message(n) for n in range(1, 5)]
        with SendHistory(self.db, SCOPE) as history:
            for source_id in ("1", "2"):
                history.begin_send(source_id)
                history.confirm_sent(source_id, "old-receipt")
        self.run_job()
        self.assertEqual([n for n, _ in self.client.attempts], ["3", "4"])
        self.assertEqual(self.client.searches, [("10", 0), ("10", 2)])

    def test_send_limit_applies_to_new_sends_and_remaining_mail_is_processed_next_run(self):
        self.client.messages = [message(n) for n in range(1, 6)]
        self.run_job()
        self.assertEqual(len(self.client.attempts), 2)
        self.run_job()
        self.run_job()
        self.assertEqual([n for n, _ in self.client.attempts], ["1", "2", "3", "4", "5"])

    def test_duplicate_source_in_multiple_folders_is_not_sent_twice(self):
        self.cfg["folders"].append({"folder_id": "11"})
        self.run_job()
        self.assertEqual(len(self.client.attempts), 1)

    def test_fetch_failure_is_not_recorded_and_can_retry(self):
        self.client.fetch_error = OSError("temporary fetch failure")
        with self.assertRaises(SystemExit):
            self.run_job()
        self.assertIsNone(self.state("1"))
        self.assertEqual(len(self.client.attempts), 0)
        self.client.fetch_error = None
        self.run_job()
        self.assertEqual(self.state("1"), "sent")

    def test_network_timeout_leaves_pending_and_blocks_automatic_resend(self):
        self.client.send_error = TimeoutError("send outcome unknown")
        with self.assertRaises(SystemExit):
            self.run_job()
        self.assertEqual(self.state("1"), "pending")
        self.client.send_error = None
        with self.assertRaises(SystemExit):
            self.run_job()
        self.assertEqual(len(self.client.attempts), 1)
        self.notify.assert_called_once()
        self.assertIn("unconfirmed", self.notify.call_args.kwargs["payload"]["failures"][0]["error"])

    def test_receipt_write_failure_does_not_repeat_an_accepted_send(self):
        with patch.object(SendHistory, "confirm_sent", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(SystemExit):
                self.run_job()
        self.assertEqual(self.state("1"), "pending")
        with self.assertRaises(SystemExit):
            self.run_job()
        self.assertEqual(len(self.client.attempts), 1)

    def test_missing_send_receipt_is_not_treated_as_confirmed(self):
        self.client.receipt = ""
        with self.assertRaises(SystemExit):
            self.run_job()
        self.assertEqual(self.state("1"), "pending")

    def test_pending_record_must_be_durable_before_network_send(self):
        original_send = self.client.send_message

        def check_pending(**kwargs):
            self.assertEqual(self.state("1"), "pending")
            return original_send(**kwargs)

        with patch.object(self.client, "send_message", side_effect=check_pending):
            self.run_job()

    def test_failed_pending_write_prevents_network_send(self):
        with patch.object(SendHistory, "begin_send", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(SystemExit):
                self.run_job()
        self.assertEqual(len(self.client.attempts), 0)

    def test_test_send_does_not_suppress_production_send(self):
        self.run_job(test=True)
        self.assertEqual(self.state("1", "test"), "sent")
        self.assertIsNone(self.state("1"))
        self.run_job()
        self.assertEqual([args["to"] for _, args in self.client.attempts],
                         [["test@example.test"], ["customer@example.test"]])

    def test_legacy_dry_run_sends_only_one_and_uses_separate_history(self):
        self.client.messages = [message(n) for n in range(1, 4)]
        self.cfg["folders"].append({"folder_id": "11"})
        self.run_job(dry_run=True)
        self.assertEqual(len(self.client.attempts), 1)
        self.assertEqual(self.state("1", "dry-run"), "sent")
        self.assertIsNone(self.state("1"))

    def test_content_skip_does_not_claim_a_send_or_starve_later_messages(self):
        self.client.messages = [message(n) for n in range(1, 4)]
        self.cfg["forward"]["limit"] = 1
        with patch.object(main, "_signature_parts", side_effect=[main.SkipForward("ignore"), ("", "")]):
            self.run_job()
        self.assertEqual([n for n, _ in self.client.attempts], ["2"])
        self.assertIsNone(self.state("1"))

    def test_overlapping_job_exits_before_mailbox_login(self):
        with forwarding_lock(self.root / ".forwarding.lock") as acquired:
            self.assertTrue(acquired)
            self.run_job()
        self.assertEqual(self.client.searches, [])
        self.assertFalse(self.db.exists())

    def test_migration_prevents_resending_confirmed_historical_mail(self):
        (self.root / "error.log").write_text("OK  id=1 subject='old'\nTEST OK id=2\n")
        self.client.messages = [message("1"), message("2")]
        self.run_job()
        self.assertEqual([n for n, _ in self.client.attempts], ["2"])

    def test_error_notice_requires_checking_sent_before_a_manual_retry(self):
        text, html = main._build_error_alert_bodies({})
        self.assertIn("Check Sent before manually sending", text)
        self.assertIn("Check Sent before manually sending", html)
        self.assertNotIn("remain unread", text)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "history.sqlite3"

    def test_legacy_import_only_accepts_production_successes_and_is_idempotent(self):
        log = self.root / "error.log"
        log.write_text("OK id=1 subject='a'\nTEST OK id=2\nDRY-RUN OK id=3\nFAIL id=4\nOK  id=1\nOK id=5\n")
        with SendHistory(self.db, SCOPE) as history:
            self.assertEqual(history.import_legacy_successes(log), 2)
            self.assertEqual(history.status("1"), "sent")
            self.assertEqual(history.status("5"), "sent")
            for source_id in ("2", "3", "4"):
                self.assertIsNone(history.status(source_id))
            log.write_text("OK id=6\n")
            self.assertEqual(history.import_legacy_successes(log), 0)
            self.assertIsNone(history.status("6"))

    def test_incomplete_send_survives_restart_and_mailboxes_are_isolated(self):
        with SendHistory(self.db, SCOPE) as history:
            history.begin_send("1")
        with SendHistory(self.db, SCOPE) as history:
            self.assertEqual(history.status("1"), "pending")
        other = dict(MAILBOX, email="other@example.test")
        with SendHistory(self.db, mailbox_scope(other, "production")) as history:
            self.assertIsNone(history.status("1"))

    def test_lock_excludes_another_process_and_releases_after_exit(self):
        path = self.root / ".forwarding.lock"
        code = (
            "from pathlib import Path\nfrom send_history import forwarding_lock\n"
            "import sys\nwith forwarding_lock(Path(sys.argv[1])) as acquired:\n"
            "    sys.exit(1 if acquired else 0)\n"
        )
        with forwarding_lock(path) as acquired:
            self.assertTrue(acquired)
            result = subprocess.run([sys.executable, "-c", code, str(path)], timeout=10)
            self.assertEqual(result.returncode, 0)
        with forwarding_lock(path) as acquired:
            self.assertTrue(acquired)

    def test_empty_page_with_more_is_reported_instead_of_looping(self):
        client = SimpleNamespace(search_messages=lambda **kwargs: SimpleNamespace(messages=(), more=True))
        with self.assertRaisesRegex(RuntimeError, "empty page"):
            list(main.iter_folder_messages(client, "10", 50))


if __name__ == "__main__":
    unittest.main()
