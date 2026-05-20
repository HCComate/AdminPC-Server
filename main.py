import eventlet
import threading
import socketio
from flask import Flask

from database import init_db, db_worker, get_db
from auth import auth, init_users_db
from api_routes import api
from socket_events import register_events
from config import device_status

# Flask 앱 생성 및 블루프린트 등록
flask_app = Flask(__name__)
flask_app.register_blueprint(api)     # REST API (검사 데이터)
flask_app.register_blueprint(auth)    # 인증 / 사용자 관리


# CORS 허용 (React 프론트엔드에서 API 호출 허용)
@flask_app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    return response

# Socket.IO 서버 생성 및 Flask 앱 통합
sio = socketio.Server(cors_allowed_origins='*')
app = socketio.WSGIApp(sio, flask_app)

# Socket.IO 이벤트 핸들러 등록
register_events(sio)

# api 블루프린트에서 소켓 이벤트를 보낼 수 있도록 sio 인스턴스 주입
api._sio = sio

# 서버 실행
if __name__ == '__main__':
    init_db()           # 검사 로그 DB (inspection_logs.db)
    init_users_db()     # 사용자 DB   (users.db)

    # 🚀 장비 상태(device_status)를 DB에 등록된 장비 목록으로 초기화
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT device_id FROM devices')
    for row in cursor.fetchall():
        device_id = row['device_id']
        device_status[device_id] = {"status": "IDLE"}
    conn.close()
    print(f"✅ 총 {len(device_status)}대의 장비를 인메모리 상태에 IDLE로 초기화했습니다.")

    # DB 저장 전담 스레드 가동
    threading.Thread(target=db_worker, daemon=True).start()
    print("✅ DB 저장 스레드 백그라운드 가동 시작...")

    print("🚀 관리자 PC 서버가 포트 5000에서 대기 중입니다...")
    print("📡 REST API 엔드포인트:")
    print("   [인증]")
    print("   POST /api/auth/login       - 로그인")
    print("   GET  /api/auth/me          - 내 정보 조회")
    print("   [사용자 관리 - Master 전용]")
    print("   GET  /api/users            - 사용자 목록")
    print("   POST /api/users            - 사용자 등록")
    print("   DELETE /api/users/<id>     - 사용자 삭제")
    print("   [검사 데이터]")
    print("   GET  /api/devices          - 장비 목록 조회")
    print("   GET  /api/logs             - 검사 이력 조회")
    print("   GET  /api/logs/after       - 최신 데이터 동기화")
    print("   GET  /api/dashboard/summary - 대시보드 요약")
    print("   [장비 관리 - Master 전용]")
    print("   GET  /api/devices/registered - 전체 장비 마스터 조회")
    print("   POST /api/devices/registered - 새 장비 등록")
    print("   DELETE /api/devices/registered/<id> - 장비 삭제")
    print("   [장비 잠금/해제 - Master, Technician 전용]")
    print("   GET  /api/devices/locked   - 잠긴 장비 목록")
    print("   POST /api/devices/<id>/resolve - 장비 잠금 해제")
    eventlet.wsgi.server(eventlet.listen(('0.0.0.0', 5000)), app)
