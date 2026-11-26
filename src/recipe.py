import json
import re

import openrouter

CUISINE_OPTIONS = ["상관없음", "한식", "양식", "중식", "일식"]
DIFFICULTY_OPTIONS = ["상관없음", "초급", "중급", "고급"]
TIME_OPTIONS = ["상관없음", "15분 이내", "30분 이내", "30분 이상"]
SERVINGS_OPTIONS = ["상관없음", "1인분", "2인분", "3인분", "4인분"]

# 오픈라우터 무료 모델 목록 조회 자체가 실패할 경우를 위한 최소 안전망 (2026-10-31 동작 확인).
SAFETY_NET_RECIPE_MODELS = [
    "nvidia/nemotron-3.5-super-vl:free",
    "cohere/north-mini-code:free",
]

# 2026-10-31 재료 7종으로 다시 실측한 우선순위. 앞쪽일수록 먼저 시도한다.
# minimax-m3(2026-10-22)와 nemotron-3-super(2026-10-30)는 무료 목록에서 빠졌다.
# 이미지 입력 모델도 텍스트 전용 프롬프트에 정상 응답하므로 후보에서 제외하지 않는다.
#   nemotron-3.5-super-vl  성공 2/2, 레시피 3개, 조리 3.8단계, 10~15초. 가장 빠름
#   nemotron-3-ultra       성공 2/2, 레시피 3개, 조리 5.5단계, 25~39초. 조리 순서가 가장 상세
#                          (2026-10-16 녹화 중 5분 넘게 응답하지 않은 적이 있어 2순위에 둠)
#   north-mini-code        성공 2/2, 레시피 3개, 조리 3.0단계, 15~62초
# 목록에 없는 모델은 이 뒤에 무작위 순서로 붙는다 (새로 등장한 무료 모델도 기회를 얻도록).
PREFERRED_RECIPE_MODELS = [
    "nvidia/nemotron-3.5-super-vl:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "cohere/north-mini-code:free",
]


def list_free_text_models(allowed_models=None):
    """현재 사용 가능한 무료 텍스트 모델 id 목록. allowed_models가 주어지면 그 안에서만 고른다.

    이미지 입력을 함께 받는 모델도 텍스트 프롬프트에 정상 응답하므로 제외하지 않는다.
    allowed_models로 걸러낸 결과가 비어있으면(사용자가 골라둔 모델이 전부 사라진 경우)
    안전하게 전체 목록으로 되돌아간다.
    """
    entries = openrouter.free_model_entries(require_image=False)
    return openrouter.model_ids(entries, SAFETY_NET_RECIPE_MODELS, allowed_models)


def prioritize_models(pool):
    """실측 우선순위(PREFERRED_RECIPE_MODELS) 순서로 후보를 정렬한다."""
    return openrouter.prioritize_models(pool, PREFERRED_RECIPE_MODELS)


def list_free_text_models_detailed():
    """프로필 화면의 모델 선택 UI에 쓸 상세 정보(id/name/context_length/created)를 반환."""
    return openrouter.model_details(openrouter.free_model_entries(require_image=False))


def build_prompt(ingredient_names, cuisine=None, difficulty=None, time_pref=None, servings=None):
    ingredients_text = ", ".join(ingredient_names)
    conditions = []
    if cuisine and cuisine != "상관없음":
        conditions.append(f"요리 종류: {cuisine}")
    if difficulty and difficulty != "상관없음":
        conditions.append(f"난이도: {difficulty}")
    if time_pref and time_pref != "상관없음":
        conditions.append(f"조리 시간: {time_pref}")
    if servings and servings != "상관없음":
        conditions.append(f"인원: {servings}")
    condition_text = ""
    if conditions:
        condition_text = "다음 조건에 맞춰줘: " + ", ".join(conditions) + ".\n"

    return (
        f"다음 재료로 만들 수 있는 요리 3가지를 추천해줘: {ingredients_text}.\n"
        f"{condition_text}"
        "요리명, 조리 순서 등 모든 텍스트는 반드시 한국어로만 작성해줘. 영어를 섞지 마.\n"
        "각 레시피는 아래 JSON 형식으로만 답해줘.\n"
        "{\n"
        '  "recipes": [\n'
        "    {\n"
        '      "name": "요리명",\n'
        '      "used_ingredients": ["보유 재료 중 사용되는 것"],\n'
        '      "missing_ingredients": ["추가로 필요한 재료"],\n'
        '      "steps": ["조리 순서 1", "조리 순서 2"],\n'
        '      "cook_time_minutes": 20\n'
        "    }\n"
        "  ]\n"
        "}"
    )


def call_recipe_model(model, ingredient_names, api_key, **options):
    messages = [{"role": "user", "content": build_prompt(ingredient_names, **options)}]
    return openrouter.post_chat(model, messages, api_key)


def generate_recipes(ingredient_names, api_key, on_attempt=None, attempts=3, allowed_models=None, **options):
    """오픈라우터의 현재 무료 텍스트 모델 중 서로 다른 모델을 순서대로 시도한다.

    실측 우선순위(PREFERRED_RECIPE_MODELS)가 앞에 오고, 아직 실측하지 않은 모델이
    무작위 순서로 뒤를 잇는다. 429·5xx·타임아웃·연결 오류·200인데 내용 없음·JSON 파싱 실패·
    한국어가 아닌 응답은 다음 모델로 넘어가고, 401/402·무료 일일 한도처럼 모델을 바꿔도 같은
    오류는 바로 멈춘다 (세부 정책은 openrouter.call_with_fallback 참고).
    allowed_models: 사용자가 프로필에서 선택한 모델 id 목록. 없으면 전체 무료 모델을 대상으로 한다.
    반환: (response, used_model)
    """
    pool = prioritize_models(list_free_text_models(allowed_models=allowed_models))
    models = openrouter.plan_attempts(pool, attempts, SAFETY_NET_RECIPE_MODELS)
    prompt = build_prompt(ingredient_names, **options)
    messages = [{"role": "user", "content": prompt}]

    def accept(content):
        recipes = parse_recipes(content)
        return bool(recipes) and is_korean_enough(recipes)

    return openrouter.call_with_fallback(
        models,
        send=lambda model: openrouter.post_chat(model, messages, api_key),
        accept=accept,
        on_attempt=on_attempt,
    )


_HANGUL_RE = re.compile(r"[가-힣]")


def is_korean_enough(recipes):
    """레시피명/조리 순서에 한글이 전혀 없으면(영어로만 응답한 경우) 실패로 간주한다."""
    parts = []
    for r in recipes:
        if not isinstance(r, dict):
            continue
        parts.append(str(r.get("name") or ""))
        steps = r.get("steps")
        if isinstance(steps, list):
            parts.extend(str(step) for step in steps)
    return bool(_HANGUL_RE.search(" ".join(parts)))


def parse_recipes(content):
    if not isinstance(content, str):
        return None
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    recipes = data.get("recipes")
    if not isinstance(recipes, list):
        return None
    normalized = [_normalize_recipe(r) for r in recipes if isinstance(r, dict)]
    return normalized or None


_DIGITS_RE = re.compile(r"\d+")


def _text_list(value):
    """문자열 목록으로 정리한다. 문자열 하나면 [문자열], 목록이 아니면 []. 빈 항목은 뺀다."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def _cook_minutes(value):
    """조리 시간을 분 단위 정수로. "20분"처럼 글자가 섞이면 첫 숫자를 쓰고, 알 수 없으면 None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            minutes = int(value)
        except (ValueError, OverflowError):
            return None
        return minutes if minutes > 0 else None
    if isinstance(value, str):
        match = _DIGITS_RE.search(value)
        if match:
            minutes = int(match.group(0))
            return minutes if minutes > 0 else None
    return None


def _normalize_recipe(recipe):
    """화면 표시·DB 저장이 깨지지 않도록 레시피 한 개의 필드 형식을 맞춘다.

    모델은 cook_time_minutes에 "20분"이나 null을, 목록 필드에 문자열이나 숫자를 섞어 보내기도 한다.
    """
    # 이름이 비어도 기본 문구로 채우지 않는다 (채우면 영어 응답이 한글 검사를 통과해 버린다). 표시할 때 채운다.
    return {
        "name": str(recipe.get("name") or "").strip(),
        "used_ingredients": _text_list(recipe.get("used_ingredients")),
        "missing_ingredients": _text_list(recipe.get("missing_ingredients")),
        "steps": _text_list(recipe.get("steps")),
        "cook_time_minutes": _cook_minutes(recipe.get("cook_time_minutes")),
    }
