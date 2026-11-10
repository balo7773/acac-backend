# ACAC Backend (AFIT × Cowrywise Ambassadors Competition)

This repository contains the complete backend infrastructure for the ACAC platform. It powers the candidate registration, AI-assisted essay grading, judge scoring, and admin management.

## 🛠 Tech Stack
*   **Framework:** FastAPI (Python 3.12)
*   **Database:** PostgreSQL 16 (with strict Row-Level Security)
*   **AI Integration:** DeepSeek V4 Pro & Gemma 4 (via a custom unified `httpx` client)
*   **Deployment:** Docker Compose + Caddy (Reverse Proxy)

## 📂 Project Structure
```text
acac/
├── backend/
│   ├── app/
│   │   ├── core/         # Config, Database (RLS), Security (JWT)
│   │   ├── routers/      # API Endpoints (auth, candidates, judges, admin, public)
│   │   ├── services/     # AI clients (DeepSeek, Gemma), PDF processing
│   │   └── main.py       # FastAPI entry point
│   ├── tests/            # Pytest suite (30+ integration tests)
│   └── worker/           # Background worker (AI Jobs, Expired Windows, Shingling)
├── db/                   # SQL Migrations & Seed Data
└── docker-compose.yml    # Container orchestration
