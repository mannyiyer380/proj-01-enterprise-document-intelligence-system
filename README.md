# Enterprise Document Intelligence System (EDIS)

A FastAPI web app for working with business documents (PDF, DOCX, TXT) using LLMs. It can:

- **Chat with documents (RAG):** upload files or load the bundled dataset, then ask questions. Answers cite the document and page they came from.
- **Analyze a document:** extract its title, summary, keywords and language.
- **Compare two documents:** summarize the differences page by page for PDFs, or section by section for long unpaginated files.
- **Custom instructions:** set the language, emphasis or level of detail for an analysis or comparison, and save it as your default.
- **Human review:** approve an analysis or comparison result, or request changes with a note.

The sample dataset in `documents/` contains Indian financial and regulatory documents (RBI, IRDAI, HDFC Life, LIC).

## How it works

| Piece | Implementation |
|---|---|
| API and web UI | FastAPI, static HTML/JS in `app/ui/` |
| LLMs | OpenAI, Groq or Google Gemini through LangChain |
| Workflows | LangGraph graphs for document chat, analysis (sections summarised in parallel) and comparison (pages compared in parallel) |
| Result cache | In-memory `cachetools` cache for analysis and comparison results |
| Tracing | Optional LangSmith tracing |
| Embeddings | `BAAI/bge-small-en-v1.5` via the Hugging Face Inference API, falling back to a local [fastembed](https://github.com/qdrant/fastembed) copy if your HF credits run out. Google embeddings are also supported. |
| Vector store | ChromaDB, one collection per session |
| Chunking | Character-based, 1600 chars with 100 overlap (configurable) |
| Auth | Built-in SQLite accounts (default), no login (`local`), or JWT bearer tokens (`jwt`) |

```
app/
├── main.py              # App factory, middleware, error handlers
├── api/                 # HTTP routes (/api/...)
├── ai_module/           # Ingestion, retrieval, chat, analysis, compare, prompts
├── services/            # Session storage, accounts, workflow orchestration
├── config/config.py     # All settings, read from environment variables
├── utils/               # Logging, model provider, exceptions, LangSmith tracing
└── ui/                  # Front-end pages
documents/               # Sample dataset used by "Load Dataset"
data_downloading_script/ # Downloader for the sample documents, with a Streamlit front end
tests/                   # Pytest suite (model calls are mocked)
.github/workflows/       # CI: flake8 and pytest on every push and pull request
```

## Implementation status

✅ done · ⏳ pending

### Architecture layers

| Layer | Status | What EDIS includes |
| --- | --- | --- |
| UI | ✅ | HTML, CSS and JavaScript. Login, signup and workspace pages. |
| API | ✅ | FastAPI endpoints for upload, chat, analysis, comparison, review and health checks. |
| AI | ✅ | LangChain and LangGraph workflows. OpenAI is the default provider; Groq and Gemini can be selected with `LLM_PROVIDER`. |
| Data | ✅ | Chroma vector index, SQLite for accounts, and local JSON storage for chat history and review decisions. |
| Cloud / deployment | ⏳ | A Dockerfile is available, but the image build has not been verified. AWS ECS Fargate deployment is pending. |
| Monitoring / tracing | ✅ | `structlog` JSONL logs, request IDs and optional LangSmith traces. Infrastructure dashboards and alerts are pending. |
| Evaluation | ✅ | An offline Document Chat benchmark that checks facts, citations, missing evidence and routing. |
| Security | ✅ | Login, password hashing, JWT support, session ownership, upload validation and trace masking. |
| Testing | ✅ | Flake8, pytest and a GitHub Actions workflow on pushes and pull requests. 31 tests pass locally. |

### Features

| Feature | Status | What EDIS includes |
| --- | --- | --- |
| Validation (Pydantic) | ✅ | API schemas, intent routing and structured result validation. |
| Guardrails | ✅ | One retry for an invalid intent, citation ID validation, and a safe response when no evidence is found. |
| Evaluation | ✅ | Seven benchmark cases for Document Chat. |
| MCP client / server | ⏳ | Pending. |
| Human-in-the-loop | ✅ | **Approve** and **Request changes** for Analysis and Compare, with saved review notes. |
| Memory | ✅ | Separate histories for General Chat and Document Chat. Users can resume after a refresh in the same tab, or start a **New Chat**. |
| Caching | ✅ | In-memory `cachetools` cache for Analysis and Compare results. |
| LLM gateway | ⏳ | Pending. The app calls providers directly. |
| Async | ✅ | Upload, dataset loading, analysis and comparison use async API routes. Blocking work runs in a thread pool. |

## Getting started

Requires **Python 3.12**.

```bash
git clone https://github.com/mannyiyer380/proj-01-enterprise-document-intelligence-system.git
cd proj-01-enterprise-document-intelligence-system

python3.12 -m venv env
source env/bin/activate        # Windows: env\Scripts\activate
pip install -r requirements.txt

cp .env.example .env           # then fill in your keys
```

### Configure `.env`

With the default settings you need:

- `OPENAI_API_KEY`, because `LLM_PROVIDER` defaults to `openai`
- `HF_TOKEN`, because `EMBEDDING_PROVIDER` defaults to `huggingface`

The app checks your settings at startup and tells you if a required key is missing. See [`.env.example`](.env.example) for the common options and [`app/config/config.py`](app/config/config.py) for every setting and its default.

#### Choose the language model

`LLM_PROVIDER` picks the model for document chat, analysis and comparison. OpenAI is the default. Add the API key for the provider you pick. Each model variable is optional.

| Provider | `LLM_PROVIDER` | Model setting and default | API key |
| --- | --- | --- | --- |
| OpenAI | `openai` | `OPENAI_CHAT_MODEL=gpt-4.1-mini` | `OPENAI_API_KEY` |
| Groq | `groq` | `GROQ_CHAT_MODEL=qwen/qwen3.8-27b` | `GROQ_API_KEY` |
| Google Gemini | `google` | `GOOGLE_CHAT_MODEL=gemini-3.5-flash-lite` | `GOOGLE_API_KEY` |

Set `CHAT_LLM_PROVIDER` to use a different provider for chat only. For example, `LLM_PROVIDER=openai` with `CHAT_LLM_PROVIDER=groq` uses OpenAI for analysis and comparison and Groq for chat. Leave it blank to have chat use `LLM_PROVIDER`.

You can also set these for a single run in the terminal. Terminal values take precedence over `.env`:

```bash
LLM_PROVIDER=google GOOGLE_CHAT_MODEL=gemini-3.5-flash-lite uvicorn app.main:app --port 8080
```

### Run

```bash
uvicorn app.main:app --reload --port 8080
```

Open <http://localhost:8080>, create an account, then either click **Load Dataset** to index `documents/` or upload your own files.

- Interactive API docs: <http://localhost:8080/docs>
- Health check: <http://localhost:8080/api/health>

Use port 8080 unless you also change `CORS_ORIGINS`.

### Run with Docker

```bash
docker build -t edis .

docker run --rm -p 8080:8080 \
  -v "$PWD/.env:/app/.env:ro" \
  -v edis-data:/app/output \
  edis
```

- `.env` is mounted read-only at runtime and is never copied into the image (see [`.dockerignore`](.dockerignore)).
- The `edis-data` volume keeps accounts, sessions and indexes when the container is recreated. Remove it with `docker volume rm edis-data` to start fresh.
- Then open <http://localhost:8080> as above.

## Custom instructions

Open **Custom instructions** in Analysis & Summary or Compare to set the language, emphasis or level of detail for that run. Choose **Save as my default** to prefill that workflow for your account. Clear the field and save it to reset the default. The document evidence rules and response validation still apply.

## Human review

Analysis and Compare results start as pending review. Choose **Approve** to accept a result, or **Request changes** with a note explaining what needs revising. The review status and note are saved for the user who generated the result. API clients can read the decision with `GET /api/reviews/{review_id}`, using the `X-Review-ID` header from the original result.

## Caching

Analysis and Compare results are cached in memory with `cachetools`. Each entry belongs to one user, lasts up to one hour, and the cache holds at most 100 entries across both workflows. Changing the custom instructions creates a separate entry. A cache hit skips document parsing and model calls. Look for `cache_hit` or `cache_miss` in the logs. The cache is per process, so it is emptied when the app restarts.

## Tests

The **Python CI** workflow in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and pull request. Flake8 checks for syntax errors and Pyflakes findings such as undefined names and unused imports, and pytest runs the test suite with mocked model responses. It needs no API keys.

Run the same checks locally:

```bash
export PYTHON_DOTENV_DISABLED=1 LANGSMITH_TRACING=false LANGSMITH_TRACING_V2=false
python -m flake8 --select E9,F app data_downloading_script tests
python -m pytest -q tests
```

## Chat evaluation

`app/ai_module/evals.py` benchmarks document chat against the providers configured in `.env`. It makes real model and embedding calls, so it uses your API credits.

```bash
python -m app.ai_module.evals
```

It builds a synthetic two-page handbook in a temporary Chroma index. Then it checks answer facts and cited pages, how missing evidence is handled, and whether greetings, follow-ups and an unrelated question skip retrieval. LangSmith tracing is off during the run. Exit code `0` means every case passed, `1` means at least one failed, and `2` means setup failed.

## LangSmith tracing (optional)

Tracing is off by default. To turn it on, add these to your `.env`:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<your-workspace-api-key>
LANGSMITH_PROJECT=<your-tracing-project-name>
# LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com   # only for regional workspaces
```

Restart the app and run a chat or analysis request. Traces keep timings and selected metadata, but inputs and outputs are hidden and raw error text is replaced by a pointer to the application logs. Each trace's `request_id` matches the `X-Request-ID` response header and the JSON logs.

## Downloading more sample documents

The downloader also has a Streamlit front end:

```bash
cd data_downloading_script
streamlit run streamlit_app.py
```

Files are saved to `data_downloading_script/downloads/`, which is git-ignored.

## Runtime data

The app writes the following folders at runtime. Both are git-ignored:

- `output/storage/`: user accounts (SQLite), per-session uploads, chat history and Chroma indexes
- `logs/`: structured JSON logs (`edis_*.jsonl`)

Delete `output/` to reset all accounts and sessions.

## Production notes

- `APP_ENV=production` requires `AUTH_MODE=jwt` and a `JWT_SECRET` of at least 32 characters.
- Never commit `.env`. Only `.env.example` belongs in git.
