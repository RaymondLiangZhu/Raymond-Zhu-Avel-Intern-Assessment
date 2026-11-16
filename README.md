# Aircraft Quote CLI

A command-line tool that asks for an aircraft manufacturer and class, gets a price quote for one aircraft from a simulated LLM API, and asks the user to approve it.

Requires Python 3.9+. It uses only the standard library, so there is nothing to install.

## Files

| File | What it does |
|---|---|
| `quote.py` | A mock LLM API for testing. Takes a manufacturer and an aircraft class and returns an LLM-style response with a price for one aircraft. You can make it slow or make it fail on purpose. It also works as its own command or as a local HTTP server. |
| `interface.py` | The CLI. Asks for a manufacturer and class, cleans up the input, sends it to the mock LLM (with a timeout and retries), checks the response, shows the quote and waits for approval. It ends with a status of `APPROVED`, `NOT APPROVED` or `ERROR`. |
| `test.py` | Tests for the CLI: a successful response, an LLM timeout and a bad LLM response. |

## Running the CLI

From this project folder:

```bash
python3 interface.py
```

Example session:

```
Manufacturer (e.g. Boeing, Airbus, Gulfstream, Cessna): Boeing
Aircraft class (e.g. narrowbody, widebody, light jet, piston): widebody

Requesting quote for: Boeing / widebody ...
  ...
  Unit price    : $398,641,000 USD
  ...
Approve this quote? [y/n]: y

Status: APPROVED - Quote quote_c2ce... approved at $398,641,000 USD.
```

The exit code is `0` for approved, `1` for not approved and `2` for error.

### Options

| Flag | Default | Purpose |
|---|---|---|
| `--timeout SECONDS` | `5` | How long to wait for the LLM before giving up |
| `--retries N` | `2` | How many times to retry after a timeout or a temporary error (rate-limited or overloaded) |
| `--seed N` | none | Gives the same price on every run |
| `--temperature T` | `0.7` | `0` always returns the middle of the price range; higher values vary the price |
| `--latency MIN MAX` | `0 0.5` | Adds a delay of MIN to MAX seconds before each LLM response |
| `--failure-rate P` | `0` | Chance (0 to 1) that a request fails with a rate-limited or overloaded error |

To try the error handling:

```bash
python3 interface.py --timeout 0.5 --latency 3 3          # force a timeout
python3 interface.py --failure-rate 0.6 --retries 3       # see retries in action
```

To list the manufacturers and classes it supports:

```bash
python3 quote.py catalog
```

## Running the tests

```bash
python3 test.py
```

## Limitations

### The mock LLM is much stricter than a real LLM

`quote.py` does not understand language. It makes the input lowercase, removes spaces and punctuation, and then looks for an exact match in a fixed list of accepted names. Anything not on that list is rejected with a `400` error, which the CLI reports as `ERROR`:

| Accepted | Rejected |
|---|---|
| `BOEING`, `boeing`, `Gulf stream` | `Boieng`, `Bombadier` (typos) |
| `Wide-Body`, `wide body`, `regional jet` | `widebodies`, `light jets` (plurals) |
| | `Boeing Co`, `Cessna Citation` (extra words) |
| | `business jet`, `airliner`, `jet` (common terms it doesn't know) |

A real LLM would be able to handle these inputs instead of rejecting them.

## Project Design.

The core of how the CLI runs is in the run function in interface.py. The program first asks for two inputs, the aircraft manufactorer and type, then sends the two input strings to the simmulated LLM in quote.py. The response is used to formate a price quote and the user approval decision is then waited for.