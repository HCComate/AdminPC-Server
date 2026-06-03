import os
import queue
import secrets
from dotenv import load_dotenv

load_dotenv()

# ── 데이터베이스 설정 ──────────────────────────
DB_NAME       = os.getenv('DB_NAME',       'inspection_logs.db')
USERS_DB_NAME = os.getenv('USERS_DB_NAME', 'users.db')

# ── JWT 설정 ──────────────────────────────────
# .env에 JWT_SECRET이 없으면 서버 실행 시마다 랜덤 키 생성 (재시작 시 기존 토큰 무효화)
JWT_SECRET       = os.getenv('JWT_SECRET') or secrets.token_hex(32)
JWT_EXPIRY_HOURS = int(os.getenv('JWT_EXPIRY_HOURS', '24'))

# ── 내부 통신 보안 ────────────────────────────
INTERNAL_SECRET  = os.getenv('INTERNAL_SECRET', 'capstone2026')

# ── CORS 허용 출처 ────────────────────────────
# 쉼표로 구분하여 여러 주소 허용 가능 (예: http://localhost:5173,http://192.168.0.10:5173)
CORS_ORIGINS     = os.getenv('CORS_ORIGINS', 'http://localhost:5173,http://localhost:3000').split(',')

# ── 역할별 권한 매핑 ──────────────────────────
ROLE_PERMISSIONS = {
    "OPERATOR": [
        "dashboard_view",
        "device_detail",
        "inspection_result",
        "inspection_stats",
        "alert_receive",
        "alert_assign",
        "sensitivity_setting",
    ],
    "TECHNICIAN": [
        "dashboard_view",
        "device_detail",
        "inspection_result",
        "inspection_stats",
        "alert_receive",
        "alert_assign",
        "idle_setting",
        "log_export",
        "sensitivity_setting",
    ],
    "MASTER": [
        "dashboard_view",
        "device_detail",
        "inspection_result",
        "inspection_stats",
        "alert_receive",
        "alert_assign",
        "idle_setting",
        "alert_priority",
        "device_manage",
        "user_manage",
        "log_export",
        "system_setting",
        "sensitivity_setting",
    ],
}

# ── 공유 상태 ─────────────────────────────────
data_queue     = queue.Queue()
device_status  = {}
locked_devices = {}
online_users   = {}
mobile_online_users = {} # { username: { "user_id": 1, "username": "hansung1", "role": "OPERATOR", "last_seen": timestamp } }
escalation_sessions = {}

