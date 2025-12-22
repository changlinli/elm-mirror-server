# Future Work

## Sync behavior with changed package list

When a package is marked as "ignored" in `registry.json` (because it wasn't in the package list during initial sync), it won't be re-synced if the package list is later updated to include it.

**Current workaround:** Manually remove the package entries from `registry.json` or change their status from "ignored" to "pending" before re-running sync.

**Potential fix:** The sync command could detect when packages in the updated package list have status "ignored" and automatically change them to "pending" for re-processing.
