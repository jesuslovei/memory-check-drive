import csv, io, json, os, re, uuid, tempfile
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ====== 설정 ======
THRESHOLD = float(os.getenv("THRESHOLD", "0.85"))
OPENAI_MODEL = os.getenv("OPENAI_STT_MODEL", "gpt-4o-transcribe")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Google Drive 설정
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "").strip()
GDRIVE_CREDENTIALS_JSON = os.getenv("GDRIVE_CREDENTIALS_JSON", "")
GDRIVE_PUBLIC_LINK = os.getenv("GDRIVE_PUBLIC_LINK", "false").lower() == "true"

# 로컬 저장 위치
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ====== 유틸 ======
def normalize_korean(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[\s\.,!?:;\-—“”\"'()\[\]·…]", "", s)
    return s

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_korean(a), normalize_korean(b)).ratio()

def today_folder() -> str:
    return datetime.now().strftime("%Y%m%d")

# ====== 구절 로드 ======
with open("verses.json", "r", encoding="utf-8") as f:
    _raw = json.load(f)

# 아래 형식 모두 지원:
# 1) {"verse_id": "...", "text": "..."} (단일)
# 2) [{"verse_id": "...", "text": "..."}, {...}] (배열)
# 3) {"verses": [ {...}, {...} ]} (래핑)
if isinstance(_raw, dict) and "verses" in _raw:
    VERSES = _raw["verses"]
elif isinstance(_raw, dict) and "text" in _raw:
    VERSES = [_raw]
elif isinstance(_raw, list):
    VERSES = _raw
else:
    raise RuntimeError("verses.json 형식을 확인하세요. 'text' 포함 객체 또는 'verses' 배열 형식이어야 합니다.")

# ====== OpenAI STT ======
def stt_with_openai(audio_bytes: bytes) -> str:
    from openai import OpenAI
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 미설정")
    client = OpenAI(api_key=OPENAI_API_KEY)
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        path = tmp.name
    with open(path, "rb") as f:
        res = client.audio.transcriptions.create(
            model=OPENAI_MODEL,
            file=f,
        )
    return getattr(res, "text", "") or ""

# ====== Google Drive 업로드 ======
_drive_service = None

def get_drive_service():
    global _drive_service
    if _drive_service is not None:
        return _drive_service
    if not GDRIVE_CREDENTIALS_JSON:
        raise RuntimeError("GDRIVE_CREDENTIALS_JSON 환경변수가 비어 있습니다.")
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    info = json.loads(GDRIVE_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/drive.file"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service

def drive_upload_file(local_path: Path, mime_type: str = "application/octet-stream") -> dict:
    if not GDRIVE_FOLDER_ID:
        raise RuntimeError("GDRIVE_FOLDER_ID 환경변수가 비어 있습니다.")
    from googleapiclient.http import MediaFileUpload
    service = get_drive_service()
    file_metadata = {
        "name": local_path.name,
        "parents": [GDRIVE_FOLDER_ID],
    }
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)
    created = service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink, webContentLink").execute()
    file_id = created.get("id")

    # 공개 링크 부여 (선택)
    view_link = created.get("webViewLink")
    if GDRIVE_PUBLIC_LINK and file_id:
        try:
            service.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
            ).execute()
            # 권한 반영 후 링크 다시 조회
            created = service.files().get(fileId=file_id, fields="id, webViewLink, webContentLink").execute()
            view_link = created.get("webViewLink")
        except Exception:
            pass

    return {"id": file_id, "webViewLink": view_link, "webContentLink": created.get("webContentLink")}

# ====== FastAPI ======
app = FastAPI(title="제자반 암송 자동확인 (Google Drive)")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
def root():
    return FileResponse("static/index.html")

@app.post("/submit")
async def submit_audio(
    audio: UploadFile = File(...),
    name: str = Form(default="익명"),
):
    # 0) 업로드 파일 로컬 저장
    content = await audio.read()
    unique = uuid.uuid4().hex[:8]
    filename = f"{name}_{datetime.now().strftime('%H%M%S')}_{unique}.webm"
    folder = UPLOAD_DIR / today_folder()
    folder.mkdir(parents=True, exist_ok=True)
    local_path = folder / filename
    with open(local_path, "wb") as f:
        f.write(content)

    # 1) STT
    try:
        transcript = stt_with_openai(content)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"STT 실패: {e}"}, status_code=500)

    # 2) 유사도 & 판정
    score = similarity(transcript, VERSE["text"])
    passed = score >= THRESHOLD

    # 3) Google Drive 업로드 (음성 파일)
    drive_file = None
    try:
        drive_file = drive_upload_file(local_path, mime_type="audio/webm")
    except Exception as e:
        # 드라이브 업로드 실패해도 로컬 저장은 됨
        drive_file = {"error": str(e)}

    # 4) 로그 저장(CSV) → 드라이브에도 복사
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "name": name,
        "verse_id": VERSE.get("verse_id", ""),
        "target_text": VERSE.get("text", ""),
        "transcript": transcript,
        "score": f"{score:.3f}",
        "passed": "Y" if passed else "N",
        "file_local": str(local_path),
        "file_drive_id": (drive_file or {}).get("id", ""),
        "file_drive_link": (drive_file or {}).get("webViewLink", ""),
    }
    write_header = not os.path.exists("submissions.csv")
    with open("submissions.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if write_header:
            w.writeheader()
        w.writerow(row)

    # 드라이브에 submissions.csv 업로드(이력 보존을 위해 같은 이름으로 계속 새 버전 생성)
    try:
        drive_upload_file(Path("submissions.csv"), mime_type="text/csv")
    except Exception:
        pass

    # 결과 응답
    file_link = (drive_file or {}).get("webViewLink") or ""
    # 공개 안했으면 링크가 열리지 않을 수 있음
    return {
        "ok": True,
        "name": name,
        "verse_id": VERSE.get("verse_id", ""),
        "score": round(score, 3),
        "passed": passed,
        "transcript": transcript,
        "file_link": file_link,
    }
