# 0.23.0 Marketing And Finance Verification

Date: 2026-07-23

## Scope

This local-candidate release implements the two previously planned business modules:

- Marketing: source-versioned campaign-day facts, explainable ROAS/CTR/no-order spend diagnostics, content drafts, and limited catalog fact checks.
- Finance: source-versioned operating expenses and settlement statements, management-profit estimates, reconciliation differences, and human task transitions.

The work explicitly excludes real-time bidding, budget changes, content publication, general-ledger posting, tax calculation, settlement execution, refunds, and fund movement.

## Evidence

- SQLite schema v23 creates five tenant-scoped tables: `marketing_campaign_metrics`, `marketing_content_drafts`, `operating_expenses`, `settlement_statements`, and `reconciliation_tasks`.
- Marketing and finance facts use source timestamps, payload hashes, stale-version rejection, and idempotent replay behavior where applicable.
- `get_marketing_diagnosis` and `get_profit_reconciliation` are registered as read-only Agent tools.
- The admin console exposes actual API outputs in dedicated Marketing and Finance workspaces. Content drafts display their fact-check result and remain non-publishable. Reconciliation tasks require an optimistic-versioned human transition.
- The explicit virtual fixture adds two campaign records, four expenses, one settlement statement, D14, and D15. The scenario output includes the inputs, assertions, service outputs, and Agent tool outputs.

## Verification

Passed:

```text
py -3.12 -m pytest tests/test_virtual_store_simulation.py tests/test_marketing_finance_api.py -q
3 passed in 21.42s

py -3.12 -m pytest tests/test_admin_console.py tests/test_api.py tests/test_virtual_store_simulation.py tests/test_marketing_finance_api.py -q
10 passed in 51.19s

node inline-script parser for docs/admin-console.html
inline scripts parsed: 1

http://127.0.0.1:8107 local preview
version=0.23.0; simulation=15/15; marketing findings=2;
management profit=1491.00; financial_statement=false
```

The virtual-store report passed all 15 scenarios. D14 detects the deliberately seeded `high_spend_no_orders` campaign and verifies that an associated content draft is not publishable. D15 returns `4181.00` gross sales, `1491.00` management profit, and the deliberate `-16.00` settlement difference as a human reconciliation task.

`py -3.12 -m pytest -q` was started but did not complete before the 120-second command limit. This release therefore does not claim a full-suite result.

## Conclusion

The two modules meet the local candidate acceptance boundary. All demonstrated marketing, profit, and reconciliation results are derived from explicitly virtual or manually imported facts and must not be represented as production platform or accounting-system evidence.
