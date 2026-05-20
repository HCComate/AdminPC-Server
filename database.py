import sqlite3
import json
from config import DB_NAME, data_queue


def init_db():
    """테이블이 없으면 생성하는 초기화 함수"""
    conn = sqlite3.connect(DB_NAME, timeout=10)
    cursor = conn.cursor()
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

    conn.commit()
    conn.close()
    print("✅ 데이터베이스 초기화 완료.")


def get_db():
    """REST API 읽기용 DB 연결 헬퍼 (딕셔너리 형태 반환)"""
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def db_worker():
    """큐에서 데이터를 꺼내 DB에 저장하는 백그라운드 워커"""
    conn = sqlite3.connect(DB_NAME, check_same_thread=False, timeout=10)
    cursor = conn.cursor()

    while True:
        data = data_queue.get()

        cursor.execute('''
            INSERT INTO logs (
                device_id, batch_id, model_name, sequence, machine_status,
                status_info, vision_result, vision_result_code, defect_type,
                sensor_data, timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', data)
        conn.commit()

        data_queue.task_done()
