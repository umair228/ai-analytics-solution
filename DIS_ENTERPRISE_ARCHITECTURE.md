# DIS — Enterprise Architecture & Delivery Guide

**Decision Intelligence Suite for laboratories — on-prem, multi-lab, regulated-grade.**
This is the target architecture and roadmap to take DIS from the working prototype
(Mac dev + Ollama on a workstation) to an enterprise production system.

---

## 0. Guiding principle (non-negotiable)
**Accuracy comes from GROUNDING, not from the model's memory.** The LLM never
recalls facts — it **calls validated tools** (NL→SQL, statistics, anomaly, root
cause, forecasting) over the live LIMS and **retrieves** from the document corpus
(ASTM/ISO/SOPs), then **narrates the exact results with citations**. This gives:
- **Accuracy + auditability** — every figure is computed/quoted, with a trace.
- **Validatability** — the deterministic engines are unit-tested with known answers,
  so the analytics can be formally validated (an ISO 17025 / data-integrity
  requirement a black-box LLM cannot meet).
- **Portability** — point it at any lab's DB + docs and it works with no retraining.

Fine-tuning has a narrow, legitimate role (retriever + tool-calling skill), **never
fact memorization** — see §7.

---

## 1. Logical architecture (layers)
```
        ┌──────────────── Experience ────────────────┐
        │ React app: dashboards · assistant · ask-data │
        └───────────────────────┬─────────────────────┘
                                 │ HTTPS / JWT+SSO
        ┌──────────────── Application (DIS) ───────────┐
        │ Django/DRF · Agent orchestrator · Tool registry│
        │ stateless, horizontally scaled (uvicorn+LB)    │
        └───┬───────────────┬───────────────┬───────────┘
            │               │               │
   ┌────────▼──────┐ ┌──────▼───────┐ ┌─────▼─────────────┐
   │ Inference     │ │ Retrieval    │ │ Analytics (det.)  │
   │ vLLM cluster  │ │ pgvector/    │ │ stats · anomaly · │
   │ (OpenAI API)  │ │ Milvus + bge │ │ RCA · forecasting │
   │ AWQ 14B/32B/72│ │ + reranker   │ │ (validated, tested)│
   └───────────────┘ └──────┬───────┘ └─────┬─────────────┘
                            │               │ read-only
                     ┌──────▼───────────────▼──────┐
                     │ Data: per-lab LIMS connectors│
                     │ (MSSQL/Oracle/PG) + semantic  │
                     │ layer + query governance      │
                     └───────────────────────────────┘
   Cross-cutting: SSO/IdP · Secrets (Vault) · Audit/ALCOA+ ·
                  Observability/tracing · CI/CD · Backups/DR
```

---

## 2. Inference layer (the model serving)
- **Production = vLLM** (or TGI / TensorRT-LLM), OpenAI-compatible, on a dedicated
  GPU node. Gives continuous batching, high concurrency, prefix caching, `guided_json`
  for structured tool output. **Ollama is dev-only.** DIS already speaks this via the
  `local` provider — only `LLM_BASE_URL`/`LLM_MODEL` change.
- **Model tier by GPU** (quantized AWQ/GPTQ for serving):

  | Model | VRAM (AWQ) | Use |
  |---|---|---|
  | Qwen2.5-7B | ~8 GB | high throughput / cost-sensitive |
  | Qwen2.5-14B | ~16–20 GB | **recommended default** — strong tool-use + insight |
  | Qwen2.5-32B | ~24–40 GB | best reasoning for complex RCA/insight |
  | Qwen2.5-72B | ~2×48 GB | maximum quality, multi-GPU |

- **Capacity**: size for concurrent users × tokens/sec; KV-cache dominates VRAM at
  high concurrency. Start 1× 48 GB (L40S/A6000/H100); 2× for HA. RTX 5070 (12 GB) = dev.
- **No egress**: the GPU node stays inside the org network → fully air-gapped.

## 3. Retrieval layer (RAG)
- **Vector store**: `pgvector` (you already run Postgres) for moderate scale; **Milvus/
  Qdrant** if corpus → millions of chunks.
- **Embeddings**: `bge-base/large` served as a small GPU/CPU service; **cross-encoder
  reranker** for precision (DIS already does hybrid BM25+dense+RRF+rerank).
- **Ingestion pipeline** (already built in `docsearch`): upload → **stage → review/
  approve → index** (governed corpus, versioned). Schedule re-index; track provenance.
- **Tune retrieval, not the generator** — chunking, hybrid weights, reranker, and an
  optional **domain-fine-tuned embedder** are where accuracy gains live (§7).

## 4. Data layer
- **Read-only** connectors per lab (MSSQL/Oracle/PG) with **dedicated read-only DB
  users**, query **timeouts + row caps** (`DSE_QUERY_MAX_ROWS`), and a table allow-list.
- **Semantic layer**: curated datasets + metadata + join paths (the relationship layer)
  so NL→SQL/agents resolve correctly and never CROSS JOIN blindly.
- **Representative sampling** for heavy analytics (the dataset-127 lesson) + caching.

## 5. Multi-lab (multi-tenancy)
- Tenant model **Org → Site → Lab**; each tenant has its **own connections, datasets,
  KB corpus, RBAC, and audit scope**. Data + retrieval are isolated; the **model is
  shared**. Onboarding a new lab = *register connection + seed datasets + load its
  docs* — **no retraining**. (This is the payoff of grounding.)

## 6. Security & compliance (regulated lab)
- **Air-gapped** on-prem LLM + embeddings (no data leaves the org).
- **AuthN/Z**: SSO via **LDAP/AD / OIDC / SAML**; **RBAC** (the lab-role hierarchy);
  least privilege; per-tenant isolation.
- **Data integrity / ALCOA+ / ISO 17025 / 21 CFR Part 11**: immutable **audit trail**
  of *who asked what, which tools ran, what data + which model/version* (extend the
  existing `AuditLog`); versioned, approval-gated KB; e-signatures where required.
- **Guardrails**: tools are **read-only**; the agent loop is **bounded + traced**;
  **every answer cites its sources**; **refuse/▸"insufficient data"** instead of
  fabricating; human-in-the-loop for any action/write.
- **Secrets** in a vault (rotate keys — the committed-key incident is the lesson);
  **TLS** in transit, **encryption at rest**.

## 7. Where fine-tuning fits (and where it does NOT)
| Tune | Effect | Verdict |
|---|---|---|
| **Embedder/retriever** on lab vocabulary | better RAG recall → better grounding | ✅ do this — real, safe accuracy gain |
| **Tool-calling / NL→SQL** SFT (bigger model, traces) | more reliable orchestration | ✅ optional, **eval-gated** |
| **Generator fact-memorization** | confident hallucination + loops | ❌ never (proven twice) |
Your Unsloth pipeline (train→GGUF/merge→serve) stays in the toolkit for the ✅ rows.

## 8. Evaluation & quality (enterprise QA)
- **Golden eval sets per capability** (NL→SQL, each stat test, anomaly, RCA, forecast,
  doc retrieval) with expected answers → **CI gate** on every model/prompt change
  (extend `eval_astm` / `eval_domain`).
- **Online monitoring**: faithfulness (answer grounded in tool/retrieval output),
  retrieval precision, refusal rate, latency, cost, user feedback (👍/👎), drift.
- **Regression tests** before promoting any model or prompt.

## 9. LLMOps & observability
- **Model lifecycle**: versioned models, eval-gated promotion, A/B, instant rollback
  (swap `LLM_MODEL`/endpoint).
- **Tracing**: per-request trace — prompt, tool calls, retrieved chunks, tokens,
  latency, cost (Langfuse / Phoenix / OpenTelemetry). The agent's evidence trace feeds it.
- **CI/CD + IaC** (Terraform/Ansible); containerized (Docker → K8s); blue-green deploys.
- **Data/RAG ops**: scheduled ingestion, re-index, dataset refresh, freshness SLAs.

## 10. Scale / HA / DR
- ≥2 inference nodes behind a load balancer; **stateless app tier** scales horizontally;
  **Postgres HA** (primary/replica) + pgvector; Redis cache; backups + tested DR.

## 11. Reference on-prem topology
```
Users ─▶ Reverse proxy / LB (TLS, SSO) ─▶ DIS app pods (K8s)
                                              ├─▶ vLLM GPU node(s)  [air-gapped]
                                              ├─▶ Postgres (HA) + pgvector
                                              ├─▶ Lab LIMS (read-only replicas)
                                              └─▶ Object store (KB docs)
   Platform: IdP(AD/OIDC) · Vault · Audit/log store · Observability · Backup/DR
```

## 12. Delivery roadmap
- **Phase A — Foundation (pilot):** GPU server + vLLM (14B/32B AWQ); containerize DIS
  (backend+frontend); wire EGPC tenant (read-only) + SSO + audit; stand up the eval
  suite as a CI gate; pilot with a few users.
- **Phase B — Productionize:** HA, observability/tracing, security hardening, secrets
  vault, validation documentation (for ISO/compliance), runbooks.
- **Phase C — Multi-lab rollout:** tenant-onboarding playbook (connection + datasets +
  KB per lab), per-tenant RBAC/audit, governance.
- **Phase D — Optimize:** embedder fine-tune, optional tool-calling fine-tune
  (eval-gated), caching, cost/latency tuning, capacity scaling.

## 13. Do this now (concrete)
1. **Procure the GPU server** (BusinessWare): target **≥24–48 GB** (L40S / A6000 / H100)
   for 14B–32B + concurrency. Stand up **vLLM** with `Qwen2.5-14B-Instruct-AWQ`.
2. **Containerize DIS** (Docker images for backend + frontend); point `LLM_BASE_URL` at
   vLLM, keep `LLM_PROVIDER=local`.
3. **Lock down data**: read-only DB users per lab, query caps/timeouts, audit on.
4. **SSO + RBAC**: wire AD/OIDC; enforce the lab-role permissions.
5. **Eval gate**: expand `eval_astm`/`eval_domain` into golden sets; run in CI.
6. **Pilot on EGPC**; measure accuracy, latency, faithfulness; iterate retrieval.

> The workstation + Ollama you set up is the perfect **dev/staging** rig and the
> fine-tune lab. Production = vLLM on the GPU server + containerized DIS + the
> grounded agent, per the layers above.

## 14. Implemented deployment artifacts (this repo)
The blueprint above is now backed by concrete, verified files:

| Concern | Artifact | Notes |
|---|---|---|
| Backend image | [Dockerfile](Dockerfile) | py3.13-slim + MS ODBC 18 + spaCy model; gunicorn |
| Frontend image | [../interactive-analytics-dse/Dockerfile](../interactive-analytics-dse/Dockerfile) | multi-stage node→nginx; [nginx.conf](../interactive-analytics-dse/nginx.conf) = SPA + `/api` proxy + static/media |
| Full stack | [docker-compose.yml](docker-compose.yml) | `db` (pgvector/pg16) · `api` · `scheduler` · `web` · `vllm` (`--profile gpu`); validated with `docker compose config` |
| Metadata DB | [db/init-pgvector.sql](db/init-pgvector.sql) | `CREATE EXTENSION vector` on first init |
| Config template | [.env.prod.example](.env.prod.example) | LLM/vLLM, pgvector, read-only LIMS, OIDC, email; preserves `DSE_FERNET_KEY` |
| Read-only LIMS | [db/grant-readonly-mssql.sql](db/grant-readonly-mssql.sql) | `db_datareader` + explicit `DENY` writes |
| SSO | [config/settings/sso.py](config/settings/sso.py) + [config/oidc_backend.py](config/oidc_backend.py) | OIDC, claim→group/role map; no-op unless `OIDC_ENABLED=True` |
| Compliance | [SECURITY.md](SECURITY.md) | ALCOA+ / ISO 17025 / 21 CFR Part 11 control mapping + go-live checklist |
| Accuracy gate | [Makefile](Makefile) (`make ci`) + [.github/workflows/ci.yml](.github/workflows/ci.yml) | django check + analytics/ai/**NL→SQL golden** tests (46), no GPU needed |
| Retriever tuning | [finetune/train_embedder.py](finetune/train_embedder.py) | BGE fine-tune on mined lab pairs (recall@k before/after) — the recommended accuracy lever over LLM-SFT |

**Bring it up:**
```bash
cp .env.prod.example .env.prod      # fill in secrets; generate DSE_FERNET_KEY once
make build && make up               # add ARGS=--profile gpu to run vLLM locally
# or point LLM_BASE_URL at an external vLLM GPU node and omit the gpu profile
```
Status: `manage.py check` clean (dev + prod), 46 tests green, compose config valid.
