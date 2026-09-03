# 개인정보 및 네트워크 안내

## 이 앱이 다루는 데이터

본인이 로그인한 Plaud 계정에서 다음 정보를 가져올 수 있습니다.

- 녹음 제목·시간·길이·폴더·태그
- Plaud가 생성한 전사·요약·개요
- macOS판의 재생을 위한 단기 오디오 URL 또는 사용자가 명시적으로 내려받은 오디오

이 데이터에는 민감한 대화와 개인정보가 포함될 수 있습니다. 개인 컴퓨터에서만 사용하고, 화면 공유·백업·지원 요청 전에 내용을 점검하세요.

## 저장 위치

- macOS 인증: Keychain의 `com.cmdspace.PlaudNoteManagerCommunity.auth`
- macOS DB·캐시·설정: `~/Library/Application Support/com.cmdspace.PlaudNoteManagerCommunity/`
- macOS 앱 내 로그인 세션: Community 앱 전용 WebKit 저장소
- Windows 인증: `%LOCALAPPDATA%\CMDSPACE\PlaudNoteManagerCommunityLite\config\auth.bin`
- Windows DB·캐시·내보내기: `%LOCALAPPDATA%\CMDSPACE\PlaudNoteManagerCommunityLite\`

macOS는 전용 폴더를 0700 권한으로 만들고 Python 프로세스는 077 umask를 사용합니다. Windows 인증 파일은 현재 로그인한 Windows 사용자에게 묶인 DPAPI로 암호화하며 평문 대체 저장을 하지 않습니다. 로컬 DB와 내보낸 전사 자체는 별도 암호화하지 않으므로, 운영체제 계정·디스크 암호화·백업 설정에 따라 실제 보호 수준이 달라집니다.

## 네트워크

기본 기능은 Plaud 서비스와 통신합니다.

- `https://web.plaud.ai`: macOS 앱 내 로그인, 또는 Windows에서 사용자가 직접 cURL을 복사하는 웹 세션
- `https://api*.plaud.ai`: 계정 지역의 API, 토큰 갱신, 목록·전사·요약·오디오 접근

API 자격증명은 HTTPS Plaud API 하위 도메인에만 전송합니다. API 클라이언트는 리디렉션과 시스템 프록시 환경 변수를 따르지 않습니다. Community판은 외부 생성형 AI, ElevenLabs, Obsidian, 로컬 Chrome 프로필 또는 셸 설정을 자동으로 탐색하지 않습니다.

Windows 화면은 `127.0.0.1`의 임의 포트에만 바인딩합니다. 임의 세션 토큰은 URL fragment로 전달한 뒤 주소창 기록에서 제거하며, 토큰 헤더가 없는 로컬 API 요청을 거부합니다. 정적 파일과 API에는 `no-store`와 제한적인 Content Security Policy를 보냅니다. cURL을 붙여 넣는 순간에는 브라우저 페이지와 시스템 클립보드에 인증값이 있으므로 신뢰하는 브라우저 프로필만 사용하고 연결 후 클립보드를 덮어쓰세요.

## 사용자가 선택하는 작업

- Sync: 제목과 목록 중심의 메타데이터를 로컬 DB에 저장합니다.
- Backfill: 아직 없는 전사·요약을 로컬 DB에 내려받습니다.
- Download/Export: 선택한 내용을 로컬 파일로 저장합니다. Windows Lite판은 전용 `exports` 폴더만 사용합니다.
- 폴더·제목 정리: macOS판에서만 제공하며 실행하면 본인의 Plaud Cloud 상태가 바뀔 수 있습니다. Windows Lite판은 Plaud Cloud 변경 기능을 제공하지 않습니다.

## 연결 해제와 삭제

`plaud disconnect` 또는 화면의 **연결 해제**는 해당 운영체제 Community 인증값과 설정 파일에 남을 수 있는 인증 키만 제거합니다. DB·캐시·내보낸 파일은 자동 삭제하지 않습니다. 웹 세션과 로컬 데이터까지 지우는 방법은 [삭제 안내](UNINSTALL-KR.md)를 따르세요.

## 하지 않는 것

- 개발자의 개인 인증정보·DB·볼트·브라우저 프로필을 포함하거나 읽지 않음
- 참가자의 Plaud 비밀번호를 앱 코드나 설정 파일에 기록하지 않음
- 사용자 동의 없이 전체 전사·요약을 자동 Backfill하지 않음
- 외부 AI 서비스에 전사·요약을 전송하지 않음

비밀 스캔과 권한 검사는 방어 수단이지 비밀 부재의 수학적 증명은 아닙니다. 배포자는 최종 ZIP마다 제공된 감사 스크립트와 체크섬을 다시 생성해야 합니다.
