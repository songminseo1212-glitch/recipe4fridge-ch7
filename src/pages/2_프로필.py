from datetime import datetime, timezone

import pandas as pd
import psycopg2
import streamlit as st

import db
from auth_ui import current_user, flash, render_sidebar_auth, report_db_error
from recipe import (
    CUISINE_OPTIONS,
    DIFFICULTY_OPTIONS,
    SERVINGS_OPTIONS,
    TIME_OPTIONS,
    list_free_text_models_detailed,
)
from vision import list_free_vision_models_detailed

st.set_page_config(page_title="프로필", page_icon="👤")
render_sidebar_auth()

st.title("👤 프로필")

user = current_user()
if not user:
    st.info(
        "로그인하면 프로필을 볼 수 있어요. "
        "왼쪽 사이드바(모바일은 왼쪽 위 > 버튼)에서 로그인하거나 회원가입해 주세요."
    )
    st.stop()

try:
    saved = db.get_user_by_id(user["id"])
except psycopg2.Error as e:
    report_db_error("프로필 정보를 불러오지 못했어요.", e)
    st.stop()
if not saved:
    st.error("❌ 계정 정보를 찾을 수 없어요. 사이드바에서 로그아웃한 뒤 다시 로그인해 주세요.")
    st.stop()

st.write(f"**이메일**: {user['email']}")

with st.form("profile_form"):
    new_nickname = st.text_input("닉네임", value=user["nickname"])
    submitted = st.form_submit_button("닉네임 저장", type="primary")
    if submitted:
        new_nickname = new_nickname.strip()
        if not new_nickname:
            st.warning("닉네임을 입력해 주세요.")
        elif new_nickname == user["nickname"]:
            st.info("지금 쓰는 닉네임과 같아요.")
        else:
            try:
                db.update_nickname(user["id"], new_nickname)
            except psycopg2.Error as e:
                report_db_error("닉네임을 바꾸지 못했어요.", e)
            else:
                st.session_state.current_user["nickname"] = new_nickname
                # 사이드바 인사말도 새 닉네임으로 바꾸려면 다시 그려야 하므로, 완료 안내는 토스트로 넘긴다.
                flash(f"닉네임을 '{new_nickname}'(으)로 바꿨어요.", icon="✅")
                st.rerun()

st.divider()
st.subheader("🍳 레시피 추천 기본값")
st.caption("여기서 고른 값이 레시피 추천 화면(③)의 처음 선택값이 돼요. 그 화면에서 언제든 바꿀 수 있어요.")


def _index(saved_value, options):
    return options.index(saved_value) if saved_value in options else 0


with st.form("recipe_preference_form"):
    pref_cols = st.columns(4)
    pref_cuisine = pref_cols[0].selectbox(
        "요리 종류", CUISINE_OPTIONS, index=_index(saved.get("default_cuisine"), CUISINE_OPTIONS)
    )
    pref_difficulty = pref_cols[1].selectbox(
        "난이도", DIFFICULTY_OPTIONS, index=_index(saved.get("default_difficulty"), DIFFICULTY_OPTIONS)
    )
    pref_time = pref_cols[2].selectbox(
        "조리 시간", TIME_OPTIONS, index=_index(saved.get("default_time"), TIME_OPTIONS)
    )
    pref_servings = pref_cols[3].selectbox(
        "인원", SERVINGS_OPTIONS, index=_index(saved.get("default_servings"), SERVINGS_OPTIONS)
    )
    pref_submitted = st.form_submit_button("기본값 저장", type="primary")
    if pref_submitted:
        try:
            db.update_recipe_preferences(user["id"], pref_cuisine, pref_difficulty, pref_time, pref_servings)
        except psycopg2.Error as e:
            report_db_error("레시피 추천 기본값을 저장하지 못했어요.", e)
        else:
            st.success("레시피 추천 기본값을 저장했어요. 다음에 레시피 추천 화면을 처음 열 때부터 적용돼요.")

st.divider()
st.subheader("🤖 AI 모델 선택")
st.caption(
    "체크한 모델만 식재료 인식·레시피 추천에 써요. 잘 모르겠다면 그대로 두세요. "
    "하나도 체크하지 않고 저장하면 전체 무료 모델을 써요. "
    "(오픈라우터가 응답 속도·처리량은 공개하지 않아 이름·컨텍스트·출시일만 보여줘요.)"
)


def _relative_release(created_ts):
    if not created_ts:
        return "-"
    try:
        created = datetime.fromtimestamp(created_ts, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "-"
    days = (datetime.now(timezone.utc) - created).days
    if days <= 0:
        return "오늘"
    if days < 30:
        return f"{days}일 전"
    months = days // 30
    return f"{months}개월 전"


def _model_dataframe(entries, selected_ids):
    has_selection = bool(selected_ids)
    selected_set = set(selected_ids or [])
    rows = [
        {
            "선택": (e["id"] in selected_set) if has_selection else True,
            "모델명": e["name"],
            "컨텍스트": e["context_length"],
            "출시일": _relative_release(e["created"]),
        }
        for e in entries
    ]
    return pd.DataFrame(rows, columns=["선택", "모델명", "컨텍스트", "출시일"])


def _selected_ids(entries, edited_df):
    """체크된 행의 모델 id. 표시 이름은 겹칠 수 있어 행 번호(entries 순서와 같음)로 id를 찾는다."""
    return [
        entries[row_index]["id"]
        for row_index, row in edited_df.iterrows()
        if row["선택"] and 0 <= row_index < len(entries)
    ]


with st.spinner("사용할 수 있는 모델 목록을 불러오는 중이에요..."):
    vision_entries = list_free_vision_models_detailed()
    text_entries = list_free_text_models_detailed()
saved_vision_ids = db.parse_model_id_list(saved.get("selected_vision_models"))
saved_text_ids = db.parse_model_id_list(saved.get("selected_text_models"))

st.markdown("**식재료 인식(이미지) 모델**")
if not vision_entries:
    st.warning("지금은 오픈라우터에서 무료 이미지 인식 모델 목록을 가져오지 못했어요. 잠시 후 새로고침해 주세요.")
    edited_vision = None
else:
    edited_vision = st.data_editor(
        _model_dataframe(vision_entries, saved_vision_ids),
        hide_index=True,
        disabled=["모델명", "컨텍스트", "출시일"],
        key="vision_model_editor",
        use_container_width=True,
    )

st.markdown("**레시피 추천 모델**")
if not text_entries:
    st.warning("지금은 오픈라우터에서 레시피 추천용 무료 모델 목록을 가져오지 못했어요. 잠시 후 새로고침해 주세요.")
    edited_text = None
else:
    edited_text = st.data_editor(
        _model_dataframe(text_entries, saved_text_ids),
        hide_index=True,
        disabled=["모델명", "컨텍스트", "출시일"],
        key="text_model_editor",
        use_container_width=True,
    )

if st.button("모델 선택 저장", type="primary"):
    new_vision_ids = _selected_ids(vision_entries, edited_vision) if edited_vision is not None else saved_vision_ids
    new_text_ids = _selected_ids(text_entries, edited_text) if edited_text is not None else saved_text_ids
    try:
        db.update_model_selection(user["id"], new_vision_ids or [], new_text_ids or [])
    except psycopg2.Error as e:
        report_db_error("모델 선택을 저장하지 못했어요.", e)
    else:
        st.success("모델 선택을 저장했어요. 다음 인식·추천부터 적용돼요.")
