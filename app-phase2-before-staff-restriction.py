import os, sqlite3, csv, io, secrets
from pathlib import Path
from datetime import timedelta
from datetime import date, datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, make_response, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv('DATABASE_PATH', os.path.join(BASE_DIR, 'attendancepro.db'))
REPORT_DIR = os.path.join(BASE_DIR, 'reports')
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY') or 'dev-only-change-this-secret-' + secrets.token_hex(12)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY AUTOINCREMENT, student_id TEXT UNIQUE, name TEXT NOT NULL,
 email TEXT UNIQUE, department TEXT DEFAULT 'ECE', year INTEGER DEFAULT 2, class_name TEXT DEFAULT 'A',
 role TEXT NOT NULL DEFAULT 'student', password_hash TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, must_change_password INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS departments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS classes (id INTEGER PRIMARY KEY AUTOINCREMENT, department TEXT NOT NULL, class_name TEXT NOT NULL, year INTEGER NOT NULL DEFAULT 2, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(department,class_name));
CREATE TABLE IF NOT EXISTS subjects (
 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 department TEXT DEFAULT 'ECE', year INTEGER DEFAULT 2, faculty TEXT DEFAULT '', class_name TEXT NOT NULL DEFAULT '*', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS attendance (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE, attendance_date TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('Present','Absent','OD','Leave')), note TEXT DEFAULT '',
 marked_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(user_id, subject_id, attendance_date)
);
CREATE TABLE IF NOT EXISTS notices (
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL,
 audience TEXT NOT NULL DEFAULT 'all', created_by INTEGER REFERENCES users(id), target_type TEXT NOT NULL DEFAULT 'all', target_department TEXT DEFAULT '', target_class TEXT DEFAULT '', event_start TEXT, event_end TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit_logs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT NOT NULL, details TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS password_reset_requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, status TEXT NOT NULL DEFAULT 'pending', requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, handled_at TEXT, handled_by INTEGER REFERENCES users(id)
);
'''

def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.execute('PRAGMA busy_timeout=5000')
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys=ON')
    return g.db

@app.teardown_appcontext
def close_db(_=None):
    conn = g.pop('db', None)
    if conn is not None: conn.close()

def log_action(action, details=''):
    db().execute('INSERT INTO audit_logs(actor_id, action, details) VALUES(?,?,?)', (session.get('user_id'), action, details))
    db().commit()

def init_db():
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row; conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(SCHEMA)
    # Indexes keep roster and daily attendance queries responsive as the campus grows.
    conn.executescript('''
      CREATE INDEX IF NOT EXISTS idx_users_role_active_class ON users(role,active,department,class_name);
      CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance(attendance_date);
      CREATE INDEX IF NOT EXISTS idx_attendance_user_date ON attendance(user_id,attendance_date);
      CREATE INDEX IF NOT EXISTS idx_attendance_subject_date ON attendance(subject_id,attendance_date);
    ''')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    # Safe migrations for databases created by earlier Nova builds.
    cols = {r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()}
    if 'class_name' not in cols: conn.execute("ALTER TABLE users ADD COLUMN class_name TEXT DEFAULT 'A'")
    if 'must_change_password' not in cols: conn.execute('ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0')
    subject_cols = {r[1] for r in conn.execute('PRAGMA table_info(subjects)').fetchall()}
    if 'class_name' not in subject_cols: conn.execute("ALTER TABLE subjects ADD COLUMN class_name TEXT NOT NULL DEFAULT '*'")
    notice_cols = {r[1] for r in conn.execute('PRAGMA table_info(notices)').fetchall()}
    for col, ddl in [('target_type', "TEXT NOT NULL DEFAULT 'all'"), ('target_department', "TEXT DEFAULT ''"), ('target_class', "TEXT DEFAULT ''"), ('event_start', 'TEXT'), ('event_end', 'TEXT')]:
        if col not in notice_cols: conn.execute(f'ALTER TABLE notices ADD COLUMN {col} {ddl}')
    for dep in ('ECE','CSE','EEE','IT','MECH','CIVIL','AI&DS'): conn.execute('INSERT OR IGNORE INTO departments(name) VALUES(?)',(dep,))
    # Backfill class catalog for databases created before class management existed.
    for row in conn.execute("SELECT DISTINCT department, COALESCE(class_name,'A') class_name, COALESCE(year,2) year FROM users WHERE role IN ('student','staff') AND department IS NOT NULL AND department<>''").fetchall():
        conn.execute('INSERT OR IGNORE INTO classes(department,class_name,year) VALUES(?,?,?)',(row['department'],row['class_name'],row['year'] or 2))
    for row in conn.execute("SELECT DISTINCT department, class_name, year FROM subjects WHERE class_name<>'*' AND department IS NOT NULL").fetchall():
        conn.execute('INSERT OR IGNORE INTO classes(department,class_name,year) VALUES(?,?,?)',(row['department'],row['class_name'],row['year'] or 2))
    conn.execute("INSERT OR IGNORE INTO classes(department,class_name,year) VALUES('ECE','A',2)")
    if not conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone():
        conn.execute('INSERT INTO users(student_id,name,email,department,year,role,password_hash) VALUES(NULL,?,?,?,?,?,?)',
            ('Administrator', 'admin@attendancepro.local', 'Administration', 0, 'admin', generate_password_hash(os.getenv('ADMIN_PASSWORD','Admin@12345'))))
    if not conn.execute('SELECT id FROM users WHERE role="student" LIMIT 1').fetchone():
        conn.execute('INSERT INTO users(student_id,name,email,department,year,role,password_hash) VALUES(?,?,?,?,?,?,?)',
            ('STU0001','Demo Student','student@attendancepro.local','ECE',2,'student',generate_password_hash(os.getenv('DEMO_STUDENT_PASSWORD','Student@123'))))
    defaults = [('EC201','Electromagnetic Fields','ECE',2,'Dr. Kumar'),('EC202','Electronic Circuits','ECE',2,'Dr. Priya'),('MA201','Engineering Mathematics','ECE',2,'Dr. Anand'),('CS201','Python Programming','CSE',2,'Ms. Divya')]
    for item in defaults:
        conn.execute('INSERT OR IGNORE INTO subjects(code,name,department,year,faculty) VALUES(?,?,?,?,?)',item)
    conn.commit(); conn.close()

def _write_weekly_report(start_day, end_day):
    """Create a CSV archive for a date range, including student and class details."""
    os.makedirs(REPORT_DIR, exist_ok=True)
    filename = f"attendance-week-{start_day.isoformat()}-to-{end_day.isoformat()}.csv"
    path = os.path.join(REPORT_DIR, filename)
    if os.path.exists(path):
        return path
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("""SELECT u.student_id, u.name, u.email, u.department, COALESCE(u.class_name,'A'), u.year,
            s.code, s.name, a.attendance_date, a.status, COALESCE(a.note,''), COALESCE(mark.name,'')
            FROM attendance a JOIN users u ON u.id=a.user_id JOIN subjects s ON s.id=a.subject_id
            LEFT JOIN users mark ON mark.id=a.marked_by
            WHERE a.attendance_date BETWEEN ? AND ? ORDER BY u.department, u.class_name, u.student_id, a.attendance_date, s.code""",
            (start_day.isoformat(), end_day.isoformat())).fetchall()
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(['Student ID','Student Name','Email','Department','Class','Year','Subject Code','Subject','Attendance Date','Status','Note','Marked By'])
            writer.writerows(rows)
        return path
    finally:
        conn.close()

@app.before_request
def archive_previous_week_if_due():
    # Automatic weekly archive: on Monday, the first request archives the full previous week.
    # For always-on production scheduling, configure a platform cron job as described in README.
    now = date.today()
    if now.weekday() == 0:
        end_day = now - timedelta(days=1)
        start_day = end_day - timedelta(days=6)
        try:
            _write_weekly_report(start_day, end_day)
        except (sqlite3.Error, OSError):
            app.logger.exception('Could not create weekly attendance report')

@app.before_request
def expire_closed_events():
    # Event announcements are automatically removed once their end date/time has passed.
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('DELETE FROM notices WHERE event_end IS NOT NULL AND event_end <= ?', (datetime.now().strftime('%Y-%m-%dT%H:%M'),))
        conn.commit(); conn.close()
    except sqlite3.Error:
        pass

@app.before_request
def csrf_and_active_user():
    if 'csrf_token' not in session: session['csrf_token'] = secrets.token_urlsafe(24)
    if session.get('user_id'):
        g.current_user = db().execute('SELECT * FROM users WHERE id=? AND active=1', (session['user_id'],)).fetchone()
        if not g.current_user:
            session.clear()

@app.context_processor
def inject_globals():
    return {'csrf_token': session.get('csrf_token',''), 'current_user': getattr(g,'current_user',None), 'today': date.today().isoformat()}

@app.before_request
def enforce_forced_password_change():
    if getattr(g, 'current_user', None) and g.current_user['role']=='student' and g.current_user['must_change_password']:
        allowed = {'force_password_change', 'logout', 'static'}
        if request.endpoint not in allowed:
            return redirect(url_for('force_password_change'))

@app.before_request
def csrf_protect():
    if request.method == 'POST':
        supplied = request.form.get('csrf_token','')
        if not supplied or not secrets.compare_digest(supplied, session.get('csrf_token','')):
            return 'Security token expired. Refresh the page and try again.', 400

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not getattr(g,'current_user',None): return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapped

def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        if g.current_user['role'] != 'admin':
            flash('Administrator access is required.','error'); return redirect(url_for('student_dashboard'))
        return fn(*args, **kwargs)
    return wrapped

@app.route('/')
def index():
    if getattr(g,'current_user',None):
        return redirect(url_for('admin_dashboard' if g.current_user['role']=='admin' else ('staff_dashboard' if g.current_user['role']=='staff' else 'student_dashboard')))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        identifier = request.form.get('identifier','').strip()
        password = request.form.get('password','')
        selected_role = request.form.get('login_role','student').strip().lower()
        if selected_role not in ('admin','staff','student'):
            flash('Choose Admin, Staff, or Student before signing in.','error')
            return render_template('login.html')
        user = db().execute('SELECT * FROM users WHERE active=1 AND role=? AND (email=? COLLATE NOCASE OR student_id=? COLLATE NOCASE)', (selected_role, identifier.strip(),identifier.strip())).fetchone()
        if user and check_password_hash(user['password_hash'],password):
            session.clear(); session['user_id']=user['id']; session['csrf_token']=secrets.token_urlsafe(24)
            log_action('login', f"Successful login: {user['name']}")
            if user['role']=='student' and user['must_change_password']:
                return redirect(url_for('force_password_change'))
            return redirect(url_for('admin_dashboard' if user['role']=='admin' else ('staff_dashboard' if user['role']=='staff' else 'student_dashboard')))
        flash('Invalid login details. Please try again.','error')
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    log_action('logout','User logged out'); session.clear(); flash('You have been signed out.','success'); return redirect(url_for('login'))

@app.route('/admin')
@admin_required
def admin_dashboard():
    conn=db(); total_students=conn.execute("SELECT COUNT(*) FROM users WHERE role='student' AND active=1").fetchone()[0]
    total_subjects=conn.execute('SELECT COUNT(*) FROM subjects').fetchone()[0]
    today_total=conn.execute('SELECT COUNT(*) FROM attendance WHERE attendance_date=?',(date.today().isoformat(),)).fetchone()[0]
    present=conn.execute("SELECT COUNT(*) FROM attendance WHERE attendance_date=? AND status='Present'",(date.today().isoformat(),)).fetchone()[0]
    # Show attendance sessions, not one row per student. Records saved together are
    # summarized by teacher, class, subject, date and minute of creation.
    records=conn.execute('''
        SELECT COALESCE(m.name, 'Unknown staff') AS teacher_name,
               COALESCE(u.department, '—') AS department,
               COALESCE(u.year, 0) AS year,
               COALESCE(u.class_name, '—') AS class_name,
               s.code AS subject_code, s.name AS subject_name,
               a.attendance_date AS attendance_date,
               substr(a.created_at, 1, 16) AS marked_at,
               COUNT(*) AS class_total,
               SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END) AS present_count,
               SUM(CASE WHEN a.status='Absent' THEN 1 ELSE 0 END) AS absent_count,
               SUM(CASE WHEN a.status='OD' THEN 1 ELSE 0 END) AS od_count,
               SUM(CASE WHEN a.status='Leave' THEN 1 ELSE 0 END) AS leave_count
        FROM attendance a
        JOIN users u ON u.id=a.user_id
        JOIN subjects s ON s.id=a.subject_id
        LEFT JOIN users m ON m.id=a.marked_by
        GROUP BY a.marked_by, u.department, u.year, u.class_name, a.subject_id,
                 a.attendance_date, substr(a.created_at, 1, 16)
        ORDER BY a.attendance_date DESC, marked_at DESC
        LIMIT 8
    ''').fetchall()
    students=conn.execute("SELECT * FROM users WHERE role='student' ORDER BY created_at DESC LIMIT 5").fetchall()
    month=conn.execute("SELECT substr(attendance_date,1,7) m, COUNT(*) total, SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END) present FROM attendance WHERE attendance_date>=date('now','-5 months') GROUP BY m ORDER BY m").fetchall()
    return render_template('admin.html', total_students=total_students,total_subjects=total_subjects,today_total=today_total,present=present,records=records,students=students,month=month)

@app.route('/admin/classes', methods=['GET','POST'])
@admin_required
def classes_page():
    conn = db()
    departments = conn.execute('SELECT name FROM departments ORDER BY name').fetchall()
    if request.method == 'POST':
        department = request.form.get('department', '').strip().upper()
        class_name = request.form.get('class_name', '').strip().upper()
        year = request.form.get('year', '2')
        if not department or not class_name or len(class_name) > 24:
            flash('Enter a department and a class/section name (maximum 24 characters).', 'error')
        else:
            try:
                conn.execute('INSERT INTO classes(department,class_name,year) VALUES(?,?,?)', (department,class_name,int(year)))
                log_action('class_created', f'{department}/{class_name}, year {year}')
                conn.commit(); flash(f'Class {department} / {class_name} created.', 'success')
            except sqlite3.IntegrityError:
                conn.rollback(); flash('That class already exists in this department, or the department is invalid.', 'error')
            except ValueError:
                conn.rollback(); flash('Choose a valid year.', 'error')
        return redirect(url_for('classes_page'))
    classes = conn.execute("SELECT c.*, (SELECT COUNT(*) FROM users u WHERE u.role='student' AND u.department=c.department AND u.class_name=c.class_name AND u.active=1) student_count, (SELECT COUNT(*) FROM subjects s WHERE s.department=c.department AND (s.class_name=c.class_name OR s.class_name='*')) subject_count FROM classes c ORDER BY c.department,c.year,c.class_name").fetchall()
    return render_template('classes.html', classes=classes, departments=departments)

@app.route('/admin/classes/<int:class_id>/delete', methods=['POST'])
@admin_required
def delete_class(class_id):
    conn = db()
    cls = conn.execute('SELECT * FROM classes WHERE id=?', (class_id,)).fetchone()
    if not cls:
        flash('Class not found.', 'error')
    else:
        students = conn.execute("SELECT COUNT(*) FROM users WHERE role='student' AND department=? AND class_name=?", (cls['department'],cls['class_name'])).fetchone()[0]
        staff = conn.execute("SELECT COUNT(*) FROM users WHERE role='staff' AND department=? AND class_name=?", (cls['department'],cls['class_name'])).fetchone()[0]
        if students or staff:
            flash('Cannot delete a class that still has students or staff assigned. Move those accounts first.', 'error')
        else:
            conn.execute('DELETE FROM classes WHERE id=?', (class_id,)); log_action('class_deleted', f"{cls['department']}/{cls['class_name']}"); conn.commit(); flash('Class deleted.', 'success')
    return redirect(url_for('classes_page'))

@app.route('/admin/students', methods=['GET','POST'])
@admin_required
def students_page():
    conn=db()
    if request.method=='POST':
        sid=request.form.get('student_id','').strip().upper(); name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); dept=request.form.get('department','ECE').strip().upper(); year=request.form.get('year','2'); class_name=request.form.get('class_name','A').strip().upper(); password='1234567890'
        try:
            year_num=int(year)
            if year_num not in (1,2,3,4): raise ValueError
        except ValueError:
            year_num=0
        if not sid or not name or not email or year_num not in (1,2,3,4):
            flash('Enter student ID, full name, valid email, and choose academic year 1–4. No account was created.','error')
        else:
            class_row=conn.execute('SELECT year FROM classes WHERE department=? AND class_name=?',(dept,class_name)).fetchone()
            if not class_row:
                flash('That department/class does not exist. Create it in Classes first. No account was created.','error')
            elif int(class_row['year']) != year_num:
                flash(f'Year mismatch: you selected Year {year_num}, but {dept} / {class_name} belongs to Year {class_row["year"]}. Select a matching class. No account was created.','error')
            else:
                try:
                    conn.execute("INSERT INTO users(student_id,name,email,department,year,class_name,role,password_hash,must_change_password) VALUES(?,?,?,?,?,?,'student',?,1)",(sid,name,email,dept,year_num,class_name,generate_password_hash(password)))
                    log_action('student_created',f'{sid} · {name} · {dept} Year {year_num} / {class_name}'); conn.commit(); flash(f'Student {sid} created. Initial password: 1234567890. Student must change it at first login.','success')
                except sqlite3.IntegrityError: conn.rollback(); flash('Student ID or email already exists. No account was created.','error')
        return redirect(url_for('students_page'))
    q=request.args.get('q','').strip()
    dept_filter=request.args.get('department','').strip().upper()
    year_filter=request.args.get('year','').strip()
    where="role='student' AND (student_id LIKE ? OR name LIKE ? OR email LIKE ?)"
    args=[f'%{q}%',f'%{q}%',f'%{q}%']
    if dept_filter:
        where += ' AND department=?'; args.append(dept_filter)
    if year_filter in ('1','2','3','4'):
        where += ' AND year=?'; args.append(int(year_filter))
    class_filter=request.args.get('class_name','').strip().upper()
    if class_filter:
        where += ' AND class_name=?'; args.append(class_filter)
    students=conn.execute(f"SELECT * FROM users WHERE {where} ORDER BY active DESC,name",args).fetchall()
    classes=conn.execute('SELECT * FROM classes ORDER BY department,year,class_name').fetchall()
    departments=conn.execute('SELECT name FROM departments ORDER BY name').fetchall()
    campus=conn.execute("SELECT department,year,class_name,COUNT(*) total FROM users WHERE role='student' GROUP BY department,year,class_name ORDER BY department,year,class_name").fetchall()
    return render_template('students.html',students=students,q=q,classes=classes,departments=departments,campus=campus,dept_filter=dept_filter,year_filter=year_filter,class_filter=class_filter)

@app.route('/admin/students/<int:user_id>/delete', methods=['POST'])
@admin_required
def delete_student(user_id):
    conn=db()
    student=conn.execute("SELECT * FROM users WHERE id=? AND role='student'",(user_id,)).fetchone()
    if not student:
        flash('Student account was not found.','error')
    else:
        sid=student['student_id']
        # Foreign-key cascade removes attendance and pending reset requests for this account.
        conn.execute('DELETE FROM users WHERE id=?',(user_id,))
        log_action('student_deleted',f'{sid} · {student["name"]}')
        conn.commit()
        flash(f'Student {sid} and their linked attendance/reset-request records were deleted.','success')
    return redirect(url_for('students_page'))

@app.route('/admin/students/<int:user_id>/toggle', methods=['POST'])
@admin_required
def toggle_student(user_id):
    user=db().execute("SELECT * FROM users WHERE id=? AND role='student'",(user_id,)).fetchone()
    if user:
        db().execute('UPDATE users SET active=? WHERE id=?',(0 if user['active'] else 1,user_id)); log_action('student_status_changed',user['student_id']); db().commit(); flash('Student status updated.','success')
    return redirect(url_for('students_page'))

@app.route('/admin/subjects', methods=['GET','POST'])
@admin_required
def subjects_page():
    conn = db()
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        name = request.form.get('name', '').strip()
        dept = request.form.get('department', '').strip().upper()
        year = request.form.get('year', '2')
        faculty = request.form.get('faculty', '').strip()
        class_name = request.form.get('class_name', '*').strip().upper() or '*'
        try:
            if not conn.execute('SELECT 1 FROM departments WHERE name=?', (dept,)).fetchone():
                raise ValueError('department')
            year_num = int(year)
            if year_num not in (1, 2, 3, 4) or not code or not name:
                raise ValueError('fields')
            if class_name != '*':
                class_row = conn.execute(
                    'SELECT year FROM classes WHERE department=? AND class_name=?',
                    (dept, class_name)
                ).fetchone()
                if not class_row:
                    raise ValueError('class')
                if int(class_row['year']) != year_num:
                    raise ValueError('year')
            conn.execute(
                'INSERT INTO subjects(code,name,department,year,faculty,class_name) VALUES(?,?,?,?,?,?)',
                (code, name, dept, year_num, faculty, class_name)
            )
            log_action('subject_created', f'{code} · {name} · {dept}')
            conn.commit()
            flash(f'Subject {code} added to {dept}.', 'success')
        except sqlite3.IntegrityError:
            conn.rollback()
            flash('Subject code must be unique. No subject was added.', 'error')
        except (ValueError, TypeError):
            conn.rollback()
            flash('Check the department, year, and class. The selected class must belong to the department and year.', 'error')
        return redirect(url_for('subjects_page', department=dept))

    departments = conn.execute('''
        SELECT d.name, COUNT(DISTINCT s.id) AS subject_count
        FROM departments d
        LEFT JOIN subjects s ON s.department = d.name
        GROUP BY d.id, d.name
        ORDER BY d.name
    ''').fetchall()
    selected_department = request.args.get('department', '').strip().upper()
    department_names = {row['name'] for row in departments}
    if selected_department not in department_names:
        selected_department = ''
    subjects = []
    classes = []
    if selected_department:
        subjects = conn.execute('''
            SELECT s.*, COUNT(a.id) AS records
            FROM subjects s
            LEFT JOIN attendance a ON a.subject_id = s.id
            WHERE s.department = ?
            GROUP BY s.id
            ORDER BY s.year, s.class_name, s.code
        ''', (selected_department,)).fetchall()
        classes = conn.execute(
            'SELECT department,class_name,year FROM classes WHERE department=? ORDER BY year,class_name',
            (selected_department,)
        ).fetchall()
    all_classes = conn.execute(
        'SELECT department,class_name,year FROM classes ORDER BY department,year,class_name'
    ).fetchall()
    return render_template(
        'subjects.html', subjects=subjects, departments=departments,
        selected_department=selected_department, classes=classes, all_classes=all_classes
    )

@app.route('/admin/attendance', methods=['GET','POST'])
@admin_required
def attendance_page():
    """Dean attendance workspace: select department/class/subject, then mark the whole roster."""
    conn = db()
    if request.method == 'POST':
        try:
            dept = request.form.get('department', '').strip()
            cls = request.form.get('class_name', '').strip()
            subject_id = int(request.form.get('subject_id', ''))
            dt = request.form.get('attendance_date', date.today().isoformat())
            datetime.strptime(dt, '%Y-%m-%d')
            if dt > date.today().isoformat(): raise ValueError('future date')
            subject = conn.execute("SELECT * FROM subjects WHERE id=? AND department=? AND (class_name='*' OR class_name=?)", (subject_id, dept, cls)).fetchone()
            if not dept or not cls or not subject: raise ValueError('class or subject')
            roster = conn.execute("SELECT id,student_id,name FROM users WHERE role='student' AND active=1 AND department=? AND class_name=? ORDER BY student_id COLLATE NOCASE", (dept, cls)).fetchall()
            if not roster: raise ValueError('empty class')
            allowed = {'Present', 'Absent', 'OD', 'Leave'}; counts = {status: 0 for status in allowed}
            for student in roster:
                status = request.form.get(f"status_{student['id']}", '')
                if status not in allowed: raise ValueError('status')
                note = request.form.get(f"note_{student['id']}", '').strip()[:250]
                conn.execute("""INSERT INTO attendance(user_id,subject_id,attendance_date,status,note,marked_by) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(user_id,subject_id,attendance_date) DO UPDATE SET status=excluded.status,note=excluded.note,marked_by=excluded.marked_by""",
                    (student['id'], subject_id, dt, status, note, g.current_user['id']))
                counts[status] += 1
            log_action('admin_class_attendance_saved', f'{dept}/{cls}, subject={subject_id}, date={dt}, counts={counts}')
            conn.commit()
            flash(f"Saved {len(roster)} students: {counts['Present']} present, {counts['Absent']} absent, {counts['OD']} OD, {counts['Leave']} leave.", 'success')
            return redirect(url_for('attendance_page', department=dept, class_name=cls, subject_id=subject_id, attendance_date=dt))
        except (ValueError, TypeError):
            conn.rollback(); flash('Please choose a valid department, class, subject and date. No attendance was saved.', 'error')
            return redirect(url_for('attendance_page'))
    departments = conn.execute("SELECT DISTINCT department FROM users WHERE role='student' AND active=1 AND department IS NOT NULL AND department<>'' ORDER BY department").fetchall()
    dept = request.args.get('department', (departments[0]['department'] if departments else '')).strip()
    classes = conn.execute("SELECT DISTINCT class_name FROM users WHERE role='student' AND active=1 AND department=? ORDER BY class_name", (dept,)).fetchall() if dept else []
    cls = request.args.get('class_name', (classes[0]['class_name'] if classes else '')).strip()
    subjects = conn.execute("SELECT * FROM subjects WHERE department=? AND (class_name='*' OR class_name=?) ORDER BY code", (dept, cls)).fetchall() if dept and cls else []
    selected_subject = request.args.get('subject_id', ''); dt = request.args.get('attendance_date', date.today().isoformat())
    try: datetime.strptime(dt, '%Y-%m-%d')
    except ValueError: dt = date.today().isoformat()
    roster = conn.execute("""SELECT u.id,u.student_id,u.name,a.status,a.note FROM users u
        LEFT JOIN attendance a ON a.user_id=u.id AND a.subject_id=? AND a.attendance_date=?
        WHERE u.role='student' AND u.active=1 AND u.department=? AND u.class_name=?
        ORDER BY u.student_id COLLATE NOCASE""", (int(selected_subject) if selected_subject.isdigit() else -1, dt, dept, cls)).fetchall() if dept and cls else []
    q = request.args.get('q', '').strip(); filter_date = request.args.get('date', '')
    # The recent register summarizes a saved attendance session instead of exposing
    # a row for every student. Session grouping uses the teacher, class, subject,
    # attendance date, and creation minute shared by the bulk save.
    total_college_students = conn.execute("SELECT COUNT(*) FROM users WHERE role='student' AND active=1").fetchone()[0]
    today = date.today().isoformat()
    today_counts = conn.execute("""SELECT
        SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END) AS present_count,
        SUM(CASE WHEN status='Absent' THEN 1 ELSE 0 END) AS absent_count,
        COUNT(*) AS marked_count
        FROM attendance WHERE attendance_date=?""", (today,)).fetchone()
    sql = """SELECT COALESCE(m.name,'Unknown staff') AS teacher_name,
        u.department, u.year, u.class_name, s.code AS subject_code, s.name AS subject_name,
        a.attendance_date, substr(a.created_at,1,16) AS marked_at,
        COUNT(*) AS class_total,
        SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END) AS present_count,
        SUM(CASE WHEN a.status='Absent' THEN 1 ELSE 0 END) AS absent_count,
        SUM(CASE WHEN a.status='OD' THEN 1 ELSE 0 END) AS od_count,
        SUM(CASE WHEN a.status='Leave' THEN 1 ELSE 0 END) AS leave_count
        FROM attendance a
        JOIN users u ON u.id=a.user_id
        JOIN subjects s ON s.id=a.subject_id
        LEFT JOIN users m ON m.id=a.marked_by
        WHERE (?='' OR m.name LIKE ? OR u.department LIKE ? OR u.class_name LIKE ? OR s.code LIKE ? OR s.name LIKE ?)
        AND (?='' OR a.attendance_date=?)
        GROUP BY a.marked_by,u.department,u.year,u.class_name,a.subject_id,a.attendance_date,substr(a.created_at,1,16)
        ORDER BY a.attendance_date DESC, marked_at DESC LIMIT 100"""
    like = f'%{q}%'
    records = conn.execute(sql, (q, like, like, like, like, like, filter_date, filter_date)).fetchall()
    return render_template('attendance.html', students=roster, subjects=subjects, records=records, q=q, filter_date=filter_date,
        departments=departments, classes=classes, selected_department=dept, selected_class=cls, selected_subject=selected_subject, attendance_date=dt,
        total_college_students=total_college_students, today_present=today_counts['present_count'] or 0,
        today_absent=today_counts['absent_count'] or 0, today_marked=today_counts['marked_count'] or 0)


@app.route('/admin/attendance-warnings')
@admin_required
def attendance_warnings_page():
    """Admin view: low-attendance students grouped by department, year and class."""
    conn = db()
    dept = request.args.get('department', '').strip()
    cls = request.args.get('class_name', '').strip()
    params = []
    where = "u.role='student' AND u.active=1"
    if dept:
        where += ' AND u.department=?'; params.append(dept)
    if cls:
        where += ' AND u.class_name=?'; params.append(cls)
    sql = ("SELECT u.id,u.student_id,u.name,u.email,u.department,u.year,u.class_name, "
           "COUNT(a.id) total, SUM(CASE WHEN a.status IN ('Present','OD') THEN 1 ELSE 0 END) attended, "
           "CASE WHEN COUNT(a.id)=0 THEN 0 ELSE ROUND(100.0*SUM(CASE WHEN a.status IN ('Present','OD') THEN 1 ELSE 0 END)/COUNT(a.id),1) END percentage "
           "FROM users u LEFT JOIN attendance a ON a.user_id=u.id WHERE " + where + " GROUP BY u.id "
           "HAVING COUNT(a.id)>0 AND percentage<50 ORDER BY u.department,u.year,u.class_name,percentage ASC,u.name")
    rows = conn.execute(sql, params).fetchall()
    departments = conn.execute('SELECT name FROM departments ORDER BY name').fetchall()
    classes = conn.execute('SELECT DISTINCT department,class_name,year FROM classes ORDER BY department,year,class_name').fetchall()
    grouped = []
    group_map = {}
    for student in rows:
        key = (student['department'] or 'Unassigned', student['year'] or 0, student['class_name'] or 'Unassigned')
        if key not in group_map:
            group_map[key] = {'department': key[0], 'year': key[1], 'class_name': key[2], 'students': []}
            grouped.append(group_map[key])
        group_map[key]['students'].append(student)
    return render_template('attendance_warnings.html', students=rows, grouped_students=grouped,
        departments=departments, classes=classes, selected_department=dept, selected_class=cls,
        page_scope='admin', page_title='Campus low-attendance list')

@app.route('/staff/attendance-warnings')
@login_required
def staff_attendance_warnings_page():
    """Staff can see warnings only for their own assigned department and class."""
    if g.current_user['role'] != 'staff':
        return redirect(url_for('attendance_warnings_page' if g.current_user['role']=='admin' else 'student_dashboard'))
    conn = db()
    dept = g.current_user['department'] or ''
    cls = g.current_user['class_name'] or 'A'
    rows = conn.execute("""SELECT u.id,u.student_id,u.name,u.email,u.department,u.year,u.class_name,
        COUNT(a.id) total,
        SUM(CASE WHEN a.status IN ('Present','OD') THEN 1 ELSE 0 END) attended,
        CASE WHEN COUNT(a.id)=0 THEN 0 ELSE ROUND(100.0*SUM(CASE WHEN a.status IN ('Present','OD') THEN 1 ELSE 0 END)/COUNT(a.id),1) END percentage
        FROM users u LEFT JOIN attendance a ON a.user_id=u.id
        WHERE u.role='student' AND u.active=1 AND u.department=? AND u.class_name=?
        GROUP BY u.id HAVING COUNT(a.id)>0 AND percentage<50
        ORDER BY percentage ASC,u.name""", (dept, cls)).fetchall()
    return render_template('attendance_warnings.html', students=rows,
        grouped_students=[{'department':dept or 'Unassigned','year':g.current_user['year'] or 0,'class_name':cls,'students':rows}] if rows else [],
        departments=[], classes=[], selected_department=dept, selected_class=cls,
        page_scope='staff', page_title=f'{dept} · Class {cls} low-attendance list')

@app.route('/admin/attendance-warnings/<int:user_id>')
@admin_required
def attendance_warning_detail(user_id):
    return _attendance_warning_detail(user_id, staff_only=False)

@app.route('/staff/attendance-warnings/<int:user_id>')
@login_required
def staff_attendance_warning_detail(user_id):
    if g.current_user['role'] != 'staff':
        return redirect(url_for('attendance_warnings_page' if g.current_user['role']=='admin' else 'student_dashboard'))
    return _attendance_warning_detail(user_id, staff_only=True)

def _attendance_warning_detail(user_id, staff_only=False):
    conn = db()
    student = conn.execute("SELECT id,student_id,name,email,department,year,class_name FROM users WHERE id=? AND role='student' AND active=1", (user_id,)).fetchone()
    if not student or (staff_only and (student['department'] != g.current_user['department'] or student['class_name'] != (g.current_user['class_name'] or 'A'))):
        flash('Student record was not found in your permitted class.', 'error')
        return redirect(url_for('staff_attendance_warnings_page' if staff_only else 'attendance_warnings_page'))
    totals = conn.execute("SELECT COUNT(*) total,SUM(CASE WHEN status IN ('Present','OD') THEN 1 ELSE 0 END) attended FROM attendance WHERE user_id=?", (user_id,)).fetchone()
    history = conn.execute("SELECT a.attendance_date,a.status,a.note,s.code subject_code,s.name subject_name FROM attendance a JOIN subjects s ON s.id=a.subject_id WHERE a.user_id=? ORDER BY a.attendance_date DESC,a.id DESC", (user_id,)).fetchall()
    total = totals['total'] or 0; attended = totals['attended'] or 0
    percentage = round(attended * 100 / total, 1) if total else 0
    return render_template('attendance_warning_detail.html', student=student, history=history, total=total,
        attended=attended, percentage=percentage, staff_scope=staff_only)

@app.route('/staff/attendance/prior-status/<int:user_id>')
@login_required
def staff_prior_attendance_status(user_id):
    if g.current_user['role'] != 'staff': return jsonify({'ok': False}), 403
    dt = request.args.get('date', date.today().isoformat())
    current_subject = request.args.get('subject_id', '').strip()
    try: datetime.strptime(dt, '%Y-%m-%d')
    except ValueError: return jsonify({'ok': False}), 400
    sql = "SELECT s.code,s.name FROM attendance a JOIN subjects s ON s.id=a.subject_id WHERE a.user_id=? AND a.attendance_date=? AND a.status='Present'"
    args = [user_id, dt]
    if current_subject.isdigit(): sql += ' AND a.subject_id<>?'; args.append(int(current_subject))
    row = db().execute(sql + ' ORDER BY a.created_at DESC LIMIT 1', args).fetchone()
    return jsonify({'ok': True, 'was_present': bool(row), 'subject': (row['code'] + ' · ' + row['name']) if row else ''})

@app.route('/admin/staff', methods=['GET','POST'])
@admin_required
def staff_page():
    conn=db()
    if request.method=='POST':
        sid=request.form.get('staff_id','').strip().upper(); name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); dept=request.form.get('department','ECE').strip(); class_name=request.form.get('class_name','A').strip().upper(); password=request.form.get('password','')
        if not sid or not name or not email or len(password)<10:
            flash('Enter staff ID, name, email, and a password of at least 10 characters.','error')
        elif not db().execute('SELECT 1 FROM classes WHERE department=? AND class_name=?',(dept,class_name)).fetchone():
            flash('Create the department/class first in the Classes page.','error')
        else:
            try:
                conn.execute("INSERT INTO users(student_id,name,email,department,class_name,role,password_hash) VALUES(?,?,?,?,?,'staff',?)",(sid,name,email,dept,class_name,generate_password_hash(password)))
                log_action('staff_created',f'{sid} · {name} · {dept}/{class_name}'); conn.commit(); flash(f'Staff account {sid} created.','success')
            except sqlite3.IntegrityError: conn.rollback(); flash('Staff ID or email already exists.','error')
        return redirect(url_for('staff_page'))
    staff=conn.execute("SELECT * FROM users WHERE role='staff' ORDER BY active DESC,name").fetchall()
    classes=conn.execute('SELECT department,class_name,year FROM classes ORDER BY department,year,class_name').fetchall()
    departments=conn.execute('SELECT name FROM departments ORDER BY name').fetchall()
    return render_template('staff.html',staff=staff,classes=classes,departments=departments)

@app.route('/admin/staff/<int:user_id>/toggle', methods=['POST'])
@admin_required
def toggle_staff(user_id):
    u=db().execute("SELECT * FROM users WHERE id=? AND role='staff'",(user_id,)).fetchone()
    if u:
        db().execute('UPDATE users SET active=? WHERE id=?',(0 if u['active'] else 1,user_id)); log_action('staff_status_changed',u['student_id']); db().commit(); flash('Staff access updated.','success')
    return redirect(url_for('staff_page'))

@app.route('/staff')
@login_required
def staff_dashboard():
    if g.current_user['role']=='admin': return redirect(url_for('admin_dashboard'))
    if g.current_user['role']!='staff': return redirect(url_for('student_dashboard'))
    # Staff have no overview/dashboard; their landing page is attendance.
    return redirect(url_for('staff_group_attendance'))

@app.route('/staff/students', methods=['GET','POST'])
@login_required
def staff_students_page():
    # Staff accounts intentionally have only attendance and announcements access.
    if g.current_user['role']!='admin':
        flash('Student management is available only to the dean/admin.','error')
        return redirect(url_for('staff_group_attendance' if g.current_user['role']=='staff' else 'student_dashboard'))
    conn=db(); dept=g.current_user['department']; cls=g.current_user['class_name'] or 'A'
    if request.method=='POST':
        sid=request.form.get('student_id','').strip().upper(); name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); password=request.form.get('password','Student@123')
        if not sid or not name or not email or len(password)<8: flash('Enter all fields and a password of at least 8 characters.','error')
        elif not conn.execute('SELECT 1 FROM classes WHERE department=? AND class_name=?',(dept,cls)).fetchone(): flash('Your assigned class is missing. Ask the dean to configure it.','error')
        else:
            try:
                conn.execute("INSERT INTO users(student_id,name,email,department,year,class_name,role,password_hash) VALUES(?,?,?,?,?,?, 'student',?)",(sid,name,email,dept,g.current_user['year'] or 2,cls,generate_password_hash(password)))
                log_action('student_created_by_staff',f'{sid} · {dept}/{cls}'); conn.commit(); flash(f'Student {sid} added to {dept} / {cls}.','success')
            except sqlite3.IntegrityError: conn.rollback(); flash('Student ID or email already exists.','error')
        return redirect(url_for('staff_students_page'))
    students=conn.execute("SELECT * FROM users WHERE role='student' AND department=? AND class_name=? ORDER BY student_id",(dept,cls)).fetchall()
    return render_template('staff_students.html',students=students,department=dept,class_name=cls)

@app.route('/staff/attendance', methods=['GET','POST'])
@login_required
def staff_group_attendance():
    """Staff can take sequential roll-call for any configured college class."""
    if g.current_user['role']!='staff':
        return redirect(url_for('admin_dashboard' if g.current_user['role']=='admin' else 'student_dashboard'))
    conn=db()
    classes=conn.execute('SELECT department,class_name,year FROM classes ORDER BY department,year,class_name').fetchall()
    departments=conn.execute('SELECT name FROM departments ORDER BY name').fetchall()
    selected_dept=(request.form.get('department') if request.method=='POST' else request.args.get('department')) or g.current_user['department'] or (departments[0]['name'] if departments else '')
    selected_cls=(request.form.get('class_name') if request.method=='POST' else request.args.get('class_name')) or g.current_user['class_name'] or 'A'
    class_exists=conn.execute('SELECT 1 FROM classes WHERE department=? AND class_name=?',(selected_dept,selected_cls)).fetchone()
    if not class_exists:
        selected_dept=g.current_user['department'] or (departments[0]['name'] if departments else '')
        selected_cls=g.current_user['class_name'] or 'A'
    subjects=conn.execute("SELECT * FROM subjects WHERE department=? AND (class_name='*' OR class_name=?) ORDER BY code",(selected_dept,selected_cls)).fetchall()
    all_students=conn.execute("SELECT id,student_id,name FROM users WHERE role='student' AND active=1 AND department=? AND class_name=? ORDER BY student_id COLLATE NOCASE",(selected_dept,selected_cls)).fetchall()
    allowed_statuses={'Present','Absent','OD','Leave'}
    if request.method=='POST':
        try:
            subject_id=int(request.form.get('subject_id',''))
            dt=request.form.get('attendance_date',date.today().isoformat())
            datetime.strptime(dt,'%Y-%m-%d')
            if dt > date.today().isoformat(): raise ValueError('future date')
            if not class_exists: raise ValueError('class')
            subject=conn.execute("SELECT * FROM subjects WHERE id=? AND department=? AND (class_name='*' OR class_name=?)",(subject_id,selected_dept,selected_cls)).fetchone()
            if not subject: raise ValueError('subject')
            roster=all_students
            if not roster: raise ValueError('empty roster')
            counts={status:0 for status in allowed_statuses}
            for r in roster:
                status=request.form.get(f"status_{r['id']}",'')
                if status not in allowed_statuses: raise ValueError('status')
                conn.execute("""INSERT INTO attendance(user_id,subject_id,attendance_date,status,marked_by) VALUES(?,?,?,?,?)
                    ON CONFLICT(user_id,subject_id,attendance_date) DO UPDATE SET status=excluded.status,marked_by=excluded.marked_by""",
                    (r['id'],subject_id,dt,status,g.current_user['id']))
                counts[status]+=1
            log_action('group_attendance_saved',f'{selected_dept}/{selected_cls}, subject={subject_id}, date={dt}, students={len(roster)}, counts={counts}')
            conn.commit()
            flash(f"Saved {len(roster)} records for {selected_dept}/{selected_cls}: {counts['Present']} present, {counts['Absent']} absent, {counts['OD']} OD, {counts['Leave']} leave.",'success')
        except (ValueError,TypeError):
            conn.rollback(); flash('Invalid department, class, subject, date, roll-number range or attendance status. No records were saved.','error')
        return redirect(url_for('staff_group_attendance',department=selected_dept,class_name=selected_cls,subject_id=request.form.get('subject_id',''),attendance_date=request.form.get('attendance_date','')))
    students=all_students
    selected_subject=request.args.get('subject_id','')
    attendance_date=request.args.get('attendance_date',date.today().isoformat())
    return render_template('staff_attendance.html',students=students,all_students=all_students,subjects=subjects,department=selected_dept,class_name=selected_cls,selected_subject=selected_subject,classes=classes,departments=departments,attendance_date=attendance_date)

@app.route('/admin/notices', methods=['GET','POST'])
@app.route('/staff/announcements', methods=['GET','POST'])
@login_required
def notices_page():
    if g.current_user['role'] not in ('admin','staff'):
        return redirect(url_for('student_dashboard'))
    conn=db(); actor=g.current_user; is_admin=actor['role']=='admin'
    if request.method=='POST':
        title=request.form.get('title','').strip(); body=request.form.get('body','').strip()
        target_type=request.form.get('target_type','all')
        target_department=request.form.get('target_department','').strip()
        target_class=request.form.get('target_class','').strip().upper()
        event_start=request.form.get('event_start','').strip(); event_end=request.form.get('event_end','').strip()
        try:
            if not title or not body or target_type not in ('all','department','class'):
                raise ValueError('title/body/audience')
            if target_type in ('department','class') and not target_department: raise ValueError('department')
            if target_type=='class' and not target_class: raise ValueError('class')
            if not event_start or not event_end: raise ValueError('event date/time')
            start_dt=datetime.strptime(event_start,'%Y-%m-%dT%H:%M')
            end_dt=datetime.strptime(event_end,'%Y-%m-%dT%H:%M')
            if end_dt <= start_dt: raise ValueError('event end')
            # Both administrators and staff may publish to all students, one department,
            # or a particular department/class. Validate targeted recipients against the
            # registered campus catalogue so misspelled targets cannot silently send nowhere.
            if target_type in ('department','class'):
                valid_department=conn.execute('SELECT 1 FROM departments WHERE name=?',(target_department,)).fetchone()
                if not valid_department: raise ValueError('unknown department')
            if target_type=='class':
                valid_class=conn.execute('SELECT 1 FROM classes WHERE department=? AND class_name=?',(target_department,target_class)).fetchone()
                if not valid_class: raise ValueError('unknown class')
            conn.execute("INSERT INTO notices(title,body,audience,created_by,target_type,target_department,target_class,event_start,event_end) VALUES(?,?,?,?,?,?,?,?,?)",(title,body,'students',actor['id'],target_type,target_department if target_type in ('department','class') else '',target_class if target_type=='class' else '',event_start,event_end))
            log_action('event_announcement_created',f'{title} · {target_type} · {event_start}–{event_end}')
            conn.commit()
            flash('Event announcement published in AttendancePro. Email delivery is disabled.', 'success')
        except (ValueError,TypeError):
            conn.rollback(); flash('Enter a title, message, valid audience, event start and end. End must be after start; staff may only target their own department/class.','error')
        return redirect(url_for('notices_page'))
    if is_admin:
        notices=conn.execute('SELECT n.*,u.name author FROM notices n LEFT JOIN users u ON u.id=n.created_by ORDER BY n.event_start DESC,n.id DESC').fetchall()
        departments=conn.execute('SELECT name FROM departments ORDER BY name').fetchall()
        classes=conn.execute('SELECT department,class_name,year FROM classes ORDER BY department,year,class_name').fetchall()
    else:
        notices=conn.execute("SELECT n.*,u.name author FROM notices n LEFT JOIN users u ON u.id=n.created_by WHERE n.target_type='all' OR (n.target_department=? AND n.target_type='department') OR (n.target_department=? AND n.target_class=? AND n.target_type='class') ORDER BY n.event_start DESC,n.id DESC",(actor['department'],actor['department'],actor['class_name'] or 'A')).fetchall()
        # Staff have the same announcement audience controls as administrators.
        departments=conn.execute('SELECT name FROM departments ORDER BY name').fetchall()
        classes=conn.execute('SELECT department,class_name,year FROM classes ORDER BY department,year,class_name').fetchall()
    return render_template('notices.html',notices=notices,departments=departments,classes=classes,is_admin=is_admin)

@app.route('/admin/weekly-reports')
@admin_required
def weekly_reports_page():
    os.makedirs(REPORT_DIR, exist_ok=True)
    files = sorted([name for name in os.listdir(REPORT_DIR) if name.endswith('.csv') and name.startswith('attendance-week-')], reverse=True)
    return render_template('weekly_reports.html', report_files=files)

@app.route('/admin/weekly-reports/download/<path:filename>')
@admin_required
def download_weekly_report(filename):
    from flask import send_from_directory, abort
    if not filename.startswith('attendance-week-') or not filename.endswith('.csv') or '/' in filename or '\\' in filename:
        abort(404)
    full_path = os.path.join(REPORT_DIR, filename)
    if not os.path.isfile(full_path): abort(404)
    return send_from_directory(REPORT_DIR, filename, as_attachment=True, mimetype='text/csv')

@app.route('/admin/weekly-reports/generate', methods=['POST'])
@admin_required
def generate_weekly_report_now():
    end_day = date.today()
    start_day = end_day - timedelta(days=6)
    path = _write_weekly_report(start_day, end_day)
    flash('Weekly attendance report generated for the last 7 days.', 'success')
    return redirect(url_for('download_weekly_report', filename=os.path.basename(path)))

@app.route('/admin/reports.csv')
@admin_required
def export_csv():
    rows=db().execute('''SELECT u.student_id,u.name,u.department,s.code,s.name,a.attendance_date,a.status,a.note FROM attendance a JOIN users u ON u.id=a.user_id JOIN subjects s ON s.id=a.subject_id ORDER BY a.attendance_date DESC''').fetchall()
    output=io.StringIO(); writer=csv.writer(output); writer.writerow(['Student ID','Student Name','Department','Subject Code','Subject','Date','Status','Note'])
    for r in rows: writer.writerow(list(r))
    response=make_response('\ufeff'+output.getvalue()); response.headers['Content-Type']='text/csv; charset=utf-8'; response.headers['Content-Disposition']='attachment; filename=attendancepro-report.csv'; return response

@app.route('/admin/audit')
@admin_required
def audit_page():
    logs=db().execute('SELECT l.*,u.name actor FROM audit_logs l LEFT JOIN users u ON u.id=l.actor_id ORDER BY l.id DESC LIMIT 200').fetchall()
    return render_template('audit.html',logs=logs)

@app.route('/forgot-password', methods=['GET','POST'])
def forgot_password():
    """Create an admin-reviewed reset request; never reset using ID/email alone."""
    if request.method=='POST':
        identifier=request.form.get('student_id','').strip()
        email=request.form.get('email','').strip().lower()
        user=db().execute("SELECT id FROM users WHERE role='student' AND active=1 AND student_id=? COLLATE NOCASE AND email=? COLLATE NOCASE",(identifier,email)).fetchone()
        if user:
            pending=db().execute("SELECT id FROM password_reset_requests WHERE user_id=? AND status='pending'",(user['id'],)).fetchone()
            if not pending:
                db().execute("INSERT INTO password_reset_requests(user_id) VALUES(?)",(user['id'],))
                log_action('password_reset_requested',f'Student ID {identifier}')
                db().commit()
        flash('If the student ID and registered email match an active account, a reset request has been sent to the administrator for review.','success')
        return redirect(url_for('forgot_password'))
    return render_template('forgot_password.html')

@app.route('/admin/password-resets', methods=['GET','POST'])
@admin_required
def password_resets_page():
    conn=db()
    if request.method=='POST':
        request_id=request.form.get('request_id','')
        req=conn.execute("SELECT r.*,u.student_id,u.name FROM password_reset_requests r JOIN users u ON u.id=r.user_id WHERE r.id=? AND r.status='pending' AND u.role='student'",(request_id,)).fetchone()
        if not req:
            flash('That reset request is no longer pending.','error')
        else:
            temporary=secrets.token_urlsafe(12)
            conn.execute('UPDATE users SET password_hash=?,must_change_password=1 WHERE id=?',(generate_password_hash(temporary),req['user_id']))
            conn.execute("UPDATE password_reset_requests SET status='approved',handled_at=CURRENT_TIMESTAMP,handled_by=? WHERE id=?",(g.current_user['id'],req['id']))
            log_action('password_reset_issued',f"Temporary password issued for {req['student_id']}")
            conn.commit()
            flash(f"Temporary password for {req['student_id']} (show this to the student through a verified private channel): {temporary}",'success')
        return redirect(url_for('password_resets_page'))
    requests=conn.execute("SELECT r.*,u.student_id,u.name,u.email,u.department,u.class_name FROM password_reset_requests r JOIN users u ON u.id=r.user_id WHERE r.status='pending' ORDER BY r.requested_at",).fetchall()
    return render_template('password_resets.html',requests=requests)

@app.route('/student/force-password-change', methods=['GET','POST'])
@login_required
def force_password_change():
    user=g.current_user
    if user['role']!='student': return redirect(url_for('index'))
    if request.method=='POST':
        new=request.form.get('new_password',''); confirm=request.form.get('confirm_password','')
        if len(new)<12 or not any(c.islower() for c in new) or not any(c.isupper() for c in new) or not any(c.isdigit() for c in new) or not any(not c.isalnum() for c in new):
            flash('Use at least 12 characters with uppercase, lowercase, number and symbol.','error')
        elif new!=confirm:
            flash('Passwords do not match.','error')
        elif check_password_hash(user['password_hash'],new):
            flash('Choose a password different from the temporary password.','error')
        else:
            db().execute('UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?',(generate_password_hash(new),user['id']))
            log_action('password_changed_after_reset','Student changed temporary password')
            db().commit(); flash('Password changed. You are now signed in securely.','success')
            return redirect(url_for('student_dashboard'))
    return render_template('force_password_change.html')

@app.route('/student')
@login_required
def student_dashboard():
    if g.current_user['role']=='admin': return redirect(url_for('admin_dashboard'))
    if g.current_user['role']=='staff': return redirect(url_for('staff_dashboard'))
    conn=db(); user_id=g.current_user['id']
    totals=conn.execute("SELECT COUNT(*) total,SUM(CASE WHEN status IN ('Present','OD') THEN 1 ELSE 0 END) attended FROM attendance WHERE user_id=?",(user_id,)).fetchone()
    total=totals['total'] or 0; attended=totals['attended'] or 0; percentage=round(attended*100/total) if total else 0
    subjects=conn.execute("SELECT s.id,s.code,s.name,s.faculty,COUNT(a.id) total,SUM(CASE WHEN a.status IN ('Present','OD') THEN 1 ELSE 0 END) attended FROM subjects s LEFT JOIN attendance a ON a.subject_id=s.id AND a.user_id=? WHERE s.department=? AND (s.class_name='*' OR s.class_name=?) GROUP BY s.id ORDER BY s.code",(user_id,g.current_user['department'],g.current_user['class_name'] or 'A')).fetchall()
    history=conn.execute('''SELECT a.*,s.code subject_code,s.name subject_name FROM attendance a JOIN subjects s ON s.id=a.subject_id WHERE a.user_id=? ORDER BY attendance_date DESC,a.id DESC LIMIT 12''',(user_id,)).fetchall()
    notices=conn.execute("SELECT * FROM notices WHERE audience IN ('all','students') AND (event_end IS NULL OR event_end > ?) AND (target_type='all' OR (target_type='department' AND target_department=?) OR (target_type='class' AND target_department=? AND target_class=?)) ORDER BY event_start ASC,id DESC LIMIT 8",(datetime.now().strftime('%Y-%m-%dT%H:%M'),g.current_user['department'],g.current_user['department'],g.current_user['class_name'] or 'A')).fetchall()
    return render_template('student.html',total=total,attended=attended,percentage=percentage,subjects=subjects,history=history,notices=notices)

@app.route('/student/profile', methods=['GET','POST'])
@login_required
def profile_page():
    if g.current_user['role']=='admin': return redirect(url_for('admin_dashboard'))
    if g.current_user['role']=='staff':
        flash('Staff access is limited to Attendance and Announcements.','error')
        return redirect(url_for('staff_group_attendance'))
    if request.method=='POST':
        old=request.form.get('current_password',''); new=request.form.get('new_password','')
        if not check_password_hash(g.current_user['password_hash'],old): flash('Current password is incorrect.','error')
        elif len(new)<12 or not any(c.islower() for c in new) or not any(c.isupper() for c in new) or not any(c.isdigit() for c in new) or not any(not c.isalnum() for c in new): flash('Use at least 12 characters with uppercase, lowercase, number and symbol.','error')
        else:
            db().execute('UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?',(generate_password_hash(new),g.current_user['id'])); log_action('password_changed','Student changed password'); db().commit(); flash('Password changed successfully.','success')
        return redirect(url_for('profile_page'))
    return render_template('profile.html')

@app.errorhandler(404)
def not_found(_): return render_template('error.html',code=404,message='This page could not be found.'),404

if __name__ == '__main__':
    init_db()
    app.run(host='127.0.0.1', port=int(os.getenv('PORT','5000')), debug=os.getenv('FLASK_DEBUG','0')=='1')
