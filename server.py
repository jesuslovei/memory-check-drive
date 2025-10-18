import csv, json, os, re, uuid
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ============ 환경설정 ============
THRESHOLD = float(os.getenv("THRESHOLD", "0.85"))  # 합격 기준

# Google Drive
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "").strip()
GDRIVE_CREDENTIALS_JSON = os.getenv("GDRIVE_CREDENTIALS_JSON", "")
GDRIVE_PUBLIC_LINK = os.getenv("GDRIVE_PUBLIC_LINK", "false").lower() == "true"

# 업로드 폴더
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# ============ 유틸 함수 ============
def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[\s\.,!?:;\-—“”\"'()\[\]·…]", "", s)
    return s

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()

# ============ 구절 로드 ============
with open("verses.json", "r", encoding="utf-8") as f:
    raw = json.load(f)

WEEK_ID = raw.get("week_id", datetime.now().strftime("%Y-%m-%d"))
WEEK_TITLE = raw.get("title", WEEK_ID)
VERSES = raw["verses"]

# ============ Google Speech-To-Text ============
from google.cloud import speech
from google.oauth2.service_account import Credentials

def stt(audio_bytes: bytes, lang_hint: Optional[str] = None, mime_hint: Optional[str] = None) -> str:
    creds_json = os.getenv("GCP_SPEECH_CREDENTIALS_JSON")
    creds = Credentials.from_service_account_info(json.loads(creds_json))
    client = speech.SpeechClient(credentials=creds)

    lang_map = {"kr": "ko-KR", "en": "en-US"}
    language_code = lang_map.get((lang_hint or "").lower(), "ko-KR")

    enc = speech.RecognitionConfig.AudioEncoding.ENCODING_UNSPECIFIED
    if mime_hint:
        mh = mime_hint.lower()
        if "webm" in mh:
            enc = speech.RecognitionConfig.AudioEncoding.WEBM_OPUS
        elif "ogg" in mh:
            enc = speech.RecognitionConfig.AudioEncoding.OGG_OPUS

    config = speech.RecognitionConfig(
        encoding=enc,
        language_code=language_code,
        enable_automatic_punctuation=True,
        model="latest_long",
    )
    audio = speech.RecognitionAudio(content=audio_bytes)
    resp = client.recognize(config=config, audio=audio, timeout=90)

    return " ".join(r.alternatives[0].transcript for r in resp.results if r.alternatives).strip()
# ============ Google Drive 업로드 ============
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

_drive = None

def _drive_client():
    global _drive
    if _drive:
        return _drive
    if not GDRIVE_CREDENTIALS_JSON:
        raise RuntimeError("🚨 GDRIVE_CREDENTIALS_JSON 환경변수가 비어있습니다.")
    creds = Credentials.from_service_account_info(json.loads(GDRIVE_CREDENTIALS_JSON))
    _drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive

def _ensure_folder(parent_id: str, name: str) -> str:
    """기존 폴더 검색 → 없으면 생성."""
    svc = _drive_client()
    q = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and '{parent_id}' in parents"
    res = svc.files().list(q=q, fields="files(id)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    folder = svc.files().create(body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}, fields="id").execute()
    return folder["id"]

def upload_file_to_drive(path: Path, week_id: str, mime_type: str):
    """Google Drive에 업로드 (주차별 audio 저장 안정화 버전)."""
    svc = _drive_client()

    # ✅ MIME 자동 보정 (iPhone/Android 대응)
    if not mime_type or mime_type == "":
        mime_type = "application/octet-stream"
    elif "audio/mp4" in mime_type or "mp4" in mime_type or "m4a" in mime_type:
        mime_type = "audio/m4a"
    elif "webm" in mime_type:
        mime_type = "audio/webm"
    elif "ogg" in mime_type:
        mime_type = "audio/ogg"

    try:
        # ✅ audio/week_id 폴더 자동 생성
        week_folder = _ensure_folder(GDRIVE_FOLDER_ID, "audio")
        week_subfolder = _ensure_folder(week_folder, week_id)

        # ✅ 업로드 실행
        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
        file_metadata = {"name": path.name, "parents": [week_subfolder]}

        file = svc.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, webViewLink"
        ).execute()

        # ✅ 공개 링크 옵션
        if GDRIVE_PUBLIC_LINK:
            try:
                svc.permissions().create(
                    fileId=file["id"],
                    body={"type": "anyone", "role": "reader"}
                ).execute()
            except Exception as pe:
                print(f"⚠️ 공개 링크 설정 실패 (무시): {pe}")

        return file.get("webViewLink")

    except Exception as e:
        print(f"🚨 [Drive Upload Error] 파일 업로드 실패: {e}")
        print(f"📌 파일경로: {path}, MIME: {mime_type}, WEEK: {week_id}")
        return ""  # 서버 죽지 않게 안전 처리

# ============ FastAPI 서버 ============
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse("static/index.html")

@app.get("/verses")
def get_verses():
    return {"week_id": WEEK_ID, "title": WEEK_TITLE, "verses": VERSES}

@app.post("/submit")
async def submit_audio(
    audio: UploadFile = File(...),
    name: str = Form(...),
    lang: str = Form("kr"),
    verse_idx: int = Form(...),
    week_id: str = Form(WEEK_ID),
    mime: str = Form(None)
):
    # 1. 파일 저장 (원본 유지)
    data = await audio.read()
    safe_name = name.replace(" ", "_")
    verse_label = VERSES[int(verse_idx)]["verse_id"].replace(" ", "").replace(":", "_")
    today = datetime.now().strftime("%Y-%m-%d")
    lang_tag = "KR" if lang == "kr" else "EN"
    ext = ".m4a" if "mp4" in (mime or "") else ".webm"
    filename = f"{safe_name}-{verse_label}-{today}-{lang_tag}{ext}"
    filepath = UPLOAD_DIR / filename
    filepath.write_bytes(data)

    # 2. STT
    transcript = stt(data, lang_hint=lang, mime_hint=mime)

    # 3. 채점
    target_text = VERSES[int(verse_idx)][lang]
    score = round(_similarity(transcript, target_text), 3)
    passed = score >= THRESHOLD

    # 4. Google Drive 업로드
    try:
        file_link = upload_file_to_drive(filepath, week_id, mime or "application/octet-stream")
    except Exception:
        file_link = ""

    # 5. CSV 기록
    row = [
        datetime.now().isoformat(timespec="seconds"),
        week_id,
        name,
        VERSES[int(verse_idx)]["verse_id"],
        score,
        "PASS" if passed else "FAIL",
        transcript,
        file_link
    ]
    write_header = not Path("submissions.csv").exists()
    with open("submissions.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["time", "week", "name", "verse", "score", "result", "transcript", "drive_link"])
        w.writerow(row)

    return {
        "ok": True,
        "week_id": week_id,
        "file_url": file_link,
        "transcript": transcript,
        "score": score,
        "passed": passed,
    }

# ======== 서버 실행 ========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
