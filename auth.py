import sqlite3
import jwt
import datetime
from functools import wraps
from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from config import USERS_DB_NAME, JWT_SECRET, JWT_EXPIRY_HOURS, ROLE_PERMISSIONS, online_users

auth = Blueprint('auth', __name__)

# ============================================================
# 사용자 DB 초기화 및 헬퍼
# ============================================================

def init_users_db():
    """사용자 테이블 생성 + 기본 Master 계정 시드"""
    conn = sqlite3.connect(USERS_DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'Operator',
            nickname TEXT DEFAULT '',
            emp_id TEXT UNIQUE,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    # nickname 컬럼이 없는 기존 DB 호환 (마이그레이션)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN nickname TEXT DEFAULT ''")
        print("📋 기존 DB에 nickname 컬럼 추가 완료.")
    except sqlite3.OperationalError:
        pass  # 이미 존재하면 무시

    # emp_id 컬럼이 없는 기존 DB 호환 (마이그레이션)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN emp_id TEXT")
        print("📋 기존 DB에 emp_id 컬럼 추가 완료.")
    except sqlite3.OperationalError:
        pass  # 이미 존재하면 무시

    # 스케줄 테이블 생성 (Cascade Delete 적용을 위해 PRAGMA foreign_keys 필요)
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(date, user_id)
        )
    ''')

    # 주요 일정(Calendar Events) 테이블 생성 (작성자 기록 없음 - 익명성)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            content TEXT NOT NULL
        )
    ''')

    # 공지사항(Notices) 테이블 생성
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            is_important INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    # 기본 계정이 없으면 생성 (Master / Technician / Operator 각 1개)
    seed_accounts = [
        ('admin',      'admin1234',    'Master',     '관리자'),
        ('tech1',      'tech1234',     'Technician', '엔지니어1'),
        ('operator1',  'oper1234',     'Operator',   '작업자1'),
    ]

    for username, password, role, nickname in seed_accounts:
        cursor.execute("SELECT COUNT(*) FROM users WHERE username = ?", (username,))
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                "INSERT INTO users (username, password_hash, role, nickname, emp_id) VALUES (?, ?, ?, ?, ?)",
                (username, generate_password_hash(password), role, nickname, f"EMP_{username}")
            )
            print(f"🔑 기본 {role} 계정 생성: {username} / {password} (닉네임: {nickname})")

    conn.commit()
    conn.close()
    print("✅ 사용자 데이터베이스 초기화 완료.")


def get_users_db():
    """사용자 DB 연결 헬퍼"""
    conn = sqlite3.connect(USERS_DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON;") # CASCADE 활성화
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# JWT 토큰 유틸리티
# ============================================================

def create_token(user_id, username, role):
    """JWT 토큰 생성"""
    payload = {
        'user_id': user_id,
        'username': username,
        'role': role,
        'permissions': ROLE_PERMISSIONS.get(role, []),
        'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=JWT_EXPIRY_HOURS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')


def decode_token(token):
    """JWT 토큰 검증 및 디코딩"""
    return jwt.decode(token, JWT_SECRET, algorithms=['HS256'])


# ============================================================
# 권한 체크 데코레이터
# ============================================================

def require_auth(f):
    """로그인 필수 데코레이터 (JWT 토큰 검증)"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization')

        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]

        if not token:
            return jsonify({"error": "토큰이 필요합니다."}), 401

        try:
            payload = decode_token(token)
            request.user = payload
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "토큰이 만료되었습니다."}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "유효하지 않은 토큰입니다."}), 401

        return f(*args, **kwargs)
    return decorated


def require_role(*allowed_roles):
    """특정 역할만 접근 가능한 데코레이터"""
    def decorator(f):
        @wraps(f)
        @require_auth
        def decorated(*args, **kwargs):
            user_role = request.user.get('role')
            if user_role not in allowed_roles:
                return jsonify({"error": f"권한이 없습니다. (필요: {', '.join(allowed_roles)})"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def require_permission(permission):
    """특정 권한(permission)이 있는 역할만 접근 가능한 데코레이터"""
    def decorator(f):
        @wraps(f)
        @require_auth
        def decorated(*args, **kwargs):
            user_permissions = request.user.get('permissions', [])
            if permission not in user_permissions:
                return jsonify({"error": f"권한이 없습니다. (필요: {permission})"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


# ============================================================
# 인증 API
# ============================================================

# POST /api/auth/login
@auth.route('/api/auth/login', methods=['POST'])
def login():
    """로그인 → JWT 토큰 발급"""
    body = request.get_json()
    if not body:
        return jsonify({"error": "요청 본문이 필요합니다."}), 400

    username = body.get('username')
    password = body.get('password')

    if not username or not password:
        return jsonify({"error": "username과 password를 입력해주세요."}), 400

    conn = get_users_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()

    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({"error": "아이디 또는 비밀번호가 틀렸습니다."}), 401

    token = create_token(user['id'], user['username'], user['role'])

    return jsonify({
        "message": "로그인 성공",
        "token": token,
        "user": {
            "id": user['id'],
            "username": user['username'],
            "role": user['role'],
            "nickname": user['nickname'] or user['username'],
            "emp_id": user['emp_id'],
            "permissions": ROLE_PERMISSIONS.get(user['role'], [])
        }
    })


# GET /api/auth/me
@auth.route('/api/auth/me', methods=['GET'])
@require_auth
def get_me():
    """현재 로그인한 사용자 정보 조회"""
    return jsonify({
        "user_id": request.user['user_id'],
        "username": request.user['username'],
        "role": request.user['role'],
        "permissions": request.user['permissions']
    })


# ============================================================
# 사용자 관리 API (Master 전용)
# ============================================================

# GET /api/users
@auth.route('/api/users', methods=['GET'])
@require_role('Master')
def list_users():
    """사용자 목록 조회"""
    conn = get_users_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role, nickname, emp_id, created_at FROM users ORDER BY id")
    users_list = []
    # 각 사용자의 근무 상태(온라인 여부)를 함께 반환
    online_user_ids = {info['user_id'] for info in online_users.values()}
    for row in cursor.fetchall():
        u = dict(row)
        u['nickname'] = u.get('nickname') or u['username']
        u['is_online'] = u['id'] in online_user_ids
        users_list.append(u)
    conn.close()
    users = users_list
    return jsonify(users)


# POST /api/users
@auth.route('/api/users', methods=['POST'])
@require_role('Master')
def create_user():
    """사용자 등록"""
    body = request.get_json()
    username = body.get('username')
    password = body.get('password')
    role = body.get('role', 'Operator')
    nickname = body.get('nickname', '')
    emp_id = body.get('emp_id')

    if not username or not password:
        return jsonify({"error": "username과 password를 입력해주세요."}), 400

    if role not in ROLE_PERMISSIONS:
        return jsonify({"error": f"유효하지 않은 역할입니다. (가능: {', '.join(ROLE_PERMISSIONS.keys())})"}), 400

    conn = get_users_db()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, nickname, emp_id) VALUES (?, ?, ?, ?, ?)",
            (username, generate_password_hash(password), role, nickname, emp_id)
        )
        conn.commit()
        new_id = cursor.lastrowid
    except sqlite3.IntegrityError as e:
        conn.close()
        if "emp_id" in str(e):
            return jsonify({"error": "이미 존재하는 사번입니다."}), 409
        return jsonify({"error": "이미 존재하는 사용자명입니다."}), 409

    conn.close()
    return jsonify({"message": "사용자 등록 완료", "user_id": new_id}), 201


# DELETE /api/users/<id>
@auth.route('/api/users/<int:user_id>', methods=['DELETE'])
@require_role('Master')
def delete_user(user_id):
    """사용자 삭제"""
    conn = get_users_db()
    cursor = conn.cursor()

    cursor.execute("SELECT username, role FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return jsonify({"error": "존재하지 않는 사용자입니다."}), 404

    # Master가 자기 자신을 삭제하는 것 방지
    if user['role'] == 'Master':
        cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'Master'")
        master_count = cursor.fetchone()[0]
        if master_count <= 1:
            conn.close()
            return jsonify({"error": "마지막 Master 계정은 삭제할 수 없습니다."}), 400

    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": f"사용자 '{user['username']}' 삭제 완료"})


# PUT /api/users/<id>
@auth.route('/api/users/<int:user_id>', methods=['PUT'])
@require_role('Master')
def update_user(user_id):
    """사용자 정보 변경 (직급, 닉네임)"""
    body = request.get_json()
    new_role = body.get('role')
    new_nickname = body.get('nickname')
    new_emp_id = body.get('emp_id')

    conn = get_users_db()
    cursor = conn.cursor()

    cursor.execute("SELECT username, role FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return jsonify({"error": "존재하지 않는 사용자입니다."}), 404

    # 직급(Role) 변경 처리
    if new_role:
        if new_role not in ROLE_PERMISSIONS:
            conn.close()
            return jsonify({"error": f"유효하지 않은 역할입니다. (가능: {', '.join(ROLE_PERMISSIONS.keys())})"}), 400

        # 유일한 Master의 권한을 강등시키는 것 방지
        if user['role'] == 'Master' and new_role != 'Master':
            cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'Master'")
            master_count = cursor.fetchone()[0]
            if master_count <= 1:
                conn.close()
                return jsonify({"error": "마지막 Master 계정의 권한은 강등할 수 없습니다."}), 400

        cursor.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))

    # 닉네임 변경 처리
    if new_nickname is not None:
        cursor.execute("UPDATE users SET nickname = ? WHERE id = ?", (new_nickname, user_id))
        
    # 사번 변경 처리
    if new_emp_id is not None:
        try:
            cursor.execute("UPDATE users SET emp_id = ? WHERE id = ?", (new_emp_id, user_id))
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({"error": "이미 존재하는 사번입니다."}), 409

    conn.commit()
    conn.close()

    changes = []
    if new_role:
        changes.append(f"권한→{new_role}")
    if new_nickname is not None:
        changes.append(f"닉네임→{new_nickname}")
    if new_emp_id is not None:
        changes.append(f"사번→{new_emp_id}")

    return jsonify({"message": f"사용자 '{user['username']}' 수정 완료 ({', '.join(changes)})"})


# ============================================================
# 근무 상태(온라인) 조회 API
# ============================================================

# GET /api/workers/status
@auth.route('/api/workers/status', methods=['GET'])
@require_auth
def get_workers_status():
    """전체 사용자의 근무 상태(온라인/오프라인) 조회"""
    conn = get_users_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role, nickname, emp_id FROM users ORDER BY id")

    online_user_ids = {info['user_id'] for info in online_users.values()}
    workers = []
    for row in cursor.fetchall():
        u = dict(row)
        workers.append({
            "id": u['id'],
            "username": u['username'],
            "nickname": u['nickname'] or u['username'],
            "emp_id": u['emp_id'],
            "role": u['role'],
            "is_online": u['id'] in online_user_ids
        })
    conn.close()
    return jsonify(workers)


# ============================================================
# 스케줄 관리 API
# ============================================================

# GET /api/schedules?date=YYYY-MM-DD
@auth.route('/api/schedules', methods=['GET'])
@require_auth
def get_schedules():
    date_str = request.args.get('date')
    if not date_str:
        return jsonify({"error": "date 파라미터가 필요합니다."}), 400

    conn = get_users_db()
    cursor = conn.cursor()
    # 해당 날짜의 스케줄과 사용자 정보 조인
    cursor.execute('''
        SELECT u.id, u.username, u.nickname, u.emp_id, u.role
        FROM schedules s
        JOIN users u ON s.user_id = u.id
        WHERE s.date = ?
    ''', (date_str,))
    
    scheduled_users = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return jsonify(scheduled_users)


# POST /api/schedules
@auth.route('/api/schedules', methods=['POST'])
@require_role('Master', 'Technician')
def set_schedules():
    body = request.get_json()
    date_str = body.get('date')
    user_ids = body.get('user_ids', [])

    if not date_str:
        return jsonify({"error": "date 파라미터가 필요합니다."}), 400

    conn = get_users_db()
    cursor = conn.cursor()
    
    try:
        # 기존 해당 날짜의 스케줄 삭제 후 다시 덮어쓰기
        cursor.execute("DELETE FROM schedules WHERE date = ?", (date_str,))
        
        for uid in user_ids:
            cursor.execute(
                "INSERT INTO schedules (date, user_id) VALUES (?, ?)",
                (date_str, uid)
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"error": f"스케줄 저장 실패: {str(e)}"}), 500
        
    conn.close()
    return jsonify({"message": f"{date_str} 근무 인원 설정 완료."})


# ============================================================
# 주요 일정(Calendar Events) API
# ============================================================

# GET /api/events
@auth.route('/api/events', methods=['GET'])
@require_auth
def get_events():
    """주요 일정 조회 (날짜별 또는 월별)"""
    date_str = request.args.get('date')
    month_str = request.args.get('month') # YYYY-MM 형식
    
    conn = get_users_db()
    cursor = conn.cursor()
    
    if date_str:
        cursor.execute("SELECT id, date, content FROM calendar_events WHERE date = ? ORDER BY id", (date_str,))
    elif month_str:
        cursor.execute("SELECT id, date, content FROM calendar_events WHERE date LIKE ? ORDER BY date, id", (f"{month_str}-%",))
    else:
        # 파라미터 없으면 최근 데이터 100개만 반환
        cursor.execute("SELECT id, date, content FROM calendar_events ORDER BY date DESC LIMIT 100")
        
    events = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(events)


# POST /api/events
@auth.route('/api/events', methods=['POST'])
@require_role('Master', 'Technician')
def add_event():
    """특정 날짜에 주요 일정 등록"""
    body = request.get_json()
    date_str = body.get('date')
    content = body.get('content')

    if not date_str or not content:
        return jsonify({"error": "date와 content 파라미터가 필요합니다."}), 400

    conn = get_users_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO calendar_events (date, content) VALUES (?, ?)", (date_str, content))
        conn.commit()
        new_id = cursor.lastrowid
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"error": f"일정 저장 실패: {str(e)}"}), 500
        
    conn.close()
    return jsonify({"message": "일정 등록 완료", "id": new_id, "date": date_str, "content": content}), 201


# DELETE /api/events/<id>
@auth.route('/api/events/<int:event_id>', methods=['DELETE'])
@require_role('Master', 'Technician')
def delete_event(event_id):
    """주요 일정 삭제"""
    conn = get_users_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM calendar_events WHERE id = ?", (event_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({"error": "존재하지 않는 일정입니다."}), 404
        
    cursor.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "일정 삭제 완료"})


# ============================================================
# 공지사항(Notices) API
# ============================================================

# GET /api/notices
@auth.route('/api/notices', methods=['GET'])
@require_auth
def get_notices():
    """공지사항 목록 조회 (최신순)"""
    conn = get_users_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, content, is_important, created_at FROM notices ORDER BY created_at DESC")
    notices = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(notices)


# POST /api/notices
@auth.route('/api/notices', methods=['POST'])
@require_role('Master', 'Technician')
def add_notice():
    """공지사항 등록"""
    body = request.get_json()
    title = body.get('title')
    content = body.get('content')
    is_important = 1 if body.get('is_important') else 0

    if not title or not content:
        return jsonify({"error": "title과 content 파라미터가 필요합니다."}), 400

    conn = get_users_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO notices (title, content, is_important) VALUES (?, ?, ?)",
            (title, content, is_important)
        )
        conn.commit()
        new_id = cursor.lastrowid
        # 방금 삽입한 행의 created_at을 가져오기
        cursor.execute("SELECT created_at FROM notices WHERE id = ?", (new_id,))
        created_at = cursor.fetchone()['created_at']
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"error": f"공지 저장 실패: {str(e)}"}), 500

    conn.close()
    return jsonify({
        "message": "공지사항 등록 완료",
        "id": new_id,
        "title": title,
        "content": content,
        "is_important": is_important,
        "created_at": created_at
    }), 201


# DELETE /api/notices/<id>
@auth.route('/api/notices/<int:notice_id>', methods=['DELETE'])
@require_role('Master', 'Technician')
def delete_notice(notice_id):
    """공지사항 삭제"""
    conn = get_users_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM notices WHERE id = ?", (notice_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({"error": "존재하지 않는 공지사항입니다."}), 404

    cursor.execute("DELETE FROM notices WHERE id = ?", (notice_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "공지사항 삭제 완료"})

