# Controlled domain discovery fixtures

Every source, search response, Hermes conversation, model response and expected
result in this directory is a **Fixture**. ExampleSilicon is fictional. The L40S
baseline is test data, never the formal business database or live verification.

`acceptance.json` records separate extraction and independent review responses.
`catalog.html`, `catalog.txt` and `catalog.pdf` exercise the actual page parsers.
The HTTP runner in `test_domain_acceptance.py` validates provider-facing JSON
schemas and calls the three actual tool endpoints, retaining its request/response
trace in the pytest fixture directory. All backend proof is produced by the real
frozen ModelService, identity resolver, linked scheduler, comparison and history.

Business digests use SQLite consistent backup, then canonical JSON of every
column and row in all six business/provenance tables. WAL/page bytes are not
business equality. A controlled external writer is detected, then the check is
repeated after that writer has finished; other processes are never stopped.

These tests do not establish acceptance on an actual installed Hermes host or a
real model provider. That remains the separate host acceptance task.
