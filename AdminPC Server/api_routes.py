from flask import Blueprint, request, jsonify
from config import device_status
from database import get_db
from auth import require_auth, require_permission

api = Blueprint('api', __name__)


# 📌 API 1. 장비 목록 조회
# GET /api/devices
# 권한: dashboard_view (Operator, Technician, Master)
@api.route('/api/devices', methods=['GET'])
@require_auth
def get_devices():
    devices = [
        {"device_id": did, "status": info["status"]}
        for did, info in device_status.items()
    ]
    return jsonify(devices)


# 📌 API 2. 검사 이력 조회
# GET /api/logs?device_id=VISION_01&limit=100
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

    rows = [dict(row) for row in cursor.fetchall()]
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

    rows = [dict(row) for row in cursor.fetchall()]
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

    # 전체 검사 수
    cursor.execute('SELECT COUNT(*) FROM logs')
    total_inspections = cursor.fetchone()[0]

    # OK / NG 카운트
    cursor.execute("SELECT COUNT(*) FROM logs WHERE test_result = 'OK'")
    ok_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM logs WHERE test_result = 'NG'")
    ng_count = cursor.fetchone()[0]

    ng_rate = round((ng_count / total_inspections * 100), 2) if total_inspections > 0 else 0.0

    conn.close()

    # 장비 수는 인메모리 상태에서 집계
    total_devices = len(device_status)
    running_devices = sum(1 for d in device_status.values() if d["status"] == "RUN")

    return jsonify({
        "total_devices": total_devices,
        "running_devices": running_devices,
        "total_inspections": total_inspections,
        "ok_count": ok_count,
        "ng_count": ng_count,
        "ng_rate": ng_rate
    })
