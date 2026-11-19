import unittest
from praxis_agent.failure_analysis import analyze_failure

class FailureAnalysisTests(unittest.TestCase):
    def test_uncertain_action_requires_reconciliation_and_is_not_retryable(self):
        result=analyze_failure({"pending_action":{"name":"click"},"last_error":{"may_have_executed":True}})
        self.assertEqual(result["category"],"uncertain_action")
        self.assertFalse(result["retryable"])
        self.assertTrue(result["requires_reconciliation"])

    def test_policy_denial_is_not_retryable(self):
        result=analyze_failure({"last_error":{"code":"policy_denied","phase":"policy"}})
        self.assertEqual(result["category"],"policy_denied")
        self.assertFalse(result["retryable"])

    def test_task_acceptance_failure_includes_only_bounded_check_signals(self):
        result=analyze_failure({"last_error":{"code":"task_acceptance_failed","phase":"task_verification"},
            "last_task_verification":{"checks":{"report":{"status":"missing"},"large":{"status":"stale"}}}})
        self.assertEqual(result["category"],"verification_failed")
        self.assertIn("failed_check:report:missing",result["signals"])
        self.assertNotIn("raw report contents",str(result))

    def test_transient_backend_error_is_retryable(self):
        result=analyze_failure({"last_error":{"code":"connection_timeout","phase":"observation"}})
        self.assertEqual(result["category"],"transient_backend_failure")
        self.assertTrue(result["retryable"])

    def test_action_failure_uses_host_result_not_model_claim(self):
        result=analyze_failure({"last_result":{"ok":False,"summary":"injected failure"}})
        self.assertEqual(result["category"],"action_failed")
        self.assertTrue(result["retryable"])

    def test_success_clears_stale_action_failure(self):
        self.assertIsNone(analyze_failure({"last_result":{"ok":True},"last_error":{"code":"connection_timeout"}}))

    def test_no_failure_returns_none(self):
        self.assertIsNone(analyze_failure({"last_result":{"ok":True}}))

if __name__=="__main__":
    unittest.main()
