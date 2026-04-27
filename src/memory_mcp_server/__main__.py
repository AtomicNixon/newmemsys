"""Entry point: python -m memory_mcp_server"""
import asyncio
from memory_mcp_server.server import main

asyncio.run(main())
