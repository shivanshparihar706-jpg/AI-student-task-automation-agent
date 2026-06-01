from flask import (Flask, render_template, request,
                   redirect, url_for, make_response, jsonify)
import sqlite3, os, re, hashlib, jwt, pdfplumber
from datetime import datetime, timedelta
from functools import wraps
from groq import Groq

app = Flask(__name__)
JWT_SECRET    = "ai_agent_jwt_2026"
DB_PATH       = "agent.db"
UPLOAD_FOLDER = "uploads"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

client = Groq(api_key="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")



def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c    = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            email       TEXT    NOT NULL UNIQUE,
            password    TEXT    NOT NULL,
            roll_no     TEXT,
            university  TEXT,
            course      TEXT,
            semester    TEXT,
            subjects    TEXT,
            study_time  TEXT,
            agent_notes TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS deadlines (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            subject    TEXT,
            task       TEXT,
            date       TEXT,
            status     TEXT DEFAULT 'pending',
            added_on   TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (student_id) REFERENCES students(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS marksheets (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id     INTEGER NOT NULL,
            semester_label TEXT,
            uploaded_at    TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (student_id) REFERENCES students(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS marks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            marksheet_id INTEGER NOT NULL,
            student_id   INTEGER NOT NULL,
            subject      TEXT,
            obtained     REAL,
            max_marks    REAL DEFAULT 100,
            percentage   REAL,
            grade        TEXT,
            FOREIGN KEY (marksheet_id) REFERENCES marksheets(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            action     TEXT,
            time       TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (student_id) REFERENCES students(id)
        )
    """)

    conn.commit()
    conn.close()
    print("Database ready — agent.db")


def hash_pw(password):
    salt = "agent_salt_2026"
    return hashlib.sha256(f"{salt}{password}{salt}".encode()).hexdigest()

def check_pw(password, hashed):
    return hash_pw(password) == hashed

def make_token(sid, name, email):
    payload = {
        'id'   : sid,
        'name' : name,
        'email': email,
        'exp'  : datetime.utcnow() + timedelta(hours=24)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')

def read_token(token):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except:
        return None

def login_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        token = request.cookies.get('token')
        if not token:
            return redirect(url_for('login'))
        data = read_token(token)
        if not data:
            r = make_response(redirect(url_for('login')))
            r.delete_cookie('token')
            return r
        request.student = data
        return f(*args, **kwargs)
    return wrap

def log(sid, action):
    try:
        db = get_db()
        db.execute("INSERT INTO activity_log (student_id,action) VALUES (?,?)", (sid, action))
        db.commit()
        db.close()
    except: pass


def grade_from_pct(p):
    if p >= 90: return "A+"
    if p >= 80: return "A"
    if p >= 70: return "B+"
    if p >= 60: return "B"
    if p >= 50: return "C"
    if p >= 40: return "D"
    return "F"

def scrape_pdf(path):
    results  = []
    raw_text = ""

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:

            # Try table extraction first
            for table in (page.extract_tables() or []):
                for row in table:
                    if not row: continue
                    row = [str(c).strip() if c else "" for c in row]
                    if any(h in row[0].lower() for h in
                           ["subject","course","paper","sr","s.no","code","sl","name"]):
                        continue
                    nums = [c for c in row if re.match(r"^\d+\.?\d*$", c)]
                    if nums and len(row) >= 2:
                        subj = re.sub(r"^\d+[\.\)]\s*", "", row[0]).strip()
                        if len(subj) < 2: continue
                        obt = float(nums[0])
                        mx  = float(nums[1]) if len(nums) > 1 else 100
                        pct = round((obt / mx) * 100, 2) if mx else 0
                        grd = next((c.strip().upper() for c in row
                                    if re.match(r"^[A-Fa-f][+\-]?$", c.strip())), grade_from_pct(pct))
                        results.append({"subject": subj[:80], "obtained": obt,
                                        "max_marks": mx, "percentage": pct, "grade": grd})

            raw_text += (page.extract_text() or "") + "\n"

    # Fallback text parsing
    if not results:
        seen = set()
        for line in raw_text.split("\n"):
            line = line.strip()
            if len(line) < 5: continue
            for pat in [
                r"([A-Za-z][A-Za-z &\(\)/\-]{3,50})\s+(\d{1,3})\s+(\d{2,3})\s*([A-Fa-f][+\-]?)?",
                r"([A-Za-z][A-Za-z &\(\)/\-]{3,50})\s+(\d{1,3})\s*/\s*(\d{2,3})",
                r"([A-Za-z][A-Za-z &\(\)/\-]{3,50})\s{2,}(\d{1,3})\b",
            ]:
                m = re.search(pat, line)
                if m:
                    g    = m.groups()
                    subj = re.sub(r"^\d+[\.\)]\s*", "", g[0].strip()).strip()
                    if subj.lower() in seen or len(subj) < 3: continue
                    seen.add(subj.lower())
                    obt = float(g[1]) if g[1] else 0
                    mx  = float(g[2]) if len(g) > 2 and g[2] else 100
                    grd = g[3].upper() if len(g) > 3 and g[3] else ""
                    pct = round((obt / mx) * 100, 2) if mx else 0
                    if not grd: grd = grade_from_pct(pct)
                    if obt <= mx:
                        results.append({"subject": subj[:80], "obtained": obt,
                                        "max_marks": mx, "percentage": pct, "grade": grd})
                    break
    return results

def summary(marks):
    if not marks: return {}
    tot_obt = sum(m["obtained"]  for m in marks)
    tot_max = sum(m["max_marks"] for m in marks)
    pct     = round((tot_obt / tot_max) * 100, 2) if tot_max else 0
    srt     = sorted(marks, key=lambda x: x["percentage"])
    return {
        "total_obtained":    round(tot_obt, 1),
        "total_max":         round(tot_max, 1),
        "overall_percentage":pct,
        "overall_grade":     grade_from_pct(pct),
        "total_subjects":    len(marks),
        "weakest":           srt[0]["subject"]  if srt else "—",
        "strongest":         srt[-1]["subject"] if srt else "—",
    }


# ══════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════

@app.route("/")
def home():
    t = request.cookies.get("token")
    if t and read_token(t):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# ── LOGIN ─────────────────────────────────────────────
# Connects with your existing login.html
@app.route("/login", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email","").strip()
        pw    = request.form.get("password","").strip()
        db    = get_db()
        row   = db.execute(
            "SELECT * FROM students WHERE LOWER(email)=LOWER(?)", (email,)
        ).fetchone()
        db.close()
        if row and check_pw(pw, row["password"]):
            token = make_token(row["id"], row["name"], row["email"])
            log(row["id"], "Logged in")
            r = make_response(redirect(url_for("dashboard")))
            r.set_cookie("token", token, httponly=True, max_age=86400)
            return r
        error = "Email or password is incorrect."
    return render_template("login.html", error=error)


# ── REGISTER ──────────────────────────────────────────
# Connects with your existing register.html
@app.route("/register", methods=["GET","POST"])
def register():
    error = success = None
    if request.method == "POST":
        name  = request.form.get("name",       "").strip()
        email = request.form.get("email",      "").strip().lower()
        pw    = request.form.get("password",   "").strip()
        roll  = request.form.get("roll_no",    "").strip()
        uni   = request.form.get("university", "").strip()
        course= request.form.get("course",     "").strip()
        sem   = request.form.get("semester",   "").strip()
        subjs = request.form.get("subjects",   "").strip()
        stime = request.form.get("study_time", "").strip()

        if not name or not email or not pw:
            error = "Name, email and password are required."
        else:
            db = get_db()
            if db.execute("SELECT id FROM students WHERE LOWER(email)=LOWER(?)",(email,)).fetchone():
                error = "This email is already registered."
                db.close()
            else:
                db.execute("""
                    INSERT INTO students
                    (name,email,password,roll_no,university,course,semester,subjects,study_time)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (name, email, hash_pw(pw), roll, uni, course, sem, subjs, stime))
                db.commit()
                sid = db.execute(
                    "SELECT id FROM students WHERE LOWER(email)=LOWER(?)",(email,)
                ).fetchone()["id"]
                db.close()
                log(sid, "Account created")
                success = "Account created! You can now login."
    return render_template("register.html", error=error, success=success)


# ── DASHBOARD ─────────────────────────────────────────
# Connects with your existing dashboard.html
@app.route("/dashboard")
@login_required
def dashboard():
    sid = request.student["id"]
    db  = get_db()

    student = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()

    # Build memory dict — matches exactly what your dashboard.html expects
    subj_list = [s.strip() for s in (student["subjects"] or "").split(",") if s.strip()]

    memory = {
        "profile": {
            "name":       student["name"],
            "email":      student["email"],
            "roll_no":    student["roll_no"]    or "",
            "university": student["university"] or "",
            "course":     student["course"]     or "",
            "semester":   student["semester"]   or "",
            "joined":     student["created_at"] or "",
        },
        "preferences": {
            "subjects":   subj_list,
            "study_time": student["study_time"] or "",
        },
        "deadlines":    [],
        "activity_log": [],
        "agent_notes":  student["agent_notes"] or "",
        "last_updated": datetime.now().strftime("%d %b %Y, %I:%M %p"),
    }

    # Deadlines
    dls = db.execute(
        "SELECT * FROM deadlines WHERE student_id=? AND status='pending' ORDER BY date ASC",
        (sid,)
    ).fetchall()
    memory["deadlines"] = [dict(d) for d in dls]

    # Activity log
    acts = db.execute(
        "SELECT * FROM activity_log WHERE student_id=? ORDER BY time DESC LIMIT 6",
        (sid,)
    ).fetchall()
    memory["activity_log"] = [{"action": a["action"], "time": a["time"]} for a in acts]

    db.close()
    log(sid, "Visited dashboard")
    return render_template("dashboard.html", memory=memory)


# ── ADD DEADLINE ──────────────────────────────────────
@app.route("/add_deadline", methods=["POST"])
@login_required
def add_deadline():
    sid  = request.student["id"]
    subj = request.form.get("subject","")
    task = request.form.get("task","")
    date = request.form.get("date","")
    db   = get_db()
    db.execute(
        "INSERT INTO deadlines (student_id,subject,task,date) VALUES (?,?,?,?)",
        (sid, subj, task, date)
    )
    db.commit()
    db.close()
    log(sid, f"Added deadline: {task}")
    return redirect(url_for("dashboard"))


# ── UPDATE MEMORY ─────────────────────────────────────
@app.route("/update", methods=["POST"])
@login_required
def update():
    sid   = request.student["id"]
    subjs = request.form.get("subjects",   "")
    stime = request.form.get("study_time", "")
    note  = request.form.get("agent_note", "")
    db    = get_db()
    db.execute(
        "UPDATE students SET subjects=?, study_time=?, agent_notes=? WHERE id=?",
        (subjs, stime, note, sid)
    )
    db.commit()
    db.close()
    log(sid, "Updated preferences")
    return redirect(url_for("dashboard"))


# ── MARKSHEET — PDF Upload + Auto Scrape + Table ──────
@app.route("/marksheet", methods=["GET","POST"])
@login_required
def marksheet():
    sid     = request.student["id"]
    message = error = None
    scraped = []
    smry    = {}

    if request.method == "POST":
        f    = request.files.get("pdf_file")
        slbl = request.form.get("semester_label","").strip()

        if not f or f.filename == "":
            error = "Please select a PDF file."
        elif not f.filename.lower().endswith(".pdf"):
            error = "Only PDF files are accepted."
        else:
            fname = f"{sid}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
            path  = os.path.join(UPLOAD_FOLDER, fname)
            f.save(path)
            try:
                scraped = scrape_pdf(path)
                if not scraped:
                    error = "Could not extract marks. Make sure PDF is text-based, not a scanned image."
                    os.remove(path)
                else:
                    smry = summary(scraped)
                    db   = get_db()
                    db.execute(
                        "INSERT INTO marksheets (student_id,semester_label) VALUES (?,?)",
                        (sid, slbl)
                    )
                    db.commit()
                    ms_id = db.execute(
                        "SELECT id FROM marksheets WHERE student_id=? ORDER BY id DESC LIMIT 1",
                        (sid,)
                    ).fetchone()["id"]
                    for row in scraped:
                        db.execute("""
                            INSERT INTO marks
                            (marksheet_id,student_id,subject,obtained,max_marks,percentage,grade)
                            VALUES (?,?,?,?,?,?,?)
                        """, (ms_id, sid, row["subject"], row["obtained"],
                              row["max_marks"], row["percentage"], row["grade"]))
                    db.commit()
                    db.close()
                    log(sid, f"Uploaded marksheet: {slbl} ({len(scraped)} subjects)")
                    message = f"{len(scraped)} subjects extracted and saved to database!"
            except Exception as e:
                error = f"Error: {str(e)}"
                if os.path.exists(path): os.remove(path)

    # Load history
    db         = get_db()
    student    = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    marksheets = db.execute(
        "SELECT * FROM marksheets WHERE student_id=? ORDER BY uploaded_at DESC", (sid,)
    ).fetchall()
    all_marks = {}
    for ms in marksheets:
        rows = db.execute(
            "SELECT * FROM marks WHERE marksheet_id=? ORDER BY percentage DESC",
            (ms["id"],)
        ).fetchall()
        all_marks[ms["id"]] = rows
    db.close()

    return render_template("marksheet.html",
                           student    = student,
                           marksheets = marksheets,
                           all_marks  = all_marks,
                           scraped    = scraped,
                           summary    = smry,
                           message    = message,
                           error      = error)


# ── DEADLINES PAGE — full CRUD table ──────────────────
@app.route("/deadlines")
@login_required
def deadlines_page():
    sid = request.student["id"]
    db  = get_db()
    dls = db.execute(
        "SELECT * FROM deadlines WHERE student_id=? ORDER BY date ASC", (sid,)
    ).fetchall()
    student = db.execute("SELECT name FROM students WHERE id=?", (sid,)).fetchone()
    db.close()
    return render_template("deadlines.html", deadlines=dls, student=student)

@app.route("/deadline/done/<int:did>")
@login_required
def deadline_done(did):
    sid = request.student["id"]
    db  = get_db()
    db.execute("UPDATE deadlines SET status='done' WHERE id=? AND student_id=?", (did, sid))
    db.commit()
    db.close()
    log(sid, "Marked deadline done")
    return redirect(url_for("deadlines_page"))

@app.route("/deadline/delete/<int:did>")
@login_required
def delete_deadline(did):
    sid = request.student["id"]
    db  = get_db()
    db.execute("DELETE FROM deadlines WHERE id=? AND student_id=?", (did, sid))
    db.commit()
    db.close()
    log(sid, "Deleted deadline")
    return redirect(url_for("deadlines_page"))



# ── A: AI CHAT ────────────────────────────────────────
@app.route("/ai/chat", methods=["POST"])
@login_required
def ai_chat():
    sid      = request.student["id"]
    question = request.json.get("question", "").strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400
    db      = get_db()
    student = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    db.close()
    system = f"""You are a smart AI study assistant for {student["name"]},
a {student["course"]} student at {student["university"]}, Semester {student["semester"]}.
Their subjects are: {student["subjects"]}.
Be helpful, concise, and encouraging. Answer in simple English."""
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system",  "content": system},
                {"role": "user",    "content": question}
            ],
            max_tokens=500
        )
        answer = resp.choices[0].message.content
        log(sid, f"AI Chat: {question[:40]}")
        return jsonify({"answer": answer})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── B: AI MARKSHEET ANALYZER ──────────────────────────
@app.route("/ai/analyze", methods=["POST"])
@login_required
def ai_analyze():
    sid = request.student["id"]
    db  = get_db()
    student = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    marksheets = db.execute(
        "SELECT * FROM marksheets WHERE student_id=? ORDER BY uploaded_at DESC LIMIT 1",
        (sid,)
    ).fetchall()
    marks_text = ""
    for ms in marksheets:
        rows = db.execute(
            "SELECT * FROM marks WHERE marksheet_id=?", (ms["id"],)
        ).fetchall()
        for r in rows:
            marks_text += f"{r['subject']}: {r['obtained']}/{r['max_marks']} ({r['percentage']}%) Grade: {r['grade']}\n"
    db.close()
    if not marks_text:
        return jsonify({"error": "No marksheet found. Please upload one first!"}), 400
    prompt = f"""Student: {student["name"]}, {student["course"]}, Semester {student["semester"]}
Marksheet:
{marks_text}
Give a detailed analysis:
1. Overall performance summary
2. Top 2 strongest subjects
3. Top 2 weakest subjects needing improvement
4. Specific study tips for weak subjects
5. Motivational message
Be friendly and encouraging."""
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600
        )
        analysis = resp.choices[0].message.content
        log(sid, "AI Marksheet Analysis")
        return jsonify({"analysis": analysis})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── C: AI STUDY PLANNER ───────────────────────────────
@app.route("/ai/planner", methods=["POST"])
@login_required
def ai_planner():
    sid = request.student["id"]
    db  = get_db()
    student = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    deadlines = db.execute(
        "SELECT * FROM deadlines WHERE student_id=? AND status=\'pending\' ORDER BY date ASC",
        (sid,)
    ).fetchall()
    db.close()
    if not deadlines:
        return jsonify({"error": "No pending deadlines found. Add some deadlines first!"}), 400
    dl_text = ""
    for d in deadlines:
        dl_text += f"- {d['subject']}: {d['task']} (Due: {d['date']})\n"
    prompt = f"""Student: {student["name"]}, {student["course"]}, Semester {student["semester"]}
Study Time Preference: {student["study_time"] or "Not specified"}
Subjects: {student["subjects"]}
Pending Deadlines:
{dl_text}
Create a practical day-by-day study plan to complete all deadlines on time.
Include:
1. Priority order of tasks
2. Daily study schedule based on their preferred study time
3. Time allocation per subject
4. Tips to stay focused
Keep it simple and actionable."""
    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700
        )
        plan = resp.choices[0].message.content
        log(sid, "AI Study Plan Generated")
        return jsonify({"plan": plan})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── AI PAGE ───────────────────────────────────────────
@app.route("/ai")
@login_required
def ai_page():
    sid = request.student["id"]
    db  = get_db()
    student = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    db.close()
    log(sid, "Visited AI page")
    return render_template("ai.html", student=student)

# ── LOGOUT ────────────────────────────────────────────
@app.route("/logout")
def logout():
    t = request.cookies.get("token")
    if t:
        d = read_token(t)
        if d: log(d["id"], "Logged out")
    r = make_response(redirect(url_for("login")))
    r.delete_cookie("token")
    return r


# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    print("\n" + "="*50)
    print("  AI Student Agent — JWT + SQLite + PDF")
    print("  Open: http://127.0.0.1:5000")
    print("="*50 + "\n")
    app.run(debug=True)