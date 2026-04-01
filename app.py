from flask import Flask, render_template, request, jsonify, redirect, url_for
from datetime import datetime, timedelta
import gspread
import json
import os
import time

app = Flask(__name__)
app.config['PROPAGATE_EXCEPTIONS'] = True

SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '1c8vUU9XshrPFD6OzrXyoEmOKIl4mS-jow3P_p0w-p1Q')
WEEKDAY_NAMES = ['월', '화', '수', '목', '금', '토', '일']

# Google Sheets 인증
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), 'service-account.json')
SERVICE_ACCOUNT_INFO = os.environ.get('GOOGLE_CREDENTIALS', '')

# 캐시
_gc_cache = None
_sh_cache = None
_data_cache = {'schedules': None, 'bookings': None, 'time': 0}
CACHE_TTL = 10  # 10초 캐시


def get_gc():
    global _gc_cache
    if _gc_cache is None:
        if SERVICE_ACCOUNT_INFO:
            import base64
            try:
                decoded = base64.b64decode(SERVICE_ACCOUNT_INFO).decode('utf-8')
                info = json.loads(decoded)
            except Exception:
                info = json.loads(SERVICE_ACCOUNT_INFO)
            _gc_cache = gspread.service_account_from_dict(info)
        else:
            _gc_cache = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    return _gc_cache


def get_sheet():
    global _sh_cache
    if _sh_cache is None:
        gc = get_gc()
        _sh_cache = gc.open_by_key(SPREADSHEET_ID)
    return _sh_cache


def get_schedules_ws():
    return get_sheet().worksheet('schedules')


def get_bookings_ws():
    return get_sheet().worksheet('bookings')


def invalidate_cache():
    _data_cache['time'] = 0


def get_all_schedules():
    now = time.time()
    if _data_cache['schedules'] is not None and (now - _data_cache['time']) < CACHE_TTL:
        return _data_cache['schedules']
    ws = get_schedules_ws()
    records = ws.get_all_records()
    _data_cache['schedules'] = records
    _data_cache['time'] = now
    return records


def get_all_bookings():
    now = time.time()
    if _data_cache['bookings'] is not None and (now - _data_cache['time']) < CACHE_TTL:
        return _data_cache['bookings']
    ws = get_bookings_ws()
    records = ws.get_all_records()
    _data_cache['bookings'] = records
    _data_cache['time'] = now
    return records


def get_all_data():
    """스케줄과 예약을 한번에 가져오기 (API 호출 최소화)"""
    now = time.time()
    if (_data_cache['schedules'] is not None and
        _data_cache['bookings'] is not None and
        (now - _data_cache['time']) < CACHE_TTL):
        return _data_cache['schedules'], _data_cache['bookings']

    sh = get_sheet()
    s_ws = sh.worksheet('schedules')
    b_ws = sh.worksheet('bookings')
    schedules = s_ws.get_all_records()
    bookings = b_ws.get_all_records()
    _data_cache['schedules'] = schedules
    _data_cache['bookings'] = bookings
    _data_cache['time'] = now
    return schedules, bookings


def next_id(ws):
    values = ws.col_values(1)  # id 컬럼
    if len(values) <= 1:
        return 1
    ids = [int(v) for v in values[1:] if v]
    return max(ids) + 1 if ids else 1


# ── 수강생 페이지 ──

@app.route('/')
def index():
    schedules, bookings = get_all_data()
    today = datetime.now().strftime('%Y-%m-%d')

    booking_counts = {}
    for b in bookings:
        sid = str(b['schedule_id'])
        booking_counts[sid] = booking_counts.get(sid, 0) + 1

    schedule_data = {}
    for s in schedules:
        if str(s.get('closed', 0)) == '1' or s['closed'] == 1:
            continue
        if s['date'] < today:
            continue
        booked = booking_counts.get(str(s['id']), 0)
        max_slots = int(s['max_slots'])
        if booked >= max_slots:
            continue

        d = s['date']
        if d not in schedule_data:
            schedule_data[d] = []
        schedule_data[d].append({
            'id': s['id'],
            'time': s['time'],
            'remaining': max_slots - booked
        })

    # 날짜순 정렬
    schedule_data = dict(sorted(schedule_data.items()))

    return render_template('index.html',
                           schedule_json=json.dumps(schedule_data, ensure_ascii=False),
                           has_dates=len(schedule_data) > 0)


@app.route('/book', methods=['POST'])
def book():
    schedule_id = request.form['schedule_id']
    name = request.form['name']
    phone = request.form['phone']

    schedules = get_all_schedules()
    schedule = None
    for s in schedules:
        if str(s['id']) == str(schedule_id) and str(s.get('closed', 0)) != '1' and s.get('closed', 0) != 1:
            schedule = s
            break

    if not schedule:
        return '잘못된 요청입니다.', 400

    bookings = get_all_bookings()
    booked = sum(1 for b in bookings if str(b['schedule_id']) == str(schedule_id))
    if booked >= int(schedule['max_slots']):
        return '해당 시간은 이미 마감되었습니다.', 400

    ws = get_bookings_ws()
    new_id = next_id(ws)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    ws.append_row([new_id, int(schedule_id), name, phone, schedule['date'], now])
    invalidate_cache()

    return render_template('confirm.html',
                           date=schedule['date'],
                           time=schedule['time'],
                           name=name)


# ── 관리자 페이지 ──

@app.route('/admin')
def admin():
    schedules, bookings = get_all_data()

    booking_counts = {}
    for b in bookings:
        sid = str(b['schedule_id'])
        booking_counts[sid] = booking_counts.get(sid, 0) + 1

    schedule_list = []
    for s in schedules:
        dt = datetime.strptime(s['date'], '%Y-%m-%d')
        schedule_list.append({
            'id': s['id'],
            'date': s['date'],
            'weekday': WEEKDAY_NAMES[dt.weekday()],
            'time': s['time'],
            'max_slots': int(s['max_slots']),
            'booked': booking_counts.get(str(s['id']), 0),
            'closed': int(s.get('closed', 0)),
        })

    # 캘린더용 JSON 데이터
    cal_data = {}
    for s in schedule_list:
        d = s['date']
        if d not in cal_data:
            cal_data[d] = {'schedules': [], 'bookings': []}
        cal_data[d]['schedules'].append(s)

    booking_list = []
    for b in bookings:
        bd = b.get('date', '')
        booking_list.append(dict(b))
        if bd and bd in cal_data:
            cal_data[bd]['bookings'].append(dict(b))

    return render_template('admin.html',
                           schedules=schedule_list,
                           bookings=booking_list,
                           cal_json=json.dumps(cal_data, ensure_ascii=False, default=str))


@app.route('/admin/bulk-add', methods=['POST'])
def bulk_add():
    start = request.form['start_date']
    end = request.form['end_date']
    time_val = request.form['time']
    max_slots = int(request.form.get('max_slots', 1))
    day_type = request.form.get('day_type', 'weekday')

    start_dt = datetime.strptime(start, '%Y-%m-%d')
    end_dt = datetime.strptime(end, '%Y-%m-%d')

    ws = get_schedules_ws()
    existing = get_all_schedules()
    existing_set = {(s['date'], s['time']) for s in existing}
    new_id = next_id(ws)

    rows_to_add = []
    current = start_dt
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    while current <= end_dt:
        wd = current.weekday()
        add = False
        if day_type == 'weekday' and wd < 5:
            add = True
        elif day_type == 'weekend' and wd >= 5:
            add = True
        elif day_type == 'all':
            add = True

        if add:
            date_str = current.strftime('%Y-%m-%d')
            if (date_str, time_val) not in existing_set:
                rows_to_add.append([new_id, date_str, time_val, max_slots, 0, now])
                new_id += 1
        current += timedelta(days=1)

    if rows_to_add:
        ws.append_rows(rows_to_add)
    invalidate_cache()

    return redirect(url_for('admin'))


@app.route('/admin/toggle-close/<int:sid>', methods=['POST'])
def toggle_close(sid):
    ws = get_schedules_ws()
    records = ws.get_all_records()
    for i, s in enumerate(records):
        if int(s['id']) == sid:
            row_num = i + 2  # 헤더 제외
            current = int(s.get('closed', 0))
            ws.update_cell(row_num, 5, 0 if current else 1)  # E열 = closed
            break
    invalidate_cache()
    return redirect(url_for('admin'))


@app.route('/admin/delete-schedule/<int:sid>', methods=['POST'])
def delete_schedule(sid):
    # 관련 예약 삭제
    bws = get_bookings_ws()
    bookings = bws.get_all_records()
    rows_to_delete = []
    for i, b in enumerate(bookings):
        if int(b['schedule_id']) == sid:
            rows_to_delete.append(i + 2)
    for row in sorted(rows_to_delete, reverse=True):
        bws.delete_rows(row)

    # 스케줄 삭제
    ws = get_schedules_ws()
    records = ws.get_all_records()
    for i, s in enumerate(records):
        if int(s['id']) == sid:
            ws.delete_rows(i + 2)
            break
    invalidate_cache()
    return redirect(url_for('admin'))


@app.route('/admin/delete-booking/<int:bid>', methods=['POST'])
def delete_booking(bid):
    ws = get_bookings_ws()
    records = ws.get_all_records()
    for i, b in enumerate(records):
        if int(b['id']) == bid:
            ws.delete_rows(i + 2)
            break
    invalidate_cache()
    return redirect(url_for('admin'))


@app.route('/health')
def health():
    try:
        has_creds = bool(os.environ.get('GOOGLE_CREDENTIALS', ''))
        has_sheet = bool(os.environ.get('SPREADSHEET_ID', ''))
        gc = get_gc()
        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = sh.worksheet('schedules')
        count = len(ws.get_all_records())
        return jsonify({'status': 'ok', 'has_creds': has_creds, 'has_sheet': has_sheet, 'schedules': count})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e), 'has_creds': bool(os.environ.get('GOOGLE_CREDENTIALS', '')), 'has_sheet': bool(os.environ.get('SPREADSHEET_ID', ''))})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
