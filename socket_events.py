import json
from config import device_status, data_queue, locked_devices, online_users
from auth import decode_token


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

    # 🌟 웹 UI에서 '특정 장비 시작 버튼'을 눌렀을 때 들어오는 이벤트
    @sio.on('ui_start_btn')
    def on_ui_start_btn(sid, data):
        target_device = data.get('device_id')

        # ⛔ 잠긴 장비는 시작 차단
        if target_device in locked_devices:
            sio.emit('start_blocked', {
                "device_id": target_device,
                "reason": "치명적 오류가 해결되지 않았습니다."
            }, to=sid)
            print(f"⛔ [{target_device}] 잠금 상태 - 가동 요청 거부됨")
            return

        print(f"\n▶️ 웹 UI로부터 [{target_device}] 가동 요청을 받았습니다.")

        # 장비 상태를 IDLE로 초기 등록 (아직 데이터 수신 전)
        device_status[target_device] = {"status": "IDLE"}

        # 라즈베리 파이에게 해당 장비를 켜라고 명령 하달
        sio.emit('start_request', data)

    # 🌟 라즈베리 파이에서 검사 데이터를 실시간으로 받을 때
    @sio.on('device_data')
    def on_device_data(sid, data):
        header = data.get('header', {})
        body = data.get('body', {})

        device_id = header.get('device_id')
        machine_status = body.get('machine_status', 'UNKNOWN')
        vision_result = body.get('vision_result', {})

        # 장비 상태 실시간 갱신 (잠긴 장비는 덮어쓰지 않음)
        if device_id not in locked_devices:
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

        # 2) 모바일 앱 서버로 실시간 데이터 포워딩 (중계 역할)
        sio.emit('mobile_data_feed', data)

        # 3) ERROR 상태 처리: CRITICAL일 때만 장비 잠금 + 알림
        if machine_status == "ERROR":
            status_info = body.get('status_info', [])
            codes = [s.get('code', '?') for s in status_info]
            severities = [s.get('severity', '') for s in status_info]
            has_critical = any(s == 'CRITICAL' for s in severities)

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

                # 프론트엔드 + 모바일앱에 긴급 알림 브로드캐스트
                sio.emit('critical_alert', locked_devices[device_id])

                print(f"🚨 [{device_id}] CRITICAL 오류! 장비 잠금 및 긴급 알림 발송!")
            else:
                # CRITICAL이 아닌 에러 → 가동 유지 (RUN 상태 덮어쓰기)
                device_status[device_id] = {"status": "RUN"}
                print(f"⚠️ [{device_id}] 오류 발생(가동 유지) 코드: {', '.join(codes)}")
        else:
            print(f"[{device_id}] {body.get('sequence')}/100 수신 완료")

    @sio.on('batch_complete')
    def on_batch_complete(sid, data):
        device_id = data.get('device_id')
        # 검사 완료 → 장비 상태를 STOP으로 변경
        device_status[device_id] = {"status": "STOP"}
        print(f"\n🏁 --- [{device_id}] 100개 검사 완료 --- 🏁\n")
        # 프론트엔드로 완료 이벤트 포워딩
        sio.emit('batch_complete_notify', data)

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
            sio.emit('worker_auth_result', {"success": True, "user": user_info}, to=sid)

            # 전체 클라이언트에게 근무자 상태 변경 알림
            sio.emit('worker_status_changed', {
                "user_id": payload['user_id'],
                "username": payload['username'],
                "is_online": True
            })
            print(f"🟢 [{payload['username']}] 근무 시작 (모바일 앱 접속)")
        except Exception as e:
            sio.emit('worker_auth_result', {"success": False, "error": str(e)}, to=sid)

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
