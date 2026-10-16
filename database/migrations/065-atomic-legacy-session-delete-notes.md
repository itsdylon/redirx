# Atomic historical project deletion

Apply063 after062 and before deploying the backend route in this package. Deferred billing migrations059–061 are not prerequisites. This change has only been staged and tested locally; no production schema was changed.

The historical DELETE endpoint previously issued three independent requests: delete mappings, delete embeddings, delete sessions. A later failure could leave a project with missing evidence. The new service-role-only RPC performs these operations in one transaction, verifies ownership and preview descendants, locks parent records, rejects newly discovered descendants after locking, and checks that sessions really disappeared before returning success. Any failure rolls back earlier deletes. Unknown transport outcomes still require history readback; the route never claims rollback or silently falls back to independent deletes.

The RPC rejects MCP-bound sessions and preserves055 evidence guards and foreign-key behavior. It is not a way to remove durable migration evidence. Browser roles cannot execute it. Historical deletion remains permanent: no restoration API is introduced, and the misleading frontend Undo action is being addressed in the history workstream.

If063 is absent, the new route returns a recoverable503 without attempting partial deletes. Rollback the backend before removing the unused RPC; do not change prior migration files. Production migration execution and deployment remain outside this staged R&D package.

Validation uses the actual schema/guards through058 plus062–063 in disposable native PostgreSQL (JSONB stands in for vectors; vector search is not exercised). Tests cover nested preview deletion, ownership, cross-owner descendant rejection, pivot-session rejection, browser-role denial, foreign-key failure rollback, a trigger that suppresses final deletion, and the actual authenticated Flask→RPC→SQL route. No live sessions were deleted.
