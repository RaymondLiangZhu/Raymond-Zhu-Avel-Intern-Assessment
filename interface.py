"""
interface.py - Command-line front end for the mock quoting LLM in quote.py.

Workflow
--------
1. Ask the user for an aircraft manufacturer and class.
2. Preprocess the input (trim, normalize, validate).
3. Send the request to the simulated LLM (with a timeout and limited retries).
4. Post-process and validate the LLM's response.
5. Show the price quote and wait for the user to approve it.
6. Exit with a final status: APPROVED, NOT APPROVED, or ERROR.

Exit codes: 0 = APPROVED, 1 = NOT APPROVED, 2 = ERROR

Usage
-----
    python interface.py
    python interface.py --timeout 2 --latency 3 5           # force a timeout
    python interface.py --failure-rate 0.5 --retries 3      # exercise retries
"""

from __future__ import annotations

import argparse
import queue
import re
import sys
import threading
import time

from quote import MockAPIError, MockQuoteClient

STATUS_APPROVED = "APPROVED"
STATUS_NOT_APPROVED = "NOT APPROVED"
STATUS_ERROR = "ERROR"
EXIT_CODES = {STATUS_APPROVED: 0, STATUS_NOT_APPROVED: 1, STATUS_ERROR: 2}

MAX_INPUT_LENGTH = 50
MAX_INPUT_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = {429, 500, 503, 529}


class QuoteError(Exception):
    """Any failure that should end the program with an ERROR status."""


class LLMTimeoutError(QuoteError):
    pass


# ---------------------------------------------------------------------------
# 1-2. Input + preprocessing
# ---------------------------------------------------------------------------
def preprocess(raw: str, field: str) -> str:
    """Clean up a user-typed value. Raises ValueError if it is unusable."""
    text = raw.strip()
    text = re.sub(r"[^A-Za-z0-9 \-_]", "", text)  # drop punctuation / control chars
    text = re.sub(r"\s+", " ", text)               # collapse repeated whitespace
    if not text:
        raise ValueError(f"{field} cannot be empty.")
    if len(text) > MAX_INPUT_LENGTH:
        raise ValueError(f"{field} must be {MAX_INPUT_LENGTH} characters or fewer.")
    if not re.search(r"[A-Za-z]", text):
        raise ValueError(f"{field} must contain letters.")
    return text


def prompt_field(label: str, hint: str) -> str:
    """Ask for a value, re-prompting on bad input up to MAX_INPUT_ATTEMPTS times."""
    for attempt in range(1, MAX_INPUT_ATTEMPTS + 1):
        raw = input(f"{label} ({hint}): ")
        try:
            return preprocess(raw, label)
        except ValueError as e:
            remaining = MAX_INPUT_ATTEMPTS - attempt
            print(f"  Invalid input: {e}" + (f" ({remaining} attempt(s) left)" if remaining else ""))
    raise QuoteError(f"Too many invalid attempts entering {label.lower()}.")


# ---------------------------------------------------------------------------
# 3. Call the LLM with timeout + retries
# ---------------------------------------------------------------------------
def call_with_timeout(func, timeout: float, *args, **kwargs):
    """
    Run func in a daemon thread and wait at most `timeout` seconds.
    A daemon thread is used so a hung call can't keep the program alive after we give up.
    """
    result: queue.Queue = queue.Queue(maxsize=1)

    def worker():
        try:
            result.put(("ok", func(*args, **kwargs)))
        except BaseException as e:  # forward every exception to the caller
            result.put(("err", e))

    threading.Thread(target=worker, daemon=True).start()
    try:
        kind, value = result.get(timeout=timeout)
    except queue.Empty:
        raise LLMTimeoutError(f"The quoting service did not respond within {timeout:g} seconds.")
    if kind == "err":
        raise value
    return value


def request_quote(client: MockQuoteClient, manufacturer: str, aircraft_class: str,
                  timeout: float, retries: int) -> dict:
    """Send the request, retrying transient errors with exponential backoff."""
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            return call_with_timeout(client.get_quote, timeout, manufacturer, aircraft_class)
        except MockAPIError as e:
            if e.status_code in RETRYABLE_STATUS_CODES and attempt < attempts:
                wait = 0.5 * 2 ** (attempt - 1)
                print(f"  Service busy ({e.status_code} {e.error_type}); retrying in {wait:g}s "
                      f"[attempt {attempt + 1}/{attempts}]...")
                time.sleep(wait)
                continue
            raise QuoteError(f"Quoting service error ({e.status_code} {e.error_type}): {e.message}") from e
        except LLMTimeoutError:
            if attempt < attempts:
                print(f"  Request timed out; retrying [attempt {attempt + 1}/{attempts}]...")
                continue
            raise
        except QuoteError:
            raise
        except Exception as e:
            raise QuoteError(f"Unexpected error from quoting service: {type(e).__name__}: {e}") from e
    raise QuoteError("Quoting service request failed.")  # unreachable safety net


# ---------------------------------------------------------------------------
# 4. Post-process / validate the response
# ---------------------------------------------------------------------------
def postprocess(response: object) -> dict:
    """Check the response is well formed and pull out the fields we show the user."""
    if not isinstance(response, dict):
        raise QuoteError("Malformed response: expected a JSON object.")
    quote = response.get("quote")
    if not isinstance(quote, dict):
        raise QuoteError("Malformed response: missing 'quote' section.")

    required = ("manufacturer", "aircraft_class", "unit_price", "currency", "quantity")
    missing = [k for k in required if k not in quote]
    if missing:
        raise QuoteError(f"Malformed response: missing field(s) {', '.join(missing)}.")

    price = quote["unit_price"]
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        raise QuoteError(f"Invalid price in response: {price!r}.")
    if quote["quantity"] != 1:
        raise QuoteError(f"Expected a quote for 1 aircraft, got quantity {quote['quantity']!r}.")

    price_range = quote.get("price_range") or {}
    low, high = price_range.get("low"), price_range.get("high")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and not (low <= price <= high):
        raise QuoteError("Quoted price falls outside the reported price range.")

    return {
        "id": response.get("id", "n/a"),
        "manufacturer": str(quote["manufacturer"]),
        "aircraft_class": str(quote["aircraft_class"]).replace("_", " "),
        "model": str(quote.get("example_model", "n/a")),
        "price": float(price),
        "currency": str(quote["currency"]),
        "low": low,
        "high": high,
        "valid_until": quote.get("valid_until", "n/a"),
    }


def format_money(amount: float, currency: str) -> str:
    return f"${amount:,.0f} {currency}" if currency == "USD" else f"{amount:,.0f} {currency}"


def display_quote(q: dict) -> None:
    print("\n" + "=" * 52)
    print("  AIRCRAFT PURCHASE QUOTE")
    print("=" * 52)
    print(f"  Quote ID      : {q['id']}")
    print(f"  Manufacturer  : {q['manufacturer']}")
    print(f"  Class         : {q['aircraft_class']}")
    print(f"  Example model : {q['model']}")
    print(f"  Quantity      : 1")
    print(f"  Unit price    : {format_money(q['price'], q['currency'])}")
    if q["low"] is not None and q["high"] is not None:
        print(f"  Typical range : {format_money(q['low'], q['currency'])} - "
              f"{format_money(q['high'], q['currency'])}")
    print(f"  Valid until   : {q['valid_until']}")
    print("=" * 52 + "\n")


# ---------------------------------------------------------------------------
# 5. Approval
# ---------------------------------------------------------------------------
def ask_approval() -> bool:
    while True:
        answer = input("Approve this quote? [y/n]: ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please enter 'y' or 'n'.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(client: MockQuoteClient, timeout: float, retries: int) -> tuple[str, str]:
    """Returns (status, message)."""
    try:
        print("Aircraft Quote Assistant - get a price for one new aircraft.\n")
        manufacturer = prompt_field("Manufacturer", "e.g. Boeing, Airbus, Gulfstream, Cessna")
        aircraft_class = prompt_field("Aircraft class", "e.g. narrowbody, widebody, light jet, piston")

        print(f"\nRequesting quote for: {manufacturer} / {aircraft_class} ...")
        response = request_quote(client, manufacturer, aircraft_class, timeout, retries)
        quote = postprocess(response)
        display_quote(quote)

        if ask_approval():
            return STATUS_APPROVED, f"Quote {quote['id']} approved at {format_money(quote['price'], quote['currency'])}."
        return STATUS_NOT_APPROVED, f"Quote {quote['id']} was declined."
    except QuoteError as e:
        return STATUS_ERROR, str(e)
    except (KeyboardInterrupt, EOFError):
        return STATUS_ERROR, "Input cancelled by user."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CLI for requesting a single-aircraft price quote.")
    parser.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for the LLM (default 5)")
    parser.add_argument("--retries", type=int, default=2, help="retries for timeouts/transient errors (default 2)")
    # Pass-through knobs for the simulated LLM, useful for testing.
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--latency", type=float, nargs=2, default=(0.0, 0.5), metavar=("MIN", "MAX"))
    parser.add_argument("--failure-rate", type=float, default=0.0)
    args = parser.parse_args(argv)

    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.retries < 0:
        parser.error("--retries cannot be negative")

    client = MockQuoteClient(
        seed=args.seed,
        temperature=args.temperature,
        latency=tuple(args.latency),
        failure_rate=args.failure_rate,
    )
    status, message = run(client, args.timeout, args.retries)
    print(f"\nStatus: {status} - {message}")
    return EXIT_CODES[status]


if __name__ == "__main__":
    sys.exit(main())
