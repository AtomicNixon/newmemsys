#!/usr/bin/env python
"""
newmemsys-graph — Render NewMemSys memory graph as a local force-directed HTML page.

Standalone — no MCP, no Claude Code. Hits Postgres directly, writes a
self-contained HTML file with vis-network (loaded from CDN), and opens it
in the default browser.

Usage:
    python newmemsys-graph.py                          # whole graph, capped at 300 nodes
    python newmemsys-graph.py --memory-id <uuid>       # neighborhood of one memory
    python newmemsys-graph.py --memory-id <uuid> --depth 2
    python newmemsys-graph.py --max-nodes 500          # raise the cap
    python newmemsys-graph.py --out graph.html         # custom output path
    python newmemsys-graph.py --no-open                # don't auto-open browser

The HTML is a single file you can share, email, or open on another machine.
"""
from __future__ import annotations

import asyncio
import json
import sys
import webbrowser
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from cli_db import connect, close, fetch, fetchrow


DEFAULT_MAX_NODES = 300
DEFAULT_DEPTH    = 1


async def _fetch_full_graph(pool, max_nodes: int) -> tuple[list[dict], list[dict]]:
    """Fetch the whole graph, capped at max_nodes by most-connected first."""
    # Pick the top-N most-connected memories so the viz is meaningful
    rows = await fetch(
        pool,
        """
        WITH top_mems AS (
            SELECT m.id, m.content, m.type, m.importance, m.emotional_valence,
                   (SELECT COUNT(*) FROM memory_graph g
                    WHERE g.memory_id = m.id OR g.connected_memory_id = m.id) AS degree
            FROM memories m
            WHERE m.status = 'active'
            ORDER BY degree DESC
            LIMIT $1
        )
        SELECT g.memory_id, g.connected_memory_id, g.relationship_type, g.confidence,
               tm.content, tm.type, tm.importance, tm.emotional_valence
        FROM memory_graph g
        JOIN top_mems tm ON tm.id = g.memory_id
        """,
        max_nodes,
    )

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for r in rows:
        src = str(r["memory_id"])
        dst = str(r["connected_memory_id"])
        if src not in nodes:
            nodes[src] = {"id": src, "content": r["content"], "type": r["type"],
                          "importance": float(r["importance"]),
                          "valence": float(r["emotional_valence"])}
        edges.append({
            "from": src, "to": dst,
            "label": r["relationship_type"],
            "confidence": float(r["confidence"]),
        })

    # Also fetch dst nodes that aren't in top_mems (so edges don't dangle)
    dst_ids = {e["to"] for e in edges} - set(nodes.keys())
    if dst_ids:
        # Fetch in batches of 100 (avoid parameter limit)
        dst_list = list(dst_ids)
        for i in range(0, len(dst_list), 100):
            batch = dst_list[i:i+100]
            placeholders = ",".join(f"${j+1}" for j in range(len(batch)))
            drows = await fetch(
                pool,
                f"""SELECT id, content, type, importance, emotional_valence
                    FROM memories WHERE id::text IN ({placeholders})""",
                *batch,
            )
            for r in drows:
                nodes[str(r["id"])] = {
                    "id": str(r["id"]), "content": r["content"], "type": r["type"],
                    "importance": float(r["importance"]),
                    "valence": float(r["emotional_valence"]),
                }

    return list(nodes.values()), edges


async def _fetch_neighborhood(pool, memory_id: str, depth: int, max_nodes: int) -> tuple[list[dict], list[dict]]:
    """BFS from a seed memory, out to `depth` hops, capped at max_nodes."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    visited: set[str] = set()
    frontier: set[str] = {memory_id}

    for hop in range(depth + 1):
        if not frontier or len(nodes) >= max_nodes:
            break

        # Fetch node details for the frontier
        frontier_list = list(frontier)
        placeholders = ",".join(f"${j+1}" for j in range(len(frontier_list)))
        nrows = await fetch(
            pool,
            f"""SELECT id, content, type, importance, emotional_valence
                FROM memories WHERE id::text IN ({placeholders})""",
            *frontier_list,
        )
        for r in nrows:
            nid = str(r["id"])
            if nid not in nodes:
                nodes[nid] = {
                    "id": nid, "content": r["content"], "type": r["type"],
                    "importance": float(r["importance"]),
                    "valence": float(r["emotional_valence"]),
                }

        # Fetch edges from the frontier (only if more hops remain)
        if hop < depth:
            # Build distinct placeholder sets: $1..$N for first IN, $N+1..$2N for second
            n = len(frontier_list)
            ph1 = ",".join(f"${i+1}" for i in range(n))
            ph2 = ",".join(f"${i+1+n}" for i in range(n))
            erows = await fetch(
                pool,
                f"""SELECT memory_id, connected_memory_id, relationship_type, confidence
                    FROM memory_graph
                    WHERE memory_id::text IN ({ph1})
                       OR connected_memory_id::text IN ({ph2})""",
                *frontier_list, *frontier_list,
            )
            next_frontier: set[str] = set()
            for r in erows:
                src = str(r["memory_id"])
                dst = str(r["connected_memory_id"])
                edges.append({
                    "from": src, "to": dst,
                    "label": r["relationship_type"],
                    "confidence": float(r["confidence"]),
                })
                other = dst if src in frontier else src
                if other not in visited:
                    next_frontier.add(other)

            visited.update(frontier)
            frontier = next_frontier - visited
            if len(nodes) + len(frontier) > max_nodes:
                # Trim frontier to stay under cap
                frontier = set(list(frontier)[: max_nodes - len(nodes)])

    return list(nodes.values()), edges


def _build_html(nodes: list[dict], edges: list[dict], title: str) -> str:
    """Build a self-contained HTML page with vis-network."""
    # Color nodes by type
    type_colors = {
        "episodic":   "#4CAF50",  # green
        "semantic":   "#2196F3",  # blue
        "procedural": "#FF9800",  # orange
        "strategic":  "#9C27B0",  # purple
        "working":    "#607D8B",  # blue-grey
    }
    # Edge colors by relationship
    rel_colors = {
        "causes":       "#F44336",
        "caused_by":    "#F44336",
        "contradicts":  "#F44336",
        "supports":     "#4CAF50",
        "related_to":   "#9E9E9E",
        "precedes":     "#FF9800",
        "follows":      "#FF9800",
        "part_of":      "#2196F3",
        "example_of":   "#9C27B0",
    }

    vis_nodes = []
    for n in nodes:
        color = type_colors.get(n["type"], "#607D8B")
        # Size by importance
        size = 15 + (n["importance"] * 25)
        # Truncate content for label
        label = n["content"][:60].replace("\n", " ").replace('"', "'")
        if len(n["content"]) > 60:
            label += "…"
        vis_nodes.append({
            "id": n["id"],
            "label": label,
            "title": f"imp={n['importance']:.2f}  val={n['valence']:.2f}\n{n['content'][:200]}",
            "color": color,
            "size": size,
            "font": {"size": 10},
        })

    vis_edges = []
    for i, e in enumerate(edges):
        color = rel_colors.get(e["label"], "#9E9E9E")
        vis_edges.append({
            "id": i,
            "from": e["from"],
            "to": e["to"],
            "label": e["label"],
            "color": {"color": color, "opacity": 0.6},
            "arrows": "to",
            "title": f"{e['label']} (conf={e['confidence']:.2f})",
        })

    nodes_json = json.dumps(vis_nodes)
    edges_json = json.dumps(vis_edges)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1a1a1a; color: #eee; }}
  #network {{ width: 100vw; height: 100vh; }}
  #legend {{ position: fixed; top: 10px; left: 10px; background: rgba(0,0,0,0.8); padding: 12px 16px; border-radius: 6px; font-size: 12px; z-index: 10; }}
  #legend h3 {{ margin: 0 0 8px 0; font-size: 13px; }}
  #legend .row {{ margin: 2px 0; }}
  #legend .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }}
  #info {{ position: fixed; bottom: 10px; right: 10px; background: rgba(0,0,0,0.8); padding: 8px 12px; border-radius: 6px; font-size: 11px; }}
</style>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
</head>
<body>
<div id="legend">
  <h3>Node types</h3>
  <div class="row"><span class="dot" style="background:#4CAF50"></span>episodic</div>
  <div class="row"><span class="dot" style="background:#2196F3"></span>semantic</div>
  <div class="row"><span class="dot" style="background:#FF9800"></span>procedural</div>
  <div class="row"><span class="dot" style="background:#9C27B0"></span>strategic</div>
  <div class="row"><span class="dot" style="background:#607D8B"></span>working</div>
  <h3 style="margin-top:10px">Edges</h3>
  <div class="row"><span class="dot" style="background:#F44336"></span>causes / contradicts</div>
  <div class="row"><span class="dot" style="background:#4CAF50"></span>supports</div>
  <div class="row"><span class="dot" style="background:#9E9E9E"></span>related_to</div>
  <div class="row"><span class="dot" style="background:#FF9800"></span>precedes / follows</div>
</div>
<div id="network"></div>
<div id="info">{len(vis_nodes)} nodes · {len(vis_edges)} edges</div>
<script>
  const nodes = new vis.DataSet({nodes_json});
  const edges = new vis.DataSet({edges_json});
  const container = document.getElementById('network');
  const data = {{ nodes, edges }};
  const options = {{
    physics: {{
      stabilization: {{ iterations: 200, updateInterval: 25 }},
      barnesHut: {{ gravitationalConstant: -8000, springLength: 120, springConstant: 0.04 }}
    }},
    nodes: {{ shape: 'dot', scaling: {{ min: 10, max: 40 }} }},
    edges: {{ font: {{ size: 9, color: '#aaa', strokeWidth: 0, background: 'transparent' }}, smooth: {{ type: 'continuous' }} }},
    interaction: {{ hover: true, tooltipDelay: 100 }}
  }};
  const network = new vis.Network(container, data, options);
</script>
</body>
</html>"""


async def main() -> int:
    args = sys.argv[1:]

    # Parse args
    memory_id = None
    depth = DEFAULT_DEPTH
    max_nodes = DEFAULT_MAX_NODES
    out_path = None
    no_open = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--memory-id" and i + 1 < len(args):
            memory_id = args[i+1]; i += 2
        elif a == "--depth" and i + 1 < len(args):
            depth = int(args[i+1]); i += 2
        elif a == "--max-nodes" and i + 1 < len(args):
            max_nodes = int(args[i+1]); i += 2
        elif a == "--out" and i + 1 < len(args):
            out_path = args[i+1]; i += 2
        elif a == "--no-open":
            no_open = True; i += 1
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            sys.stderr.write(f"Unknown arg: {a}\n")
            print(__doc__)
            return 1

    pool = await connect()
    try:
        if memory_id:
            # Verify the memory exists
            row = await fetchrow(
                pool,
                "SELECT id FROM memories WHERE id::text = $1", memory_id
            )
            if not row:
                sys.stderr.write(f"ERROR: memory {memory_id} not found\n")
                return 1
            title = f"NewMemSys graph — neighborhood of {memory_id[:8]} (depth={depth})"
            nodes, edges = await _fetch_neighborhood(pool, memory_id, depth, max_nodes)
        else:
            title = f"NewMemSys graph — top {max_nodes} most-connected memories"
            nodes, edges = await _fetch_full_graph(pool, max_nodes)
    finally:
        await close(pool)

    if not nodes:
        print("No nodes to display.")
        return 0

    html = _build_html(nodes, edges, title)

    if out_path:
        out = Path(out_path)
    else:
        out = Path.cwd() / "newmemsys_graph.html"
    out.write_text(html, encoding="utf-8")

    print(f"Wrote {len(nodes)} nodes / {len(edges)} edges → {out}")

    if not no_open:
        webbrowser.open(out.resolve().as_uri())

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
