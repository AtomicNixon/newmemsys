# NewMemSys Standalone CLI Tools

Three command-line tools that let you check on NewMemSys without needing a
live Claude Code session or MCP round-trip. They hit the Postgres DB directly
(via `cli_db.py`), so the only requirement is that the Docker container
`newmemsys_brain` is running.

## Requirements

- Python 3.10+ with `asyncpg` installed (`pip install asyncpg`)
- Docker container `newmemsys_brain` running on port 5433
- All three scripts share `cli_db.py` for connection params

## Connection config

Defaults match `docker-compose.yml`. Override via environment variables:

```
NEWMEMSYS_DB_HOST       (default: localhost)
NEWMEMSYS_DB_PORT       (default: 5433)
NEWMEMSYS_DB_NAME       (default: memory_system)
NEWMEMSYS_DB_USER       (default: memory_user)
NEWMEMSYS_DB_PASSWORD   (default: memsys_secure_2026)
```

---

## newmemsys-status.py

A human-readable dashboard. Run any time to see the whole system at a glance.

```
python newmemsys-status.py              # dashboard
python newmemsys-status.py --json       # machine-readable
```

Shows: memory counts (by status + type), bridge coverage, graph edge count,
pending outbox items, diary/worldview/cluster counts, heartbeat daemon status
(running/stale/disabled, last cycle, next scheduled, energy budget), and
Ollama reachability with available models.

---

## newmemsys-graph.py

Renders the memory graph as a force-directed HTML page using vis-network.
Opens in your default browser automatically.

```
python newmemsys-graph.py                                   # top 300 most-connected memories
python newmemsys-graph.py --max-nodes 500                   # raise the cap
python newmemsys-graph.py --memory-id <uuid>                # neighborhood of one memory
python newmemsys-graph.py --memory-id <uuid> --depth 2      # 2-hop neighborhood
python newmemsys-graph.py --out graph.html                  # custom output path
python newmemsys-graph.py --no-open                         # don't auto-open browser
```

Node colors by type (episodic=green, semantic=blue, procedural=orange,
strategic=purple, working=grey). Node size by importance. Edge colors by
relationship (causes/contradicts=red, supports=green, related_to=grey,
precedes/follows=orange, part_of=blue, example_of=purple).

The HTML file is self-contained (single file, CDN-loaded vis-network) — you
can email it, share it, or open it on another machine.

---

## newmemsys-review.py

A retention / consolidation report. Surfaces what NewMemSys actually tracks
(importance, half-life, decay status, valence) and flags suspicious patterns.

```
python newmemsys-review.py              # full report
python newmemsys-review.py --json       # machine-readable
python newmemsys-review.py --decay      # only memories past their half-life
python newmemsys-review.py --consolidate    # run a consolidation pass
```

The report includes:
- **Importance distribution** — bucketed 0.0-1.0 with counts and percentages
- **Uniform-score suspicion** — flags any single importance value that
  accounts for >30% of all memories (the "stuck at default" detector)
- **Half-life distribution** — how many memories fall in each half-life bucket
- **Decay due for review** — memories older than their half-life, top 50
- **Valence distribution** — negative / neutral / positive
- **Type × importance cross-tab** — avg/min/max/stddev per type
- **Bridge coverage** — bridged vs unbridged by importance

**Note on FSRS**: NewMemSys uses importance + half_life_hours for decay, not
FSRS. If you see a uniform "65% retention" metric, that's coming from Vestige,
not from this database. This report shows what NewMemSys actually tracks.

**Consolidation pass** (`--consolidate`): marks memories as `expired` (soft,
reversible) if they are importance < 0.1 AND older than 2x their half-life.
Does NOT delete anything.
