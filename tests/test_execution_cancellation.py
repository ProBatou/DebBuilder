import threading
import unittest
from unittest import mock

from debbuilder.build_models import STEP_STATUSES, new_run, validate_run
from debbuilder.execution_cancellation import (
    CANCELLATION_CODE,
    USER_REQUESTED,
    CancellationControl,
)


class CancellationControlTests(unittest.TestCase):
    def test_initial_state_is_open_and_unsignalled(self):
        control = CancellationControl()

        self.assertEqual(control.owner, "open")
        self.assertIsNone(control.request)
        self.assertFalse(control.event.is_set())

    def test_first_request_wins_and_repeated_request_keeps_metadata(self):
        control = CancellationControl()

        first = control.request_cancel()
        repeated = control.request_cancel()

        self.assertTrue(first["accepted"])
        self.assertTrue(first["first_request"])
        self.assertEqual(first["owner"], "cancellation_owned")
        self.assertEqual(first["cancellation"]["code"], CANCELLATION_CODE)
        self.assertEqual(first["cancellation"]["reason"], USER_REQUESTED)
        self.assertTrue(first["cancellation"]["requested_at"])
        self.assertTrue(control.event.is_set())
        self.assertTrue(repeated["accepted"])
        self.assertFalse(repeated["first_request"])
        self.assertEqual(repeated["cancellation"], first["cancellation"])

    def test_terminal_claim_wins_before_cancellation(self):
        control = CancellationControl()

        self.assertTrue(control.claim_terminal())
        self.assertFalse(control.claim_terminal())
        rejected = control.request_cancel()

        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["owner"], "terminal_owned")
        self.assertFalse(control.event.is_set())

    def test_cancellation_wins_before_terminal_claim(self):
        control = CancellationControl()

        self.assertTrue(control.request_cancel()["accepted"])
        self.assertFalse(control.claim_terminal())
        self.assertEqual(control.owner, "cancellation_owned")

    def test_concurrent_requests_have_one_first_request_and_one_timestamp(self):
        control = CancellationControl()
        barrier = threading.Barrier(12)
        results = []
        results_lock = threading.Lock()

        def request():
            barrier.wait()
            result = control.request_cancel()
            with results_lock:
                results.append(result)

        threads = [threading.Thread(target=request) for _index in range(12)]
        with mock.patch("debbuilder.execution_cancellation.utc_now", wraps=__import__("debbuilder.execution_cancellation", fromlist=["utc_now"]).utc_now) as clock:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(sum(result["first_request"] for result in results), 1)
        self.assertTrue(all(result["accepted"] for result in results))
        self.assertEqual(len({result["cancellation"]["requested_at"] for result in results}), 1)
        self.assertEqual(clock.call_count, 1)

    def test_cancellation_and_terminalization_have_exactly_one_owner(self):
        for _attempt in range(50):
            control = CancellationControl()
            barrier = threading.Barrier(2)
            results = {}

            def cancel():
                barrier.wait()
                results["cancel"] = control.request_cancel()

            def terminalize():
                barrier.wait()
                results["terminal"] = control.claim_terminal()

            threads = [threading.Thread(target=cancel), threading.Thread(target=terminalize)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            cancellation_won = results["cancel"]["accepted"]
            self.assertNotEqual(cancellation_won, results["terminal"])
            self.assertEqual(
                control.owner,
                "cancellation_owned" if cancellation_won else "terminal_owned",
            )


class CancellationModelTests(unittest.TestCase):
    def test_cancelled_is_accepted_without_changing_existing_step_statuses(self):
        self.assertEqual(
            STEP_STATUSES,
            frozenset({"pending", "running", "success", "failed", "skipped", "cancelled"}),
        )
        run = new_run("model-cancelled", "recipe", "build", "/tmp/model-cancelled", "digest")
        run["steps"][0]["status"] = "cancelled"

        validate_run(run)


if __name__ == "__main__":
    unittest.main()
