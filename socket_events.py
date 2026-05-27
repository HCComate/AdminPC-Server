import json
import random
import eventlet
from config import device_status, data_queue, locked_devices, online_users, escalation_sessions
from auth import decode_token

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

    # 2. 담당자 sid 찾기
    manager_sid = None
    for sid, info in online_users.items():
        if info.get('username') == manager_username:
            manager_sid = sid
            break

    queue = []
    
    # 0순위: 담당자 추가
    if manager_sid:
        queue.append(manager_sid)

    # 다른 인원 분류 (OPERATOR 제외)
    other_technicians = []
    other_masters = []
    for sid, info in online_users.items():
        if sid == manager_sid:
            continue
        role = info.get('role')
        if role == 'TECHNICIAN':
            other_technicians.append(sid)
        elif role == 'MASTER':
            other_masters.append(sid)

    import random
    random.shuffle(other_technicians)
    random.shuffle(other_masters)

    # 직급별 정책 적용
    if manager_role == 'MASTER':
        # 담당자가 MASTER면 다른 MASTER에게만 에스컬레이션
        queue.extend(other_masters)
    else:
        # 담당자가 TECHNICIAN이거나 없는 경우 (기본)
        queue.extend(other_technicians)
        queue.extend(other_masters)

    return queue

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
        # 모든 큐 소진 시 방치 (기존의 브로드캐스트 제거)
        print(f"🚨 [{device_id}] 에스컬레이션 큐 소진! 응답자가 없어 장비가 오류 상태로 방치됩니다.")
        return

    next_sid = session["queue"].pop(0)
    session["current_target"] = next_sid
    
    # 다음 사람에게만 전송
    sio.emit('escalation_alert', session["error_data"], to=next_sid)
    user_info = online_users.get(next_sid, {})
    print(f"📩 [{device_id}] 에스컬레이션 알림 발송 -> {user_info.get('username', next_sid)}")

    # 20초 타임아웃
    session["timer_task"] = eventlet.spawn_after(20.0, escalation_timeout, sio, device_id, next_sid)

def escalation_timeout(sio, device_id, sid):
    session = escalation_sessions.get(device_id)
    if session and session.get("current_target") == sid:
        user_info = online_users.get(sid, {})
        print(f"⏰ [{device_id}] {user_info.get('username', sid)} 응답 시간(20초) 초과. 다음 사람으로 넘어갑니다.")
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
            device_status[device_id] = {"status": "IDLE"}

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
        device_status[device_id] = {"status": "IDLE"}

        sio.emit('device_unlock', {
            "device_id": device_id,
            "resolved_by": "AdminPC"
        })

        sio.emit('error_resolved', {
            "device_id": device_id,
            "resolved_by": "AdminPC"
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
                    "batch_id": header.get('batch_id')
                }
                device_status[device_id] = {"status": "LOCKED"}

                # 라즈베리파이에 즉시 정지 명령
                sio.emit('device_lock', {"device_id": device_id})

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
            device_status[device_id] = {"status": "IDLE"}
            print(f"🔌 [{device_id}] 전원 ON (IDLE 전환)")
            sio.emit('device_status_changed', {
                "device_id": device_id,
                "status": "IDLE",
                "message": "장비 전원이 켜졌습니다."
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
        device_status[device_id] = {"status": "STOP"}
        print(f"\n⏹️ [{device_id}] 연속 가동 종료 완료 (총 {total_count}건)\n")
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
            print(f"🟢 [{payload['username']}] 근무 시작 (모바일 앱 접속)")
        except Exception as e:
            sio.emit('worker_auth_result', {"success": False, "error": str(e)}, to=sid)

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
            print(f"🔴 [{user_info['username']}] 퇴근 (모바일 앱 종료)")
        else:
            print(f"[{sid}] 클라이언트 연결 끊김")
