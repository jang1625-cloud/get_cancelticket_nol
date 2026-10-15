"""로그인·대기열·예매 및 보조 도구에서 공유하는 브라우저 설정."""

import os
from pathlib import Path

# 터미널의 현재 폴더와 무관하게 이 프로젝트의 세션만 읽고 저장합니다.
SESSION_PATH = str(Path(__file__).resolve().parent / "browser_session.json")

# 직접 지정한 실행 경로가 최우선입니다. None이면 아래 설정을 따릅니다.
CHROME_EXECUTABLE = None  # 예: r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# False: 설치된 Chrome 자동 탐색, 없으면 Patchright용 Chromium 사용.
# True: Patchright용 Chromium 사용(설치: python -m patchright install chromium).
USE_BUNDLED_CHROMIUM = True


def _chrome_paths():
    paths = [
        Path(os.environ.get("PROGRAMFILES") or "C:/Program Files")
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)") or "C:/Program Files (x86)")
        / "Google/Chrome/Application/chrome.exe",
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        paths.append(Path(local_app_data) / "Google/Chrome/Application/chrome.exe")
    paths.extend([
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
    ])
    return paths


def get_launch_options():
    """지정 경로 → 번들 설정 → 시스템 탐색 순서로 실행 옵션을 만든다."""
    options = {"headless": False}
    if CHROME_EXECUTABLE:
        options["executable_path"] = str(CHROME_EXECUTABLE)
        print(f"🔧 지정한 Chrome 사용: {CHROME_EXECUTABLE}")
    elif USE_BUNDLED_CHROMIUM:
        print("🔧 설정에 따라 Patchright용 Chromium 사용")
    else:
        chrome = next((path for path in _chrome_paths() if path.is_file()), None)
        if chrome:
            options["executable_path"] = str(chrome)
            print(f"🔧 시스템 Chrome 사용: {chrome}")
        else:
            print("ℹ️ 시스템 Chrome 없음 — Patchright용 Chromium 사용")
    return options
