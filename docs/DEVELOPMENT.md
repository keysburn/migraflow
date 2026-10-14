# 개발 및 운영 인계

## 1. 현재 상태와 시작점

프로젝트 경로: `C:\Users\Coreit\Documents\Projects\migraflow`

D1과 local/mounted 양쪽 조합의 전송·내용 검증을 구현했다. preflight/plan/status/run/resume/verify가 실행 가능하다. run은 rsync 전송 후 전체 SHA-256/MD5 비교를 자동 수행한다. 전송 재개·제한 자동 재시도와 실행 중 Rich 실시간 화면을 지원하며 스캔 재개, 새 회차 sync/cutover는 미구현이다.

Windows Python 3.12 + WSL rsync 및 Ubuntu Python 3.14 직접 실행의 실제 로컬 전송을 검사했다. mounted 조합은 모의 Linux 마운트 정체성과 실제 rsync로 전송·재개·양측 해시·변조 복구를 검사했다. 실제 NAS와 대규모 성능 시험은 아직 통과하지 않았다. 소스 보호는 사전 점검 결과에 표시하지만 전송 준비 완료를 선언하지 않는다. source/destination는 기존 디렉터리만 지원한다.

첫 작업은 양쪽 endpoint의 local/원격 타입과 환경 확인 및 읽기 전용 사전 점검이다. NFS/SMB 중 어떤 마운트를 사용할지는 실제 NAS가 지원하는 기능과 메타데이터 요구에 따라 정한다. 운영 NAS 전송·삭제·서비스 전환을 “개발 시작”의 일부로 자동 실행하지 않는다.

## 2. 개발 환경 제안

- 개발 저장소: 지정된 Windows 폴더
- 개발 실행: WSL2 Linux, 운영 실행: Linux 중계 인스턴스 우선
- Python: 3.12 이상, 로컬 개발 검사 버전 3.12.10
- rsync: 실행 환경에서 설치·버전·지원 옵션을 preflight로 확인
- Rich: CLI/CUI 표시, Python 표준 sqlite3와 hashlib: 상태·해시
- 테스트: pytest 제안, 패키징: pyproject.toml 기반 제안

상태 DB는 실제 로컬 Linux 디스크에 둔다. WSL2에서는 Linux 파일시스템을 사용하고 NAS 공유 또는 /mnt/c 경유 DB를 운영 기본으로 삼지 않는다. NAS 자격증명은 OS 권한·SSH 키/agent 등으로 제공하고 예제 설정에 실제 비밀을 넣지 않는다.

CLI 파서는 argparse, 설정은 사용자 요청에 따라 YAML로 채택했다. migration.example.yml을 migration.local.yml로 복사해 수정한다. 기본 파일은 현재 폴더의 migration.local.yml이며 --config로 다른 파일을 지정할 수 있다. CLI 옵션 → YAML → 기본값 순으로 적용한다. 파일의 상대 경로는 파일 위치 기준, CLI 상대 경로는 현재 폴더 기준이다. local은 기본 타입이며 state_dir 기본값은 state다. mounted는 mount_source가 필수다.

현재 수용하는 YAML 키는 source/destination(type, path, mount_source), state_dir, verification(algorithm), retry(max_attempts, initial_delay_seconds, max_delay_seconds)이다. verification.algorithm은 sha256이 기본이며 md5도 지원한다. 설정값을 계획·preflight·status에 기록하고 run/verify가 실제 양쪽 내용 해시를 비교한다. 미래 전송 정책 키는 아직 추가하지 않는다. 비밀번호/.env/환경변수 치환은 지원하지 않는다. 비밀이 필요하면 별도 설계하고 YAML·로그·Git에 노출하지 않는다. 라이선스는 아직 미확정이다.

테스트 의존성은 pytest이며 설치 명령은 README를 따른다. 검사 환경은 PyYAML 6.0.3, Rich 14.3.4, pytest 9.1.1이다.

전체 목표 디렉터리(현재는 config.py, preflight.py, planner.py, state.py, cli.py, transfer.py, rsync_adapter.py, verifier.py, locking.py, worker.py, progress.py와 tests/test_planning.py, tests/test_transfer.py, tests/test_resume.py, tests/test_mounted.py, tests/test_progress.py; 나머지는 향후 제안):
~~~text
migraflow/
  README.md
  AGENTS.md
  docs/
    REQUIREMENTS.md
    ARCHITECTURE.md
    DEVELOPMENT.md
  pyproject.toml          # D1 구현됨
  src/migraflow/
    cli.py
    coordinator.py
    preflight.py
    scanner.py
    state.py
    rsync_adapter.py
    verifier.py
    reporting.py
  tests/
~~~

작업 모듈은 실제 책임이 생길 때 만든다. 미구현 전송 명령을 성공하는 빈 명령으로 추가하지 않는다.

## 3. 개발 단계와 통과 기준

| 단계 | 구현 범위 | 다음 단계 진입 기준 |
|---|---|---|
| D0 환경 조사 | NAS·권한·프로토콜·스냅샷·대상 데이터·성능 예산 확인 | 미확정 항목과 지원 범위 기록 |
| D1 안전한 계획 | local/mounted endpoint 설정, preflight, 점진 스캔, WAL DB, plan/dry-run | 대상 쓰기 없이 계획 생성; 소스·대상 충돌/마운트 누락 차단 |
| D2 최소 전송 | local/mounted 양쪽 조합, rsync adapter, 배치, 전용 대상 경로, 결과 수집 | 작은 트리 복사와 소스 무변경 입증; 부분 성공을 성공 처리하지 않음 |
| D3 복구 | 체크포인트, 작업 잠금, 제한 재시도, 중지/재개 | 강제 종료·단절 후 누락 없이 재개; 이중 실행 차단 |
| D4 무결성과 증분 | 기본 SHA-256/선택 MD5, 세대·알고리즘 관리, 전체 검증, 증분 회차 | 변조/같은 크기·mtime 변경 탐지, SOURCE_CHANGED 무효화 |
| D5 운영 화면 | Rich CUI, 구조화 status, 로그·보고서 | 비대화형/화면 출력 일치, 진행률과 ETA 의미 확인 |
| D6 규모 검증 | 100만 → 1000만 파일, 실제 NAS 대표 표본 | 정한 자원 예산 충족, 재개 시간·DB 증가·IO 근거 확보 |
| D7 전환 준비 | cutover-check, 운영 체크리스트, 삭제 별도 계획 | 동결 상태에서 최종 증분·전체 검증·복구 연습 통과 |
| D8 확장 | P2/P3 adapter와 계약 테스트 | 초기 경로 안전성 유지, 기능 부족 시 fail-closed |

단계의 완료는 검사 결과로 판단한다. 환경이 아직 없으면 통합·규모 검증은 미완료로 기록하고 기능 구현 완료와 구분한다.

## 3.1. 현재 검증 범위와 제한

현재 시험은 설정 우선순위·잘못된 YAML, 경로 중첩, 원본/대상 내용·mtime·모드 무변경, 1001개 파일 배치와 빈 디렉터리, WAL 저장·조회, 잠금 배제, 파일/디렉터리 충돌, Unicode 정규화 충돌, 읽기 실패, 스캔 중단, endpoint 정체성 변경과 모의 마운트 조합을 포함한다.

D1 해시 설정 구현 당시 결과: 51 passed, 2 skipped. 해시 알고리즘 기본값·MD5 선택·잘못된 설정 거부·저장 및 표시·정책 fingerprint 변경·기존 계획 호환성을 추가 검증했다. Windows의 실제 symlink/hardlink 생성 시험은 권한 제한으로 skip했다. 설치된 CLI와 python -m 진입점으로 샘플 preflight·plan·status를 실행했고 파일 1개·빈 디렉터리 1개의 계획을 저장·조회했다. 로컬 샘플 설정은 migration.local.yml, 샘플 데이터는 work/demo에 두며 Git에서 제외한다.

Windows에서 심볼릭 링크/하드링크를 생성할 권한이 없으면 해당 실제 파일시스템 시험은 skip하며 통과로 간주하지 않는다. 링크 판별의 제어 흐름 시험은 별도로 수행한다. 로컬 내용 해시와 실제 rsync 파일 전송은 검증했다. 로컬 전송 복구는 D3에서 검증했다. NAS rename·권한·연결 복구·100만~1000만 규모는 아직 검증하지 않았다.

plan은 메타데이터만 조사하며 읽기 권한·호환성의 모든 문제를 검출하지 않는다. 링크/특수 파일은 BLOCKED이며 차단된 디렉터리 내부는 스캔하지 않는다. 대소문자/정규화 충돌은 보수적으로 차단한다. 대상 전용 파일의 전체 목록은 아직 만들지 않는다. 라이브 소스의 일관된 스냅샷을 보장하지 않는다.

상태 DB는 endpoint와 겹치지 않는 로컬 디스크에 두며 plan/run/verify가 이 위치에 기록한다. source/destination/state 디렉터리는 실행 중 안정적으로 유지해야 한다. hostile 경로 교체 경쟁에 대한 완전한 보호는 미구현이다. 디렉터리 열거에 따른 atime 변화는 내용·mtime 무변경과 별개다.

스캔 재개는 아직 없으므로 중단 후 plan을 다시 실행해 새 ID로 시작한다. status는 endpoint가 사라진 후에도 저장 결과를 조회할 수 있다. --job에는 현재 plan ID를 사용하며 향후 job/run 분리는 별도 스키마 변경이다.

## 3.2. 로컬 실제 전송 및 검증 결과

2026-10-13: Windows Python 3.12 + 실제 WSL rsync에서 73 passed, 2 skipped(실제 symlink 생성 권한, POSIX 전용 파일명). Ubuntu Python 3.14 직접 실행에서 76 passed. Windows에서는 추가된 WSL 경로 변환 timeout 회귀 시험 1개도 별도로 통과했다. 시스템 패키지를 변경하지 않고 프로젝트 가상환경의 pure Python 테스트 의존성을 참조했다.

실제 SHA-256/MD5 복사·DB 양측 해시·빈 파일/디렉터리·한글/공백/선행 하이픈·501개 다중 배치·덮어쓰기 보호·대상 여분 파일 보존·동일 크기/mtime 1바이트 변조·재검증·원본 루트 변경 시 이전 성공/해시 무효화·version=1 DB 업그레이드·소스 내용/mtime/모드 무변경을 확인했다. Linux는 줄바꿈과 비UTF8 파일명도 확인했다. 설치된 CLI로 두 알고리즘의 별도 샘플 run/verify를 성공시켰다.

변경 중인 원본은 root/디렉터리 mtime, 파일 세대와 해시 전후 stat/fstat로 탐지한다. 이 점검만으로 악의적인 변경이나 여러 파일의 원자적 스냅샷을 보장하지 않는다. VERIFIED는 계획 항목의 검증이며 cutover 승인이 아니다.

운영 전송은 read-only 소스 마운트를 요구한다. 격리된 개발 데이터만 --allow-unprotected-source로 예외를 허용한다. 대상 기존 파일에는 --overwrite가 필요하며 사용자가 계획과 복구 가능한 백업을 확인하는 의미다. 옵션과 rsync 호출은 preflight.execution_options에 기록한다. 자동 백업은 하지 않는다.

현재 Linux Python 전송은 local/mounted 양쪽 조합을 지원한다. Windows Python은 local→local만 지원하고 기본 WSL 배포판과 wslpath를 사용한다. batch timeout은 1800초다. 전송 중단 후 같은 계획 ID의 resume과 부분 파일 재개를 지원하며 감시 프로세스가 잔여 rsync 종료를 확인한다. 최초 전송은 계획 파일 크기 합계, 재개는 남은 파일 크기 합계 이상의 대상 여유 공간을 요구하는 보수적인 검사다.

## 3.3. D3 복구·재시도 검증

2026-10-13: Linux 전체 회귀 검사 122 passed. Windows+WSL 최종 전체 검사 120 passed, 2 skipped(실제 symlink 생성 권한, POSIX 파일명). 의존성 검사·컴파일·문서 공백 검사도 통과했다.

실제 rsync를 테스트 subprocess에서만 속도 제한해 두 번째 배치 전송 도중 coordinator를 강제 종료했다. 처음 500개 파일의 VERIFIED 체크포인트를 확인하고 실제 임시 파일이 생성된 시점에 종료했다. resume에서 rsync 복사 인자에 이 파일들이 없는 것과 대상 inode가 유지되는 것을 대조하고, 남은 부분 파일과 대용량 파일의 최종 해시 일치를 확인했다. Windows+WSL 및 Linux 모두 실제 감시 장치를 사용한다.

기존 v2 DB/구성에 retry 항목이 없는 경우의 업그레이드·해시 재사용, publish/DB commit 사이 중단, 검증 중 중단, 두 번째 배치 실패, 같은 size/mtime 대상 변조 복구, 원본 변경 차단, worker lease 점유 시 재개 차단을 시험했다. 두 알고리즘에서 검증 완료 파일의 재복사가 없는 것을 확인했다.

일시 rsync timeout/socket 오류, EAGAIN 전송·해시 읽기의 성공 재시도와 지수 백오프/상한/예산 소진을 시험했다. permission/ENOSPC/rsync23·24는 한 번만 시도한다. 자동 재시도는 실행 중인 명령 안에서만 수행하고, 프로그램 종료 후 별도 스케줄러가 자동으로 작업을 시작하지 않는다. 수동 resume은 새 예산으로 시작한다.

감시 프로세스는 부모 EOF에 따라 종료·회수하고 worker.lock이 유지된 동안 다른 전송을 차단한다. WSL 응답이 없거나 감시 장치 자체를 별도로 강제 종료한 경우에는 프로세스 상태를 확인해야 한다. v2 이전 감시 lease 없는 비정상 종료 작업은 자동 안전 판정을 할 수 없어 재개를 거부한다. 스캔 재개와 NAS 단절·전원 장애 환경은 후속 검증이다.

## 3.4. NAS 마운트 개발·운영 검증

2026-10-14: Linux 최종 전체 검사 149 passed(mounted 검사 27개 포함). Windows+WSL 전체 검사 132 passed, 16 skipped였으며 Linux 전용 루트 고정 시험을 추가한 후 Windows mounted 검사만 다시 실행해 12 passed, 15 skipped를 확인했다. skip은 Linux 전용 mounted 실행과 기존 symlink 권한/POSIX 파일명 제한에 해당한다. 의존성 검사·컴파일·git diff --check도 통과했다.

세 방향×NFS/CIFS 모의 마운트×SHA-256/MD5에서 실제 rsync로 전송 후 DB 커밋 전 중단·재개·재복사 방지·양측 해시 저장·변조 탐지/복구·소스 무변경을 확인했다. 실행 전 mount 누락/다른 공유/remount/ro 변경, 중첩 공유의 서로 다른 device, 재시도 대기 중 마운트 해제, 해시 읽기 중 mount ID 변경을 차단했다. 실제 rsync를 테스트에서만 속도 제한해 전송 중 모의 마운트를 바꾸고 rsync 종료·worker lease 해제와 원래 마운트 정체성 복원 후 재개를 확인했다. worker가 루트를 연 다음 목적지 디렉터리를 교체한 시험에서 원래 디렉터리에만 복사하고 새 빈 로컬 디렉터리에는 쓰지 않았다. 실제 NAS 서버나 마운트 해제 시스템 호출은 사용하지 않았다.

운영 시험은 Linux Python 3.12 이상과 rsync를 설치한 중계에서 수행한다. OS에서 소스를 read-only, 대상을 read-write로 마운트하고 전용 테스트 경로를 만든다. YAML의 mount_source는 /proc/self/mountinfo의 source와 일치시킨다. SQLite state_dir는 양쪽 경로 밖의 Linux 로컬 디스크에 둔다. CLI 명령과 NAS YAML은 README의 NAS 마운트 절차를 따른다. 기존 대상은 계획·백업 확인 후 --overwrite가 필요하다.

작은 대표 트리에서 source 내용/mtime/모드의 전후 비교, 양측 선택 해시 일치, 동일 크기/mtime 변조 탐지, Ctrl+C와 강제 종료 후 같은 ID 재개를 확인한다. 실제 네트워크 연결 단절도 시험하되 마운트 유지/해제 여부를 구분한다. mount 유지+원본 동일은 resume, remount는 새 plan이다. NFS hard IO의 차단·SMB inode 안정성·mtime 정밀도·NAS rename/flush/권한은 실제 장비에서 확인한다. 실패 파일이 있으면 VERIFIED 또는 운영 준비로 보고하지 않는다. 마운트 해제/다른 공유로 재연결한 경우 노출된 로컬 디렉터리에 새 파일이 생기지 않는지 확인한다.

## 3.5. Rich 실시간 화면 검증

2026-10-14: 실시간 화면/rsync 진행 표시 추가 후 Linux 전체 검사 165 passed, Windows+WSL 전체 검사 148 passed, 17 skipped. 이후 CP949 표시 보완과 실제 Live CLI 결합 시험을 추가했고 최종 화면 전용 검사는 Linux 및 Windows+WSL에서 각각 20 passed다. 컴파일·의존성 검사·git diff --check를 확인했다.

두 해시 알고리즘에서 표시 이벤트와 실제 파일·DB 결과를 대조하고 재개 시 재복사 없음, 재시도·변조 실패 건수, Ctrl+C의 INTERRUPTED 상태, 표시 observer/출력 오류로 인한 전송 방해 방지를 검사했다. 실제 rsync를 시험에서만 속도 제한해 배치 완료 전 바이트/백분율이 표시되고 그 시점에는 VERIFIED 파일이 없는 것을 확인했다. 로그 조각·CR/LF·알 수 없는 행·읽기 오류와 4096B 상한을 검사했다. JSON 단일 결과, 리다이렉션 비활성화, TTY 자동/명시 활성화와 --no-live, 잘못된 --live 조합, 좁은 화면·제어문자·literal markup·CP949/비ASCII 파일명 표시를 검사했다. 실제 CLI run/verify를 Rich Live에 연결해 복사·재검증 성공을 확인했다.

현재 화면은 D5의 실행 중 표시 기능이다. 별도 status 감시 화면·스캔 진행 화면·키보드 메뉴·최근 구간 처리량·실제 네트워크 바이트·NAS 부하·DB 크기·여유 공간의 실시간 추적은 후속 범위다. 실제 NAS 성능이나 모든 터미널 렌더링을 검증 완료로 해석하지 않는다.

## 4. CLI 계약(현재와 향후)

run/resume/verify에 --live/--no-live를 추가했다. stderr가 TTY이면 자동으로 실시간 화면을 표시하고 JSON/리다이렉션은 끈다. --live+--json 또는 비대화형 --live는 종료 코드 2다. Ctrl+C는 작업을 중단하고 worker를 종료·회수한 뒤 INTERRUPTED를 기록하며 resume으로 이어간다. 별도 관찰만 종료하는 키는 없다. 상세 사용법과 지표의 의미·제한은 README의 Rich 실시간 화면을 따른다.

2026-10-14 목적지 쓰기 점검 검증: Windows Python에서 계획/사전 점검 검사 68 passed, 1 skipped, 전체 검사 120 passed, 2 skipped. 기본 실행의 무쓰기, 권한 조회 실패 시 plan 유지, 임시 파일 실제 기록·동기화·삭제, 소스 및 기존 파일 보존, 생성/기록/flush/fsync 실패·일반 중단 시 정리, 삭제 실패 경로 보고, 경로 중첩·정체성 변경·읽기 전용/누락 마운트 차단을 확인했다. 기본 임시 폴더 접근 권한 문제로 work 아래 테스트 경로를 사용했다. 앞선 전체 검사에서 WSL rsync --version 시간 초과 1건이 있었으나 최종 전체 검사에서는 재현되지 않았다. 실제 NAS 쓰기 권한·Linux 실제 마운트 시험은 이 작업에서 수행하지 않았다.

기본 preflight와 plan은 읽기 전용 명령이다. preflight는 destination_write_permission으로 목적지 권한 조회를 보고하며 --test-destination-write를 주면 목적지에 고유 임시 파일을 생성·기록·flush·fsync·삭제하고 destination_write_test=passed를 반환한다(기본 not_requested). 시험 실패는 종료 코드 2이며 삭제 실패 시 남은 경로를 보고한다. 일반 중단은 정리를 시도하고 종료 코드 130이다. 강제 종료 시 파일이 남을 수 있고 대상 디렉터리 mtime가 바뀔 수 있다. 소스나 기존 대상 파일은 수정하지 않으며 옵션은 preflight만 지원한다. 권한 조회와 시험의 보장 범위는 README를 따른다. run/resume/sync는 대상에 쓰므로 계획·정체성 검사를 통과해야 한다. verify는 읽어서 검증하고 DB에 결과를 기록한다. status는 상태 조회만 한다. cutover-check는 준비 보고서를 생성하고 서비스 연결을 바꾸지 않는다.

현재 계획은 id와 config fingerprint를 반환한다. 향후 전송 작업은 job_id와 run_id를 분리한다. 기본 출력은 사람이 읽기 쉽고 --json은 자동화용 구조화 결과를 제안한다. CUI는 동일 엔진의 표시 계층이다.

현재 종료 코드: 0 성공, 1 실행 오류/BLOCKED/불완전 작업, 2 설정·정책·사전 점검 실패, 3 내용 해시·메타데이터·세대 검증 실패, 130 사용자 중지. 향후 별도 승인 거부 코드는 필요할 때 정의한다. 오류가 여러 개면 최종 결과와 오류 목록으로 의미를 보존한다.

설정에 필요한 항목은 source/destination 각각의 type과 경로, endpoint identity, 로컬 state_dir, 포함·제외 정책, 메타데이터 정책, 배치 크기, 동시성, timeout, retry limit, bandwidth limit이다. 수치는 측정 후 확정한다. 삭제 정책은 기본 disabled다.

첫 SIGINT는 새 배치를 중지하고 실행 중 프로세스를 안전하게 종료·회수해 체크포인트를 남긴다. 강제 종료로 커밋을 놓친 경우에도 재개 복구가 안전해야 한다. CUI 화면 종료와 작업 취소의 의미를 명확히 표시한다.

## 5. 필수 테스트

| 검사 | 시나리오 | 기대 결과 |
|---|---|---|
| endpoint 조합 | local→local, local→mounted, mounted→local, mounted→mounted; P2에서 SSH push/pull 추가 | 각 조합에 복사·증분·재개·SHA-256 검증 적용 |
| 타입 점검 | 잘못된 type, 필수 경로/호스트 누락, local로 지정한 네트워크 마운트 | 계획 단계 차단; mounted 정책 요구 |
| 소스 보호 | 모든 타입의 읽기 전용 원본, 전후 내용·메타데이터 비교 | MigraFlow에 의한 쓰기 없음 |
| 경로 보호 | 동일/중첩 경로, 루트 탈출, symlink 경로 | 실행 차단 |
| 마운트 | 실행 전/중 NAS 해제, 다른 공유 재연결 | 실패/정지; 빈 로컬 경로를 NAS로 오인하지 않음 |
| 파일명 | 공백·줄바꿈·한글·비UTF8·선행 하이픈 | 정체성 보존, 명령 주입 없음 |
| 충돌 | 대소문자/정규화 충돌, 대상 기존 데이터 | 사전 보고 및 정책 차단 |
| 재개 | rsync 중/rename 후 DB commit 전/검증 중 강제 종료 | 불확실 결과 재확인; VERIFIED 오판 없음 |
| 재시도 | 단절·timeout·권한 거부·공간 부족 | 일시 오류만 제한 재시도 |
| 검증 | 1바이트 변조·빈 파일·동일 크기/mtime 변경 | 불일치 탐지, 재전송 후 재검증 |
| 동시 변경 | 전송/해시 중 원본 변경 | 세대 무효화, 최종 완료 차단 |
| 증분 | 신규·변경·삭제·이름 변경 | 회차 차이 반영; 삭제 기본 미실행 |
| 불완전 스캔 | 읽기 거부·스캔 중단·원본 마운트 소실 | 삭제 후보 확정 및 완료 승격 차단 |
| 메타데이터 | 필수 ACL/mtime/링크/디렉터리 정책 | 보존·검증 또는 명확한 미지원 실패 |
| WAL/잠금 | 긴 reader·writer 충돌·이중 조정자·DB 복구 | 짧은 대기·직렬 쓰기·중복 실행 차단 |
| cutover | 미검증·추가 파일·소스 미동결·미승인 | 준비 완료 판정 차단 |

단위 테스트는 상태 전이·세대 무효화·정책·인자 생성에 집중한다. 통합 테스트는 실제 rsync와 임시 디렉터리에서 수행한다. NAS 호환성은 해당 NAS 조합의 마운트·권한·rename·해시 시험이 따로 필요하다. mock 결과만으로 재개·전송 호환성을 주장하지 않는다.

## 6. 규모 시험

작은 트리 → 10만 → 100만 → 1000만 파일 순으로 확장한다. 작은 파일 중심, 혼합 크기, 깊은 경로, 대규모 단일 디렉터리, 비ASCII 이름, 빈 파일, 큰 단일 파일을 포함한다. 대표 NAS 표본은 전용 시험 경로에서 사용한다.

각 시험에 CPU/RAM/rsync/NAS/네트워크 버전과 배치·동시성 설정을 기록한다. scan files/s, transfer files/s 및 bytes/s, verify bytes/s, peak RSS, DB/WAL/인덱스 크기, 대상 임시 공간, 중단 후 재개 시간, 오류율을 측정한다.

DB 인덱스와 경로 길이 때문에 1000만 행의 상태 DB가 작다고 가정하지 않는다. 상태 디스크 용량은 표본 DB 크기를 외삽하고 여유 공간 경고·정지 정책을 마련한다. 처리량·메모리·허용 복구시간 목표는 D0에서 정한다. 벤치마크하지 않은 규모를 지원 완료로 표기하지 않는다.

## 7. 운영 절차

### 최초 이전

1. 양쪽 endpoint 타입·정체성·기능·권한·원본 read-only·대상 전용 경로·복구 수단을 점검한다.
2. 계획을 생성하고 제외·충돌·미지원 속성·덮어쓰기를 검토한다.
3. 작은 표본에서 실제 복사와 전체 검증, 중지·재개를 시험한다.
4. 본 이전을 실행하며 NAS 부하, 공간, 오류, 체크포인트를 관찰한다.
5. 해당 회차를 검증하고 실패·미검증을 해결한다.
6. 운영 쓰기가 계속되면 증분 회차를 반복한다.

### 최종 전환

1. 담당자, 유지보수 시간, 원복 기준, 원본 보존 기간을 확정한다.
2. 서비스를 쓰기 동결하고 데이터 세대를 확정한다. 스냅샷으로 이전했다면 스냅샷 이후 변경분의 최종 반영 방법도 확인한다.
3. 최종 스캔·증분을 수행한다. 삭제가 필요하면 별도 승인된 계획과 대상 스냅샷을 사용한다.
4. 전체 내용·필수 메타데이터 검증과 누락/추가 항목 대조를 완료한다.
5. 검증 실패 0, 미검증 0, 미승인 차이 0을 확인하고 cutover-check 보고서에 담당자가 승인한다.
6. 운영 담당자가 공유 연결/서비스 설정을 새 NAS로 변경한다.
7. 읽기·쓰기·권한·업무 기능을 검증하고 새 NAS의 쓰기 시작 시점을 기록한다.
8. 원본은 즉시 삭제하지 않고 보존 정책에 따라 유지한다.

### 롤백

새 NAS 쓰기 시작 전에는 기존 연결 복원이 가능한지 연습한다. 새 NAS 쓰기 시작 후에는 양쪽 쓰기를 멈추고 새 변경분의 보존·역반영 또는 백업 복원 방식을 결정한다. 단순 연결 복원으로 새 데이터를 잃지 않도록 한다. MigraFlow가 양방향 동기화를 자동 제공한다고 가정하지 않는다.

## 8. 다음 개발 세션 인계문

“이 저장소의 README.md, AGENTS.md와 docs 세 문서를 읽고 MigraFlow 개발을 이어간다. 문서의 확정 사항과 제안을 구분한다. 현재 D1 계획 및 local/mounted run/resume/verify 구현과 테스트를 확인하고 실제 NAS 검증·새 회차 증분 동기화·규모 검증의 진입 조건을 정한다. 구현 전에 가정과 검증 기준을 설명하고 실제 운영 데이터 전송·삭제·cutover는 수행하지 않는다. 완료 시 변경, 검사 결과, 다음 단계의 조건을 보고한다.”
