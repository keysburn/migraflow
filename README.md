# MigraFlow

로컬 파일시스템과 원격 NAS 사이의 대규모 파일 이전을 위한 Python CLI/CUI 프로젝트입니다. 실행 인스턴스에서 rsync 전송을 제어하고 SQLite 상태관리, 재개, SHA-256 검증을 제공하는 것을 목표로 합니다.

## 현재 구현 상태

2026-10-13 기준 D1 계획 기능에 로컬→로컬 rsync 전송과 전체 파일 내용 해시 검증을 추가했습니다. YAML/CLI 설정, 사전 점검, 점진 스캔, SQLite WAL 저장, Rich/JSON 출력, run·resume·verify·status를 사용할 수 있습니다.

plan은 계획만 생성하며 run이 실제 전송 후 자동 검증합니다. verify는 저장된 전송을 다시 검증합니다. Windows Python + WSL rsync 및 Ubuntu 직접 실행으로 실제 로컬 전송을 검증했습니다. 같은 계획 ID의 전송 재개·검증 완료 체크포인트 재사용·일시 오류 제한 재시도를 지원합니다. NAS 전송·스캔 재개·새 회차 간 증분 최적화·cutover·실시간 CUI와 대규모 성능 검증은 아직 미구현/미검증입니다.

## 설치

Python 3.12 이상이 필요합니다. 프로젝트 폴더의 PowerShell에서 실행합니다.

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
~~~

이미 가상환경이 있다면 활성화와 설치만 진행합니다.

실제 전송에는 rsync가 필요합니다. Windows CLI는 기본 WSL 배포판의 rsync와 표준 Python 3 감시 프로세스를 호출합니다. 아래 명령으로 사용할 수 있는지 확인합니다.

~~~powershell
wsl --exec rsync --version
wsl --exec python3 --version
~~~

기본 WSL 배포판이 Ubuntu라면 rsync가 없을 때 Ubuntu 터미널에서 설치합니다.

~~~sh
sudo apt update
sudo apt install rsync python3
~~~

Linux에서 Python CLI를 실행할 때는 같은 환경의 rsync를 사용합니다. Linux CLI에서는 Linux 경로와 로컬 상태 디스크를 사용합니다. Windows CLI는 고정 로컬 드라이브 경로만 지원하며 WSL 경로 변환이 실패하면 전송을 중지합니다.

## 설정 파일로 사용

출발지·목적지·상태 저장 위치는 YAML에 미리 작성합니다.

~~~powershell
Copy-Item migration.example.yml migration.local.yml
~~~

migration.local.yml을 수정합니다. 출발지와 목적지는 미리 존재하는 디렉터리여야 합니다. Windows 경로는 작은따옴표를 사용하면 역슬래시를 그대로 적을 수 있습니다.

이 개발 세션에서는 migration.local.yml을 work/demo의 로컬 샘플 경로로 생성했습니다. 현재 파일을 유지하면 바로 계획 기능을 시험할 수 있습니다. 실제 경로를 사용하기 전에 이 파일을 수정하세요. 샘플과 로컬 설정은 Git에서 제외합니다.

~~~yaml
source:
  type: local
  path: 'C:\data\source'
destination:
  type: local
  path: 'C:\data\destination'
state_dir: state
~~~

~~~powershell
migraflow preflight
migraflow plan
migraflow status
~~~

기본 파일은 현재 폴더의 migration.local.yml입니다. 다른 파일은 명시적으로 지정합니다.

~~~powershell
migraflow plan --config my-migration.yml --json
migraflow status --config my-migration.yml --job PLAN_ID
~~~

YAML 안의 상대 경로는 설정 파일 위치 기준입니다. 알 수 없는 키, 중복 키, 잘못된 타입을 거부합니다. .env 로딩과 환경변수 치환은 현재 지원하지 않으며 YAML에 비밀번호를 넣지 않습니다.

## 목적지 쓰기 권한과 실제 쓰기 시험

기본 preflight는 목적지의 쓰기·탐색 권한과 읽기 전용 마운트 여부만 조회하고 파일을 만들지 않습니다. destination_write_permission은 권한 조회 결과이며 실제 쓰기를 보장하지 않습니다. 특히 Windows ACL과 NAS 권한은 실제 쓰기 결과와 다를 수 있습니다. 권한 조회가 실패하면 경고하며 읽기 전용 plan은 계속 사용할 수 있습니다.

~~~powershell
migraflow preflight --config migration.local.yml --json
migraflow preflight --config migration.local.yml --test-destination-write --json
~~~

--test-destination-write는 preflight에서만 지원합니다. 안전 점검과 소스·목적지 정체성 재확인 후 목적지 루트에 .migraflow-write-test-로 시작하는 고유 임시 파일을 배타적으로 생성하고, 작은 시험 데이터를 기록·flush·fsync한 뒤 닫고 삭제합니다. 성공하면 destination_write_test는 passed, 기본 실행은 not_requested입니다. 소스와 기존 대상 파일은 수정하지 않지만 목적지 디렉터리 mtime는 바뀔 수 있습니다. YAML에 옵션을 저장하거나 plan/run/resume/verify에서 자동 실행하지 않습니다.

생성·기록·동기화·삭제 실패는 종료 코드 2로 보고합니다. 실패나 일반 사용자 중단에도 임시 파일 삭제를 시도하며 삭제 실패 시 남은 파일 경로를 오류에 표시합니다. 강제 종료·전원 장애 시 파일이 남을 수 있습니다. 시험 성공은 기존 파일 덮어쓰기·하위 디렉터리 권한·미래 전송 성공을 보장하지 않으며 transfer_ready와 cutover_ready도 승격하지 않습니다.

## 로컬→로컬 실제 전송

먼저 plan 결과의 id와 덮어쓰기 후보를 검토합니다.

~~~powershell
migraflow plan
migraflow run --job PLAN_ID
migraflow verify --job PLAN_ID
migraflow status --job PLAN_ID
~~~

run은 설정과 endpoint 정체성이 저장된 계획과 일치하는지 확인하고, 계획에 있는 정규 파일과 빈 디렉터리를 rsync로 전송합니다. 모든 정규 파일을 설정된 SHA-256/MD5로 양쪽에서 다시 읽어 비교하고 파일 크기·mtime, 디렉터리 존재·mtime도 확인합니다. 검증 성공은 VERIFIED, 내용 또는 메타데이터 불일치는 VERIFY_FAILED입니다. 전송 실패를 성공으로 표시하지 않습니다.

운영에서는 읽기 전용 소스 마운트를 요구합니다. 읽기 전용 보호가 확인되지 않는 Windows/개발용 로컬 샘플은 격리된 테스트 데이터에서 다음 옵션으로만 실행합니다. 이 옵션은 소스의 외부 쓰기를 막지 않으며 사용 여부를 작업 기록에 저장합니다.

~~~powershell
migraflow run --job PLAN_ID --allow-unprotected-source
~~~

대상에 이미 있는 파일은 기본적으로 덮어쓰지 않습니다. 계획을 검토하고 복구 가능한 백업을 확보한 작업에만 다음 옵션을 사용합니다.

~~~powershell
migraflow run --job PLAN_ID --overwrite --allow-unprotected-source
~~~

원본과 대상에만 있는 파일은 삭제하지 않습니다. rsync는 임시 파일을 작성한 뒤 최종 파일을 교체하며 --inplace·--delete·--remove-source-files는 사용하지 않습니다. 새 계획의 최초 전송은 같은 크기·mtime만으로 건너뛰지 않습니다. 재개에서는 저장된 해시와 현재 양쪽 내용을 대조해 일치하는 파일을 복사 목록에서 제외합니다. 새로운 계획/회차 사이의 증분 최적화는 이후 단계입니다.

소스·endpoint·알고리즘이 바뀐 경우 새 plan을 생성해야 합니다. 중단/실패한 같은 계획은 resume으로 계속할 수 있습니다. 작업 기록은 state_dir/migraflow.sqlite3, rsync 로그와 NUL 매니페스트는 state_dir/transfer-PLAN_ID에 저장합니다. 과거 기록은 자동 삭제하지 않습니다.

## 중단 후 재개와 자동 재시도

Ctrl+C 또는 프로그램 강제 종료 후 **새 plan을 만들지 않고 같은 PLAN_ID**로 실행합니다.

~~~powershell
migraflow resume --job PLAN_ID
migraflow status --job PLAN_ID
~~~

읽기 전용 보호가 없는 격리된 개발용 경로는 재개에도 명시적으로 옵션을 전달합니다.

~~~powershell
migraflow resume --job PLAN_ID --allow-unprotected-source
~~~

배치마다 복사 후 해시 검증을 커밋합니다. 재개 시 SQLite 무결성·설정·소스/대상 정체성·원본 세대를 확인합니다. 이미 검증된 파일도 현재 양쪽 해시를 다시 읽어 확인하지만, 일치하면 **rsync로 다시 복사하지 않습니다**. 전송 완료 후 DB 기록 전에 중단된 파일도 전체 해시가 일치하면 검증 완료로 복구합니다. 따라서 재개 중 읽기 IO는 발생합니다.

대상에서 변조·누락된 작업 소유 파일은 재전송합니다. 원본이 바뀌면 기존 계획의 재개를 중지하고 새 plan을 요구합니다. 같은 계획이 만든 것으로 확인된 파일의 복구 교체는 허용하지만, 출처가 불확실한 기존 대상 파일은 --overwrite 없이 교체하지 않습니다. 최초 --overwrite 승인 범위는 같은 계획의 재개에 유지됩니다.

rsync 부분 파일은 대상 각 디렉터리의 .migraflow-partial-PLAN_ID에 보존하고 다음 전송의 basis로 사용합니다. 완료 후 rsync가 사용한 부분 파일을 정리합니다. 부분 파일을 완료된 데이터로 판정하지 않고 최종 전체 해시 검증을 요구합니다. 중단된 작업을 재개할 때 상태 DB와 부분 파일을 지우지 마세요.

감시 프로세스는 부모 프로그램의 연결이 끊기면 rsync 프로세스 그룹을 정리합니다. Windows에서는 WSL 안에도 감시 프로세스를 두어 Linux rsync를 정리합니다. worker.lock이 해제되기 전에는 다른 전송을 시작하지 않습니다. 이전 감시 프로세스가 응답하지 않으면 재개를 차단하며, 잠금 파일을 삭제해 강제로 우회하지 않습니다.

자동 재시도 설정은 다음과 같습니다.

~~~yaml
retry:
  max_attempts: 3
  initial_delay_seconds: 1
  max_delay_seconds: 30
~~~

max_attempts는 최초 시도를 포함합니다. 기본값에서는 일시 오류 시 1초·2초 대기 후 다시 시도합니다. max_attempts는 1~10, 대기 시간은 0~30초이며 initial_delay_seconds는 max_delay_seconds 이하입니다. 재시도 정책만 바꾸는 것은 새 계획을 요구하지 않습니다.

자동 재시도는 rsync의 socket/protocol-stream/timeout 오류(10·12·30·35)와 명확한 일시 OS IO 오류, 일시적인 해시 읽기 오류에 적용합니다. 권한 거부·공간 부족·경로/설정 문제·원본 사라짐·해시 불일치와 원인이 불명확한 partial-transfer 오류(23)는 자동 재시도하지 않습니다. 제한에 도달하면 실패로 기록하고 운영자가 원인을 해결한 후 resume합니다. 수동 resume은 새 시도 예산을 시작하며 이전 실패/대기 이력은 보존합니다.

status/JSON의 retry_count는 누적 재시도 수, reused_files는 가장 최근 재개에서 복사 없이 재사용한 파일 수, verified_files는 현재 검증 완료 건수입니다. 실행 상태에 RECOVERING과 RETRY_WAIT가 추가되었습니다.

현재 재개는 전송·검증만 지원하며 불완전 스캔은 새 plan이 필요합니다. 이 버전 이전에 감시 장치 없이 강제 종료된 legacy 작업은 남은 프로세스의 안전한 종료를 입증할 수 없어 재개를 차단합니다. 이전 프로세스를 확인한 후 새 계획으로 진행하세요. 저장된 legacy 완료 파일의 해시는 DB 업그레이드 후 재사용할 수 있습니다.

## 검증 알고리즘 설정

내용 검증 알고리즘의 기본값은 SHA-256이다. MD5를 선택하려면 YAML에 다음 항목을 추가한다.

~~~yaml
verification:
  algorithm: md5
~~~

허용 값은 sha256과 md5다. verification 또는 algorithm을 생략하면 sha256을 사용한다. 선택값은 preflight, plan, status의 hash_algorithm에 표시하고 계획에 저장한다. 기존 계획 조회는 현재 설정이 아닌 저장 당시의 알고리즘을 보여 준다. 알고리즘 설정이 없던 과거 계획은 당시 기본값인 sha256으로 표시한다.

run과 verify가 선택한 알고리즘으로 실제 파일 내용을 1MiB 청크로 읽어 비교한다. 알고리즘과 양쪽 해시·검증 상태를 파일별로 SQLite에 기록한다. MD5 선택 시 충돌 저항성이 SHA-256보다 약하다는 경고를 표시한다. 내부 설정 fingerprint는 내용 검증 알고리즘과 별개로 항상 SHA-256을 사용한다.

## 명령어만 사용하거나 설정 덮어쓰기

설정 파일 없이도 실행할 수 있습니다.

~~~powershell
migraflow plan --source 'C:\data\source' --destination 'D:\data\destination' --state-dir state
~~~

파일이 있으면 명령어 옵션이 우선합니다.

~~~powershell
migraflow plan --config migration.local.yml --destination 'D:\another-destination'
migraflow status --state-dir state --json
~~~

우선순위는 명령어 옵션 → YAML → 기본값입니다. 명령어의 상대 경로는 현재 작업 폴더 기준입니다. CLI 진입점 대신 python -m migraflow도 사용할 수 있습니다.

## 지원 타입과 안전 규칙

출발지와 목적지는 독립적으로 local 또는 mounted를 지정합니다. local은 실행 인스턴스의 경로입니다. mounted는 Linux에 마운트한 원격 NAS이며 mount_source에 예상 공유 정체성을 필수로 적습니다. 예: nas-source:/export/data. 마운트 없는 SSH push/pull은 다음 단계입니다.

| 조합 | 초기 경로 |
|---|---|
| 로컬 → 로컬 | local → local |
| 로컬 → NAS | local → mounted |
| NAS → 로컬 | mounted → local |
| NAS → NAS | mounted → mounted |

Windows에서는 고정 로컬 드라이브의 계획 기능을 지원하고 네트워크 드라이브·UNC·mounted는 차단합니다. Linux에서는 예상 마운트 정체성을 확인하며 공유를 local로 지정해 마운트 검사를 우회할 수 없습니다.

동일·중첩 경로, symlink/junction 루트, endpoint 내부 상태 저장 위치를 거부합니다. 상태 DB는 로컬 디스크에만 저장합니다. 기본 preflight와 plan은 양쪽 데이터를 수정하지 않고 run은 대상만 수정합니다. preflight --test-destination-write는 위에서 설명한 목적지 임시 파일 쓰기 시험을 수행합니다. 원본 내용·mtime·권한은 변경하지 않습니다. 디렉터리 조회가 atime에 영향을 줄 수 있으므로 원본 접근시간 보존은 별도 마운트/스냅샷 정책이 필요합니다. 소스 read-only 보호가 확인되지 않으면 경고하며 run은 개발용 명시 옵션 없이 진행하지 않습니다. transfer_ready와 cutover_ready는 아직 운영 준비 조건 전체를 구현하지 않아 false로 유지합니다.

계획 상태 PLANNED는 메타데이터 스캔 완료만 뜻합니다. 같은 크기·mtime는 QUICK_MATCH_UNVERIFIED이며 해시 검증이 아닙니다. COPY_REPLACE는 --overwrite가 필요한 덮어쓰기 후보입니다. 링크·특수 파일·파일/디렉터리 충돌·대소문자/Unicode 정규화 충돌은 BLOCKED로 표시합니다. 초기 충돌 검사는 안전을 위해 대소문자를 구분하는 파일시스템에서도 보수적으로 적용합니다.

현재 대상 전용 파일 목록, ACL/xattr·경로 길이·예약 이름·시간 정밀도 호환성 검사는 미구현입니다. plan은 최종 전송 또는 cutover 승인 자료를 대체하지 않습니다. source/destination/state는 실행 중 관리자가 안정적으로 유지해야 하며 적대적인 동시 경로 교체에 대한 완전한 보호는 아직 제공하지 않습니다.

계획 생성마다 새 ID를 발급합니다. 중단된 스캔은 재개하지 않고 plan을 다시 실행합니다. 실패·중단 상태를 성공으로 표시하지 않습니다. 종료 코드는 0 계획/전송/조회 성공, 1 실행 실패·BLOCKED·불완전 작업, 2 설정/정책/사전 점검 실패, 3 해시·메타데이터·세대 검증 실패, 130 사용자 중지입니다. status도 저장된 실패 상태에 따라 비정상 종료 코드를 반환합니다.

## 확정한 전체 목표

- NAS → 중계 → NAS와 로컬/원격 모든 방향
- Python CLI 및 Rich CUI, rsync 스트리밍 중계
- SQLite WAL, 체크포인트·제한 재시도·재개
- 내용 해시 검증(기본 SHA-256, 선택 MD5), 증분 동기화, 모니터링
- 100만~1000만 파일, 총 데이터 10TB 미만
- 소스 무변경, 삭제 기본 비활성화, 단계별 프로토콜 확장, 안전한 cutover

이전은 복사·동기화를 의미하며 복사 후 원본 자동 삭제는 포함하지 않습니다. Windows에서의 실제 전송은 WSL rsync를 통해 수행하며 Windows 네이티브 rsync 엔진은 지원하지 않습니다. NAS 운영 전송은 Linux를 우선 대상으로 합니다.

## 개발 문서와 테스트

- [요구사항](docs/REQUIREMENTS.md)
- [아키텍처](docs/ARCHITECTURE.md)
- [개발 계획](docs/DEVELOPMENT.md)
- [개발 에이전트 규칙](AGENTS.md)

~~~powershell
python -m pytest -q
~~~

확정 요구사항, 제안, 구현 완료, 검증 완료를 문서에서 구분합니다. 이전 ChatGPT 설계 본문은 조회 시 참조 표시만 반환되어 복구하지 못했으며, 요구사항 근거는 이 프로젝트에서 받은 사용자 요청입니다. 환경 조사 항목은 NAS 제품·프로토콜·메타데이터·스냅샷·대상 기존 데이터·중단 시간·담당자·라이선스입니다.

기술 근거: [rsync 매뉴얼](https://download.samba.org/pub/rsync/rsync.1.html), [SQLite WAL](https://www.sqlite.org/wal.html), [PyYAML 문서](https://pyyaml.org/wiki/PyYAMLDocumentation). 실제 NAS 호환성과 성능은 별도 검증이 필요합니다.
