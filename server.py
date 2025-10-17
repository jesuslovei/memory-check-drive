import csv, json, os, re, uuid, tempfile
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ===== 설정 =====
THRESHOLD = 0.85
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-4o-transcribe"

# Google Drive
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")
GDRIVE_CREDENTIALS_JSON = os.getenv("GDRIVE_CREDENTIALS_JSON")

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# ===== 유틸 =====
def clean_text(s):
    return re.sub(r"[\s\.,!?:;\"'\-—()\[\]]", "", s.lower())

def score(a, b):
    return SequenceMatcher(None, clean_text(a), clean_text(b)).ratio()

# ===== 구절 로드 =====
with open("verses.json", "r", encoding="utf-8") as f:
    VERSES = json.load(f)["verses"]

# ===== STT =====
def stt(audio_bytes):
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        path = tmp.name
    with open(path, "rb") as f:
        result = client.audio.transcriptions.create(
            model=OPENAI_MODEL,
            file=f
        )
    return result.text

# ===== Google Drive =====
_drive_service = None
def drive():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    global _drive_service
    if not _drive_service:
        creds = Credentials.from_service_account_info(json.loads(GDRIVE_CREDENTIALS_JSON))
        _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service

def upload_to_drive(path):
    from googleapiclient.http import MediaFileUpload
    file_meta = {"name": path.name, "parents": [GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(path, resumable=True)
    file = drive().files().create(
        body=file_meta, media_body=media, fields="id, webViewLink"
    ).execute()
    return file.get("webViewLink")

# ===== 서버 시작 =====
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse("static/index.html")

@app.get("/verses")
def verses():
    return {"verses": VERSES}

@app.post("/submit")
async def submit(audio: UploadFile = File(...), name: str = Form(...), lang: str = Form(...)):
    audio_bytes = await audio.read()
    filename = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{name}-{uuid.uuid4().hex[:6]}.webm"
    path = UPLOAD_DIR / filename
    path.write_bytes(audio_bytes)

    transcript = stt(audio_bytes)
    verse_scores = []
    for v in VERSES:
        target = v["kr"] if lang == "kr" else v["en"]
        verse_scores.append({
            "verse": v["verse_id"],
            "score": round(score(transcript, target), 3)
        })
    passed = all(v["score"] >= THRESHOLD for v in verse_scores)

    drive_link = upload_to_drive(path)

    with open("submissions.csv", "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([name, lang, transcript, verse_scores, passed, drive_link])

    return {
        "ok": True,
        "name": name,
        "scores": verse_scores,
        "passed": passed,
        "file": drive_link,
        "transcript": transcript
    }
