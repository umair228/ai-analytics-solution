# Decision Intelligence Suite (DIS) — Handover & Runbook

**AI-Powered Insights for Modern Laboratories.** A fully local, open-source,
agentic analytics platform over LabWare LIMS data. No data leaves the org — it
runs on a local Qwen model via Ollama/vLLM (no Claude/cloud dependency).

---

## 1. What it is (capabilities → MOM)
| MOM requirement | Where it lives |
|---|---|
| Reporting & analytics platform | datasets + dashboards + Advanced Analytics page |
| Comprehensive statistics (ANOVA, t/F/χ², regression, normality, **control charts, Cp/Cpk**, trend) | `analytics/stats_tests.py` (13 tests) |
| Dedicated anomaly detection | `analytics/anomaly.py` (IsolationForest/LOF) + `analytics/predict.py` (z-score/IQR) |
| Root cause (drivers, association rules, clustering) | `analytics/rootcause.py` |
| Expanded forecasting (personnel/time/cost) | `analytics/forecasting_ext.py` + `forecasting/` (NeuralProphet) |
| Contextual intelligence / relationship discovery | `analytics/relationships.py` |
| AI assistant + agentic AI | `ai/agent.py` (17 tools, evidence trace), `ai/providers/` |
| Document search + ASTM/ISO retrieval | `docsearch/` (hybrid BM25+embeddings+reranker) |
| NL→SQL ("Ask Data") | `analytics/semantic.py`, `docsearch/nlp_sql.py` |

## 2. Stack
- **LLM (local):** `qwen2.5:14b-instruct` via Ollama (7B fallback). Swappable to vLLM. Provider abstraction in `ai/providers/` (`LLM_PROVIDER=local`).
- **Embeddings/rerank:** MiniLM + cross-encoder (sentence-transformers).
- **ML/stats:** scipy, statsmodels, scikit-learn, mlxtend, NeuralProphet — all pinned under **numpy<2** (see `requirements.txt` + `.docsearch-constraints.txt`).
- **Backend:** Django 5.2 + DRF (`ai-analytics-solution`). **Frontend:** React 19 + Vite + ECharts (`interactive-analytics-dse`).
- **Data:** live SQL Server **EGPC_DEV** (primary) + SQLite demo warehouses.

## 3. Run it
```bash
# 0. Ollama (local model)
ollama serve &                       # if not already running
ollama pull qwen2.5:14b-instruct     # or 7b-instruct (faster, less RAM)

# 1. Backend
cd ai-analytics-solution
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver 127.0.0.1:8010

# 2. Frontend
cd ../interactive-analytics-dse
npm install && npm run dev           # proxies /api → :8010

# 3. Smoke-tests
cd ../ai-analytics-solution
.venv/bin/python manage.py llm_check
.venv/bin/python manage.py agent_run "Which product has the worst out-of-spec rate?" --dataset 128
```
Key pages: `/analytics` (Advanced Analytics), `/assistant` (AI agent + trace), `/ask` (NL→SQL), `/doc-search`, `/dashboards`, `/forecasting`.

## 4. Data setup
- **EGPC connection + datasets/dashboards:** `.venv/bin/python seed_egpc_demo.py` (reads `.env` DB_* → EGPC_DEV; creates 14 datasets + 4 dashboards). Re-run anytime (idempotent).
- **ASTM/ISO into the Knowledge Base:** `.venv/bin/python manage.py build_doc_index --seed --source standards/`
- **Demo logins/roles:** `.venv/bin/python manage.py seed_lab` (admin etc.).

## 5. Config (`ai-analytics-solution/.env`)
| Key | Meaning |
|---|---|
| `LLM_PROVIDER` | `local` (Ollama/vLLM) — keep local for air-gap |
| `LLM_BASE_URL` / `LLM_MODEL` | model endpoint + name (point at the GPU box / fine-tuned model) |
| `LLM_TEMPERATURE` / `LLM_NUM_CTX` | 0.2 / 16384 (tuned for reliable tool-calling) |
| `AGENT_MAX_STEPS` / `AGENT_MAX_TOOL_CALLS` | agent loop bounds (6 / 24) |
| `DB_ENGINE` | `sqlite` (demo warehouse) or `mssql` (live LIMS) |
| `DB_NAME=EGPC_DEV`, `DB_HOST/PORT/USER/PASSWORD/DRIVER` | the EGPC SQL Server |

## 6. AI agent — how to use well
- The agent **plans → calls read-only tools → narrates**, with a full **evidence trace** (auditable) on every answer. The deterministic engines compute the numbers; the LLM only narrates → figures are exact regardless of model.
- **Reliable:** focused questions (a dataset selected in the UI, or `agent_run … --dataset <id>`), the dashboards, and the REST/evals.
- **Caveat on 16 GB Macs:** *unfocused* questions (no dataset; the agent must discover among 50+ datasets) can be flaky because the 14B is memory-pressured. → Use a focused dataset, or run the model on the GPU box. This goes away with more VRAM / the fine-tune.

## 7. Evaluation & tests
```bash
.venv/bin/python manage.py test analytics ai     # 40 unit tests
.venv/bin/python manage.py eval_astm             # ASTM/ISO retrieval (12/12)
```

## 8. Fine-tuning (domain adaptation) — the GPU step
Teach the model EGPC/QC/ASTM specifics so a smaller model matches the 14B in-domain. See **`finetune/README.md`** and **`finetune/SETUP_WINDOWS.md`**.
1. Mac: `.venv/bin/python finetune/prepare_data.py` → `finetune/data/domain_sft.jsonl` (206 grounded examples; expand for a heavier run).
2. GPU box (Windows + RTX 50-series): Unsloth Docker → `python finetune/train_unsloth.py --model unsloth/Qwen2.5-7B-Instruct --gguf q4_k_m`.
3. `ollama create` the GGUF → point `.env` `LLM_MODEL` at it → `eval_domain.py` + `eval_astm` gate (promote only if it wins).

## 9. Known nuances / interpretation
- **"Worst out-of-spec" has two valid lenses:** by RATE (spec-compliance-by-product dataset) → **PROPANE 36%** (low volume); by VOLUME DRIVER (root-cause) → **STEAM** (high volume, ~3.9× base). Cite both.
- EGPC quirks: operators = `RESULT.ENTERED_BY` (not `SAMPLE.ASSIGNED_OPERATOR`, which is empty); `TEST.INSTRUMENT` is empty (instrument-level RCA limited); `PRODUCT_SPEC` limits are varchar (cast with `TRY_CONVERT(float,…)`).
- `RESULT.ENTERED_ON` is concentrated 2022-09→2023-03; drive sample-volume forecasting off `SAMPLE.LOGIN_DATE` (full 3-yr range).
- Three demos are seeded (Water, Refinery, EGPC); for EGPC-only demos use the EGPC datasets (ids 122–135) / the EGPC dashboards.

## 10. Optional refinements (backlog)
- Push the fine-tune set to 400+ (raise caps / teacher-augment).
- Adapt the NeuralProphet forecasting app to the EGPC schema (`DB_ENGINE=mssql`).
- Per-product+unit sulphur SPC dataset; customer-feedback (C_CUST_FEEDBACK) use case.
- Broader **DSE→DIS** rename across code/UI.
- De-clutter to EGPC-only datasets for cleaner unfocused-agent behaviour.

## 11. Security / air-gap
- `LLM_PROVIDER=local` → no outbound LLM calls. Keep the model on Ollama/vLLM in-org.
- The old committed Claude key was scrubbed; `.env` is not git-tracked. Rotate any historical key on the provider side.
