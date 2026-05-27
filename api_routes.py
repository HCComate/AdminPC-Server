import json
from flask import Blueprint, request, jsonify
from config import device_status, locked_devices, escalation_sessions
from database import get_db
from auth import require_auth, require_permission
from error_loader import load_error_codes

api = Blueprint('api', __name__)

# 서버 시작 시 에러 코드 CSV 로드
ERROR_CODE_MASTER = load_error_codes()
print(f"📋 에러 코드 마스터 로드 완료: {len(ERROR_CODE_MASTER)}개")


# 📌 API 5. 에러 코드 조회
# GET /api/error-codes              → 전체 목록
# GET /api/error-codes?code=HM-PO-01 → 특정 코드 조회
# GET /api/error-codes?severity=Critical → 심각도별 필터
@api.route('/api/error-codes', methods=['GET'])
@require_auth
def get_error_codes():
    code = request.args.get('code')
    severity = request.args.get('severity')

    if code:
        # 특정 에러 코드 조회
        error = ERROR_CODE_MASTER.get(code)
        if not error:
            return jsonify({"error": f"에러 코드 '{code}'를 찾을 수 없습니다."}), 404
        return jsonify(error)

    result = ERROR_CODE_MASTER

    if severity:
        result = {
            k: v for k, v in result.items()
            if v["severity"] == severity.upper()
        }

    return jsonify(list(result.values()))


# 📌 API 1. 장비 목록 조회
# GET /api/devices
# 권한: dashboard_view (OPERATOR, TECHNICIAN, MASTER)
@api.route('/api/devices', methods=['GET'])
@require_auth
def get_devices():
    devices = []
    for did, info in device_status.items():
        status = info["status"]
        if did in locked_devices:
            status = "LOCKED"
        devices.append({"device_id": did, "status": status})
    return jsonify(devices)


# 📌 API 2. 검사 이력 조회
# GET /api/logs?device_id=RASP_PI_01&limit=100
# 권한: inspection_result (OPERATOR, TECHNICIAN, MASTER)
@api.route('/api/logs', methods=['GET'])
@require_permission('inspection_result')
def get_logs():
    device_id = request.args.get('device_id')
    limit = request.args.get('limit', 100, type=int)

    conn = get_db()
    cursor = conn.cursor()

    if device_id:
        cursor.execute(
            'SELECT * FROM logs WHERE device_id = ? ORDER BY id DESC LIMIT ?',
            (device_id, limit)
        )
    else:
        cursor.execute(
            'SELECT * FROM logs ORDER BY id DESC LIMIT ?',
            (limit,)
        )

    rows = []
    for row in cursor.fetchall():
        r = dict(row)
        # JSON TEXT 컬럼을 객체로 복원하여 응답
        r['status_info'] = json.loads(r['status_info']) if r.get('status_info') else []
        r['vision_result'] = json.loads(r['vision_result']) if r.get('vision_result') else {}
        r['sensor_data'] = json.loads(r['sensor_data']) if r.get('sensor_data') else {}
        rows.append(r)

    conn.close()
    return jsonify(rows)


# 📌 API 3. 최신 데이터 동기화 (폴링용)
# GET /api/logs/after?last_id=500
# 권한: inspection_result (OPERATOR, TECHNICIAN, MASTER)
@api.route('/api/logs/after', methods=['GET'])
@require_permission('inspection_result')
def get_logs_after():
    last_id = request.args.get('last_id', 0, type=int)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT * FROM logs WHERE id > ? ORDER BY id ASC',
        (last_id,)
    )

    rows = []
    for row in cursor.fetchall():
        r = dict(row)
        r['status_info'] = json.loads(r['status_info']) if r.get('status_info') else []
        r['vision_result'] = json.loads(r['vision_result']) if r.get('vision_result') else {}
        r['sensor_data'] = json.loads(r['sensor_data']) if r.get('sensor_data') else {}
        rows.append(r)

    conn.close()
    return jsonify(rows)


# 📌 API 4. 대시보드 요약
# GET /api/dashboard/summary
# 권한: dashboard_view (OPERATOR, TECHNICIAN, MASTER)
@api.route('/api/dashboard/summary', methods=['GET'])
@require_auth
def get_dashboard_summary():
    conn = get_db()
    cursor = conn.cursor()

    # 🚀 성능 최적화: 4개의 개별 COUNT 쿼리를 1개의 집계 쿼리로 합침 (Full Scan 1회로 단축)
    cursor.execute('''
        SELECT 
            COUNT(*),
            SUM(CASE WHEN vision_result_code = 'OK' THEN 1 ELSE 0 END),
            SUM(CASE WHEN vision_result_code = 'NG' THEN 1 ELSE 0 END),
            SUM(CASE WHEN machine_status = 'ERROR' THEN 1 ELSE 0 END)
        FROM logs
    ''')
    row = cursor.fetchone()
    
    total_inspections = row[0] or 0
    ok_count = row[1] or 0
    ng_count = row[2] or 0
    error_count = row[3] or 0

    ng_rate = round((ng_count / total_inspections * 100), 2) if total_inspections > 0 else 0.0

    conn.close()

    # 장비 수는 인메모리 상태에서 집계
    total_devices = len(device_status)
    running_devices = sum(1 for d in device_status.values() if d["status"] == "RUN")
    error_devices = sum(1 for d in device_status.values() if d["status"] == "ERROR")

    return jsonify({
        "total_devices": total_devices,
        "running_devices": running_devices,
        "error_devices": error_devices,
        "total_inspections": total_inspections,
        "ok_count": ok_count,
        "ng_count": ng_count,
        "ng_rate": ng_rate,
        "error_count": error_count
    })


# ═══════════════════════════════════════════════════════════════
# 📊 통계 API 영역 (DB 테이블 추가 없이, logs 단일 테이블의 SQL 집계로 구현)
# 모바일 앱 프론트엔드(daily.tsx, weekly.tsx, monthly.tsx, yearly.tsx)의
# TypeScript 인터페이스에 100% 일치하는 JSON을 반환합니다.
# ═══════════════════════════════════════════════════════════════

# 📊 통계 API 1. 일간 통계
# GET /api/stats/daily?date=YYYY-MM-DD
@api.route('/api/stats/daily', methods=['GET'])
@require_auth
def get_daily_stats():
    from datetime import datetime
    target_date = request.args.get('date')
    if not target_date:
        target_date = datetime.now().strftime('%Y-%m-%d')

    conn = get_db()
    cursor = conn.cursor()

    # 1) 시간대별 상태 변화 추이 (status_trend_by_hour)
    cursor.execute('''
        SELECT 
            substr(timestamp, 12, 2) as hour,
            SUM(CASE WHEN machine_status = 'RUN' THEN 1 ELSE 0 END) as run_cnt,
            SUM(CASE WHEN machine_status = 'ERROR' THEN 1 ELSE 0 END) as err_cnt,
            SUM(CASE WHEN machine_status NOT IN ('RUN', 'ERROR') THEN 1 ELSE 0 END) as idle_cnt
        FROM logs
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY substr(timestamp, 12, 2)
        ORDER BY hour ASC
    ''', (target_date,))
    status_trend = [{"hour": row[0] + "시", "RUN": row[1], "ERROR": row[2], "IDLE": row[3]} for row in cursor.fetchall()]

    # 2) 평균 센서 상태 (average_sensor)
    cursor.execute('''
        SELECT sensor_data FROM logs
        WHERE substr(timestamp, 1, 10) = ? AND sensor_data IS NOT NULL AND sensor_data != ''
    ''', (target_date,))
    sensor_rows = cursor.fetchall()
    temp_sum = hum_sum = vib_x_sum = vib_y_sum = illu_sum = 0.0
    sensor_count = 0
    for row in sensor_rows:
        try:
            sd = json.loads(row[0])
            temp_sum += float(sd.get('temperature', 0))
            hum_sum += float(sd.get('humidity', 0))
            vib = sd.get('vibration', {})
            vib_x_sum += float(vib.get('x', 0)) if isinstance(vib, dict) else 0.0
            vib_y_sum += float(vib.get('y', 0)) if isinstance(vib, dict) else 0.0
            illu_sum += float(sd.get('light', 0))
            sensor_count += 1
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    avg_sensor = {
        "temperature": round(temp_sum / sensor_count, 1) if sensor_count > 0 else 0.0,
        "humidity": round(hum_sum / sensor_count, 1) if sensor_count > 0 else 0.0,
        "vibration_x": round(vib_x_sum / sensor_count, 2) if sensor_count > 0 else 0.0,
        "vibration_y": round(vib_y_sum / sensor_count, 2) if sensor_count > 0 else 0.0,
        "illumination": round(illu_sum / sensor_count, 1) if sensor_count > 0 else 0.0,
    }

    # 3) 일일 에러 발생 빈도 (daily_error_count)
    cursor.execute('''
        SELECT COUNT(*) FROM logs
        WHERE substr(timestamp, 1, 10) = ? AND machine_status = 'ERROR'
    ''', (target_date,))
    daily_error_count = cursor.fetchone()[0] or 0

    # 4) 일일 비전 NG 비율 (daily_vision_ng_rate)
    cursor.execute('''
        SELECT 
            COUNT(*),
            SUM(CASE WHEN vision_result_code = 'NG' THEN 1 ELSE 0 END)
        FROM logs
        WHERE substr(timestamp, 1, 10) = ?
    ''', (target_date,))
    ng_row = cursor.fetchone()
    total = ng_row[0] or 0
    ng = ng_row[1] or 0
    daily_vision_ng_rate = round((ng / total * 100), 2) if total > 0 else 0.0

    # 5) 심각도 분포 (severity_distribution)
    cursor.execute('''
        SELECT status_info FROM logs
        WHERE substr(timestamp, 1, 10) = ? AND machine_status = 'ERROR'
              AND status_info IS NOT NULL AND status_info != ''
    ''', (target_date,))
    sev_counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0}
    for row in cursor.fetchall():
        try:
            info_list = json.loads(row[0])
            if isinstance(info_list, list):
                for item in info_list:
                    sev = item.get('severity', '').upper()
                    if sev in sev_counts:
                        sev_counts[sev] += 1
            elif isinstance(info_list, dict):
                sev = info_list.get('severity', '').upper()
                if sev in sev_counts:
                    sev_counts[sev] += 1
        except (json.JSONDecodeError, TypeError):
            continue

    # 6) 장비별 로그 발생량 (log_count_by_device)
    cursor.execute('''
        SELECT device_id, COUNT(*) as cnt FROM logs
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY device_id
        ORDER BY cnt DESC
    ''', (target_date,))
    log_by_device = [{"device_id": row[0], "count": row[1]} for row in cursor.fetchall()]

    conn.close()
    return jsonify({
        "target_date": target_date,
        "status_trend_by_hour": status_trend,
        "average_sensor": avg_sensor,
        "daily_error_count": daily_error_count,
        "daily_vision_ng_rate": daily_vision_ng_rate,
        "severity_distribution": sev_counts,
        "log_count_by_device": log_by_device
    })


# 📊 통계 API 2. 주간 통계
# GET /api/stats/weekly?date=YYYY-MM-DD
@api.route('/api/stats/weekly', methods=['GET'])
@require_auth
def get_weekly_stats():
    from datetime import datetime, timedelta
    date_str = request.args.get('date')
    if date_str:
        ref_date = datetime.strptime(date_str, '%Y-%m-%d')
    else:
        ref_date = datetime.now()

    start_of_week = ref_date - timedelta(days=ref_date.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    week_start = start_of_week.strftime('%Y-%m-%d')
    week_end = end_of_week.strftime('%Y-%m-%d')
    iso_cal = ref_date.isocalendar()
    target_week = f"{iso_cal[0]}-W{iso_cal[1]:02d}"

    day_names = ['월', '화', '수', '목', '금', '토', '일']

    conn = get_db()
    cursor = conn.cursor()

    # 1) 요일별 에러 발생 추이 (error_trend_by_day)
    error_trend = []
    for i in range(7):
        d = (start_of_week + timedelta(days=i)).strftime('%Y-%m-%d')
        cursor.execute('''
            SELECT COUNT(*) FROM logs
            WHERE substr(timestamp, 1, 10) = ? AND machine_status = 'ERROR'
        ''', (d,))
        error_trend.append({"day": day_names[i], "error_count": cursor.fetchone()[0] or 0})

    # 2) 장비별 에러 발생 순위 (error_ranking_by_device)
    cursor.execute('''
        SELECT device_id, COUNT(*) as cnt FROM logs
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ? AND machine_status = 'ERROR'
        GROUP BY device_id ORDER BY cnt DESC
    ''', (week_start, week_end))
    error_ranking = [{"device_id": row[0], "error_count": row[1]} for row in cursor.fetchall()]

    # 3) 환경 데이터 이상치 (sensor_anomaly_by_day)
    anomaly_data = []
    for i in range(7):
        d = (start_of_week + timedelta(days=i)).strftime('%Y-%m-%d')
        cursor.execute('''
            SELECT sensor_data FROM logs
            WHERE substr(timestamp, 1, 10) = ? AND sensor_data IS NOT NULL AND sensor_data != ''
        ''', (d,))
        anomaly_count = 0
        for row in cursor.fetchall():
            try:
                sd = json.loads(row[0])
                temp = float(sd.get('temperature', 25))
                humidity = float(sd.get('humidity', 50))
                if temp > 40 or temp < 10 or humidity > 80 or humidity < 20:
                    anomaly_count += 1
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        anomaly_data.append({"day": day_names[i], "anomaly_count": anomaly_count})

    # 4) 에러 코드 TOP5 (top5_error_codes)
    cursor.execute('''
        SELECT status_info FROM logs
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ? AND machine_status = 'ERROR'
              AND status_info IS NOT NULL AND status_info != ''
    ''', (week_start, week_end))
    code_counter = {}
    for row in cursor.fetchall():
        try:
            info_list = json.loads(row[0])
            if isinstance(info_list, list):
                for item in info_list:
                    code = item.get('code', 'UNKNOWN')
                    code_counter[code] = code_counter.get(code, 0) + 1
            elif isinstance(info_list, dict):
                code = info_list.get('code', 'UNKNOWN')
                code_counter[code] = code_counter.get(code, 0) + 1
        except (json.JSONDecodeError, TypeError):
            continue
    top5 = sorted(code_counter.items(), key=lambda x: x[1], reverse=True)[:5]
    top5_codes = [{"code": c, "count": n} for c, n in top5]

    # 5) 장비 상태 분포 (status_distribution)
    cursor.execute('''
        SELECT 
            SUM(CASE WHEN machine_status = 'RUN' THEN 1 ELSE 0 END),
            SUM(CASE WHEN machine_status = 'ERROR' THEN 1 ELSE 0 END),
            SUM(CASE WHEN machine_status NOT IN ('RUN', 'ERROR') THEN 1 ELSE 0 END)
        FROM logs WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
    ''', (week_start, week_end))
    st_row = cursor.fetchone()

    conn.close()
    return jsonify({
        "target_week": target_week,
        "error_trend_by_day": error_trend,
        "error_ranking_by_device": error_ranking,
        "sensor_anomaly_by_day": anomaly_data,
        "top5_error_codes": top5_codes,
        "status_distribution": {"RUN": st_row[0] or 0, "ERROR": st_row[1] or 0, "IDLE": st_row[2] or 0}
    })


# 📊 통계 API 3. 월간 통계
# GET /api/stats/monthly?month=YYYY-MM
@api.route('/api/stats/monthly', methods=['GET'])
@require_auth
def get_monthly_stats():
    from datetime import datetime
    target_month = request.args.get('month')
    if not target_month:
        target_month = datetime.now().strftime('%Y-%m')

    conn = get_db()
    cursor = conn.cursor()

    # 1) 월간 설비 상태 비율 (status_distribution)
    cursor.execute('''
        SELECT 
            SUM(CASE WHEN machine_status = 'RUN' THEN 1 ELSE 0 END),
            SUM(CASE WHEN machine_status = 'ERROR' THEN 1 ELSE 0 END),
            SUM(CASE WHEN machine_status NOT IN ('RUN', 'ERROR') THEN 1 ELSE 0 END)
        FROM logs WHERE substr(timestamp, 1, 7) = ?
    ''', (target_month,))
    st_row = cursor.fetchone()

    # 2) 핵심 에러 코드 분포 (error_code_distribution)
    cursor.execute('''
        SELECT status_info FROM logs
        WHERE substr(timestamp, 1, 7) = ? AND machine_status = 'ERROR'
              AND status_info IS NOT NULL AND status_info != ''
    ''', (target_month,))
    code_counter = {}
    for row in cursor.fetchall():
        try:
            info_list = json.loads(row[0])
            if isinstance(info_list, list):
                for item in info_list:
                    code = item.get('code', 'UNKNOWN')
                    code_counter[code] = code_counter.get(code, 0) + 1
            elif isinstance(info_list, dict):
                code = info_list.get('code', 'UNKNOWN')
                code_counter[code] = code_counter.get(code, 0) + 1
        except (json.JSONDecodeError, TypeError):
            continue
    total_errors = sum(code_counter.values()) if code_counter else 1
    error_code_dist = sorted(
        [{"code": c, "count": n, "percentage": round(n / total_errors * 100, 2)} for c, n in code_counter.items()],
        key=lambda x: x['count'], reverse=True
    )

    # 3) 월간 평균 센서 변화 - 주차별 (sensor_trend_by_week)
    sensor_trend = []
    for week_num in range(1, 5):
        day_start = (week_num - 1) * 7 + 1
        day_end = min(week_num * 7, 28)
        start_d = f"{target_month}-{day_start:02d}"
        end_d = f"{target_month}-{day_end:02d}"
        cursor.execute('''
            SELECT sensor_data FROM logs
            WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
                  AND sensor_data IS NOT NULL AND sensor_data != ''
        ''', (start_d, end_d))
        t_sum = h_sum = vx_sum = 0.0
        cnt = 0
        for row in cursor.fetchall():
            try:
                sd = json.loads(row[0])
                t_sum += float(sd.get('temperature', 0))
                h_sum += float(sd.get('humidity', 0))
                vib = sd.get('vibration', {})
                vx_sum += float(vib.get('x', 0)) if isinstance(vib, dict) else 0.0
                cnt += 1
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        sensor_trend.append({
            "week": f"{week_num}주",
            "temperature": round(t_sum / cnt, 1) if cnt > 0 else 0.0,
            "humidity": round(h_sum / cnt, 1) if cnt > 0 else 0.0,
            "vibration_x": round(vx_sum / cnt, 2) if cnt > 0 else 0.0,
        })

    # 4) 장비별 에러 누적 (error_accumulation_by_device)
    cursor.execute('''
        SELECT device_id, COUNT(*) as cnt FROM logs
        WHERE substr(timestamp, 1, 7) = ? AND machine_status = 'ERROR'
        GROUP BY device_id ORDER BY cnt DESC
    ''', (target_month,))
    error_accum = [{"device_id": row[0], "total_error": row[1]} for row in cursor.fetchall()]

    # 5) 부위별 에러 발생률 (error_rate_by_part) - defect_type 기반
    cursor.execute('''
        SELECT defect_type, COUNT(*) as cnt FROM logs
        WHERE substr(timestamp, 1, 7) = ? AND defect_type IS NOT NULL
              AND defect_type != '' AND vision_result_code = 'NG'
        GROUP BY defect_type ORDER BY cnt DESC
    ''', (target_month,))
    part_rows = cursor.fetchall()
    total_defects = sum(r[1] for r in part_rows) if part_rows else 1
    error_rate_part = [{"part_location": row[0], "percentage": round(row[1] / total_defects * 100, 2)} for row in part_rows]

    conn.close()
    return jsonify({
        "target_month": target_month,
        "status_distribution": {"RUN": st_row[0] or 0, "ERROR": st_row[1] or 0, "IDLE": st_row[2] or 0},
        "error_code_distribution": error_code_dist,
        "sensor_trend_by_week": sensor_trend,
        "error_accumulation_by_device": error_accum,
        "error_rate_by_part": error_rate_part
    })


# 📊 통계 API 4. 연간 통계
# GET /api/stats/yearly?year=YYYY
@api.route('/api/stats/yearly', methods=['GET'])
@require_auth
def get_yearly_stats():
    from datetime import datetime
    target_year = request.args.get('year')
    if not target_year:
        target_year = str(datetime.now().year)

    conn = get_db()
    cursor = conn.cursor()

    # 1) 분기별 에러 발생 추이 (error_trend_by_quarter)
    quarter_data = []
    for q in range(1, 5):
        m_start = (q - 1) * 3 + 1
        m_end = q * 3
        start_d = f"{target_year}-{m_start:02d}"
        end_d = f"{target_year}-{m_end:02d}"
        cursor.execute('''
            SELECT COUNT(*) FROM logs
            WHERE substr(timestamp, 1, 7) BETWEEN ? AND ? AND machine_status = 'ERROR'
        ''', (start_d, end_d))
        quarter_data.append({"quarter": f"{q}분기", "error_count": cursor.fetchone()[0] or 0})

    # 2) 연간 상태 분포 (status_distribution)
    cursor.execute('''
        SELECT 
            SUM(CASE WHEN machine_status = 'RUN' THEN 1 ELSE 0 END),
            SUM(CASE WHEN machine_status = 'ERROR' THEN 1 ELSE 0 END),
            SUM(CASE WHEN machine_status NOT IN ('RUN', 'ERROR') THEN 1 ELSE 0 END)
        FROM logs WHERE substr(timestamp, 1, 4) = ?
    ''', (target_year,))
    st_row = cursor.fetchone()

    # 3) 심각도 기반 리스크 점수 (risk_score)
    cursor.execute('''
        SELECT status_info FROM logs
        WHERE substr(timestamp, 1, 4) = ? AND machine_status = 'ERROR'
              AND status_info IS NOT NULL AND status_info != ''
    ''', (target_year,))
    risk_score = 0
    severity_weights = {"CRITICAL": 10, "HIGH": 5, "MEDIUM": 2, "LOW": 1}
    for row in cursor.fetchall():
        try:
            info_list = json.loads(row[0])
            if isinstance(info_list, list):
                for item in info_list:
                    sev = item.get('severity', '').upper()
                    risk_score += severity_weights.get(sev, 0)
            elif isinstance(info_list, dict):
                sev = info_list.get('severity', '').upper()
                risk_score += severity_weights.get(sev, 0)
        except (json.JSONDecodeError, TypeError):
            continue

    # 4) 장기 에러 발생 트렌드 (long_term_error_trend)
    long_term = []
    for m in range(1, 13):
        month_str = f"{target_year}-{m:02d}"
        cursor.execute('''
            SELECT COUNT(*) FROM logs
            WHERE substr(timestamp, 1, 7) = ? AND machine_status = 'ERROR'
        ''', (month_str,))
        long_term.append({"month": f"{m:02d}월", "error_count": cursor.fetchone()[0] or 0})

    # 5) 연간 비전 NG 추이 (vision_ng_trend_by_month)
    vision_ng_trend = []
    for m in range(1, 13):
        month_str = f"{target_year}-{m:02d}"
        cursor.execute('''
            SELECT COUNT(*), SUM(CASE WHEN vision_result_code = 'NG' THEN 1 ELSE 0 END)
            FROM logs WHERE substr(timestamp, 1, 7) = ?
        ''', (month_str,))
        vr = cursor.fetchone()
        total_v = vr[0] or 0
        ng_v = vr[1] or 0
        ng_rate = round((ng_v / total_v * 100), 2) if total_v > 0 else 0.0
        vision_ng_trend.append({"month": f"{m:02d}월", "ng_rate": ng_rate})

    # 6) 연간 센서 안정성 분석 (sensor_stability_by_month)
    stability = []
    for m in range(1, 13):
        month_str = f"{target_year}-{m:02d}"
        cursor.execute('''
            SELECT sensor_data FROM logs
            WHERE substr(timestamp, 1, 7) = ? AND sensor_data IS NOT NULL AND sensor_data != ''
        ''', (month_str,))
        t_sum = h_sum = v_sum = 0.0
        cnt = 0
        for row in cursor.fetchall():
            try:
                sd = json.loads(row[0])
                t_sum += float(sd.get('temperature', 0))
                h_sum += float(sd.get('humidity', 0))
                vib = sd.get('vibration', {})
                v_sum += float(vib.get('x', 0)) if isinstance(vib, dict) else 0.0
                cnt += 1
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        stability.append({
            "month": f"{m:02d}월",
            "avg_temperature": round(t_sum / cnt, 1) if cnt > 0 else 0.0,
            "avg_humidity": round(h_sum / cnt, 1) if cnt > 0 else 0.0,
            "avg_vibration": round(v_sum / cnt, 2) if cnt > 0 else 0.0,
        })

    conn.close()
    return jsonify({
        "target_year": target_year,
        "error_trend_by_quarter": quarter_data,
        "status_distribution": {"RUN": st_row[0] or 0, "ERROR": st_row[1] or 0, "IDLE": st_row[2] or 0},
        "risk_score": risk_score,
        "long_term_error_trend": long_term,
        "vision_ng_trend_by_month": vision_ng_trend,
        "sensor_stability_by_month": stability
    })


# 📌 API 6. 잠긴 장비 목록 조회
# GET /api/devices/locked
# 권한: MASTER, TECHNICIAN만
@api.route('/api/devices/locked', methods=['GET'])
@require_auth
def get_locked_devices():
    user_role = request.user.get('role')
    if user_role not in ('MASTER', 'TECHNICIAN'):
        return jsonify({"error": "권한이 없습니다. (필요: MASTER 또는 TECHNICIAN)"}), 403

    return jsonify(list(locked_devices.values()))


# 📌 API 7. 장비 에러 해제 (모바일 앱에서 '확인' 버튼)
# POST /api/devices/<device_id>/resolve
# 권한: 에스컬레이션을 수락한 담당자 또는 MASTER
@api.route('/api/devices/<device_id>/resolve', methods=['POST'])
@require_auth
def resolve_device_error(device_id):
    user_id = request.user.get('user_id')
    user_role = request.user.get('role')
    username = request.user.get('username')

    if device_id not in locked_devices:
        return jsonify({"error": f"'{device_id}'는 현재 잠긴 상태가 아닙니다."}), 404

    # 에스컬레이션 권한 체크
    session = escalation_sessions.get(device_id)
    is_assigned = (session and session.get("assigned_to") == user_id)
    is_master = (user_role == 'MASTER')

    if not is_assigned and not is_master:
        return jsonify({"error": "오류를 해제할 권한이 없습니다. (담당자 또는 MASTER만 가능)"}), 403

    # 잠금 해제
    del locked_devices[device_id]
    
    # STANDBY 전환 및 타이머 시작
    from socket_events import set_standby_and_start_timer
    if hasattr(api, '_sio'):
        set_standby_and_start_timer(api._sio, device_id)
        api._sio.emit('device_status_changed', {
            "device_id": device_id,
            "status": "STANDBY",
            "message": "장비 잠금이 해제되었습니다. 가동 준비 중입니다."
        })
    else:
        device_status[device_id] = {"status": "STANDBY"}
    
    if device_id in escalation_sessions:
        del escalation_sessions[device_id]

    # 📝 잠금 해제 이력을 DB에 영구 저장
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO resolve_logs (device_id, resolved_by) VALUES (?, ?)',
            (device_id, username)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ resolve_logs 저장 실패: {e}")

    print(f"🔓 [{device_id}] 장비 잠금 해제됨 (해제자: {username})")

    # Socket.IO로 전체 클라이언트에 해제 알림 (main.py에서 sio를 주입받아 사용)
    resolve_data = {
        "device_id": device_id,
        "resolved_by": username
    }

    # sio 인스턴스에 접근하기 위해 app context 활용
    if hasattr(api, '_sio'):
        api._sio.emit('device_unlock', resolve_data)
        api._sio.emit('error_resolved', resolve_data)

    return jsonify({
        "message": f"장비 '{device_id}' 잠금 해제 완료",
        "resolved_by": username
    })

# 📌 API 8. 등록된 전체 장비 목록 조회 (마스터 데이터)
# GET /api/devices/registered
@api.route('/api/devices/registered', methods=['GET'])
@require_auth
def get_registered_devices():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT device_id, name, model_name, manager_username, idle_timeout, created_at FROM devices ORDER BY id ASC')
    devices = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # 담당자 상세 정보 매핑 (users.db 활용)
    try:
        from auth import get_users_db
        u_conn = get_users_db()
        u_cursor = u_conn.cursor()
        u_cursor.execute("SELECT username, nickname, role FROM users")
        user_dict = {row['username']: {'name': row['nickname'] or row['username'], 'role': row['role']} for row in u_cursor.fetchall()}
        u_conn.close()

        for d in devices:
            m_user = d.get('manager_username')
            if m_user and m_user in user_dict:
                d['manager_name'] = user_dict[m_user]['name']
                d['manager_role'] = user_dict[m_user]['role']
            else:
                d['manager_name'] = None
                d['manager_role'] = None
    except Exception as e:
        print(f"사용자 DB 연동 중 오류: {e}")

    return jsonify(devices)

# 📌 API 9. 새 장비 등록
# POST /api/devices/registered
# 권한: MASTER만
@api.route('/api/devices/registered', methods=['POST'])
@require_permission('device_manage')
def register_device():
    data = request.json
    device_id = data.get('device_id')
    name = data.get('name')
    model_name = data.get('model_name')
    manager_username = data.get('manager_username')

    idle_timeout = data.get('idle_timeout', 10)

    if not device_id or not name or not model_name:
        return jsonify({"error": "device_id, name, model_name이 모두 필요합니다."}), 400

    conn = get_db()
    cursor = conn.cursor()

    # 중복 담당자 검증 (1인 1장비)
    if manager_username:
        cursor.execute("SELECT device_id FROM devices WHERE manager_username = ?", (manager_username,))
        if cursor.fetchone():
            conn.close()
            return jsonify({"error": f"해당 사용자({manager_username})는 이미 다른 장비의 담당자로 지정되어 있습니다."}), 400

    try:
        cursor.execute('''
            INSERT INTO devices (device_id, name, model_name, manager_username, idle_timeout)
            VALUES (?, ?, ?, ?, ?)
        ''', (device_id, name, model_name, manager_username, idle_timeout))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": f"장비 등록 실패: {str(e)}"}), 400

    conn.close()
    
    # 런타임 상태 즉시 추가
    if device_id not in device_status:
        device_status[device_id] = {"status": "IDLE"}
        
    return jsonify({"message": f"장비 '{device_id}'가 성공적으로 등록되었습니다."}), 201

# 📌 API 9-1. 장비 담당자 변경
# PUT /api/devices/registered/<device_id>
# 권한: MASTER만
@api.route('/api/devices/registered/<device_id>', methods=['PUT'])
@require_permission('device_manage')
def update_device_manager(device_id):
    data = request.json
    manager_username = data.get('manager_username')
    idle_timeout = data.get('idle_timeout')

    conn = get_db()
    cursor = conn.cursor()

    # 중복 담당자 검증 (1인 1장비)
    if manager_username:
        cursor.execute("SELECT device_id FROM devices WHERE manager_username = ? AND device_id != ?", (manager_username, device_id))
        if cursor.fetchone():
            conn.close()
            return jsonify({"error": f"해당 사용자({manager_username})는 이미 다른 장비의 담당자로 지정되어 있습니다."}), 400

    if idle_timeout is not None:
        cursor.execute('UPDATE devices SET manager_username = ?, idle_timeout = ? WHERE device_id = ?', (manager_username, idle_timeout, device_id))
    else:
        cursor.execute('UPDATE devices SET manager_username = ? WHERE device_id = ?', (manager_username, device_id))
    
    if cursor.rowcount == 0:
        conn.close()
        return jsonify({"error": "장비를 찾을 수 없습니다."}), 404
        
    conn.commit()
    conn.close()
    return jsonify({"message": f"장비 '{device_id}'의 담당자가 변경되었습니다."})

# 📌 API 10. 장비 삭제
# DELETE /api/devices/registered/<device_id>
# 권한: MASTER만
@api.route('/api/devices/registered/<device_id>', methods=['DELETE'])
@require_permission('device_manage')
def delete_device(device_id):
    # 가동 중이거나 에러 상태인 장비는 삭제 불가
    status = device_status.get(device_id, {}).get('status', 'IDLE')
    if status in ['RUN', 'ERROR']:
        return jsonify({"error": f"장비 '{device_id}'가 가동 중이거나 오류 상태입니다. 정지 후 삭제해주세요."}), 400

    if device_id in locked_devices:
        return jsonify({"error": f"장비 '{device_id}'가 잠겨있습니다. 잠금 해제 후 삭제해주세요."}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM devices WHERE device_id = ?', (device_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()

    if deleted:
        # 런타임 상태에서도 제거
        if device_id in device_status:
            del device_status[device_id]
        return jsonify({"message": f"장비 '{device_id}'가 삭제되었습니다."})
    else:
        return jsonify({"error": f"장비 '{device_id}'를 찾을 수 없습니다."}), 404


# 📌 API 11. 로그 내보내기 및 비우기
# POST /api/logs/export-and-clear
# 권한: MASTER, TECHNICIAN만
@api.route('/api/logs/export-and-clear', methods=['POST'])
@require_auth
def export_and_clear_logs():
    import csv
    import os
    from datetime import datetime

    user_role = request.user.get('role')
    if user_role not in ('MASTER', 'TECHNICIAN'):
        return jsonify({"error": "권한이 없습니다. (필요: MASTER 또는 TECHNICIAN)"}), 403

    conn = get_db()
    cursor = conn.cursor()

    # 1) 현재 로그 전체 조회
    cursor.execute('SELECT * FROM logs ORDER BY id ASC')
    rows = cursor.fetchall()

    if len(rows) == 0:
        conn.close()
        return jsonify({"error": "내보낼 로그가 없습니다. (DB가 비어 있음)"}), 400

    # 2) CSV 저장 폴더 생성
    export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'exported_logs')
    os.makedirs(export_dir, exist_ok=True)

    # 3) 파일명: 내보낸 시각 기반
    now = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"logs_{now}.csv"
    filepath = os.path.join(export_dir, filename)

    # 4) CSV 파일 작성
    columns = [desc[0] for desc in cursor.description]
    with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(columns)   # 헤더
        writer.writerows(rows)     # 데이터

    row_count = len(rows)

    # 5) 로그 테이블 비우기
    cursor.execute('DELETE FROM logs')
    conn.commit()
    conn.close()

    print(f"📁 로그 {row_count}건 내보내기 완료 → {filepath}")

    return jsonify({
        "message": f"로그 {row_count}건을 CSV로 내보내고 DB를 비웠습니다.",
        "file": filename,
        "count": row_count
    })


# 📌 API 12. 장비 잠금 해제 이력 조회
# GET /api/resolve-logs?device_id=RASP_PI_01&limit=50
# 권한: 로그인 사용자 전원
@api.route('/api/resolve-logs', methods=['GET'])
@require_auth
def get_resolve_logs():
    device_id = request.args.get('device_id')
    limit = request.args.get('limit', 50, type=int)

    conn = get_db()
    cursor = conn.cursor()

    if device_id:
        cursor.execute(
            'SELECT * FROM resolve_logs WHERE device_id = ? ORDER BY id DESC LIMIT ?',
            (device_id, limit)
        )
    else:
        cursor.execute(
            'SELECT * FROM resolve_logs ORDER BY id DESC LIMIT ?',
            (limit,)
        )

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(rows)
