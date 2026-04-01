# Architecture

## System Overview

The genealogy agent is a multi-layer application built on [ollama-khonliang](https://github.com/tolldog/ollama-khonliang). It manages family tree data (GEDCOM files), routes user queries to specialist LLM roles, validates responses against tree data, and orchestrates cross-tree matching via consensus voting.

## Layer Diagram

```mermaid
block-beta
  columns 1

  block:Integration["Integration Layer"]
    columns 4
    WS["WebSocket Chat Server"] MCP["MCP Server"] CLI["CLI Tool"] WebUI["Web UI"]
  end

  block:Command["Command Layer"]
    columns 4
    Handler["ResearchChatHandler\n(! commands)"] Intent["Intent Classifier"] RateCmd["/rate"] MentionCmd["@mention routing"]
  end

  block:Agent["Agent Layer"]
    columns 5
    Researcher["ResearcherRole"] FactChecker["FactCheckerRole"] NarratorA["NarratorRole"] MatchAgentA["MatchAgentRole"] Personalities["Personalities"]
  end

  block:QualityLayer["Quality Layer"]
    columns 3
    SelfEval["Self-Evaluator\n(DateCheck, RelationshipCheck)"] ConsensusA["Consensus Voting\n(AgentTeam)"] HeuristicA["Heuristic Learning\n(HeuristicPool)"]
  end

  block:MatchingLayer["Matching Layer"]
    columns 4
    CrossMatcherA["CrossMatcher\n(heuristic scoring)"] MatchAgentB["MatchAgent\n(LLM eval)"] MergeA["MergeEngine"] ImporterA["GedcomImporter\n(sanity + export)"]
  end

  block:KnowledgeLayer["Knowledge Layer"]
    columns 4
    KS["KnowledgeStore\n(3-tier)"] TSA["TripleStore"] Lib["Librarian"] ResearchA["Research Pool\n+ Triggers"]
  end

  block:DataLayer["Data Layer"]
    columns 3
    GedcomA["GedcomTree\n(parser)"] ForestA["TreeForest\n(multi-tree)"] PersonA["Person / Family\nQualifiedPerson"]
  end

  block:Infra["Infrastructure (khonliang)"]
    columns 4
    Pool["ModelPool"] Client["OllamaClient"] RouterA["ModelRouter"] Config["Config"]
  end

  Integration --> Command --> Agent --> QualityLayer --> MatchingLayer --> KnowledgeLayer --> DataLayer --> Infra
```

## Module Map

### Data Layer

| Module | Purpose |
|--------|---------|
| `gedcom_parser.py` | GEDCOM 5.5/5.5.1 parser. `Person` and `Family` dataclasses. `GedcomTree` with search, relationship traversal, ancestor/descendant chains, context building. |
| `forest.py` | `TreeForest` manages multiple named `GedcomTree` instances. `QualifiedPerson` wraps persons with tree provenance. Qualified xrefs (`tree_name:@I1@`) prevent collisions. `load_forest_from_config()` handles backward compat. |

### Agent Layer

| Module | Purpose |
|--------|---------|
| `roles.py` | `ResearcherRole`, `FactCheckerRole`, `NarratorRole` — extend `BaseRole`. Smart context building with session injection and heuristic rule injection via `_effective_system_prompt()`. |
| `match_agent.py` | `MatchAgentRole` — dedicated LLM role for cross-tree person comparison. Produces `MatchAssessment` (verdict/confidence/evidence/conflicts). `MatchVotingAgent` wraps for consensus. |
| `router.py` | `GenealogyRouter` — extends `BaseRouter` with keyword dispatch to fact_checker, narrator, or researcher (fallback). |
| `personalities.py` | 4 genealogy personas (genealogist, historian, detective, skeptic) registered with khonliang's `PersonalityRegistry`. |
| `intent.py` | LLM-based intent classifier with compound intent support. |

### Matching Layer

| Module | Purpose |
|--------|---------|
| `cross_matcher.py` | `CrossMatcher` — heuristic person matching across trees. Weighted scoring: name (40%), date (25%), place (20%), family structure (15%). Surname pre-filtering for performance. |
| `importer.py` | `GedcomImporter` — import with `TreeAnalyzer` sanity checks (date anomalies block, missing data warns). GEDCOM 5.5.1 export with roundtrip fidelity. |
| `merge.py` | `MergeEngine` — merge matched persons with strategies (prefer_target, prefer_source, merge_all). Records provenance as `merged_into` triples. |

### Quality Layer

| Module | Purpose |
|--------|---------|
| `self_eval.py` | `DateCheckRule`, `RelationshipCheckRule` verify LLM claims against tree data. `SpeculationRule`, `UncertaintyRule` detect hedging. Factory: `create_genealogy_evaluator()`. |
| `consensus.py` | `GenealogyVotingAgent` wraps roles for `AgentTeam`. `create_consensus_team()` for response quality. `create_match_consensus_team()` for match disputes (MatchAgent 55%, FactChecker 45%). |

### Integration Layer

| Module | Purpose |
|--------|---------|
| `server.py` | `GenealogyChat` extends `ChatServer`. `build_server()` wires all components. Message flow: /rate -> @mention -> !commands -> intent -> routing -> eval -> consensus -> feedback. |
| `chat_handler.py` | `ResearchChatHandler` dispatches 25+ `!` commands: research, analysis, knowledge, matching, import/export. |
| `mcp_server.py` | `GenealogyMCPServer` — tree tools, forest tools, matching tools, training tools. Stdio + HTTP transports. |
| `web_server.py` | HTTP server for web UI with theme injection. |
| `web/index.html` | Chat interface with tree selector dropdown, import/export buttons. |
| `tool.py` | CLI interface for external LLMs (`genealogy` command). |

## Request Flow

```mermaid
flowchart TD
    Msg["User Message"]

    Msg -->|"/rate N"| FB["FeedbackStore.add_feedback()"]
    Msg -->|"@persona query"| PR["PersonalityRegistry"]
    PR --> BP["build_prompt() + format_response()"]
    BP --> PP["_post_process_response()"]

    Msg -->|"! command"| CH["ResearchChatHandler"]
    CH -->|"!trees / !load / !import"| Forest["TreeForest / GedcomImporter"]
    CH -->|"!scan / !matches / !link"| Match["CrossMatcher / MatchAgent / TripleStore"]
    CH -->|"!merge / !export"| Merge["MergeEngine / GedcomImporter"]
    CH -->|"!lookup / !search / !find"| RP["ResearchPool + Triggers"]
    CH -->|"!gaps / !report / !knowledge"| Analysis["TreeAnalyzer / ReportBuilder / Librarian"]

    Msg -->|"natural language"| IC["Intent Classifier"]
    IC --> SC["Session Context"]
    SC --> GR["GenealogyRouter"]
    GR --> Role["Role.handle()"]
    Role --> SE["Self-Evaluator"]
    SE -->|passed| Caveat["Append caveat"]
    SE -->|high severity| CE["ConsensusEngine.evaluate()"]
    CE -->|disagreement| DO["DebateOrchestrator"]
    SE & CE & DO --> HP["HeuristicPool.record_outcome()"]
    HP --> FS["FeedbackStore.log_interaction()"]
```

## Cross-Tree Matching Flow

```mermaid
flowchart LR
    subgraph Scan["!scan tree_a tree_b"]
        direction TB
        CM["CrossMatcher.scan()"] -->|surname filter + scoring| MC["MatchCandidate list"]
        MC --> Store["Store as possible_match triples"]
        MC -->|top 5| MA["MatchAgentRole.evaluate_match()"]
        MA --> Parse["Parse VERDICT / CONFIDENCE / EVIDENCE"]
        Parse --> Update["Update confidence in TripleStore"]
    end

    subgraph Link["!link"]
        direction TB
        Upgrade["possible_match → same_as\n(confidence=1.0, user_confirmed)"]
    end

    subgraph MergeFlow["!merge"]
        direction TB
        ME["MergeEngine.merge_person()"]
        ME --> Fields["Field-by-field merge\n(prefer_target / prefer_source / merge_all)"]
        Fields --> Triple["Record merged_into triple"]
    end

    Scan --> Link --> MergeFlow
```

## Storage

All SQLite stores share `data/knowledge.db`:

| Store | Purpose | Key Tables |
|-------|---------|------------|
| `KnowledgeStore` | Three-tier knowledge (axiom/imported/derived) | `knowledge_entries`, `knowledge_fts` |
| `TripleStore` | Semantic facts + cross-tree links | `triples`, `triples_fts` |
| `FeedbackStore` | Interaction logging + user ratings | `agent_interactions`, `training_feedback` |
| `HeuristicPool` | Evaluation outcomes + learned rules | `outcomes`, `heuristics` |

### TripleStore Link Conventions

| Predicate | Usage | Source |
|-----------|-------|--------|
| `same_as` | Confirmed identity match | `user_confirmed`, `match_agent`, `mcp_confirmed` |
| `possible_match` | Candidate awaiting confirmation | `cross_matcher`, `match_agent` |
| `not_same_as` | Explicitly rejected match | `user_rejected` |
| `merged_into` | Post-merge provenance record | `merge_engine` |

## Configuration

All features are config-driven via `config.yaml` with env var overrides:

```yaml
app:
  gedcom: "data/family.ged"         # Single tree (backward compat)
  gedcoms:                           # Multi-tree: name -> path
    toll: "data/toll.ged"
    smith: "data/smith.ged"

ollama:
  models:
    researcher: "llama3.2:3b"       # Fast, stays hot (30m keep_alive)
    fact_checker: "qwen2.5:7b"      # Medium, 5m keep_alive
    narrator: "llama3.1:8b"         # Larger, 5m keep_alive
    match_agent: "qwen2.5:7b"       # Shares model with fact_checker

matching:
  min_heuristic_score: 0.6          # Minimum CrossMatcher score for candidates
  min_agent_confidence: 0.75        # Minimum MatchAgent confidence for auto-link
  max_scan_results: 50

consensus:
  enabled: true
  timeout: 30                        # Seconds per voting agent
  debate_enabled: true
  debate_rounds: 2
  disagreement_threshold: 0.6

training:
  feedback_enabled: true             # Log interactions to FeedbackStore
  heuristics_enabled: true           # Record outcomes, learn rules
```

Environment overrides: `OLLAMA_URL`, `GEDCOM_FILE`, `GEDCOM_FILES` (comma-separated `name=path` pairs), `WS_PORT`, `WEB_PORT`, `APP_TITLE`.

## Public API

From `genealogy_agent/__init__.py`:

**Core**: `GedcomTree`, `Person`, `Family`, `load_config`

**Multi-tree**: `TreeForest`, `QualifiedPerson`, `load_forest_from_config`

**Roles & routing**: `GenealogyRouter`, `ResearcherRole`, `FactCheckerRole`, `NarratorRole`

**Matching**: `CrossMatcher`, `MatchCandidate`, `MatchAgentRole`, `MatchVotingAgent`, `MatchAssessment`

**Import/merge**: `GedcomImporter`, `ImportResult`, `MergeEngine`, `MergeResult`

**Consensus**: `GenealogyVotingAgent`, `create_consensus_team`, `create_debate_orchestrator`, `create_match_consensus_team`

**Evaluation**: `create_genealogy_evaluator`

**Personalities**: `create_genealogy_registry`
