# Spike 0C: Luna/Terra provider and remote transport

**Status:** capability matrix **not measured**. This environment has no
configured Luna/Terra model deployment and no credentials, so no request was
ever sent to a real provider. `schemas/assessment.wire.schema.json` therefore
remains the conservative lowest-common-denominator projection described in
spec §5.7: `const`, `format`, `minLength`, `maxLength`, `minimum`, `maximum`,
`minItems`, `maxItems`, `uniqueItems`, `oneOf`, and external `$ref` are
assumed unsupported by strict structured-output mode until a real deployment
proves otherwise. Nothing in the implementation depends on any of those
omissions being individually necessary — the projection can only be relaxed
per-deployment later, never tightened by surprise.

## What this spike cannot establish here

- Which real model identifiers back the `luna` and `terra` profiles for any
  given deployment. `src/codex_watchtower/models/config.py` treats these as
  configuration (a `model_profile` name plus endpoint/auth), never as
  hard-coded constants, precisely because this spike could not resolve them.
- Actual strict-mode keyword rejection behavior for any specific provider.
- Real latency/cost numbers.

## What is implemented against instead

`src/codex_watchtower/models/client.py` implements the transport contract
from spec §5.7/§7.3 (bounded response reading, wire-schema-only requests,
retry/backoff classification, cumulative byte ceiling) against a
provider-shaped OpenAI-compatible structured-output HTTP interface, tested
with `respx`-mocked responses rather than a live endpoint. This is testable
and correct independent of which real provider a deployment points at,
because the contract is about bytes-on-the-wire and schema shape, not about
a specific model's behavior.

`redacted-results.json` is intentionally an empty placeholder (`[]`) — no
real request was made, so there is nothing non-fabricated to record here.

## Required before enabling a specific deployment in production

1. Resolve the deployment's actual Luna/Terra model identifiers.
2. Submit `schemas/assessment.schema.json` unmodified once; record which
   keywords from the list above are rejected.
3. Confirm `schemas/assessment.wire.schema.json` is accepted as-is.
4. Record the result as a row in the capability matrix below, keyed by
   provider/model/API version, and relax the wire schema for that
   deployment only if the matrix shows it is safe to.
5. Verify HTTPS certificate validation, redirect rejection, endpoint
   allowlisting, disabled proxy inheritance, response-size cap, retry
   budget, and the provider's retention/logging policy end-to-end against
   the real endpoint.

## Capability matrix

| Provider | Model | API version | const | format | minLength/maxLength | minimum/maximum | minItems/maxItems | uniqueItems | oneOf | external $ref | Verified on |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| _(none yet — no live deployment available)_ | | | | | | | | | | | |
