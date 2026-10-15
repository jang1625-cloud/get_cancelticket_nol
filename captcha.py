"""캡차 오디오를 STT(음성인식)로 자동 판독한다.

흐름:
  1) POST /onestop/api/captcha/image   → 응답 {"Img": "...", "EncRnd": "..."}
       - 화면용 이미지(Img)와 오디오 요청용 토큰(EncRnd)을 받는다.
  2) GET  GetCaptchaAudio?v=en&t=<EncRnd>  → audio/wav 바이너리
  3) SpeechRecognition(recognize_google) 로 wav → 텍스트 인식
       - 공백 제거 + 대문자화 (캡차는 대소문자 구분 없음)

주의:
  - recognize_google 은 인터넷으로 구글 STT 를 호출한다(무료, 키 불필요, 호출 제한 있음).
  - 오디오가 표준 PCM WAV 가 아니면 sr.AudioFile 이 못 읽을 수 있어, 그 경우
    audioop/wave 로 재저장하거나 형식 변환이 필요할 수 있다(현재는 원본 그대로 시도).
"""

import asyncio
import os
import tempfile
from urllib.parse import quote

import speech_recognition as sr

TICKETS_ORIGIN = "https://tickets.interpark.com"
CAPTCHA_IMAGE_URL = f"{TICKETS_ORIGIN}/onestop/api/captcha/image"
CAPTCHA_AUDIO_URL = "https://aspseat-ticket.interpark.com/CommonAPI/Captcha/GetCaptchaAudio"

# 오디오 파일 저장 폴더
CAPTCHA_DIR = "captcha"


async def fetch_captcha_image(req):
    """POST captcha/image → {"Img", "EncRnd"} 반환. EncRnd 는 오디오 요청 토큰."""
    res = await req.post(
        CAPTCHA_IMAGE_URL,
        headers={"x-requested-with": "XMLHttpRequest", "referer": f"{TICKETS_ORIGIN}/onestop/seat"},
    )
    if not res.ok:
        raise RuntimeError(f"captcha/image 실패: {res.status}")
    body = await res.json()
    enc_rnd = body.get("EncRnd")
    if not enc_rnd:
        raise RuntimeError(f"captcha/image 응답에 EncRnd 없음: {str(body)[:200]}")
    return body


async def fetch_captcha_audio(req, enc_rnd, save_path, lang="en"):
    """GetCaptchaAudio?v=<lang>&t=<EncRnd> → wav 바이너리 저장. 저장 경로 반환."""
    res = await req.get(
        f"{CAPTCHA_AUDIO_URL}?v={lang}&t={quote(enc_rnd, safe='')}",
        headers={"accept": "*/*", "referer": f"{TICKETS_ORIGIN}/"},
    )
    if not res.ok:
        raise RuntimeError(f"GetCaptchaAudio 실패: {res.status}")
    data = await res.body()
    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(data)
    return save_path


async def recognize_wav(wav_path):
    """wav 파일을 STT 로 인식해 '공백제거 + 대문자' 문자열로 반환."""
    loop = asyncio.get_event_loop()

    def _read_audio():
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
        text = recognizer.recognize_google(audio)
        return text.replace(" ", "").upper()

    return await loop.run_in_executor(None, _read_audio)


async def solve_captcha(req, name="captcha", lang="en", enc_rnd=None):
    """캡차 오디오 다운로드 → STT 판독. EncRnd 를 알면(후킹으로 가로챈 경우) 재사용하고,
    없으면 captcha/image 를 직접 POST 해서 얻는다.

    파라미터:
      enc_rnd : 이미 확보한 EncRnd(선택). 주면 captcha/image POST 를 생략한다.

    반환: {
      "ok": bool,
      "text": "ABC123",         # 인식 결과(대문자, 공백제거). 실패 시 None
      "encRnd": "...",           # 사용한 EncRnd
      "img": "data:image/...",   # 화면용 이미지(직접 POST 한 경우만)
      "wavPath": "captcha/xxx.wav",
      "error": "..."             # 실패 시 사유
    }
    """
    result = {"ok": False, "text": None, "encRnd": enc_rnd, "img": None, "wavPath": None, "error": None}
    try:
        if not enc_rnd:
            image = await fetch_captcha_image(req)
            result["encRnd"] = image.get("EncRnd")
            result["img"] = image.get("Img")

        wav_path = os.path.join(CAPTCHA_DIR, f"{name}.wav")
        await fetch_captcha_audio(req, result["encRnd"], wav_path, lang=lang)
        result["wavPath"] = wav_path

        text = await recognize_wav(wav_path)
        result["text"] = text
        result["ok"] = bool(text)
        if not text:
            result["error"] = "STT 결과가 비어 있음"
    except sr.UnknownValueError:
        result["error"] = "STT 인식 실패(음성을 이해하지 못함)"
    except sr.RequestError as e:
        result["error"] = f"STT 요청 실패(네트워크/구글 API): {e}"
    except Exception as e:
        result["error"] = str(e)
    return result


# 단독 실행: 브라우저 세션으로 캡차 오디오를 받아 STT 판독 결과를 출력.
async def _main():
    from patchright.async_api import async_playwright
    from browser_config import SESSION_PATH, get_launch_options

    async with async_playwright() as p:
        browser = await p.chromium.launch(**get_launch_options())
        opts = {
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            "locale": "ko-KR",
        }
        if os.path.exists(SESSION_PATH):
            opts["storage_state"] = SESSION_PATH
            print(f"📂 세션 파일 사용(로그인 유효 여부는 별도 확인): {SESSION_PATH}")
        else:
            print(f"ℹ️ 세션 파일 없음 — 새 세션 시작: {SESSION_PATH}")
        context = await browser.new_context(**opts)
        page = await context.new_page()
        await page.goto(f"{TICKETS_ORIGIN}/onestop/seat", wait_until="domcontentloaded", timeout=30_000)

        res = await solve_captcha(page.request, name="test")
        print("\n=== 캡차 STT 결과 ===")
        print(f"  ok      : {res['ok']}")
        print(f"  text    : {res['text']}")
        print(f"  wavPath : {res['wavPath']}")
        print(f"  error   : {res['error']}")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(_main())
