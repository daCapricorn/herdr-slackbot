# Herdr Bridge

[English](README.md) | **한국어**

이 포크는 독립 Slack 봇에서 OpenClaw 기반 `herdr-bridge`로 변경되었습니다. Herdr가 로컬 터미널을 관리하고, Bridge가 이벤트와 정확한 승인 요청을 연결하며, OpenClaw가 Slack 인증·메시지·버튼 콜백을 처리합니다.

현재 지원 경로는 OpenClaw 봇의 Slack DM입니다. 에이전트 상태 조회, 지정 pane 읽기, 대기 중인 에이전트에 프롬프트 보내기, 인식된 Codex 명령 승인과 Claude 단일 선택 대화 상자 처리를 지원합니다. Bridge에 별도 Slack token이나 새 Slack 앱을 설정할 필요가 없습니다.

설치 방법과 현재 기능 범위는 [영문 README](README.md)와 [사용 가이드](docs/GUIDE.md)를 참고하세요. 새로운 실행 방식은 macOS에서 검증했으며 Windows 통합은 아직 실제 환경에서 검증하지 않았습니다.

기존 Home 탭, 새 에이전트 모달, 페어링과 스레드 라우팅은 [보관된 독립 봇 문서](legacy/README.ko.md)에 설명되어 있습니다. 이 기능들은 새로운 플러그인의 기능이 아닙니다.
