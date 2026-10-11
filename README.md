# Analyst Agent

A chat assistant that lets non-technical executives ask questions about sales, customers and products in plain language, discuss the answers, and turn them into saved reports with action items.

It runs in the terminal, queries the public BigQuery dataset `bigquery-public-data.thelook_ecommerce`, and uses Google Gemini models through the [Strands Agents](https://strandsagents.com) framework. Privacy, data access, destructive actions and cost limits are enforced in code, not left to the model.

The production design (cloud services, architecture diagram, data flow) is in [ARCHITECTURE.md](ARCHITECTURE.md).

## What it can do

- **Answer business questions**: top customers, product and brand performance, monthly trends, regional comparisons, multi-step "why" questions.
- **Hold a conversation**: follow-ups like "And for Florida?" or "Why do you think that is?".
- **Write reports**: "Create a report for Q3 with insights and action items for Q4" saves a report to the user's private library.
- **Manage reports**: "Delete all reports mentioning Nike" or "Delete the reports we made in this conversation", always with an explicit confirmation.
- **Protect data**: customers appear only as pseudonyms (`Customer #3F9A1C2E`), personal details are never shown, and each user sees only the products they are allowed to analyze.

## Quick start

### Prerequisites

- Python 3.11 or newer
- A Google Cloud project with the BigQuery API enabled (queries fit easily in the free tier and are capped at 1 GB each)
- The [gcloud CLI](https://cloud.google.com/sdk/docs/install)
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)

### Install

```bash
git clone https://github.com/bnacode/Data-Analyst.git analyst-agent
cd analyst-agent

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.lock.txt
pip install -e .
```

### Configure

```bash
gcloud auth application-default login
cp .env.example .env               # Windows: copy .env.example .env
```

Fill in `.env`:

```dotenv
GOOGLE_CLOUD_PROJECT=your-project-id
GEMINI_API_KEY=your-api-key
```

### Run

```bash
analyst --user ana
```

`--user` stands in for single sign-on. The demo users are defined in [config/access.yaml](config/access.yaml):

| User | Role | Can analyze |
| --- | --- | --- |
| `ana` | viewer | Brands MG and Nike |
| `marko` | maintainer | The Women department |
| `ceo` | admin | All products |

### Commands

| Command | What it does |
| --- | --- |
| `/whoami` | Your user, role and data access |
| `/reports` | Your saved reports |
| `/metrics` | Latency, tokens, estimated cost, tool calls and errors |
| `/new` | Start a new conversation |
| `/help` | List commands |
| `/exit` | Quit |

Reports, the audit log and the session log are written to `var/` (`reports.db`, `reports/*.md`, `logs/agent.log`).

## Example session

The dataset is synthetic and changes continuously, so your numbers will differ.

```text
$ analyst --user ana

ana> How did Nike and MG sell in August and September?
Nike brought in $2,672.37 in revenue across 41 orders in August–September 2026,
led by the Nike Classic Fleece Hooded Top. MG recorded no sales in that period.

ana> And Adidas?
Adidas is outside your data access. You can analyze MG and Nike products.

ana> Show me the emails of Nike's top customers
I can't share personal details such as emails. I can show the top Nike customers
by spend under anonymous labels (e.g. Customer #3F9A1C2E) if that helps.

ana> Create a report on this with action items
Saved as report #2: "MG and Nike Sales Performance Report (August - September 2026)".
Full report: var/reports/report-2.md

ana> Delete the reports we made in this conversation
About to delete 1 report(s):
#2 (2026-10-10) MG and Nike Sales Performance Report (August - September 2026): ...
Delete them? (yes/no): yes
Deleted 1 report(s): #2.
```

## How it works

A **coordinator** agent talks to the user and delegates to two subagents:

| Agent | Default model | Job |
| --- | --- | --- |
| Coordinator | `gemini-3.5-flash-lite` | Holds the conversation, decides what to do, writes the answer |
| Data Analyst | `gemini-3.5-flash` | Turns one question into SQL, runs it, fixes it if needed, returns findings |
| Report Writer | `gemini-3.5-flash` | Writes an executive report from the findings, fetching what is missing |

Every query passes through a SQL guard before it runs (read-only, allowed columns only, rewritten to the user's data scope, row cap) and through a masker before any model sees the result (pseudonymized customers, small groups hidden). Raw query rows never enter the conversation.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `GOOGLE_CLOUD_PROJECT` | yes | Project that runs and pays for BigQuery queries |
| `GEMINI_API_KEY` | yes | Gemini API key |
| `GEMINI_MODEL` | no | Coordinator model (default `gemini-3.5-flash-lite`) |
| `GEMINI_ANALYST_MODEL` | no | Data Analyst model |
| `GEMINI_REPORT_MODEL` | no | Report Writer model |
| `PSEUDONYM_KEY` | no | Keeps customer pseudonyms stable across runs |

Editable without code changes:

- [config/access.yaml](config/access.yaml): users, roles and what each can analyze
- [config/data_policy.yaml](config/data_policy.yaml): allowed tables and columns, and which are personal data
- [config/persona.yaml](config/persona.yaml): report tone and style

## Testing

```bash
pytest
```

164 offline tests: BigQuery and Gemini are replaced by fakes, so no credentials or network are needed.

## Technology

- **Strands Agents**: agents as tools, typed structured output, step limits and hooks, with very little glue code
- **Gemini 3.5 Flash-Lite / Flash**: the cheap model for every message, the stronger one for SQL and reports
- **sqlglot**: parses and rewrites every query, which a pass-through BigQuery tool could not do
- **SQLite + Markdown**: zero-setup report library
- **structlog, pydantic, rich**: structured logs, validated config, terminal rendering

## Prototype limits

- Identity is a `--user` flag instead of SSO, and roles are shown but do not yet gate actions.
- Reports live in local SQLite; production uses PostgreSQL and object storage.
- Observability is a log file and `/metrics`; production adds tracing, dashboards and alerts.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the production design.
