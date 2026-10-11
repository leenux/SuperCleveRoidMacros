# -*- coding: utf-8 -*-
import pymysql, sys
sys.stdout.reconfigure(encoding='utf-8')
c = pymysql.connect(host='127.0.0.1', port=13306, user='mangos', password='mangos',
                    database='tw_world', charset='utf8mb4')
cur = c.cursor()
cur.execute("""
SELECT entry, name_loc0, name_loc4
FROM locales_creature
WHERE name_loc4 LIKE %s OR name_loc0 LIKE %s OR name_loc0 LIKE %s
ORDER BY entry
""", ('%训练假人%', '%Training Dummy%', '%training dummy%'))
for r in cur.fetchall():
    print(r)

# also creature_template name field
cur.execute("SHOW TABLES LIKE 'creature_template'")
print('tables', cur.fetchall())
cur.execute("""
SELECT entry, name FROM creature_template
WHERE name LIKE %s OR name LIKE %s
ORDER BY entry
""", ('%Training Dummy%', '%训练假人%'))
for r in cur.fetchall():
    print('ct', r)
