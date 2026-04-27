import sys

path = r'C:\Users\Acat\.claude.json'
with open(path, encoding='utf-8') as f:
    content = f.read()

if 'memory_mcp_server' in content:
    print('Already present — no changes made.')
    sys.exit(0)

# Find exact closing pattern of the cerebellum block (last entry in mcpServers)
target = '      "env": {}\n    }\n  }'

replacement = (
    '      "env": {}\n'
    '    },\n'
    '    "memory": {\n'
    '      "type": "stdio",\n'
    '      "command": "C:\\\\Python312\\\\python.exe",\n'
    '      "args": ["-m", "memory_mcp_server"],\n'
    '      "cwd": "E:\\\\ClaudeAI\\\\NewMemSys\\\\src",\n'
    '      "env": {\n'
    '        "POSTGRES_HOST": "localhost",\n'
    '        "POSTGRES_PORT": "5433",\n'
    '        "POSTGRES_DB": "memory_system",\n'
    '        "POSTGRES_USER": "memory_user",\n'
    '        "POSTGRES_PASSWORD": "memsys_secure_2026",\n'
    '        "OLLAMA_BASE_URL": "http://localhost:11434",\n'
    '        "OLLAMA_EMBED_MODEL": "nomic-embed-text"\n'
    '      }\n'
    '    }\n'
    '  }'
)

if target not in content:
    print('Pattern not found. Showing end of mcpServers section:')
    idx = content.rfind('"cerebellum"')
    print(repr(content[idx:idx+400]))
    sys.exit(1)

# Replace only the LAST occurrence (end of mcpServers block)
last_idx = content.rfind(target)
new_content = content[:last_idx] + replacement + content[last_idx + len(target):]

with open(path, 'w', encoding='utf-8') as f:
    f.write(new_content)

print('Done — memory server added to .claude.json')
