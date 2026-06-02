import json
import random
import eventlet
from datetime import datetime
from config import device_status, data_queue, locked_devices, online_users, escalation_sessions, mobile_online_users
from auth import decode_token

standby_versions = {}

def get_idle_timeout(device_id):
    try:
        from database import get_db
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT idle_timeout FROM devices WHERE device_id = ?", (device_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row['idle_timeout'] is not None:
            return row['idle_timeout']
    except Exception as e:
        print("Timeout DB fetch error:", e)
    return 10

def set_standby_and_start_timer(sio, device_id):
    device_status[device_id] = {"status": "STANDBY"}
    version = standby_versions.get(device_id, 0) + 1
    standby_versions[device_id] = version
    
    timeout_seconds = get_idle_timeout(device_id)
    
    def timer_task(expected_version):
        eventlet.sleep(timeout_seconds)
        if device_status.get(device_id, {}).get("status") == "STANDBY" and standby_versions.get(device_id) == expected_version:
            device_status[device_id] = {"status": "IDLE"}
            print(f"⏱️ [{device_id}] {timeout_seconds}초 유휴 시간 초과 -> IDLE 전환")
            sio.emit('device_status_changed', {
                "device_id": device_id,
                "status": "IDLE",
                "message": f"장비가 설정된 시간({timeout_seconds}초) 동안 아무 작업도 수행하지 않아 대기(IDLE) 모드로 전환되었습니다."
            })
            
    eventlet.spawn(timer_task, version)

def build_escalation_queue(device_id):
    # 1. 담당자 조회
    from database import get_db
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT manager_username FROM devices WHERE device_id = ?", (device_id,))
    row = cursor.fetchone()
    manager_username = row['manager_username'] if row else None
    conn.close()

    manager_role = None
    if manager_username:
        from auth import get_users_db
        u_conn = get_users_db()
        u_cursor = u_conn.cursor()
        u_cursor.execute("SELECT role FROM users WHERE username = ?", (manager_username,))
        u_row = u_cursor.fetchone()
        manager_role = u_row['role'] if u_row else None
        u_conn.close()

    # 2. online_users(Socket.IO) + mobile_online_users(REST heartbeat) 통합
    #    ⚠️ username 기준으로 중복 제거 — 같은 사람이 두 경로로 접속해도 큐에 1번만 등록
    #    모바일 앱은 /api/alerts/pending REST 폴링으로 알림을 받으므로 mobile_ 키를 우선
    combined_users = {}  # key → { username, role }
    seen_usernames = set()

    # 2-1. 모바일 REST 접속자 우선 등록
    for username, info in mobile_online_users.items():
        uname = info.get('username')
        if uname and uname not in seen_usernames:
            combined_users[f"mobile_{username}"] = info
            seen_usernames.add(uname)

    # 2-2. Socket.IO 접속자 중 아직 등록 안 된 username만 추가
    for sid, info in online_users.items():
        uname = info.get('username')
        if uname and uname not in seen_usernames:
            combined_users[sid] = info
            seen_usernames.add(uname)

    # 3. 담당자 key 찾기
    manager_sid = None
    for key, info in combined_users.items():
        if info.get('username') == manager_username:
            manager_sid = key
            break

    queue = []

    # 0순위: 담당자 추가
    if manager_sid:
        queue.append(manager_sid)

    # 다른 인원 분류 (OPERATOR 제외)
    other_technicians = []
    other_masters = []
    for key, info in combined_users.items():
        if key == manager_sid:
            continue
        role = info.get('role')
        if role == 'TECHNICIAN':
            other_technicians.append(key)
        elif role == 'MASTER':
            other_masters.append(key)

    import random
    random.shuffle(other_technicians)
    random.shuffle(other_masters)

    # 직급별 정책 적용
    if manager_role == 'MASTER':
        queue.extend(other_masters)
    else:
        queue.extend(other_technicians)
        queue.extend(other_masters)

    return queue

def _retry_escalation(sio, device_id):
    """큐 소진 후 60초가 지나도 미해결이면 에스컬레이션을 처음부터 재시작합니다."""
    if device_id not in locked_devices:
        return  # 이미 해결됨
    error_data = locked_devices[device_id]
    print(f"🔄 [{device_id}] 미해결 CRITICAL — 에스컬레이션 재시작")
    # 기존 세션 제거 후 재시작
    if device_id in escalation_sessions:
        del escalation_sessions[device_id]
    start_escalation(sio, device_id, error_data)

def start_escalation(sio, device_id, error_data):
    queue = build_escalation_queue(device_id)
    if not queue:
        # 온라인 사용자 없음 -> 0순위 브로드캐스트
        sio.emit('critical_alert', error_data)
        print(f"🚨 [{device_id}] CRITICAL 오류! (접속자 없음, 전체 알림 발송)")
        return

    escalation_sessions[device_id] = {
        "queue": queue,
        "current_target": None,
        "assigned_to": None,
        "timer_task": None,
        "error_data": error_data
    }
    print(f"🔄 [{device_id}] 에스컬레이션 시작: {len(queue)}명 대기 중")
    notify_next_escalation(sio, device_id)

def notify_next_escalation(sio, device_id):
    session = escalation_sessions.get(device_id)
    if not session:
        return

    # 기존 타이머 취소
    if session.get("timer_task"):
        session["timer_task"].cancel()
        session["timer_task"] = None

    if not session["queue"]:
        # 큐 소진 → 2분 후 전체 사용자 대상 재에스컬레이션
        print(f"🚨 [{device_id}] 에스컬레이션 큐 소진! 2분 후 전체 재알림 예정")
        sio.emit('critical_alert', {**session["error_data"], "retry": True})
        session["timer_task"] = eventlet.spawn_after(
            120.0, _retry_escalation, sio, device_id
        )
        return

    next_sid = session["queue"].pop(0)
    session["current_target"] = next_sid

    error_data = session["error_data"]

    if next_sid.startswith("mobile_"):
        # REST heartbeat 모바일 유저 → /api/alerts/pending 폴링으로 수신 (별도 처리 불필요)
        username = next_sid.replace("mobile_", "")
        print(f"📩 [{device_id}] 에스컬레이션 알림 (모바일 REST) -> {username}")
    else:
        # Socket.IO 연결 유저 → 직접 이벤트 전송
        sio.emit('escalation_alert', error_data, to=next_sid)
        user_info = online_users.get(next_sid, {})
        print(f"📩 [{device_id}] 에스컬레이션 알림 (Socket.IO) -> {user_info.get('username', next_sid)}")

    # 2분(120초) 타임아웃
    session["timer_task"] = eventlet.spawn_after(120.0, escalation_timeout, sio, device_id, next_sid)

def escalation_timeout(sio, device_id, sid):
    session = escalation_sessions.get(device_id)
    if session and session.get("current_target") == sid:
        user_info = online_users.get(sid, {})
        print(f"⏰ [{device_id}] {user_info.get('username', sid)} 응답 시간(2분) 초과. 다음 사람으로 넘어갑니다.")
        notify_next_escalation(sio, device_id)


def register_events(sio):
    """Socket.IO 이벤트 핸들러를 서버 인스턴스에 등록"""

    @sio.event
    def connect(sid, environ):
        print(f"[{sid}] 클라이언트 연결됨")

    # 🔓 웹 UI에서 '전체 잠금 해제' 버튼을 눌렀을 때 들어오는 이벤트
    @sio.on('unlock_all_devices')
    def on_unlock_all(sid):
        if not locked_devices:
            print("ℹ️ 잠긴 장비가 없습니다.")
            return

        unlocked_list = list(locked_devices.keys())
        for device_id in unlocked_list:
            del locked_devices[device_id]
            if device_id in escalation_sessions:
                del escalation_sessions[device_id]
            
            set_standby_and_start_timer(sio, device_id)

            # 라즈베리파이에 잠금 해제 명령
            sio.emit('device_unlock', {
                "device_id": device_id,
                "resolved_by": "AdminPC (일괄 해제)"
            })

            # 프론트엔드 + 모바일에 해제 알림
            sio.emit('error_resolved', {
                "device_id": device_id,
                "resolved_by": "AdminPC (일괄 해제)"
            })
            
            # 상태 변경 알림
            sio.emit('device_status_changed', {
                "device_id": device_id,
                "status": "STANDBY",
                "message": "장비 잠금이 일괄 해제되었습니다. 가동 준비 중입니다."
            })

        print(f"🔓 전체 장비 잠금 해제 완료: {unlocked_list}")

    # 🔓 웹 UI에서 개별 장비 '잠금 해제' 버튼을 눌렀을 때 들어오는 이벤트
    @sio.on('unlock_device')
    def on_unlock_device(sid, data):
        device_id = data.get("device_id")
        if device_id not in locked_devices:
            return

        del locked_devices[device_id]
        if device_id in escalation_sessions:
            del escalation_sessions[device_id]
            
        set_standby_and_start_timer(sio, device_id)

        sio.emit('device_unlock', {
            "device_id": device_id,
            "resolved_by": "AdminPC"
        })

        sio.emit('error_resolved', {
            "device_id": device_id,
            "resolved_by": "AdminPC"
        })

        sio.emit('device_status_changed', {
            "device_id": device_id,
            "status": "STANDBY",
            "message": "장비 잠금이 해제되었습니다. 가동 준비 중입니다."
        })

        print(f"🔓 개별 장비 잠금 해제 완료: {device_id}")


    # 🌟 라즈베리 파이에서 검사 데이터를 실시간으로 받을 때
    @sio.on('device_data')
    def on_device_data(sid, data):
        header = data.get('header', {})
        body = data.get('body', {})

        device_id = header.get('device_id')
        machine_status = body.get('machine_status', 'UNKNOWN')
        vision_result = body.get('vision_result', {})

        # 장비 상태 실시간 갱신 (잠긴 장비는 덮어쓰지 않음, ERROR는 아래에서 별도 처리)
        if device_id not in locked_devices and machine_status != "ERROR":
            device_status[device_id] = {"status": machine_status}

        # 1) DB 저장을 위해 큐에 데이터 적재 (튜플 형태)
        row_data = (
            device_id,
            header.get('batch_id'),
            header.get('model_name'),
            body.get('sequence'),
            machine_status,
            json.dumps(body.get('status_info', []), ensure_ascii=False),
            json.dumps(vision_result, ensure_ascii=False),
            vision_result.get('result'),        # OK / NG (빠른 조회용)
            vision_result.get('defect_type'),    # 결함 유형 (빠른 조회용)
            json.dumps(body.get('sensor_data', {}), ensure_ascii=False),
            body.get('timestamp')
        )
        data_queue.put(row_data)

        # 2) ERROR 판별용 변수 추출
        if machine_status == "ERROR":
            status_info = body.get('status_info', [])
            codes = [s.get('code', '?') for s in status_info]
            severities = [s.get('severity', '') for s in status_info]
            has_critical = any(s == 'CRITICAL' for s in severities)
        else:
            has_critical = False
            codes = []

        # 모바일 앱 서버로 실시간 데이터 포워딩 (중계 역할)
        sio.emit('mobile_data_feed', data)

        # 3) ERROR 상태 처리: CRITICAL일 때만 장비 잠금 + 알림
        if machine_status == "ERROR":

            if has_critical:
                # ── CRITICAL: 장비 잠금 + 긴급 알림 ──
                locked_devices[device_id] = {
                    "device_id": device_id,
                    "error_codes": codes,
                    "timestamp": body.get('timestamp'),
                    "batch_id": header.get('batch_id'),
                    "locked_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                    "severity": "CRITICAL",
                    "message": f"CRITICAL 오류 발생: {', '.join(codes)}"
                }
                device_status[device_id] = {"status": "LOCKED"}

                # 라즈베리파이에 즉시 정지 명령
                sio.emit('device_lock', {"device_id": device_id})
                
                # 웹 UI에 LOCKED 상태 방송
                sio.emit('device_status_changed', {
                    "device_id": device_id,
                    "status": "LOCKED",
                    "message": "치명적 오류로 인해 장비가 잠겼습니다."
                })

                # 에스컬레이션 알림 시스템 시작
                start_escalation(sio, device_id, locked_devices[device_id])
            else:
                # CRITICAL이 아닌 에러 → 가동 유지 (RUN 상태 덮어쓰기)
                device_status[device_id] = {"status": "RUN"}
                print(f"⚠️ [{device_id}] 오류 발생(가동 유지) 코드: {', '.join(codes)}")
        else:
            if int(body.get('sequence', 0)) % 10 == 0:
                print(f"[{device_id}] 연속 가동 중 - {body.get('sequence')}건 수신 완료")
    # 🔌 웹 UI에서 전원 ON 요청
    @sio.on('ui_power_on')
    def on_ui_power_on(sid, data):
        device_id = data.get('device_id')
        if device_id and device_id not in locked_devices:
            print(f"🔌 [{device_id}] 전원 ON (STANDBY 전환)")
            set_standby_and_start_timer(sio, device_id)
            sio.emit('device_status_changed', {
                "device_id": device_id,
                "status": "STANDBY",
                "message": "장비 전원이 켜졌습니다. 가동 준비 중입니다."
            })

    # 🔌 웹 UI에서 전원 OFF 요청
    @sio.on('ui_power_off')
    def on_ui_power_off(sid, data):
        device_id = data.get('device_id')
        if device_id and device_id not in locked_devices:
            device_status[device_id] = {"status": "STOP"}
            print(f"🔌 [{device_id}] 전원 OFF (STOP 전환)")
            sio.emit('device_status_changed', {
                "device_id": device_id,
                "status": "STOP",
                "message": "장비 전원이 꺼졌습니다."
            })

    # 🔄 연속 가동 장비: 웹 UI에서 시작 버튼을 눌렀을 때
    @sio.on('ui_start_continuous')
    def on_ui_start_continuous(sid, data):
        target_device = data.get('device_id')

        # ⛔ 잠긴 장비는 시작 차단
        if target_device in locked_devices:
            sio.emit('start_blocked', {
                "device_id": target_device,
                "reason": "치명적 오류가 해결되지 않았습니다."
            }, to=sid)
            return

        print(f"\n🔄 웹 UI로부터 [{target_device}] 연속 가동 요청을 받았습니다.")
        device_status[target_device] = {"status": "IDLE"}

        # 라즈베리 파이에게 연속 가동 시작 명령
        sio.emit('start_continuous', data)

    # 🔄 연속 가동 장비: 웹 UI에서 종료 버튼을 눌렀을 때
    @sio.on('ui_stop_continuous')
    def on_ui_stop_continuous(sid, data):
        device_id = data.get('device_id')
        print(f"⏹️ 웹 UI로부터 [{device_id}] 연속 가동 종료 요청을 받았습니다.")
        # 라즈베리 파이에게 종료 명령
        sio.emit('stop_continuous', data)

    # 🔄 연속 가동 장비: 라즈베리 파이가 종료 완료를 알렸을 때
    @sio.on('continuous_stopped')
    def on_continuous_stopped(sid, data):
        device_id = data.get('device_id')
        total_count = data.get('total_count', 0)
        print(f"\n⏹️ [{device_id}] 연속 가동 종료 완료 (총 {total_count}건)\n")
        set_standby_and_start_timer(sio, device_id)
        # 프론트엔드로 종료 완료 알림
        sio.emit('continuous_stopped_notify', data)

    # 📱 모바일 앱 사용자 인증 (소켓 연결 후 토큰을 보내 근무 상태 등록)
    @sio.on('worker_auth')
    def on_worker_auth(sid, data):
        token = data.get('token')
        if not token:
            sio.emit('worker_auth_result', {"success": False, "error": "토큰이 필요합니다."}, to=sid)
            return

        try:
            payload = decode_token(token)
            user_info = {
                'user_id': payload['user_id'],
                'username': payload['username'],
                'role': payload['role'],
            }
            online_users[sid] = user_info
            
            # 모바일 앱에서 미리 캐싱할 수 있도록 비전 검사 이미지 URL 목록 전달
            images = {
                "ok": "/static/images/vision_ok.png",
                "crack": "/static/images/vision_crack.png",
                "dent": "/static/images/vision_dent.png",
                "misaligned": "/static/images/vision_misaligned.png",
                "missing": "/static/images/vision_missing.png",
                "open": "/static/images/vision_open.png",
                "scratch": "/static/images/vision_scratch.png"
            }
            sio.emit('worker_auth_result', {"success": True, "user": user_info, "images": images}, to=sid)

            # 전체 클라이언트에게 근무자 상태 변경 알림
            sio.emit('worker_status_changed', {
                "user_id": payload['user_id'],
                "username": payload['username'],
                "is_online": True
            })
            print(f"🟢 [{payload['username']}] 근무 시작 (앱/웹 접속)")
        except Exception as e:
            sio.emit('worker_auth_result', {"success": False, "error": str(e)}, to=sid)

    @sio.on('mobile_presence')
    def on_mobile_presence(sid, data):
        # data = [{"username": "hansung1", "user_id": 1, "role": "OPERATOR"}, ...]
        from config import mobile_online_users
        import time

        new_active_usernames = set()
        for user_data in data:
            username = user_data.get('username')
            if username:
                new_active_usernames.add(username)
                
                # 만약 기존에 없던 유저라면 근무 시작 알림
                if username not in mobile_online_users:
                    sio.emit('worker_status_changed', {
                        "user_id": user_data.get('user_id'),
                        "username": username,
                        "is_online": True
                    })
                    print(f"🟢 [{username}] 근무 시작 (모바일 앱 접속)")
                
                mobile_online_users[username] = {
                    "user_id": user_data.get('user_id'),
                    "username": username,
                    "role": user_data.get('role'),
                    "last_seen": time.time()
                }

        # 기존 모바일 접속자 중 이번 heartbeat에 없는(끊긴) 유저 처리
        expired_users = []
        for username, info in list(mobile_online_users.items()):
            if username not in new_active_usernames:
                expired_users.append(info)
                del mobile_online_users[username]
                
        for info in expired_users:
            sio.emit('worker_status_changed', {
                "user_id": info['user_id'],
                "username": info['username'],
                "is_online": False
            })
            print(f"🔴 [{info['username']}] 퇴근 (모바일 앱 접속 종료)")

    # 🚨 에스컬레이션 관련 이벤트
    @sio.on('escalation_accept')
    def on_escalation_accept(sid, data):
        device_id = data.get('device_id')
        session = escalation_sessions.get(device_id)
        if session and session.get("current_target") == sid:
            if session.get("timer_task"):
                session["timer_task"].cancel()
                session["timer_task"] = None
            
            user_info = online_users.get(sid, {})
            session["assigned_to"] = user_info.get("user_id")
            session["current_target"] = None
            
            sio.emit('escalation_assigned', {
                "device_id": device_id,
                "assigned_to": user_info.get("user_id"),
                "username": user_info.get("username")
            })
            print(f"✅ [{device_id}] {user_info.get('username')}님이 오류 수정을 수락했습니다.")

    @sio.on('escalation_reject')
    def on_escalation_reject(sid, data):
        device_id = data.get('device_id')
        session = escalation_sessions.get(device_id)
        if session and session.get("current_target") == sid:
            user_info = online_users.get(sid, {})
            print(f"❌ [{device_id}] {user_info.get('username')}님이 알림을 거절했습니다.")
            notify_next_escalation(sio, device_id)

    @sio.on('escalation_giveup')
    def on_escalation_giveup(sid, data):
        device_id = data.get('device_id')
        session = escalation_sessions.get(device_id)
        user_info = online_users.get(sid, {})
        if session and session.get("assigned_to") == user_info.get("user_id"):
            print(f"🏳️ [{device_id}] {user_info.get('username')}님이 수락 후 오류 수정을 포기했습니다. 다음 작업자 호출.")
            session["assigned_to"] = None
            notify_next_escalation(sio, device_id)

    @sio.event
    def disconnect(sid):
        # 모바일 앱 사용자였다면 근무 상태 해제
        if sid in online_users:
            user_info = online_users.pop(sid)
            sio.emit('worker_status_changed', {
                "user_id": user_info['user_id'],
                "username": user_info['username'],
                "is_online": False
            })
            print(f"🔴 [{user_info['username']}] 퇴근 (앱/웹 접속 종료)")
        else:
            print(f"[{sid}] 클라이언트 연결 끊김")
