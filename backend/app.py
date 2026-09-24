import os
import secrets
from functools import wraps

import psycopg2
from flask import Flask, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


def writer_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if session.get("role") != "writer":
            return ("仅审评员可操作暗码册", 403)
        return fn(*args, **kwargs)

    return wrap


def new_code(cur) -> str:
    """生成六位暗码（数字字母，排除易混字符），库内查重。"""
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        cur.execute("SELECT 1 FROM blind_codes WHERE code = %s", (code,))
        if cur.fetchone() is None:
            return code


@app.get("/health")
def health():
    return {"status": "ok", "service": "tea-blend-cupping"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        account = ACCOUNTS.get(name)
        if not account or account["password"] != request.form.get("password", ""):
            error = "用户名或密码错误"
        else:
            session["user"] = name
            session["role"] = account["role"]
            return redirect(url_for("home"))
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    is_writer = session.get("role") == "writer"
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if is_writer:
            cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        else:
            # 观察员：只取暗码，真名不下发到模板
            cur.execute(
                """SELECT c.id, c.aroma, c.taste, c.liquor, c.score,
                          c.verdict, c.note, c.created_by, bc.code
                     FROM cuppings c
                     LEFT JOIN blind_codes bc ON bc.lot = c.lot
                    ORDER BY c.id DESC"""
            )
        rows = cur.fetchall()
    return render_template("home.html", rows=rows, can_write=is_writer)


@app.post("/cuppings")
@login_required
def create():
    if session.get("role") != "writer":
        return ("仅审评员可提交拼配审评", 403)
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"]),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row, can_write=True)
    return redirect(url_for("home"))


@app.get("/codes")
@login_required
def codes():
    is_writer = session.get("role") == "writer"
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if is_writer:
            cur.execute(
                """SELECT lot, code, created_by, created_at
                     FROM blind_codes ORDER BY created_at DESC"""
            )
        else:
            # 观察员打开暗码册：服务端就只查暗码列
            cur.execute("SELECT code FROM blind_codes ORDER BY created_at DESC")
        code_rows = cur.fetchall()
        if is_writer:
            cur.execute(
                """SELECT DISTINCT lot FROM cuppings
                   WHERE lot NOT IN (SELECT lot FROM blind_codes)
                   ORDER BY lot"""
            )
            pending = [r["lot"] for r in cur.fetchall()]
        else:
            pending = []
    return render_template(
        "codes.html", code_rows=code_rows, pending=pending, can_write=is_writer
    )


@app.post("/codes")
@login_required
@writer_required
def issue_code():
    """为批次生成六位暗码；已发码的批次不重发。"""
    lot = request.form.get("lot", "").strip()
    if not lot:
        return ("批次名不能为空", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT 1 FROM blind_codes WHERE lot = %s", (lot,))
        if cur.fetchone() is not None:
            return ("该批次已发暗码", 409)
        code = new_code(cur)
        cur.execute(
            """INSERT INTO blind_codes (lot, code, created_by)
               VALUES (%s, %s, %s)""",
            (lot, code, session["user"]),
        )
        conn.commit()
    return redirect(url_for("codes"))


@app.post("/codes/rename")
@login_required
@writer_required
def rename_lot():
    """改正真名：同步总表批次名，已发暗码保持不变。"""
    old_lot = request.form.get("old_lot", "").strip()
    new_lot = request.form.get("new_lot", "").strip()
    if not old_lot or not new_lot:
        return ("真名不能为空", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT code FROM blind_codes WHERE lot = %s", (old_lot,))
        held = cur.fetchone()
        if held is None:
            return ("暗码册中没有该批次", 404)
        cur.execute("SELECT 1 FROM blind_codes WHERE lot = %s", (new_lot,))
        if cur.fetchone() is not None:
            return ("目标真名已在暗码册中", 409)
        cur.execute("UPDATE blind_codes SET lot = %s WHERE lot = %s", (new_lot, old_lot))
        cur.execute("UPDATE cuppings SET lot = %s WHERE lot = %s", (new_lot, old_lot))
        conn.commit()
    return redirect(url_for("codes"))
