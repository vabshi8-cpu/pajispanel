import threading, time, sqlite3
from vps import stats, suspend

DB = "panel.db"

def watch():
    while True:
        try:
            con = sqlite3.connect(DB)
            c = con.cursor()
            c.execute("SELECT container_id FROM vps WHERE status='running'")
            for (cid,) in c.fetchall():
                if not cid: continue
                s = stats(cid)
                if s.get("cpu", 0) > 80:
                    suspend(cid)
                    c.execute("UPDATE vps SET status='suspended' WHERE container_id=?", (cid,))
                    con.commit()
                    print(f"[MONITOR] Suspended {cid[:12]} — CPU {s['cpu']}%")
            con.close()
        except Exception as e:
            print(f"[MONITOR] err: {e}")
        time.sleep(15)

def start_monitor():
    t = threading.Thread(target=watch, daemon=True)
    t.start()
