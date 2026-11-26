"""vision.py / recipe.py가 함께 쓰는 OpenRouter 호출 공통 모듈.

- 모델 목록 원본은 이 모듈의 캐시 하나만 쓴다 (비전/텍스트 목록을 따로 조회하지 않는다).
- 응답 본문 추출(extract_content)과 모델 폴백 루프(call_with_fallback)를 한 곳에서 처리한다.
"""

import random
import threading
import time

import requests
import streamlit as st

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
EXCLUDE_KEYWORDS = ["safety", "rerank", "guard"]

# (연결, 읽기) 타임아웃. 읽기 60초는 기존 값 그대로이고, 연결 단계만 10초로 짧게 끊어
# 네트워크가 막혔을 때 한 모델에서 60초씩 기다리지 않게 한다.
CHAT_TIMEOUT = (10, 60)
MODELS_TIMEOUT = 10

# 어떤 모델로 바꿔도 같은 결과가 나오는 계정 단위 오류. 다음 모델을 시도하지 않고 바로 끝낸다.
#   401: API 키 오류, 402: 잔액/크레딧 부족
FATAL_STATUSES = {401, 402}
# 잠시 기다리면 풀릴 수 있는 일시적 오류. 다음 시도 전에 대기한다.
TRANSIENT_STATUSES = {408, 429}

_thread_local = threading.local()


def _session():
    """스레드별 requests.Session. 같은 스크립트 실행 안의 연속 호출(목록 조회, 1~3차 시도)이
    TCP/TLS 연결을 재사용하게 한다. Session은 스레드 안전이 보장되지 않으므로 스레드마다 따로 둔다.
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


@st.cache_data(ttl=300, show_spinner=False)
def fetch_models_payload():
    """오픈라우터 모델 목록 원본을 5분간 캐시한다 (비전/텍스트 공용 캐시 하나).

    실패하면 예외를 그대로 올려서 실패 결과가 캐시에 남지 않게 한다
    (일시적인 네트워크 오류가 5분 동안 굳어지지 않도록).
    """
    response = _session().get(MODELS_URL, timeout=MODELS_TIMEOUT)
    response.raise_for_status()
    return response.json().get("data", [])


def free_model_entries(require_image=False):
    """현재 사용 가능한 무료 모델의 원본 정보. require_image=True면 이미지 입력 모델만 고른다.

    조회 자체가 실패하면 빈 리스트를 반환한다 (호출부에서 안전망으로 대체).
    """
    try:
        data = fetch_models_payload()
    except (requests.exceptions.RequestException, ValueError, AttributeError):
        return []

    entries = []
    for m in data or []:
        if not isinstance(m, dict):
            continue
        model_id = m.get("id") or ""
        if not model_id.endswith(":free"):
            continue
        if any(keyword in model_id.lower() for keyword in EXCLUDE_KEYWORDS):
            continue
        if require_image:
            # architecture 키가 있어도 값이 null인 경우가 있어 `or {}`로 막는다.
            modality = (m.get("architecture") or {}).get("input_modalities") or []
            if "image" not in modality:
                continue
        entries.append(m)
    return entries


def model_ids(entries, safety_net, allowed_models=None):
    """모델 id 목록. allowed_models가 주어지면 그 안에서만 고른다.

    allowed_models로 걸러낸 결과가 비어있으면(사용자가 골라둔 모델이 전부 목록에서
    사라진 경우) 안전하게 전체 목록으로 되돌아간다.
    """
    models = [m.get("id", "") for m in entries] or list(safety_net)
    if allowed_models:
        filtered = [m for m in models if m in allowed_models]
        if filtered:
            return filtered
    return models


def model_details(entries):
    """프로필 화면의 모델 선택 UI에 쓸 상세 정보(id/name/context_length/created)."""
    return [
        {
            "id": m.get("id", ""),
            "name": m.get("name") or m.get("id", ""),
            "context_length": m.get("context_length"),
            "created": m.get("created"),
        }
        for m in entries
    ]


def prioritize_models(pool, preferred):
    """실측 우선순위(preferred) 순서로 후보를 정렬한다.

    우선순위 목록에 없는 모델은 뒤쪽에 무작위 순서로 배치한다. 무료 모델 라인업은
    수시로 바뀌므로, 아직 실측하지 않은 새 모델도 뒤에서는 시도될 수 있게 남겨둔다.
    """
    ranked = [m for m in preferred if m in pool]
    rest = [m for m in pool if m not in ranked]
    random.shuffle(rest)
    return ranked + rest


def plan_attempts(pool, attempts, safety_net):
    """attempts번 시도할 모델 순서를 만든다.

    풀이 attempts보다 적으면(예: 사용자가 모델을 1~2개만 선택한 경우) 안전망 모델로
    채우지 않고 풀 안에서 순환한다 - 사용자가 고르지 않은 모델을 몰래 끼워넣지 않기 위함.
    """
    if pool:
        return [pool[i % len(pool)] for i in range(attempts)]
    return [random.choice(safety_net) for _ in range(attempts)]


def post_chat(model, messages, api_key):
    return _session().post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages},
        timeout=CHAT_TIMEOUT,
    )


def error_info(response):
    """오류 응답 본문의 error 객체. 형식이 어긋나면 빈 dict."""
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def is_daily_limit(response):
    """429가 모델 순간 혼잡이 아니라 계정의 무료 일일 한도 소진인지 판별한다."""
    if response.status_code != 429:
        return False
    error = error_info(response)
    metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
    return (
        "free-models-per-day" in str(error.get("message") or "")
        or metadata.get("limit_source") == "openrouter_free_tier_daily"
    )


def extract_content(response):
    """200 응답에서 choices[0].message.content 문자열을 꺼낸다.

    상태 코드가 200이 아니거나, JSON이 아니거나, choices/message/content가 없거나
    문자열이 아니거나 비어 있으면 None을 반환한다 (무료 모델은 200인데 content가 null인 경우가 있다).
    """
    if response is None or response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def call_with_fallback(models, send, accept, on_attempt=None):
    """models 순서대로 send(model)을 호출해, accept(content)가 참이 되는 첫 응답에서 멈춘다.

    시도 결과별 처리:
      - 성공(200 + accept 통과): 즉시 반환
      - 401/402, 무료 일일 한도 429: 계정 단위 오류라 다른 모델도 같으므로 즉시 중단
      - 일시적 오류(429 순간 혼잡, 408, 5xx): 시도마다 늘어나는 대기(2초, 4초…) 후 다음 모델
      - 그 밖의 4xx(400 잘못된 모델 id 등): 그 모델만의 문제로 보고 대기 없이 다음 모델,
        같은 모델이 순환으로 다시 나오면 건너뛴다
      - 200인데 content 없음/파싱 실패: 대기 없이 다음 모델 (다음은 다른 모델이므로)
      - 타임아웃·연결 오류(RequestException): 대기 없이 다음 모델. 마지막 시도였다면 예외를 올린다

    반환: (response, used_model). response는 마지막으로 받은 HTTP 응답.
    """
    response = None
    used_model = models[0] if models else None
    last_error = None
    skipped = set()
    attempt_no = 0
    for i, model in enumerate(models):
        if model in skipped:
            continue
        attempt_no += 1
        if on_attempt is not None:
            on_attempt(f"{attempt_no}차 시도", model)
        used_model = model
        try:
            current = send(model)
        except requests.exceptions.RequestException as e:
            last_error = e
            continue
        last_error = None
        response = current
        status = response.status_code

        if status == 200:
            content = extract_content(response)
            try:
                if content is not None and accept(content):
                    break
            except (TypeError, ValueError, AttributeError, KeyError, OverflowError):
                # 파싱 함수가 예상 못 한 응답 형식으로 실패해도 루프를 벗어나지 않고 다음 모델로 간다.
                pass
            continue
        if status in FATAL_STATUSES or is_daily_limit(response):
            break
        if status in TRANSIENT_STATUSES or status >= 500:
            if i < len(models) - 1:
                # 429는 모델별 순간 혼잡이라 1초로는 잘 풀리지 않는다. 시도마다 대기를 늘린다.
                time.sleep(2 * attempt_no)
            continue
        skipped.add(model)

    if last_error is not None:
        raise last_error
    return response, used_model
