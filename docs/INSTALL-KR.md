# 설치 안내

## 공통 준비

- 본인의 개인 컴퓨터, 본인의 Plaud 계정, 인터넷 연결을 준비합니다.
- 전달받은 ZIP의 운영체제·CPU 표기가 컴퓨터와 맞는지 확인합니다.
- `SHA256SUMS`와 ZIP을 같은 전달 경로에서 받은 뒤 해시를 비교합니다.
- 기존 개인판 `Plaud Note Manager`가 있어도 Community판은 별도 앱·저장소를 사용합니다.

## Apple silicon Mac

1. `Plaud Note Manager Community-<버전>-macOS-arm64.zip`을 사용합니다.
2. 터미널에서 무결성을 확인합니다.

   ```bash
   shasum -a 256 -c SHA256SUMS
   ```

3. ZIP을 풀고 `Plaud Note Manager Community.app`을 `응용 프로그램`으로 옮깁니다.
4. 첫 실행은 Finder에서 앱을 Control-클릭 → **열기** → **열기**로 진행합니다.

## Intel Mac

1. Apple 메뉴 → 이 Mac에 관하여에서 프로세서가 Intel인지 확인합니다.
2. `Plaud Note Manager Community-<버전>-macOS-x86_64.zip`을 사용합니다.
3. Apple silicon 안내와 같은 방법으로 체크섬을 확인하고 설치합니다.

두 macOS 빌드는 macOS 14 이상이 필요합니다. 현재 워크숍 빌드는 ad-hoc 서명이고 Apple 공증을 받지 않았습니다. 출처나 해시를 확인할 수 없는 파일이라면 보안 경고를 우회하지 마세요.

## Windows 11 x64

1. 설정 → 시스템 → 정보 → 시스템 종류가 64비트 운영 체제, x64 기반 프로세서인지 확인합니다. Windows on ARM은 현재 대상이 아닙니다.
2. `Plaud Note Manager Community-<버전>-Windows-x64.zip`의 해시를 확인합니다.

   ```powershell
   (Get-FileHash '.\Plaud Note Manager Community-<버전>-Windows-x64.zip' -Algorithm SHA256).Hash.ToLower()
   Get-Content '.\SHA256SUMS'
   ```

3. 두 값이 같으면 ZIP을 개인 문서 폴더 아래 새 폴더에 **모두 압축 해제**합니다. ZIP 안에서 직접 실행하지 않습니다.
4. `Start Plaud Community.cmd`를 더블클릭합니다. 로컬 화면이 기본 브라우저에 열립니다.
5. 실행이 되지 않으면 `Diagnose Plaud Community.cmd`를 실행하고 `PASS` 또는 `FAIL`만 운영자에게 알립니다. 인증값이나 DB는 보내지 않습니다.

이 ZIP은 Windows 코드 서명을 받지 않아 평판 기반 경고가 나타날 수 있습니다. 출처와 SHA-256을 모두 확인했고 Windows가 해당 파일에 한정된 실행 선택지를 제공할 때만 진행하세요. SmartScreen, Smart App Control, 백신을 전역으로 끄지 마세요. 조직 정책이 실행을 차단하면 관리자를 통해 서명본을 사용해야 합니다.

### Windows 연결 방법

Windows Lite판에는 앱 안의 Web Login이 없습니다.

1. 신뢰하는 Chrome 또는 Edge 프로필에서 `https://web.plaud.ai`에 로그인합니다.
2. 개발자 도구 → Network를 열고 녹음 목록을 새로고침합니다.
3. `https://api-…plaud.ai`로 향하는 본인 계정 요청 하나를 우클릭해 **Copy as cURL (cmd)**로 복사합니다.
4. Community 로컬 화면의 Plaud cURL 칸에 붙여 넣고 **안전하게 연결**을 누릅니다.
5. 연결 후 시스템 클립보드를 평범한 문장으로 덮어씁니다. cURL에는 임시 인증값이 들어 있으므로 메신저나 지원 채널에 보내지 않습니다.

로컬 화면은 `127.0.0.1` 임의 포트에서만 열리고, 주소 조각으로 전달한 임의 세션 토큰이 없는 API 요청은 거부합니다. 브라우저 확장 프로그램이 페이지 내용을 읽을 수 있으므로 신뢰하는 브라우저 프로필만 사용하세요.

## 첫 동기화

1. macOS는 앱 안의 Plaud Web Login, Windows는 위의 cURL 가져오기로 연결합니다.
2. 목록 동기화가 끝날 때까지 기다립니다.
3. Backfill은 설명을 읽고 필요한 경우에만 실행합니다. 이 작업은 Plaud가 만든 전사·요약을 컴퓨터의 Community 전용 폴더에 저장합니다.

앱은 Plaud 비밀번호를 직접 저장하지 않습니다. macOS는 동작에 필요한 인증 묶음을 Keychain에, Windows는 현재 사용자에게 묶인 DPAPI 암호문에 보관합니다.

## macOS 로컬 빌드 설치

개발자는 프로젝트 루트에서 다음을 실행할 수 있습니다.

```bash
scripts/package-macos-app.sh
scripts/install-local.sh
```

로컬 설치 위치는 `~/Applications/Plaud Note Manager Community.app`입니다. 기존 동일 앱이 있으면 삭제하지 않고 `~/Applications/Plaud Note Manager Community Backups/`로 이동합니다.

## 문제 해결

- 인증이 만료되면 macOS는 Web Login을 다시 진행하고, Windows는 새 cURL을 다시 가져옵니다.
- 목록은 보이지만 전사가 없으면 해당 녹음의 Plaud 처리가 끝났는지 확인한 뒤 다시 동기화합니다.
- 지원 요청에는 운영체제, CPU, 앱 버전, 자가진단 PASS/FAIL만 먼저 전달합니다.
- Keychain 값, cURL, 인증 헤더, `auth.bin`, DB, 실제 녹음 제목은 보내지 마세요.
