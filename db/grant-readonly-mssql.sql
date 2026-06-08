/* ===========================================================================
   DIS — dedicated READ-ONLY login for the LIMS source database (SQL Server).

   DIS must never write to the LIMS. The application already blocks non-SELECT
   SQL (querybuilder.executor.assert_read_only), but defense-in-depth requires
   the database account itself to be read-only. Run this as a sysadmin against
   the LIMS server, then set DB_USER=dis_readonly / DB_PASSWORD in .env.prod.

   Replace the password and the database name (EGPC_DEV) for your tenant.
   =========================================================================== */

-- 1) Server login -----------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'dis_readonly')
    CREATE LOGIN [dis_readonly] WITH PASSWORD = N'CHANGE_ME_TO_A_STRONG_SECRET',
        CHECK_POLICY = ON;
GO

-- 2) Database user + read role ----------------------------------------------
USE [EGPC_DEV];
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'dis_readonly')
    CREATE USER [dis_readonly] FOR LOGIN [dis_readonly];
GO
ALTER ROLE [db_datareader] ADD MEMBER [dis_readonly];   -- SELECT on all tables/views
GO

-- 3) Explicitly deny every write/exec path (belt and braces) ----------------
DENY INSERT, UPDATE, DELETE, ALTER, EXECUTE, CREATE TABLE TO [dis_readonly];
GO

/* Verify:
   EXECUTE AS USER = 'dis_readonly';
   SELECT TOP 1 * FROM RESULT;          -- should succeed
   -- UPDATE RESULT SET ... ;           -- should fail: permission denied
   REVERT;
*/
