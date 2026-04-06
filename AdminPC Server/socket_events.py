from config import device_status, data_queue


def register_events(sio):
    """Socket.IO 이벤트 핸들러를 서버 인스턴스에 등록"""

    @sio.event
    def connect(sid, environ):
        print(f"[{sid}] 클라이언트 연결됨")

    # 🌟 웹 UI에서 '특정 장비 시작 버튼'을 눌렀을 때 들어오는 이벤트
    @sio.on('ui_start_btn')
    def on_ui_start_btn(sid, data):
        target_device = data.get('device_id')
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

        # 장비 상태 실시간 갱신
        device_status[device_id] = {
            "status": body.get('machine_status', 'UNKNOWN')
        }

        # 1) DB 저장을 위해 큐에 데이터 적재 (튜플 형태)
        row_data = (
            device_id,
            header.get('batch_id'),
            body.get('sequence'),
            body.get('machine_status'),
            body.get('test_result'),
            body.get('timestamp')
        )
        data_queue.put(row_data)

        # 2) 모바일 앱 서버로 실시간 데이터 포워딩 (중계 역할)
        sio.emit('mobile_data_feed', data)

        print(f"[{device_id}] {body.get('sequence')}/100 수신 및 큐 적재 완료")

    @sio.on('batch_complete')
    def on_batch_complete(sid, data):
        device_id = data.get('device_id')
        # 검사 완료 → 장비 상태를 STOP으로 변경
        device_status[device_id] = {"status": "STOP"}
        print(f"\n🏁 --- [{device_id}] 100개 검사 완료 --- 🏁\n")

    @sio.event
    def disconnect(sid):
        print(f"[{sid}] 클라이언트 연결 끊김")
