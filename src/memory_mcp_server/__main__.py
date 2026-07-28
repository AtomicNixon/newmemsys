"""Entry point: python -m memory_mcp_server"""
import asyncio
import sys
from memory_mcp_server.server import main

try:
    asyncio.run(main())
except KeyboardInterrupt:
    sys.exit(0)
