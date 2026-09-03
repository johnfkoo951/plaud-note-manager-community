# 연결 해제 및 삭제

각 항목은 독립적입니다. 앱이나 압축을 푼 폴더만 지워도 운영체제 보안 저장소와 로컬 캐시는 남습니다.

## 1. Plaud 연결 해제

프로젝트 소스에서 실행하는 경우:

```bash
uv run plaud disconnect
```

이 명령은 macOS Community판의 전용 Keychain 인증 묶음과 `settings.env`의 인증 관련 키만 제거합니다. 녹음 DB와 캐시는 보존합니다.

앱 안에서는 인증 창의 **Clear Web Session**으로 Community판 WebKit 로그인 세션을 별도로 지울 수 있습니다.

Windows에서는 로컬 화면의 **연결 해제**를 누릅니다. 이 작업은 DPAPI 암호문 `auth.bin`과 인증 관련 설정만 지우고 로컬 DB·전사·내보내기는 유지합니다. Windows Lite판에는 별도 WebKit 로그인 세션이 없습니다.

## 2. 로컬 DB와 캐시 삭제

설정 → **Show in Finder**를 눌러 다음 전용 폴더를 확인합니다.

```text
~/Library/Application Support/com.cmdspace.PlaudNoteManagerCommunity/
```

앱을 완전히 종료한 뒤 이 폴더를 Finder의 휴지통으로 이동합니다. 이 작업은 로컬 캐시만 지우며 Plaud Cloud의 원본 녹음을 삭제하지 않습니다.

Windows에서는 브라우저 화면에서 **앱 종료**를 누른 뒤 다음 폴더를 파일 탐색기 휴지통으로 이동합니다.

```text
%LOCALAPPDATA%\CMDSPACE\PlaudNoteManagerCommunityLite\
```

이 폴더에는 DB·캐시와 기본 `exports` 내보내기가 함께 있습니다. 보존할 내보내기를 먼저 다른 위치로 옮기세요.

## 3. 앱 삭제

`Plaud Note Manager Community.app`을 Finder의 휴지통으로 이동합니다. 개인 개발판 `Plaud Note Manager.app`과 이름과 Bundle ID가 다르므로 대상 앱명을 확인하세요.

Windows에서는 압축을 풀었던 `Plaud Note Manager Community` 폴더를 휴지통으로 이동합니다. 이 작업만으로 `%LOCALAPPDATA%`의 인증·캐시가 지워지지는 않습니다.

## 4. 사용자가 내보낸 파일

Download/Export로 Application Support 밖에 저장한 파일은 자동으로 추적하거나 삭제할 수 없습니다. 사용자가 선택했던 폴더를 직접 확인합니다.
