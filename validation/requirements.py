"""The User Requirements Specification, and its traceability matrix.

Every traceability matrix ever written in a word processor is wrong within a month.
Someone renames a test, someone deletes one, someone adds a requirement and forgets
the row — and the document keeps asserting a link that no longer exists. An assessor
cannot tell, and neither can the team.

So this one is **code**. Each requirement names the automated tests that verify it, by
import path. ``manage.py qualify`` runs them and refuses to produce a summary if a
named test does not exist. A matrix that cannot rot is the only kind worth having.

This follows the GAMP 5 (2nd ed.) / CSA position that automated tests are legitimate
qualification evidence and are *better* evidence than a screenshot of someone clicking:
they are repeatable, they are dated, and they run again on every change.

Three things this file is scrupulous about:

* **Risk-based.** A requirement is graded HIGH only if its failure could release a
  bad batch, corrupt a record, or leak patient/lab data. Grading everything HIGH is
  the same as grading nothing.
* **Honest about what cannot be automated.** TLS termination, an encrypted backup, a
  read-only database grant on the customer's server — no unit test can prove these.
  They are marked ``manual`` and appear in the summary as OPERATOR TO VERIFY, not as
  a silent pass.
* **Honest about what is not yet decided.** PQ acceptance thresholds are commitments
  the laboratory will be held to in an audit. They are proposed here with reasoning
  and marked SIGN-OFF REQUIRED. They are not invented.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Requirement:
    id: str
    category: str
    statement: str          # what the system SHALL do
    rationale: str          # why a laboratory needs it — the harm if it is absent
    risk: str               # high | medium | low  (GAMP 5 risk-based)
    verified_by: tuple = ()  # import paths of the automated tests that prove it
    manual: str = ""        # non-empty => cannot be automated; this is the procedure
    note: str = ""


# ===========================================================================
# 1. DATA INTEGRITY — the requirements whose failure releases a bad batch.
# ===========================================================================
DATA_INTEGRITY = [
    Requirement(
        id="URS-001",
        category="Data integrity",
        statement="The system shall never modify data in the source LIMS.",
        rationale=("DIS is a decision-support layer; the LIMS remains the system of "
                   "record. A write from an analytics tool is an uncontrolled change "
                   "to a GxP record."),
        risk="high",
        verified_by=(
            "querybuilder.tests.ExportAllTests.test_rejects_writes",
            "ai.tests_texttosql.ValidateSqlTests.test_non_select_rejected",
        ),
    ),
    Requirement(
        id="URS-002",
        category="Data integrity",
        statement=("The system shall not report a censored or non-numeric result "
                   "(e.g. '<0.05', 'ND') as a number."),
        rationale=("A censored result read as zero drags an average down and can turn "
                   "an off-spec batch into a passing one. SQLite's CAST('<0.05' AS "
                   "REAL) returns 0.0, not NULL — this failure was real, and shipped."),
        risk="high",
        verified_by=(
            "semantics.tests_portability.RealSqliteExecutionTests"
            ".test_censored_results_become_null_not_zero",
            "semantics.tests_portability.RealSqliteExecutionTests"
            ".test_mart_cast_has_the_same_guard",
        ),
    ),
    Requirement(
        id="URS-003",
        category="Data integrity",
        statement=("A figure shall have the same value regardless of which supported "
                   "data source it is computed from."),
        rationale=("A laboratory that moves from the LIMS to a replica must not see its "
                   "numbers change. DATEDIFF(hour, …) truncates to whole hours on MSSQL "
                   "while Postgres returns the fraction — the same turnaround tile "
                   "answered differently per dialect."),
        risk="high",
        verified_by=(
            "connections.tests_lakehouse.TurnaroundIsTheSameNumberEverywhereTests",
            "semantics.tests_portability.ShippedSpecIsPortableTests",
            "connections.tests_lakehouse.TheWholeSemanticLayerRendersOnALakehouseTests",
        ),
    ),
    Requirement(
        id="URS-004",
        category="Data integrity",
        statement=("The system shall not certify a sample as conforming unless every "
                   "test ordered against it has been reported and adjudicated."),
        rationale=("A test ordered and never reported is the dangerous case: a "
                   "certificate that lists only the results it happens to find, sees no "
                   "failures, and prints CONFORMS is a batch released without being "
                   "fully tested."),
        risk="high",
        verified_by=("coa.tests.IncompleteNeverPassesTests",),
    ),
    Requirement(
        id="URS-005",
        category="Data integrity",
        statement=("Numerical figures shall be produced by deterministic, unit-tested "
                   "engines and never by the language model."),
        rationale=("A language model that computes can be confidently wrong. The model "
                   "selects and narrates; the validated engine computes."),
        risk="high",
        verified_by=(
            "analytics.tests.DescriptiveTests.test_descriptive_known_values",
            "analytics.tests.QualityTests",
            "analytics.tests_spc.WestgardRuleTests",
            "coa.tests.IssuanceTests"
            ".test_the_document_states_that_no_figure_came_from_a_language_model",
        ),
    ),
]

# ===========================================================================
# 2. ATTRIBUTABILITY & THE AUDIT TRAIL (ALCOA+, 21 CFR Part 11 §11.10)
# ===========================================================================
AUDIT = [
    Requirement(
        id="URS-010",
        category="Audit trail",
        statement="The audit trail shall be append-only.",
        rationale=("An audit trail a user can delete is not an audit trail. This system "
                   "previously denied add and change in the admin but not delete."),
        risk="high",
        verified_by=("core.tests.AuditLogIsAppendOnlyTests",),
        note=("Enforced at the APPLICATION layer. A holder of direct database "
              "credentials can still alter the table — see URS-062."),
    ),
    Requirement(
        id="URS-011",
        category="Audit trail",
        statement="An action that cannot be recorded in the audit trail shall not occur.",
        rationale=("A silently-failing audit write is worse than none: the gap is "
                   "indistinguishable from 'nothing happened'."),
        risk="high",
        verified_by=("core.tests.AuditFailsClosedTests",
                     "core.tests.AuditInputsAreSanitisedTests"),
    ),
    Requirement(
        id="URS-012",
        category="Audit trail",
        statement=("SQL generated by the language model shall be recorded, with its "
                   "content, attributable to the user who caused it."),
        rationale=("An LLM composing SQL against the LIMS is the highest-risk action in "
                   "the product. It previously produced no log entry at all."),
        risk="high",
        verified_by=("ai.tests_governance.AskDatabaseIsAuditedTests",),
    ),
    Requirement(
        id="URS-013",
        category="Audit trail",
        statement=("Records shall survive the deletion of the user account that created "
                   "them."),
        rationale=("Offboarding an employee must not erase the record of what the system "
                   "told them. Conversation.owner previously CASCADEd."),
        risk="high",
        verified_by=("ai.tests_governance.ConversationSurvivesUserDeletionTests",),
    ),
    Requirement(
        id="URS-014",
        category="Audit trail",
        statement=("A change to a record shall record what the record said before it, and "
                   "shall not disclose any secret it changed."),
        rationale=(
            "21 CFR Part 11 §11.10(e): 'record changes shall not obscure previously "
            "recorded information.' The trail said \"Updated data source 'EGPC LIMS'\" and "
            "stopped — an assessor asks what the value was before and there was no answer "
            "anywhere in the system. Editing or deleting a Dataset was not recorded at "
            "all. The second clause is not decoration: the obvious way to implement the "
            "first is to diff the model, which writes DataSource.password into a trail "
            "that is served over REST and copied into every backup. A compliance feature "
            "that leaks credentials is not a compliance feature."),
        risk="high",
        verified_by=("core.tests_audit_trail.SecretsNeverReachTheTrailTests",
                     "core.tests_audit_trail.WhatItUsedToSayTests",
                     "core.tests_audit_trail.ReasonForChangeTests"),
    ),
]

# ===========================================================================
# 3. ELECTRONIC RECORDS & SIGNATURES (21 CFR Part 11 subpart C)
# ===========================================================================
SIGNATURES = [
    Requirement(
        id="URS-020",
        category="Electronic signature",
        statement=("Applying a signature shall require re-authentication of the signer "
                   "(§11.200)."),
        rationale=("Being logged in is not signing. An unattended terminal must not be "
                   "able to release a batch."),
        risk="high",
        verified_by=("coa.tests.SignatureBindingTests.test_signing_requires_the_password",
                     "coa.tests.CoaApiTests.test_a_refused_signature_is_itself_audited"),
    ),
    Requirement(
        id="URS-021",
        category="Electronic signature",
        statement=("A signature shall be invalidated if the record it signed is "
                   "subsequently altered (§11.70)."),
        rationale=("A signature that merely points at a row can be transferred to a "
                   "record that says something else. It must bind to the content."),
        risk="high",
        verified_by=("coa.tests.SignatureBindingTests"
                     ".test_tampering_with_the_content_INVALIDATES_the_signature",),
    ),
    Requirement(
        id="URS-022",
        category="Electronic signature",
        statement=("A signature shall record the printed name of the signer, the date "
                   "and time, and the MEANING of the signature (§11.50)."),
        rationale="A signature with no meaning records that somebody clicked something.",
        risk="high",
        verified_by=("coa.tests.SignatureBindingTests"
                     ".test_an_approved_certificate_carries_a_manifestation",),
    ),
    Requirement(
        id="URS-023",
        category="Electronic signature",
        statement=("A certificate that cannot account for every test ordered shall not "
                   "be approvable."),
        rationale=("Authority does not substitute for a missing result. Refusal must be "
                   "at the door, not a warning that can be clicked past."),
        risk="high",
        verified_by=("coa.tests.SignatureBindingTests"
                     ".test_an_INCOMPLETE_certificate_cannot_be_approved",
                     "coa.tests.CoaApiTests"
                     ".test_an_incomplete_certificate_is_refused_approval_over_the_wire"),
    ),
]

# ===========================================================================
# 4. CONFIDENTIALITY — where the laboratory's data is allowed to go
# ===========================================================================
CONFIDENTIALITY = [
    Requirement(
        id="URS-030",
        category="Confidentiality",
        statement=("Laboratory data shall not be transmitted outside the customer "
                   "network without an explicit, deliberate configuration act."),
        rationale=("Prompts carry sample rows. The provider defaulted to the cloud, so "
                   "a missing .env file would have exfiltrated LIMS records."),
        risk="high",
        verified_by=("ai.tests_governance.EgressBlockedByDefaultTests",
                     "ai.tests_governance.PrivateAddressTests",
                     "ai.tests_governance.ProviderFallbackTests"),
    ),
    Requirement(
        id="URS-031",
        category="Confidentiality",
        statement=("A user shall not be able to read data belonging to another user's "
                   "private knowledge base."),
        rationale="Cross-tenant retrieval leakage in a RAG index is silent and total.",
        risk="high",
        verified_by=("docsearch.tests.test_authz",),
    ),
    Requirement(
        id="URS-032",
        category="Confidentiality",
        statement=("A user shall read the data of their own laboratory, and shall not "
                   "read the data of another laboratory unless it is deliberately "
                   "shared with them or with the organisation."),
        rationale=(
            "This is the access-control question an ISO 17025 assessor actually asks, "
            "and until now the honest answer was that DIS had no laboratory boundary at "
            "all. `visibility` was read by no queryset and no permission class: PRIVATE "
            "and SHARED behaved identically, `User.labs` was decoration, and the eleven "
            "roles all resolved to the same two outcomes — admin saw everything, and "
            "everyone else saw only what was explicitly, individually shared with them. "
            "The rule now has exactly one definition (core.access) and every list, every "
            "object endpoint, every alert and the AI agent are built from it."),
        risk="high",
        verified_by=("core.tests_access.LabScopeTests",
                     "core.tests_access.TheRuleHasOneDefinitionTests"),
    ),
    Requirement(
        id="URS-034",
        category="Confidentiality",
        statement=("System access shall be limited to individuals an administrator has "
                   "authorised."),
        rationale=(
            "Part 11 §11.10(d). POST /api/auth/register/ was open to anyone who could "
            "reach the server, and that was survivable only by accident: a stranger's new "
            "account could see nothing, because visibility was decided entirely by "
            "owner/shared_with and both were empty for them. The moment Visibility.ORG "
            "began to mean something (URS-032), an open registration endpoint became "
            "'read every organisation-wide dataset in the laboratory' — over the "
            "plaintext HTTP the pilot still serves. Registration is now closed by "
            "default, and an attempt to self-register is itself a recorded security "
            "event."),
        risk="high",
        verified_by=("accounts.tests_login_security.RegistrationIsClosedTests",),
    ),
    Requirement(
        id="URS-035",
        category="Confidentiality",
        statement=("Attempts at unauthorised use shall be prevented, detected, and "
                   "reported."),
        rationale=(
            "Part 11 §11.300(d). LoginView recorded a login only `if response.status_code "
            "== 200`, so a FAILED login wrote nothing anywhere: the trail could not "
            "distinguish a quiet night from a night spent guessing passwords. There was "
            "no lockout and no rate limit. All three verbs matter — the rate limit "
            "prevents, the audit entry detects, and the mail to the security contact "
            "reports, because a row in a table nobody opens is not a report."),
        risk="high",
        verified_by=("accounts.tests_login_security.FailedLoginsAreRecordedTests",
                     "accounts.tests_login_security.LockoutTests",
                     "accounts.tests_login_security.ThrottleTests"),
    ),
    Requirement(
        id="URS-066",
        category="Confidentiality",
        statement=("A second authentication factor shall be available to every user, "
                   "requirable by role, and not bypassable by the client."),
        rationale=(
            "Part 11 §11.200(a)(1) asks a signature to use two distinct identification "
            "components, and DIS already has them — an identification code and a password, "
            "re-entered at the point of signing. So MFA is not what makes us compliant. It "
            "is what makes the *password* worth anything after it has been phished, reused, "
            "or read out of somebody else's breach dump, and a laboratory's analysts reuse "
            "passwords like everybody else.\n\n"
            "The TOTP is RFC 6238, implemented here and proved against the RFC's own "
            "published test vectors rather than asserted by naming a package. Four things "
            "make it a control rather than a decoration: the login hands back NO session "
            "while it still owes a second factor (the challenge is a signed blob, not a "
            "JWT); a code cannot be replayed inside its 30-second life; the secret is "
            "encrypted at rest (it cannot be hashed — verifying means recomputing the "
            "HMAC); and the role policy is enforced in MIDDLEWARE. That last one is not "
            "fussiness: it was first written as a DRF permission class in "
            "DEFAULT_PERMISSION_CLASSES and it was *decorative*, because nearly every "
            "viewset here overrides `permission_classes` and an override REPLACES the "
            "defaults."),
        risk="high",
        verified_by=("accounts.tests_mfa.RfcTestVectorTests",
                     "accounts.tests_mfa.TheLoginMustNotHandBackASessionTests",
                     "accounts.tests_mfa.ACodeCannotBeReplayedTests",
                     "accounts.tests_mfa.TheSecretIsNotReadableFromTheDatabaseTests",
                     "accounts.tests_mfa.APolicyNoViewCanDropTests",
                     "accounts.tests_mfa.BackupCodeTests",
                     "accounts.tests_mfa.TurningItOffCostsSomethingTests",
                     "accounts.tests_mfa.EnrolmentTests"),
    ),
    Requirement(
        id="URS-067",
        category="Confidentiality",
        statement=("Sharing a saved view shall never widen access to data."),
        rationale=(
            "A saved view is a *configuration* — a chart type, a group-by, a set of filters "
            "— so it is tempting to treat it as harmless and govern it only by its own "
            "visibility. That would be a hole punched straight through URS-032, and it "
            "would look like a feature.\n\n"
            "Two ways it leaks. The obvious one: a view shared organisation-wide, pointing "
            "at a dataset only one laboratory may read, becomes a key to that data. The "
            "subtler one: a view's NAME is content. 'Crude Distillation — off-spec batches' "
            "tells an analyst in Lubricants that the dataset exists and what is in it, "
            "without them ever opening it.\n\n"
            "So a view is visible only when the user may see BOTH the view and the dataset "
            "behind it, you cannot save a view of a dataset you cannot see, and widening a "
            "share is recorded in the audit trail with a before/after (URS-014) like any "
            "other controlled change."),
        risk="high",
        verified_by=("datasets.tests_saved_views.SharingAViewMustNotWidenAccessToDataTests",
                     "datasets.tests_saved_views.TheViewStaysTheThingYouSharedTests",
                     "datasets.tests_saved_views.WideningAShareIsAudited"),
    ),
    Requirement(
        id="URS-036",
        category="Confidentiality",
        statement=("The system's own security posture shall be readable only by "
                   "administrators."),
        rationale=(
            "Making the Installation Qualification visible in the product is an honesty "
            "feature, and honesty features have a way of becoming reconnaissance "
            "endpoints. Preflight is a plain-language inventory of this deployment's "
            "weaknesses with the remediation attached — 'DIS connects to this LIMS as "
            "sa, the database SUPERUSER'; 'the application is serving over plaintext "
            "HTTP'. Every authenticated user does not need that, and on a system that "
            "until recently would hand an account to anyone who asked, they must not have "
            "it. The qualification record (what the BUILD proves) is open to all; the "
            "posture (what THIS BOX is currently weak at) is not."),
        risk="high",
        verified_by=("validation.tests_api.PostureIsForAdministratorsOnlyTests",),
    ),
    Requirement(
        id="URS-045",
        category="Accuracy",
        statement=("A user shall be able to record whether an answer was right, and no "
                   "such verdict shall change what the system is graded against until a "
                   "second person with authority has reviewed it."),
        rationale=(
            "The product had NO signal on whether it was correct. It could have been "
            "confidently wrong for a year and the only way anyone would find out is a "
            "customer noticing in a meeting. But the fix is dangerous in exactly the way "
            "the product is: a mistaken thumbs-down, promoted straight into the golden "
            "set, becomes a permanent false fact that every future accuracy measurement "
            "is graded against. So a verdict is EVIDENCE, never truth — a thumbs-up "
            "promotes nothing, a complaint must be upheld by someone other than the "
            "complainant, and only then can it become a test case. The reviewer's name "
            "goes on it."),
        risk="high",
        verified_by=("ai.tests_feedback.ATumbsUpIsNotGroundTruthTests",
                     "ai.tests_feedback.ReviewTests",
                     "ai.tests_feedback.GivingAVerdictTests",
                     "ai.tests_export_golden.ExportGoldenTests"),
    ),
    Requirement(
        id="URS-046",
        category="Accuracy",
        statement=("A certified metric reported as wrong shall raise an incident, not a "
                   "queue item."),
        rationale=(
            "A certified metric is a hand-written, deterministic SQL definition that the "
            "router prefers over the language model, that dashboards, alerts and "
            "certificates of analysis are built on, and that the UI renders with a ✓ "
            "Certified badge telling the user to trust it above the AI. If that "
            "definition is wrong, it is wrong EVERYWHERE, for everyone, silently, and "
            "with our endorsement. A wrong text-to-SQL answer is one bad answer to one "
            "person — the router exists precisely to keep the important questions away "
            "from the model. When somebody tells us the safe path is the broken one, the "
            "asymmetry runs entirely the other way."),
        risk="high",
        verified_by=("ai.tests_feedback.ACertifiedMetricBeingWrongIsAnIncidentTests",),
    ),
    Requirement(
        id="URS-048",
        category="Accuracy",
        statement=("Every answer shall state how far it can be trusted, in terms of facts "
                   "the system actually holds — and shall never state a confidence "
                   "percentage."),
        rationale=(
            "The product rendered 'Generated SQL — validated before it ran' on every "
            "generated answer. That is true, and it is heard as 'the answer is right'. It "
            "means read-only parse, table allow-list, sanity checks — not one of which says "
            "the query answered THE QUESTION THAT WAS ASKED. A model that writes a "
            "perfectly valid `SELECT avg(density)` when you asked about sulphur passes "
            "every gate we have, first time, with no warnings.\n\n"
            "Meanwhile the runner was already counting how many times the model's SQL "
            "failed validation and had to be corrected, and already collecting the "
            "post-execution sanity violations — and the envelope THREW BOTH AWAY. An answer "
            "the model got right first time and one it got wrong twice reached the screen "
            "identical. The confidence signal was never missing; it was being discarded.\n\n"
            "The second clause is not decoration. A percentage here would be a "
            "confidently-wrong number wearing the costume of rigour — precisely the thing "
            "this entire product exists to eliminate. So: a level, with reasons, each of "
            "which is a fact, and no model scoring itself."),
        risk="high",
        verified_by=("ai.tests_confidence.TheEvidenceWeWereThrowingAwayTests",
                     "ai.tests_confidence.ACertifiedAnswerIsNotAGeneratedOneTests",
                     "ai.tests_confidence.ThereIsNoFakePercentageTests",
                     "ai.tests_confidence.TheEnvelopeActuallyCarriesItTests"),
    ),
    Requirement(
        id="URS-049",
        category="Accuracy",
        statement=("When the system cannot answer, it shall say so — and shall name the "
                   "questions it CAN answer exactly."),
        rationale=(
            "A refusal that stops at 'I don't know' is a dead end, and in front of a "
            "customer it is an embarrassment. A refusal that names the certified metrics "
            "available on that data source — approved SQL, the model writes none of it — is "
            "the product's whole argument, made at the moment it is most persuasive. It is "
            "also the honest answer: we are not guessing, and here is exactly where we are "
            "not guessing."),
        risk="medium",
        verified_by=("ai.tests_confidence.WhenWeDoNotKnowWeSaySoTests",),
    ),
    Requirement(
        id="URS-047",
        category="Accuracy",
        statement=("Accuracy shall be reported per route, and a route nobody has judged "
                   "shall report no accuracy at all."),
        rationale=(
            "'The system is 84% accurate' is a number that means nothing. 'Certified "
            "metrics: 100%. Text-to-SQL: 78%' is an argument — it is the evidence for the "
            "certified layer existing at all, and if certified metrics are NOT measurably "
            "more accurate then the certified layer is ceremony and we should say so. The "
            "second clause matters as much: a route nobody has judged has no accuracy, and "
            "printing 0% or 100% for it would be inventing the very kind of confident "
            "number this product exists to eliminate."),
        risk="medium",
        verified_by=("ai.tests_feedback.AccuracyByRouteTests",),
    ),
    Requirement(
        id="URS-037",
        category="Accuracy",
        statement=("A build that carries no qualification evidence shall say so, and "
                   "shall not present as validated."),
        rationale=(
            "The validation page is the argument for letting an AI near a regulated "
            "laboratory, which makes its worst failure mode a quiet one: a page that is "
            "empty because nothing ever ran, shown to a customer as though it meant "
            "everything passed. An unqualified build and a qualified one must never "
            "render the same. The OQ is therefore an artefact of a specific build — it "
            "names the commit it is evidence about — and its absence is reported in those "
            "words."),
        risk="high",
        verified_by=("validation.tests_api.OqReportTests",
                     "validation.tests_api.TheArtifactNamesItsBuildTests"),
    ),
    Requirement(
        id="URS-033",
        category="Confidentiality",
        statement=("The AI agent shall not be able to reach data its user cannot reach."),
        rationale=(
            "Otherwise 'ask the AI' is the most convenient bypass in the product: a "
            "question answered from rows the REST API would have refused to serve. The "
            "agent's dataset and data-source tools are built from the same core.access "
            "rule as the endpoints, so the two cannot diverge."),
        risk="high",
        verified_by=(
            "core.tests_access.LabScopeTests.test_the_agent_cannot_reach_further_than_its_user",
        ),
    ),
]

# ===========================================================================
# 5. ACCURACY & REPRODUCIBILITY
# ===========================================================================
ACCURACY = [
    Requirement(
        id="URS-040",
        category="Accuracy",
        statement="The same question shall produce the same answer.",
        rationale=("'We re-ran it and got a different number' ends a CSV conversation. "
                   "The Anthropic path previously passed no temperature at all and ran "
                   "at the API default of 1.0."),
        risk="high",
        verified_by=("ai.tests_governance.DeterminismTests",),
        note=("temperature=0 + a fixed seed is the closest a hosted LLM gets to "
              "determinism. The deterministic ENGINES are exactly reproducible "
              "(fixed random_state); the narrative prose is not guaranteed to be "
              "byte-identical. Figures are reproducible; wording may vary."),
    ),
    Requirement(
        id="URS-041",
        category="Accuracy",
        statement=("Statistical process-control rules shall be implemented correctly, "
                   "and shall distinguish a warning from a rejection."),
        rationale=("Westgard 1₂ₛ is a warning. An alert that rejects on it would page "
                   "someone one run in twenty, forever — which is how a QC scheme gets "
                   "switched off."),
        risk="high",
        verified_by=("analytics.tests_spc.WestgardRuleTests",
                     "analytics.tests_spc.NelsonRuleTests",
                     "analytics.tests_spc.LeveyJenningsTests"),
    ),
    Requirement(
        id="URS-042",
        category="Accuracy",
        statement=("A predictive model shall not use information that is unavailable at "
                   "the moment of prediction, and shall declare itself unusable when it "
                   "cannot predict."),
        rationale=("A model trained on the result value scores 0.99 and cannot run on a "
                   "live sample. A ranked list of at-risk samples from a useless model "
                   "looks exactly like one from a good model."),
        risk="high",
        verified_by=("analytics.tests_risk.LeakageTests",
                     "analytics.tests_risk.HonestyTests"),
    ),
    Requirement(
        id="URS-043",
        category="Accuracy",
        statement=("Generated SQL shall be validated before execution and the system "
                   "shall refuse rather than guess."),
        rationale="A confidently wrong number is the principal risk of this product.",
        risk="high",
        verified_by=("ai.tests_texttosql.ValidateSqlTests",
                     "ai.tests_texttosql.SanityGateTests",
                     "ai.tests_texttosql.IdentifierExtractionTests",
                     "semantics.tests.RouterTests",
                     "semantics.tests.EgpcMetricRoutingTests",
                     "semantics.tests_routing.RoutingGoldenSetTests",
                     "semantics.tests_routing.TheDangerousCasesTests"),
    ),
    Requirement(
        id="URS-044",
        category="Accuracy",
        statement=("A certified metric shall NEVER answer a question it cannot honour. "
                   "Router precision shall be 1.0 on the routing benchmark."),
        rationale=("The certified path bypasses the LLM AND the validator, so a wrong "
                   "match is a confidently-wrong deterministic answer carrying a "
                   "'Certified' badge. Asking 'how many results are off-spec LAST MONTH' "
                   "and being handed the all-time total, stamped certified, is the worst "
                   "failure this system can produce. A MISS is cheap — it falls through "
                   "to the validated long-tail. The two are not symmetric, and precision "
                   "is therefore not negotiable."),
        risk="high",
        verified_by=("semantics.tests_routing.RoutingGoldenSetTests",
                     "semantics.tests_routing.TheDangerousCasesTests"),
        note=("This is the only accuracy claim in the product that can be gated in CI: "
              "the router is a pure function of the question and the curated layer, so "
              "it needs no LLM and no database. 64 benchmark cases, 26 of them "
              "adversarial questions the router must REFUSE."),
    ),
]

# ===========================================================================
# 6. PROVENANCE & FRESHNESS
# ===========================================================================
PROVENANCE = [
    Requirement(
        id="URS-050",
        category="Provenance",
        statement="Every answer shall state how current the data behind it is.",
        rationale=("An answer computed from a three-day-stale replica must not be "
                   "indistinguishable from a live one. `freshness` was previously a "
                   "hardcoded string literal."),
        risk="high",
        verified_by=("replication.tests_freshness.FreshnessTests",),
    ),
    Requirement(
        id="URS-051",
        category="Provenance",
        statement=("A replica that has stopped refreshing shall be detected and "
                   "reported, not served silently."),
        rationale=("A silently-failing replication serves stale data with full "
                   "confidence, indefinitely, and nobody finds out."),
        risk="high",
        verified_by=("replication.tests_freshness.FreshnessTests"
                     ".test_an_overdue_table_makes_the_whole_answer_stale_and_warns",
                     "replication.tests_api.LineageApiTests"
                     ".test_lineage_surfaces_a_stale_table"),
    ),
    Requirement(
        id="URS-052",
        category="Provenance",
        statement=("A user shall be able to see, on demand, the exact query that "
                   "produced any figure."),
        rationale="A number you cannot trace is a number you cannot defend.",
        risk="medium",
        verified_by=("ai.tests_envelope.EnvelopeTests",
                     "replication.tests_api.SemanticsApiTests"
                     ".test_detail_shows_both_authored_and_rendered_sql"),
    ),
]

# ===========================================================================
# 7. WHAT NO UNIT TEST CAN PROVE — operator verification (IQ)
# ===========================================================================
MANUAL = [
    Requirement(
        id="URS-060",
        category="Infrastructure (IQ)",
        statement="Transport shall be encrypted (TLS) end to end.",
        rationale=("The pilot ships WEB_PORT=80 and DJANGO_SSL_REDIRECT=False. JWTs, "
                   "passwords and lab results traverse the network in cleartext."),
        risk="high",
        manual=("On the server: terminate TLS at the web tier, set "
                "DJANGO_SSL_REDIRECT=True and DJANGO_SECURE_COOKIES=True in .env.prod, "
                "restart, then confirm that http:// redirects to https:// and that the "
                "session cookie carries the Secure flag. Attach the output of "
                "`curl -sI http://<host>/` and a screenshot of the cookie flags."),
    ),
    Requirement(
        id="URS-061",
        category="Infrastructure (IQ)",
        statement=("The LIMS shall be reached through a database account that has no "
                   "write privilege."),
        rationale=("The application refuses writes; the database grant is the backstop "
                   "if the application is ever bypassed."),
        risk="high",
        manual=("Apply db/grant-readonly-mssql.sql. Then, as the DIS account, attempt "
                "`INSERT INTO dbo.SAMPLE …` and attach the permission-denied error."),
    ),
    Requirement(
        id="URS-062",
        category="Infrastructure (IQ)",
        statement=("The audit trail and the electronic signatures shall be protected "
                   "against alteration at the database layer, not only the application "
                   "layer."),
        rationale=(
            "URS-010 is enforced by the application, and the application is not what "
            "someone holding the database password goes through — that password is a "
            "string in .env.prod. The one record that exists to prove what everybody did "
            "was the one record any of them could rewrite. Signatures are included for the "
            "same reason: one that can be edited after the fact is not a signature.\n\n"
            "This requirement previously prescribed a REVOKE. Running that against a real "
            "PostgreSQL showed it does nothing here — the application connects as a "
            "SUPERUSER (the postgres image makes POSTGRES_USER one) and a superuser "
            "bypasses privilege checks entirely. The control is an append-only TRIGGER, "
            "which refuses the statement whatever the privileges say."),
        risk="high",
        manual=(
            "On the deploy host:  make protect-audit\n"
            "It installs an append-only trigger on core_auditlog and coa_signature and "
            "prints the proof. Then `make preflight`, which checks the same thing from "
            "inside the application. Attach both outputs.\n\n"
            "State the limit honestly in the IQ: a database superuser can still ALTER "
            "TABLE … DISABLE TRIGGER. No database defends against its own superuser. What "
            "changes is that tampering stops being an UPDATE that leaves no mark and "
            "becomes the deliberate dismantling of a named protection. The stronger control "
            "— running the application as a NON-superuser role that does not own these "
            "tables — is the right end state and preflight will keep telling you so."),
    ),
    Requirement(
        id="URS-063",
        category="Infrastructure (IQ)",
        statement=("Stored data-source credentials shall be encrypted with an explicitly "
                   "configured key."),
        rationale=("If DSE_FERNET_KEY is unset the key is DERIVED FROM SECRET_KEY — and "
                   "nothing enforces that it is set."),
        risk="high",
        manual=("Confirm DSE_FERNET_KEY is set in .env.prod to a value that is NOT "
                "derived from SECRET_KEY, and that it is backed up separately from the "
                "database dump it protects."),
    ),
    Requirement(
        id="URS-064",
        category="Infrastructure (IQ)",
        statement=("Backups shall be encrypted, held off-host, and shown to restore."),
        rationale=("A dump of this database is the audit trail, every certificate of "
                   "analysis and its signatures, and the encrypted data-source "
                   "credentials. It was being written unencrypted, to the same host as "
                   "the key protecting the rest of it. Encryption is now enforced by "
                   "backup-db.sh, which refuses to run without a passphrase; the "
                   "off-host copy and the restore proof remain acts an operator must "
                   "perform, and this is the record that they must."),
        risk="high",
        manual=("On the deploy host, run:  BACKUP_PASSPHRASE='…' make verify-backup\n"
                "It decrypts the newest backup, restores it into a THROWAWAY database, "
                "counts the audit/certificate/signature rows that came back, and "
                "re-checks that every signature still binds to its certificate. Attach "
                "its output. Also confirm BACKUP_REMOTE is set: a backup on the same "
                "machine as the database protects you from a DROP TABLE and nothing "
                "else. A backup that has never been restored is a hypothesis."),
    ),
    Requirement(
        id="URS-065",
        category="Infrastructure (IQ)",
        statement=("The deployed software shall be reproducible, and the model answering "
                   "questions shall be the model that was qualified."),
        rationale=(
            "TAG=latest means nobody can say which build is running — and the /validation "
            "page's entire claim is that its Operational Qualification is evidence about "
            "*this build*.\n\n"
            "The model is the sharper half, because it fails silently. LLM_MODEL is a "
            "mutable tag: re-pull it and the weights can change underneath the system while "
            "the OQ, the golden-set results and the accuracy page all carry on describing "
            "the model they replaced. Ollama has no digest-pinned reference, so 'pin the "
            "model' — what this requirement used to say — is advice nobody can follow. The "
            "answerable question is whether it has MOVED, and preflight now FAILS when it "
            "has, because at that point the qualification is not stale. It is void."),
        risk="high",
        manual=(
            "On the deploy host:  make pin\n"
            "It resolves the running images to their registry digests (TAG=latest@sha256:… "
            "— valid in the compose file unchanged) and records the digest of the model "
            "that is actually serving, as DSE_LLM_MODEL_DIGEST. Then `make up && make "
            "preflight`, and attach the output.\n\n"
            "From then on, preflight FAILS if the model's digest moves. If it does, re-run "
            "`make ci` against the new model before trusting anything the system says — or "
            "restore the weights it was qualified with."),
    ),
]

ALL: list[Requirement] = (
    DATA_INTEGRITY + AUDIT + SIGNATURES + CONFIDENTIALITY + ACCURACY
    + PROVENANCE + MANUAL
)


# ===========================================================================
# PERFORMANCE QUALIFICATION — the numbers the laboratory will be held to.
# ===========================================================================
@dataclass(frozen=True)
class PqCriterion:
    id: str
    statement: str
    measured_by: str        # the command that produces the number
    observed: str           # what the system does today
    proposed: str           # what we suggest committing to, and why
    rationale: str
    # Most PQ criteria are a NUMBER the laboratory commits to. Some are not: a QC
    # manager confirming that the flagged runs match their own records is a judgement,
    # and inventing a percentage for it would be false precision.
    needs_number: bool = True
    signed_off: bool = False   # <- YOU set this. It is not ours to set.


PQ: list[PqCriterion] = [
    PqCriterion(
        id="PQ-001",
        statement=("Natural-language questions shall be answered correctly on the "
                   "curated benchmark at a rate of at least [THRESHOLD]."),
        measured_by="python manage.py eval_golden",
        observed=("The golden set holds 21 questions (eval/golden_egpc.jsonl). Run the "
                  "command on THIS deployment to obtain the current figure — quoting a "
                  "number measured on someone else's data would be worthless."),
        proposed=("90% — but only once the golden set is grown to ~40 questions. A "
                  "percentage over 21 items moves 4.8 points per question, which is too "
                  "coarse to be an acceptance criterion."),
        rationale=("This is the headline accuracy claim in the sales conversation. "
                   "Committing to it in a validation document means committing to it in "
                   "an audit."),
    ),
    PqCriterion(
        id="PQ-002",
        statement=("Document retrieval shall place a correct passage in the top-k for at "
                   "least [THRESHOLD] of the reference queries."),
        measured_by="python manage.py eval_astm",
        observed="69-item ASTM/ISO retrieval eval ships with the product.",
        proposed=("Recall@5 ≥ 0.85 on the shipped 69-item eval. Measure it on THIS corpus first: a threshold set on our reference documents says nothing about the laboratory's own SOPs, which are what it will actually be asked about."),
        rationale=("Retrieval is the accuracy lever for document Q&A; the generator "
                   "cannot cite what the retriever did not find."),
    ),
    PqCriterion(
        id="PQ-003",
        statement=("Forecast error on the laboratory's own history shall not exceed "
                   "[THRESHOLD]."),
        measured_by="The Forecast Workbench backtest leaderboard (rolling-origin sMAPE).",
        observed=("The engine auto-selects the best of 10 methods by backtest, so the "
                  "achievable figure depends entirely on the series. Sample volume is "
                  "forecastable; off-spec rate is much noisier."),
        proposed=("Set a threshold PER SERIES, not one global number. A single sMAPE "
                  "target across sample volume and off-spec rate will be either trivially "
                  "met or impossible."),
        rationale=("A forecast accuracy threshold that ignores the series is the kind of "
                   "acceptance criterion that gets waived at the first review, which "
                   "teaches everyone that the criteria are decorative."),
    ),
    PqCriterion(
        id="PQ-004",
        statement=("Control-chart rules shall raise no false rejection on a run that is "
                   "genuinely in control."),
        measured_by="analytics.tests_spc — stable-process cases.",
        observed=("Automated tests assert that a stable process fires no rule under "
                  "Westgard and Nelson."),
        proposed=("Accept the automated result, and additionally run the last 3 months of "
                  "a real QC material through the Levey-Jennings chart with its ASSIGNED "
                  "mean/SD and have the QC manager confirm the flagged runs match their "
                  "own records."),
        rationale=("The unit tests prove the rules are implemented. Only the QC manager "
                   "can confirm they fire on the right runs in this laboratory."),
        needs_number=False,
    ),
]
