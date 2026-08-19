import os, sqlite3, time, secrets, threading
from flask import Flask, request, render_template, redirect, url_for, jsonify, session, Response, stream_with_context
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from vps import create_vps_container, destroy_vps, suspend_vps, unsuspend_vps, regen_tmate, get_container_stats, build_logs_stream
from monitor import start_monitor

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = secrets.token_hex(32)

DB = "panel.db"
login_manager = LoginManager(app)
login_manager.login_view = "login"

def get_db():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        signup_ip TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        container_id TEXT NOT NULL,
        ssh_command TEXT,
        status TEXT DEFAULT 'creating',
        creator_ip TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_regen INTEGER DEFAULT 0,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    db.commit()
    db.close()

class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.username = row["username"]
        self.is_admin = bool(row["is_admin"])

@login_manager.user_loader
def load_user(uid):
    row = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return User(row) if row else None

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"]
        ip = request.remote_addr
        if not u or not p:
            return render_template("register.html", error="Fill both fields")
        db = get_db()
        if db.execute("SELECT 1 FROM users WHERE username=?", (u,)).fetchone():
            return render_template("register.html", error="Username taken")
        db.execute("INSERT INTO users(username,password,signup_ip,created_at) VALUES(?,?,?,?)",
                   (u, generate_password_hash(p), ip, int(time.time())))
        db.commit()
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"]
        row = get_db().execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        if row and check_password_hash(row["password"], p):
            login_user(User(row))
            return redirect(url_for("admin" if row["is_admin"] else "dashboard"))
        return render_template("login.html", error="Bad credentials")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    vps = db.execute("SELECT * FROM vps WHERE user_id=?", (current_user.id,)).fetchone()
    return render_template("dashboard.html", vps=vps)

@app.route("/vps/create", methods=["POST"])
@login_required
def vps_create():
    db = get_db()
    ip = request.remote_addr

    if db.execute("SELECT 1 FROM vps WHERE user_id=?", (current_user.id,)).fetchone():
        return "You already have a VPS", 403
    if db.execute("SELECT 1 FROM vps WHERE creator_ip=?", (ip,)).fetchone():
        return "This IP already owns a VPS", 403
    user_row = db.execute("SELECT signup_ip FROM users WHERE id=?", (current_user.id,)).fetchone()
    if db.execute("SELECT 1 FROM vps WHERE creator_ip=?", (user_row["signup_ip"],)).fetchone():
        return "Your signup IP already owns a VPS", 403

    db.execute("INSERT INTO vps(user_id,container_id,creator_ip,created_at,status) VALUES(?,?,?,?,?)",
               (current_user.id, "pending", ip, int(time.time()), "creating"))
    db.commit()
    vps_id = db.execute("SELECT id FROM vps WHERE user_id=?", (current_user.id,)).fetchone()["id"]

    threading.Thread(target=_build_vps, args=(vps_id, current_user.id), daemon=True).start()
    return redirect(url_for("vps_view", vps_id=vps_id))

def _build_vps(vps_id, user_id):
    try:
        cid, ssh = create_vps_container(f"vps-{user_id}")
        db = get_db()
        db.execute("UPDATE vps SET container_id=?, ssh_command=?, status='running' WHERE id=?",
                   (cid, ssh, vps_id))
        db.commit()
        db.close()
    except Exception as e:
        db = get_db()
        db.execute("UPDATE vps SET status='failed', ssh_command=? WHERE id=?", (str(e), vps_id))
        db.commit()
        db.close()

@app.route("/vps/<int:vps_id>")
@login_required
def vps_view(vps_id):
    db = get_db()
    vps = db.execute("SELECT * FROM vps WHERE id=?", (vps_id,)).fetchone()
    if not vps or (vps["user_id"] != current_user.id and not current_user.is_admin):
        return "Not found", 404
    return render_template("vps_view.html", vps=vps)

@app.route("/vps/<int:vps_id>/logs")
@login_required
def vps_logs(vps_id):
    db = get_db()
    vps = db.execute("SELECT * FROM vps WHERE id=?", (vps_id,)).fetchone()
    if not vps or (vps["user_id"] != current_user.id and not current_user.is_admin):
        return "Not found", 404

    @stream_with_context
    def gen():
        # NOTE: was build_logs_stream(vps_id) — must pass the container_id, not the DB row id
        for line in build_logs_stream(vps["container_id"]):
            yield f"data: {line}\n\n"
    return Response(gen(), mimetype="text/event-stream")

@app.route("/vps/<int:vps_id>/stats")
@login_required
def vps_stats(vps_id):
    db = get_db()
    vps = db.execute("SELECT * FROM vps WHERE id=?", (vps_id,)).fetchone()
    if not vps or (vps["user_id"] != current_user.id and not current_user.is_admin):
        return jsonify({"error": "no"}), 404
    if vps["status"] != "running":
        return jsonify({"error": "not running", "status": vps["status"]})
    try:
        return jsonify(get_container_stats(vps["container_id"], vps["created_at"]))
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/vps/<int:vps_id>/regen_ssh", methods=["POST"])
@login_required
def regen_ssh(vps_id):
    db = get_db()
    vps = db.execute("SELECT * FROM vps WHERE id=?", (vps_id,)).fetchone()
    if not vps:
        return jsonify({"error": "VPS not found"}), 404
    if vps["user_id"] != current_user.id and not current_user.is_admin:
        return jsonify({"error": "Not your VPS"}), 403
    if vps["status"] != "running":
        return jsonify({"error": "VPS must be running"}), 400
    if time.time() - vps["last_regen"] < 30:
        return jsonify({"error": "Wait 30s between regens"}), 429
    try:
        new_ssh = regen_tmate(vps["container_id"])
        db.execute("UPDATE vps SET ssh_command=?, last_regen=? WHERE id=?",
                   (new_ssh, int(time.time()), vps_id))
        db.commit()
        return jsonify({"ssh": new_ssh})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin")
@login_required
def admin():
    if not current_user.is_admin:
        return "Forbidden", 403
    db = get_db()
    rows = db.execute("""SELECT vps.*, users.username FROM vps
                         JOIN users ON users.id = vps.user_id""").fetchall()
    users = db.execute("SELECT id, username, signup_ip, is_admin, created_at FROM users").fetchall()
    return render_template("admin.html", vpses=rows, users=users)

@app.route("/admin/vps/<int:vps_id>/<action>", methods=["POST"])
@login_required
def admin_vps_action(vps_id, action):
    if not current_user.is_admin:
        return "Forbidden", 403
    db = get_db()
    vps = db.execute("SELECT * FROM vps WHERE id=?", (vps_id,)).fetchone()
    if not vps:
        return "Not found", 404
    if action == "suspend":
        suspend_vps(vps["container_id"])
        db.execute("UPDATE vps SET status='suspended' WHERE id=?", (vps_id,))
    elif action == "unsuspend":
        unsuspend_vps(vps["container_id"])
        db.execute("UPDATE vps SET status='running' WHERE id=?", (vps_id,))
    elif action == "delete":
        destroy_vps(vps["container_id"])
        db.execute("DELETE FROM vps WHERE id=?", (vps_id,))
    db.commit()
    return redirect(url_for("admin"))

if __name__ == "__main__":
    init_db()
    start_monitor()
    app.run(host="0.0.0.0", port=5000, threaded=True)
