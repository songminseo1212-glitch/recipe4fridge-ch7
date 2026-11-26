import logging

import psycopg2
import streamlit as st

import auth
import db

logger = logging.getLogger(__name__)

db.init_db()

# app.py의 진행 상태 키. "처음부터" 버튼과 로그아웃에서 함께 쓰는 단일 목록이다.
# 로그아웃 시 이전 사용자의 화면(재료 목록/레시피/사용 모델 등)이 남지 않도록 모두 지운다.
# app.py에서 새 session_state 키를 추가하면 여기만 갱신하면 된다.
APP_FLOW_STATE_KEYS = [
    "wizard_step",
    "uploaded_photo_bytes",
    "uploaded_photo_name",
    "ingredients",
    "recognition_error",
    "recognition_error_detail",
    "recognition_error_level",
    "recognition_photo_id",
    "recognize_requested",
    "used_vision_model",
    "used_recipe_model",
    "recipes",
    "recipes_not_korean",
    "recipe_error",
    "recipe_error_detail",
    "recipe_requested",
    "selected_recipe",
    "last_recipe_request_time",
    "recipe_cuisine",
    "recipe_difficulty",
    "recipe_time",
    "recipe_servings",
]

DB_ERROR_HINT = "잠시 후 다시 시도해 주세요. 계속되면 관리자에게 문의해 주세요."


def current_user():
    return st.session_state.get("current_user")


def reset_app_flow_state():
    """진행 상태를 모두 지우고 사진 업로더도 비운다 (업로더는 key를 바꿔야 비워진다)."""
    for key in APP_FLOW_STATE_KEYS:
        st.session_state.pop(key, None)
    st.session_state.uploader_key = st.session_state.get("uploader_key", 0) + 1


def flash(message, icon=None):
    """st.rerun() 뒤에도 사라지지 않도록 다음 실행에서 토스트로 보여줄 메시지를 남긴다."""
    st.session_state.setdefault("_flash_messages", []).append((message, icon))


def _show_flash():
    for message, icon in st.session_state.pop("_flash_messages", []):
        st.toast(message, icon=icon)


def report_db_error(failure, error, container=st):
    """DB 오류를 사용자용 문구로 보여주고, 개발자용 정보는 서버 로그에 남긴다.

    failure: 무엇을 못 했는지 알려주는 문장 (예: "레시피를 저장하지 못했어요.")
    psycopg2 오류 문구에는 접속 호스트 등 연결 정보가 섞일 수 있어 화면과 로그에는
    오류 종류와 SQLSTATE 코드만 남긴다.
    """
    logger.error("DB 오류 (%s): %s pgcode=%s", failure, type(error).__name__, getattr(error, "pgcode", None))
    container.error(f"❌ {failure} 데이터 서버와 통신하는 중 문제가 생겼어요. {DB_ERROR_HINT}")
    with container.expander("자세한 정보 (개발자용)"):
        st.code(f"{type(error).__name__} (SQLSTATE: {getattr(error, 'pgcode', None) or '없음'})")


def _sync_google_login():
    """Google 로그인 리다이렉트 후 돌아왔을 때, st.user 정보를 우리 DB/세션과 연결한다."""
    if current_user() is not None:
        return
    if not getattr(st.user, "is_logged_in", False):
        return

    info = st.user.to_dict()
    email = info.get("email")
    if not email:
        return
    nickname = info.get("name") or email.split("@")[0]
    try:
        google_user = db.get_or_create_google_user(email, nickname)
    except psycopg2.Error as e:
        report_db_error("Google 계정 정보를 불러오지 못했어요.", e, container=st.sidebar)
        return
    st.session_state.current_user = {
        "id": google_user["id"],
        "email": google_user["email"],
        "nickname": google_user["nickname"],
        "via_google": True,
    }


def render_sidebar_auth():
    """사이드바에 로그인 상태/로그인·회원가입 폼을 그린다. 모든 페이지에서 공통으로 호출한다."""
    _show_flash()
    _sync_google_login()
    # 모바일 터치 타겟 권장 크기(44px) 확보. 모든 페이지에서 공통으로 적용되도록 여기서 주입한다.
    st.markdown(
        """
        <style>
        [data-testid="stBaseButton-secondary"],
        [data-testid="stBaseButton-secondaryFormSubmit"],
        [data-testid="stBaseButton-primary"],
        [data-testid="stBaseButton-primaryFormSubmit"] {
            min-height: 44px;
        }
        [data-testid="stNumberInputStepUp"],
        [data-testid="stNumberInputStepDown"] {
            min-width: 44px;
            min-height: 44px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.sidebar:
        user = current_user()
        if user:
            st.markdown(f"👋 **{user['nickname']}**님")
            st.caption(user["email"])
            if st.button("로그아웃", key="logout_button"):
                # Google 로그아웃도 st.logout()이 세션을 새로 시작하지만, 그 전에 이전 사용자의
                # 화면 상태가 남지 않도록 직접 한 번 더 지운다.
                st.session_state.pop("current_user", None)
                reset_app_flow_state()
                if user.get("via_google"):
                    st.logout()
                else:
                    flash("로그아웃했어요.", icon="👋")
                    st.rerun()
        else:
            st.subheader("로그인 / 회원가입")
            st.caption(
                "로그인하지 않아도 사진 인식과 레시피 추천은 쓸 수 있어요. "
                "로그인하면 마음에 드는 레시피를 저장하고 '내 레시피'에서 다시 볼 수 있어요."
            )

            if st.button("Google로 로그인", key="google_login_button"):
                try:
                    st.login()
                except Exception as e:
                    st.error("Google 로그인을 지금 쓸 수 없어요. 아래에서 이메일로 로그인하거나 잠시 후 다시 시도해 주세요.")
                    with st.expander("자세한 정보 (개발자용)"):
                        st.code(f"{type(e).__name__}: {e}")

            st.divider()

            tab_login, tab_signup = st.tabs(["로그인", "회원가입"])

            with tab_login:
                with st.form("login_form"):
                    login_email = st.text_input("이메일", key="login_email", placeholder="you@example.com")
                    login_password = st.text_input("비밀번호", type="password", key="login_password")
                    submitted = st.form_submit_button("로그인", type="primary")
                    if submitted:
                        if not login_email.strip() or not login_password:
                            st.warning("이메일과 비밀번호를 모두 입력해 주세요.")
                        else:
                            try:
                                ok, result = auth.login(login_email, login_password)
                            except psycopg2.Error as e:
                                report_db_error("로그인하지 못했어요.", e)
                            else:
                                if ok:
                                    st.session_state.current_user = result
                                    flash(f"{result['nickname']}님, 반가워요!", icon="👋")
                                    st.rerun()
                                else:
                                    st.error(result)

            with tab_signup:
                with st.form("signup_form"):
                    signup_email = st.text_input("이메일", key="signup_email", placeholder="you@example.com")
                    signup_nickname = st.text_input(
                        "닉네임", key="signup_nickname", placeholder="화면에 표시될 이름"
                    )
                    signup_password = st.text_input(
                        "비밀번호", type="password", key="signup_password", help="8자 이상으로 입력해 주세요."
                    )
                    submitted = st.form_submit_button("가입하기", type="primary")
                    if submitted:
                        try:
                            ok, result = auth.signup(signup_email, signup_password, signup_nickname)
                        except psycopg2.Error as e:
                            report_db_error("회원가입을 마치지 못했어요.", e)
                        else:
                            if ok:
                                st.session_state.current_user = result
                                flash(f"회원가입을 마쳤어요. {result['nickname']}님, 환영해요!", icon="🎉")
                                st.rerun()
                            else:
                                st.error(result)
