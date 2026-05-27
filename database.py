import sqlite3
import json
import time
from config import DB_NAME, data_queue

# ── 배치 커밋 설정 ──
BATCH_SIZE = 50          # 최대 50건씩 모아서 커밋
FLUSH_INTERVAL = 0.3     # 큐가 비어도 0.3초마다 강제 커밋 (데이터 유실 방지)


def init_db():
    """테이블이 없으면 생성하는 초기화 함수"""
    conn = sqlite3.connect(DB_NAME, timeout=10)
    cursor = conn.cursor()

    # 🚀 성능 최적화: WAL 모드 활성화 (읽기/쓰기 병렬 처리)
    cursor.execute('PRAGMA journal_mode=WAL;')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            batch_id TEXT,
            model_name TEXT,
            sequence INTEGER,
            machine_status TEXT,
            status_info TEXT,
            vision_result TEXT,
            vision_result_code TEXT,
            defect_type TEXT,
            sensor_data TEXT,
            timestamp TEXT
        )
    ''')
    # 성능 최적화: 자주 조회/집계되는 컬럼에 인덱스 추가
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_vision_result ON logs(vision_result_code)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_device_id ON logs(device_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_machine_status ON logs(machine_status)')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            model_name TEXT NOT NULL,
            manager_username TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    ''')

    # 기존 DB 호환을 위한 마이그레이션
    try:
        cursor.execute("ALTER TABLE devices ADD COLUMN manager_username TEXT")
        print("📋 기존 devices 테이블에 manager_username 컬럼 추가 완료.")
    except sqlite3.OperationalError:
        pass  # 컬럼이 이미 존재하면 무시

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS resolve_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            resolved_by TEXT NOT NULL,
            resolved_at TEXT DEFAULT (datetime('now','localtime'))
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_resolve_logs_device ON resolve_logs(device_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_resolve_logs_time ON resolve_logs(resolved_at)')

    # 시드 데이터 삽입: devices 테이블이 비어있는 경우 기본 장비 5대 추가
    cursor.execute('SELECT COUNT(*) FROM devices')
    if cursor.fetchone()[0] == 0:
        seed_devices = [
            ('RASP_PI_01', '비전검사 장비 #1', 'SMT_CHIP_A20'),
            ('RASP_PI_02', '비전검사 장비 #2', 'SMT_CHIP_A20'),
            ('RASP_PI_03', '비전검사 장비 #3', 'SMT_CHIP_B15'),
            ('RASP_PI_04', '비전검사 장비 #4', 'SMT_CHIP_B15'),
            ('RASP_PI_05', '비전검사 장비 #5', 'SMT_CHIP_C10')
        ]
        cursor.executemany('''
            INSERT INTO devices (device_id, name, model_name)
            VALUES (?, ?, ?)
        ''', seed_devices)
        print("🌱 기본 장비 5대가 DB에 추가되었습니다.")

    conn.commit()
    conn.close()
    print("✅ 데이터베이스 초기화 완료. (WAL 모드 활성화)")


def get_db():
    """REST API 읽기용 DB 연결 헬퍼 (딕셔너리 형태 반환)"""
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def db_worker():
    """큐에서 데이터를 꺼내 DB에 저장하는 백그라운드 워커 (배치 커밋)"""
    conn = sqlite3.connect(DB_NAME, check_same_thread=False, timeout=10)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')

    INSERT_SQL = '''
        INSERT INTO logs (
            device_id, batch_id, model_name, sequence, machine_status,
            status_info, vision_result, vision_result_code, defect_type,
            sensor_data, timestamp
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    '''

    batch = []              # 배치 버퍼
    last_flush = time.time()

    while True:
        # 큐에서 데이터를 꺼냄 (최대 FLUSH_INTERVAL초 대기)
        try:
            data = data_queue.get(timeout=FLUSH_INTERVAL)
            batch.append(data)
            data_queue.task_done()
        except Exception:
            pass  # 타임아웃: 큐가 비어있으면 아래에서 시간 기반 플러시 수행

        now = time.time()
        elapsed = now - last_flush

        # 🚀 배치 크기 도달 또는 시간 초과 시 한 번에 커밋
        if batch and (len(batch) >= BATCH_SIZE or elapsed >= FLUSH_INTERVAL):
            cursor.executemany(INSERT_SQL, batch)
            conn.commit()
            batch.clear()
            last_flush = now
