# PETZOO offline regression status

Status: `IMPLEMENTED_NOT_ACCEPTED`.

The isolated offline regression is complete: 1,105 nodes collected; collection matched the prior baseline. The normal partition reported 1,095 passed, 7 skipped, 0 failed, 0 errors, and 13 subtests passed. H01, K09, and P11 each passed in the same source snapshot. The complete evidence and process records are retained under the task evidence directory, attempt 10.

The seven skips are environmental or authorization-bound: four private incident-reconciliation tests require `B2B_RUN_INCIDENT_RECONCILIATION=1`, which was not enabled; three reparse-point tests require symlink creation, unavailable on this host. No user data or private incident inputs were substituted.

Runtime source-tree SHA-256: `d63e5f982d680fb12db2935efebafb4c860b4fa58694448a6fe5cc22ff9750e2` (baseline: `ac7cea155eead34c4d462492a34348f40f98d8212e27ed741be7fe27c4144b2c`). The production change reconciles affected dispatch-round state when terminal provider receipts close work allocations.

This status is not acceptance of a live run. No live/paid provider calls or real-firm measurement were made; the requested real-firm success rate remains unmeasured. The protected 162-firm run was not modified.
