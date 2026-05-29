# HCComate - AdminPC Server (Backend)

이 디렉토리는 HCComate 플랫폼의 중심 백엔드 서버가 위치한 곳입니다. 프론트엔드 웹 대시보드, 모바일 앱, 그리고 라즈베리 파이 기반 비전 장비들 사이에서 발생하는 모든 상태와 데이터를 관장합니다.

## 🛠 주요 기술 및 특징

- **Framework**: Flask (REST API) + Flask-SocketIO (WebSocket)
- **Concurrency**: Eventlet 기반 비동기 I/O 처리
- **Database**: SQLite3 (동시성 향상을 위한 `WAL` 저널링 모드 적용)
- **Authentication**: JWT 토큰 기반 (Role 기반 접근 제어: MASTER, TECHNICIAN, OPERATOR)

## ✨ 핵심 기능

1. **소켓 이벤트 브로드캐스팅 (`socket_events.py`)**:
   - `client_pi`가 전송하는 검사 로그를 수신하고, 연결된 웹/모바일 클라이언트들에게 실시간으로 브로드캐스팅합니다.
   - 장비의 전원 ON/OFF, 연속 가동 제어, 장비 잠금 해제(`unlock_device`) 기능을 중계합니다.
   - **자동 IDLE 타이머**: 장비 전원이 켜지거나 연속 가동이 멈춘 직후 `STANDBY` 상태에 진입하며, 비동기 스레드 타이머(`eventlet.spawn`)를 구동해 장비별 대기 시간이 초과되면 자동으로 `IDLE`로 전환합니다.
2. **에스컬레이션 엔진**:
   - 에러 발생 시 지정된 담당자의 모바일 기기로 푸시 알림 이벤트를 발송하며, 응답 지연 시 `MASTER` 권한자에게 이관(Escalate)하는 타이머 기반 큐 시스템을 포함합니다.
3. **배치 기반 데이터베이스 커밋 (`database.py`)**:
   - 라즈베리 파이에서 쏟아지는 초당 수십 건의 로그 데이터를 메인 스레드 블로킹 없이 처리하기 위해 Queue와 백그라운드 워커를 사용한 일괄(Batch) INSERT 트랜잭션을 적용했습니다.

## 🚀 실행 방법

```bash
# 의존성 패키지 설치
pip install -r requirements.txt

# 서버 구동 (기본 포트: 5000)
python main.py
```
