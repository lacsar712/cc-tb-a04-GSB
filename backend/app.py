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
            return ("仅审评员可操作", 403)
        return fn(*args, **kwargs)

    return wrap


def new_code(cur):
    for _ in range(50):
        code = f"{secrets.randbelow(1_000_000):06d}"
        cur.execute("SELECT 1 FROM blind_codes WHERE code = %s", (code,))
        if cur.fetchone() is None:
            return code
    raise RuntimeError("无法生成唯一暗码")


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
    can_write = session.get("role") == "writer"
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT c.*, b.code
               FROM cuppings c
               LEFT JOIN blind_codes b ON b.lot = c.lot
               ORDER BY c.id DESC"""
        )
        rows = cur.fetchall()
    for row in rows:
        if can_write:
            row["display_lot"] = row["lot"]
            if row["code"]:
                row["display_lot"] += f"（暗码 {row['code']}）"
        else:
            # 观察员只见暗码，真名不下发页面
            row["display_lot"] = row["code"] or "未发码"
            row.pop("lot", None)
    return render_template("home.html", rows=rows, can_write=can_write)


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
    row["display_lot"] = row["lot"]
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


@app.get("/codes")
@login_required
def codes():
    can_write = session.get("role") == "writer"
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM blind_codes ORDER BY created_at DESC, lot")
        entries = cur.fetchall()
        lots = []
        if can_write:
            cur.execute("SELECT DISTINCT lot FROM cuppings ORDER BY lot")
            lots = [r["lot"] for r in cur.fetchall()]
    if not can_write:
        # 观察员只保留暗码列，其余字段不下发
        entries = [{"code": e["code"]} for e in entries]
    return render_template("codes.html", entries=entries, lots=lots, can_write=can_write)


@app.post("/codes")
@login_required
@writer_required
def issue_code():
    lot = request.form.get("lot", "").strip()
    if not lot:
        return ("批次真名不能为空", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT 1 FROM blind_codes WHERE lot = %s", (lot,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO blind_codes (lot, code, created_by) VALUES (%s,%s,%s)",
                (lot, new_code(cur), session["user"]),
            )
        conn.commit()
    return redirect(url_for("codes"))


@app.post("/lots/rename")
@login_required
@writer_required
def rename_lot():
    old = request.form.get("old_lot", "").strip()
    new = request.form.get("new_lot", "").strip()
    if not old or not new:
        return ("批次真名不能为空", 400)
    if old == new:
        return redirect(url_for("codes"))
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT 1 FROM blind_codes WHERE lot = %s", (new,))
        if cur.fetchone() is not None:
            return ("目标真名已发过暗码，不能并入", 409)
        # 真名改正同步到审评记录与暗码册，已发暗码本身不变
        cur.execute("UPDATE cuppings SET lot = %s WHERE lot = %s", (new, old))
        cur.execute("UPDATE blind_codes SET lot = %s WHERE lot = %s", (new, old))
        conn.commit()
    return redirect(url_for("codes"))
