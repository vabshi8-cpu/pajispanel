import sqlite3, threading, queue, secrets, os
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, Response
from werkzeug.security import generate_password_hash, check_password_hash
import setup, vps, monitor

DB = "panel.db"
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

setup.init()
monitor.start()

BUILD_LOGS = {}  # user_id -> queue

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()

def current_user():
    if "uid" not in session: return None
    con = db()
    u = con.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    con.close()
    return u

@app.route("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"]
        e = request.form.get("email", "").strip()
        if len(u) < 3 or len(p) < 4:
            flash("Username 3+ chars, password 4+ chars.")
            return redirect(url_for("register"))
        con = db()
        try:
            con.execute("INSERT INTO users(username,password,email,signup_ip) VALUES(?,?,?,?)",
                        (u, generate_password_hash(p), e, client_ip()))
            con.commit()
        except sqlite3.IntegrityError:
            flash("Username already taken.")
            con.close()
            return redirect(url_for("register"))
        con.close()
        flash("Account created. Login now.")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"]
        con = db()
        row = con.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        con.close()
        if not row or not check_password_hash(row["password"], p):
            flash("Invalid credentials.")
            return redirect(url_for("login"))
        session.clear()
        session["uid"] = row["id"]
        session["is_admin"] = bool(row["is_admin"])
        return redirect(url_for("admin" if row["is_admin"] else "dashboard"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    u = current_user()
    if not u: return redirect(url_for("login"))
    if u["is_admin"]: return redirect(url_for("admin"))
    con = db()
    v = con.execute("SELECT * FROM vps WHERE user_id=?", (u["id"],)).fetchone()
    con.close()
    return render_template("dashboard.html", user=u, vps=v)

@app.route("/create_vps", methods=["POST"])
def create_vps():
    u = current_user()
    if not u: return redirect(url_for("login"))
    ip = client_ip()
    con = db()

    # farming check: this account already has one
    existing = con.execute("SELECT id FROM vps WHERE user_id=?", (u["id"],)).fetchone()
    if existing:
        con.close()
        flash("You already own a VPS.")
        return redirect(url_for("dashboard"))

    # farming check: this IP already has a VPS on another account
    ip_row = con.execute("SELECT user_id FROM ip_vps WHERE ip=?", (ip,)).fetchone()
    if ip_row and ip_row["user_id"] != u["id"]:
        con.close()
        flash("This network already has a VPS registered under a different account.")
        return redirect(url_for("dashboard"))

    # farming check: signup IP already tied to a VPS
    same_ip_user = con.execute(
        "SELECT v.id FROM vps v JOIN users us ON us.id=v.user_id WHERE us.signup_ip=? AND v.user_id!=?",
        (u["signup_ip"], u["id"])
    ).fetchone()
    if same_ip_user:
        con.close()
        flash("Detected multi-account abuse. VPS creation blocked.")
        return redirect(url_for("dashboard"))

    con.close()
    q = queue.Queue()
    BUILD_LOGS[u["id"]] = q

    def build():
        try:
            cid, ssh = vps.create_vps(u["username"], lambda m: q.put(m))
            con2 = db()
            con2.execute("INSERT INTO vps(user_id,container_id,tmate_ssh,created_ip) VALUES(?,?,?,?)",
                         (u["id"], cid, ssh, ip))
            con2.execute("INSERT OR REPLACE INTO ip_vps(ip,user_id) VALUES(?,?)", (ip, u["id"]))
            con2.commit()
            con2.close()
            q.put("[DONE]")
        except Exception as ex:
            q.put(f"[ERROR] {ex}")
            q.put("[DONE]")

    threading.Thread(target=build, daemon=True).start()
    return redirect(url_for("building"))

@app.route("/building")
def building():
    u = current_user()
    if not u: return redirect(url_for("login"))
    return render_template("vps_view.html", building=True)

@app.route("/logs")
def logs():
    u = current_user()
    if not u: return "no", 403
    def stream():
        q = BUILD_LOGS.get(u["id"])
        if not q:
            yield "data: [no build]\n\n"
            return
        while True:
            msg = q.get()
            yield f"data: {msg}\n\n"
            if msg == "[DONE]": break
    return Response(stream(), mimetype="text/event-stream")

@app.route("/vps/stats")
def vps_stats():
    u = current_user()
    if not u: return jsonify({}), 403
    con = db()
    v = con.execute("SELECT * FROM vps WHERE user_id=?", (u["id"],)).fetchone()
    con.close()
    if not v: return jsonify({})
    s = vps.stats(v["container_id"])
    s["ssh"] = v["tmate_ssh"]
    s["db_status"] = v["status"]
    return jsonify(s)

# ---------------- ADMIN ----------------

@app.route("/admin")
def admin():
    u = current_user()
    if not u or not u["is_admin"]: return redirect(url_for("login"))
    con = db()
    rows = con.execute("""SELECT v.*, us.username, us.email FROM vps v
                          JOIN users us ON us.id=v.user_id""").fetchall()
    users = con.execute("SELECT id,username,email,is_admin,signup_ip,created_at FROM users").fetchall()
    con.close()
    live = []
    for r in rows:
        s = vps.stats(r["container_id"])
        live.append({**dict(r), **s})
    return render_template("admin.html", vpses=live, users=users)

@app.route("/admin/suspend/<int:vid>", methods=["POST"])
def admin_suspend(vid):
    u = current_user()
    if not u or not u["is_admin"]: return "no", 403
    con = db()
    row = con.execute("SELECT * FROM vps WHERE id=?", (vid,)).fetchone()
    if row:
        vps.suspend(row["container_id"])
        con.execute("UPDATE vps SET status='suspended' WHERE id=?", (vid,))
        con.commit()
    con.close()
    return redirect(url_for("admin"))

@app.route("/admin/resume/<int:vid>", methods=["POST"])
def admin_resume(vid):
    u = current_user()
    if not u or not u["is_admin"]: return "no", 403
    con = db()
    row = con.execute("SELECT * FROM vps WHERE id=?", (vid,)).fetchone()
    if row:
        vps.resume(row["container_id"])
        con.execute("UPDATE vps SET status='running' WHERE id=?", (vid,))
        con.commit()
    con.close()
    return redirect(url_for("admin"))

@app.route("/admin/delete/<int:vid>", methods=["POST"])
def admin_delete(vid):
    u = current_user()
    if not u or not u["is_admin"]: return "no", 403
    con = db()
    row = con.execute("SELECT * FROM vps WHERE id=?", (vid,)).fetchone()
    if row:
        vps.destroy(row["container_id"])
        con.execute("DELETE FROM vps WHERE id=?", (vid,))
        con.execute("DELETE FROM ip_vps WHERE user_id=?", (row["user_id"],))
        con.commit()
    con.close()
    return redirect(url_for("admin"))

@app.route("/admin/user/delete/<int:uid>", methods=["POST"])
def admin_user_delete(uid):
    u = current_user()
    if not u or not u["is_admin"]: return "no", 403
    if uid == u["id"]:
        flash("Cannot delete yourself.")
        return redirect(url_for("admin"))
    con = db()
    row = con.execute("SELECT * FROM vps WHERE user_id=?", (uid,)).fetchone()
    if row:
        vps.destroy(row["container_id"])
        con.execute("DELETE FROM vps WHERE user_id=?", (uid,))
    con.execute("DELETE FROM ip_vps WHERE user_id=?", (uid,))
    con.execute("DELETE FROM users WHERE id=?", (uid,))
    con.commit()
    con.close()
    return redirect(url_for("admin"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
