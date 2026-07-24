# 0.23.0 Marketing And Finance Pressure Verification

Date: 2026-07-24

## Full Input And Output Evidence

- Browser report: `/reports/marketing-finance-pressure`
- Raw JSON: `/reports/marketing-finance-pressure.json`
- Repository evidence: `docs/marketing-finance-pressure-evidence.json`

The evidence file is generated only when the pressure test receives an explicit
`MARKETING_FINANCE_PRESSURE_REPORT_PATH`. It contains the actual input payloads,
first applied and idempotent write outputs, all 64 content-draft outputs, all
64 reconciliation outputs, source-version rejection errors, task-transition
outputs, query outputs, and an isolated-tenant response. It contains virtual
test data only.

## Scope

This run exercised the local marketing and finance services against an isolated
temporary SQLite data directory. It covered source-versioned campaign metrics,
operating expenses, settlement statements, non-publishable content drafts,
reconciliation generation, optimistic task transitions, concurrent reporting,
and tenant isolation.

The run did not contact an advertising platform, financial system, accounting
ledger, tax service, settlement system, or customer data source.

## Reproducible Command

```text
$env:MARKETING_FINANCE_PRESSURE_REPORT_PATH = 'docs\\marketing-finance-pressure-evidence.json'
py -3.12 -m pytest tests\\test_marketing_finance_pressure.py -q
```

## Result

```text
1 passed in 9.16s

MARKETING_FINANCE_PRESSURE_REPORT={
  "completed_operations": 818,
  "concurrent_reads": 240,
  "concurrent_writes": 448,
  "durations_seconds": {
    "reads": 1.217,
    "reconciliation": 1.016,
    "writes": 3.777
  },
  "reconciliation_runs": 64,
  "tenant_isolation_reads": 64,
  "workers": 16,
  "write_statuses": {
    "expense": {"applied": 1, "idempotent": 127},
    "marketing": {"applied": 1, "idempotent": 127},
    "statement": {"applied": 1, "idempotent": 127}
  }
}
```

The HTML report reads this generated JSON and presents the complete structured
evidence in collapsible input/output blocks, with a direct download link.

## Assertions

- 128 concurrent replays of each source-versioned fact produced exactly one
  applied write and 127 idempotent results for campaign metrics, expenses, and
  settlement statements.
- 64 concurrent content drafts were all retained as drafts; every result kept
  `publication_allowed=false` and a limited fact check that requires review.
- Three source-contract negative checks rejected a same-version payload change,
  a stale expense source timestamp, and a same-version settlement change.
- 64 concurrent reconciliation runs created exactly one difference task and
  refreshed the same open task thereafter. The expected difference was
  `30.00`.
- Two simultaneous manual transitions with the same record version returned
  exactly one successful transition and one
  `reconciliation_task_version_conflict`.
- 240 concurrent diagnostics, profit reports, task queries, and draft queries
  returned internally consistent records. The profit response continued to
  declare `financial_statement=false`.
- 64 concurrent reads under another tenant returned no campaign, expense,
  statement, reconciliation-task, or content-draft data.

## Boundary

This is a local, single-process, 16-thread SQLite concurrency and correctness
check. It is not a capacity benchmark, multi-process database test, 24/72-hour
soak test, security assessment, or production release gate. Marketing remains
diagnostic-only and content remains non-publishable; finance remains a
management estimate and reconciliation only creates or manually transitions
tasks.
