# 아키텍처

## 1. 확정과 제안

확정 구성은 중계 인스턴스, Python CLI/CUI + Rich, rsync, SQLite WAL, 체크포인트·재개와 SHA-256이다. 아래 모듈·스키마·전송 배치·지원 OS는 요구사항을 구현하기 위한 제안이다.

## 1.1. 현재 D1 및 전송·검증 구현

설정은 사용자 요청에 따라 YAML로 채택했다. TOML 제안은 폐기하고 PyYAML SafeLoader 기반 로딩과 argparse 옵션을 사용한다. 객체 생성 태그·중복 키·알 수 없는 키를 거부한다. 명령어 옵션이 파일보다 우선하며 환경변수/.env 치환은 아직 지원하지 않는다.

내용 검증 정책은 verification.algorithm으로 설정하며 기본 sha256, 선택 md5를 지원한다. 설정 검증·계획 저장·표시와 실제 로컬 파일 내용 해시 비교를 구현했다. Config.hash_algorithm을 설정 JSON에 포함해 계획 fingerprint에도 반영한다. 설정 fingerprint 자체의 알고리즘은 SHA-256으로 유지한다. 이전 계획에 필드가 없으면 기존 기본값 sha256으로 해석한다. MD5 선택 시 preflight 경고를 저장한다.

현재 모듈은 config, preflight, planner, state, cli, transfer, rsync_adapter, verifier, locking, worker다. DB는 plans, entries, directories, file_results, attempts와 user_version=3을 사용한다. version=1/2 DB를 업그레이드하고 file_results.copied(성공적인 대상 게시의 소유 근거)와 attempts를 추가한다. 기존 계획에 소스 루트 세대 기록이 없으면 전송용 새 계획이 필요하다. 아래 jobs/runs/file_versions 스키마는 향후 제안이며 실제 생성된 스키마가 아니다.

planner는 DB의 pending 디렉터리 큐를 순차적으로 열거하고 500개 항목마다 커밋한다. 상대 경로는 BLOB으로 저장한다. 전체 목록을 메모리에 적재하지 않는다. state_dir별 OS 파일 잠금으로 writer를 제한한다. 잠금은 프로세스 종료 시 해제된다. status는 읽기 전용 DB 연결을 사용한다.

계획은 SCANNING → PLANNED/BLOCKED/FAILED/INTERRUPTED다. 강제 종료로 남은 SCANNING은 불완전 상태다. 현재 스캔 재개는 미구현이며 새 plan을 실행한다. 이 상태들과 별도로 run은 RUNNING → TRANSFER_COMPLETE → VERIFYING → VERIFIED/VERIFY_FAILED를 사용한다. 오류·중단은 FAILED/INTERRUPTED다. resume은 RECOVERING → RUNNING/VERIFYING으로 복구하고 일시 오류 대기는 RETRY_WAIT다. transfer_ready와 cutover_ready는 전체 운영 조건을 구현하지 않아 false다. 대상 전용 파일 조사·전체 메타데이터 호환성은 다음 단계다.

로컬 전송은 저장된 계획과 설정 fingerprint·endpoint 정체성·소스 루트 mtime를 비교한다. 각 항목의 종류·크기·mtime와 대상 충돌을 쓰기 전에 점검한다. 내용 전송은 500개씩 NUL 매니페스트를 rsync --files-from=- --from0에 전달하고 --dirs/--no-recursive로 계획 밖의 재귀 복사를 막는다. --ignore-times로 내용이 달라도 quick-check로 건너뛰지 않는다. 덮어쓰기 비허용 시 --ignore-existing도 사용하며 각 배치 직전에 대상 충돌을 다시 검사한다. 전송 배치 timeout은 1800초다.

Windows는 기본 WSL 배포판의 rsync와 wslpath를 호출하며 Python 상태 DB는 Windows 로컬 디스크에 둔다. Linux는 같은 환경의 rsync를 직접 실행한다. WSL 파일 시간 정밀도 차이를 보완하도록 대상 파일·디렉터리 mtime를 Python의 native API로 계획 값에 맞추고 재검증한다. 파일 바이트 복사를 Python으로 대체하지 않는다.

file_results는 plan_id+상대 경로, status, algorithm, source_generation, source_hash, destination_hash, error를 저장한다. 전송 전에 원본 device/inode/size/mtime/ctime를 기록하고 검증 시 같은 세대인지 확인한다. Windows Python 3.12의 stat/fstat ctime 의미 차이는 API 간 device/inode/size/mtime 비교와 각 API 안의 ctime 전후 비교로 처리한다.

원본 read-only가 확인되지 않으면 기본 차단하고, 개발용 --allow-unprotected-source와 --overwrite 선택은 실행 기록에 저장한다. 자동 백업·자동 롤백은 제공하지 않는다. run/verify/resume은 성공 체크포인트를 일괄 삭제하지 않고 파일별 결과가 확정될 때 교체한다. verify는 실제 양쪽 내용을 다시 읽는다. 링크·특수 파일·대상 하드링크·정책 충돌은 차단하며 원본/대상 여분 파일은 삭제하지 않는다. Linux 중단은 자식 프로세스 그룹을 종료·회수한다. 현재 D3는 Windows native 감시 장치와 WSL 내부 Python 표준 라이브러리 감시 장치를 연결한다. stdin lease의 EOF로 부모 사망을 탐지하고 Linux rsync 그룹을 종료·회수한다. native worker.lock은 WSL 종료 확인까지 유지하며 미확인 종료는 재개 쓰기를 차단한다.

## 1.2. D3 체크포인트·재개·재시도 구현

전송은 기존의 500개 목록 배치를 사용한다. 각 배치의 intent와 원본 세대를 먼저 커밋하고 rsync 실행 후 대상 게시·mtime 복원·copied receipt·전체 내용 해시 결과를 커밋한다. 다음 배치를 시작하기 전에 VERIFIED 체크포인트가 영속화된다. 파일시스템과 DB가 원자적이라고 가정하지 않는다.

resume은 planner.lock과 worker.lock, SQLite quick_check, 저장된 설정 fingerprint, endpoint 정체성과 원본 세대를 검사한다. retry 설정은 스케줄링 정책이므로 변경을 허용하되 저장된 구성 fingerprint의 무결성을 확인하고 source/destination/state_dir/algorithm 변경은 거부한다. 재개 시마다 양쪽 내용을 다시 읽어 저장된 원본 해시와 대조한다. 검증됐거나 게시 후 커밋을 놓친 파일의 내용이 일치하면 재복사 없이 VERIFIED로 복구한다.

copied=1은 작업이 확인한 대상 게시의 근거다. 변조된 해당 파일의 복구 교체는 허용하되 불확실한 기존 대상은 최초 --overwrite 정책 또는 명시 승인이 필요하다. 새 경로와 교체 허용 경로를 같은 rsync batch로 묶어 덮어쓰기 권한을 넓히지 않는다. 원본 변경은 이전 검증 상태를 무효화하고 새 계획을 요구한다.

--partial-dir=.migraflow-partial-PLAN_ID와 --no-whole-file로 대상의 부분 파일을 다음 전송 basis로 사용한다. 예약 부분 디렉터리 충돌·symlink·mount 변경을 점검한다. 부분 파일 자체는 완료 증거가 아니며 마지막 전체 해시/메타데이터 검증을 유지한다.

worker는 표준 라이브러리만 사용해 WSL에서도 별도 패키지 설치 없이 실행한다. READY → START handshake와 부모 stdin lease를 사용하며, START 전에 부모가 죽으면 복사를 시작하지 않는다. Linux 자식은 새 process group으로 실행하고 EOF/timeout 때 그룹을 종료·회수한다. Windows native worker는 WSL guardian에 lease를 전달하고 guardian은 Linux rsync 그룹을 관리한다. native worker.lock은 전체 수명 동안 유지한다.

자동 재시도는 max_attempts(기본 3), initial_delay_seconds(기본 1), max_delay_seconds(기본 30)로 제한한다. rsync 10/12/30/35와 명시적 transient errno만 허용한다. code=23/24/11, 권한·공간·원본 세대·해시 오류는 자동 재시도하지 않는다. 해시 읽기도 transient errno에 한해 제한 재시도한다. attempts에 batch/attempt/created_at/outcome/error/delay_seconds를 기록한다. 결과는 SUCCESS/RETRY/FAILED/HASH_RETRY이며 해시 읽기의 batch는 -1이다.

시도 전에도 완료 파일을 해시 확인해 재전송 후보에서 제외한다. 수동 resume은 새로운 시도 예산을 사용하며 누적 이력을 삭제하지 않는다. RETRY_WAIT 중 프로세스가 종료돼도 영속 결과를 기반으로 복구한다. 스캔 재개와 예약된 대기 시각의 정밀 복원은 아직 구현하지 않았다.

목적지 쓰기 점검 결정(사용자 요청): 기본 preflight는 os.access(W_OK | X_OK)와 read_only_mount로 destination_write_permission을 반환한다. 실패는 경고로 보고해 읽기 전용 plan을 유지한다. 권한 조회가 실제 쓰기를 보장하지 않아 별도 CLI 옵션 --test-destination-write를 채택했으며 자동 실행과 YAML 저장은 제외했다. 옵션은 기존 안전 점검 후 양쪽 정체성을 재확인하고 읽기 전용 목적지 마운트를 거부한다. tempfile.mkstemp의 배타적 생성으로 기존 파일 덮어쓰기를 피하며 목적지 루트에 시험 데이터 기록·flush·fsync 후 닫고 finally에서 삭제한다. 생성/기록/동기화/삭제 실패는 ConfigError로 보고하며 삭제 실패 메시지에 경로를 포함한다. destination_write_test는 not_requested/passed다. 대상 디렉터리 mtime 변경과 강제 종료 시 임시 파일 잔류가 가능하다. 기존의 적대적인 경로 교체 경쟁 보호 한계는 동일하다.

## 1.3. NAS 마운트 전송 구현

Linux의 local/mounted 모든 조합은 기존 rsync adapter·상태·재개·검증 엔진을 사용한다. NAS 파일을 중계에 전체 사본으로 저장하지 않고 마운트 경로 사이에서 rsync가 스트리밍한다. mounted는 NFS/CIFS 등 네트워크 마운트로 판별되고 mount_source가 일치해야 한다. Windows Python은 local→local만 허용하고 NAS는 Linux/WSL 안의 Python으로 실행한다.

/proc/self/mountinfo의 네트워크 마운트 record에 mount_id를 추가했다. mount ID·fs·source·root·point·options 및 endpoint device/inode를 계획에 저장하고 재개와 대조한다. 기존 local 계획의 mount record 형식은 유지한다. mount ID 없는 과거 mounted 계획과 remount된 경로는 새 계획이 필요하다. 동일 fs/source의 mounted 경로는 st_dev가 달라도 mount root 기준 물리 상대 경로의 중첩을 차단한다. 서로 다른 export/서버 이름의 실제 저장소 별칭은 자동 판별하지 않는다.

mounted 배치에는 guard callback을 전달한다. worker READY 이후 START 전, 실행 중 약 0.2초마다, 종료 후 마운트 테이블을 검사한다. mountinfo 조회는 NAS 파일 stat를 사용하지 않는다. 변경 시 stdin lease를 닫아 worker가 rsync를 종료·회수하게 하고 ConfigError로 작업을 중지한다. 재시도 시작·해시 읽기 전후·파일 메타데이터 쓰기 전에도 검사한다. 폴링만으로 쓰기 대상을 보호할 수 없어 Linux worker가 양쪽 루트를 O_DIRECTORY/O_NOFOLLOW로 열고 pass_fds로 rsync에 전달하며 경로를 /proc/self/fd/N/로 고정한다. rsync가 열린 디렉터리를 사용하므로 마운트 해제 후 드러난 로컬 경로로 전환하지 않는다. 감시 종료 전에 FD와 worker lease를 해제하지 않는다.

대상 read-only 마운트는 전송 전에 차단한다. 소스 read-only 보호·덮어쓰기·삭제 정책과 SHA-256/MD5의 전체 해시·세대·mtime 검증은 기존 계약을 유지한다. NAS의 mtime 정밀도가 계획 값과 다르면 검증 실패다. mount 유지 상태의 transient IO만 제한 재시도하며 마운트 변경은 자동 복구하지 않는다. OS의 hard NFS IO 등은 종료 신호에도 오래 차단될 수 있고 적대적인 동시 경로 교체의 완전한 보호는 아직 없다. 실제 NAS의 일관성과 권한·메타데이터 보장은 별도 시험이 필요하다.

## 1.4. Rich 실시간 표시 구현

progress 모듈은 ContextVar로 현재 실행의 선택적 observer를 연결한다. execute는 scope를 설정하고 상태 커밋·단계·처리·검증 커밋·재사용·오류 이벤트를 발행한다. verifier는 기존 1MiB 읽기 청크마다 해시 시작/바이트/종료 이벤트를 발행한다. observer 오류는 해당 실행의 표시 이벤트를 비활성화하고 전송 정책/상태 전이는 유지한다. KeyboardInterrupt는 삼키지 않아 기존 안전 중단 경로로 전달한다. DB 스키마나 설정 fingerprint는 바꾸지 않는다.

Dashboard는 제한된 단일 상태와 RLock을 사용하며 Rich Live가 약 4회/초 화면을 갱신한다. 백그라운드 refresh는 파일/DB를 읽거나 정책을 실행하지 않고 메모리 상태만 렌더링한다. 검증 건수는 시작 시 저장된 summary에서 읽고 이후 커밋된 delta로 반영하며 화면 때문에 대량 SQL 집계를 반복하지 않는다. 단계 막대는 단계마다 초기화하고 파일 처리와 검증 성공/실패를 구분한다. 해시 속도는 현재 파일 읽기 기준, 단계 ETA는 처리 파일 평균 기준이다. 길고 특수한 경로는 Text로 출력하고 제어문자를 이스케이프한다.

observer가 있을 때만 rsync에 --info=progress2,name0와 --outbuf=N을 추가한다. 기존 로그의 시작 offset 이후를 한 번에 최대 4096B 읽고 CR/LF 출력과 조각을 파싱한다. 보유 조각은 최대 4096B이며 알 수 없는 포맷/로그 읽기 오류는 무시한다. 진행 수치는 화면만 갱신하며 copied·VERIFIED 등 파일별 판정은 기존 세대·해시·DB 경로로만 수행한다. 프로세스 대기는 0.2초 간격이며 마운트 guard와 worker lease/정리 계약을 유지한다. rsync 수치의 분모/속도/시간은 rsync의 배치 추정이며 애플리케이션의 성공 증거 또는 네트워크 바이트가 아니다.

CLI의 run/resume/verify는 stderr TTY에서 자동 Live를 사용한다. --no-live로 비활성화하며 --live는 TTY에서만 허용하고 --json과 충돌한다. JSON/리다이렉션에는 기존 출력·결과·종료 코드를 유지한다. Live 종료 후 기존 최종 summary를 표시한다. 현재는 실행 화면이며 status 감시·plan 스캔 화면·키보드 제어는 구현하지 않는다.

## 2. 중계 데이터 경로

아래 그림은 NAS→NAS 조합이다. 출발지와 목적지 각각의 local 타입과 로컬/원격 모든 방향의 이전은 확정 범위다.

~~~text
소스 NAS ── 읽기 전용 마운트 ──┐
                              │
                     중계 Linux 인스턴스
                     Python 작업 조정자
                     rsync 전송 프로세스
                     SHA-256 검증 워커
                     로컬 SQLite / 로그
                              │
대상 NAS ── 쓰기 가능한 마운트 ─┘
~~~

첫 구현은 실행 인스턴스의 로컬 경로와 NAS 마운트 경로를 양쪽에 독립적으로 지정하도록 한다. NAS→NAS 조합에서는 두 NAS를 중계 인스턴스에 마운트한다. rsync는 소스 경로를 읽어 대상 경로에 기록한다. 중계는 전체 데이터의 영구 staging 공간 없이 제한된 메모리 버퍼와 NAS 클라이언트 IO를 사용한다. OS 캐시가 사용할 메모리와 대상 임시 파일 공간은 별도로 필요하다.

“스트리밍 중계”는 데이터가 중계를 경유하고 전체 로컬 복제본 없이 전송되는 의미다. rsync stdout을 바이트 스트림으로 다음 rsync stdin에 넣는 의미가 아니다. 두 원격 endpoint를 한 rsync 명령에 지정하지 않는다.

rsync 로컬 경로 간 전송은 원격 전송과 성능 특성이 다르다. 마운트 경로에서 증분 파일 선별은 가능하지만 블록 델타로 네트워크 비용이 항상 감소한다고 가정하지 않는다. 변경 파일 재전송 비용을 측정하고 강제로 옵션을 변경하기 전에 벤치마크한다.

개발 저장소는 Windows 경로지만 운영 중계는 Linux를 우선 제안한다. WSL2 개발 시 저장소와 테스트 데이터 위치를 구분하고 NAS mount 및 실행 경로는 Linux 기준으로 명시한다. 운영 DB는 네트워크 공유 또는 Windows 공유 드라이브 경유 위치에 두지 않는다.

## 2.1. endpoint 타입과 전송 경로

source와 destination은 각각 type과 루트 경로를 갖는다. local 지원은 확정이며 아래 원격 타입 이름과 설정 구조는 구현 제안이다.

| type | 의미 | 설정·점검 |
|---|---|---|
| local | 실행 인스턴스의 로컬 파일시스템 | path, 파일시스템 정체성, 소스 쓰기 차단, 대상 공간·충돌 |
| mounted | 실행 인스턴스에 마운트한 원격 NAS | path, 예상 공유/마운트 정체성, 연결·권한 |
| rsync_ssh | SSH로 접근하는 원격 rsync endpoint | host, user, 원격 path, 호스트 키·권한·원격 해시 기능 |

local과 mounted는 rsync에 로컬 경로로 전달되지만 사전 점검은 구분한다. local 경로가 네트워크 마운트로 판별되면 mounted 정책을 요구한다. 재개 시 타입·루트·정체성을 구성 fingerprint와 대조한다.

| 조합 | 전송 방식 | 단계 |
|---|---|---|
| local/mounted → local/mounted | 두 실행 인스턴스 경로 사이 rsync | P1 |
| local/mounted → rsync_ssh | rsync push, 원격 대상 SHA-256 검증 | P2 |
| rsync_ssh → local/mounted | rsync pull, 원격 소스 SHA-256 검증 | P2 |
| rsync_ssh → rsync_ssh | 직접 실행 차단; 마운트 또는 별도 설계 필요 | 미지원 경로 |

로컬→원격과 원격→로컬은 P1의 NAS 마운트 조합부터 지원하고 SSH 접근은 P2에서 추가한다. 로컬→로컬도 동일한 상태관리·검증 엔진과 rsync를 사용한다. 처음부터 별도 로컬 복사 엔진을 만들지 않는다.

현재 YAML 설정 예시(Linux mounted는 실제 NAS에서 검증 필요):
~~~yaml
source:
  type: local
  path: /data/source
destination:
  type: mounted
  path: /mnt/nas-destination
  mount_source: nas-destination:/export/data
state_dir: /var/lib/migraflow
~~~

로컬 소스는 읽기 전용 bind mount 또는 쓰기 권한 없는 실행 계정 등으로 보호를 검증한다. 앱이 쓰기 API를 호출하지 않는 것만으로 운영 보호를 충족했다고 판정하지 않는다.

## 3. 구성 요소

| 구성 | 책임 |
|---|---|
| CLI/CUI | 명령 입력, Rich 화면, 비대화형 출력, 종료 코드 |
| Preflight | endpoint 타입·정체성·권한·공간·경로 충돌·필수 기능 확인 |
| Scanner/Planner | 점진적 인벤토리, scan epoch, 변경 후보와 배치 계획 |
| Coordinator | 작업 잠금, 상태 전이, 실행·중지·재개·재시도 정책 |
| Rsync adapter | 안전한 인자 생성, NUL 매니페스트, 자식 프로세스·결과 관리 |
| Verifier | 안정된 파일 세대의 선택 해시(기본 SHA-256, 선택 MD5) 및 메타데이터 비교 |
| StateStore | 트랜잭션, 체크포인트, 회차·파일·배치·이벤트 기록 |
| Reporter | 건수·바이트·오류·검증 결과와 cutover 준비 보고서 |

처음에는 단일 조정자, 단일 DB 쓰기 경로, 제한된 전송/검증 워커로 시작한다. 병렬 수는 NAS 부하와 작은 파일 성능 측정 후 조정한다. UI와 엔진을 분리하되 미리 플러그인 프레임워크를 만들지 않는다.

## 4. 상태 모델

작업 상태 제안:

~~~text
CREATED → PREFLIGHTED → SCANNING → PLANNED → RUNNING
                                          ↓
                                  TRANSFER_COMPLETE
                                          ↓
                                      VERIFYING
                                          ↓
                               ROUND_VERIFIED
                                          ↓
                              CUTOVER_READY → CUTOVER_DONE

RUNNING / SCANNING / VERIFYING → PAUSED 또는 FAILED
재개 → 사전 점검·불확실 상태 복구 → 해당 단계
새 증분 회차 → SCANNING (이전 회차의 완료와 구분)
~~~

ROUND_VERIFIED는 특정 회차·소스 세대만 검증됐다는 뜻이다. 운영 쓰기가 지속된 상태의 회차 검증은 CUTOVER_READY가 아니다. CUTOVER_READY는 최종 동결/스냅샷과 검증 조건을 만족한 경우에만 부여한다. 실제 서비스 전환 기록은 별도 승인과 운영 확인을 요구한다.

파일 상태는 DISCOVERED → QUEUED → TRANSFERRING → TRANSFERRED → VERIFYING → VERIFIED다. RETRY_WAIT, FAILED, SOURCE_CHANGED, MISSING, EXCLUDED를 별도 표시한다. 소스 세대가 바뀌면 기존 해시와 VERIFIED를 무효화한다. 삭제 후보는 이 상태와 별도 tombstone 계획으로 관리한다.

## 5. SQLite WAL 설계

상태 DB·-wal·-shm은 중계 인스턴스의 로컬 디스크에 둔다. 모든 접근은 동일 호스트에서 수행한다. 하나의 writer 큐를 사용하고 읽기 연결은 짧은 트랜잭션으로 유지한다. WAL 모드, busy_timeout, foreign_keys, synchronous=FULL을 초기 정책으로 제안하며 실제 설정값을 기록한다.

제안 스키마:

| 테이블 | 핵심 항목 |
|---|---|
| jobs | id, 구성 fingerprint, source/destination identity, 상태, 정책 버전 |
| runs | id, job_id, scan_epoch, source_snapshot_id, 동결 기록, 상태, 시작/종료 |
| files | id, job_id, 상대 경로 bytes, 표시 경로, 종류, 마지막 관측 epoch |
| file_versions | id, file_id, run_id, size, mtime_ns, source generation, 상태, hash_algorithm, 양측 내용 해시 |
| batches | id, run_id, manifest 경로/해시, 실행 상태, rsync 종료 코드 |
| batch_files | batch_id, file_version_id, 재검증 필요 여부 |
| attempts | file_version_id/batch_id, 횟수, 오류 분류, next_retry_at |
| events | run_id, 시간, 종류, 구조화 메시지 |
| approvals | run_id, 대상 범위/계획 해시, 승인자·시간·결과 |

경로는 원래 바이트 정체성을 보존하고 표시용 문자열을 별도로 둔다. job_id+상대 경로에 유일성을 두고, 상태/재시도 시각/scan epoch 조회를 인덱싱한다. 인벤토리 추가는 제한된 트랜잭션으로 처리한다. 해시·통계·이벤트를 파일 읽기 청크마다 기록하지 않는다.

작업 체크포인트는 커밋된 배치·파일 세대·스캔 회차의 재개 지점이다. SQLite WAL checkpoint는 WAL을 DB로 반영하는 저장소 관리 작업이며 작업 완료 판정과 다르다. WAL 크기와 장기 reader를 관찰하고 적절한 시점에 checkpoint한다. 실행 중 DB 파일만 단독 복사하지 않고 SQLite backup API 등을 사용한다.

전원 장애 내구성은 SQLite 설정뿐 아니라 디스크·NAS의 flush 보장에도 의존한다. 파일시스템과 DB는 하나의 트랜잭션이 아니므로, 재개 때 대상 증거를 확인하는 복구 절차가 필요하다.

## 6. 배치, 체크포인트와 재개

전체 트리를 한 번에 메모리에 올리지 않고 디렉터리별 스캔 큐를 영속화한다. 재개 시 진행 중 디렉터리를 다시 스캔해 UPSERT하고, 회차가 끝나기 전 삭제 후보를 확정하지 않는다. 디렉터리 열거 offset을 영구적으로 안정된 cursor라고 가정하지 않는다.

배치는 파일 수와 예상 바이트 상한으로 제한한다. 초기 값은 규모 시험으로 결정한다. --files-from과 --from0용 NUL 매니페스트를 사용하고 외부 인자 목록으로 대량 파일명을 전달하지 않는다. 디렉터리와 빈 디렉터리를 별도 계획하며 디렉터리 속성은 파일 전송 후 최종화한다.

실행 순서:
1. 대상 정체성·파일 세대·정책을 재확인하고 매니페스트를 로컬에 기록한다.
2. 배치와 실행 의도를 DB에 커밋한다.
3. rsync를 실행하고 종료 코드와 요약·원본 로그를 수집한다.
4. 배치 종료와 파일별 대상 증거를 확인한다. 근거가 불충분하면 미확정 상태로 남긴다.
5. 검증 결과를 세대와 함께 커밋하고 체크포인트를 갱신한다.

부분 파일은 대상의 작업 전용 위치에 유지한다. --partial-dir 사용과 최종 파일 rename은 대상 파일시스템에서 실제로 검증한다. 대상 여유 공간에는 기존 파일·새 임시 파일·보존할 부분 파일을 포함한다. --inplace는 초기 정책에서 제외한다.

재개는 작업 잠금을 획득하고 DB 무결성·구성 fingerprint·마운트 정체성을 검사한다. 남은 자식 프로세스가 없는지 확인한다. TRANSFERRING/VERIFYING 상태는 불확실 상태로 돌려 대상 크기·해시를 확인하고 재전송 또는 재검증한다. 재전송은 허용하지만 검증되지 않은 성공을 재사용하지 않는다.

## 7. 내용 해시 및 일관성

현재 로컬 verifier는 설정된 sha256 또는 md5를 1MiB 청크로 계산한다. 양쪽에 같은 알고리즘을 사용하고 결과에 알고리즘을 함께 저장한다. 알고리즘이 바뀌면 이전 해시·검증 완료를 재사용하지 않는다. MD5는 충돌 저항성이 약한 선택 정책이며 SHA-256이 기본이다. 소스와 대상 파일을 읽고 각각 해시를 구한다. 소스는 해시 전후 크기·mtime 및 가능한 세대 식별을 검사하고 변경되면 결과를 버린다. stat 전후 비교만으로 동시 변경을 완전히 배제할 수 없으므로 최종 판정은 안정된 스냅샷 또는 쓰기 동결을 전제로 한다.

선택한 내용 해시 비교는 rsync 내부 checksum과 독립된 후검증이다. rsync 성공 코드는 별도 내용 해시 검증의 대체가 아니다. 대상도 검증 기간에 외부 쓰기를 차단한다. 대상 검증 중 변경이 있으면 결과를 폐기한다.

소스와 대상 전체를 추가로 읽는 검증 비용을 계획한다. 수백만 작은 파일에서는 파일 open/stat 비용과 NAS IO가 병목일 수 있다. 검증 워커 수는 전송 부하와 합산해 제한한다. 링크·디렉터리·ACL은 별도 검증 규칙을 적용한다.

## 8. 증분과 삭제

완료된 scan epoch 간 차이로 신규·변경·누락을 계산한다. 시간과 크기가 같은 숨은 변경은 최종 전체 내용 해시 검증에서 탐지하고 재전송 대상으로 올린다. 해시 불일치 파일은 기본 quick-check에 맡겨 반복 skip하지 않고 해당 파일을 강제로 재전송하도록 계획한다.

삭제는 별도 tombstone 매니페스트로 관리한다. 소스가 비어 보이거나 마운트가 끊겼을 때 삭제 계획을 만들지 않는다. 대상 여분 파일은 보고하며, 삭제 승인이 없다면 남긴다. 완전 미러 요구와 “복사만” 정책의 차이는 cutover 전에 확정한다.

## 9. 단계별 프로토콜 확장

| 단계 | 전송 경로 | 진입 조건 |
|---|---|---|
| P1 | local/mounted 양쪽 조합 + rsync | 로컬→로컬·로컬→NAS·NAS→로컬·NAS→NAS에서 소스 보호·검증·재개와 마운트 검사를 입증 |
| P2 | local/mounted ↔ rsync_ssh | push/pull 각각 검증; 적어도 한 쪽 실행 인스턴스 경로, SSH 호스트 검증, 원격 해시 기능 확보 |
| P3 | 추가 SFTP/SMB/NFS API 등 | 사용자 요구, 운영 환경, 공통 계약 테스트와 성능 검증 |

P2에서도 마운트/원격 조합의 데이터가 중계를 경유하는지 확인한다. 양쪽 모두 원격 rsync만 가능하면 P2로 자동 처리하지 않는다. 무전체-staging 요구와의 충돌을 설명하고 새 설계를 결정한다.

추가 adapter는 enumerate/stat, transfer, verify, resume capability, metadata capability를 노출한다. 기능이 없는 endpoint는 해당 기능을 조용히 생략하지 않는다. 원격 내용 해시는 선택한 알고리즘을 지원하는 고정된 helper와 인증된 연결을 사용하거나 중계에서 실제 내용을 다시 읽어야 한다. 일반화는 두 번째 adapter를 구현하는 시점에 최소화한다.

## 10. 공식 기술 근거

- [rsync 매뉴얼](https://download.samba.org/pub/rsync/rsync.1.html): 로컬/원격 전송 형태와 두 원격 호스트 간 직접 복사 제약, 매니페스트 및 부분 전송 옵션의 확인 근거.
- [SQLite WAL](https://www.sqlite.org/wal.html): 같은 호스트·로컬 파일시스템 배치의 확인 근거.

실제 NAS의 권한·rename·시간 정밀도·메타데이터 보존은 위 문서만으로 보장되지 않으며 사전 점검과 통합 시험으로 결정한다.
