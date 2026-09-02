# 개인정보 및 네트워크 안내

## 이 앱이 다루는 데이터

본인이 로그인한 Plaud 계정에서 다음 정보를 가져올 수 있습니다.

- 녹음 제목·시간·길이·폴더·태그
- Plaud가 생성한 전사·요약·개요
- 재생을 위한 단기 오디오 URL 또는 사용자가 명시적으로 내려받은 오디오

이 데이터에는 민감한 대화와 개인정보가 포함될 수 있습니다. 개인 Mac에서만 사용하고, 화면 공유·백업·지원 요청 전에 내용을 점검하세요.

## 저장 위치

- 인증: macOS Keychain의 `com.cmdspace.PlaudNoteManagerCommunity.auth`
- DB와 캐시: `~/Library/Application Support/com.cmdspace.PlaudNoteManagerCommunity/data/`
- 비밀이 아닌 설정: `~/Library/Application Support/com.cmdspace.PlaudNoteManagerCommunity/settings.env`
- 앱 내 웹 로그인 세션: Community 앱 전용 WebKit 저장소

Application Support와 data 폴더는 앱이 0700 권한으로 만들고, Python 프로세스는 077 umask로 새 파일을 생성합니다. macOS 사용자 계정과 디스크 암호화 상태에 따라 실제 보호 수준은 달라집니다.

## 네트워크

기본 기능은 Plaud 서비스와 통신합니다.

- `https://web.plaud.ai`: 앱 내 로그인
- `https://api*.plaud.ai`: 계정 지역의 API, 토큰 갱신, 목록·전사·요약·오디오 접근

API 자격증명은 HTTPS Plaud API 하위 도메인에만 전송합니다. API 클라이언트는 리디렉션을 따라가지 않습니다. Community판은 외부 생성형 AI, ElevenLabs, Obsidian, 로컬 Chrome 프로필 또는 셸 설정을 사용하지 않습니다.

## 사용자가 선택하는 작업

- Sync: 제목과 목록 중심의 메타데이터를 로컬 DB에 저장합니다.
- Backfill: 아직 없는 전사·요약을 로컬 DB에 내려받습니다.
- Download/Export: 선택한 내용을 사용자가 정한 파일 경로에 저장합니다.
- 폴더·제목 정리: 실행하면 본인의 Plaud Cloud 상태가 바뀔 수 있습니다. 수업 중에는 결과를 확인하고 소수 항목으로 먼저 시험하세요.

## 연결 해제와 삭제

`plaud disconnect`는 Community판 Keychain 인증값과 설정 파일에 남을 수 있는 인증 키만 제거합니다. DB·캐시·내보낸 파일은 자동 삭제하지 않습니다. 웹 세션과 로컬 데이터까지 지우는 방법은 [삭제 안내](UNINSTALL-KR.md)를 따르세요.

## 하지 않는 것

- 개발자의 개인 인증정보·DB·볼트·브라우저 프로필을 포함하거나 읽지 않음
- 참가자의 Plaud 비밀번호를 앱 코드나 설정 파일에 기록하지 않음
- 사용자 동의 없이 전체 전사·요약을 자동 Backfill하지 않음
- 외부 AI 서비스에 전사·요약을 전송하지 않음

비밀 스캔과 권한 검사는 방어 수단이지 비밀 부재의 수학적 증명은 아닙니다. 배포자는 최종 ZIP마다 제공된 감사 스크립트와 체크섬을 다시 생성해야 합니다.
