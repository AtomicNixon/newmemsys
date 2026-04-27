import psycopg2
conn = psycopg2.connect(host='localhost',port=5433,dbname='memory_system',user='memory_user',password='memsys_secure_2026')
cur = conn.cursor()
cur.execute("UPDATE heartbeat_config SET value = '20' WHERE key = 'energy_current'")
cur.execute("SELECT value FROM heartbeat_config WHERE key = 'energy_current'")
print('energy_current now:', cur.fetchone()[0])
conn.commit()
conn.close()
