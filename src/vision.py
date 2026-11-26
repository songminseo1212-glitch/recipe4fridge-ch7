import base64
import io
import json
import re
import uuid

from PIL import Image, ImageOps

import openrouter

MAX_IMAGE_DIMENSION = 1024
# 디코딩 전에 헤더의 가로×세로만 보고 거절하는 픽셀 수 상한 (약 8000×8000).
# 10MB 용량 제한은 압축된 크기만 보므로, 단색 PNG처럼 작은 파일이 수억 픽셀로 풀리는 경우를 막는다.
# 일반 폰 사진(12~50MP)은 통과한다. Pillow 기본값(약 1.8억 픽셀에서 오류)보다 엄격하다.
MAX_INPUT_PIXELS = 64_000_000
# st.image가 화면 표시용으로 줄이는 최대 폭(Streamlit 내부 MAXIMUM_CONTENT_WIDTH = 2×730)과 같게 맞춘다.
PREVIEW_MAX_WIDTH = 1460
_EXIF_ORIENTATION_TAG = 0x0112
UNIT_OPTIONS = ["개", "병", "봉지", "팩", "단", "g", "kg", "ml", "L", "기타"]
RECOGNITION_PROMPT = (
    '이 냉장고 사진에서 보이는 식재료를 한국어로 인식해서 JSON 배열 형식으로만 답해줘. '
    '각 항목은 name(이름), quantity(수량, 숫자), unit(단위: 개/병/봉지/팩/단/g/kg/ml/L 중 하나)을 포함해줘.\n'
    '수량은 눈에 보이는 낱개를 하나씩 세어서 적어줘. '
    '달걀판처럼 여러 개가 담긴 것도 팩이 아니라 보이는 알의 개수로 세어줘. '
    '가려져서 셀 수 없을 때만 quantity를 1, unit을 "개"로 적어줘.\n'
    '음식이 아닌 물건(보관용기, 그릇, 포장재)은 목록에 넣지 마.\n'
    '예: [{"name": "계란", "quantity": 6, "unit": "개"}, {"name": "우유", "quantity": 1, "unit": "개"}]'
)

# 오픈라우터 무료 모델 목록 조회 자체가 실패할 경우를 위한 최소 안전망 (2026-10-31 동작 확인).
SAFETY_NET_VISION_MODELS = [
    "nvidia/nemotron-3.5-super-vl:free",
    "dots-studio/dots-3-note-preview:free",
]

# 2026-10-31 냉장고 사진으로 다시 실측한 우선순위. 앞쪽일수록 먼저 시도한다.
# minimax-m3는 2026-10-22 무료 목록에서 빠졌다.
# 무료 모델은 응답 자체가 거부되는 경우가 잦아, 성공률을 인식 정확도보다 앞에 둔다.
#   nemotron-3.5-super-vl  성공 3/3, 품목 7/7, 19~52초
#   dots-3-note-preview    성공 2/3, 품목 7/7, 52~71초 (실패 1회는 10분 뒤 빈 응답)
#   gemma-4-26b            성공 0/3, 429 (2026-08-27에는 성공 2/13, 개수 7.0/7)
#   gemma-4-31b            성공 0/3, 429 (2026-08-27에는 성공 1/13, 개수 6.0/7)
# 목록에 없는 모델은 이 뒤에 무작위 순서로 붙는다 (새로 등장한 무료 모델도 기회를 얻도록).
PREFERRED_VISION_MODELS = [
    "nvidia/nemotron-3.5-super-vl:free",
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
]


class InvalidImageError(ValueError):
    """업로드 이미지를 읽을 수 없거나(손상·형식 오류) 해상도가 상한을 넘을 때."""


def list_free_vision_models(allowed_models=None):
    """현재 사용 가능한 무료 비전 모델 id 목록. allowed_models가 주어지면 그 안에서만 고른다.

    allowed_models로 걸러낸 결과가 비어있으면(예: 사용자가 골라둔 모델이 전부 목록에서
    사라진 경우) 안전하게 전체 목록으로 되돌아간다.
    """
    entries = openrouter.free_model_entries(require_image=True)
    return openrouter.model_ids(entries, SAFETY_NET_VISION_MODELS, allowed_models)


def prioritize_models(pool):
    """실측 우선순위(PREFERRED_VISION_MODELS) 순서로 후보를 정렬한다."""
    return openrouter.prioritize_models(pool, PREFERRED_VISION_MODELS)


def list_free_vision_models_detailed():
    """프로필 화면의 모델 선택 UI에 쓸 상세 정보(id/name/context_length/created)를 반환."""
    return openrouter.model_details(openrouter.free_model_entries(require_image=True))


_DECODE_ERRORS = (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError)
_UNREADABLE_MESSAGE = "이미지 파일을 읽을 수 없습니다. 손상되지 않은 JPG/PNG 파일인지 확인해주세요."


def _open_checked(file_like):
    """이미지를 연다(헤더만 읽음). 가로×세로가 MAX_INPUT_PIXELS를 넘으면 디코딩 전에 거절한다.

    주의: PNG는 getexif()가 본문 전체를 디코딩하므로, 해상도 검사는 반드시 그보다 먼저 한다.
    """
    image = Image.open(file_like)
    width, height = image.size
    if width * height > MAX_INPUT_PIXELS:
        image.close()
        raise InvalidImageError(
            f"이미지 해상도가 너무 큽니다 ({width}×{height}). "
            f"{MAX_INPUT_PIXELS // 1_000_000}백만 화소 이하로 줄여서 올려주세요."
        )
    return image


def _load_rgb(file_like, bound):
    """이미지를 열어 EXIF 회전을 적용하고 bound(가로, 세로) 상자 안으로 축소한 RGB 이미지를 돌려준다.

    - 디코딩 전에 헤더의 해상도로 MAX_INPUT_PIXELS를 검사한다.
    - JPEG은 draft()로 DCT 단계에서 1/2·1/4·1/8 축소 디코딩해 큰 사진의 디코딩 시간과 메모리를 줄인다.
      목표 크기의 2배 이상은 남기므로(thumbnail의 reducing_gap=2와 같은 기준) 최종 화질은 같다.
    - 읽을 수 없는 파일은 InvalidImageError로 바꿔 올린다.
    """
    try:
        image = _open_checked(file_like)
        width, height = image.size
        orientation = image.getexif().get(_EXIF_ORIENTATION_TAG, 1)
        # 5~8은 90도 회전이라 화면 기준 가로·세로가 저장된 값과 뒤바뀐다.
        shown_w, shown_h = (height, width) if orientation in (5, 6, 7, 8) else (width, height)
        scale = min(bound[0] / shown_w, bound[1] / shown_h, 1.0)
        if scale < 1.0:
            image.draft(None, (max(1, int(width * scale * 2)), max(1, int(height * scale * 2))))
        # 폰 사진은 픽셀이 누운 채 저장되고 EXIF로 회전 정보만 붙는 경우가 많다.
        ImageOps.exif_transpose(image, in_place=True)
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.thumbnail(bound)
        return image
    except InvalidImageError:
        raise
    except _DECODE_ERRORS as e:
        # UnidentifiedImageError(형식 인식 불가), 잘린 파일(OSError) 등
        raise InvalidImageError(_UNREADABLE_MESSAGE) from e


def to_resized_data_uri(file_like):
    """이미지를 긴 변 기준 MAX_IMAGE_DIMENSION 이하로 축소해 base64 data URI로 변환한다.

    이미지를 읽을 수 없거나 해상도가 너무 크면 InvalidImageError를 올린다.
    """
    image = _load_rgb(file_like, (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    resized_bytes = buffer.getvalue()
    b64 = base64.b64encode(resized_bytes).decode()
    return f"data:image/jpeg;base64,{b64}", len(resized_bytes)


def make_preview_bytes(raw_bytes):
    """화면 표시용 이미지 바이트를 만든다.

    st.image는 바이트를 받을 때마다(=rerun마다) 원본을 디코딩해 폭 1460px로 줄이고 JPEG로
    다시 인코딩한다. 그 결과를 여기서 한 번만 만들어 두면 이후 rerun에서는 헤더만 읽고 끝난다.
    폭이 이미 작고 회전 정보도 없으면 원본을 그대로 돌려준다 (st.image도 이 경우 원본을 쓴다).
    이미지를 읽을 수 없거나 해상도가 너무 크면 InvalidImageError를 올린다.
    """
    try:
        with _open_checked(io.BytesIO(raw_bytes)) as probe:
            if probe.size[0] <= PREVIEW_MAX_WIDTH and probe.getexif().get(_EXIF_ORIENTATION_TAG, 1) == 1:
                # 헤더만 정상이고 본문이 잘린 파일을 걸러내기 위해 끝까지 디코딩해 본다.
                probe.load()
                return raw_bytes
    except InvalidImageError:
        raise
    except _DECODE_ERRORS as e:
        raise InvalidImageError(_UNREADABLE_MESSAGE) from e
    image = _load_rgb(io.BytesIO(raw_bytes), (PREVIEW_MAX_WIDTH, PREVIEW_MAX_WIDTH * 100))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def call_vision_model(model, data_uri, api_key):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": RECOGNITION_PROMPT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }
    ]
    return openrouter.post_chat(model, messages, api_key)


def recognize_ingredients(data_uri, api_key, on_attempt=None, attempts=3, allowed_models=None):
    """오픈라우터의 현재 무료 비전 모델 중 서로 다른 모델을 순서대로 시도한다.

    실측 우선순위(PREFERRED_VISION_MODELS)가 앞에 오고, 아직 실측하지 않은 모델이
    무작위 순서로 뒤를 잇는다. 429·5xx·타임아웃·연결 오류·200인데 내용 없음·JSON 파싱 실패는
    다음 모델로 넘어가고, 401/402·무료 일일 한도처럼 모델을 바꿔도 같은 오류는 바로 멈춘다
    (세부 정책은 openrouter.call_with_fallback 참고). 마지막 시도가 네트워크 예외면 그 예외를 올린다.

    on_attempt: 선택적 콜백. (label, model) 을 인자로 호출되어 진행 상황을 UI에 전달할 수 있다.
    allowed_models: 사용자가 프로필에서 선택한 모델 id 목록. 없으면 전체 무료 모델을 대상으로 한다.
    반환: (response, used_model)
    """
    pool = prioritize_models(list_free_vision_models(allowed_models=allowed_models))
    models = openrouter.plan_attempts(pool, attempts, SAFETY_NET_VISION_MODELS)
    return openrouter.call_with_fallback(
        models,
        send=lambda model: call_vision_model(model, data_uri, api_key),
        accept=lambda content: parse_ingredients(content) is not None,
        on_attempt=on_attempt,
    )


def merge_duplicate_ingredients(ingredients):
    """이름이 같은(공백 제거, 대소문자 무시) 재료는 수량을 합쳐 하나로 합친다."""
    merged = {}
    order = []
    for item in ingredients:
        key = item["name"].strip().lower()
        if key not in merged:
            merged[key] = dict(item)
            order.append(key)
        else:
            try:
                merged[key]["quantity"] = str(int(merged[key]["quantity"]) + int(item["quantity"]))
            except (TypeError, ValueError):
                pass
    return [merged[key] for key in order]


def parse_ingredients(content):
    """모델 응답을 [{"name", "quantity", "unit"}, ...] 형태로 파싱한다.

    모델이 예전 방식대로 문자열 배열(["계란", "우유"])로 답한 경우도
    수량 1, 단위 "개"로 채워 동일한 형태로 반환한다 (하위 호환).
    동일한 이름의 재료가 중복 인식된 경우 수량을 합쳐 하나로 정리한다.
    content가 문자열이 아니면(None 등) None을 반환한다.
    """
    if not isinstance(content, str):
        return None
    match = re.search(r"\[.*\]", content, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None

    ingredients = []
    for item in data:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            try:
                quantity = int(float(item.get("quantity", 1)))
            except (TypeError, ValueError, OverflowError):
                quantity = 1
            unit = str(item.get("unit") or "개").strip() or "개"
        else:
            name = str(item).strip()
            if not name:
                continue
            quantity = 1
            unit = "개"
        ingredients.append({"_id": uuid.uuid4().hex, "name": name, "quantity": str(quantity), "unit": unit})
    if not ingredients:
        return None
    return merge_duplicate_ingredients(ingredients)
