# 자동 폴더 정리

Community판의 자동 라우터는 사용자의 현재 Plaud 계정에 이미 존재하는 폴더만 후보로 사용합니다. 앱에 제작자의 개인 폴더 구조나 숨은 분류 체계가 들어 있지 않으며 새 폴더를 만들지 않습니다.

## 안전한 실행 순서

1. **Sync**로 현재 녹음과 폴더 목록을 가져옵니다.
2. **자동 폴더 정리**에서 로컬 전용 또는 선택한 AI 미리보기를 실행합니다.
3. 제안과 근거를 검토합니다. 신뢰도 60% 미만은 적용할 수 없습니다.
4. 맞는 녹음만 선택해 Plaud Cloud 변경을 한 번 더 확인합니다.
5. macOS 앱·Windows 화면·CLI에서는 적용 직후 **Undo/되돌리기**로 정확한 이전 폴더를 복원할 수 있습니다.

미리보기 자체는 Plaud Cloud를 바꾸지 않습니다. 적용은 30분 이내의 저장된 미리보기와 그때 발급된 정확한 `plan_id`만 사용하고 모델을 다시 호출하지 않습니다. 폴더 목록, 녹음의 기존 로컬 배치 또는 Plaud Cloud 배치가 달라졌으면 첫 Cloud 변경 전에 전체 적용을 거부합니다. 되돌리기도 최근 적용 직후의 대상 폴더가 Cloud와 로컬에 그대로인지 전 항목을 먼저 검사하며, 충돌이 있으면 아무 항목도 변경하지 않습니다.

적용·되돌리기 기록은 충돌 복구용 WAL과 함께 로컬에 내구성 있게 남습니다. 앱을 다시 열어도 macOS와 Windows 화면이 읽기 전용 상태 점검으로 Undo를 다시 표시합니다. 적용 도중 앱이 중단됐다면 첫 번째 명시적 동작은 기록된 적용을 안정화할 뿐 되돌리지 않고, 화면이 그 사실을 알린 뒤 두 번째 명시적 Undo에서만 이전 폴더를 복원합니다. Community macOS의 이 복구 배너는 자동으로 사라지지 않습니다.

## 로컬 규칙과 AI 판정

로컬 규칙은 폴더 이름을 제목·키워드·요약·전사와 비교하며 외부 AI를 호출하지 않습니다. AI 모드를 선택하면 로컬 점수가 약한 항목만 선택한 공급자가 판정합니다.

- 한 번에 최근 미분류 녹음 최대 200개
- 외부 AI 요청은 미리보기당 최대 20회
- 매 AI 미리보기마다 폴더 이름과 녹음 텍스트 전송·비용 가능성을 확인
- Claude·Codex: 각 CLI의 앱 로그인 또는 개인 API key
- Gemini·Grok: 개인 API key만 지원
- API key: macOS Keychain 또는 Windows DPAPI에 공급자별로 분리 저장

모델 응답은 임의의 폴더를 만들 수 없습니다. 현재 Plaud API가 반환한 정확한 폴더 ID/이름과 일치하지 않거나 응답이 잘못되면 로컬 규칙 결과로 되돌아가며 UI에 fallback 상태를 표시합니다.

CLI 모드는 공급자의 OAuth/access token 값을 이 앱으로 가져오지 않습니다. Claude는 세션 저장 없이 제한된 무도구 실행으로, Codex는 사용자 설정을 섞지 않는 strict config와 네트워크·셸·MCP·웹 계열 기능 비활성화를 사전 확인한 뒤 읽기 전용 분류 프롬프트만 실행합니다. 필요한 격리 옵션을 설치된 CLI가 지원하지 않으면 외부 AI 실행 자체를 거부합니다.

## CLI

외부 전송 없는 미리보기:

```bash
uv run plaud auto-folder --json --limit 200
```

Claude CLI 로그인 세션을 이용한 미리보기:

```bash
uv run plaud auto-folder --json --limit 200 --llm --provider claude --backend cli --confirm-external
```

개인 API key를 쓰는 예(키 값은 argv가 아니라 stdin으로만 전달):

```bash
pbpaste | uv run plaud provider-key-set anthropic
uv run plaud config-backend claude api
uv run plaud config-model claude YOUR_MODEL_ID
uv run plaud auto-folder --json --limit 200 --llm --provider claude --backend api --confirm-external
```

OpenAI는 key 저장 이름이 `openai`, 라우팅 공급자 이름이 `codex`입니다. Gemini와 Grok은 각각 `gemini`, `grok`을 사용하며 API backend만 허용됩니다. 패키지 앱에서는 같은 값을 **Settings/설정** 화면에서 저장할 수 있습니다.

선택한 정확한 항목만 적용:

```bash
uv run plaud auto-folder --apply --only FILE_ID --plan-id PLAN_ID --json
```

`PLAN_ID`는 바로 앞에서 검토한 preview JSON의 모든 행에 동일하게 포함된 32자리 `plan_id`입니다. 새 미리보기가 저장되면 이전 값은 더 이상 적용 권한이 아닙니다.

되돌리기:

```bash
uv run plaud classify-undo --json
```

재시작 뒤 네트워크 호출 없이 Undo/복구 가능 여부만 확인:

```bash
uv run plaud classify-undo-status --json
```

`--confirm-external`은 이번 실행에만 적용되며 설정에 영구 동의로 저장되지 않습니다.
