# Offline trading lifecycle fault drills

Run: `python -m unittest tests.test_trading_fault_drills -v`.
The existing CI unittest discovery includes this module. No workflow change or production restart is required.

## What is exercised

- Real main executor entry, stop placement, partial exit, database reopen, final exit and residual-order check.
- Real MR entry and later session exit across database reopen.
- MR partial exits at 10%, 40%, 90%, each combined with lost response, SQLite transaction abort or simulated process interruption. A further unavailable-fill-history tick must not issue another exit; restored evidence permits recovery and final settlement exactly once.
- Stale MR position after partial receipt remains recoverable; fresh fill evidence records the partial once.
- Stop filled during cancellation, delayed fills, database reopen and subsequent reconciliation close at the fill price without a new close/trigger order.
- Untrusted order snapshots prevent unknown-exit recovery writes across reconstructed executors and database connections.

Network socket connections are forbidden inside drills. Exchange I/O and candle acquisition/aggregation are fixtures; production business methods and SQLite transactions execute normally.

## Known boundary, not a safety pass

`test_known_boundary_lost_response_marks_estimated_close_incomplete` characterizes existing MR behavior: when the exchange is flat after a lost exit response and fill history is unreadable, a recent exit intent can lead to mark-estimated CLOSED with `net_pnl_complete=false` and action_required. This does NOT prove evidence-only settlement or automatic correction when fills later arrive. That recovery policy remains unchanged and unresolved by this test-only PR.

## Limits

These are deterministic regression trajectories, not exhaustive model checking. Process interruption is a BaseException unwinding business execution, followed by closing/reopening SQLite and rebuilding the leg; it is not SIGKILL, a physical disk failure or host power loss. Most main-leg recovery trajectories begin with seeded open positions. Candle/session selection and real Kraken protocol/network behavior are not certified by these fixtures. Dedicated real account faults are not induced.

Existing CI runs the entire test suite on every PR and main push. These tests do not require adding workflow permissions or deploying a new production image.
