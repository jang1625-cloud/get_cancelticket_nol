"""텔레그램 알림. 토큰/채팅ID 는 환경변수 또는 프로젝트 루트의 .env 에서 읽는다(.env 는 gitignore)."""

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
_MAX_ATTEMPTS = 3
_MAX_RETRY_DELAY = 10


def _load_env():
    vals = {}
    try:
        # UTF-8 BOM, 따옴표, 줄 끝 주석을 지원한다. 값 보간은 하지 않는다.
        with open(_ENV_PATH, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                        continue
                    if v.startswith(("'", '"')):
                        match = re.fullmatch(r"(['\"])(.*?)\1\s*(?:#.*)?", v)
                        if not match:
                            print("⚠️ .env 따옴표 형식 오류 — 해당 설정을 건너뜁니다")
                            continue
                        v = match.group(2)
                    else:
                        v = re.split(r"\s+#", v, maxsplit=1)[0].rstrip()
                    vals[k] = v
    except (OSError, UnicodeError):
        pass
    return vals


def _retry_delay(payload, attempt):
    """서버 대기 지시가 너무 길면 즉시 포기하며 더 일찍 재시도하지 않는다."""
    delay = 2 ** (attempt - 1)
    if isinstance(payload, dict):
        params = payload.get("parameters")
        if isinstance(params, dict) and "retry_after" in params:
            value = params["retry_after"]
            if not isinstance(value, (int, float)) or not 0 <= value <= _MAX_RETRY_DELAY:
                return None
            delay = max(delay, value)
    return delay


def _send_sync(text: str) -> bool:
    env = _load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("⚠️ 텔레그램 설정 없음(TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) — 알림 생략")
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        payload = None
        retryable = False
        try:
            with urllib.request.urlopen(
                    f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10) as r:
                payload = json.loads(r.read().decode())
            if isinstance(payload, dict) and payload.get("ok") is True:
                print("📨 텔레그램 전송 완료")
                return True
            code = payload.get("error_code") if isinstance(payload, dict) else None
            retryable = isinstance(code, int) and (code == 429 or 500 <= code < 600)
            print("⚠️ 텔레그램 전송 거절")
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or 500 <= e.code < 600
            try:
                payload = json.loads(e.read().decode())
            except (ValueError, OSError):
                pass
            finally:
                e.close()
            print(f"⚠️ 텔레그램 HTTP 오류: {e.code}")
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            retryable = True
            print("⚠️ 텔레그램 통신 오류")
        except Exception as e:
            # 예외 메시지/URL/응답 본문에는 토큰이 포함될 수 있어 출력하지 않는다.
            print(f"⚠️ 텔레그램 전송 실패: {type(e).__name__}")
        delay = _retry_delay(payload, attempt)
        if not retryable or attempt == _MAX_ATTEMPTS or delay is None:
            break
        print(f"↻ 텔레그램 {delay}초 후 재시도 ({attempt + 1}/{_MAX_ATTEMPTS})")
        time.sleep(delay)
    print("⚠️ 텔레그램 알림 미전송 — 브라우저에서 예매 상태를 확인하세요")
    return False


async def send_telegram(text: str) -> bool:
    return await asyncio.to_thread(_send_sync, text)
