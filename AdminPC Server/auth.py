import sqlite3
import jwt
import datetime
from functools import wraps
from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from config import USERS_DB_NAME, JWT_SECRET, JWT_EXPIRY_HOURS, ROLE_PERMISSIONS

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
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    # 기본 Master 계정이 없으면 생성
    cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'Master'")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ('admin', generate_password_hash('admin1234'), 'Master')
        )
        print("🔑 기본 Master 계정 생성: admin / admin1234")

    conn.commit()
    conn.close()
    print("✅ 사용자 데이터베이스 초기화 완료.")


def get_users_db():
    """사용자 DB 연결 헬퍼"""
    conn = sqlite3.connect(USERS_DB_NAME)
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
    cursor.execute("SELECT id, username, role, created_at FROM users ORDER BY id")
    users = [dict(row) for row in cursor.fetchall()]
    conn.close()
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

    if not username or not password:
        return jsonify({"error": "username과 password를 입력해주세요."}), 400

    if role not in ROLE_PERMISSIONS:
        return jsonify({"error": f"유효하지 않은 역할입니다. (가능: {', '.join(ROLE_PERMISSIONS.keys())})"}), 400

    conn = get_users_db()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), role)
        )
        conn.commit()
        new_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
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
