import sqlite3
from config import DB_NAME, data_queue


def init_db():
    """테이블이 없으면 생성하는 초기화 함수"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            batch_id TEXT,
            sequence INTEGER,
            machine_status TEXT,
            test_result TEXT,
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()
    print("✅ 데이터베이스 초기화 완료.")


def get_db():
    """REST API 읽기용 DB 연결 헬퍼 (딕셔너리 형태 반환)"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def db_worker():
    """큐에서 데이터를 꺼내 DB에 저장하는 백그라운드 워커"""
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()

    while True:
        data = data_queue.get()

        cursor.execute('''
            INSERT INTO logs (device_id, batch_id, sequence, machine_status, test_result, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', data)
        conn.commit()

        data_queue.task_done()
