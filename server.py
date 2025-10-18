import csv, json, os, re, uuid, tempfile
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ========================= 설정 ============================
THRESHOLD = float(os.getenv("THRESHOLD", "0.85"))  # 합격 기준 점수

# Google Drive
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "").strip()
GDRIVE_CREDENTIALS_JSON = os.getenv("GDRIVE_CREDENTIALS_JSON", "")
GDRIVE_PUBLIC_LINK = os.getenv("GDRIVE_PUBLIC_LINK", "false").lower() == "true"

# 업로드 폴더
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
VERSES = raw["verses"] if isinstance(raw, dict) and "verses" in raw else raw

# =================== Google Cloud Speech ===================
from google.cloud import speech
from google.oauth2.service_account import Credentials

def stt(audio_bytes: bytes, lang_hint: str | None = None) -> str:
    """
    Google Cloud Speech-to-Text (동기 인식).
    MediaRecorder 기본(WebM/Opus) 지원.
    """
    creds_json = os.getenv("GCP_SPEECH_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("GCP_SPEECH_CREDENTIALS_JSON is empty")

    creds = Credentials.from_service_account_info(json.loads(creds_json))
    client = speech.SpeechClient(credentials=creds)

    # 언어 매핑
    lang_map = {"kr": "ko-KR", "en": "en-US"}
    language_code = lang_map.get((lang_hint or "").lower(), os.getenv("SPEECH_LOCALE_FALLBACK", "ko-KR"))

    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.WEBM_OPUS,
        language_code=language_code,
        enable_automatic_punctuation=True,
        model="latest_long",
    )
    audio = speech.RecognitionAudio(content=audio_bytes)
    resp = client.recognize(config=config, audio=audio, timeout=90)

    lines = []
    for result in resp.results:
        if result.alternatives:
            lines.append(result.alternatives[0].transcript)
    return " ".join(lines).strip()

# ====================== Google Drive =======================
_drive = None
def _drive_client():
    from googleapiclient.discovery import build
    global _drive
    if _drive: return _drive
    if not GDRIVE_CREDENTIALS_JSON:
        raise RuntimeError("GDRIVE_CREDENTIALS_JSON is empty")
    creds = Credentials.from_service_account_info(json.loads(GDRIVE_CREDENTIALS_JSON))
    _drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive

def _ensure_child_folder(parent_id: str, name: str) -> str:
    """parent_id 아래에 name 폴더가 없으면 만들고, 있으면 그 id 반환"""
    svc = _drive_client()
    q = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and '{parent_id}' in parents and trashed=false"
    res = svc.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
    items = res.get("files", [])
    if items:
        return items[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    created = svc.files().create(body=meta, fields="id").execute()
    return created["id"]

def upload_to_drive(path: Path, mime: str = "application/octet-stream", kind: str = "file") -> str:
    """
    kind: 'audio' | 'log'  →  audio/ 또는 logs/ 하위에 저장
    """
    from googleapiclient.http import MediaFileUpload
    svc = _drive_client()
    if not GDRIVE_FOLDER_ID:
        raise RuntimeError("GDRIVE_FOLDER_ID is empty")

    # 상위/하위 폴더 자동 구성: {루트}/audio, {루트}/logs
    target_parent = GDRIVE_FOLDER_ID
    sub = "audio" if kind == "audio" else "logs"
    target_parent = _ensure_child_folder(target_parent, sub)

    media = MediaFileUpload(str(path), mimetype=mime, resumable=True)
    meta = {"name": path.name, "parents": [target_parent]}
    created = svc.files().create(body=meta, media_body=media, fields="id, webViewLink").execute()

    file_id = created.get("id")
    webViewLink = created.get("webViewLink", "")

    # 공개 링크(옵션)
    if GDRIVE_PUBLIC_LINK and file_id:
        try:
            svc.permissions().create(fileId=file_id, body={"role": "reader", "type": "anyone"}).execute()
            created = svc.files().get(fileId=file_id, fields="id, webViewLink").execute()
            webViewLink = created.get("webViewLink", webViewLink)
        except Exception as e:
            # 링크 생성 실패 시에도 업로드는 성공했으니 조용히 진행
            pass
    return webViewLink

# ========================== FastAPI ========================
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse("static/index.html")

@app.get("/verses")
def get_verses():
    # 프론트에 구절 목록 전달 (화면에는 verse_id만 표시)
    return {"verses": VERSES}

from typing import Optional

@app.post("/submit")
async def submit(
    audio: UploadFile = File(...),
    name: str = Form(...),
    lang: str = Form("kr"),
    verse_idx: Optional[int] = Form(None),   # ★ 추가: 0 or 1
):
    # 1) 파일 저장
    b = await audio.read()
    fname = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{name}-{uuid.uuid4().hex[:6]}.webm"
    path = UPLOAD_DIR / fname
    path.write_bytes(b)

    # 2) STT
    try:
        transcript = stt(b, lang_hint=lang)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"STT failed: {e}"}, status_code=500)

    # 3) 채점
    scores = []
    if verse_idx is not None:
        # 한 구절만
        v = VERSES[int(verse_idx)]
        target = v["kr"] if lang == "kr" else v["en"]
        s = round(_similarity(transcript, target), 3)
        scores.append({"verse": v.get("verse_id",""), "score": s})
        passed = (s >= THRESHOLD)
    else:
        # 기존: 모든 구절
        for v in VERSES:
            target = v["kr"] if lang == "kr" else v["en"]
            scores.append({"verse": v.get("verse_id",""), "score": round(_similarity(transcript, target), 3)})
        passed = all(s["score"] >= THRESHOLD for s in [*scores])

    # 4) Drive 업로드
    link = ""
    try:
        link = upload_to_drive(path, mime="audio/webm", kind="audio")
    except Exception as e:
        # 링크 실패해도 CSV는 기록
        link = ""

    # 5) CSV 기록 (verse_idx 포함)
    write_header = not Path("submissions.csv").exists()
    with open("submissions.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["timestamp","name","lang","verse_idx","verses","transcript","scores","passed","drive_link"])
        w.writerow([
            datetime.now().isoformat(timespec="seconds"),
            name,
            lang,
            ("" if verse_idx is None else int(verse_idx)),
            " | ".join(v.get("verse_id","") for v in VERSES),
            transcript,
            "; ".join(f'{s["verse"]}:{s["score"]}' for s in scores),
            "Y" if passed else "N",
            link
        ])

    # CSV도 드라이브에 동기화
    try:
        upload_to_drive(Path("submissions.csv"), mime="text/csv", kind="log")
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
    }
