# 연결 해제 및 삭제

각 항목은 독립적입니다. 앱만 휴지통에 넣어도 Keychain과 로컬 캐시는 남습니다.

## 1. Plaud 연결 해제

프로젝트 소스에서 실행하는 경우:

```bash
uv run plaud disconnect
```

이 명령은 Community판의 전용 Keychain 인증 묶음과 `settings.env`의 인증 관련 키만 제거합니다. 녹음 DB와 캐시는 보존합니다.

앱 안에서는 인증 창의 **Clear Web Session**으로 Community판 WebKit 로그인 세션을 별도로 지울 수 있습니다.

## 2. 로컬 DB와 캐시 삭제

설정 → **Show in Finder**를 눌러 다음 전용 폴더를 확인합니다.

```text
~/Library/Application Support/com.cmdspace.PlaudNoteManagerCommunity/
```

앱을 완전히 종료한 뒤 이 폴더를 Finder의 휴지통으로 이동합니다. 이 작업은 로컬 캐시만 지우며 Plaud Cloud의 원본 녹음을 삭제하지 않습니다.

## 3. 앱 삭제

`Plaud Note Manager Community.app`을 Finder의 휴지통으로 이동합니다. 개인 개발판 `Plaud Note Manager.app`과 이름과 Bundle ID가 다르므로 대상 앱명을 확인하세요.

## 4. 사용자가 내보낸 파일

Download/Export로 Application Support 밖에 저장한 파일은 자동으로 추적하거나 삭제할 수 없습니다. 사용자가 선택했던 폴더를 직접 확인합니다.
