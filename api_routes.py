import json
from flask import Blueprint, request, jsonify
from config import device_status, locked_devices
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
# 권한: dashboard_view (Operator, Technician, Master)
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
# 권한: inspection_result (Operator, Technician, Master)
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
# 권한: inspection_result (Operator, Technician, Master)
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
# 권한: dashboard_view (Operator, Technician, Master)
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


# 📌 API 6. 잠긴 장비 목록 조회
# GET /api/devices/locked
# 권한: Master, Technician만
@api.route('/api/devices/locked', methods=['GET'])
@require_auth
def get_locked_devices():
    user_role = request.user.get('role')
    if user_role not in ('Master', 'Technician'):
        return jsonify({"error": "권한이 없습니다. (필요: Master 또는 Technician)"}), 403

    return jsonify(list(locked_devices.values()))


# 📌 API 7. 장비 에러 해제 (모바일 앱에서 '확인' 버튼)
# POST /api/devices/<device_id>/resolve
# 권한: Master, Technician만
@api.route('/api/devices/<device_id>/resolve', methods=['POST'])
@require_auth
def resolve_device_error(device_id):
    user_role = request.user.get('role')
    username = request.user.get('username')

    if user_role not in ('Master', 'Technician'):
        return jsonify({"error": "권한이 없습니다. (필요: Master 또는 Technician)"}), 403

    if device_id not in locked_devices:
        return jsonify({"error": f"'{device_id}'는 현재 잠긴 상태가 아닙니다."}), 404

    # 잠금 해제
    del locked_devices[device_id]
    device_status[device_id] = {"status": "IDLE"}

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

