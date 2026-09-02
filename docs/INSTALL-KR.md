# 설치 안내

## 준비

- Apple silicon Mac인지 확인합니다: Apple 메뉴 → 이 Mac에 관하여.
- macOS 14 이상인지 확인합니다.
- 기존 개인판 `Plaud Note Manager`가 있어도 Community판은 별도 앱·저장소를 사용합니다.

## ZIP으로 설치

1. 전달받은 `Plaud Note Manager Community-<버전>-macOS-arm64.zip`과 `SHA256SUMS`를 같은 폴더에 둡니다.
2. 터미널에서 파일 무결성을 확인합니다.

   ```bash
   shasum -a 256 -c SHA256SUMS
   ```

3. ZIP을 풀고 `Plaud Note Manager Community.app`을 `응용 프로그램`으로 옮깁니다.
4. 첫 실행은 Finder에서 앱을 Control-클릭 → **열기** → **열기**로 진행합니다.

현재 워크숍 빌드는 ad-hoc 서명이고 Apple 공증을 받지 않았습니다. 출처를 확인하지 못한 파일이라면 보안 경고를 우회하지 마세요.

## 로그인과 첫 동기화

1. 앱 우측 상단 인증 버튼을 누릅니다.
2. 앱 안의 Plaud Web Login에서 본인 계정으로 로그인합니다.
3. 목록 동기화가 끝날 때까지 기다립니다.
4. Backfill은 설명을 읽고 필요한 경우에만 실행합니다. 이 작업은 Plaud가 만든 전사·요약을 로컬 Application Support에 저장합니다.

앱은 원본 Plaud 비밀번호를 직접 저장하지 않습니다. 웹 로그인 이후 앱 동작에 필요한 API 인증 묶음만 macOS Keychain에 보관합니다.

## 로컬 빌드 설치

개발자는 프로젝트 루트에서 다음을 실행할 수 있습니다.

```bash
scripts/package-macos-app.sh
scripts/install-local.sh
```

로컬 설치 위치는 `~/Applications/Plaud Note Manager Community.app`입니다. 기존 동일 앱이 있으면 삭제하지 않고 `~/Applications/Plaud Note Manager Community Backups/`로 이동합니다.

## 문제 해결

- 인증이 만료되면 인증 버튼에서 Web Login을 다시 진행합니다.
- 앱이 열리지 않으면 파일명이 맞는지, macOS/CPU 요구 사항을 충족하는지 확인합니다.
- 목록은 보이지만 전사가 없으면 해당 녹음의 Plaud 처리가 끝났는지 확인하고 다시 동기화합니다.
- 해결되지 않으면 설정 창의 데이터 경로와 앱 버전을 함께 전달하되, Keychain 값이나 인증 헤더는 보내지 마세요.
