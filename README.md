# get_cancelticket_nol

NOL·인터파크 야구 예매의 로그인, 대기열 진입, 좌석 조회·선점과 텔레그램 알림을 처리하는 Python 프로젝트입니다. 결제는 브라우저에서 직접 마무리합니다.

## 설치

Python 3.11 이상과 `uv`를 사용합니다. 저장소 폴더에서 실행하세요.

```powershell
python -m pip install uv
uv sync
uv run python -m patchright install chromium
Copy-Item .env.example .env
```

## 설정

- `line_up.py`: 경기 코드(`GOODS_CODE`), 장소 코드(`PLACE_CODE`) 등 예매 대상
- `booking.py`: 선호 블록(`PREFER_BLOCKS`), 최대·최소 매수, 좌석 선택 모드
- `browser_config.py`: 브라우저 선택 및 세션 파일 경로

`USE_BUNDLED_CHROMIUM = True`이면 Patchright용 Chromium을 사용합니다. `False`이면 설치된 Chrome을 찾고, 없으면 Patchright용 Chromium을 사용합니다. `CHROME_EXECUTABLE`에 직접 지정한 경로가 있으면 이 설정이 최우선입니다.

텔레그램 알림을 사용하려면 직접 만든 봇의 채팅에서 `/start`를 보낸 뒤 `.env`를 채웁니다.

```dotenv
TELEGRAM_BOT_TOKEN=발급받은_봇_토큰
TELEGRAM_CHAT_ID=알림을_받을_채팅_ID
```

실제 값은 따옴표 없이 입력해도 됩니다. 환경변수에 같은 설정이 있으면 환경변수가 우선합니다. 두 값이 없으면 알림을 생략합니다.

## 실행

```powershell
uv run python login.py
uv run python line_up.py
```

`login.py`에서 브라우저로 로그인한 뒤 터미널에서 Enter를 누르면 세션을 저장합니다. `line_up.py`에서도 인증이 필요하면 로그인을 안내하고, 인증 성공 확인 직후 세션을 저장합니다.

`browser_session.json`은 실행 위치와 무관하게 이 프로젝트 폴더에서 읽고 저장합니다. 저장된 세션의 인증이 만료되면 다시 로그인해야 합니다.

좌석 선점 성공이 확인되면 좌석 정보와 결제 안내를 텔레그램으로 보냅니다. 실패 또는 결과 불명 상황은 콘솔에 표시합니다. 결과 불명 시 자동 진행을 멈추므로 브라우저에서 직접 상태를 확인하세요. 전송의 일시적 오류는 총 3회까지 시도하며, 응답 유실 후 재시도에서는 중복 알림이 발생할 수 있습니다.

보조 도구:

- `booking.py`: 좌석 예매 단계 단독 실행
- `seat.py`: 블록별 좌석 상태 조회
- `capture_har.py`: 브라우저 네트워크 캡처

## 검증

```powershell
uv run python -m unittest discover -s tests -v
```

알림 테스트는 모의 요청을 사용하며 실제 메시지를 보내지 않습니다.

## 로컬 파일

`.env`, 로그인 세션, HAR 캡처, 캡차 산출물 및 가상환경은 Git에서 제외합니다. 공유용 설정 예시는 `.env.example`에 있습니다.
