# 월간 CMDS 20회차 운영 체크리스트

## 세션

- 제목: 수집의 기술 — 사라지기 전에 잡고, 쓸모 있게 거르고, 필요할 때 되찾기
- 일시: 09/03(목) 19:30–22:30
- 실습: Plaud Note 활용, 만료 가능한 개인 데이터 확보, 전사·요약 점검, 재검색

## D-1 배포자 점검

- [ ] 깨끗한 Community 전용 Git 이력에서 빌드함
- [ ] `scripts/audit-source.sh` 통과
- [ ] Python 테스트·Ruff·Swift release build 통과
- [ ] `scripts/package-macos-app.sh`와 `scripts/package-macos-intel.sh` 통과
- [ ] `python3 scripts/package-windows-portable.py` 정적 감사 통과
- [ ] 실제 Windows 11 x64에서 packaged HTTP·DPAPI 자가진단 통과
- [ ] 최종 앱에 `.env`, DB, 로그, 테스트 fixture, `.git`이 없음
- [ ] 앱 바이너리/런타임에서 빌드 사용자 홈 경로와 개인 볼트명이 검출되지 않음
- [ ] ZIP SHA-256 확인
- [ ] 개인판과 Community판의 Bundle ID, Keychain, Application Support, WebKit 세션 분리 확인
- [ ] arm64/x86_64/Windows x64 ZIP과 `SHA256SUMS`가 한 세트인지 확인
- [ ] macOS 미공증·Windows 미서명 제한과 실기기 검증 상태를 참가자에게 사전 공지

## 참가자 사전 안내

- [ ] 개인 Mac 또는 Windows 11 x64 PC와 본인 Plaud 계정 준비
- [ ] 민감한 녹음은 화면 공유 전에 숨기거나 별도 계정/자료 사용
- [ ] 첫 실행 Control-클릭 → 열기 절차 안내
- [ ] Windows 참가자에게 압축 전체 해제, cURL (cmd) 복사, 연결 후 클립보드 덮어쓰기 안내
- [ ] SmartScreen·Smart App Control·백신을 전역으로 끄지 않도록 안내
- [ ] Backfill이 전사·요약을 Mac에 저장한다는 점 안내
- [ ] 수업 종료 후 연결 해제·Web Session·로컬 캐시 삭제 방법 제공

## 19:30–20:00 설치와 안전 경계

- [ ] 체크섬 확인 후 설치
- [ ] Community판 저장 위치와 외부 AI 비활성화 설명
- [ ] macOS는 앱 내 Web Login을 우선하고, 멈추면 인증 창의 cURL 폴백 사용. Windows는 로컬 화면의 cURL 가져오기
- [ ] Sync로 목록만 먼저 확인

## 20:00–21:00 수집

- [ ] Plaud 녹음 한 건의 제목·시간·처리 상태 확인
- [ ] 필요한 항목만 상세 동기화/내보내기 시연
- [ ] 카카오톡·문자·웹 링크는 각 서비스의 공식 내보내기·저장 방법을 우선 사용
- [ ] 만료 시점·원본 URL·수집일·출처를 함께 남기는 원칙 설명

## 21:00–22:00 점검과 거르기

- [ ] 전사 오인식, 화자, 날짜, 고유명사 확인
- [ ] Plaud 요약이 원문에 근거하는지 표본 대조
- [ ] 제목·폴더 변경은 소수 항목으로 먼저 시험하고 Cloud 반영 확인
- [ ] 민감도와 재사용 가치에 따라 보존/삭제/격리 결정

## 22:00–22:30 되찾기와 종료

- [ ] 제목·전사·요약 검색 실습
- [ ] 검색 결과에서 원 녹음으로 돌아가기
- [ ] 내보낸 파일의 저장 위치 확인
- [ ] 공용/대여 Mac 사용자는 연결 해제, Web Session 삭제, 캐시 삭제
- [ ] 실패 사례와 앱 버전만 수집하고 인증 헤더·DB 원본은 지원 채널에 올리지 않기

## 수업 후 기록

- 설치 성공/실패 수, OS/기기 분포, 실패 단계만 비식별 집계합니다.
- 실제 녹음 제목, 계정 식별자, 토큰, DB, 스크린샷은 동의 없이 수집하지 않습니다.
- API 변경·인증 실패·검색 누락은 재현 조건과 앱 버전을 함께 기록합니다.
