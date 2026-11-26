import html
import io
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
import requests
import streamlit as st
from dotenv import load_dotenv

import db
import openrouter
from auth_ui import (
    current_user,
    flash,
    render_sidebar_auth,
    report_db_error,
    reset_app_flow_state,
)
from recipe import (
    CUISINE_OPTIONS,
    DIFFICULTY_OPTIONS,
    SERVINGS_OPTIONS,
    TIME_OPTIONS,
    generate_recipes,
    is_korean_enough,
    parse_recipes,
)
from vision import (
    MAX_IMAGE_DIMENSION,
    MAX_QUANTITY,
    UNIT_OPTIONS,
    InvalidImageError,
    is_empty_result,
    make_preview_bytes,
    merge_duplicate_ingredients,
    parse_ingredients,
    recognize_ingredients,
    to_resized_data_uri,
)

logger = logging.getLogger(__name__)

load_dotenv()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
RECIPE_REQUEST_COOLDOWN_SECONDS = 5
WIZARD_STEP_LABELS = ["① 사진 업로드", "② 식재료 확인", "③ 레시피 추천"]

# "처음부터" 버튼/로그아웃 시 초기화할 진행 상태 키는 auth_ui.APP_FLOW_STATE_KEYS 한 곳에서 관리한다.
# 새 session_state 키를 추가하면 그 목록을 갱신해야 한다.
SIDEBAR_HINT = "왼쪽 사이드바(모바일은 왼쪽 위 > 버튼)"


def rate_limit_message(response, action):
    """429 응답을 계정 일일 한도와 모델 순간 혼잡으로 구분해 안내 문구를 만든다.

    둘은 사용자가 할 일이 다르다. 일일 한도는 초기화될 때까지 다시 눌러도 소용없고,
    순간 혼잡은 잠시 뒤 다시 시도하면 대체로 풀린다.
    """
    if not openrouter.is_daily_limit(response):
        return (
            f"❌ {action} 실패: 무료 AI 모델들이 모두 붐비고 있어요 (429). "
            "1~2분 뒤에 다시 시도해 주세요."
        )

    metadata = openrouter.error_info(response).get("metadata")
    headers = metadata.get("headers") if isinstance(metadata, dict) else None
    if not isinstance(headers, dict):
        headers = {}
    limit = headers.get("X-RateLimit-Limit")
    quota = f"오늘 쓸 수 있는 무료 AI 호출 {limit}회를" if limit else "오늘 쓸 수 있는 무료 AI 호출을"
    when = _format_reset_time(headers.get("X-RateLimit-Reset"))
    return (
        f"❌ {action} 실패: {quota} 모두 썼어요. "
        f"{when} 다시 쓸 수 있고, 그 전에는 다시 시도해도 같은 오류가 나요."
    )


def _format_reset_time(reset_ms):
    """한도 초기화 시각(에포크 밀리초)을 한국 시각 문구로 바꾼다.

    배포 환경(Streamlit Cloud)의 시간대가 UTC이므로 한국 시각으로 명시해 변환한다.
    """
    try:
        moment = datetime.fromtimestamp(int(reset_ms) / 1000, ZoneInfo("Asia/Seoul"))
    except (TypeError, ValueError, OSError, OverflowError):
        return "잠시 후"
    half = "오전" if moment.hour < 12 else "오후"
    hour = moment.hour % 12 or 12
    return f"{moment.month}월 {moment.day}일 {half} {hour}시에"


def interpret_model_response(response, action, parse, parse_failure_message):
    """인식/레시피 공통: 최종 응답을 (결과, 오류 문구, 기술 정보)로 해석한다.

    성공이면 (parse 결과, None, None), 실패면 (None, 오류 문구, 기술 정보 또는 None).
    """
    if response is None:
        return None, f"❌ {action} 실패: AI 모델의 응답을 받지 못했어요. 잠시 후 다시 시도해 주세요.", None
    status = response.status_code
    if status == 429:
        return None, rate_limit_message(response, action), response.text
    if status == 401:
        return (
            None,
            f"❌ {action} 실패: AI 서비스 인증에 실패했어요 (API 키 문제). 관리자에게 문의해 주세요.",
            response.text,
        )
    if status == 402:
        return (
            None,
            f"❌ {action} 실패: AI 서비스 이용 크레딧이 부족해요. 관리자에게 문의해 주세요.",
            response.text,
        )
    if status >= 500:
        return (
            None,
            f"❌ {action} 실패: AI 모델 제공 서버에 일시적인 문제가 생겼어요 ({status}). 잠시 후 다시 시도해 주세요.",
            response.text,
        )
    if status != 200:
        return (
            None,
            f"❌ {action} 실패: AI 모델이 요청을 처리하지 못했어요 (오류 코드 {status}). 잠시 후 다시 시도해 주세요.",
            response.text,
        )
    content = openrouter.extract_content(response)
    if content is None:
        return (
            None,
            f"❌ {action} 실패: AI 모델이 빈 응답을 보냈어요. 잠시 후 다시 시도해 주세요.",
            response.text,
        )
    parsed = parse(content)
    if not parsed:
        return None, parse_failure_message, content
    return parsed, None, None


@st.cache_data(max_entries=8, ttl=1800, show_spinner=False)
def photo_preview(raw_bytes):
    """업로드 사진의 화면 표시용 바이트(최대 폭 1460px). 같은 사진이면 rerun마다 다시 만들지 않는다.

    InvalidImageError는 캐시되지 않고 그대로 올라간다.
    """
    return make_preview_bytes(raw_bytes)


def get_api_key():
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    try:
        return st.secrets["OPENROUTER_API_KEY"]
    except (FileNotFoundError, KeyError):
        st.error(
            "AI 서비스 설정(OPENROUTER_API_KEY)이 없어 앱을 시작할 수 없어요. "
            "관리자라면 .env 또는 Streamlit secrets를 확인해 주세요."
        )
        st.stop()


def network_error_message(action, error):
    """요청 예외를 (사용자 문구, 개발자용 정보)로 바꾼다. API 키는 헤더에만 있어 예외 문구에 섞이지 않는다."""
    if isinstance(error, requests.exceptions.Timeout):
        message = (
            f"❌ {action} 실패: AI 모델이 제시간에 답하지 않았어요 (서로 다른 모델을 차례로 시도했어요). "
            "잠시 후 다시 시도해 주세요."
        )
    else:
        message = f"❌ {action} 실패: AI 서비스에 연결하지 못했어요. 인터넷 연결을 확인한 뒤 다시 시도해 주세요."
    return message, f"{type(error).__name__}: {error}"


def load_user_profile():
    """로그인한 사용자의 DB 행. 로그인하지 않았거나 DB를 읽지 못하면 None.

    프로필은 기본값·모델 선택에만 쓰이므로, 읽지 못해도 기본 설정으로 계속 진행한다(로그에는 남긴다).
    """
    user = current_user()
    if not user:
        return None
    try:
        return db.get_user_by_id(user["id"])
    except psycopg2.Error as e:
        logger.error("프로필 조회 실패: %s pgcode=%s", type(e).__name__, getattr(e, "pgcode", None))
        st.toast("프로필 설정을 불러오지 못해 기본 설정으로 진행해요.", icon="⚠️")
        return None


def get_allowed_models(field_name):
    """로그인한 사용자가 프로필에서 선택해둔 모델 목록을 읽어온다. 없으면 None(전체 사용)."""
    saved = load_user_profile()
    if not saved:
        return None
    return db.parse_model_id_list(saved.get(field_name))


def cook_time_text(recipe):
    minutes = recipe.get("cook_time_minutes")
    return f"⏱ 약 {minutes}분" if isinstance(minutes, int) and minutes > 0 else "⏱ 조리 시간 정보 없음"


def safe_quantity(value):
    """수량 입력칸(0~MAX_QUANTITY)에 넣을 수 있는 정수로 바꾼다. 읽을 수 없으면 1."""
    try:
        quantity = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 1
    return min(max(quantity, 0), MAX_QUANTITY)


def _forget_widget_state(item_id, fields=("qty",)):
    """목록에서 사라지는 재료 줄의 위젯 상태를 지운다."""
    for field in fields:
        st.session_state.pop(f"ingredient_{field}_{item_id}", None)


def _push_quantity_to_widget(item):
    """코드에서 바꾼 수량을 입력칸에 반영한다.

    key가 있는 위젯은 value 인자가 바뀌어도 같은 위젯으로 취급되어 브라우저가 이전 값을 계속 보여준다.
    session_state에서 키를 지우는 것만으로는 화면이 바뀌지 않으므로, 새 값을 직접 대입해
    Streamlit이 그 값을 브라우저로 보내게 한다. 단, 이미 그려진 위젯의 상태는 같은 실행 안에서
    바꿀 수 없으므로 여기서는 예약만 하고, 다음 실행에서 위젯을 만들기 직전에 대입한다.
    """
    st.session_state.setdefault("_pending_qty", {})[item["_id"]] = safe_quantity(item["quantity"])


def render_ingredient_editor():
    """② 식재료 확인 섹션. 2단계와 3단계 화면에서 공통으로 사용한다.

    재료 한 개당 한 줄(이름/수량/단위/삭제)로 배치한다 (추가 폼과 동일한 형태).
    """
    st.subheader("② 식재료 확인")
    st.caption(
        "⚠️ AI 인식 결과는 틀릴 수 있어요. 각 줄의 이름·수량·단위를 확인하고 필요하면 고쳐 주세요. "
        "빠진 재료는 맨 아래 칸에서 추가할 수 있어요."
    )

    ingredients = st.session_state.get("ingredients", [])
    if not ingredients:
        st.info("아직 재료가 없어요. 맨 아래 칸에 재료 이름을 적고 '추가'를 눌러 주세요.")

    delete_index = None
    for i, item in enumerate(ingredients):
        item.setdefault("unit", "개")
        # 위젯 key는 배열 인덱스가 아니라 재료별 고유 id를 써야 한다. 인덱스를 쓰면
        # 항목 삭제로 뒤쪽 재료들의 인덱스가 당겨질 때 Streamlit이 그 위젯들의
        # 이전(다른 재료) 값을 그대로 들고 있어서, 중간 삭제 시 엉뚱한(마지막) 항목이
        # 사라진 것처럼 보이는 문제가 있었다.
        item_id = item.setdefault("_id", uuid.uuid4().hex)
        qty_key = f"ingredient_qty_{item_id}"
        pending_qty = st.session_state.get("_pending_qty", {})
        qty_pushed = item_id in pending_qty
        if qty_pushed:
            st.session_state[qty_key] = pending_qty.pop(item_id)
        col_name, col_qty, col_unit, col_delete = st.columns([2.5, 2, 1.5, 1.2])
        item["name"] = col_name.text_input(
            "이름", value=item["name"], key=f"ingredient_name_{item_id}", label_visibility="collapsed"
        )
        new_qty_value = col_qty.number_input(
            "수량",
            min_value=0,
            max_value=MAX_QUANTITY,
            step=1,
            # 방금 session_state로 값을 넣은 실행에서는 value를 비워 둔다. 둘 다 넘기면 Streamlit이
            # '기본값과 Session State 값이 함께 지정됐다'는 경고를 띄운다. number_input은 key로만
            # 식별되므로(key_as_main_identity) value 인자가 실행마다 달라져도 위젯이 초기화되지 않는다.
            value=None if qty_pushed else safe_quantity(item["quantity"]),
            key=qty_key,
            label_visibility="collapsed",
        )
        # 사용자가 수량 칸을 비우면 None이 온다. 이때는 이전 수량을 유지한다.
        if new_qty_value is not None:
            item["quantity"] = str(new_qty_value)
        # 모델이 목록에 없는 단위(예: "통")를 주면 조용히 "개"로 바꾸지 않고 그 단위를 선택지에 더해 보여준다.
        unit_options = UNIT_OPTIONS if item["unit"] in UNIT_OPTIONS else [item["unit"], *UNIT_OPTIONS]
        item["unit"] = col_unit.selectbox(
            "단위",
            unit_options,
            index=unit_options.index(item["unit"]),
            key=f"ingredient_unit_{item_id}",
            label_visibility="collapsed",
        )
        if col_delete.button("삭제", key=f"delete_{item_id}"):
            delete_index = i
    if delete_index is not None:
        removed = ingredients.pop(delete_index)
        flash(f"'{removed['name'] or '이름 없는 재료'}'를 목록에서 뺐어요.", icon="🗑️")
        st.rerun()

    with st.form("add_ingredient_form", clear_on_submit=True):
        col_name, col_qty, col_unit, col_add = st.columns([2.5, 2, 1.5, 1.2])
        new_name = col_name.text_input(
            "추가할 재료 이름", label_visibility="collapsed", placeholder="추가할 재료 (예: 대파)"
        )
        new_qty = col_qty.number_input(
            "추가할 수량", min_value=0, max_value=MAX_QUANTITY, step=1, value=1, label_visibility="collapsed"
        )
        new_unit = col_unit.selectbox("추가할 재료 단위", UNIT_OPTIONS, label_visibility="collapsed")
        submitted = col_add.form_submit_button("추가")
        if submitted and not new_name.strip():
            st.warning("추가할 재료 이름을 입력해 주세요.")
        elif submitted:
            new_name_clean = new_name.strip()
            existing = next(
                (it for it in ingredients if it["name"].strip().lower() == new_name_clean.lower()), None
            )
            if existing:
                existing["quantity"] = str(min(safe_quantity(existing["quantity"]) + int(new_qty), MAX_QUANTITY))
                _push_quantity_to_widget(existing)
                flash(
                    f"이미 있는 '{existing['name']}'에 수량 {int(new_qty)}을(를) 더했어요 "
                    f"(지금 {existing['quantity']}{existing['unit']}).",
                    icon="➕",
                )
            else:
                ingredients.append(
                    {"_id": uuid.uuid4().hex, "name": new_name_clean, "quantity": str(new_qty), "unit": new_unit}
                )
                flash(f"'{new_name_clean}'를 추가했어요.", icon="➕")
            st.rerun()

    merged = merge_duplicate_ingredients(ingredients)
    if len(merged) < len(ingredients):
        # 이름을 고쳐서 다른 재료와 같아진 경우: 하나로 합치고 수량을 더한다.
        # 합쳐진 줄의 위젯 상태를 지우고 바로 다시 그려야 합산 수량이 입력칸에 반영된다.
        kept_ids = {item["_id"] for item in merged}
        for item in ingredients:
            if item["_id"] not in kept_ids:
                _forget_widget_state(item["_id"], fields=("name", "qty", "unit"))
        for item in merged:
            _push_quantity_to_widget(item)
        st.session_state.ingredients = merged
        flash("이름이 같은 재료를 하나로 합치고 수량을 더했어요.", icon="🔗")
        st.rerun()
    st.session_state.ingredients = merged


st.set_page_config(page_title="냉장고 식재료 인식", page_icon="🥬", layout="wide")
render_sidebar_auth()

# '자세히 보기' 버튼이 카드마다 다른 높이에 걸리지 않도록, 카드 안 마지막 요소(버튼)를 바닥에 붙인다.
st.markdown(
    """
    <style>
    div[data-testid="column"] > div[data-testid="stVerticalBlockBorderWrapper"] > div {
        display: flex;
        flex-direction: column;
        height: 100%;
    }
    div[data-testid="column"] > div[data-testid="stVerticalBlockBorderWrapper"] > div > div:last-child {
        margin-top: auto;
    }
    .ingredient-chip {
        display: inline-block;
        background: #e8e8e8;
        color: #333;
        border-radius: 14px;
        padding: 4px 12px;
        margin: 2px 4px 2px 0;
        font-size: 0.9em;
        white-space: nowrap;
    }
    /* layout="wide"는 본문 폭이 브라우저 폭(=확대/축소 배율)에 비례해서 늘어난다.
       업로드 사진은 use_container_width=True라 그 폭을 그대로 따라가므로,
       화면을 축소(더 넓은 영역이 보임)하면 사진이 오히려 커지는 문제가 있었다.
       최대 폭을 제한해 과도하게 커지지 않게 한다 (좁은 화면에서는 계속 축소됨). */
    [data-testid="stImage"] img {
        max-width: 700px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.session_state.setdefault("uploader_key", 0)
st.session_state.setdefault("wizard_step", 1)

def _do_restart():
    reset_app_flow_state()
    st.session_state.wizard_step = 1
    st.rerun()


def _request(flag):
    """버튼 on_click 콜백. 다음 실행에서 버튼을 비활성화한 채 작업을 하도록 표시만 남긴다.

    클릭 처리 중(AI 호출 중)에 버튼을 다시 눌러 같은 요청이 한 번 더 나가는 것을 막는다.
    """
    st.session_state[flag] = True


def _select_recipe(idx):
    st.session_state.selected_recipe = idx


_LEADING_STEP_NUMBER_RE = re.compile(r"^\s*(?:\d+\s*[.)]|step\s*\d+\s*[:.)]?)\s*", re.IGNORECASE)


def _clean_step(step):
    """조리 순서 한 줄. 모델이 붙인 번호("1. ...")는 지우고(목록 번호와 겹침) 줄바꿈은 공백으로 바꾼다."""
    return _LEADING_STEP_NUMBER_RE.sub("", " ".join(str(step).split()))


@st.fragment(run_every=1)
def cooldown_notice(last_request):
    """'다시 추천받기' 쿨다운 남은 시간을 1초마다 갱신하고, 끝나면 화면 전체를 다시 그려 버튼을 켠다."""
    remaining = RECIPE_REQUEST_COOLDOWN_SECONDS - (time.time() - last_request)
    if remaining <= 0:
        st.rerun()
    st.caption(f"⏳ {math.ceil(remaining)}초 뒤에 다시 추천받을 수 있어요.")


def render_save_button(recipe):
    """레시피 상세의 저장 영역. 로그인 여부와 저장 여부에 따라 버튼 상태를 바꿔 중복 저장을 막는다."""
    user = current_user()
    if not user:
        st.button("💾 레시피 저장", key="save_recipe", disabled=True, help="로그인하면 저장할 수 있어요.")
        st.info(
            f"로그인하면 이 레시피를 저장하고 나중에 '내 레시피'에서 다시 볼 수 있어요. "
            f"{SIDEBAR_HINT}에서 로그인해 주세요."
        )
        return
    if recipe.get("_saved_id"):
        st.button("✓ 저장한 레시피예요", key="save_recipe", disabled=True)
        st.caption("사이드바의 '내 레시피'에서 볼 수 있어요.")
        return
    if st.button("💾 레시피 저장", key="save_recipe", type="primary"):
        try:
            # 저장 직후 바로 표시를 남겨, 다음 실행부터 버튼이 '저장한 레시피예요'로 잠기게 한다.
            recipe["_saved_id"] = db.save_recipe(user["id"], recipe)
        except psycopg2.Error as e:
            report_db_error("레시피를 저장하지 못했어요.", e)
        else:
            flash(f"'{recipe.get('name') or '이름 없는 요리'}' 레시피를 저장했어요. 사이드바의 '내 레시피'에서 볼 수 있어요.", icon="💾")
            st.rerun()


header_col, restart_col = st.columns([5, 1])
header_col.title("🥬 냉장고 레시피 추천")
with restart_col:
    has_progress = st.session_state.wizard_step > 1 or bool(st.session_state.get("ingredients"))
    if has_progress:
        # 진행 중인 작업(인식된 재료/레시피)이 있을 때만 확인을 한 번 거치게 해서 실수로 잃지 않게 한다.
        with st.popover("🔄 처음부터"):
            st.write("지금까지 인식하거나 고친 재료와 추천받은 레시피가 모두 사라져요. (저장한 레시피는 남아요.)")
            if st.button("네, 처음부터 시작할게요", type="primary"):
                _do_restart()
    else:
        if st.button("🔄 처음부터"):
            _do_restart()

api_key = get_api_key()
wizard_step = st.session_state.wizard_step

step_cols = st.columns(3)
for i, col in enumerate(step_cols, start=1):
    label = WIZARD_STEP_LABELS[i - 1]
    # 마지막 단계(③)는 다음 단계가 없어 i < wizard_step 조건으로는 절대 완료 표시가 되지 않는다.
    # 레시피가 실제로 생성되었는지(st.session_state.recipes)를 완료 기준으로 따로 확인한다.
    is_last_step_done = i == 3 and i == wizard_step and bool(st.session_state.get("recipes"))
    if is_last_step_done:
        col.markdown(f"✅ **:red[{label}]**")
    elif i == wizard_step:
        col.markdown(f"**:red[{label}]**")
    elif i < wizard_step:
        col.markdown(f"✅ {label}")
    else:
        col.markdown(label)
st.divider()

# ==================== 1단계 화면: 사진 업로드(좌) + 식재료 인식(우) ====================
if wizard_step == 1:
    st.subheader("① 사진 업로드")
    st.caption("냉장고 안쪽이 잘 보이게 찍은 사진 1장을 올려 주세요. AI가 사진 속 식재료를 찾아 줘요.")
    uploaded_file = st.file_uploader(
        "냉장고 사진 (JPG·PNG, 10MB 이하)",
        type=["jpg", "jpeg", "png"],
        key=f"fridge_photo_{st.session_state.uploader_key}",
        help="사진을 이 칸에 끌어다 놓거나 버튼을 눌러 고르세요. 휴대폰에서는 바로 촬영한 사진도 올릴 수 있어요.",
    )

    if uploaded_file is not None:
        if uploaded_file.size > MAX_UPLOAD_BYTES:
            st.error(
                f"❌ 사진 용량이 10MB를 넘어요 (지금 {uploaded_file.size / 1024 / 1024:.1f}MB). "
                "더 작은 사진을 올려 주세요."
            )
            st.stop()

        raw_photo = uploaded_file.getvalue()
        try:
            # 표시용 축소본은 사진당 한 번만 만든다. st.image에 원본을 넘기면 rerun마다
            # 원본 전체를 디코딩·축소·재인코딩한다. 손상·초대형 이미지도 여기서 걸러진다.
            preview_bytes = photo_preview(raw_photo)
        except InvalidImageError as e:
            st.error(f"❌ {e}")
            st.stop()

        # 다른 사진으로 바꿨으면 이전 사진의 인식 실패 안내는 지운다 (새 사진과 무관하므로).
        photo_id = getattr(uploaded_file, "file_id", None) or f"{uploaded_file.name}:{uploaded_file.size}"
        if st.session_state.get("recognition_photo_id") != photo_id:
            for key in ("recognition_error", "recognition_error_detail", "recognition_error_level"):
                st.session_state.pop(key, None)
            st.session_state.recognition_photo_id = photo_id

        photo_col, recognize_col = st.columns(2)

        with photo_col:
            st.image(preview_bytes, caption=f"업로드한 사진: {uploaded_file.name}", use_container_width=True)

        with recognize_col:
            recognizing = st.session_state.pop("recognize_requested", False)
            st.button(
                "🔍 식재료 인식하기",
                type="primary",
                disabled=recognizing,
                on_click=_request,
                args=("recognize_requested",),
                help="사진 분석에는 보통 20초~1분 정도 걸려요.",
            )
            if recognizing:
                for key in ("recognition_error", "recognition_error_detail", "recognition_error_level"):
                    st.session_state.pop(key, None)
                st.session_state.pop("ingredients", None)
                st.session_state.pop("recipes", None)
                st.session_state.pop("selected_recipe", None)
                # 2단계 화면 표시에만 쓰므로 원본(최대 10MB) 대신 표시용 축소본을 세션에 둔다.
                st.session_state.uploaded_photo_bytes = preview_bytes
                st.session_state.uploaded_photo_name = uploaded_file.name

                with st.status("사진 속 식재료를 찾는 중이에요... (보통 20초~1분)", expanded=True) as status:
                    try:
                        data_uri, resized_bytes = to_resized_data_uri(io.BytesIO(raw_photo))
                    except InvalidImageError as e:
                        status.update(label="분석 실패", state="error")
                        st.session_state.recognition_error = f"❌ 식재료 인식 실패: {e}"
                        st.rerun()
                    status.write(
                        f"이미지 축소: {uploaded_file.size // 1024}KB → {resized_bytes // 1024}KB "
                        f"(긴 변 {MAX_IMAGE_DIMENSION}px 이하)"
                    )
                    try:
                        response, used_model = recognize_ingredients(
                            data_uri,
                            api_key,
                            on_attempt=lambda label, model: status.write(f"{label}: `{model}` 모델에 요청하는 중..."),
                            allowed_models=get_allowed_models("selected_vision_models"),
                        )
                    except requests.exceptions.RequestException as e:
                        status.update(label="분석 실패", state="error")
                        message, detail = network_error_message("식재료 인식", e)
                        st.session_state.recognition_error = message
                        st.session_state.recognition_error_detail = detail
                        st.rerun()
                    status.update(label="분석 완료", state="complete")

                st.session_state.used_vision_model = used_model

                ingredients, error, detail = interpret_model_response(
                    response,
                    "식재료 인식",
                    parse_ingredients,
                    "❌ 식재료 인식 실패: 서로 다른 AI 모델 3곳의 답을 모두 재료 목록으로 읽지 못했어요. "
                    "다시 시도하거나 재료를 직접 입력해 주세요.",
                )
                if error and is_empty_result(openrouter.extract_content(response)):
                    # 형식은 맞지만 빈 목록: 오류가 아니라 "재료를 못 찾음"이므로 다른 말과 수준으로 안내한다.
                    st.session_state.recognition_error = (
                        "사진에서 식재료를 찾지 못했어요. 냉장고 안이 밝고 잘 보이게 다시 찍어 올리거나, "
                        "재료를 직접 입력해 주세요."
                    )
                    st.session_state.recognition_error_level = "warning"
                elif error:
                    st.session_state.recognition_error = error
                    if detail:
                        st.session_state.recognition_error_detail = detail
                else:
                    st.session_state.ingredients = ingredients
                    st.session_state.wizard_step = 2
                    flash(f"식재료 {len(ingredients)}가지를 찾았어요. 맞는지 확인해 주세요.", icon="🥕")
                st.rerun()

            if st.session_state.get("used_vision_model"):
                st.caption(f"인식에 쓴 AI 모델: `{st.session_state.used_vision_model}`")

            if st.session_state.get("recognition_error"):
                if st.session_state.get("recognition_error_level") == "warning":
                    st.warning(st.session_state.recognition_error)
                else:
                    st.error(st.session_state.recognition_error)
                if st.session_state.get("recognition_error_detail"):
                    with st.expander("자세한 정보 (개발자용)"):
                        st.code(st.session_state.recognition_error_detail)
                st.caption("'식재료 인식하기'를 다시 누르거나, 사진 없이 재료를 직접 입력할 수 있어요.")
                if st.button("✏️ 재료를 직접 입력할게요"):
                    st.session_state.ingredients = []
                    for key in ("recognition_error", "recognition_error_detail", "recognition_error_level"):
                        st.session_state.pop(key, None)
                    st.session_state.wizard_step = 2
                    st.rerun()

# ==================== 2단계 화면: 업로드한 사진(좌) + 식재료 확인(우) ====================
elif wizard_step == 2:
    photo_col, ingredient_col = st.columns(2)

    with photo_col:
        st.subheader("① 사진 업로드")
        if st.session_state.get("uploaded_photo_bytes"):
            st.image(
                st.session_state.uploaded_photo_bytes,
                caption=f"업로드한 사진: {st.session_state.get('uploaded_photo_name', '')}",
                use_container_width=True,
            )
        else:
            st.caption("사진 없이 재료를 직접 입력하고 있어요.")
        if st.session_state.get("used_vision_model"):
            st.caption(f"인식에 쓴 AI 모델: `{st.session_state.used_vision_model}`")

    with ingredient_col:
        render_ingredient_editor()

    st.divider()
    nav_cols = st.columns(2)
    if nav_cols[0].button("← 사진 다시 고르기"):
        st.session_state.wizard_step = 1
        st.rerun()
    has_named_ingredient = any(
        item.get("name", "").strip() for item in st.session_state.get("ingredients", [])
    )
    if nav_cols[1].button(
        "다음: 레시피 추천받기 →",
        type="primary",
        disabled=not has_named_ingredient,
        help=None if has_named_ingredient else "재료를 1개 이상 추가해야 다음 단계로 갈 수 있어요.",
    ):
        st.session_state.wizard_step = 3
        st.rerun()
    if not has_named_ingredient:
        # 비활성 버튼의 help는 마우스를 올려야 보이고 모바일에서는 보이지 않아 따로 적는다.
        nav_cols[1].caption("재료를 1개 이상 추가하면 다음 단계로 갈 수 있어요.")

# ==================== 3단계 화면: 식재료 확인(좌) + 레시피 추천(우) ====================
elif wizard_step == 3:
    ingredient_col, recipe_col = st.columns(2)

    with ingredient_col:
        render_ingredient_editor()
        if st.button("← 사진과 함께 재료 확인하기"):
            st.session_state.wizard_step = 2
            st.rerun()

    with recipe_col:
        st.subheader("③ 레시피 추천")

        ingredient_names = [
            item["name"] for item in st.session_state.get("ingredients", []) if item.get("name", "").strip()
        ]
        # 재료 이름은 사용자 입력이거나 모델 출력이라 HTML로 끼워 넣기 전에 이스케이프한다.
        chips_html = "".join(
            f'<span class="ingredient-chip">{html.escape(name)}</span>' for name in ingredient_names
        )
        st.markdown(chips_html or "_왼쪽에서 재료를 먼저 추가해 주세요._", unsafe_allow_html=True)

        # 이번 세션에서 처음 진입한 경우, 프로필에 저장된 기본 조건을 초깃값으로 채운다.
        # 이미 값이 있으면(사용자가 수정했거나 이전에 채워졌으면) 건드리지 않는다.
        if "recipe_cuisine" not in st.session_state:
            saved = load_user_profile()

            def _default(saved_value, options):
                return saved_value if saved_value in options else options[0]

            st.session_state.recipe_cuisine = _default(saved and saved.get("default_cuisine"), CUISINE_OPTIONS)
            st.session_state.recipe_difficulty = _default(
                saved and saved.get("default_difficulty"), DIFFICULTY_OPTIONS
            )
            st.session_state.recipe_time = _default(saved and saved.get("default_time"), TIME_OPTIONS)
            st.session_state.recipe_servings = _default(saved and saved.get("default_servings"), SERVINGS_OPTIONS)

        opt_cols = st.columns(4)
        cuisine = opt_cols[0].selectbox("요리 종류", CUISINE_OPTIONS, key="recipe_cuisine")
        difficulty = opt_cols[1].selectbox("난이도", DIFFICULTY_OPTIONS, key="recipe_difficulty")
        time_pref = opt_cols[2].selectbox("조리 시간", TIME_OPTIONS, key="recipe_time")
        servings = opt_cols[3].selectbox("인원", SERVINGS_OPTIONS, key="recipe_servings")
        recipe_options = {
            "cuisine": cuisine,
            "difficulty": difficulty,
            "time_pref": time_pref,
            "servings": servings,
        }

        generating = st.session_state.pop("recipe_requested", False)
        last_request = st.session_state.get("last_recipe_request_time", 0)
        can_request = time.time() - last_request >= RECIPE_REQUEST_COOLDOWN_SECONDS
        has_recipes = bool(st.session_state.get("recipes"))

        if not ingredient_names:
            button_help = "재료를 1개 이상 추가해야 레시피를 추천받을 수 있어요."
        elif not can_request:
            button_help = "연속 요청을 막기 위해 잠시 기다린 뒤 다시 누를 수 있어요."
        else:
            button_help = "AI가 레시피 3가지를 추천해요. 보통 10초~1분 정도 걸려요."
        st.button(
            "🔁 다시 추천받기" if has_recipes else "🍳 레시피 추천받기",
            type="primary",
            disabled=generating or not can_request or not ingredient_names,
            help=button_help,
            on_click=_request,
            args=("recipe_requested",),
        )
        if not ingredient_names:
            st.caption(button_help)
        elif not can_request and not generating:
            cooldown_notice(last_request)

        if generating:
            st.session_state.last_recipe_request_time = time.time()
            st.session_state.pop("recipe_error", None)
            st.session_state.pop("recipe_error_detail", None)
            with st.status("레시피를 만드는 중이에요... (보통 10초~1분)", expanded=True) as status:
                try:
                    response, used_recipe_model = generate_recipes(
                        ingredient_names,
                        api_key,
                        on_attempt=lambda label, model: status.write(f"{label}: `{model}` 모델에 요청하는 중..."),
                        allowed_models=get_allowed_models("selected_text_models"),
                        **recipe_options,
                    )
                except requests.exceptions.RequestException as e:
                    status.update(label="추천 실패", state="error")
                    message, detail = network_error_message("레시피 추천", e)
                    st.session_state.recipe_error = message
                    st.session_state.recipe_error_detail = detail
                    st.rerun()
                status.update(label="추천 완료", state="complete")

            st.session_state.used_recipe_model = used_recipe_model

            recipes, error, detail = interpret_model_response(
                response,
                "레시피 추천",
                parse_recipes,
                "❌ 레시피 추천 실패: 서로 다른 AI 모델 3곳의 답을 모두 레시피로 읽지 못했어요. "
                "잠시 후 버튼을 다시 눌러 주세요.",
            )
            if error:
                # 실패해도 이전에 받은 레시피는 지우지 않는다 (다시 추천받기가 실패해도 보던 레시피를 잃지 않게).
                st.session_state.recipe_error = error
                if detail:
                    st.session_state.recipe_error_detail = detail
            else:
                st.session_state.recipes = recipes
                st.session_state.selected_recipe = 0
                # 폴백 3회가 모두 영어로 답한 경우에도 결과는 보여주되, 다시 받을 수 있다고 알린다.
                st.session_state.recipes_not_korean = not is_korean_enough(recipes)
                flash(f"레시피 {len(recipes)}가지를 추천받았어요.", icon="🍳")
            # 오류·결과를 session_state에 남기고 다시 그린다. 상단 단계 표시도 이때 완료(✅)로 바뀐다.
            st.rerun()

        if st.session_state.get("recipe_error"):
            st.error(st.session_state.recipe_error)
            if has_recipes:
                st.caption("이전에 추천받은 레시피는 아래에 그대로 남겨 뒀어요.")
            if st.session_state.get("recipe_error_detail"):
                with st.expander("자세한 정보 (개발자용)"):
                    st.code(st.session_state.recipe_error_detail)

        if st.session_state.get("used_recipe_model"):
            st.caption(f"추천에 쓴 AI 모델: `{st.session_state.used_recipe_model}`")

        if has_recipes:
            recipes = st.session_state.recipes
            if st.session_state.get("recipes_not_korean"):
                st.warning("이번에는 AI가 레시피를 한국어로 쓰지 않았어요. 한국어로 받으려면 '다시 추천받기'를 눌러 주세요.")
            st.caption("💡 같은 재료라도 누를 때마다 새로운 아이디어가 나와요. 카드의 '자세히 보기'로 조리 순서를 확인하세요.")
            selected_idx = st.session_state.get("selected_recipe")
            cols = st.columns(len(recipes))
            for idx, (col, r) in enumerate(zip(cols, recipes)):
                with col:
                    with st.container(border=True, height=260):
                        st.markdown(f"**{r.get('name') or '이름 없는 요리'}**")
                        st.write(cook_time_text(r))
                        missing = r.get("missing_ingredients") or []
                        if missing:
                            st.warning("더 필요: " + ", ".join(missing))
                        else:
                            st.success("재료가 다 있어요!")
                        is_selected = idx == selected_idx
                        st.button(
                            "✓ 보는 중" if is_selected else "자세히 보기",
                            key=f"select_recipe_{idx}",
                            disabled=is_selected,
                            on_click=_select_recipe,
                            args=(idx,),
                        )

            if selected_idx is not None and 0 <= selected_idx < len(recipes):
                recipe = recipes[selected_idx]
                st.divider()
                detail_header_col, detail_back_col = st.columns([4, 1])
                detail_header_col.markdown(f"### 📖 {recipe.get('name') or '이름 없는 요리'}")
                if detail_back_col.button("닫기 ✕", help="레시피 상세를 닫아요."):
                    st.session_state.pop("selected_recipe", None)
                    st.rerun()

                st.write(cook_time_text(recipe))
                used = recipe.get("used_ingredients") or []
                st.markdown("**사용 재료**: " + (", ".join(used) if used else "정보 없음"))
                missing = recipe.get("missing_ingredients") or []
                if missing:
                    st.warning("**더 필요한 재료**: " + ", ".join(missing))
                else:
                    st.success("필요한 재료가 다 있어요!")
                st.markdown("**조리 순서**")
                steps = [_clean_step(step) for step in recipe.get("steps") or []]
                steps = [step for step in steps if step]
                if steps:
                    st.markdown("\n".join(f"{n}. {step}" for n, step in enumerate(steps, start=1)))
                else:
                    st.info("조리 순서 정보가 없어요. '다시 추천받기'로 새 레시피를 받아 보세요.")

                render_save_button(recipe)
