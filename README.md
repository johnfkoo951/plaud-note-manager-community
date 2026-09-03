# Plaud Note Manager Community

Plaud 녹음 목록과 Plaud가 생성한 전사·요약을 동기화하고 로컬에서 다시 찾는 macOS·Windows 워크숍용 앱입니다. 개인 개발판의 인증 저장소, 데이터 폴더, 브라우저 세션과 분리한 제한판입니다.

> 비공식 커뮤니티 프로젝트입니다. Plaud와 제휴하거나 Plaud가 보증하는 앱이 아닙니다. 자신의 계정과 자신에게 처리 권한이 있는 녹음에만 사용하세요. Plaud 웹 API 변경에 따라 작동이 중단될 수 있습니다.

## 공유판의 범위

두 운영체제에 공통으로 포함:

- 녹음 메타데이터 동기화
- 사용자가 직접 시작하는 전사·요약 캐시 가져오기
- 로컬 검색과 Markdown 내보내기
- Plaud Cloud에 쓰지 않는 5단계 사용 상태와 수동 태그
- Plaud Cloud 데이터를 읽는 핵심 흐름

macOS판에만 포함:

- 앱 안의 Plaud Web Login과 전용 WebKit 세션
- Web Login이 멈추거나 SSO가 호환되지 않을 때 쓰는 클립보드/직접 붙여넣기 cURL 폴백
- 재생, 로컬 별표, 명시적으로 실행하는 폴더·제목 정리
- 갱신용 정보가 확보된 경우 만료 전 토큰 갱신

Windows Community Lite판의 경계:

- 기본 브라우저에 `127.0.0.1` 로컬 화면을 열고 임의 세션 토큰으로 API를 보호
- 본인 Plaud Web 세션에서 복사한 API cURL로 연결
- 오디오 재생, 앱 안의 Web Login, Plaud Cloud 변경, 자동 토큰 갱신은 제외

제외:

- Codex, Claude, Gemini 등 외부 생성형 AI 호출
- ElevenLabs 재전사
- Obsidian/개인 볼트 연동
- Chrome 프로필·쉘 설정·호스트 API 키 탐색
- 로그인 직후 전체 라이브러리 자동 백필
- AI 기반 자동 폴더 분류·자동 이동

## 개인정보 경계

- macOS 인증값은 전용 Keychain 서비스 `com.cmdspace.PlaudNoteManagerCommunity.auth`에 저장합니다.
- Windows 인증값은 현재 Windows 사용자에게 묶인 DPAPI로 암호화한 `auth.bin`에 저장합니다. 평문 대체 저장은 없습니다.
- macOS 캐시는 `~/Library/Application Support/com.cmdspace.PlaudNoteManagerCommunity/`에만 저장합니다.
- Windows 캐시는 `%LOCALAPPDATA%\CMDSPACE\PlaudNoteManagerCommunityLite\`에만 저장합니다.
- 개인 개발판의 DB, Keychain, WebKit 세션, `.env`, 셸 API 키를 읽지 않습니다.
- 포함된 Python을 격리 모드(`-I -B`)로 실행하며 필요한 런타임을 배포 파일 안에 포함합니다.
- Plaud API 호스트는 HTTPS `api*.plaud.ai`만 허용하고 API 요청은 리디렉션을 따라가지 않습니다.

자세한 내용은 [개인정보 안내](docs/PRIVACY-KR.md)와 [삭제 안내](docs/UNINSTALL-KR.md)를 확인하세요.
선택형 Chrome 인증 브리지의 후속 설계와 검증 조건은 [제안서](docs/CHROME-AUTH-BRIDGE-PROPOSAL.md)에 분리했습니다.

## 설치

운영체제에 맞는 ZIP을 선택합니다.

- Apple silicon Mac + macOS 14 이상: `macOS-arm64.zip`
- Intel Mac + macOS 14 이상: `macOS-x86_64.zip`
- 64비트 Windows 11: `Windows-x64.zip`
- 인터넷 연결과 본인의 Plaud 계정

macOS에서는 ZIP을 풀고 앱을 `응용 프로그램` 폴더로 옮깁니다. Windows에서는 ZIP 전체를 푼 뒤 `Start Plaud Community.cmd`를 실행합니다. 현재 산출물은 Apple 공증과 Windows 코드 서명을 받지 않았습니다. 운영체제 보안 기능을 전역으로 끄지 말고, 출처와 SHA-256을 확인한 파일만 실행하세요. 자세한 순서는 [설치 안내](docs/INSTALL-KR.md)에 있습니다.

## 첫 사용

1. macOS는 앱의 **Plaud Web Login**으로 로그인합니다. 내장 로그인이 멈추면 같은 인증 창의 **Import Copied cURL** 또는 수동 붙여넣기를 사용합니다. Windows는 안내에 따라 Plaud API cURL을 복사해 로컬 화면에 붙여 넣습니다.
2. **Sync**로 녹음 목록만 먼저 가져옵니다.
3. 필요한 경우에만 **Backfill**을 눌러 Plaud 전사·요약을 로컬에 캐시합니다.
4. 검색창에서 제목·전사·요약을 검색합니다.
5. 녹음별 사용 상태와 수동 태그는 Community 전용 로컬 DB에서 정리합니다.

Backfill은 계정의 아직 캐시되지 않은 전사·요약을 컴퓨터에 내려받습니다. 공용 컴퓨터에서는 사용하지 마세요.

## 소스에서 검증·빌드

```bash
uv sync --group dev
PLAUD_SECRET_STORE=test-file PLAUD_AUTO_REFRESH=0 uv run pytest -q
uv run ruff check .
uv run ruff format --check .
swift build --package-path app -c release
swift build --package-path app --triple x86_64-apple-macosx14.0 -c release
scripts/audit-source.sh
scripts/package-macos-app.sh
scripts/package-macos-intel.sh
python3 scripts/package-windows-portable.py
```

패키징은 깨끗한 Git 상태를 요구하며 앱·ZIP·`SHA256SUMS`를 `dist/`에 만듭니다. Windows 스크립트는 고정한 공식 CPython x64 런타임과 Windows wheel을 묶은 뒤 정적 아키텍처·개인정보 감사를 실행합니다. 네이티브 Windows 자가진단은 `.github/workflows/build-windows.yml` 또는 실제 Windows 11 PC에서 별도로 통과해야 합니다.

## 알려진 배포 한계

- macOS arm64는 Apple silicon에서, x86_64는 Apple silicon의 Rosetta에서 빌드·CLI 스모크 테스트합니다. 실제 Intel Mac GUI 확인은 별도 배포 게이트입니다.
- Windows ZIP은 정적으로 교차 빌드할 수 있지만, 실제 배포 전 Windows 11 x64에서 포함된 `--self-test`와 DPAPI 왕복 테스트를 통과해야 합니다.
- Developer ID 서명·Apple 공증과 Windows Authenticode 서명은 별도 릴리스 단계이며 현재 산출물에는 적용되지 않았습니다.
- 실제 Plaud 계정 로그인·토큰 갱신·동기화는 각 참가자가 자신의 계정에서 확인해야 합니다.
- Plaud 웹 API는 공식 공개 SDK가 아니므로 서버 변경 시 수정이 필요할 수 있습니다.

테스트 범위와 미검증 항목은 [검증 기록](docs/TESTING.md)에 분리해 기록합니다.

## 라이선스

프로젝트 코드는 Apache-2.0입니다. `LICENSE`의 저작권자 이름은 공개 저작권 고지이며 앱 사용자의 개인정보가 아닙니다. 포함된 제3자 소프트웨어는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 따릅니다.
