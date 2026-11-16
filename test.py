"""
test.py - Tests for interface.py against the simulated LLM in quote.py.

Run with either:
    python3 test.py
    python3 -m pytest test.py
"""

import io
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from interface import STATUS_APPROVED, STATUS_ERROR, run
from quote import MockQuoteClient


def run_cli(client, user_inputs, timeout=5.0, retries=0):
    """Run the CLI workflow with scripted user input and captured output."""
    output = io.StringIO()
    with patch("builtins.input", side_effect=user_inputs), redirect_stdout(output):
        status, message = run(client, timeout=timeout, retries=retries)
    return status, message, output.getvalue()


class TestQuoteInterface(unittest.TestCase):

    def test_successful_llm_response(self):
        """A valid LLM response is shown to the user as a quote and can be approved."""
        client = MockQuoteClient(seed=1, temperature=0)  # temperature 0 -> midpoint price

        status, message, output = run_cli(client, ["Boeing", "widebody", "y"])

        self.assertEqual(status, STATUS_APPROVED)
        self.assertIn("$350,000,000 USD", message)  # midpoint of Boeing widebody range
        self.assertIn("AIRCRAFT PURCHASE QUOTE", output)
        self.assertIn("Manufacturer  : Boeing", output)

    def test_llm_timeout(self):
        """An LLM that takes longer than the timeout produces an ERROR, without hanging."""
        slow_client = MockQuoteClient(seed=1, latency=(3.0, 3.0))  # always takes 3 seconds

        start = time.monotonic()
        status, message, _ = run_cli(slow_client, ["Boeing", "widebody"], timeout=0.2)
        elapsed = time.monotonic() - start

        self.assertEqual(status, STATUS_ERROR)
        self.assertIn("did not respond within 0.2 seconds", message)
        self.assertLess(elapsed, 1.0, "CLI should give up at the timeout, not wait for the LLM")

    def test_llm_bad_response(self):
        """A malformed LLM response is rejected with an ERROR and is never shown as a quote."""
        bad_client = Mock()
        bad_client.get_quote.return_value = {
            "id": "quote_bad",
            "quote": {
                "manufacturer": "Boeing",
                "aircraft_class": "widebody",
                "unit_price": -100,  # invalid: negative price
                "currency": "USD",
                "quantity": 1,
            },
        }

        status, message, output = run_cli(bad_client, ["Boeing", "widebody"])

        self.assertEqual(status, STATUS_ERROR)
        self.assertIn("Invalid price", message)
        self.assertNotIn("AIRCRAFT PURCHASE QUOTE", output)
        bad_client.get_quote.assert_called_once_with("Boeing", "widebody")


if __name__ == "__main__":
    unittest.main(verbosity=2)
