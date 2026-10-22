# MigraFlow

**계획하고, 전송하고, 검증하는 파일 마이그레이션 CLI.**

MigraFlow는 로컬 디렉터리와 NAS 사이의 파일 이전을 위한 Python 도구입니다. rsync로 데이터를 전송하고 SQLite에 작업 상태를 기록해 중단 후 재개, 증분 동기화, 파일별 내용 검증을 제공합니다. Linux 중계 인스턴스에서 NAS 간 이전을 제어하거나 SSH로 원격 파일을 보내고 받을 수 있습니다.

```text
사전 점검 → 계획 생성 → 전송 → 전체 해시 검증 → 전환 준비 점검
```

현재 개발 단계입니다. 로컬 전송과 loopback SSH 통합 시험을 수행했으며, NAS 마운트 조합은 모의 마운트 정체성과 실제 rsync로 검사했습니다. **실제 NAS 호환성과 100만~1000만 파일 규모는 아직 검증하지 않았습니다.**

## 목차

- [주요 기능](#주요-기능)
- [지원 환경](#지원-환경)
- [설치](#설치)
- [빠른 시작](#빠른-시작)
- [설정](#설정)
- [사용 가이드](#사용-가이드)
- [데이터 안전과 제한](#데이터-안전과-제한)
- [개발 및 기여](#개발-및-기여)
- [문서](#문서)
- [라이선스](#라이선스)

## 주요 기능

- **여러 전송 경로:** 로컬↔로컬, 로컬↔NAS 마운트, NAS 마운트↔NAS 마운트, SSH push/pull.
- **계획 기반 실행:** 파일 목록과 충돌·덮어쓰기 후보를 확인한 뒤 별도 명령으로 전송.
- **중단 복구:** SQLite WAL 체크포인트, 스캔·전송 재개, 일시 오류의 제한 자동 재시도.
- **내용 검증:** SHA-256 기본값, 선택적 MD5, 현재 양쪽 해시가 일치하는 파일의 재복사 생략.
- **증분 동기화:** 검증 완료 회차를 기준으로 새 원본 목록을 스캔하고 신규·변경 파일 반영.
- **진행 상황 확인:** Rich 실시간 화면, 별도 상태 감시, JSON 출력.
- **메타데이터와 전환 점검:** 선택적 Linux POSIX 속성 보존·검증, 대상 추가 항목 조사, 전환 준비 보고서.

## 지원 환경

| 실행 환경 | 지원하는 전송 경로 | 필요 사항 |
|---|---|---|
| Linux / WSL 내부 Python | `local` / `mounted`의 모든 조합, 한쪽 `rsync_ssh` | Python 3.12+, rsync |
| Windows Python | 고정 로컬 드라이브의 `local → local` | Python 3.12+, 기본 WSL 배포판의 Python 3·rsync |
| SSH 원격 서버 | Linux 서버로 push / 서버에서 pull | Python 3.8+, rsync 3.1.3+, 키/agent 인증 |

`local`은 실행 인스턴스의 경로, `mounted`는 그 Linux 환경에 미리 마운트한 NAS 공유입니다. NAS와 SSH를 사용하려면 **Python CLI도 Linux/WSL 안에서 실행**합니다. Windows 네트워크 드라이브·UNC와 양쪽 모두 `rsync_ssh`인 직접 전송은 지원하지 않습니다.

SSH에는 실행 인스턴스의 OpenSSH client와 원격 `/etc/machine-id`가 필요합니다. 자세한 조건은 [SSH 가이드](docs/SSH.md)를 참고하세요.

## 설치

저장소를 내려받습니다.

```sh
git clone https://github.com/keysburn/migraflow.git
cd migraflow
```

### Linux / WSL

Python 3.12 이상과 rsync를 먼저 준비한 뒤 Linux 가상환경에 설치합니다.

```sh
python3 --version
rsync --version
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
migraflow --help
```

WSL에서는 Windows용 가상환경을 재사용하지 않습니다. 상태 DB는 `/mnt/c`나 NAS 공유 대신 WSL의 Linux 로컬 파일시스템에 둡니다.

### Windows PowerShell

로컬→로컬 전송을 사용할 때의 설치 방법입니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
wsl --exec python3 --version
wsl --exec rsync --version
migraflow --help
```

Windows의 실제 전송은 기본 WSL 배포판의 rsync를 호출합니다. WSL에 Python 3와 rsync가 없으면 먼저 설치해야 합니다.

## 빠른 시작

아래는 **격리된 로컬 샘플 데이터**로 시험하는 절차입니다. Linux/WSL에서는 Linux 로컬 파일시스템의 작업 폴더를 사용합니다. 그 안에 `demo/source`와 빈 `demo/destination` 디렉터리를 만들고 source에 시험 파일을 준비합니다.

`migration.local.yml`을 다음과 같이 작성합니다.

```yaml
source:
  type: local
  path: demo/source
destination:
  type: local
  path: demo/destination
state_dir: state
```

사전 점검과 계획 생성은 양쪽 데이터를 수정하지 않습니다.

```sh
migraflow preflight
migraflow plan
```

계획 결과가 `PLANNED`인지 확인하고 출력된 `id`를 다음 명령의 `PLAN_ID`에 넣습니다. 읽기 전용 보호가 없는 시험 디렉터리에는 개발용 예외를 명시합니다.

```sh
migraflow run --job PLAN_ID --allow-unprotected-source
migraflow status --job PLAN_ID
migraflow verify --job PLAN_ID
```

`run`은 복사 후 전체 내용 검증까지 수행하며 성공 상태는 `VERIFIED`입니다. **운영에서는 소스를 읽기 전용 마운트로 보호하고 `--allow-unprotected-source`를 사용하지 않습니다.** 쓰기 동결 또는 스냅샷으로 운영 서비스의 원본 변경도 별도로 제어해야 합니다.

## 설정

기본 파일은 현재 폴더의 `migration.local.yml`입니다. [전체 예제](migration.example.yml)를 복사해 수정하거나 `--config`로 다른 파일을 지정할 수 있습니다.

```sh
migraflow plan --config migration.local.yml
migraflow plan --config migration.local.yml --destination /srv/new-destination
```

설정 파일 없이 경로를 직접 지정할 수도 있습니다.

```powershell
migraflow plan --source 'C:\data\source' --destination 'D:\data\destination' --state-dir state
```

우선순위는 **CLI 옵션 → YAML → 기본값**입니다. YAML의 상대 경로는 설정 파일 위치, CLI의 상대 경로는 현재 작업 폴더 기준입니다. `.env` 로딩과 환경변수 치환은 지원하지 않으며 알 수 없는 키와 중복 YAML 키를 거부합니다.

### 경로와 검증 정책

| 항목 | 설명 |
|---|---|
| `source`, `destination` | 각각 `type`과 `path` 지정. 양쪽 데이터 디렉터리는 미리 존재해야 함 |
| `type: local` | 실행 인스턴스의 로컬 경로. 기본 타입 |
| `type: mounted` | Linux NAS 마운트 경로. 예상 공유 정체성인 `mount_source` 필수 |
| `type: rsync_ssh` | OpenSSH 별칭 `host`, 절대 POSIX `path`, 원격 로컬 디스크의 `remote_state_dir` 필수 |
| `state_dir` | 데이터 경로 밖의 로컬 상태 디렉터리. 기본값 `state` |
| `verification.algorithm` | `sha256` 기본값 또는 `md5` |
| `metadata.policy` | `basic` 기본값 또는 Linux 전용 `posix` |

`basic`은 내용·크기·mtime와 디렉터리 존재·mtime를 검증합니다. `posix`는 계획 항목의 mode, numeric UID/GID, POSIX ACL/xattr도 보존·검증합니다. endpoint 루트와 NAS 고유 ACL·계정 매핑은 별도 검토 대상입니다. MD5는 SHA-256보다 충돌 저항성이 약합니다. 해시 알고리즘이나 메타데이터 정책을 바꾸면 새 계획이 필요합니다.

### 자동 재시도

```yaml
verification:
  algorithm: sha256
metadata:
  policy: basic
retry:
  max_attempts: 3
  initial_delay_seconds: 1
  max_delay_seconds: 30
```

`max_attempts`는 최초 시도를 포함해 1~10회입니다. 지수 백오프를 사용하며 대기값은 0~30초, 초기값은 최대값 이하여야 합니다. 일시 IO·연결 오류만 재시도하고 권한·공간 부족·정책 위반·내용 불일치는 자동 재시도로 우회하지 않습니다.

### 장시간 작업의 시간 제한

```yaml
timeouts:
  transfer_idle_seconds: 1800
  transfer_total_seconds: null
  hash_idle_seconds: 1800
  hash_total_seconds: null
```

기본값은 **총 실행시간 제한 없음**, 무진행 제한 1800초입니다. `transfer_idle_seconds`는 rsync의 IO 무진행 제한, `transfer_total_seconds`는 선택적인 배치 실행 상한입니다. worker 준비·종료 시간은 별도이며 모든 로컬 파일시스템 호출의 정체를 감시하지는 않습니다.

`hash_idle_seconds`와 `hash_total_seconds`는 **SSH 원격 파일 해시**에 적용합니다. 실제 읽기 바이트 증가만 무진행 시간을 갱신합니다. 일반 SSH 메타데이터 RPC는 60초 제한이며 로컬 해시 호출에는 이 두 설정을 적용하지 않습니다.

단위는 초입니다. 값은 0보다 크고 2,147,483,647 이하의 유한 숫자이며 `transfer_idle_seconds`는 정수입니다. 총 제한만 `null`을 허용합니다. `retry`와 `timeouts`는 재개·새 증분 회차에서 변경할 수 있습니다.

## 사용 가이드

### 명령어

| 명령 | 용도 |
|---|---|
| `preflight` | 경로·권한·마운트·소스 보호 상태 점검 |
| `plan` | 파일 목록과 충돌·덮어쓰기 후보를 기록하고 계획 ID 발급 |
| `run --job PLAN_ID` | 계획에 따라 전송한 뒤 전체 검증 |
| `resume --job PLAN_ID` | 같은 계획의 중단·실패한 전송/검증 재개 |
| `verify --job PLAN_ID` | 현재 양쪽 파일을 다시 읽어 검증 |
| `status --job PLAN_ID` | 저장된 상태 조회. ID 생략 시 최신 작업 |
| `cutover-check --job PLAN_ID` | 새 검증·대상 조사 후 전환 준비 보고서 생성 |

명령별 전체 옵션은 `migraflow COMMAND --help`로 확인합니다. `python -m migraflow`도 같은 CLI를 실행합니다.

### 중단 후 재개

```sh
migraflow resume --job PLAN_ID
migraflow plan --resume INTERRUPTED_SCAN_ID
```

`resume --job`은 전송·검증, `plan --resume`은 스캔 재개입니다. 로컬 개발 시험은 `resume`에도 `--allow-unprotected-source`를 추가합니다. 검증 완료 파일도 현재 양쪽 해시를 다시 읽지만 일치하면 재복사하지 않습니다. 원본 세대·경로 정체성·해시/메타데이터 정책이 달라졌으면 새 계획이 필요합니다.

상태 DB와 대상의 `.migraflow-partial-PLAN_ID` 부분 파일을 유지하세요. 활성 worker가 남아 있으면 재개를 차단하므로 잠금 파일을 삭제해 우회하지 않습니다.

재개 전 검사에서 원본 파일의 크기·수정 시각·누락 또는 디렉터리 변경을 발견하면 작업을 `VERIFY_FAILED`로 기록하고 이전 검증 상태와 해시를 무효화합니다. 원본 변경을 확인한 뒤 새 계획을 생성하세요.

상태 DB는 schema v6입니다. 기존 v1~v5 DB는 첫 쓰기 명령에서 집계를 생성하며 업그레이드합니다. 큰 DB는 최초 업그레이드에 시간이 걸릴 수 있습니다. `status`는 읽기 전용이며 구형 DB도 기존 집계 방식으로 조회합니다. 업그레이드 후에는 v6 지원 버전을 사용하세요.

### 새 회차 증분 동기화

```sh
migraflow plan --baseline PREVIOUS_VERIFIED_ID
migraflow run --job NEW_PLAN_ID
```

동일한 경로·정체성·해시/메타데이터 정책과 `state_dir`의 `VERIFIED` 작업을 기준으로 새 목록을 스캔합니다. 현재 양쪽 내용이 같은 기준 파일은 재사용하고 신규·변경 파일을 전송합니다. 복사 생략에도 해시 읽기 IO가 발생합니다.

기존 대상 파일을 교체해야 한다면 계획과 복구 수단을 확인한 뒤 **해당 회차에** `--overwrite`를 지정합니다. 이전 회차의 승인은 승계하지 않습니다. 소스에서 사라진 항목은 삭제 후보로만 보고하며 대상에서 삭제하지 않습니다.

### NAS 마운트 경로 전송

관리자가 Linux에서 공유를 먼저 마운트한 후 설정합니다. `mount_source`는 Linux 마운트 테이블에 표시되는 공유 문자열과 일치해야 합니다.

```yaml
source:
  type: mounted
  path: /mnt/nas-source/data
  mount_source: nas-source:/export/data
destination:
  type: mounted
  path: /mnt/nas-destination/migration
  mount_source: nas-destination:/export/data
state_dir: /var/lib/migraflow/state
```

NFS 예제이며 SMB/CIFS는 `//nas/share` 형태의 실제 공유 정체성을 사용합니다. 한쪽을 `local`로 바꾸면 로컬↔NAS 조합이 됩니다. 소스는 읽기 전용, 대상은 쓰기 가능해야 합니다. 마운트가 사라지거나 정체성이 바뀌면 중지하며 재마운트한 경우 새 계획을 만듭니다. [NAS 시험 절차](docs/NAS_TESTING.md)를 먼저 확인하세요.

### 마운트 없는 SSH 전송

로컬 설정의 `destination`을 다음과 같이 지정하면 push, `source`에 지정하면 pull입니다. 반대쪽은 `local` 또는 `mounted`여야 합니다.

```yaml
destination:
  type: rsync_ssh
  host: nas-target
  path: /volume1/migration/data
  remote_state_dir: /var/tmp/migraflow-worker
```

`host`는 `~/.ssh/config`의 별칭으로 지정하고 계정·포트·키는 SSH 설정에서 관리합니다. 검증된 호스트 키와 키/agent 인증이 필요하며 비밀번호 프롬프트·호스트 키 자동 승인은 사용하지 않습니다. `remote_state_dir`는 양쪽 데이터와 조정자 상태 경로에 겹치지 않는 원격 로컬 디스크 위치여야 합니다. [SSH 설정 예제](migration.ssh.example.yml)와 [상세 가이드](docs/SSH.md)를 참고하세요.

### Rich 실시간 화면

```sh
migraflow run --job PLAN_ID --live
migraflow status --job PLAN_ID --watch --interval 2
migraflow status --job PLAN_ID --json
```

대화형 터미널에서는 실행 화면이 자동으로 켜지며 `--no-live`로 끕니다. 현재 단계, 파일 수, 해시 읽기 진행, 검증 체크포인트, 재시도·재사용 수를 표시합니다. rsync의 속도·백분율은 배치 참고 수치이며 최종 성공 여부는 저장된 검증 결과로 판단합니다.

실행 중 Ctrl+C는 작업을 중단합니다. 별도 `status --watch`에서는 관찰만 종료합니다. 감시 간격은 1~60초이며 다른 프로세스의 현재 파일·순간 전송 바이트는 표시하지 않습니다. JSON/리다이렉션에서는 실행 화면을 자동으로 끄고 `--live --json`과 `--watch --json`은 함께 사용할 수 없습니다.

### 전환 준비 점검

```sh
migraflow cutover-check --job FINAL_PLAN_ID \
  --source-freeze source-freeze-reference \
  --destination-freeze destination-freeze-reference \
  --backup rollback-snapshot-reference \
  --approval approval-reference \
  --metadata-review metadata-review-reference \
  --extras-review retained-extras-reference --json
```

참조값은 운영자가 확인한 동결·백업·승인·메타데이터 검토 증빙으로 바꿉니다. `--extras-review`는 대상 추가 항목이 있을 때 필요합니다. 추가 항목 전체는 DB에 기록하고 출력에는 최대 20개를 표시합니다.

`READY`는 기록한 점검 조건을 충족했다는 뜻입니다. 외부 증빙의 진위나 실제 동결을 인증하지 않으며 **서비스 전환과 삭제를 실행하지 않습니다**. 운영 담당자가 최종 전환과 원복 절차를 수행해야 합니다.

## 데이터 안전과 제한

- **소스 보호:** 원본 내용·mtime·권한을 변경하지 않습니다. 조회에 따른 atime 영향은 OS 정책에 따릅니다.
- **쓰기 시험:** 기본 `preflight`와 `plan`은 데이터에 쓰지 않습니다. `preflight --test-destination-write`만 목적지에 고유 임시 파일을 기록·동기화·삭제합니다. 대상 디렉터리 mtime는 바뀔 수 있고 강제 종료 시 시험 파일이 남을 수 있습니다.
- **덮어쓰기와 삭제:** 기존 파일 교체는 계획·복구 수단 확인 후 `--overwrite`로 허용합니다. 같은 계획이 만든 파일의 재개 복구는 소유 증거를 확인합니다. 소스 자동 삭제와 대상 삭제 동기화는 수행하지 않습니다.
- **경로 격리:** 동일·중첩 경로, symlink/junction, 중첩 마운트와 데이터 내부 상태 저장을 차단합니다. 네트워크 공유를 `local`로 지정해 마운트 검사를 우회할 수 없습니다.
- **상태 보존:** DB는 `state_dir/migraflow.sqlite3`, 전송 로그·매니페스트는 `state_dir/transfer-PLAN_ID`에 저장합니다. DB/WAL은 로컬 디스크에 유지하고 비밀을 설정·로그·Git에 넣지 않습니다.
- **완료의 의미:** `PLANNED`는 목록 생성 완료, `VERIFIED`는 해당 계획의 검증 완료입니다. 서비스 전환 완료를 뜻하지 않습니다. 일관된 스냅샷 또는 최종 쓰기 동결이 별도로 필요합니다.
- **파일 호환성:** 링크·특수 파일과 파일/디렉터리·대소문자·Unicode 정규화 충돌은 차단합니다. NAS 고유/Windows ACL, 파일명·시간 정밀도와 적대적인 동시 경로 교체에 대한 호환성·보호 범위는 별도 검토 대상입니다.

| 종료 코드 | 의미 |
|---|---|
| `0` | 성공 |
| `1` | 실행 실패, `BLOCKED` 또는 불완전 작업 |
| `2` | 설정·정책·사전 점검 오류 |
| `3` | 내용·메타데이터·세대 검증 실패 |
| `130` | 사용자 중단 |

`status`도 저장된 실패 상태에 따라 비정상 종료 코드를 반환할 수 있습니다.

## 개발 및 기여

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
```

전송 통합 테스트에는 실제 rsync가 필요합니다. 플랫폼 전용 검사와 필요한 의존성이 설치되지 않은 통합 검사는 건너뛸 수 있습니다. SSH 시험 서버 준비 방법은 [SSH 가이드](docs/SSH.md#검증과-남은-범위)를 참고하세요. 실제 NAS·WAN·대규모 수용 시험은 로컬 테스트와 구분합니다.

문제 보고에는 OS, Python·rsync 버전, 전송 조합, 재현 명령과 비밀을 제거한 오류 출력을 포함해 주세요. 변경할 때에는 소스 무변경·재개·검증 계약을 유지하고 관련 문서와 필요한 회귀 검사를 함께 갱신합니다. 자세한 절차는 [개발 가이드](docs/DEVELOPMENT.md)를 따릅니다.

## 문서

| 문서 | 내용 |
|---|---|
| [요구사항](docs/REQUIREMENTS.md) | 범위와 수용 기준 |
| [아키텍처](docs/ARCHITECTURE.md) | 전송·상태관리·검증 설계 |
| [개발 및 운영](docs/DEVELOPMENT.md) | 개발 절차, 검증 기록, 운영·원복 절차 |
| [SSH 전송](docs/SSH.md) | 원격 접속과 push/pull 가이드 |
| [NAS 수용 시험](docs/NAS_TESTING.md) | 실제 NAS 환경의 시험 절차 |
| [성능 측정](docs/PERFORMANCE.md) | 측정 조건·결과와 미검증 범위 |
| [에이전트 작업 규칙](AGENTS.md) | 코드·문서 변경 시 적용하는 규칙 |

## 라이선스

라이선스는 아직 미정이며 저장소에 `LICENSE` 파일이 추가되지 않았습니다.
