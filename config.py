import queue
import os
from dotenv import load_dotenv

load_dotenv()

# ── 데이터베이스 ──
DB_NAME = 'inspection_logs.db'       # 검사 데이터 전용
USERS_DB_NAME = 'users.db'           # 사용자/인증 전용

# ── JWT 인증 ──
JWT_SECRET = os.getenv('JWT_SECRET', 'capstone-vision-mate-2026')   # 운영 시 반드시 환경 변수로 변경
JWT_EXPIRY_HOURS = 24                      # 토큰 유효 시간

# ── 역할별 권한 매핑 (제안서 기반) ──
ROLE_PERMISSIONS = {
    "OPERATOR": [
        "dashboard_view",       # 대시보드 조회
        "device_detail",        # 장비 상세 조회
        "inspection_result",    # 검사 결과 조회
        "alert_receive",        # 알림 수신
        "alert_assign",         # 알림 수신/배정
        "sensitivity_setting",  # 감도 설정 (Medium/Low)
    ],
    "TECHNICIAN": [
        "dashboard_view",
        "device_detail",
        "inspection_result",
        "inspection_stats",     # 검사 통계 조회
        "alert_receive",
        "alert_assign",
        "idle_setting",         # IDLE 기준 설정
        "log_export",           # 로그 내보내기
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
        "alert_priority",       # 알림 우선순위 설정
        "device_manage",        # 장비 등록/수정/삭제
        "user_manage",          # 사용자 관리
        "log_export",
        "system_setting",       # 시스템 설정 변경
        "sensitivity_setting",
    ],
}

# ── 공유 상태 ──
data_queue = queue.Queue()          # DB 저장용 큐 (라즈베리 파이 → DB 버퍼)
device_status = {}                  # 장비 상태 실시간 추적 딕셔너리
locked_devices = {}                 # CRITICAL 오류로 잠긴 장비 { device_id: { error_codes, timestamp, ... } }
online_users = {}                   # 모바일 앱 접속 중인 사용자 { sid: { user_id, username, nickname, role } }
