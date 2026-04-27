import psycopg2
conn = psycopg2.connect(host='localhost',port=5433,dbname='memory_system',user='memory_user',password='memsys_secure_2026')
cur = conn.cursor()
cur.execute("""
DO $body$
BEGIN
  ALTER TABLE worldview ADD CONSTRAINT worldview_topic_unique UNIQUE (topic);
EXCEPTION WHEN duplicate_table THEN NULL;
END
$body$
""")
conn.commit()
conn.close()
print('worldview UNIQUE(topic) constraint applied')
