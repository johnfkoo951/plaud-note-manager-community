# Plaud Note Manager Community

Plaud 녹음 목록과 Plaud가 생성한 전사·요약을 한곳에서 동기화하고, 로컬에서 검색·재생·내보내기 하는 macOS 워크숍용 앱입니다. 개인 개발판에서 인증 저장소, 데이터 폴더, 브라우저 세션을 완전히 분리한 제한판입니다.

> 비공식 커뮤니티 프로젝트입니다. Plaud와 제휴하거나 Plaud가 보증하는 앱이 아닙니다. 자신의 계정과 자신에게 처리 권한이 있는 녹음에만 사용하세요. Plaud 웹 API 변경에 따라 작동이 중단될 수 있습니다.

## 공유판의 범위

포함:

- 앱 안의 Plaud Web Login
- 녹음 메타데이터 동기화
- 사용자가 직접 시작하는 전사·요약 캐시 가져오기
- 로컬 전체 텍스트 검색, 필터, 재생, 파일 내보내기
- 로컬 별표와 명시적으로 실행하는 폴더·제목 정리
- 만료 전 자동 토큰 갱신

제외:

- Codex, Claude, Gemini 등 외부 생성형 AI 호출
- ElevenLabs 재전사
- Obsidian/개인 볼트 연동
- Chrome 프로필·쉘 설정·호스트 API 키 탐색
- 로그인 직후 전체 라이브러리 자동 백필

## 개인정보 경계

- 인증값은 전용 Keychain 서비스 `com.cmdspace.PlaudNoteManagerCommunity.auth`에 저장합니다.
- 캐시는 `~/Library/Application Support/com.cmdspace.PlaudNoteManagerCommunity/`에만 저장합니다.
- 개인 개발판의 DB, Keychain, WebKit 세션, `.env`, 셸 API 키를 읽지 않습니다.
- 앱에서 Python을 격리 모드(`-I -B`)로 실행하며 필요한 런타임을 앱 안에 포함합니다.
- Plaud API 호스트는 HTTPS `api*.plaud.ai`만 허용하고 API 요청은 리디렉션을 따라가지 않습니다.

자세한 내용은 [개인정보 안내](docs/PRIVACY-KR.md)와 [삭제 안내](docs/UNINSTALL-KR.md)를 확인하세요.

## 설치

현재 워크숍 릴리스의 요구 사항은 다음과 같습니다.

- Apple silicon Mac
- macOS 14 이상
- 인터넷 연결과 본인의 Plaud 계정

배포 ZIP을 풀고 앱을 `응용 프로그램` 폴더로 옮깁니다. 현재 공개 릴리스는 ad-hoc 서명이며 Apple 공증을 받지 않았으므로 첫 실행 때 Finder에서 앱을 Control-클릭하고 **열기**를 선택해야 할 수 있습니다. 자세한 순서는 [설치 안내](docs/INSTALL-KR.md)에 있습니다.

## 첫 사용

1. 앱을 열고 우측 상단 인증 버튼을 누릅니다.
2. **Plaud Web Login**에서 본인 계정으로 로그인합니다.
3. **Sync**로 녹음 목록만 먼저 가져옵니다.
4. 필요한 경우에만 **Backfill**을 눌러 Plaud 전사·요약을 로컬에 캐시합니다.
5. 검색창에서 제목·전사·요약을 검색합니다.

Backfill은 계정의 아직 캐시되지 않은 전사·요약을 이 Mac에 내려받습니다. 공용 Mac에서는 사용하지 마세요.

## 소스에서 검증·빌드

```bash
uv sync --group dev
PLAUD_SECRET_STORE=test-file PLAUD_AUTO_REFRESH=0 uv run pytest -q
uv run ruff check .
uv run ruff format --check .
swift build --package-path app -c release
scripts/audit-source.sh
scripts/package-macos-app.sh
```

패키징은 깨끗한 Git 상태를 요구하며, 앱·ZIP·SHA-256 파일을 `dist/`에 만듭니다. 로컬 테스트 설치는 `scripts/install-local.sh`을 사용합니다. 설치 스크립트는 격리된 `~/Applications`만 사용하고 기존 앱은 타임스탬프 백업으로 이동합니다.

## 알려진 배포 한계

- Apple silicon 및 macOS 14 이상만 검증 대상으로 합니다.
- Developer ID 서명과 Apple 공증은 별도 릴리스 단계이며 현재 산출물에는 적용되지 않았습니다.
- 실제 Plaud 계정 로그인·토큰 갱신·동기화는 각 참가자가 자신의 계정에서 확인해야 합니다.
- Plaud 웹 API는 공식 공개 SDK가 아니므로 서버 변경 시 수정이 필요할 수 있습니다.

테스트 범위와 미검증 항목은 [검증 기록](docs/TESTING.md)에 분리해 기록합니다.

## 라이선스

프로젝트 코드는 Apache-2.0입니다. `LICENSE`의 저작권자 이름은 공개 저작권 고지이며 앱 사용자의 개인정보가 아닙니다. 포함된 제3자 소프트웨어는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 따릅니다.
