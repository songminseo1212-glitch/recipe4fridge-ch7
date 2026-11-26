# 코드 검토 보고서

- 검토일: 2026-11-26
- 검토자: code-reviewer 에이전트 (코드 수정 없음)
- 기준 커밋: `f1fa3a1` (main)
- 범위: `src/` 전체, `requirements.txt`, `.gitignore`, `.streamlit/config.toml`, `.claude/`(launch.json, agents/code-reviewer.md), `.env.example`, `CLAUDE.md`, `docs/PRD_step1~3.md`

## 요약

총 29건: **높음 6**, **중간 10**, **낮음 13**. 확신이 낮은 항목은 "확인 필요"로 표시했다.

잘 되어 있는 부분:

- API 키·`DATABASE_URL` 하드코딩 없음. 두 값 모두 환경 변수 또는 `st.secrets`로 읽는다.
- SQL은 모두 `%s` 바인딩을 사용한다(인젝션 위험 없음).
- 비밀번호는 bcrypt 해시로 저장한다.
- 레시피 삭제 시 `user_id`를 함께 검사한다.
- 로그인 실패 메시지가 하나로 통일되어 있다.

---

## 높음

### 1. 타임아웃·연결 오류에서 모델 폴백이 동작하지 않음
- 위치: `src/vision.py:178-195`, `src/recipe.py:177-196`
- 문제: 재시도 루프 안의 `requests.post` 호출이 `ValueError`만 잡는다. 첫 모델이 60초 타임아웃되거나 연결이 끊기면 예외가 루프를 빠져나가 `app.py`로 올라가므로 2·3차 모델은 시도되지 않는다.
- 시나리오: 무료 모델은 응답이 멈추는 일이 잦다(vision.py:34-41, recipe.py:24-31 주석). 사용자는 "시간 초과"만 보고 다른 모델로 넘어가지 못한다. "매 시도가 폴백"이라는 docstring과도 어긋난다.
- 제안: 루프 안에서 `requests.exceptions.RequestException`을 잡아 다음 모델로 진행하고, 마지막 시도에서만 예외를 올린다.

### 2. 응답 파싱 중 KeyError/TypeError/AttributeError를 잡지 않음
- 위치: `src/vision.py:188`, `src/recipe.py:188, 205`, `src/app.py:339, 345, 516, 525`
- 문제: `except ValueError`만 있다. 다음 경우 예외가 그대로 올라가 Streamlit 에러 화면이 뜬다.
  - `choices[0]["message"]["content"]`가 없거나 None인 경우. `re.search`에서 TypeError가 난다(vision.py:222, recipe.py:211).
  - recipes 항목이 dict가 아닌 경우. `r.get`에서 AttributeError가 난다.
  - `steps` 원소가 문자열이 아닌 경우. `" ".join`에서 TypeError가 난다(recipe.py:204-206).
  - app.py의 `response.json()`(339, 516)이 ValueError를 내는데 try가 없다.
- 시나리오: 200 OK인데 content가 null인 응답(무료 모델에서 흔함). 폴백도, 안내 메시지도 없다.
- 제안: 응답 추출을 한 함수(예: `extract_content(response)`)로 모아 모든 실패를 None으로 정규화하고, 루프와 app.py가 같이 사용한다.

### 3. 업로드 이미지 처리의 예외 미처리와 검증 부족
- 위치: `src/app.py:306`, `src/vision.py:128`
- 문제: `to_resized_data_uri` 호출이 try 밖에 있다. 확장자만 jpg/png이고 내용이 손상됐거나 픽셀 수가 매우 큰 이미지면 Pillow의 `UnidentifiedImageError`/`DecompressionBombError`가 그대로 터진다. 10MB 제한은 압축된 바이트 수만 검사하고 해상도 상한이 없어, 작은 파일로 수억 픽셀 PNG를 만들면 메모리가 소진될 수 있다.
- 제안: 예외를 잡아 안내 메시지를 보여 주고, `Image.MAX_IMAGE_PIXELS` 설정이나 사전 크기 검사를 둔다.

### 4. 모델이 음수 수량을 주면 위젯이 예외로 중단됨
- 위치: `src/app.py:155`, `src/vision.py:239`
- 문제: `parse_ingredients`가 `int(float(...))`로 변환하므로 `-1`이 그대로 통과한다. `st.number_input(min_value=0, value=-1)`은 `StreamlitAPIException`을 낸다. 같은 위치에서 `float("inf")` 입력은 OverflowError를 낼 수 있다(TypeError/ValueError만 잡음).
- 시나리오: 모델이 quantity에 음수를 넣으면 2단계 화면이 열리지 않는다.
- 제안: 파싱 단계에서 0 이상으로 보정하고 OverflowError도 잡는다.

### 5. 수량 합산이 위젯 상태에 덮어써질 가능성 (확인 필요)
- 위치: `src/app.py:150-159, 183-185`, `src/vision.py:209`
- 문제: 추가 폼과 병합 로직은 기존 재료의 `existing["quantity"]`를 늘린다. 그런데 편집기가 같은 key(`ingredient_qty_{id}`)를 가진 `number_input`의 반환값으로 다시 덮어쓴다. Streamlit은 key가 있는 위젯에서 `session_state` 값을 우선하므로 합산된 값이 반영되지 않을 수 있다.
- 재현: 같은 이름의 재료를 다시 추가하거나, 이름을 기존 재료와 같게 고쳐 본다.

### 6. [보안] Google 로그인이 이메일 소유권 검증 없이 기존 계정에 연결됨
- 위치: `src/auth_ui.py:45`, `src/db.py:164-170`, `src/auth.py:31-35`
- 문제: 비밀번호 가입에는 이메일 인증이 없다. 공격자가 피해자 이메일로 먼저 가입해 두면, 피해자가 나중에 Google로 로그인할 때 `get_or_create_google_user`가 공격자가 만든 계정을 그대로 돌려준다. `email_verified` 클레임도 확인하지 않는다.
- 시나리오: 피해자의 레시피와 프로필이, 공격자가 비밀번호를 아는 계정에 쌓인다.
- 제안: Google 클레임의 `email_verified`를 확인한다. 비밀번호 계정과 자동 연결하지 말고 별도로 처리하거나, 가입 시 이메일 인증을 도입한다.

---

## 중간

### 7. 72바이트 초과 비밀번호 처리 (확인 필요)
- 위치: `src/auth.py:13, 17, 27`
- 문제: `requirements.txt`가 버전을 고정하지 않는다. bcrypt 5.x는 72바이트를 넘는 비밀번호에 ValueError를 내므로, 긴 비밀번호로 가입·로그인하면 처리되지 않은 예외가 난다. 비밀번호 최대 길이 검증이 없다.
- 제안: 길이 상한을 검사하거나 사전 해시를 적용한다.

### 8. 이메일 중복 가입 경합, 이메일 열거, 로그인 시도 제한 없음
- 위치: `src/auth.py:31-34, 46`
- 문제:
  - `email_exists` 확인과 `create_user` 사이에 경합이 있다. 동시 가입 시 UNIQUE 위반(`UniqueViolation`)을 잡지 않아 예외가 난다.
  - "이미 가입된 이메일입니다" 메시지로 가입 여부가 드러난다(PRD는 로그인에만 이 원칙을 둠).
  - 로그인 시도 횟수 제한이 없다. 사용자가 없을 때 bcrypt를 건너뛰어 응답 시간 차이가 난다.

### 9. 인식·추천 API를 누구나 무제한으로 호출 가능
- 위치: `src/app.py:296-353, 450-453, 465-`
- 문제: 로그인 없이 호출할 수 있다. 인식에는 쿨다운이 없고, 레시피 쿨다운은 `session_state` 기반이라 새로고침으로 우회된다.
- 시나리오: 한 사용자가 서비스 소유자의 OpenRouter 무료 일일 한도를 소진하면 다른 모든 사용자가 429를 받는다(app.py:67 분기 참조).
- 제안: 서버 측 제한(전역·IP·계정 단위)이나 로그인 필수화를 검토한다.

### 10. `unsafe_allow_html=True`에 재료 이름을 이스케이프 없이 삽입
- 위치: `src/app.py:419`
- 문제: 재료 이름은 사용자 입력이거나 모델 출력이다. 사진 속 글자로 프롬프트를 주입해 HTML을 섞을 수 있다. 최소한 마크업이 깨지고, 스크립트 실행 가능 여부는 Streamlit의 sanitize 동작에 달려 있다(확인 필요). 같은 세션 사용자에게만 보이므로 영향 범위는 제한적이다.
- 제안: `html.escape(name)`을 적용한다.

### 11. 연결 풀 고갈 시 풀 전체를 폐기함
- 위치: `src/db.py:57-71`
- 문제: 동시 사용이 5개를 넘으면 `getconn`이 `PoolError`를 낸다. `PoolError`는 `psycopg2.Error`의 하위 클래스라 `except psycopg2.Error` 분기로 들어가고, `pool.closeall()`이 다른 스레드가 사용 중인 연결까지 닫는다.
- 시나리오: 접속이 몰리면 정상 연결까지 연쇄적으로 끊어진다.
- 제안: `PoolError`는 대기·재시도로 따로 처리하고, 풀 폐기는 `SELECT 1` 실패 시에만 한다.

### 12. 레시피 필드(`cook_time_minutes`, `used_ingredients`) 미검증
- 위치: `src/db.py:237`, `src/app.py:550, 569`, `src/pages/1_내_레시피.py:28-29`
- 문제: 모델이 `"20분"` 같은 문자열을 주면 INTEGER 컬럼 INSERT가 실패해 저장 버튼에서 예외가 난다. null이면 "약 None분"이 표시된다(`.get` 기본값 `'?'`은 키가 없을 때만 적용). `used_ingredients`에 문자열이 아닌 원소가 있으면 `", ".join`이 TypeError를 낸다.
- 제안: `parse_recipes`에서 레시피 스키마를 검증·정규화한다.

### 13. 프로필 페이지의 모델 이름 역매핑과 None 처리
- 위치: `src/pages/2_프로필.py:46, 112-118`
- 문제: `name_to_id`가 표시 이름 기준이라, 이름이 같은 모델이 둘 있으면 한 id로 덮어써진다. 46행은 `saved`가 None이면(사용자 행이 사라진 경우) `saved.get`에서 AttributeError가 난다.
- 제안: 행 인덱스나 id 컬럼으로 매핑하고, None을 처리한다.

### 14. 폴백 재시도 정책의 불일치
- 위치: `src/vision.py:106~`, `src/recipe.py:93~, 189`, `src/app.py:526`
- 문제: 401(키 오류)·400 같은 영구 오류도 3회 재시도하며 2초+4초를 기다린다. recipe.py는 200 응답이 한국어가 아니면 재시도하지만, app.py의 최종 처리는 한글 검사 없이 통과시켜 3회 모두 영어면 영어 레시피가 그대로 표시된다.
- 제안: 영구 오류는 바로 중단하고, 언어 검사 정책을 한 곳으로 통일한다.

### 15. [문서] CLAUDE.md가 실제 구조와 어긋남
- "src/에는 test_api.py만 있다", "빌드 시스템·패키지 매니페스트 없음", "실행: `python3 src/test_api.py`"는 사실과 다르다. 실제로는 app.py, auth.py, auth_ui.py, db.py, recipe.py, vision.py, pages/, requirements.txt가 있다.
- 실행 명령은 `streamlit run src/app.py`이며 `.claude/launch.json`에 있다(`runtimeExecutable`이 `python3`라 Windows에서 맞지 않을 수 있음).
- CLAUDE.md가 스스로 요구한 업데이트(언어/프레임워크, 설치 방법, PRD 단계와 모듈 대응)가 이행되지 않았다. 루트의 `screenshot/` 폴더도 문서에 없다.

### 16. test_api.py가 "실행 시 확인·보고" 규칙과 키 로딩 패턴을 따르지 않음
- 위치: `src/test_api.py:7, 33-34, 51-52`
- 문제:
  - 상태 코드와 본문을 출력만 하고 성공/실패 판정이나 문제 요약이 없다. 타임아웃·네트워크 예외 처리도 없다.
  - `os.environ["OPENROUTER_API_KEY"]`(7행)는 import 시점에 KeyError를 내고 `st.secrets` 폴백이 없다.
  - 모델이 `google/gemma-4-31b-it:free`로 고정되어 있는데, vision.py 주석상 이 모델은 429가 반복된다. 현행 동적 모델 선택과 다르다.
  - pytest가 `test_*.py`로 수집하면 네트워크 호출이 일어난다.
- 참고: 앱은 사용 모델과 오류를 UI로 보여 주지만, 시작 시 API 상태를 자동 점검하는 로직은 없다.

---

## 낮음

### 17. vision.py와 recipe.py의 대량 중복
- 다음 항목이 두 모듈에 거의 동일하게 복제되어 있다: `API_URL`, `MODELS_URL`, `_EXCLUDE_KEYWORDS`, `_fetch_models_payload`, `prioritize_models`, 재시도 루프, 모델 상세 목록 함수, 안전망 상수.
- `_fetch_models_payload`는 `@st.cache_data` 함수가 두 개라 캐시도 두 개가 되고, 모델 목록을 두 번 조회한다.
- app.py의 오류 분기(330-353과 499-538)도 거의 같은 코드다.
- 공통 모듈(예: `openrouter.py`)로 합치면 1·2번 수정도 한 곳에서 끝난다.

### 18. 세션 초기화 키 목록 중복·불일치
- 위치: `src/app.py:36-53`, `src/auth_ui.py:10-26`
- `APP_FLOW_STATE_KEYS`에 `used_recipe_model`이 없어 로그아웃 후에도 이전 사용자의 모델 표시가 남는다. Google 로그아웃은 `st.logout()`만 호출하고 이 키들을 지우지 않는다(세션이 새로 시작되는지 확인 필요). 키 목록을 한 곳에서 공유한다.

### 19. `m.get("architecture", {})`가 None이면 AttributeError
- 위치: `src/vision.py:79`. 키는 있고 값이 null인 경우다.

### 20. 업로드 이미지의 EXIF 회전 미적용
- 위치: `src/vision.py:128`. `ImageOps.exif_transpose`가 없어 폰 사진이 옆으로 누운 채 모델에 전달될 수 있다.

### 21. 레시피 생성 실패 메시지가 rerun 후 사라짐
- 위치: `src/app.py:508-538`. 인식 오류(358-362)와 달리 `session_state`에 저장하지 않는다. 쿨다운 안내 문구도 다른 조작을 해야 갱신된다.

### 22. 인식 결과가 빈 배열이면 "변환하지 못했습니다" 오류로 표시
- 위치: `src/vision.py:250`. 실제 의미는 "인식된 재료 없음"이다.

### 23. 목록에 없는 단위가 조용히 "개"로 바뀜
- 위치: `src/app.py:160-168`. 사용자는 이 변경을 알 수 없다.

### 24. requirements.txt 버전 미고정, 미사용 의존성 (확인 필요)
- 버전이 고정되어 있지 않고, `httpx`는 코드에서 사용하지 않는다. `st.login`에는 Authlib과 Streamlit 최소 버전이 필요하므로 버전 범위를 명시한다.

### 25. Google 이메일 대소문자 미정규화
- 위치: `src/auth.py:22`, `src/db.py:164`. 이메일 가입·로그인은 `lower()`를 쓰지만 Google 경로는 쓰지 않아, 대소문자 차이로 중복 계정이 생길 수 있다.

### 26. DB 스키마
- `saved_recipes.user_id`에 인덱스가 없다.
- `created_at`/`saved_at`이 TEXT다. ISO 문자열이라 정렬은 맞지만 `timestamptz`가 낫다.
- 저장 버튼을 반복 클릭하면 중복 저장된다(app.py:579-585).

### 27. PRD와 구현의 불일치 (문서 갱신 필요)
- F1-2/F2-2는 `gemma-4-31b-it:free`, `gpt-oss-20b:free`로 모델을 고정했지만, 구현은 무료 모델을 동적으로 조회·우선순위화하고 사용자가 선택하게 한다.
- step1의 출력 스키마는 문자열 배열인데, 구현은 `{name, quantity, unit, _id}` 객체다.
- step3은 SQLite/JWT 예시이고 OAuth를 범위 외로 두었지만, 구현은 Postgres(Supabase)와 Google 로그인이다.
- PRD에 없는 기능이 있다: 프로필 기본 조건, 모델 선택, 위저드.
- PRD 5.2의 "파싱 실패 시 원문 노출"은 expander로 구현되어 있다. 429/5xx의 "재시도 버튼"은 별도 버튼 없이 기존 버튼을 다시 누르는 방식이다.

### 28. code-reviewer 에이전트의 비밀 파일 접근 제한이 프롬프트 지시뿐임
- 위치: `.claude/agents/code-reviewer.md`. tools에 Read/Grep/Glob이 있어 실수로 `.env`를 읽을 수 있다. settings의 permissions deny 규칙으로도 막는 편이 안전하다.

### 29. .gitignore
- `.env`, `.env.*`(`.env.example` 예외), `.streamlit/secrets.toml`, `/secrets.toml`은 적절하다.
- `.venv/` 등이 없다. `screenshot/` 이미지에 개인정보나 키가 포함됐는지는 확인하지 않았다(확인 필요).

---

## 권장 우선순위

1. 높음 1·2(폴백·응답 파싱): 17번의 공통 모듈 추출과 함께 처리
2. 높음 6(Google 계정 연결 보안)
3. 높음 3·4(이미지·수량 입력 방어), 5(실행 확인)
4. 중간 7~12(인증·사용량 제한·풀·스키마 검증)
5. 문서 정리(15, 16, 27)

## 검토한 파일

- `src/app.py`, `auth.py`, `auth_ui.py`, `db.py`, `recipe.py`, `vision.py`, `test_api.py`
- `src/pages/1_내_레시피.py`, `src/pages/2_프로필.py`
- `requirements.txt`, `.gitignore`, `.env.example`, `.streamlit/config.toml`
- `.claude/launch.json`, `.claude/agents/code-reviewer.md`
- `docs/PRD_step1.md`, `PRD_step2.md`, `PRD_step3.md`
- `CLAUDE.md` (세션 컨텍스트로 제공된 내용 기준)

## 일부러 열지 않은 파일

- `.env`, `.streamlit/secrets.toml`, 루트 `secrets.toml` (비밀 값 보호)
- `docs/*.pdf`, `screenshot/` 이미지, `.git/` 내부
