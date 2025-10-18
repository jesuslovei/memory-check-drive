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
    """공유 드라이브 대응: supportsAllDrives / corpora / driveId 사용"""
    svc = _drive_client()
    # parent_id가 Shared Drive 내부 폴더이므로, 그 드라이브 ID를 추출해 주면 가장 정확합니다.
    # 간단하게는 includeItemsFromAllDrives=True, supportsAllDrives=True만으로도 동작합니다.
    q = (
        "mimeType='application/vnd.google-apps.folder' "
        f"and name='{name}' and '{parent_id}' in parents and trashed=false"
    )
    res = svc.files().list(
        q=q,
        fields="files(id,name,parents)",
        pageSize=1,
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        corpora="allDrives",
    ).execute()
    items = res.get("files", [])
    if items:
        return items[0]["id"]

    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    created = svc.files().create(
        body=meta,
        fields="id",
        supportsAllDrives=True
    ).execute()
    return created["id"]

def upload_file_to_drive(path: Path, week_id: str, mime_type: str):
    """Shared Drive 업로드 안정화 버전."""
    svc = _drive_client()

    # MIME 보정(원본 유지)
    if not mime_type:
        mime_type = "application/octet-stream"
    elif "audio/mp4" in mime_type or "mp4" in mime_type or "m4a" in mime_type:
        mime_type = "audio/m4a"
    elif "webm" in mime_type:
        mime_type = "audio/webm"
    elif "ogg" in mime_type:
        mime_type = "audio/ogg"

    try:
        # audio / week_id 하위 폴더 보장
        audio_root = _ensure_folder(GDRIVE_FOLDER_ID, "audio")
        week_folder = _ensure_folder(audio_root, week_id)

        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
        meta = {"name": path.name, "parents": [week_folder]}

        file = svc.files().create(
            body=meta,
            media_body=media,
            fields="id, webViewLink, parents",
            supportsAllDrives=True
        ).execute()

        if GDRIVE_PUBLIC_LINK:
            try:
                svc.permissions().create(
                    fileId=file["id"],
                    body={"type": "anyone", "role": "reader"},
                    supportsAllDrives=True
                ).execute()
            except Exception as pe:
                print(f"⚠️ 공개 링크 설정 실패(무시): {pe}")

        return file.get("webViewLink", "")
    except Exception as e:
        print(f"🚨 [Drive Upload Error] {e}")
        print(f"📌 path={path} mime={mime_type} week={week_id}")
        return ""

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
