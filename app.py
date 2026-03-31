from flask import Flask, render_template, request, jsonify, redirect, url_for
from datetime import datetime, timedelta
import sqlite3
import os

app = Flask(__name__)
DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'schedule.db'))

WEEKDAY_NAMES = ['월', '화', '수', '목', '금', '토', '일']


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            max_slots INTEGER NOT NULL DEFAULT 1,
            closed INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (schedule_id) REFERENCES schedules(id)
        );
    ''')
    # closed 컬럼이 없으면 추가 (기존 DB 호환)
    try:
        conn.execute('SELECT closed FROM schedules LIMIT 1')
    except sqlite3.OperationalError:
        conn.execute('ALTER TABLE schedules ADD COLUMN closed INTEGER NOT NULL DEFAULT 0')
        conn.commit()
    conn.close()


# ── 수강생 페이지 ──

@app.route('/')
def index():
    conn = get_db()
    rows = conn.execute('''
        SELECT s.id, s.date, s.time, s.max_slots,
               COUNT(b.id) as booked
        FROM schedules s
        LEFT JOIN bookings b ON b.schedule_id = s.id
        WHERE s.closed = 0 AND s.date >= date('now')
        GROUP BY s.id
        HAVING booked < s.max_slots
        ORDER BY s.date, s.time
    ''').fetchall()
    conn.close()

    dates = {}
    for r in rows:
        d = r['date']
        dt = datetime.strptime(d, '%Y-%m-%d')
        label = f"{d} ({WEEKDAY_NAMES[dt.weekday()]})"
        if label not in dates:
            dates[label] = []
        dates[label].append({'id': r['id'], 'time': r['time'],
                             'remaining': r['max_slots'] - r['booked']})
    return render_template('index.html', dates=dates)


@app.route('/book', methods=['POST'])
def book():
    schedule_id = request.form['schedule_id']
    name = request.form['name']
    phone = request.form['phone']

    conn = get_db()
    schedule = conn.execute('SELECT * FROM schedules WHERE id = ? AND closed = 0', (schedule_id,)).fetchone()
    if not schedule:
        conn.close()
        return '잘못된 요청입니다.', 400

    booked = conn.execute('SELECT COUNT(*) as cnt FROM bookings WHERE schedule_id = ?',
                          (schedule_id,)).fetchone()['cnt']
    if booked >= schedule['max_slots']:
        conn.close()
        return '해당 시간은 이미 마감되었습니다.', 400

    conn.execute('INSERT INTO bookings (schedule_id, name, phone) VALUES (?, ?, ?)',
                 (schedule_id, name, phone))
    conn.commit()
    conn.close()

    return render_template('confirm.html',
                           date=schedule['date'],
                           time=schedule['time'],
                           name=name)


# ── 관리자 페이지 ──

@app.route('/admin')
def admin():
    conn = get_db()
    schedules = conn.execute('''
        SELECT s.id, s.date, s.time, s.max_slots, s.closed,
               COUNT(b.id) as booked
        FROM schedules s
        LEFT JOIN bookings b ON b.schedule_id = s.id
        GROUP BY s.id
        ORDER BY s.date, s.time
    ''').fetchall()

    bookings = conn.execute('''
        SELECT b.id, b.name, b.phone, b.created_at,
               s.date, s.time
        FROM bookings b
        JOIN schedules s ON s.id = b.schedule_id
        ORDER BY s.date DESC, s.time, b.created_at
    ''').fetchall()
    conn.close()

    schedule_list = []
    for s in schedules:
        dt = datetime.strptime(s['date'], '%Y-%m-%d')
        schedule_list.append({
            'id': s['id'],
            'date': s['date'],
            'weekday': WEEKDAY_NAMES[dt.weekday()],
            'time': s['time'],
            'max_slots': s['max_slots'],
            'booked': s['booked'],
            'closed': s['closed'],
        })

    return render_template('admin.html', schedules=schedule_list, bookings=bookings)


@app.route('/admin/bulk-add', methods=['POST'])
def bulk_add():
    """기간 + 요일 + 시간으로 일괄 일정 생성"""
    start = request.form['start_date']
    end = request.form['end_date']
    time_val = request.form['time']
    max_slots = int(request.form.get('max_slots', 1))
    day_type = request.form.get('day_type', 'weekday')  # weekday, weekend, all

    start_dt = datetime.strptime(start, '%Y-%m-%d')
    end_dt = datetime.strptime(end, '%Y-%m-%d')

    conn = get_db()
    current = start_dt
    count = 0
    while current <= end_dt:
        wd = current.weekday()  # 0=월 ~ 6=일
        add = False
        if day_type == 'weekday' and wd < 5:
            add = True
        elif day_type == 'weekend' and wd >= 5:
            add = True
        elif day_type == 'all':
            add = True

        if add:
            date_str = current.strftime('%Y-%m-%d')
            existing = conn.execute(
                'SELECT id FROM schedules WHERE date = ? AND time = ?',
                (date_str, time_val)
            ).fetchone()
            if not existing:
                conn.execute(
                    'INSERT INTO schedules (date, time, max_slots) VALUES (?, ?, ?)',
                    (date_str, time_val, max_slots)
                )
                count += 1
        current += timedelta(days=1)

    conn.commit()
    conn.close()
    return redirect(url_for('admin'))


@app.route('/admin/toggle-close/<int:sid>', methods=['POST'])
def toggle_close(sid):
    """마감/오픈 토글"""
    conn = get_db()
    schedule = conn.execute('SELECT closed FROM schedules WHERE id = ?', (sid,)).fetchone()
    if schedule:
        new_val = 0 if schedule['closed'] else 1
        conn.execute('UPDATE schedules SET closed = ? WHERE id = ?', (new_val, sid))
        conn.commit()
    conn.close()
    return redirect(url_for('admin'))


@app.route('/admin/delete-schedule/<int:sid>', methods=['POST'])
def delete_schedule(sid):
    conn = get_db()
    conn.execute('DELETE FROM bookings WHERE schedule_id = ?', (sid,))
    conn.execute('DELETE FROM schedules WHERE id = ?', (sid,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin'))


@app.route('/admin/delete-booking/<int:bid>', methods=['POST'])
def delete_booking(bid):
    conn = get_db()
    conn.execute('DELETE FROM bookings WHERE id = ?', (bid,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin'))


init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
