"""
quote.py - A mock "LLM" API that returns a price quote for purchasing ONE aircraft.

FOR TESTING ONLY. Prices are rough, approximate list-price ranges and are not
real quotes. No network calls or external dependencies are needed (stdlib only).

Three ways to use it
--------------------
1) As a Python library:

    from quote import MockQuoteClient
    client = MockQuoteClient(seed=42)
    resp = client.get_quote("Boeing", "widebody")
    print(resp["quote"]["unit_price"], resp["content"])

    # Chat-style (free text, like an LLM prompt):
    resp = client.chat([{"role": "user", "content": "Quote me an Airbus narrowbody"}])

2) As a local HTTP server:

    python quote.py serve --port 8000

    curl -X POST localhost:8000/v1/quote \
         -H "Content-Type: application/json" \
         -d '{"manufacturer": "Gulfstream", "aircraft_class": "large_jet"}'

    curl -X POST localhost:8000/v1/chat \
         -H "Content-Type: application/json" \
         -d '{"messages": [{"role": "user", "content": "price for a Cessna piston?"}]}'

    curl localhost:8000/v1/catalog

3) From the command line:

    python quote.py quote Embraer regional
    python quote.py catalog

Testing knobs (constructor args or CLI flags)
---------------------------------------------
    seed          -> makes prices / ids / failures reproducible
    temperature   -> 0 returns the midpoint list price; >0 adds random variation
    latency       -> (min, max) seconds of simulated response delay
    failure_rate  -> 0.0-1.0 chance of a simulated rate-limit / overloaded error
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_NAME = "mock-aero-quote-1"
CURRENCY = "USD"
QUOTE_VALID_DAYS = 30

# ---------------------------------------------------------------------------
# Catalog: manufacturer -> class -> (low_price, high_price, example models)
# Prices in USD for a single new aircraft. Approximate and for testing only.
# ---------------------------------------------------------------------------
CATALOG: dict[str, dict[str, tuple[float, float, list[str]]]] = {
    "Boeing": {
        "narrowbody": (100e6, 135e6, ["737 MAX 8", "737 MAX 10"]),
        "widebody": (250e6, 450e6, ["787-9", "777-9"]),
    },
    "Airbus": {
        "narrowbody": (80e6, 130e6, ["A220-300", "A320neo", "A321neo"]),
        "widebody": (240e6, 370e6, ["A330-900", "A350-1000"]),
    },
    "Embraer": {
        "regional": (45e6, 65e6, ["E175", "E195-E2"]),
        "light_jet": (5e6, 10e6, ["Phenom 100EX", "Phenom 300E"]),
        "midsize_jet": (18e6, 23e6, ["Praetor 500", "Praetor 600"]),
    },
    "ATR": {
        "regional": (20e6, 30e6, ["ATR 42-600", "ATR 72-600"]),
    },
    "Bombardier": {
        "midsize_jet": (25e6, 30e6, ["Challenger 3500"]),
        "large_jet": (45e6, 80e6, ["Challenger 650", "Global 7500"]),
    },
    "Gulfstream": {
        "midsize_jet": (24e6, 27e6, ["G280"]),
        "large_jet": (55e6, 80e6, ["G500", "G700"]),
    },
    "Dassault": {
        "large_jet": (35e6, 75e6, ["Falcon 2000LXS", "Falcon 8X"]),
    },
    "Textron": {  # Cessna / Beechcraft
        "piston": (0.45e6, 1.1e6, ["Cessna 172 Skyhawk", "Beechcraft Bonanza G36"]),
        "turboprop": (2.5e6, 8e6, ["Cessna Caravan", "Beechcraft King Air 360"]),
        "light_jet": (5e6, 11e6, ["Citation M2 Gen2", "Citation CJ4 Gen2"]),
        "midsize_jet": (18e6, 32e6, ["Citation Latitude", "Citation Longitude"]),
    },
    "Pilatus": {
        "turboprop": (5e6, 6.5e6, ["PC-12 NGX"]),
        "light_jet": (11e6, 12e6, ["PC-24"]),
    },
    "Cirrus": {
        "piston": (0.6e6, 1.2e6, ["SR20", "SR22T"]),
        "light_jet": (3e6, 3.5e6, ["Vision Jet G2+"]),
    },
    "Piper": {
        "piston": (0.4e6, 1.4e6, ["Archer LX", "M350"]),
        "turboprop": (3e6, 3.6e6, ["M600 SLS"]),
    },
}

# Alternate spellings people might send. Keys are normalized (lowercase, alphanumerics only).
MANUFACTURER_ALIASES = {
    "boeing": "Boeing",
    "airbus": "Airbus",
    "embraer": "Embraer",
    "atr": "ATR",
    "bombardier": "Bombardier",
    "gulfstream": "Gulfstream",
    "dassault": "Dassault",
    "dassaultaviation": "Dassault",
    "falcon": "Dassault",
    "textron": "Textron",
    "textronaviation": "Textron",
    "cessna": "Textron",
    "beechcraft": "Textron",
    "beech": "Textron",
    "pilatus": "Pilatus",
    "cirrus": "Cirrus",
    "piper": "Piper",
}

CLASS_ALIASES = {
    "narrowbody": "narrowbody",
    "singleaisle": "narrowbody",
    "widebody": "widebody",
    "twinaisle": "widebody",
    "regional": "regional",
    "regionaljet": "regional",
    "regionalturboprop": "regional",
    "lightjet": "light_jet",
    "verylightjet": "light_jet",
    "vlj": "light_jet",
    "midsizejet": "midsize_jet",
    "midsize": "midsize_jet",
    "midjet": "midsize_jet",
    "supermidsize": "midsize_jet",
    "largejet": "large_jet",
    "heavyjet": "large_jet",
    "largecabin": "large_jet",
    "ultralongrange": "large_jet",
    "turboprop": "turboprop",
    "piston": "piston",
    "singleengine": "piston",
}


# ---------------------------------------------------------------------------
# Errors (shaped like typical LLM API errors)
# ---------------------------------------------------------------------------
class MockAPIError(Exception):
    def __init__(self, status_code: int, error_type: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message

    def to_dict(self) -> dict:
        return {"type": "error", "error": {"type": self.error_type, "message": self.message}}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def resolve_manufacturer(name: str) -> str:
    key = _norm(name)
    if key in MANUFACTURER_ALIASES:
        return MANUFACTURER_ALIASES[key]
    raise MockAPIError(
        400,
        "invalid_request_error",
        f"Unknown manufacturer '{name}'. Supported: {', '.join(CATALOG)}.",
    )


def resolve_class(name: str) -> str:
    key = _norm(name)
    if key in CLASS_ALIASES:
        return CLASS_ALIASES[key]
    all_classes = sorted({c for classes in CATALOG.values() for c in classes})
    raise MockAPIError(
        400,
        "invalid_request_error",
        f"Unknown aircraft class '{name}'. Supported: {', '.join(all_classes)}.",
    )


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class MockQuoteClient:
    """Simulates an LLM-backed quoting API for a single aircraft purchase."""

    def __init__(
        self,
        seed: int | None = None,
        temperature: float = 0.7,
        latency: tuple[float, float] = (0.0, 0.0),
        failure_rate: float = 0.0,
    ):
        self.rng = random.Random(seed)
        self.temperature = temperature
        self.latency = latency
        self.failure_rate = failure_rate

    # -- simulation helpers -------------------------------------------------
    def _simulate_network(self) -> None:
        lo, hi = self.latency
        if hi > 0:
            time.sleep(self.rng.uniform(lo, hi))
        if self.failure_rate > 0 and self.rng.random() < self.failure_rate:
            if self.rng.random() < 0.5:
                raise MockAPIError(429, "rate_limit_error", "Rate limit exceeded. Please retry later.")
            raise MockAPIError(529, "overloaded_error", "The quoting service is temporarily overloaded.")

    def _price(self, low: float, high: float, temperature: float) -> float:
        mid = (low + high) / 2
        if temperature <= 0:
            return round(mid, -3)
        spread = (high - low) / 2 * min(temperature, 1.0)
        return round(self.rng.uniform(mid - spread, mid + spread), -3)

    # -- public API ---------------------------------------------------------
    def get_quote(
        self,
        manufacturer: str,
        aircraft_class: str,
        temperature: float | None = None,
    ) -> dict:
        """Structured request: returns a quote for one aircraft."""
        self._simulate_network()
        temp = self.temperature if temperature is None else temperature

        mfr = resolve_manufacturer(manufacturer)
        cls = resolve_class(aircraft_class)
        if cls not in CATALOG[mfr]:
            raise MockAPIError(
                422,
                "unsupported_combination_error",
                f"{mfr} does not offer a '{cls}' aircraft. "
                f"Available classes for {mfr}: {', '.join(CATALOG[mfr])}.",
            )

        low, high, models = CATALOG[mfr][cls]
        price = self._price(low, high, temp)
        model = self.rng.choice(models)
        now = datetime.now(timezone.utc)
        valid_until = (now + timedelta(days=QUOTE_VALID_DAYS)).date().isoformat()

        content = (
            f"Thank you for your inquiry. For one (1) new {mfr} {model} "
            f"({cls.replace('_', ' ')}), the estimated purchase price is "
            f"${price:,.0f} {CURRENCY}. Typical pricing for this class ranges from "
            f"${low:,.0f} to ${high:,.0f} depending on configuration and options. "
            f"This quote is valid until {valid_until} and is non-binding."
        )
        prompt = f"{manufacturer} {aircraft_class}"

        return {
            "id": f"quote_{uuid.UUID(int=self.rng.getrandbits(128)).hex[:24]}",
            "object": "quote",
            "model": MODEL_NAME,
            "created": int(now.timestamp()),
            "quote": {
                "manufacturer": mfr,
                "aircraft_class": cls,
                "example_model": model,
                "quantity": 1,
                "currency": CURRENCY,
                "unit_price": price,
                "price_range": {"low": low, "high": high},
                "valid_until": valid_until,
            },
            "content": content,
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": _estimate_tokens(prompt),
                "output_tokens": _estimate_tokens(content),
            },
        }

    def chat(self, messages: list[dict], temperature: float | None = None) -> dict:
        """Free-text request (LLM style). Extracts manufacturer + class from the last user message."""
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if not user_msgs:
            raise MockAPIError(400, "invalid_request_error", "messages must include at least one 'user' message.")
        text = str(user_msgs[-1].get("content", ""))
        mfr, cls = self._extract(text)
        if mfr is None or cls is None:
            missing = " and ".join(
                part for part, val in (("manufacturer", mfr), ("aircraft class", cls)) if val is None
            )
            raise MockAPIError(400, "invalid_request_error", f"Could not determine the {missing} from the message.")
        resp = self.get_quote(mfr, cls, temperature=temperature)
        resp["usage"]["input_tokens"] = _estimate_tokens(text)
        return resp

    @staticmethod
    def _extract(text: str) -> tuple[str | None, str | None]:
        words = re.findall(r"[a-z0-9]+", text.lower())
        mfr = cls = None
        # Check 1-, 2-, and 3-word windows so "wide body" / "light jet" / "ultra long range" match.
        for size in (3, 2, 1):
            for i in range(len(words) - size + 1):
                key = "".join(words[i : i + size])
                if mfr is None and key in MANUFACTURER_ALIASES:
                    mfr = key
                if cls is None and key in CLASS_ALIASES:
                    cls = key
        return mfr, cls

    @staticmethod
    def catalog() -> dict:
        return {
            mfr: {cls: {"low": lo, "high": hi, "example_models": models} for cls, (lo, hi, models) in classes.items()}
            for mfr, classes in CATALOG.items()
        }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
def make_handler(client: MockQuoteClient):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/v1/catalog":
                self._send(200, client.catalog())
            elif self.path == "/health":
                self._send(200, {"status": "ok", "model": MODEL_NAME})
            else:
                self._send(404, MockAPIError(404, "not_found_error", f"No route {self.path}").to_dict())

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    raise MockAPIError(400, "invalid_request_error", "Request body must be valid JSON.")

                if self.path == "/v1/quote":
                    if "manufacturer" not in payload or "aircraft_class" not in payload:
                        raise MockAPIError(
                            400, "invalid_request_error", "Body requires 'manufacturer' and 'aircraft_class'."
                        )
                    resp = client.get_quote(
                        payload["manufacturer"], payload["aircraft_class"], payload.get("temperature")
                    )
                elif self.path == "/v1/chat":
                    resp = client.chat(payload.get("messages", []), payload.get("temperature"))
                else:
                    raise MockAPIError(404, "not_found_error", f"No route {self.path}")
                self._send(200, resp)
            except MockAPIError as e:
                self._send(e.status_code, e.to_dict())

    return Handler


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mock LLM API for single-aircraft price quotes (testing only).")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--latency", type=float, nargs=2, default=(0.0, 0.0), metavar=("MIN", "MAX"))
    parser.add_argument("--failure-rate", type=float, default=0.0)
    sub = parser.add_subparsers(dest="command", required=True)

    q = sub.add_parser("quote", help="Get a quote for one aircraft")
    q.add_argument("manufacturer")
    q.add_argument("aircraft_class")

    sub.add_parser("catalog", help="Print supported manufacturers and classes")

    s = sub.add_parser("serve", help="Run as a local HTTP API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    client = MockQuoteClient(
        seed=args.seed,
        temperature=args.temperature,
        latency=tuple(args.latency),
        failure_rate=args.failure_rate,
    )

    if args.command == "catalog":
        print(json.dumps(client.catalog(), indent=2))
    elif args.command == "quote":
        try:
            print(json.dumps(client.get_quote(args.manufacturer, args.aircraft_class), indent=2))
        except MockAPIError as e:
            print(json.dumps(e.to_dict(), indent=2), file=sys.stderr)
            return 1
    elif args.command == "serve":
        server = ThreadingHTTPServer((args.host, args.port), make_handler(client))
        print(f"Mock quote API ({MODEL_NAME}) listening on http://{args.host}:{args.port}")
        print("  POST /v1/quote   POST /v1/chat   GET /v1/catalog   GET /health")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
