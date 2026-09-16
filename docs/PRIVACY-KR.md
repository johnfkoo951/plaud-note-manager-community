# 개인정보 및 네트워크 안내

## 이 앱이 다루는 데이터

본인이 로그인한 Plaud 계정에서 다음 정보를 가져올 수 있습니다.

- 녹음 제목·시간·길이·폴더·태그
- Plaud가 생성한 전사·요약·개요
- macOS판의 재생을 위한 단기 오디오 URL 또는 사용자가 명시적으로 내려받은 오디오

이 데이터에는 민감한 대화와 개인정보가 포함될 수 있습니다. 개인 컴퓨터에서만 사용하고, 화면 공유·백업·지원 요청 전에 내용을 점검하세요.

## 저장 위치

- macOS 인증: Keychain의 `com.cmdspace.PlaudNoteManagerCommunity.auth`
- macOS 외부 공급자 API key: Keychain의 `com.cmdspace.PlaudNoteManagerCommunity.auth.providers` 아래 공급자별 항목
- macOS DB·캐시·설정: `~/Library/Application Support/com.cmdspace.PlaudNoteManagerCommunity/`
- macOS 앱 내 로그인 세션: Community 앱 전용 WebKit 저장소
- Windows 인증: `%LOCALAPPDATA%\CMDSPACE\PlaudNoteManagerCommunityLite\config\auth.bin`
- Windows 외부 공급자 API key: `%LOCALAPPDATA%\CMDSPACE\PlaudNoteManagerCommunityLite\config\provider-secrets\*.bin`
- Windows DB·캐시·내보내기: `%LOCALAPPDATA%\CMDSPACE\PlaudNoteManagerCommunityLite\`

macOS는 전용 폴더를 0700 권한으로 만들고 Python 프로세스는 077 umask를 사용합니다. Windows 인증 파일은 현재 로그인한 Windows 사용자에게 묶인 DPAPI로 암호화하며 평문 대체 저장을 하지 않습니다. 로컬 DB와 내보낸 전사 자체는 별도 암호화하지 않으므로, 운영체제 계정·디스크 암호화·백업 설정에 따라 실제 보호 수준이 달라집니다.

## 네트워크

기본 기능은 Plaud 서비스와 통신합니다. 외부 공급자는 사용자가 설정하고 해당 작업을 확인한 경우에만 통신합니다.

- `https://web.plaud.ai`: macOS 앱 내 로그인 또는 수동 cURL 폴백, Windows에서 사용자가 직접 cURL을 복사하는 웹 세션
- `https://api*.plaud.ai`: 계정 지역의 API, 토큰 갱신, 목록·전사·요약·오디오 접근
- `https://api.anthropic.com`, `https://api.openai.com`, `https://generativelanguage.googleapis.com`, `https://api.x.ai`: 사용자가 AI 폴더 미리보기를 확인했을 때 선택한 한 공급자
- `https://api.elevenlabs.io`: 사용자가 녹음별 업로드를 확인했을 때 Scribe v2 전사

Plaud 자격증명은 HTTPS Plaud API 하위 도메인에만 전송합니다. 외부 공급자 key는 해당 공급자의 고정 HTTPS API 호스트에만 전송합니다. API 클라이언트는 리디렉션과 시스템 프록시 환경 변수를 따르지 않습니다. Community판은 Obsidian, 로컬 Chrome 프로필, 셸 시작 파일 또는 셸 환경의 API key를 자동으로 탐색하지 않습니다. Claude·Codex CLI 모드는 해당 앱의 로그인 세션을 사용하지만 앱의 OAuth 토큰을 복사하거나 저장하지 않습니다. Gemini·Grok은 API key 방식만 지원합니다.

Windows 화면은 `127.0.0.1`의 임의 포트에만 바인딩합니다. 임의 세션 토큰은 URL fragment로 전달한 뒤 주소창 기록에서 제거하며, 토큰 헤더가 없는 로컬 API 요청을 거부합니다. 보이는 화면은 같은 인증 헤더로 15초마다 수명 신호만 보내며, 이 신호가 2분간 끊기면 진행 중인 작업을 마친 뒤 로컬 서버를 종료합니다. 수명 신호와 서버 로그에는 인증값이나 녹음 내용을 기록하지 않습니다. 정적 파일과 API에는 `no-store`와 제한적인 Content Security Policy를 보냅니다. cURL을 붙여 넣는 순간에는 브라우저 페이지와 시스템 클립보드에 인증값이 있으므로 신뢰하는 브라우저 프로필만 사용하고 연결 후 클립보드를 덮어쓰세요.

## 사용자가 선택하는 작업

- Sync: 제목과 목록 중심의 메타데이터를 로컬 DB에 저장합니다.
- Backfill: 아직 없는 전사·요약을 로컬 DB에 내려받습니다.
- Download/Export: 선택한 내용을 로컬 파일로 저장합니다. Windows Lite판은 전용 `exports` 폴더만 사용합니다.
- 사용 상태·수동 태그: macOS와 Windows 모두 Community 전용 로컬 DB에만 저장하며 Plaud Cloud로 보내지 않습니다.
- 자동 폴더 미리보기: 기본 로컬 규칙은 네트워크로 텍스트를 보내지 않습니다. AI 모드를 매번 확인하면 현재 폴더 이름과 약한 매칭의 녹음 제목·키워드·요약·전사 일부가 선택한 공급자로 전송될 수 있습니다. 최근 최대 200개, AI 요청 최대 20회이며 이 단계에서는 Cloud를 바꾸지 않습니다.
- 폴더 적용: 신뢰도 60% 이상인 저장된 미리보기에서 사용자가 선택한 정확한 녹음/기존 폴더 ID만 Plaud Cloud에 반영합니다. 미리보기는 30분 후, 폴더 목록이나 로컬·Cloud 배치가 달라진 경우 적용 전에 폐기됩니다. 자동 라우터는 폴더를 만들지 않습니다.
- 폴더 되돌리기: 최근 적용 직후의 대상 폴더가 로컬과 Plaud Cloud에 그대로인지를 모든 항목에 대해 첫 PATCH 전에 검사한 뒤 정확한 이전 폴더 ID로 복원합니다. 충돌이 있으면 아무 항목도 변경하지 않습니다. 적용 도중 중단된 기록은 첫 명시적 동작에서 안정화하고, 두 번째 명시적 Undo에서만 역변경합니다. 이 로컬 복구 상태는 재시작 뒤에도 표시됩니다.
- ElevenLabs 전사: 녹음별 확인 후 Plaud의 단기 오디오 URL에서 파일을 받아 ElevenLabs에 업로드합니다. 유료 크레딧이 사용될 수 있습니다. 일반 계정에 대해 zero retention을 주장하지 않습니다. 전송 결과가 불명확하면 해시된 로컬 시도 마커가 재업로드를 막고, 중복 청구 가능성을 다시 명시적으로 확인해야 합니다. 정상 종료 시 임시 오디오는 즉시 삭제하며 강제 종료 잔여물은 앱 전용 임시 폴더에서 6시간이 지난 뒤 다음 전사 시 제한적으로 청소합니다.
- 수동 폴더·제목 정리: macOS판에서만 제공하며 실행하면 본인의 Plaud Cloud 상태가 바뀔 수 있습니다.

## 연결 해제와 삭제

`plaud disconnect` 또는 화면의 **연결 해제**는 Plaud 인증만 제거합니다. AI·ElevenLabs API key, DB·캐시·내보낸 파일은 자동 삭제하지 않습니다. 공급자 key는 설정에서 각각 삭제하고, 웹 세션과 로컬 데이터까지 지우는 방법은 [삭제 안내](UNINSTALL-KR.md)를 따르세요.

## 하지 않는 것

- 개발자의 개인 인증정보·DB·볼트·브라우저 프로필을 포함하거나 읽지 않음
- 참가자의 Plaud 비밀번호를 앱 코드나 설정 파일에 기록하지 않음
- 사용자 동의 없이 전체 전사·요약을 자동 Backfill하지 않음
- 사용자의 실행·외부 전송 확인 없이 AI로 녹음을 분류하지 않음
- 저장된 미리보기와 적용 확인 없이 폴더를 이동하거나 새 폴더를 만들지 않음
- 녹음별 업로드 확인 없이 ElevenLabs에 오디오를 보내지 않음

비밀 스캔과 권한 검사는 방어 수단이지 비밀 부재의 수학적 증명은 아닙니다. 배포자는 최종 ZIP마다 제공된 감사 스크립트와 체크섬을 다시 생성해야 합니다.
