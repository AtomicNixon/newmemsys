"""Package setup for memory_mcp_server."""
from setuptools import setup, find_packages

setup(
    name="memory-mcp-server",
    version="1.0.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    python_requires=">=3.11",
    install_requires=[
        "mcp>=1.0.0",
        "asyncpg>=0.29.0",
        "psycopg2-binary>=2.9.9",
        "structlog>=24.1.0",
    ],
)
