# 제자반 암송 자동확인 (Google Drive 업로드 + 클라우드 배포)

이 버전은 제출한 **음성 파일과 로그(submissions.csv)**를 **Google Drive 폴더**에 자동 업로드합니다.
외부에서도 접속 가능하도록 **Render**(권장) 기준 배포 방법까지 단계별로 안내합니다.

---

## 전체 흐름
1) **Google Cloud에서 서비스 계정** 만들기 → **Drive API 활성화**  
2) Google Drive에서 **폴더 생성** → **서비스 계정 이메일과 공유(편집자)**  
3) 이 앱을 **Render에 배포**하고 환경변수 설정  
4) 배포 주소로 접속해서 사용 (녹음 → 제출)  
5) 음성은 **Drive 폴더에 저장**, 판정 로그는 **submissions.csv**로 저장 후 **Drive에도 업로드**

---

## 0. 준비물
- OpenAI API 키 (음성 인식을 위해 필요)
- Google 계정
- GitHub 계정 (코드 업로드용)
- Render 계정 (배포용, 무료 플랜 가능)

---

## 1. Google Cloud 설정 (한 번만)
1) https://console.cloud.google.com/ 접속 → 프로젝트 생성  
2) 왼쪽 메뉴 **APIs & Services → Enabled APIs & services → ENABLE APIS AND SERVICES**  
   - **Google Drive API** 검색 → **Enable**  
3) 왼쪽 메뉴 **IAM & Admin → Service Accounts → Create Service Account**
   - 이름 예: `gmc-memory-service`
   - 생성 후 해당 서비스 계정 클릭 → **Keys → Add Key → Create new key → JSON** 선택 → **키 파일 다운로드**  
   - 키 파일 안에 **`client_email`** 값을 복사해둡니다. (예: `gmc-memory-service@...iam.gserviceaccount.com`)

> 이 서비스 계정은 **사용자 드라이브에 접근 권한이 없습니다.** 대신 아래 단계에서 **드라이브 폴더를 서비스 계정 이메일과 공유**해야 합니다.

---

## 2. Google Drive 폴더 준비
1) Google Drive에서 새 폴더 만들기 (예: `GMC 암송 제출`)  
2) 폴더 우클릭 → **공유** → **사람 및 그룹 추가**에 **서비스 계정의 이메일**을 입력하고 **편집자**로 초대  
3) 폴더를 연 상태의 주소창에서 **폴더 ID**를 복사 (URL의 `/folders/` 다음의 긴 문자열)  
   - 예: `https://drive.google.com/drive/folders/ABC123...` → 폴더 ID = `ABC123...`

---

## 3. 코드 준비 (GitHub 업로드)
이 ZIP의 파일들을 새 GitHub 리포지토리에 업로드합니다.
반드시 포함되어야 하는 파일:
- `server.py`
- `requirements.txt`
- `verses.json`
- `static/index.html`

---

## 4. Render에 배포
1) Render 대시보드 → **New** → **Web Service** → GitHub 리포 선택  
2) **Build Command**: `pip install -r requirements.txt`  
3) **Start Command**: `uvicorn server:app --host 0.0.0.0 --port $PORT`  
4) **Environment** 탭에서 아래 환경변수를 추가합니다:

필수
- `OPENAI_API_KEY` = (OpenAI 키)
- `GDRIVE_FOLDER_ID` = (2단계에서 복사한 폴더 ID)
- `GDRIVE_CREDENTIALS_JSON` = 서비스 계정 **JSON 키 파일 전체 내용**을 그대로 붙여넣기 (한 줄로 붙여넣어도 됩니다)

선택
- `THRESHOLD` = `0.85` (합격 기준, 기본 0.85)
- `GDRIVE_PUBLIC_LINK` = `true` 로 설정하면 업로드된 파일에 **링크로 보기 권한(Anyone with the link)**을 자동 부여합니다. (기본: 미공개)
- `APP_BASE_URL` = 앱의 외부 URL(예: `https://your-app.onrender.com`) — 로컬 경로 대신 링크 표시를 선호할 때 사용

> 보안상 `GDRIVE_CREDENTIALS_JSON`은 **절대 GitHub에 올리지 말고**, Render **환경변수**로만 보관하세요.

---

## 5. 사용법
- 배포가 완료되면 Render가 제공하는 주소로 접속합니다.  
- 페이지에서 **이름 입력 → 녹음 → 정지 → 제출**  
- 결과: 합격/재도전 판정, 인식 텍스트 확인, **파일 링크**(공개 설정 시) 표시  
- Google Drive 폴더에 **음성 파일**과 **`submissions.csv`**가 업로드됩니다.

---

## 6. 매주 구절 바꾸기
- GitHub에서 `verses.json`의 `verse_id`, `text`를 수정하고 커밋 → Render가 재배포하여 반영됩니다.

---

## 7. 문제 해결
- **403/권한 오류**: 드라이브 폴더가 서비스 계정 이메일과 **공유되어 있는지** 확인 (편집자 권한 필요)
- **링크가 열리지 않음**: `GDRIVE_PUBLIC_LINK=true`를 설정하지 않았다면 비공개 상태입니다. 필요 시 on.
- **STT 실패**: `OPENAI_API_KEY` 확인, 일시 과금 제한 또는 네트워크 이슈 점검
- **파일 이름 한글**: 파일명은 `이름_시간_UUID.webm` 형식, 드라이브에 정상 저장됩니다.

사역에 복이 되길 바랍니다! 🙏
