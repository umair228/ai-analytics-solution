"""Runtime security posture — the Installation Qualification, made executable.

Six requirements were marked OPERATOR TO VERIFY because no *unit test* can prove them:
a unit test runs against a test database on a laptop, and TLS, a database grant and a
backup are facts about the **installed system**. That reasoning is correct, and it is
also a little lazy — because a command that runs *on the installed system* can prove
most of them, and a check nobody runs is a control nobody has.

So this is the other half. ``manage.py preflight`` runs against the live deployment,
reads the real settings and interrogates the real databases, and prints evidence an
assessor can paste into the IQ.

Two rules it keeps:

* **It never writes.** Not even in a transaction it rolls back. Proving a read-only
  grant by attempting an INSERT into a GxP LIMS is a cure worse than the disease — and
  unnecessary, because both SQL Server (``HAS_PERMS_BY_NAME``) and Postgres
  (``has_table_privilege``) will answer the question from metadata.
* **It does not pretend.** What it cannot check — that a backup *restores* — it says it
  cannot check. An IQ that quietly downgrades "unverified" to "fine" is how a system
  gets certified without being safe.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings

PASS, FAIL, WARN, UNKNOWN = "PASS", "FAIL", "WARN", "UNKNOWN"


@dataclass
class Check:
    id: str                 # the URS it produces evidence for
    name: str
    status: str
    detail: str
    fix: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.status == FAIL


# ---------------------------------------------------------------------------
# URS-060 — transport encryption
# ---------------------------------------------------------------------------
def check_tls() -> list[Check]:
    out = []
    ssl_redirect = getattr(settings, "SECURE_SSL_REDIRECT", False)
    secure_cookies = (getattr(settings, "SESSION_COOKIE_SECURE", False)
                      and getattr(settings, "CSRF_COOKIE_SECURE", False))
    hsts = getattr(settings, "SECURE_HSTS_SECONDS", 0)

    if ssl_redirect and secure_cookies:
        out.append(Check(
            "URS-060", "TLS enforced", PASS,
            f"SSL redirect on, secure cookies on, HSTS {hsts}s.",
            evidence={"SECURE_SSL_REDIRECT": True, "SESSION_COOKIE_SECURE": True,
                      "SECURE_HSTS_SECONDS": hsts}))
    else:
        out.append(Check(
            "URS-060", "TLS enforced", FAIL,
            "The application is serving over plaintext HTTP. Session cookies, JWTs, "
            "login passwords and laboratory results all traverse the network in "
            "cleartext, readable by anyone on the path.",
            fix=("Terminate TLS at the edge, then set DJANGO_SSL_REDIRECT=True and "
                 "DJANGO_SECURE_COOKIES=True in .env.prod and restart. "
                 "See dis-deploy/docs/TLS.md — `make tls`."),
            evidence={"SECURE_SSL_REDIRECT": ssl_redirect,
                      "SESSION_COOKIE_SECURE": getattr(settings, "SESSION_COOKIE_SECURE", False),
                      "CSRF_COOKIE_SECURE": getattr(settings, "CSRF_COOKIE_SECURE", False)}))

    # An https-enforcing app that still trusts http:// origins is not enforcing much.
    plain = [o for o in (list(getattr(settings, "CORS_ALLOWED_ORIGINS", []))
                         + list(getattr(settings, "CSRF_TRUSTED_ORIGINS", [])))
             if str(o).startswith("http://")]
    if plain:
        out.append(Check(
            "URS-060", "No plaintext origins trusted", FAIL,
            f"{len(plain)} origin(s) are still http://: {', '.join(plain[:4])}",
            fix="Change CORS_ALLOWED_ORIGINS / CSRF_TRUSTED_ORIGINS to https://.",
            evidence={"plaintext_origins": plain}))
    else:
        out.append(Check("URS-060", "No plaintext origins trusted", PASS,
                         "Every trusted origin is https://."))
    return out


# ---------------------------------------------------------------------------
# URS-061 — the LIMS account must not be able to write.
# ---------------------------------------------------------------------------
_WRITE_PROBE = {
    # Metadata only. Both engines answer "may I?" without our attempting anything.
    "mssql": ("SELECT HAS_PERMS_BY_NAME(:tbl, 'OBJECT', 'INSERT') AS ins, "
              "HAS_PERMS_BY_NAME(:tbl, 'OBJECT', 'UPDATE') AS upd, "
              "HAS_PERMS_BY_NAME(:tbl, 'OBJECT', 'DELETE') AS del"),
    "postgres": ("SELECT has_table_privilege(:tbl, 'INSERT') AS ins, "
                 "has_table_privilege(:tbl, 'UPDATE') AS upd, "
                 "has_table_privilege(:tbl, 'DELETE') AS del"),
}


def _probe_write_grants(datasource, table):
    from sqlalchemy import text

    from connections.engine import get_engine

    sql = _WRITE_PROBE.get(datasource.source_type)
    if not sql:
        return None
    with get_engine(datasource).connect() as conn:
        row = conn.execute(text(sql), {"tbl": table}).mappings().first()
    return {k: bool(v) for k, v in dict(row or {}).items()}


# Accounts that are, by convention, the database superuser. Connecting an analytics
# tool to a GxP LIMS as one of these is not a missing grant — it is no grant at all.
_SUPERUSER_LOGINS = {"sa", "root", "postgres", "admin", "administrator", "system"}


def check_lims_readonly() -> list[Check]:
    from connections.constants import SourceType
    from connections.models import DataSource

    sources = DataSource.objects.filter(
        source_type__in=[SourceType.MSSQL, SourceType.POSTGRES])
    if not sources.exists():
        return [Check("URS-061", "LIMS account is read-only", UNKNOWN,
                      "No MSSQL/Postgres data source is registered.")]

    out = []
    for ds in sources:
        who = (ds.username or "").strip()
        label = f"LIMS account is read-only ({ds.name})"
        table = f"{ds.schema_name or 'dbo'}.SAMPLE" if ds.source_type == "mssql" \
            else "sample"

        # Name the account BEFORE probing. If it is the sysadmin, the grant question is
        # moot and the finding is far more serious than "a DENY is missing".
        if who.lower() in _SUPERUSER_LOGINS:
            out.append(Check(
                "URS-061", label, FAIL,
                f"DIS connects to this LIMS as '{who}' — the database SUPERUSER. It has "
                f"full write and administrative rights over the laboratory's system of "
                f"record. The application's read-only SQL parser is then the ONLY thing "
                f"standing between an analytics tool and a GxP database, and "
                f"SECURITY.md §1 calls the database grant 'the authoritative backstop "
                f"if the app is ever bypassed'. There is no backstop.",
                fix=(f"Create a dedicated read-only login (db/grant-readonly-mssql.sql), "
                     f"point this data source at it, and remove the '{who}' credentials "
                     f"from .env. Rotate the '{who}' password — it has been in a "
                     f"config file."),
                evidence={"username": who, "superuser": True}))
            continue

        try:
            grants = _probe_write_grants(ds, table)
        except Exception as exc:  # noqa: BLE001
            out.append(Check(
                "URS-061", label, UNKNOWN,
                f"Could not interrogate the grant (connecting as '{who or '?'}'): "
                f"{str(exc).splitlines()[0]}",
                fix="Verify manually with db/grant-readonly-mssql.sql."))
            continue

        if grants is None:
            out.append(Check("URS-061", label, UNKNOWN,
                             f"No metadata probe for dialect '{ds.source_type}'."))
            continue

        writable = [k for k, v in grants.items() if v]
        if writable:
            out.append(Check(
                "URS-061", label, FAIL,
                f"The account '{who}' CAN {', '.join(w.upper() for w in writable)} on "
                f"{table}. The application refuses writes, but the database is the "
                f"backstop and it is not in place.",
                fix="Apply db/grant-readonly-mssql.sql (db_datareader + explicit DENY).",
                evidence={"username": who, "table": table, **grants}))
        else:
            out.append(Check(
                "URS-061", label, PASS,
                f"The account '{who}' has no INSERT/UPDATE/DELETE on {table}.",
                evidence={"username": who, "table": table, **grants}))
    return out


# ---------------------------------------------------------------------------
# URS-062 — the audit trail, protected at the DATABASE layer
# ---------------------------------------------------------------------------
# The tables that are evidence: written once, never changed. AuditLog is never .update()d
# or .delete()d anywhere in the code, and a Signature is created by coa/signing.py and never
# touched again — a signature that can be edited after the fact is not a signature.
_APPEND_ONLY_TABLES = ("core_auditlog", "coa_signature")


def check_audit_grants() -> list[Check]:
    """Is the evidence protected by the DATABASE, or only by our good intentions?

    This check used to ask one question — ``has_table_privilege('core_auditlog','UPDATE')``
    — and recommend a ``REVOKE`` when the answer was yes. Running that remediation against a
    real Postgres showed it does nothing at all, because **the application connects as a
    superuser** (the postgres image makes ``POSTGRES_USER`` one), and a superuser bypasses
    privilege checks entirely. The check would have reported FAIL forever, and the fix it
    printed would never have worked. An IQ check whose remediation does not remediate is
    worse than no check: it sends someone to do a job that cannot succeed and then tells
    them they failed.

    So it now asks the question that actually decides it: **is the append-only TRIGGER
    there?** A trigger fires whatever the privileges say — it refuses the UPDATE even from
    the superuser. That is the control. The REVOKE is still worth having (it is what starts
    working the day the app stops being a superuser), but it is not what is holding the
    line today.
    """
    from django.db import connection

    vendor = connection.vendor
    if vendor != "postgresql":
        return [Check(
            "URS-062", "Audit trail protected at the database layer", UNKNOWN,
            f"The application database is {vendor}, which has no per-table grants or "
            f"triggers to check here. URS-010 (append-only in the app) still holds; the "
            f"database-layer backstop does not exist on this engine.",
            fix="Run the application database on PostgreSQL in production.")]

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            row = cur.fetchone()
            is_superuser = bool(row and row[0])

            cur.execute(
                """
                SELECT c.relname,
                       has_table_privilege(current_user, c.oid, 'UPDATE'),
                       has_table_privilege(current_user, c.oid, 'DELETE'),
                       (SELECT count(*) FROM pg_trigger t
                         WHERE t.tgrelid = c.oid
                           AND t.tgname LIKE 'dis_%%_append_only'
                           AND NOT t.tgisinternal
                           AND t.tgenabled <> 'D')          -- 'D' = disabled
                  FROM pg_class c
                 WHERE c.relname = ANY(%s)
                """, [list(_APPEND_ONLY_TABLES)])
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        return [Check("URS-062", "Audit trail protected at the database layer", UNKNOWN,
                      f"Could not interrogate the protection: {exc}")]

    out = []
    found = {r[0]: r for r in rows}

    for table in _APPEND_ONLY_TABLES:
        label = f"Append-only at the database layer ({table})"
        row = found.get(table)
        if row is None:
            out.append(Check("URS-062", label, UNKNOWN,
                             f"{table} does not exist in this database."))
            continue

        _, can_update, can_delete, triggers = row
        if triggers:
            out.append(Check(
                "URS-062", label, PASS,
                f"An append-only trigger is enabled on {table}: UPDATE and DELETE are "
                f"refused at the database, whatever the privileges say — including for the "
                f"superuser this application currently connects as.",
                evidence={"trigger": True, "can_update": bool(can_update),
                          "can_delete": bool(can_delete)}))
        else:
            out.append(Check(
                "URS-062", label, FAIL,
                f"{table} is append-only in the APPLICATION only. Anyone holding the "
                f"database password — which is a string in .env.prod — can rewrite the one "
                f"record that exists to prove what they did.",
                fix=("psql -U <owner> -d <db> -f db/protect-audit-trail-postgres.sql\n"
                     "         (on the deploy host: `make protect-audit`)\n"
                     "         NOTE: a bare REVOKE is NOT enough here — see below."),
                evidence={"trigger": False, "can_update": bool(can_update),
                          "can_delete": bool(can_delete)}))

    # The root cause behind all of it, and the reason a REVOKE alone is useless.
    if is_superuser:
        out.append(Check(
            "URS-062", "The application is not a database superuser", FAIL,
            "DIS connects to its own metadata database as a PostgreSQL SUPERUSER (the "
            "postgres image makes POSTGRES_USER one). A superuser bypasses every privilege "
            "check, so REVOKE cannot protect anything from it, and it can disable the "
            "append-only triggers at will. The triggers still raise the cost of tampering "
            "from 'an UPDATE that leaves no mark' to 'deliberately dismantling a named "
            "protection' — but the honest statement is that no database defends against "
            "its own superuser.",
            fix=("Create a NON-superuser role for the application (SELECT/INSERT/UPDATE/"
                 "DELETE on the app tables, INSERT+SELECT only on core_auditlog and "
                 "coa_signature), point DJANGO's DATABASES at it, and keep the owner's "
                 "credential for migrations only."),
            evidence={"superuser": True}))
    else:
        out.append(Check(
            "URS-062", "The application is not a database superuser", PASS,
            "The application connects as an ordinary role, so the grants above are a real "
            "boundary rather than a formality.",
            evidence={"superuser": False}))

    return out


# ---------------------------------------------------------------------------
# URS-063 — credential encryption key
# ---------------------------------------------------------------------------
def check_fernet() -> list[Check]:
    from decouple import config

    explicit = config("DSE_FERNET_KEY", default="")
    if explicit:
        return [Check("URS-063", "Credential key is explicit", PASS,
                      "DSE_FERNET_KEY is set explicitly.")]
    return [Check(
        "URS-063", "Credential key is explicit", FAIL,
        "DSE_FERNET_KEY is not set, so the key encrypting every stored data-source "
        "password is DERIVED FROM SECRET_KEY — which lives in the same .env file. "
        "Anyone who reads that file has both the ciphertext and the key.",
        fix=("Generate one and set it in .env.prod:\n"
             "  python -c \"from cryptography.fernet import Fernet; "
             "print(Fernet.generate_key().decode())\"\n"
             "Back it up SEPARATELY from the database dump it protects, and never "
             "rotate it in place — that invalidates every saved connection password."))]


# ---------------------------------------------------------------------------
# Posture checks that are cheap and catch the classic go-live mistakes
# ---------------------------------------------------------------------------
def check_posture() -> list[Check]:
    from decouple import config

    out = []
    if settings.DEBUG:
        out.append(Check("URS-065", "DEBUG is off", FAIL,
                         "DEBUG=True in production leaks stack traces, settings and SQL.",
                         fix="Set DJANGO_DEBUG=False."))
    else:
        out.append(Check("URS-065", "DEBUG is off", PASS, "DEBUG=False."))

    if settings.SECRET_KEY.startswith("dev-insecure"):
        out.append(Check("URS-065", "SECRET_KEY is not the dev default", FAIL,
                         "The shipped development SECRET_KEY is still in use. Sessions "
                         "and password-reset tokens are forgeable by anyone with the "
                         "source.",
                         fix="Set DJANGO_SECRET_KEY to a long random value."))
    else:
        out.append(Check("URS-065", "SECRET_KEY is not the dev default", PASS, "Set."))

    if getattr(settings, "AUDIT_FAIL_OPEN", False):
        out.append(Check("URS-011", "Audit writes fail closed", FAIL,
                         "AUDIT_FAIL_OPEN=True — an action whose audit entry fails will "
                         "still succeed, silently. This voids the attributability claim.",
                         fix="Set AUDIT_FAIL_OPEN=False."))
    else:
        out.append(Check("URS-011", "Audit writes fail closed", PASS, "Fail-closed."))

    if getattr(settings, "DSE_ALLOW_EXTERNAL_LLM", False):
        out.append(Check(
            "URS-030", "No LLM egress", WARN,
            "DSE_ALLOW_EXTERNAL_LLM=True. Prompts — which carry sample rows — MAY leave "
            "the network. This is a deliberate setting; confirm the customer has agreed "
            "to it in writing.",
            fix="Set DSE_ALLOW_EXTERNAL_LLM=False for an on-premise deployment."))
    else:
        out.append(Check("URS-030", "No LLM egress", PASS,
                         "External LLM calls are refused by construction."))

    tag = config("TAG", default="latest")
    model = getattr(settings, "LLM_MODEL", "")

    if tag == "latest":
        out.append(Check(
            "URS-065", "Deployed build is reproducible", FAIL,
            "The image is pinned to TAG=latest, so the deployed build cannot be "
            "reconstructed — and the /validation page's whole claim is that its Operational "
            "Qualification is evidence about *this build*. With `latest`, nobody can say "
            "which build that was.",
            fix="make pin   (resolves the running images to digests and writes them to "
                ".env.prod; TAG=latest@sha256:… works in the compose file unchanged)"))
    elif "@sha256:" not in tag:
        out.append(Check("URS-065", "Deployed build is reproducible", WARN,
                         f"TAG={tag} is a mutable tag, not a digest — it can be moved to "
                         f"point at different code without anything here changing.",
                         fix="make pin"))
    else:
        out.append(Check("URS-065", "Deployed build is reproducible", PASS,
                         f"Pinned by digest: TAG={tag}", evidence={"TAG": tag}))

    # ---- the model that is actually answering ------------------------------------
    #
    # This used to say "pin the model by digest", which is advice nobody can follow: Ollama
    # has no digest-pinned reference. So the question changes from "is it pinned" (it cannot
    # be) to "has it MOVED" — which is the thing that actually matters, and which is
    # answerable.
    from . import model_identity

    recorded = model_identity.recorded_digest()
    if not model:
        pass
    elif not recorded:
        out.append(Check(
            "URS-065", "The model answering is the model that was qualified", WARN,
            f"No digest was recorded for LLM_MODEL={model}, so there is nothing to compare "
            f"against. `{model}` is a mutable tag: re-pulling it can silently swap the "
            f"weights, and with them every answer — while the OQ, the golden-set results "
            f"and the /validation page all go on describing the model it replaced.",
            fix="make pin   (records DSE_LLM_MODEL_DIGEST in .env.prod)"))
    else:
        live, note = model_identity.live_digest(model)
        if live is None:
            out.append(Check(
                "URS-065", "The model answering is the model that was qualified", UNKNOWN,
                f"Could not read the digest of '{model}': {note}. This is not agreement — "
                f"it is not knowing.",
                fix="Check the model host is up and serving the configured model."))
        elif live != recorded:
            out.append(Check(
                "URS-065", "The model answering is the model that was qualified", FAIL,
                f"The model has CHANGED. '{model}' was qualified as {recorded[:19]}… and is "
                f"now {live[:19]}…. Someone has re-pulled the tag. Every accuracy number, "
                f"every golden-set pass and the entire /validation page are evidence about a "
                f"model that is no longer the one answering questions — the qualification is "
                f"void until it is re-run.",
                fix="Re-run the qualification against the new model (`make ci`), then "
                    "`make pin` to record it. Or restore the qualified weights.",
                evidence={"recorded": recorded, "running": live}))
        else:
            out.append(Check(
                "URS-065", "The model answering is the model that was qualified", PASS,
                f"'{model}' still has the digest it was qualified with ({recorded[:19]}…).",
                evidence={"digest": recorded}))
    return out


# ---------------------------------------------------------------------------
# URS-034/035/036 — who may get in, and what the trail remembers
# ---------------------------------------------------------------------------
def check_access_and_trail() -> list[Check]:
    out = []

    # §11.10(d) — "limiting system access to authorized individuals".
    if getattr(settings, "DSE_ALLOW_SELF_REGISTRATION", False):
        out.append(Check(
            "URS-034", "Registration is closed", FAIL,
            "POST /api/auth/register/ will mint an account for anyone who can reach this "
            "server — and a brand-new account can read every ORG-visible dataset in the "
            "laboratory. On a system still served over plaintext HTTP, that is a public "
            "read of the lab.",
            fix="Unset DSE_ALLOW_SELF_REGISTRATION and restart. Create users in Admin."))
    else:
        out.append(Check(
            "URS-034", "Registration is closed", PASS,
            "Self-registration is off; accounts are created by an administrator.",
            evidence={"DSE_ALLOW_SELF_REGISTRATION": False}))

    # Closing the door does nothing about whoever is already inside. An account created
    # through the endpoint while it was open lands as a VIEWER with no organisation and no
    # laboratory — and now that ORG visibility means something, it can read every org-wide
    # dataset. Nobody will remember to go and look, so preflight looks.
    from django.contrib.auth import get_user_model

    User = get_user_model()
    strays = list(
        User.objects.filter(is_active=True, role=User.Role.VIEWER,
                            organization__isnull=True, labs__isnull=True)
        .exclude(is_superuser=True).values_list("username", flat=True)[:20])
    if strays:
        out.append(Check(
            "URS-034", "No unexplained accounts", WARN,
            f"{len(strays)} active account(s) have no organisation and no laboratory, "
            f"which is what self-registration used to produce: "
            f"{', '.join(strays[:8])}{'…' if len(strays) > 8 else ''}. They can read every "
            f"ORG-visible dataset. Confirm an administrator created each of them.",
            fix="Review /admin/accounts/user/ — deactivate any you do not recognise, and "
                "assign the rest to an organisation and a laboratory.",
            evidence={"accounts": strays}))
    else:
        out.append(Check("URS-034", "No unexplained accounts", PASS,
                         "Every active account belongs to an organisation or a laboratory."))

    # §11.300(d) — detect and report attempts at unauthorised use.
    rates = (getattr(settings, "REST_FRAMEWORK", {}) or {}).get("DEFAULT_THROTTLE_RATES", {})
    login_rate = rates.get("login")
    max_fail = getattr(settings, "DSE_LOGIN_MAX_FAILURES", 0)
    if login_rate and max_fail:
        out.append(Check(
            "URS-035", "Failed logins are detected and rate-limited", PASS,
            f"Login is capped at {login_rate} per address; an account locks after "
            f"{max_fail} consecutive failures for "
            f"{getattr(settings, 'DSE_LOGIN_LOCKOUT_MINUTES', 0)} min, and every attempt "
            f"is written to the trail.",
            evidence={"login_rate": login_rate, "max_failures": max_fail}))
    else:
        out.append(Check(
            "URS-035", "Failed logins are detected and rate-limited", FAIL,
            "Login is unthrottled or the lockout is disabled. A failed login leaves no "
            "mark and nothing slows down a password guesser.",
            fix="Set DSE_LOGIN_MAX_FAILURES and the `login` throttle rate."))

    # §11.10(e) — change control. Capturing a reason is shipped; DEMANDING one is a
    # deliberate act, because turning it on rejects updates from clients that do not send
    # one. Preflight's job is to make sure nobody forgets that it is off.
    controlled = getattr(settings, "DSE_REQUIRE_CHANGE_REASON", []) or []
    if controlled:
        out.append(Check(
            "URS-014", "A reason is required to change a controlled record", PASS,
            f"A stated reason is required to modify: {', '.join(controlled)}.",
            evidence={"controlled": list(controlled)}))
    else:
        out.append(Check(
            "URS-014", "A reason is required to change a controlled record", WARN,
            "Before/after values ARE recorded on every change (§11.10(e)), but no record "
            "type demands a stated reason, so most changes will carry none. For a "
            "regulated deployment that is a change-control gap, not a preference.",
            fix="DSE_REQUIRE_CHANGE_REASON=DataSource,QueryDefinition,Dataset"))
    # ---- MFA (URS-066) ------------------------------------------------------------
    from accounts import mfa as mfa_mod

    roles = getattr(settings, "DSE_REQUIRE_MFA_ROLES", []) or []
    if not roles:
        out.append(Check(
            "URS-066", "Two-factor authentication is required for privileged roles", WARN,
            "MFA is implemented and any user may enable it, but no role is OBLIGED to. On a "
            "system whose administrators can read every laboratory's data, a password is the "
            "only thing between an attacker and all of it — and passwords are reused.",
            fix="DSE_REQUIRE_MFA_ROLES=admin,qa_manager,lab_manager"))
    else:
        User = get_user_model()
        without = list(
            User.objects.filter(is_active=True, role__in=roles)
            .filter(mfa__isnull=True).values_list("username", flat=True)[:20])
        # A device that exists but was never confirmed does not count. Half an enrolment is
        # not a second factor.
        unconfirmed = list(
            User.objects.filter(is_active=True, role__in=roles, mfa__confirmed_at__isnull=True)
            .exclude(mfa__isnull=True).values_list("username", flat=True)[:20])
        outstanding = without + unconfirmed
        if outstanding:
            out.append(Check(
                "URS-066", "Two-factor authentication is required for privileged roles", WARN,
                f"{len(outstanding)} account(s) in a role that requires MFA have not enrolled: "
                f"{', '.join(outstanding[:8])}{'…' if len(outstanding) > 8 else ''}. They can "
                f"sign in, and are refused every endpoint except the ones that let them enrol "
                f"— so they are not a hole, they are an outstanding action.",
                fix="Ask them to complete Profile → Security. Nothing else will work for them.",
                evidence={"roles": list(roles), "outstanding": outstanding}))
        else:
            out.append(Check(
                "URS-066", "Two-factor authentication is required for privileged roles", PASS,
                f"Every active user in {', '.join(roles)} carries a confirmed second factor.",
                evidence={"roles": list(roles)}))

    return out


# ---------------------------------------------------------------------------
# URS-064 — backups
# ---------------------------------------------------------------------------
def check_backups() -> list[Check]:
    """The one check this command is structurally incapable of making.

    Preflight runs *inside the api container*. The backups live on the host, written by
    ``dis-deploy/scripts/backup-db.sh``, which the container cannot see. So there is no
    honest way for this process to assert that a backup exists, is encrypted, or restores.

    It could be faked — read an env var the backup script sets, and go green. That is
    precisely the failure this file exists to avoid: a control that reports on itself is
    not a control. So the check reports UNKNOWN, names the command that *can* prove it,
    and lets `manage.py preflight` do what it already does with UNKNOWN — refuse to call
    it a pass.
    """
    return [Check(
        "URS-064", "Backups encrypted, off-host, and proven to restore", UNKNOWN,
        "Cannot be verified from inside the api container: the backups are host files. "
        "This is not a soft pass — until the command below has been run, nobody knows "
        "whether this system can be recovered.",
        fix=("On the deploy host:  BACKUP_PASSPHRASE='…' make verify-backup\n"
             "         It decrypts the newest backup, restores it into a throwaway "
             "database, and counts the records that came back. Attach its output to the "
             "IQ — that output is the evidence for URS-064."))]


# ---------------------------------------------------------------------------
# A known gap that MUST NOT be silently forgotten
# ---------------------------------------------------------------------------
def check_known_gaps() -> list[Check]:
    """Gaps SECURITY.md §7 declares. Preflight repeats them, because a gap listed in a
    document nobody re-reads is a gap nobody remembers."""
    from accounts.models import APIToken

    out = []

    # The credential store must not hold usable credentials. Verified structurally, not
    # by trusting a migration ran: if `key` ever comes back, this fails.
    fields = {f.name for f in APIToken._meta.get_fields()}
    if "key" in fields:
        out.append(Check(
            "URS-063", "API tokens are hashed", FAIL,
            "APIToken still has a plaintext `key` column. Anyone with database read — "
            "or a copy of the nightly dump — can impersonate any user.",
            fix="Apply accounts/migrations/0004_hash_api_tokens.py."))
    elif "key_hash" in fields:
        n = APIToken.objects.count()
        unhashed = APIToken.objects.filter(key_hash="").count()
        if unhashed:
            out.append(Check(
                "URS-063", "API tokens are hashed", FAIL,
                f"{unhashed} of {n} tokens have no hash — they cannot authenticate and "
                f"something has gone wrong with the migration.",
                fix="Revoke and reissue them."))
        else:
            out.append(Check(
                "URS-063", "API tokens are hashed", PASS,
                f"{n} token(s), stored as SHA-256 hashes. A stolen database yields no "
                f"usable credential.",
                evidence={"tokens": n}))

    # Never-expiring credentials are the ones nobody gets round to revoking.
    forever = APIToken.objects.filter(expires_at__isnull=True).count()
    if forever:
        out.append(Check(
            "URS-063", "API tokens expire", WARN,
            f"{forever} token(s) never expire. A credential with no end date outlives "
            f"the person, the project and the reason it was issued.",
            fix="Set expires_at when issuing tokens; review the existing ones."))
    return out


def run() -> list[Check]:
    checks = []
    for fn in (check_tls, check_lims_readonly, check_audit_grants, check_fernet,
               check_posture, check_access_and_trail, check_backups, check_known_gaps):
        # getattr, not fn.__name__: the handler must not be the thing that breaks. A
        # probe that dies takes its own name with it, and an error handler that raises
        # while reporting an error hides every check after it.
        name = getattr(fn, "__name__", repr(fn))
        try:
            checks.extend(fn())
        except Exception as exc:  # noqa: BLE001 — one broken probe must not hide the rest
            checks.append(Check("—", name, UNKNOWN, f"probe failed: {exc}"))
    return checks
