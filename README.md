# khonliang-genealogy-example

LLM-backed genealogy research tool — example project demonstrating [ollama-khonliang](https://github.com/tolldog/ollama-khonliang) as a dependency.

## Features

- **GEDCOM parser** — loads standard genealogy files (2,067 persons, 605 families tested)
- **3 LLM roles** — researcher, fact checker, narrator with strict grounding rules
- **Intent classifier** — LLM-based skill detection with compound intent support
- **Query parser** — natural language to structured filters ("men from Ohio before 1920")
- **Web search** — DDG + Google + Bing in parallel with relevance filtering
- **API engines** — WikiTree and Geni.com searched alongside web engines
- **Research pool** — threaded researchers with background task queuing
- **Self-evaluation** — validates responses against tree data, flags date mismatches
- **Tree analysis** — dead ends, date anomalies, missing data, gap detection
- **Reports** — person dossiers, knowledge summaries, gap analysis, session reports
- **Knowledge management** — three-tier RAG (axioms, imported, derived from interactions)
- **WebSocket chat server** — web UI + CLI client + tool interface
- **Config-driven** — YAML config for ports, models, themes

## Quick Start

```bash
# Install (requires Ollama running locally)
pip install -e .

# Place your GEDCOM file in data/
mkdir -p data
cp /path/to/your/family.ged data/

# Load and explore
genealogy load data/family.ged
genealogy search data/family.ged "Smith"
genealogy chat data/family.ged

# Start the chat server — GEDCOM path comes from config.yaml or env var
# Option A: edit app.gedcom in config.yaml (see Configuration section below)
# Option B: override on the command line:
GEDCOM_FILE=data/family.ged python -m genealogy_agent.server

# Connect via CLI
python -m genealogy_agent.chat_client

# Or open http://localhost:8766 in a browser
```

## Chat Commands

| Command | Description |
|---------|-------------|
| Regular text | Routes to researcher/narrator/fact_checker |
| `!lookup: name` | Web search across DDG + Google + Bing |
| `!search: query` | General web search |
| `!google: query` | Google search |
| `!fetch: url` | Fetch and extract page content |
| `!fetch ingest url` | Fetch and save to knowledge |
| `!tree: name` | Structured tree data lookup |
| `!ancestors: name` | Ancestor chain |
| `!migration: name` | Migration timeline |
| `!researchwho criteria` | Filter tree + batch web research |
| `!gaps [name]` | Gap analysis (dead ends, anomalies) |
| `!dead-ends name [research]` | Find dead-end ancestors, auto-research |
| `!anomalies` | Find date errors in tree |
| `!report [name]` | Person report or knowledge summary |
| `!report gaps [name]` | Gap analysis report |
| `!session` | Session summary |
| `!ingest title \| content` | Add to knowledge (Tier 2) |
| `!ingest-file path` | Ingest a file |
| `!knowledge` | Show knowledge store status |
| `!axiom` | List/set axioms (Tier 1) |
| `!promote entry_id` | Promote knowledge entry from Tier 3 to Tier 2 |
| `!demote entry_id` | Demote knowledge entry from Tier 2 to Tier 3 |
| `!prune` | Clean up low-quality knowledge |
| `/search query` | Search knowledge base |
| `/rate 1-5` | Rate last response |

## Tool Interface (for external LLMs)

The tool module reads the GEDCOM path from `GEDCOM_FILE` env var (or `app.gedcom` in `config.yaml`):

```bash
GEDCOM_FILE=data/family.ged python -m genealogy_agent.tool summary
python -m genealogy_agent.tool person "Timothy Toll"
python -m genealogy_agent.tool ancestors "Timothy Toll" --generations 4
python -m genealogy_agent.tool migration "Timothy Toll"
python -m genealogy_agent.tool websearch "Roger Tolle"
python -m genealogy_agent.tool query "Who were Timothy's grandparents?"
```

## Configuration

Edit `config.yaml`:

```yaml
server:
  host: "localhost"
  ws_port: 8765
  web_port: 8766

app:
  title: "Genealogy Agent"
  gedcom: "data/family.ged"
  knowledge_db: "data/knowledge.db"
  default_scope: "genealogy"

ollama:
  url: "http://localhost:11434"
  models:
    researcher: "llama3.2:3b"      # Fast model for Q&A
    fact_checker: "qwen2.5:7b"     # Medium model for validation
    narrator: "llama3.1:8b"        # Larger model for narratives

theme:
  primary: "#e94560"
  background: "#1a1a2e"
```

Environment variable overrides: `OLLAMA_URL`, `GEDCOM_FILE`, `WS_PORT`, `WEB_PORT`, `APP_TITLE`.

API credentials (set in environment or `.env`): `GENI_API_KEY`, `GENI_API_SECRET`, `GENI_APP_ID`.

## Architecture

```text
User (browser/CLI/tool)
  → WebSocket Chat Server
    → Intent Classifier (LLM-based skill detection, compound intents)
    → ResearchTrigger (! commands)
    → Router (keyword matching)
    → Session Context (multi-turn coherence via contextvars)
    → Specialist Role (LLM inference)
    → Self-Evaluator (checks dates/relationships against tree data)
      → Auto-queues background research on uncertainty or date mismatch
    → Librarian (auto-indexes responses to Tier 3)
    → Research Pool (DDG + Google + Bing + WikiTree + Geni)
```

**Session context** is maintained per WebSocket connection using `contextvars` for async safety. Each role receives the last 5 exchanges as context, enabling follow-up questions like "tell me more about her parents" without re-specifying names.

**Self-evaluation** runs after every LLM response, checking date claims and relationship claims against the GEDCOM tree. If issues are found, a caveat is appended. If the LLM expressed uncertainty, background research is automatically queued.

**Intent classification** uses a fast LLM (llama3.2:3b) to detect skills from natural language. Supports compound intents like "find John Smith and then write a narrative" which chains lookup + narrator.

## License

MIT
