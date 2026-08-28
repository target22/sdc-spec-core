# SDC-SPEC Core :: Reference Implementation v3.1

[![Architecture](https://img.shields.io/badge/architecture-declarative_contract-0B0F19?style=flat-square&labelColor=1E293B&color=3B82F6)](#)
[![Validation](https://img.shields.io/badge/validation-O(1)_edge_quarantine-0B0F19?style=flat-square&labelColor=1E293B&color=10B981)](#)
[![License: MIT](https://img.shields.io/badge/license-MIT-0B0F19?style=flat-square&labelColor=1E293B&color=94A3B8)](LICENSE)

Stateless, zero-dependency reference implementation for the **Sovereign Declarative Contract (SDC-SPEC)** framework. Deploys an edge-level data transit firewall to insulate core databases and downstream systems from third-party API schema drift, unescaped payload crashes, and PII leaks.

> This is the public reference shell: a domain-blind kernel plus one domain-neutral example contract pair. It contains no real customer data, credentials, or production business logic — see [SDC-SPEC-INGRESS-v3.1.json](contracts/_reference/SDC-SPEC-INGRESS-v3.1.json) / [SDC-SPEC-EGRESS-v2.0.j2](contracts/_reference/SDC-SPEC-EGRESS-v2.0.j2) for the canonical example.

---

### Architectural Topology

```
INBOUND UNSTRUCTURED CHAOS STREAM (Webhooks / External APIs)
│
▼
[ Stateless FastAPI Proxy Firewall ]
│
┌────────────────┴────────────────┐
│                                   │
(Valid Schema)                (Schema Violation)
│                                   │
▼                                   ▼
[ O(1) Type Coercion ]      [ Automated PII Masking ]
│                                   │
▼                                   ▼
[ Core SSOT Database ]      [ Encrypted Dead Letter Queue ]
```

---

### Core Principles

* **Kernel Immutability** — the execution core is domain-blind. Business logic resides entirely in declarative JSON/Jinja2 contract manifests, never in kernel code.
* **Edge Boundary Control** — every ingress payload is validated against a strict JSON Schema (`additionalProperties: false`) before it reaches core execution.
* **Deterministic Egress** — Jinja2 templates render under `StrictUndefined`, with every value piped through `tojson`, eliminating unescaped-character syntax crashes at the output boundary.
* **Zero Vendor Lock-in** — a containerized Python/FastAPI microservice with no required third-party SaaS runtime.

---

### Repository Layout

```text
sovereign_engine/
├── main.py                     # dumb router — FastAPI app + 4 thin endpoints
├── engine/
│   ├── __init__.py             # dependency-order docstring only
│   ├── logging_setup.py        # dlq_logger, app_logger (leaf)
│   ├── security.py             # secrets, AES-GCM sanitize/decrypt, admin auth (leaf)
│   ├── transforms.py           # StepType, casters, nested_array_flatten (leaf)
│   ├── condition_evaluator.py  # whitelist-AST DECISION_MATRIX interpreter (leaf)
│   ├── template_engine.py      # jinja_env + CUSTOM_FILTERS wiring (leaf)
│   ├── dlq.py                  # DLQ write sink + read_and_decrypt_dlq()
│   ├── contracts.py            # Pydantic envelope + legacy adapter + disk I/O
│   ├── boundary.py             # upload-time contract structural validation
│   ├── payload.py              # HTTP ingress parsing + contract-id match
│   └── executor.py             # DAG walker — _run_step + _execute_dag
├── filters/
│   ├── __init__.py
│   ├── datetime_filters.py
│   └── structural_filters.py
├── Dockerfile
├── requirements.txt
├── contracts/
│   └── {contract_id}/
│       ├── logic_rule_input.json   # SSOT — declarative ingress contract
│       └── logic_rule_output.j2    # SSOT — Jinja2 egress renderer
└── logs/                       # disk-persisted DLQ log (RotatingFileHandler; bind-mount this in production)
```

---

### Quickstart

#### 1. Configure secrets

The kernel fails fast at boot without these — generate real values, don't ship the example ones:

```bash
export DLQ_KEY=$(python3 -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())")
cat > .env << EOF
ADMIN_API_KEY=replace_me_with_a_real_secret
DLQ_ENCRYPTION_KEY=${DLQ_KEY}
EOF
```

#### 2. Run via Docker Compose

```bash
docker compose up -d --build
```

Swagger UI is then available at `http://localhost:8000/docs`.

#### 3. Mount a contract

```bash
curl -X POST "http://localhost:8000/v1/admin/contracts/demo" \
     -H "X-Admin-API-Key: $ADMIN_API_KEY" \
     -F "input_contract=@contracts/_reference/SDC-SPEC-INGRESS-v3.1.json" \
     -F "output_contract=@contracts/_reference/SDC-SPEC-EGRESS-v2.0.j2"
```

#### 4. Execute against it

```bash
curl -X POST "http://localhost:8000/v1/execute/demo" \
     -H "Content-Type: application/json" \
     -d '{"data": {"id": "rec_9021", "status": "OPEN", "owner_id": "u_1042"}}'
```

---

### Enterprise SLA & Custom Implementations

This repository is the open-source reference shell. For custom Declarative Contract design, managed proxy deployments, or infrastructure risk audits:

* **Architecture audits:** direct message via LinkedIn
* **Specification framework:** see the pinned SDC-SPEC v3.1 document on LinkedIn

---

### License

MIT — see [LICENSE](LICENSE).
