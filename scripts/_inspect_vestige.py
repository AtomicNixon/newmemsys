import sqlite3

conn = sqlite3.connect(r'C:\Users\Acat\AppData\Roaming\vestige\core\data\vestige.db')
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cur.fetchall()]
print('TABLES:', tables)
print()

for t in tables:
    cur.execute(f'PRAGMA table_info("{t}")')
    cols = cur.fetchall()
    cur.execute(f'SELECT count(*) FROM "{t}"')
    count = cur.fetchone()[0]
    print(f'--- {t} ({count} rows) ---')
    for c in cols:
        print(f'  {c[1]:35s} {c[2]}')
    print()

# Sample 2 rows from each table
for t in tables:
    cur.execute(f'SELECT * FROM "{t}" LIMIT 2')
    rows = cur.fetchall()
    if rows:
        print(f'=== SAMPLE: {t} ===')
        for row in rows:
            print(' ', row)
        print()

conn.close()
