import os
import queue
from dotenv import load_dotenv

load_dotenv()

# ── 데이터베이스 설정 ──────────────────────────
DB_NAME       = os.getenv('DB_NAME',       'inspection_logs.db')
USERS_DB_NAME = os.getenv('USERS_DB_NAME', 'users.db')

# ── JWT 설정 ──────────────────────────────────
JWT_SECRET       = os.getenv('JWT_SECRET',       'capstone-vision-mate-2026')
JWT_EXPIRY_HOURS = int(os.getenv('JWT_EXPIRY_HOURS', '24'))

# ── 역할별 권한 매핑 ──────────────────────────
ROLE_PERMISSIONS = {
    "OPERATOR": [
        "dashboard_view",
        "device_detail",
        "inspection_result",
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
escalation_sessions = {}

