import sqlite3, getpass, os
from werkzeug.security import generate_password_hash

DB = "panel.db"

def init():
    con = sqlite3.connect(DB)
    c = con.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        email TEXT,
        is_admin INTEGER DEFAULT 0,
        signup_ip TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS vps(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        container_id TEXT,
        tmate_ssh TEXT,
        status TEXT DEFAULT 'running',
        created_ip TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS ip_vps(
        ip TEXT PRIMARY KEY,
        user_id INTEGER)""")
    con.commit()

    c.execute("SELECT COUNT(*) FROM users WHERE is_admin=1")
    if c.fetchone()[0] == 0:
        print("=== First-run admin setup ===")
        u = input("Admin username: ").strip()
        e = input("Admin email: ").strip()
        p = getpass.getpass("Admin password: ")
        c.execute("INSERT INTO users(username,password,email,is_admin,signup_ip) VALUES(?,?,?,1,?)",
                  (u, generate_password_hash(p), e, "127.0.0.1"))
        con.commit()
        print(f"Admin '{u}' created.")
    con.close()

if __name__ == "__main__":
    init()
