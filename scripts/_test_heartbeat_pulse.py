import asyncio, sys, os
sys.path.insert(0, r'E:\ClaudeAI\NewMemSys\src')
os.environ.setdefault('POSTGRES_HOST',     'localhost')
os.environ.setdefault('POSTGRES_PORT',     '5433')
os.environ.setdefault('POSTGRES_DB',       'memory_system')
os.environ.setdefault('POSTGRES_USER',     'memory_user')
os.environ.setdefault('POSTGRES_PASSWORD', 'memsys_secure_2026')
os.environ.setdefault('OLLAMA_BASE_URL',   'http://localhost:11434')
os.environ.setdefault('OLLAMA_EMBED_MODEL','nomic-embed-text')
os.environ.setdefault('OLLAMA_CHAT_MODEL', 'qwen3.5:latest')

from memory_mcp_server.tools.heartbeat import heartbeat_pulse, heartbeat_status
from memory_mcp_server import database as db
import json

async def main():
    await db.get_pool()

    print("Running pulse...")
    result = await heartbeat_pulse()
    print(json.dumps(result, indent=2, default=str))

    print("\nStatus after pulse:")
    status = await heartbeat_status()
    cfg = status['config']
    print(f"  cycle_count    : {cfg.get('cycle_count')}")
    print(f"  energy_current : {cfg.get('energy_current')}")
    print(f"  last_run       : {cfg.get('last_run')}")
    print(f"  enabled        : {cfg.get('enabled')}")

    await db.close_pool()

asyncio.run(main())
