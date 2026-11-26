from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
import streamlit as st

import db
from auth_ui import current_user, flash, render_sidebar_auth, report_db_error

st.set_page_config(page_title="내 레시피", page_icon="📖")
render_sidebar_auth()

st.title("📖 내 레시피")


def _saved_at_text(raw):
    """저장 시각(ISO 문자열, UTC)을 한국 시각으로 읽기 쉽게 바꾼다. 읽을 수 없으면 원문 그대로."""
    try:
        moment = datetime.fromisoformat(str(raw)).astimezone(ZoneInfo("Asia/Seoul"))
    except (TypeError, ValueError):
        return str(raw or "")
    half = "오전" if moment.hour < 12 else "오후"
    return f"{moment.year}년 {moment.month}월 {moment.day}일 {half} {moment.hour % 12 or 12}:{moment.minute:02d}"


def _cook_time_text(minutes):
    return f"⏱ 약 {minutes}분" if isinstance(minutes, int) and minutes > 0 else "⏱ 조리 시간 정보 없음"


def _join(items):
    return ", ".join(str(item) for item in items or [])


user = current_user()
if not user:
    st.info(
        "로그인하면 저장한 레시피를 볼 수 있어요. "
        "왼쪽 사이드바(모바일은 왼쪽 위 > 버튼)에서 로그인하거나 회원가입해 주세요."
    )
    st.stop()

try:
    recipes = db.list_recipes(user["id"])
except psycopg2.Error as e:
    report_db_error("저장한 레시피를 불러오지 못했어요.", e)
    st.stop()

if not recipes:
    st.info(
        "아직 저장한 레시피가 없어요. 냉장고 사진으로 레시피를 추천받은 뒤, "
        "레시피 상세에서 '💾 레시피 저장'을 누르면 여기에 모여요."
    )
    st.page_link("app.py", label="레시피 추천받으러 가기", icon="🥬")
else:
    st.caption(f"저장한 레시피 {len(recipes)}개 · 최근에 저장한 순서")
    # 삭제 실패 안내가 목록 맨 아래가 아니라 눈에 띄는 위쪽에 나오도록 자리를 먼저 잡아 둔다.
    feedback = st.container()
    delete_target = None
    for recipe in recipes:
        name = recipe.get("name") or "이름 없는 요리"
        with st.container(border=True):
            col_main, col_delete = st.columns([5, 1])
            with col_main:
                st.markdown(f"### {name}")
                st.caption(f"저장: {_saved_at_text(recipe.get('saved_at'))}")
                st.write(_cook_time_text(recipe.get("cook_time_minutes")))
                st.write("**사용 재료**: " + (_join(recipe["used_ingredients"]) or "정보 없음"))
                if recipe["missing_ingredients"]:
                    st.write("**더 필요한 재료**: " + _join(recipe["missing_ingredients"]))
                with st.expander("조리 순서 보기"):
                    if recipe["steps"]:
                        st.markdown(
                            "\n".join(f"{i}. {' '.join(str(step).split())}" for i, step in enumerate(recipe["steps"], start=1))
                        )
                    else:
                        st.write("저장된 조리 순서가 없어요.")
            with col_delete:
                # 삭제는 되돌릴 수 없으므로 한 번 더 확인을 거친 뒤 삭제한다.
                with st.popover("삭제"):
                    st.write(f"'{name}' 레시피를 삭제할까요? 삭제하면 되돌릴 수 없어요.")
                    if st.button("네, 삭제할게요", key=f"delete_saved_{recipe['id']}", type="primary"):
                        delete_target = (recipe["id"], name)

    if delete_target is not None:
        recipe_id, name = delete_target
        try:
            db.delete_recipe(user["id"], recipe_id)
        except psycopg2.Error as e:
            report_db_error(f"'{name}' 레시피를 삭제하지 못했어요.", e, container=feedback)
        else:
            flash(f"'{name}' 레시피를 삭제했어요.", icon="🗑️")
            st.rerun()
