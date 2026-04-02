from flask import Flask, render_template, request, jsonify, redirect, url_for
from datetime import datetime, timedelta
import gspread
import json
import os
import time
import threading

app = Flask(__name__)
app.config['PROPAGATE_EXCEPTIONS'] = True

SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '1c8vUU9XshrPFD6OzrXyoEmOKIl4mS-jow3P_p0w-p1Q')
WEEKDAY_NAMES = ['월', '화', '수', '목', '금', '토', '일']

SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), 'service-account.json')
SERVICE_ACCOUNT_INFO = os.environ.get('GOOGLE_CREDENTIALS', '')

# ── 캐시 ──
_gc_cache = None
_sh_cache = None
_ws_cache = {}
_data_cache = {'schedules': None, 'bookings': None, 'time': 0}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # 5분


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
        _sh_cache = get_gc().open_by_key(SPREADSHEET_ID)
    return _sh_cache


def get_schedules_ws():
    if 'schedules' not in _ws_cache:
        _ws_cache['schedules'] = get_sheet().worksheet('schedules')
    return _ws_cache['schedules']


def get_bookings_ws():
    if 'bookings' not in _ws_cache:
        _ws_cache['bookings'] = get_sheet().worksheet('bookings')
    return _ws_cache['bookings']


# ── 유틸 ──

def is_closed(val):
    """closed 값을 안전하게 판별 (int, str, bool, 빈값 모두 처리)"""
    if val is None or val == '':
        return False
    return str(val).strip() in ('1', 'TRUE', 'true', 'True')


def safe_int(val, default=0):
    """안전한 int 변환"""
    if val is None or val == '':
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def next_id_from_cache(records):
    if not records:
        return 1
    ids = [safe_int(r.get('id')) for r in records]
    ids = [i for i in ids if i > 0]
    return max(ids) + 1 if ids else 1


# ── 데이터 로드/캐시 ──

def _fetch_and_cache():
    """Google Sheets에서 데이터를 동기적으로 가져와 캐시 저장"""
    s_ws = get_schedules_ws()
    b_ws = get_bookings_ws()
    schedules = s_ws.get_all_records()
    bookings = b_ws.get_all_records()
    with _cache_lock:
        _data_cache['schedules'] = schedules
        _data_cache['bookings'] = bookings
        _data_cache['time'] = time.time()


def refresh_cache_sync():
    """쓰기 후 즉시 동기 갱신 (다음 페이지 로드가 최신 데이터 사용)"""
    _data_cache['time'] = 0
    _fetch_and_cache()


def refresh_cache_bg():
    """백그라운드 갱신 (읽기 전용 접근 시)"""
    threading.Thread(target=_fetch_and_cache, daemon=True).start()


def get_all_data():
    with _cache_lock:
        if (_data_cache['schedules'] is not None and
            _data_cache['bookings'] is not None and
            (time.time() - _data_cache['time']) < CACHE_TTL):
            return _data_cache['schedules'], _data_cache['bookings']
    _fetch_and_cache()
    return _data_cache['schedules'], _data_cache['bookings']


def get_all_schedules():
    data = get_all_data()
    return data[0]


def get_all_bookings():
    data = get_all_data()
    return data[1]


# 서버 시작 시 캐시 워밍업
try:
    _fetch_and_cache()
except Exception:
    pass


# ── 수강생 페이지 ──

@app.route('/')
def index():
    schedules, bookings = get_all_data()
    today = datetime.now().strftime('%Y-%m-%d')

    booking_counts = {}
    for b in bookings:
        sid = str(b.get('schedule_id', ''))
        if sid:
            booking_counts[sid] = booking_counts.get(sid, 0) + 1

    schedule_data = {}
    for s in schedules:
        if is_closed(s.get('closed')):
            continue
        date_str = str(s.get('date', ''))
        if not date_str or date_str < today:
            continue
        booked = booking_counts.get(str(s['id']), 0)
        max_slots = safe_int(s.get('max_slots'), 1)
        if booked >= max_slots:
            continue

        if date_str not in schedule_data:
            schedule_data[date_str] = []
        schedule_data[date_str].append({
            'id': s['id'],
            'time': s.get('time', ''),
            'remaining': max_slots - booked
        })

    schedule_data = dict(sorted(schedule_data.items()))

    return render_template('index.html',
                           schedule_json=json.dumps(schedule_data, ensure_ascii=False),
                           has_dates=len(schedule_data) > 0)


@app.route('/book', methods=['POST'])
def book():
    schedule_id = request.form['schedule_id']
    name = request.form['name']
    phone = request.form['phone']
    notion_url = request.form.get('notion_url', '').strip()

    if not notion_url:
        return '노션 링크를 입력해주세요.', 400

    schedules = get_all_schedules()
    schedule = None
    for s in schedules:
        if str(s['id']) == str(schedule_id) and not is_closed(s.get('closed')):
            schedule = s
            break

    if not schedule:
        return '잘못된 요청입니다.', 400

    bookings = get_all_bookings()
    booked = sum(1 for b in bookings if str(b.get('schedule_id', '')) == str(schedule_id))
    if booked >= safe_int(schedule.get('max_slots'), 1):
        return '해당 시간은 이미 마감되었습니다.', 400

    ws = get_bookings_ws()
    new_id = next_id_from_cache(bookings)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    ws.append_row([new_id, int(schedule_id), name, phone, schedule['date'], now, schedule.get('time', ''), notion_url])
    refresh_cache_sync()

    return render_template('confirm.html',
                           date=schedule['date'],
                           time=schedule.get('time', ''),
                           name=name)


# ── 관리자 페이지 ──

@app.route('/admin')
def admin():
    schedules, bookings = get_all_data()

    booking_counts = {}
    for b in bookings:
        sid = str(b.get('schedule_id', ''))
        if sid:
            booking_counts[sid] = booking_counts.get(sid, 0) + 1

    schedule_list = []
    for s in schedules:
        date_str = str(s.get('date', ''))
        if not date_str:
            continue
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            continue
        schedule_list.append({
            'id': s['id'],
            'date': date_str,
            'weekday': WEEKDAY_NAMES[dt.weekday()],
            'time': s.get('time', ''),
            'max_slots': safe_int(s.get('max_slots'), 1),
            'booked': booking_counts.get(str(s['id']), 0),
            'closed': 1 if is_closed(s.get('closed')) else 0,
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
        bd = str(b.get('date', ''))
        booking_list.append(dict(b))
        if bd:
            if bd not in cal_data:
                cal_data[bd] = {'schedules': [], 'bookings': []}
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
    existing_set = {(str(s.get('date', '')), str(s.get('time', ''))) for s in existing}
    new_id = next_id_from_cache(existing)

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
    refresh_cache_sync()

    return redirect(url_for('admin'))


@app.route('/admin/toggle-close/<int:sid>', methods=['POST'])
def toggle_close(sid):
    ws = get_schedules_ws()
    records = get_all_schedules()
    for i, s in enumerate(records):
        if safe_int(s.get('id')) == sid:
            row_num = i + 2
            currently_closed = is_closed(s.get('closed'))
            ws.update_cell(row_num, 5, 0 if currently_closed else 1)
            break
    refresh_cache_sync()
    return redirect(url_for('admin'))


@app.route('/admin/delete-schedule/<int:sid>', methods=['POST'])
def delete_schedule(sid):
    ws = get_schedules_ws()
    records = get_all_schedules()
    for i, s in enumerate(records):
        if safe_int(s.get('id')) == sid:
            ws.delete_rows(i + 2)
            break
    refresh_cache_sync()
    return redirect(url_for('admin'))


@app.route('/admin/bulk-delete', methods=['POST'])
def bulk_delete():
    start = request.form['start_date']
    end = request.form['end_date']

    ws = get_schedules_ws()
    records = get_all_schedules()
    rows_to_delete = []
    for i, s in enumerate(records):
        d = str(s.get('date', ''))
        if d and start <= d <= end:
            rows_to_delete.append(i + 2)

    for row in sorted(rows_to_delete, reverse=True):
        ws.delete_rows(row)

    refresh_cache_sync()
    return redirect(url_for('admin'))


@app.route('/admin/delete-booking/<int:bid>', methods=['POST'])
def delete_booking(bid):
    ws = get_bookings_ws()
    records = get_all_bookings()
    for i, b in enumerate(records):
        if safe_int(b.get('id')) == bid:
            ws.delete_rows(i + 2)
            break
    refresh_cache_sync()
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
        return jsonify({'status': 'error', 'error': str(e),
                        'has_creds': bool(os.environ.get('GOOGLE_CREDENTIALS', '')),
                        'has_sheet': bool(os.environ.get('SPREADSHEET_ID', ''))})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
