# Bounded model requests

The runtime retries only model-provider requests: HTTP 408/429/500/502/503/504 and
transport timeouts. Authentication, other permanent errors and malformed responses
stop immediately. Retries use capped exponential backoff and bounded numeric
Retry-After values. An endpoint circuit opens after three consecutive transient
failures and can be probed again after 30 seconds. No tool action is retried here.

```bash
praxis run goal.json --browser-url http://127.0.0.1:8765/ \
  --model-attempts 3 --model-timeout 30 --model-fallbacks 1 \
  --max-model-calls 40 --max-model-tokens 100000 --max-run-seconds 600 \
  --max-cost-usd 1 --input-price-per-million 1 --output-price-per-million 4
```

Prices in this example are placeholders; supply your provider's actual USD per
million token prices. Model profile JSON can override both price fields for each
profile. Cost-limited execution fails closed if selected-model prices are absent.
Failover requires configured model profiles and preserves required capabilities.
It applies only to transient provider errors, with a bounded number of alternatives.

Every actual attempt consumes the call budget. Missing usage, including failed
requests, is explicitly estimated for both input and reserved output. Known usage
is recorded separately. Cost is stored in integer microdollars; estimated charges
and unknown-price calls are labelled. Requests reserve estimated input and bounded
output cost before dispatch. These estimates are budget controls, not a provider
billing guarantee, because tokenization and provider accounting can differ.

Run time accumulates across resume. The runtime checks the deadline between
operations and caps model request timeouts to remaining time. It cannot interrupt
an already-running external tool. A resumed run with an exhausted budget pauses
until the operator supplies a larger limit. Model circuits are process-local.
