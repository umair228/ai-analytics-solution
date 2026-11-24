-- ===========================================================================
-- Make the audit trail and the electronic signatures append-only IN THE DATABASE.
--
--   psql -U <owner> -d dse -f db/protect-audit-trail-postgres.sql
--   (or, on the deploy host:  make protect-audit)
--
-- Idempotent. Safe to re-run.
-- ===========================================================================
--
-- WHY THIS EXISTS
--
-- SECURITY.md has said, correctly and uncomfortably, that append-only is enforced in the
-- *application* only: the API exposes the trail through a read-only viewset and the admin
-- denies add/change/delete. That is a real control against the application, and no control
-- at all against anyone holding the application's database password — which, on this
-- deployment, is a string in a .env file. The one record that exists to prove what everyone
-- did was the one record any of them could rewrite.
--
-- Two tables, and both are already insert-only in the code:
--
--   core_auditlog   — nothing anywhere calls .update() or .delete() on it.
--   coa_signature   — created by coa/signing.py and never touched again. A signature that
--                     can be edited after the fact is not a signature.
--
-- ===========================================================================
-- WHAT THIS DOES, AND WHAT IT HONESTLY CANNOT DO
--
-- 1. REVOKE UPDATE, DELETE from the application role.
--
--    In PostgreSQL an owner may revoke its own ordinary privileges, so this bites even
--    though the app connects as the table owner on this deployment. But the owner can also
--    GRANT them straight back. On its own, therefore, this stops the *application* and it
--    stops an accident. It does not stop someone who has the password and knows to re-grant.
--
-- 2. A trigger that REFUSES the statement outright.
--
--    This is the part that survives a re-grant: the row-level rule fires whatever the
--    privileges say. UPDATE and DELETE on these tables now raise an exception.
--
-- WHAT REMAINS TRUE, AND MUST NOT BE OVERSTATED: a database superuser — or the table owner
-- — can `ALTER TABLE … DISABLE TRIGGER`, or set `session_replication_role = replica`, and
-- then do as they please. **No database can defend against its own superuser.** What this
-- changes is the nature of the act: altering the trail is no longer an UPDATE that leaves
-- no mark, it is the deliberate dismantling of a named protection. That is the difference
-- between an accident and a decision, and it is the difference an assessor cares about.
--
-- THE STRONGER CONTROL, if you want it: run the application as a role that does NOT own
-- these tables (own them with `dse`, connect the app as `dis_app` with SELECT+INSERT only,
-- and run migrations with the owner's credential). Then the app's password cannot re-grant
-- anything, because the app's role has nothing to grant. That is a change to how the api
-- container is started, so it is not done here — but it is the right end state, and
-- `manage.py preflight` will tell you that the app is still the owner.
-- ===========================================================================

\set ON_ERROR_STOP on

DO $$
DECLARE
    app_role text := current_user;   -- whoever is running this owns the tables today
BEGIN
    -- 1. The ordinary privileges.
    EXECUTE format('REVOKE UPDATE, DELETE ON TABLE core_auditlog FROM %I', app_role);
    EXECUTE format('REVOKE UPDATE, DELETE ON TABLE coa_signature FROM %I', app_role);

    -- And from PUBLIC, in case anything was ever granted there.
    REVOKE UPDATE, DELETE ON TABLE core_auditlog FROM PUBLIC;
    REVOKE UPDATE, DELETE ON TABLE coa_signature FROM PUBLIC;

    RAISE NOTICE 'Revoked UPDATE, DELETE on core_auditlog and coa_signature from %', app_role;
END
$$;

-- 2. The rule that survives a re-grant.
CREATE OR REPLACE FUNCTION dis_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'Table % is append-only: % is refused. This is the audit/signature evidence — '
        'it is written once and never changed. If you are certain you must, the protection '
        'has to be removed deliberately and visibly (ALTER TABLE % DISABLE TRIGGER %), '
        'which is the point.',
        TG_TABLE_NAME, TG_OP, TG_TABLE_NAME, TG_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS dis_auditlog_append_only ON core_auditlog;
CREATE TRIGGER dis_auditlog_append_only
    BEFORE UPDATE OR DELETE ON core_auditlog
    FOR EACH ROW EXECUTE FUNCTION dis_append_only();

DROP TRIGGER IF EXISTS dis_signature_append_only ON coa_signature;
CREATE TRIGGER dis_signature_append_only
    BEFORE UPDATE OR DELETE ON coa_signature
    FOR EACH ROW EXECUTE FUNCTION dis_append_only();

-- Note for whoever hits this during a deploy: the triggers fire on ROW-level DML, not on
-- DDL. `ALTER TABLE core_auditlog ADD COLUMN …` — an ordinary Django migration — is
-- unaffected. What WILL fail is a data migration that rewrites existing audit rows, and
-- that failing is correct: it is the thing we are preventing.

-- ===========================================================================
-- 3. The proof. This output is the URS-062 evidence for the Installation Qualification.
-- ===========================================================================
\echo ''
\echo '=== URS-062: the audit trail is append-only at the database layer ==='
\echo ''

SELECT
    c.relname                                   AS "table",
    has_table_privilege(current_user, c.oid, 'INSERT') AS "may insert",
    has_table_privilege(current_user, c.oid, 'UPDATE') AS "may update",
    has_table_privilege(current_user, c.oid, 'DELETE') AS "may delete",
    (SELECT count(*) FROM pg_trigger t
      WHERE t.tgrelid = c.oid AND t.tgname LIKE 'dis_%_append_only'
        AND NOT t.tgisinternal)                 AS "append-only trigger"
FROM pg_class c
WHERE c.relname IN ('core_auditlog', 'coa_signature')
ORDER BY c.relname;

\echo ''
\echo 'may update / may delete must both be FALSE, and the trigger count must be 1.'
\echo 'Then run:  manage.py preflight   — it checks the same thing from inside the app.'
\echo ''
