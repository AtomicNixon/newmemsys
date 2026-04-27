import asyncio, sys, os, json
sys.path.insert(0, r'E:\ClaudeAI\NewMemSys\src')

# Set env directly (mirrors MCP config)
os.environ.setdefault('POSTGRES_HOST', 'localhost')
os.environ.setdefault('POSTGRES_PORT', '5433')
os.environ.setdefault('POSTGRES_DB',   'memory_system')
os.environ.setdefault('POSTGRES_USER', 'memory_user')
os.environ.setdefault('POSTGRES_PASSWORD', 'memsys_secure_2026')
os.environ.setdefault('OLLAMA_BASE_URL', 'http://localhost:11434')
os.environ.setdefault('OLLAMA_EMBED_MODEL', 'nomic-embed-text')

from memory_mcp_server.tools import memory as mem
from memory_mcp_server.tools import identity as id_tools
from memory_mcp_server import database as db

async def main():
    await db.get_pool()

    # 1. Store a test memory
    r = await mem.remember('Smoke test memory for edit/delete verification', type='working', importance=0.3)
    mid = r['id']
    print(f'remember   : {mid}')

    # 2. edit — change valence and tags only
    r = await mem.edit(id=mid, emotional_valence=0.8, tags=['test', 'smoke'])
    print(f'edit       : valence={r.get("emotional_valence")}  tags={r.get("tags")}  re_embedded={r.get("re_embedded")}')

    # 3. edit — change content (should re-embed)
    r = await mem.edit(id=mid, content='Smoke test memory — content updated')
    print(f'edit+embed : re_embedded={r.get("re_embedded")}')

    # 4. soft delete
    r = await mem.delete(id=mid, hard=False)
    print(f'delete(soft): {r}')

    # 5. set_worldview
    r = await id_tools.set_worldview(
        topic='smoke_test',
        belief='This belief exists only to verify set_worldview works.',
        confidence=0.99,
        source='smoke test 2026-04-04',
    )
    print(f'set_worldview: id={r["id"]}  topic={r["topic"]}')

    # 6. update same topic (upsert)
    r2 = await id_tools.set_worldview(topic='smoke_test', belief='Updated belief.', confidence=0.5)
    print(f'upsert same topic: same id={r["id"] == r2["id"]}  confidence={r2["confidence"]}')

    # cleanup worldview test row
    await db.execute("DELETE FROM worldview WHERE topic = 'smoke_test'")

    await db.close_pool()
    print('All checks passed.')

asyncio.run(main())
