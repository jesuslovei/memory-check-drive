import csv, io, json, os, re, uuid, tempfile
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ========================= 설정 ============================
THRESHOLD = float(os.getenv("THRESHOLD", "0.85"))  # 합격 기준 점수
OPENAI_MODEL = os.getenv("OPENAI_STT_MODEL", "gpt-4o-transcribe")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # OpenAI 키 필요

# Google Drive 설정
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "").strip()
GDRIVE_CREDENTIALS_JSON = os.getenv("GDRIVE_CREDENTIALS_JSON", "")
GDRIVE_PUBLIC_LINK = os.getenv("GDRIVE_PUBLIC_LINK", "false").lower() == "true"

# 업로드 폴더
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# ===========================================================
def normalize_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[\s\.,!?:;\-—“”\"'()\[\]·…]", "", s)
    return s

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()

with open("verses.json", "r", encoding="utf-8") as f:
    _raw = json.load(f)

VERSES = _raw["verses"] if "verses" in _raw else _raw

# ================= OpenAI STT =============================
def speech_to_text(audio_bytes: bytes, language="kr") -> str:
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(audio_bytes); tmp.flush(); path = tmp.name
    with open(path, "rb") as f:
        res = client.audio.transcriptions.create(model=OPENAI_MODEL, file=f)
    return res.text

# ================= Google Drive ===========================
_drive_service = None
def get_drive_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    global _drive_service
    if _drive_service: return _drive_service
    creds = Credentials.from_service_account_info(json.loads(GDRIVE_CREDENTIALS_JSON))
    _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service

def upload_to_drive(path: Path):
    from googleapiclient.http import MediaFileUpload
    service = get_drive_service()
    file_metadata = {"name": path.name, "parents": [GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(str(path), mimetype="audio/webm")
    file = service.files().create(body=file_metadata, media_body=media, fields="id,webViewLink").execute()
    if GDRIVE_PUBLIC_LINK:
        service.permissions().create(fileId=file["id"], body={"role": "reader", "type": "anyone"}).execute()
    return file.get("webViewLink", "")

# ================= FastAPI ================================
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse("static/index.html")

@app.get("/verses")
def get_verses():
    return {"verses": VERSES}

@app.post("/submit")
async def submit(audio: UploadFile = File(...), name: str = Form(...), lang: str = Form("kr")):
    if lang not in ("kr", "en"):
        return JSONResponse({"error": "invalid language"}, status_code=400)

    # ---- 파일저장 ----
    unique = uuid.uuid4().hex[:6]
    filename = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{name}-{unique}.webm"
    save_path = UPLOAD_DIR / filename
    with open(save_path, "wb") as f:
        f.write(await audio.read())

    # ---- STT ----
    transcript = speech_to_text(save_path.read_bytes())

    # ---- 구절 검사 ----
    scores = []
    for v in VERSES:
        text = v["kr"] if lang == "kr" else v["en"]
        score = similarity(transcript, text)
        scores.append({"verse": v["verse_id"], "score": round(score, 3)})
    passed = all(s["score"] >= THRESHOLD for s in scores)

    # ---- 드라이브 업로드 ----
    drive_url = upload_to_drive(save_path)

    # ---- CSV 로그 ----
    with open("submissions.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([datetime.now(), name, lang, transcript, scores, passed, drive_url])

    return {"name": name, "lang": lang, "transcript": transcript, "scores": scores, "passed": passed, "file": drive_url}
