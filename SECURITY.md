# DIS — Security & Compliance

Decision Intelligence Suite is a **read-only analytics layer** over laboratory
data (LIMS, documents). It never mutates source systems. This document maps the
implemented controls to the regimes that matter for a regulated lab —
**ALCOA+**, **ISO/IEC 17025**, and **21 CFR Part 11** — and tells an operator
how to configure them.

> Scope note: DIS is a decision-support tool. It surfaces and explains existing
> records; the LIMS remains the system of record. DIS does not generate GxP
> "original data" — it reads it.

---

## 1. Data integrity — read-only by construction

| Layer | Control | Where |
|-------|---------|-------|
| Application | All generated/handwritten SQL is parsed and rejected unless it is a single read-only `SELECT`/`WITH`; `INSERT/UPDATE/DELETE/DDL/… INTO` are blocked. | [querybuilder/executor.py](querybuilder/executor.py) — `assert_read_only` |
| Application | Query builder compiler only ever emits `SELECT`. | [querybuilder/compiler.py](querybuilder/compiler.py) |
| Application | AI agent tools are read-only; the model never writes data and never computes figures itself — it narrates validated tool output. | [ai/tools.py](ai/tools.py), [ai/agent.py](ai/agent.py) |
| Database | Dedicated **read-only DB account** with `db_datareader` + explicit `DENY` on all writes. | [db/grant-readonly-mssql.sql](db/grant-readonly-mssql.sql), `DB_USER=dis_readonly` |
| Result bounds | Row cap (`DSE_QUERY_MAX_ROWS`) and statement timeout (`DSE_QUERY_TIMEOUT_SECONDS`). | [config/settings/base.py](config/settings/base.py) |

Use **both** the app guard and the read-only DB grant — the grant is the
authoritative backstop if the app is ever bypassed.

---

## 2. ALCOA+ mapping (for the DIS audit trail)

DIS records who asked what, when, and what it returned — an audit trail of
*analysis activity* (not a replacement for the LIMS' own GxP audit trail).

| ALCOA+ | How DIS satisfies it |
|--------|----------------------|
| **Attributable** | Every query/insight is tied to an authenticated user + client IP. Writes **fail closed** — an action that cannot be audited does not happen (`AUDIT_FAIL_OPEN=False`). This includes **LLM-generated SQL**, which records both a `QueryRun` and an `AuditLog` entry carrying the question *and* the exact SQL. [core/audit.py](core/audit.py), [ai/tools.py](ai/tools.py) — `_record_ai_sql`. |
| **Legible** | Audit entries are structured JSON (`action`, `target`, `summary`, `detail`) exposed via a read-only admin/API. [core/models.py](core/models.py), [core/views.py](core/views.py) |
| **Contemporaneous** | `created_at` stamped at write time (`auto_now_add`). |
| **Original** | DIS reads the LIMS as source of truth; it never edits it. Tool outputs trace back to the exact SQL (returned in each payload). |
| **Accurate** | Figures come from deterministic, **unit-tested** engines (stats/anomaly/RCA/forecast/NL→SQL), not the LLM. The LLM *selects and narrates*; it never computes. Golden tests gate every change — see [§5](#5-accuracy-gate). |
| **Consistent** | LLM calls are pinned to `temperature=0` + a fixed `LLM_SEED` on **every** provider, so a re-run reproduces the answer. |
| **+ Complete / Enduring / Available** | The audit log is **append-only at the application layer**: the API is a `ReadOnlyModelViewSet` and the admin denies add/change/**delete**. AI conversation history is retained on user deletion (`Conversation.owner` is `SET_NULL`, not `CASCADE`) — offboarding an employee must not erase the record of what the system told them. |

Each of these is a **control, not a feature**, so each is gated by a test an
assessor can read: [core/tests.py](core/tests.py) (audit trail) and
[ai/tests_governance.py](ai/tests_governance.py) (egress, determinism, evidence).

The AI conversation history (`ai/models.py`) additionally preserves the full
question → tool-call → evidence → answer trace for each agent run.

> **Enforced at the database too — run `make protect-audit`.** Append-only in the
> application is a control against the application, and the application is not what
> someone holding the database password goes through.
> [db/protect-audit-trail-postgres.sql](db/protect-audit-trail-postgres.sql) installs an
> **append-only trigger** on `core_auditlog` *and* `coa_signature` (a signature that can be
> edited afterwards is not a signature). `manage.py preflight` verifies it — **URS-062**.
>
> **A `REVOKE` is not the backstop, and it was wrong of this document to say it was.**
> DIS connects to its own metadata database as a PostgreSQL **superuser**, and a superuser
> bypasses privilege checks entirely — the REVOKE simply does not bite. The trigger refuses
> the statement whatever the privileges say; that is what holds the line.
>
> **The honest limit:** a superuser can still `ALTER TABLE … DISABLE TRIGGER`. *No database
> defends against its own superuser.* What changes is that altering the trail stops being an
> `UPDATE` that leaves no mark and becomes the deliberate dismantling of a named protection.
> The real fix is to stop running the application as a superuser — see [§7](#7-known-gaps).

---

## 3. Access control & SSO (OIDC)

Enterprise identity via any OIDC provider (Keycloak / Okta / Azure AD / Auth0).
**Disabled by default** — turning it on requires no code change.

Enable:

1. `pip install mozilla-django-oidc` (left out of the default image; it is the
   only extra dependency).
2. In `.env.prod`:
   ```ini
   OIDC_ENABLED=True
   OIDC_RP_CLIENT_ID=dis
   OIDC_RP_CLIENT_SECRET=...
   OIDC_OP_BASE_URL=https://login.yourorg.com/realms/lab   # Keycloak-style
   # For Okta/Azure/Auth0 set explicit endpoints instead:
   # OIDC_OP_AUTHORIZATION_ENDPOINT=...  OIDC_OP_TOKEN_ENDPOINT=...
   # OIDC_OP_USER_ENDPOINT=...  OIDC_OP_JWKS_ENDPOINT=...
   OIDC_STAFF_GROUPS=lab-admins
   OIDC_SUPERUSER_GROUPS=dis-superadmins
   ```

What it does ([config/settings/sso.py](config/settings/sso.py),
[config/oidc_backend.py](config/oidc_backend.py)):

- Adds the OIDC auth backend (local Django admin remains as fallback).
- Mounts `/oidc/` login/callback/logout routes.
- **Maps the IdP `groups` claim → Django groups** on every login (IdP is the
  source of truth), and grants `is_staff`/`is_superuser` from configurable
  group lists.
- RP-initiated logout back to the IdP end-session endpoint.

When disabled, DIS uses its built-in JWT auth (`djangorestframework-simplejwt`).

---

## 4. Data egress — on-premise, enforced in code

Prompts are not just questions: they carry column statistics **and sample LIMS
rows** ([ai/context.py](ai/context.py)). Sending one to a third party sends lab
data to a third party. So egress is a **deliberate act**, not a default:

- `DSE_ALLOW_EXTERNAL_LLM` defaults to **False**. With it off, the Anthropic
  provider refuses to construct, *and* the local provider refuses an
  `LLM_BASE_URL` that is globally routable — so `LLM_PROVIDER=local` cannot be
  quietly repointed at `api.openai.com` either. "Local" has to mean local.
- `LLM_PROVIDER` defaults to **`local`**, and `get_provider()` falls back to
  `local`. A missing or misspelled `.env` therefore fails to *"local, not
  configured"* — loud and safe — rather than to *"cloud"*, which is quiet and
  exfiltrates.
- The test is *"not globally routable"*: loopback, RFC1918, link-local, IPv6 ULA,
  and the 100.64.0.0/10 CGNAT range Tailscale allocates from (where the on-prem
  GPU box lives). A hostname that resolves to any public address is refused.

See [ai/providers/egress.py](ai/providers/egress.py); gated by
[ai/tests_governance.py](ai/tests_governance.py).

> **Wording matters in a tender.** Do **not** describe this as *"air-gapped"*.
> The inference host sits on a private tailnet reachable from an internet-facing
> server — that is **network-private, not air-gapped**, and a CSV assessor will
> read the word literally. The accurate and equally strong claim is:
> **"on-premise inference; no lab data egress, enforced in code."**

---

## 5. Transport, session & secrets hardening

Enforced in [config/settings/prod.py](config/settings/prod.py): HSTS (1 yr,
preload, subdomains), SSL redirect, secure + HTTP-only session/CSRF cookies,
`SECURE_PROXY_SSL_HEADER` (behind the nginx TLS terminator), `X_FRAME_OPTIONS=DENY`,
`SECURE_CONTENT_TYPE_NOSNIFF`.

> ⚠️ **These are OFF in the shipped pilot config.** `dis-deploy/.env.prod.example`
> ships `WEB_PORT=80`, `DJANGO_SSL_REDIRECT=False`, `DJANGO_SECURE_COOKIES=False`
> and `http://` origins, so the pilot runs **plaintext over the public internet**:
> JWTs, passwords and lab results included. Terminating TLS and flipping both flags
> back to `True` is a **blocker for any production or regulated use**. See
> [§7](#7-known-gaps).

Secrets:
- Stored DataSource passwords are encrypted at rest with **Fernet**
  (`DSE_FERNET_KEY`). **Generate once and never rotate in place** — rotating it
  invalidates every saved connection password. If it is unset, the key is derived
  from `SECRET_KEY` (a dev convenience); production **must** set it explicitly.
- No secrets in the image: everything is injected via `.env.prod` (keep it out
  of version control — see `.env.prod.example`).

---

## 6. Accuracy gate

Correctness is a security property here — a wrong number presented confidently is
the main risk of an analytics assistant. DIS mitigates it structurally:

- Numbers are produced by deterministic engines with **known-answer tests**
  (`analytics/tests.py`, `ai/tests.py` — incl. NL→SQL golden cases).
- Generated SQL passes deterministic gates before it runs — read-only parse, a
  table-name allow-list (which kills hallucinated tables), and post-execution
  sanity checks — and the system **refuses rather than guesses** when it can't
  produce a valid query. [texttosql/validator.py](texttosql/validator.py).
- Retrieval quality is checked by `manage.py eval_astm`; served-model quality by
  `finetune/eval_domain.py`.
- CI (`.github/workflows/ci.yml`, `make ci`) runs the deterministic gate over
  **all apps** on every push — a regression in intent-parsing, computation, or any
  governance control in §2/§4 fails the build.

---

## 7. Known gaps

A control that is claimed but not implemented is worse than one that is absent:
it makes every *other* claim in this document unverifiable.

This table is a **register, not a to-do list**. Rows that have been closed stay in
it, marked ✅ and describing what was actually wrong — you do not delete a closed
finding, you close it, and the next assessor is entitled to see what the system
used to do. Anything **not** marked ✅ is not implemented today and must not be
represented as if it were.

| Gap | Impact | Status |
|-----|--------|--------|
| **No TLS in the shipped pilot config** | Credentials and lab data traverse the network in plaintext. | 🔴 **Blocker for production.** See §5. |
| ~~API tokens stored in plaintext~~ | ✅ **Fixed.** `APIToken` now stores a SHA-256 `key_hash` (the `key` column is **dropped**, not merely ignored), plus a `prefix` to tell tokens apart and an optional `expires_at`. The plaintext is returned by the API **exactly once**, at creation — it previously sat in `fields`, so *every* `GET /api/api-tokens/` handed back every token in the clear while the serializer's own docstring claimed otherwise. Migration `0004` hashes existing tokens in place, so nothing had to be reissued, then deletes the column. `manage.py preflight` re-checks this **structurally** on every run: if `key` ever reappears, the IQ goes red. | ✅ Shipped. |
| **Backups**: encryption enforced; off-host copy and restore-proof are operator acts | A dump of this DB *is* the audit trail, every COA and its signatures, and the encrypted data-source credentials. It was written unencrypted, to the same host as the key protecting the rest of it. | 🟡 **Encryption ✅** — `backup-db.sh` streams `pg_dump \| gzip \| openssl aes-256-cbc (PBKDF2, 200k)` so the plaintext never touches disk, writes a SHA-256 digest, and **refuses to run without `BACKUP_PASSPHRASE`** (which must not live on the host). **Operator still owes:** set `BACKUP_REMOTE`, set `BACKUP_KEEP` to the real retention obligation (the 14 default is a disk-space policy in a retention policy's clothes), run `make verify-backup`, and `make purge-plaintext-backups` to destroy the legacy dumps — *those contain the pre-migration tokens in the clear, and hashing the DB achieved nothing while they are readable.* Tracked as **URS-064**; preflight reports it **UNKNOWN**, never PASS, because a container cannot verify a host file. |
| ~~No lab/site row-level segregation~~ | ✅ **Fixed — and the gap was not what this row said it was.** It claimed `SHARED` meant *every authenticated user, cross-lab*. The truth was the opposite and worse: **`visibility` was read by nothing at all** — not one queryset, not the permission class. `SHARED` meant *nobody*. A user who ticked "Shared" shared with no one; the seeds marked every demo object `shared`, so **26 of 27 seeded users logged in to an empty application** and the AI agent offered a laboratory manager zero data sources — they could not ask a single question. Eleven roles, and only `admin` could see anything. Now: `PRIVATE` / `LAB` / `ORG`, one definition (`core/access.py`), and every list, object endpoint, alert, widget and **agent tool** built from it — an agent that could out-reach its user would be the most convenient bypass in the product. Verified by **URS-032/URS-033** (14 tests), including *Lubricants cannot read Crude Distillation* and *seeing is not editing*. | ✅ Shipped. Role-scoped data access **is** now a claim we can make. |
| ~~Append-only is app-layer only~~ | ✅ **Fixed — and the fix this row prescribed does not work.** `db/protect-audit-trail-postgres.sql` (`make protect-audit`) makes **`core_auditlog` AND `coa_signature`** append-only in the database. **Running it against a real Postgres is what taught me the row was wrong:** a bare `REVOKE` does nothing here, because **DIS connects to its own metadata database as a PostgreSQL SUPERUSER** (the postgres image makes `POSTGRES_USER` one), and a superuser bypasses privilege checks entirely. Preflight would have reported FAIL forever while prescribing a remedy that could never succeed. The control is an **append-only trigger**, which refuses the statement whatever the privileges say — verified against a live superuser: `UPDATE` refused, `DELETE` refused, signature edit refused, `INSERT` still works, and Django's schema migrations are unaffected (DDL is not row-level DML). **URS-062.** | ✅ Shipped. ⚠️ **The honest limit:** a superuser can still `ALTER TABLE … DISABLE TRIGGER`. *No database defends against its own superuser.* What changes is that tampering stops being an UPDATE that leaves no mark and becomes the deliberate dismantling of a named protection. **The real fix is to stop running the app as a superuser** — preflight now reports that as its own FAIL. |
| ~~No electronic signatures~~ | ✅ **Implemented** for Certificates of Analysis (`coa/signing.py`): §11.200 re-authentication at the point of signing, §11.50 manifestation (printed name, date/time, **meaning**), §11.70 record linking via a SHA-256 content hash — change the certificate and every signature on it becomes visibly **INVALID**. A refused signature is itself audited (§11.300(d)). **NB the scope is COAs only**; other records are not yet signable. The docsearch approve/reject flow remains a **knowledge-base curation gate and is NOT an e-signature**. | ✅ Shipped (COA scope). |
| ~~No before/after values in `AuditLog.detail`, no reason-for-change~~ | ✅ **Fixed.** Every change now records `{"changes": {field: {"from": …, "to": …}}}`, deletes keep the whole record that is about to vanish, and a stated `reason` is captured (`X-Change-Reason` or `reason` in the body). Editing or deleting a **Dataset** turned out to be audited *not at all* — that is fixed too. **The redaction is the interesting part:** the obvious implementation diffs the model and writes `DataSource.password` into a trail that is served over REST and copied into every backup, so a compliance feature would have become a credential leak. Secrets are reduced to a keyed marker — *not* a constant `"***"`, because then both sides of the diff compare equal and **a password change would leave no entry at all**, the redaction erasing the one fact it exists to record. **URS-014**, 15 tests. | ✅ Shipped. **Requiring** a reason is off by default (turning it on rejects updates from clients that don't send one); preflight WARNs while it is off — set `DSE_REQUIRE_CHANGE_REASON=DataSource,QueryDefinition,Dataset` for a regulated deployment. |
| ~~Model version is a mutable tag; image is `TAG=latest`~~ | ✅ **Tooling shipped** (`make pin`). It resolves the running images to registry digests — `TAG=latest@sha256:…`, which Docker accepts and which needs **no change to the compose file** — and records the digest of the model that is actually serving. **The old advice, "pin the model by digest", was impossible:** Ollama has no digest-pinned reference. So the question changes from *is it pinned* (it cannot be) to *has it MOVED* — and preflight now **FAILS** when it has, because a re-pulled tag silently swaps the weights while the OQ, the golden-set results and the whole `/validation` page carry on describing the model they replaced. At that point the qualification is not stale, **it is void**. **URS-065.** | ✅ Shipped — **you must run `make pin` on the box**, then `make up && make preflight`. |
| ~~IQ/OQ/PQ are unexecuted drafts; no URS, no traceability matrix~~ | ✅ **Fixed.** The URS and the traceability matrix are now **code** (`validation/requirements.py`), so they cannot rot silently: `manage.py qualify` executes the matrix — **30 requirements, 208 automated tests** — and a requirement whose tests have been deleted or renamed fails the build. (The first version of that rot-detector was itself broken: `build_suite(["does.not.Exist"])` does *not* raise — unittest synthesises a placeholder that counts as one test — so a matrix full of dead links reported OQ PASSED. *A rot detector that cannot detect rot certifies the lie.*) `make ci` gates it, CI publishes the OQ report per commit, and the **image runs its own qualification at build time and carries the result** — so the evidence at `/validation` cannot be evidence about some other build. | ✅ Shipped, **and now visible in the product** (`/validation`). **The 4 PQ acceptance thresholds are still `[DECISION REQUIRED]`** — proposed with reasoning, but they are a business decision and not ours to invent. Until they are signed off, the system is **not validated**. |
| — | **The `/validation` page shows failures as prominently as passes**, and a build with no qualification evidence says so in those words rather than rendering an empty page that a customer could read as "everything passed" (**URS-037**). The **installation posture** — the live list of *this* deployment's weaknesses — is **administrators only** (**URS-036**): an honesty feature handed to every authenticated user is a reconnaissance endpoint. | ✅ Shipped. |
| ~~Validation scope excludes the AI path~~ | ✅ **Fixed.** The matrix now covers exactly the things an assessor opens first: generated-SQL validation before execution (URS-043, 49 tests), the certified-metric router never answering a question it was not asked (URS-044, 11 tests), determinism (URS-040), no data egress (URS-030), cross-user isolation (URS-031), and data-freshness on every answer (URS-050). | ✅ Shipped. |
| ~~No account lockout, no failed-login auditing~~ · **and an OPEN registration endpoint** | ✅ **Fixed**, and the sweep uncovered something worse than the row itself. `LoginView` audited a login only `if response.status_code == 200`, so **a failed login wrote nothing, anywhere** — the trail could not tell a quiet night from a night spent guessing passwords. Meanwhile `POST /api/auth/register/` was `AllowAny` and would **mint an account for anyone who could reach the server.** That was survivable only by accident: a stranger's account could see nothing, because visibility was decided by owner/`shared_with` and both are empty for them. **The moment `Visibility.ORG` began to mean something (URS-032), open registration became "read every org-wide dataset in the laboratory"** — over the plaintext HTTP the pilot still serves. Now: registration is **closed** (`DSE_ALLOW_SELF_REGISTRATION`, default off) and an attempt is a recorded security event; every login attempt is audited (including against usernames that don't exist — that is what enumeration looks like); the account locks after 5 consecutive failures for 15 min; the lockout is **mailed** to the security contact, because a row in a table nobody opens is not a "report"; and login is rate-limited, because §11.300(d) asks for safeguards that *prevent*, not only detect. **URS-034 / URS-035**, 16 tests. | ✅ Shipped. ⚠️ **This changes behaviour on the running pilot**: if anyone signed up through the open endpoint, they now cannot. Check `/admin/accounts/user/` for accounts you did not create. ✅ **MFA is now implemented** — see the row below. |
| ~~No MFA~~ | ✅ **Implemented.** TOTP (RFC 6238), **proved against the RFC's own published test vectors** rather than asserted by naming a package — one fewer package in the supply chain of a system a customer's security team will inventory. Any user can enable it (Profile → Security); `DSE_REQUIRE_MFA_ROLES` makes it mandatory per role. Four things make it a control rather than a decoration: **the login hands back no session while it still owes a second factor** (the challenge is a signed blob, not a JWT — the single way this feature gets built wrong); **a code cannot be replayed** inside its 30-second life; **the secret is encrypted at rest** (it cannot be hashed — verifying means recomputing the HMAC); and **failed codes feed the account lockout**, because a six-digit code is a million guesses. Backup codes are single-use and hashed. **URS-066**, 33 tests. | ✅ Shipped. ⚠️ The role policy is enforced in **middleware**, not a permission class: it was first written as one in `DEFAULT_PERMISSION_CLASSES` and was *decorative*, because nearly every viewset here overrides `permission_classes` and an override **replaces** the defaults. A required-MFA user walked straight into `/api/datasets/`. |

**Scope framing.** ISO/IEC 17025 is the regime that applies to the labs DIS runs
in today (EGPC, EGA). 21 CFR Part 11 applies to the clinical/genomics deployment
and to any pharma expansion. DIS is **not Part 11-capable today** — the gaps above
are the distance.

---

## 8. Operator checklist (before go-live)

- [ ] `DSE_FERNET_KEY` generated and backed up (never rotate in place).
- [ ] `DJANGO_SECRET_KEY` set to a long random value; `DJANGO_DEBUG=False`.
- [ ] `DJANGO_ALLOWED_HOSTS` / `CORS_ALLOWED_ORIGINS` locked to real hostnames.
- [ ] LIMS reached via the **read-only** account (`db/grant-readonly-mssql.sql`).
- [ ] **TLS terminated at the web tier; `DJANGO_SSL_REDIRECT=True` and
      `DJANGO_SECURE_COOKIES=True`.** (Off in the pilot config — see §5/§7.)
- [ ] `DSE_ALLOW_EXTERNAL_LLM=False`; `LLM_BASE_URL` points at the **internal**
      Ollama/vLLM endpoint. Confirm `/api/ai/status/` reports the local model.
- [ ] `AUDIT_FAIL_OPEN=False` (the default).
- [ ] `LLM_TEMPERATURE=0.0` and `LLM_SEED` fixed; **model pinned by digest**.
- [ ] DB-level `REVOKE UPDATE, DELETE ON core_auditlog` applied.
- [ ] OIDC enabled and group→role mapping verified (if using SSO).
- [ ] `make ci` green; `eval_astm` meets the retrieval threshold.
- [ ] Audit log reviewed/exported per your retention policy.
- [ ] §7 reviewed with the customer's QA — no gap misrepresented as a control.
