# Staged067: initial Jev confirmation operation scope

**Staged and locally verified, not applied to any production database.** This is a separate deployment prerequisite for fixing cross-run initial-confirmation collisions. Merely shipping application code does not replace the database function.

The migration requires the already-installed062 Jev schema, its existing `public.prepare_jev_pass(uuid,uuid,text,integer)` function and mapping-decision/engine-attempt prerequisites from the retained pivot chain. It does not depend on deferred billing059–061 or the unrelated063–066 features. Review the actual target/catalog and deployment packet before applying; this package does not broaden the old migration runner or either063–065/066 readiness check.

067 replaces only that public function. Its only body change adds the run UUID to the initial decision operation key while retaining the original raw JSON pair digest. Explicit schema qualification ensures a nondefault migration search path cannot create a shadow function. `CREATE OR REPLACE` preserves the existing function identity, owner and ACL; native tests verify service-role execution and denial for PUBLIC/anon/authenticated. This preservation assumes the required existing062 authority and its grants are present, not an arbitrary damaged catalog.

There is no historical-row rewrite. Prepared runs retain their old keys and audited decisions. A failed collision transaction leaves no initial mapping/decision side effects; after067, an explicitly requested `refine_matches` can resume the failed same pass using current lease/seed-revision authority. No recovery is triggered automatically. The prepared flag, locking, ownership, attempt checks, seed revision, decision revision and provider budget policy are unchanged.

See [native evidence and replay commands](../../docs/research/jev-initial-seed-scope/README.md). Reapplying067 preserves rows and function identity. Replacing it with the old062 body would reintroduce the collision; this receipt does not recommend rollback as a data repair.

The separate [067 catalog checker](../../docs/research/schema-readiness/jev-seed-scope-067.md) can verify the exact installed function and effective grants in a read-only transaction. It neither applies this migration nor establishes all prerequisite schema behavior. The existing release packet and063–066 checks remain unchanged.
