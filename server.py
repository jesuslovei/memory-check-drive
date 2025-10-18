import csv, json, os, re, uuid
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ========================= 설정 ============================
THRESHOLD = float(os.getenv("THRESHOLD", "0.85"))  # 합격 임계치

# Google Drive
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "").strip()
GDRIVE_CREDENTIALS_JSON = os.getenv("GDRIVE_CREDENTIALS_JSON", "")
GDRIVE_PUBLIC_LINK = os.getenv("GDRIVE_PUBLIC_LINK", "false").lower() == "true"

# 업로드 폴더(로컬 보관)
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# ========================= 유틸 ============================
def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[\s\.,!?:;\-—“”\"'()\[\]·…]", "", s)
    return s

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()

# ========================= 구절 로드 ========================
with open("verses.json", "r", encoding="utf-8") as f:
    raw = json.load(f)

WEEK_ID = raw.get("week_id", datetime.now().strftime("%Y-%m-%d"))
WEEK_TITLE = raw.get("title", WEEK_ID)
VERSES = raw["verses"] if isinstance(raw, dict) and "verses" in raw else raw

# =================== Google Cloud Speech ===================
from google.cloud import speech
from google.oauth2.service_account import Credentials

def stt(audio_bytes: bytes, lang_hint: Optional[str] = None, mime_hint: Optional[str] = None) -> str:
    """
    Google Cloud Speech-to-Text.
    - iOS: audio/mp4  → ENC. UNSPECIFIED(자동감지)
    - Android/Chrome: audio/webm;codecs=opus → WEBM_OPUS
    - Firefox/기타: audio/ogg;codecs=opus → OGG_OPUS
    """
    creds_json = os.getenv("GCP_SPEECH_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("GCP_SPEECH_CREDENTIALS_JSON is empty")

    creds = Credentials.from_service_account_info(json.loads(creds_json))
    client = speech.SpeechClient(credentials=creds)

    # 언어 결정
    lang_map = {"kr": "ko-KR", "en": "en-US"}
    language_code = lang_map.get((lang_hint or "").lower(), os.getenv("SPEECH_LOCALE_FALLBACK", "ko-KR"))

    # MIME → 인코딩 매핑
    enc = speech.RecognitionConfig.AudioEncoding.ENCODING_UNSPECIFIED
    if mime_hint:
        mh = mime_hint.lower()
        if "webm" in mh:
            enc = speech.RecognitionConfig.AudioEncoding.WEBM_OPUS
        elif "ogg" in mh:
            enc = speech.RecognitionConfig.AudioEncoding.OGG_OPUS
        else:
            enc = speech.RecognitionConfig.AudioEncoding.ENCODING_UNSPECIFIED  # mp4/m4a 등

    config = speech.RecognitionConfig(
        encoding=enc,
        language_code=language_code,
        enable_automatic_punctuation=True,
        model="latest_long",
    )
    audio = speech.RecognitionAudio(content=audio_bytes)
    resp = client.recognize(config=config, audio=audio, timeout=90)

    out = []
    for r in resp.results:
        if r.alternatives:
            out.append(r.alternatives[0].transcript)
    return " ".join(out).strip()

# ====================== Google Drive =======================
_drive = None
def _drive_client():
    from googleapiclient.discovery import build
    global _drive
    if _drive:
        return _drive
    if not GDRIVE_CREDENTIALS_JSON:
        raise RuntimeError("GDRIVE_CREDENTIALS_JSON is empty")
    creds = Credentials.from_service_account_info(json.loads(GDRIVE_CREDENTIALS_JSON))
    _drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive

def _ensure_child_folder(parent_id: str, name: str) -> str:
    """parent_id 아래에 name 폴더가 없으면 만들고, 있으면 그 id 반환"""
    svc = _drive_client()
    q = (
        "mimeType='application/vnd.google-apps.folder' "
        f"and name='{name}' and '{parent_id}' in parents and trashed=false"
    )
    res = svc.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
    items = res.get("files", [])
    if items:
        return items[0]["id"]
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    created = svc.files().create(body=meta, fields="id").execute()
    return created["id"]

def upload_to_drive(path: Path, mime: str = "application/octet-stream", kind: str = "file", week_id: str = "") -> str:
    """
    kind: 'audio' | 'log'
    저장 경로: {루트}/{kind}/{week_id}/ 파일
    """
    from googleapiclient.http import MediaFileUpload
    svc = _drive_client()
    if not GDRIVE_FOLDER_ID:
        raise RuntimeError("GDRIVE_FOLDER_ID is empty")

    root = GDRIVE_FOLDER_ID
    sub = "audio" if kind == "audio" else "logs"
    sub_id = _ensure_child_folder(root, sub)
    week_id = week_id or WEEK_ID
    week_id = str(week_id).strip() or "week"
    week_id = week_id.replace("/", "-")
    week_folder_id = _ensure_child_folder(sub_id, week_id)

    media = MediaFileUpload(str(path), mimetype=mime, resumable=True)
    meta = {"name": path.name, "parents": [week_folder_id]}
    created = svc.files().create(body=meta, media_body=media, fields="id, webViewLink").execute()

    file_id = created.get("id")
    link = created.get("webViewLink", "")

    if GDRIVE_PUBLIC_LINK and file_id:
        try:
            svc.permissions().create(fileId=file_id, body={"role": "reader", "type": "anyone"}).execute()
            created = svc.files().get(fileId=file_id, fields="id, webViewLink").execute()
            link = created.get("webViewLink", link)
        except Exception:
            pass
    return link

# ========================== FastAPI ========================
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse("static/index.html")

@app.get("/verses")
def get_verses():
    return {"week_id": WEEK_ID, "title": WEEK_TITLE, "verses": VERSES}

@app.post("/submit")
async def submit(
    audio: UploadFile = File(...),
    name: str = Form(...),
    lang: str = Form("kr"),
    verse_idx: Optional[int] = Form(None),  # 0 또는 1: 개별 채점, None이면 전 구절
    mime: Optional[str] = Form(None),       # 프론트에서 전달한 녹음 MIME
    week_id: Optional[str] = Form(None),    # 프론트에서 보낸 주차 (없으면 verses.json의 week_id)
):
    # 1) 파일 저장 (기기 MIME을 반영해 확장자 결정)
    b = await audio.read()
    ext = ".webm"
    if mime:
        m = mime.lower()
        if "mp4" in m or "m4a" in m: ext = ".m4a"
        elif "ogg" in m:             ext = ".ogg"
        elif "webm" in m:            ext = ".webm"
    fname = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{name}-{uuid.uuid4().hex[:6]}{ext}"
    path = UPLOAD_DIR / fname
    path.write_bytes(b)

    # 2) STT
    try:
        transcript = stt(b, lang_hint=lang, mime_hint=mime)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"STT failed: {e}"}, status_code=500)

    # 3) 채점
    scores = []
    if verse_idx is not None:
        v = VERSES[int(verse_idx)]
        target = v["kr"] if lang == "kr" else v["en"]
        s = round(_similarity(transcript, target), 3)
        scores.append({"verse": v.get("verse_id", ""), "score": s})
        passed = (s >= THRESHOLD)
    else:
        for v in VERSES:
            target = v["kr"] if lang == "kr" else v["en"]
            scores.append({"verse": v.get("verse_id", ""), "score": round(_similarity(transcript, target), 3)})
        passed = all(s["score"] >= THRESHOLD for s in scores)

    # 4) Drive 업로드 (음성) + CSV 기록/업로드 (주차별 하위 폴더)
    upload_mime = (mime or "application/octet-stream")
    week = (week_id or WEEK_ID)
    link = ""
    try:
        link = upload_to_drive(path, mime=upload_mime, kind="audio", week_id=week)
    except Exception:
        link = ""

    write_header = not Path("submissions.csv").exists()
    with open("submissions.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["timestamp","week_id","name","lang","verse_idx","verses","transcript","scores","passed","drive_link"])
        w.writerow([
            datetime.now().isoformat(timespec="seconds"),
            week,
            name,
            lang,
            ("" if verse_idx is None else int(verse_idx)),
            " | ".join(v.get("verse_id","") for v in VERSES),
            transcript,
            "; ".join(f'{s["verse"]}:{s["score"]}' for s in scores),
            "Y" if passed else "N",
            link
        ])

    try:
        upload_to_drive(Path("submissions.csv"), mime="text/csv", kind="log", week_id=week)
    except Exception:
        pass

    return {
        "ok": True,
        "name": name,
        "lang": lang,
        "scores": scores,
        "passed": passed,
        "transcript": transcript,
        "file": link,
        "verse_idx": verse_idx,
        "week_id": week,
    }
