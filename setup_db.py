"""创建clauseguard数据库与专用账号"""
import sys
import psycopg2

ADMIN_PWD = "admin123!"

def out(s):
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))

c = psycopg2.connect(host="127.0.0.1", port=5432, dbname="postgres",
                     user="admin", password=ADMIN_PWD)
c.autocommit = True
cur = c.cursor()
cur.execute("SELECT 1 FROM pg_roles WHERE rolname='clauseguard'")
if not cur.fetchone():
    cur.execute("CREATE ROLE clauseguard LOGIN PASSWORD 'Cg@2026pg'")
    out("role clauseguard created")
cur.execute("SELECT 1 FROM pg_database WHERE datname='clauseguard'")
if not cur.fetchone():
    cur.execute("CREATE DATABASE clauseguard OWNER clauseguard")
    out("database clauseguard created")
c.close()

# 验证专用账号可连
c = psycopg2.connect(host="127.0.0.1", port=5432, dbname="clauseguard",
                     user="clauseguard", password="Cg@2026pg")
cur = c.cursor()
cur.execute("select current_database(), current_user")
out("OK %s" % (cur.fetchone(),))
c.close()
