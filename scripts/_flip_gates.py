import psycopg2
conn = psycopg2.connect(host='localhost',port=5433,dbname='memory_system',user='memory_user',password='memsys_secure_2026')
cur = conn.cursor()
cur.execute("UPDATE heartbeat_config SET value = 'true' WHERE key IN ('is_configured','enabled')")
conn.commit()
cur.execute("SELECT key, value FROM heartbeat_config ORDER BY key")
for row in cur.fetchall():
    print(f"  {row[0]:22s} {row[1]}")
conn.close()
