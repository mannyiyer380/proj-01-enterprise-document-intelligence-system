# Enterprise Document Intelligence System (EDIS)

A FastAPI web app for working with business documents (PDF, DOCX, TXT) using LLMs. It can:

- **Chat with documents (RAG):** upload files or load the bundled dataset, then ask questions. Answers cite the document and page they came from.
- **Analyze a document:** extract its title, summary, keywords and language.
- **Compare two documents:** summarize the differences page by page for PDFs, or section by section for long unpaginated files.

The sample dataset in `documents/` contains Indian financial and regulatory documents (RBI, IRDAI, HDFC Life, LIC).

## How it works

| Piece | Implementation |
|---|---|
| API and web UI | FastAPI, static HTML/JS in `app/ui/` |
| LLMs | OpenAI, Groq or Google Gemini through LangChain |
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
├── utils/               # Logging, model provider, exceptions
└── ui/                  # Front-end pages
documents/               # Sample dataset used by "Load Dataset"
data_downloading_script/ # Script that fetched the sample documents
```

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

To use another provider, set `LLM_PROVIDER` / `CHAT_LLM_PROVIDER` to `groq` or `google` and supply `GROQ_API_KEY` or `GOOGLE_API_KEY`. The app checks your settings at startup and tells you if a required key is missing. See [`.env.example`](.env.example) for the common options and [`app/config/config.py`](app/config/config.py) for every setting and its default.

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

## Runtime data

The app writes the following folders at runtime. Both are git-ignored:

- `output/storage/`: user accounts (SQLite), per-session uploads, chat history and Chroma indexes
- `logs/`: structured JSON logs (`edis_*.jsonl`)

Delete `output/` to reset all accounts and sessions.

## Production notes

- `APP_ENV=production` requires `AUTH_MODE=jwt` and a `JWT_SECRET` of at least 32 characters.
- Never commit `.env`. Only `.env.example` belongs in git.
