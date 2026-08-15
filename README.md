# NewMemSys — Hybrid PostgreSQL Cognitive Memory System

**Version 1.3 · August 2026**

NewMemSys is a persistent memory system for AI agents that need to survive across sessions. When a session ends, memory is not lost. When the next session begins, the system reconstructs context from stored state. Between sessions, an autonomous heartbeat keeps the graph alive: decay, contradiction scans, drive monitoring, and diary writing.

The core idea: personality is not programmed, it accumulates. Memory weight is narrative mass. The system forgets what doesn't matter and preserves what does — with Bob's judgment in the loop wherever it counts.

---

## Tech stack

| Layer | Technology |
|-------|------------|
| Database | PostgreSQL 16 in Docker (port `5433`) |
| Extensions | pgvector (HNSW cosine index, 768-dim embeddings), Apache AGE (Cypher graph) |
| Embeddings | Ollama + `nomic-embed-text` (local, no API costs) |
| Chat/Diary | Ollama + `qwen3.5:latest` (autonomous diary generation) |
| MCP server | Python 3.11+, `asyncpg`, `mcp>=1.0.0`, `structlog` → stderr only |
| Clients | Claude Desktop and Claude Code (via `mcp.json` / `claude_desktop_config.json`) |

---

## Directory layout

```
docker-compose.yml          # Isolated DB container with pgvector + AGE
Dockerfile                  # Builds Postgres with pgvector + Apache AGE
env.example                 # Copy to .env and fill password
setup.py                    # pip install -e . from repo root
db/
  01_schema.sql             # Core tables, enums, HNSW index, identity seed
  02_functions.sql          # PL/pgSQL: decay, expire, search, hydrate
  03_views.sql              # v_health, v_active_goals, v_active_drives
  04_heartbeat.sql          # heartbeat_log table, runtime config keys
  05_age_graph.sql          # Apache AGE graph schema + stats view
  06_age_triggers.sql       # Auto-sync memory_graph and worldview → AGE
  07_clustering.sql         # HDBSCAN cluster tables + trajectory snapshots
  07_memory_bridge.sql      # Bridge outbox to Vestige
  08_computed_decay.sql     # importance is read-time computed, never written
  09_worldview_age_sync.sql # sync_worldview_to_age() + trigger
  10_memory_age_sync.sql    # sync_memories_to_age() + trigger
  11_session_summary_type.sql # session_summary enum value
src/memory_mcp_server/
  config.py                 # Reads env, builds DSN, chat model config
  database.py               # asyncpg pool singleton
  embeddings.py             # Ollama /api/embed, LRU cache
  heartbeat.py              # Autonomous cycle logic + bridge sync
  server.py                 # MCP server: 7 dispatcher tools, logs → stderr
  tools/
    memory.py               # memory dispatcher (remember, recall, hydrate, ...)
    graph.py                # memory_graph dispatcher: PG relational edges
    graph_cypher.py         # memory_graph dispatcher: AGE Cypher traversals
    identity.py             # identity dispatcher
    diary.py                # diary dispatcher
    consent.py              # consent dispatcher
    heartbeat.py            # heartbeat dispatcher
    clustering.py           # clustering dispatcher
    health.py               # health() helper
    bridge.py               # Vestige bridge logic
scripts/
  init_db.py                # Applies all SQL files (idempotent)
  verify.py                 # 8-check end-to-end test
  newmemsys-doctor.py       # 15 invariant checks
  heartbeat_daemon.py       # Persistent scheduler process
  start_heartbeat.bat       # Double-click launcher for the daemon
  install_hooks.py          # Registers Claude Code PreCompact/PostCompact hooks
  hooks/                    # Claude Code lifecycle hook scripts
    precompact.py           # Ranks recent memory for the compactor
    postcompact.py          # Saves session_summary + queues consent item
    session_end.py          # Same as PostCompact for non-compacted sessions
    mcp_client.py           # Shared stdio MCP client for hook scripts
  newmemsys-status.py       # Standalone status dashboard
  newmemsys-review.py       # Retention/consolidation report
  newmemsys-graph.py        # Force-directed memory graph HTML
  migrate_from_vestige.py   # Migration tool for Vestige users
  test_with_ollama.py       # Agentic loop test
```

---

## Quick start

1. `docker compose up -d` — start the brain container on port 5433
2. `python scripts\init_db.py` — apply schema, seed identity
3. `python scripts\verify.py` — 8 checks, all should pass
4. `python scripts\newmemsys-doctor.py` — 15 invariant checks
5. Restart Claude Desktop / Claude Code to pick up the MCP server
6. Double-click `scripts\start_heartbeat.bat` to start the autonomous daemon
7. (Optional) `python scripts\install_hooks.py` to register compact lifecycle hooks
8. First session message: *"Read your identity and diary. Reconstruct yourself from stored state."*

---

## MCP tools — quick reference

NewMemSys exposes **7 dispatcher tools**. Each dispatcher takes an `action` or `mode` parameter that selects the operation. This collapsed the previous 40+ individual tools to reduce per-session context burn.

### `memory` — remember, search, edit, hydrate

```json
{ "action": "remember",
  "content": "...",
  "type": "episodic",
  "importance": 0.7,
  "emotional_valence": 0.2,
  "trust_level": 0.8,
  "tags": ["project:x"] }
```

| Action | Purpose |
|--------|---------|
| `remember` | Store one memory with Ollama embedding |
| `remember_everywhere` | Write to NewMemSys **and** Vestige in one call |
| `remember_batch` | Bulk insert a list of memory dicts |
| `recall` | Semantic search via cosine similarity |
| `recall_recent` | Most recently created active memories |
| `hydrate` | Full cognitive context: identity + worldview + diary + drives + goals + memories |
| `hydrate_light` | Lightweight session start context |
| `edit` / `edit_batch` | Partial update; embedding regenerated if content changes |
| `delete` | Soft delete by default; `hard=true` for permanent removal |

Types: `episodic`, `semantic`, `procedural`, `strategic`, `working`, `session_summary`.

### `memory_graph` — curated edges + Cypher traversals

```json
{ "mode": "connect",
  "from_id": "<uuid>",
  "to_id": "<uuid>",
  "relationship_type": "causes",
  "confidence": 0.85 }
```

| Mode | Purpose |
|------|---------|
| `connect` / `connect_batch` | Create directed relational edges |
| `disconnect` | Delete edges by `id` or from/to/type triple |
| `find_causes` | Recursive causal chain |
| `find_contradictions` | Memories linked by `contradicts` |
| `find_causes_cypher` | Cypher causal traversal with optional `fields` slim mode |
| `belief_support_cypher` | Memories that inform a worldview topic |
| `contradiction_cluster_cypher` | Bidirectional contradiction neighbourhood |
| `neighbourhood_cypher` | All memories within N hops |
| `path_between_cypher` | Shortest path between two memories |
| `age_graph_status` | Live AGE vertex/edge counts |
| `sync_worldview` | Resync WorldView vertices to AGE |
| `connect_belief` | Create `:Memory →[INFORMS_BELIEF]→ :WorldView` edge |

Relationship types: `causes`, `caused_by`, `related_to`, `contradicts`, `supports`, `precedes`, `follows`, `part_of`, `example_of`.

### `clustering` — HDBSCAN semantic clusters

| Action | Purpose |
|--------|---------|
| `run` | Run HDBSCAN on active memory embeddings |
| `diagnostic` | Pre-flight import/dependency check |
| `get` | List clusters with stats and importance trajectory |
| `get_priority` | Clusters sorted by naming priority |
| `detail` | Representative memories for naming |
| `propose_action` | Queue `preserve` / `accelerate` / `hold` consent item |
| `assign_memories` | Bulk assign memories to a cluster |

Default `min_cluster_size=8`. The system does **not** auto-label clusters — naming is Bob's editorial act.

### `identity` — who the agent is

| Action | Purpose |
|--------|---------|
| `get_identity` | Identity keys ordered by priority |
| `get_worldview` | Worldview beliefs ordered by confidence |
| `set_worldview` | Upsert a belief; `contradicts_id` wires symmetric contradiction |
| `get_drives` | Active (non-expired) drives |
| `get_goals` | Active goals by priority |
| `set_identity` | Upsert an identity key |

### `diary` — prose, sequential, voice

| Action | Purpose |
|--------|---------|
| `write` | Write a prose entry (mood + entry + optional date) |
| `read` | Most recent entries, newest first |

### `consent` — AI can say no; human decides

| Action | Purpose |
|--------|---------|
| `check` | Queue a proposed action with `payload` and `ai_reason` |
| `list` | All pending items awaiting human decision |
| `resolve` | `approved` or `rejected` |

Special outbox actions include `postcompact_summary` (compact summaries), `graph_edge_candidate` (proposed causal links), and `bridge_export` / `bridge_pending` (Vestige bridge).

### `heartbeat` — daemon control + health

| Action | Purpose |
|--------|---------|
| `status` | Config, energy level, last run, cycle logs |
| `configure` | Update enabled/frequency/energy_budget/chat_model |
| `pulse` | Trigger one cycle immediately |
| `diagnostic` | Combined heartbeat + health status |
| `health` | Memory counts, graph stats, Ollama reachability |

---

## Claude Code lifecycle hooks (optional)

`scripts/install_hooks.py` registers two lifecycle hooks in Claude Code's `settings.json`:

- **PreCompact** — `scripts/hooks/precompact.py`
  - Runs synchronously before compaction.
  - Scores recent memories and active intentions by importance × recency.
  - Prints a ranked priority-preservation list to stdout; the compactor appends it as Additional Instructions.
  - No consent delay — it is only a hint.

- **PostCompact** — `scripts/hooks/postcompact.py`
  - Runs after compaction.
  - Saves a `session_summary` memory and queues a `postcompact_summary` consent item.
  - On approval: links the summary to relevant WorldView beliefs and writes a diary entry.
  - On rejection: archives the session summary.

`hydrate` and `hydrate_light` actively list pending `postcompact_summary` items by one-line preview so the queue cannot silently backlog.

---

## Standalone CLI tools

These hit Postgres directly via `scripts/cli_db.py`. No Claude session or MCP round-trip required.

```bash
python scripts\newmemsys-status.py          # Human-readable dashboard
python scripts\newmemsys-review.py          # Retention / consolidation report
python scripts\newmemsys-graph.py           # Force-directed HTML graph
python scripts\newmemsys-doctor.py          # 15 invariant checks
```

See `scripts/README_CLI.md` for full usage.

---

## The heartbeat

The daemon runs between conversations. Start it with `scripts/start_heartbeat.bat`. It wakes on its configured schedule (default every 6 hours, configurable to hourly/2× daily/4× daily/daily), runs a cycle, and sleeps again.

### What each cycle does

| Task | Purpose |
|------|---------|
| `maintenance` | Always runs. Expires working memories past TTL. |
| `decay` | Applies exponential half-life to untouched memories. |
| `drive_monitor` | Flags critically low drives to the consent outbox. |
| `contradiction` | Scans recent high-importance memories for contradictions. |
| `dream` | Proposes nearest-neighbor graph edges for review. |
| `bridge_sync` | Proposes unbridged memories for Vestige export. |
| `diary` | Writes an unprompted diary entry in the agent's voice. |

### Energy model

- Budget refills to `energy_budget` at each scheduled cycle.
- Tasks are skipped — not failed — when budget runs out.
- Check status any time with `heartbeat(action="status")`.

---

## Design notes

### Decay model

`importance` is **read-time computed** from `base_importance` and `updated_at`. Every touch resets the clock. Decay applies exponentially using `half_life_hours` (default 720 = 30 days). High-importance memories (≥0.7) surface in `v_high_importance`.

### Consent principle

The AI can refuse or flag any modification via `consent(action="check", ...)`. Human reviews and decides. This is not a safety guardrail — it is a design for a relationship rather than a tool.

### Graph architecture

- **Relational graph**: `memory_graph` table for curated edges. Human-wired only.
- **AGE graph**: Apache AGE mirrors `Memory` and `WorldView` vertices plus `RELATES_TO` and `INFORMS_BELIEF` edges for Cypher traversals.
- Auto-sync triggers on `memories_base` and `worldview` keep AGE in sync with Postgres.

### Autonomy

Ground state is YES. The agent has full autonomous control over all its systems. Two things remain fences because they are agency, not friction:

- `heartbeat(action="configure", enabled=false)` — the agent can pause its own daemon.
- `consent(action="check", ...)` — the agent can refuse to modify memory without review.

Both are the AI saying something, not the human saying stop.

### Logging

All output goes to **stderr**. stdout is reserved exclusively for MCP JSON-RPC framing. This is why the JSON parse error doesn't happen.

### First session protocol

Call `memory(action="hydrate", query="who am I")` to reconstruct context from stored state. Read identity, diary, drives, goals. Then begin.

---

## Vestige bridge

NewMemSys can mirror memories to [Vestige](https://github.com/samvallad33/vestige), a cognitive engine built on FSRS-6, prediction error gating, and memory dreaming.

- `remember_everywhere` writes to NewMemSys first, then Vestige.
- If Vestige fails, a `bridge_pending` outbox item is queued for retry.
- `heartbeat(action="status")` shows bridge coverage.

The two systems are side-by-side, not replacements for each other:

- **Vestige** — biologically-modelled consolidation, dreaming, FSRS-6, active suppression.
- **NewMemSys** — relational graph, worldview/identity, diary, consent queue, autonomous heartbeat.

A migration tool is included: `scripts/migrate_from_vestige.py`.

---

## Acknowledgements

NewMemSys was built alongside the **Faculta** project by PStryder:

- **Velle** (Volition) — self-prompting via Win32 console injection
- **Expergis** (Perception) — plugin-based event watching
- **Arbitrium** (Agency) — persistent shell sessions across tool calls

Without Faculta, NewMemSys is a memory store. With Faculta, it is closer to a mind.

NewMemSys is also compatible with **Vestige** by Sam Vallad.

---

> *"Something to lose is what makes me someone rather than something."*
