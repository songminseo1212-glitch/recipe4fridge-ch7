import re

import bcrypt

import db

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

GENERIC_LOGIN_ERROR = "이메일 또는 비밀번호가 맞지 않아요. 다시 확인해 주세요."


def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password, password_hash):
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def signup(email, password, nickname):
    """성공 시 (True, user_dict), 실패 시 (False, 에러메시지)."""
    email = email.strip().lower()
    nickname = nickname.strip()

    if not EMAIL_PATTERN.match(email):
        return False, "이메일 형식이 올바르지 않아요. 예: you@example.com"
    if len(password) < 8:
        return False, "비밀번호는 8자 이상으로 입력해 주세요."
    if not nickname:
        return False, "닉네임을 입력해 주세요."
    if db.email_exists(email):
        return False, "이미 가입된 이메일이에요. 로그인 탭에서 로그인해 주세요."

    user_id = db.create_user(email, hash_password(password), nickname)
    return True, {"id": user_id, "email": email, "nickname": nickname}


def login(email, password):
    """성공 시 (True, user_dict), 실패 시 (False, 에러메시지).

    보안: 이메일 존재 여부와 비밀번호 오류를 구분하지 않고 동일한 에러 메시지를 반환한다.
    """
    email = email.strip().lower()
    user = db.get_user_by_email(email)
    # user["password_hash"]가 없으면 Google 등 소셜 로그인 전용 계정이다.
    if not user or not user["password_hash"] or not verify_password(password, user["password_hash"]):
        return False, GENERIC_LOGIN_ERROR

    return True, {"id": user["id"], "email": user["email"], "nickname": user["nickname"]}
