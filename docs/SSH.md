# 마운트 없는 SSH 전송

2026-10-22 최적화: 원격 stat/mount RPC가 이미 수행하는 링크·중첩 마운트·루트 정체성 검사를 활용해 조정자의 중복 check/mount/stat 요청을 줄였다. 검사 결과를 장기 캐시하지 않으며, helper는 각 요청 전후의 최신 정체성과 파일 해시/세대를 계속 확인한다. 작은 파일 10개 loopback push의 run 호출은 678회에서 448회로 줄었다. 실제 WAN 처리량 수치는 아니며 상세 조건과 비용은 [PERFORMANCE.md](PERFORMANCE.md)를 따른다.

2026-10-21 추가. Linux/WSL 안의 Python CLI에서 `local/mounted → rsync_ssh` push와 `rsync_ssh → local/mounted` pull을 지원한다. 중계 인스턴스에 원격 공유를 마운트하거나 전체 파일을 내려받아 검증할 필요가 없다. 원격 파일의 해시는 원격에서 계산해 비교한다. 양쪽이 모두 rsync_ssh인 직접 전송은 차단한다. 기존 NAS→NAS 마운트 조합과 구분한다.

## 준비

- 실행 인스턴스: Python 3.12 이상, OpenSSH client, rsync 3.1.3 이상. Windows 사용자는 Python CLI도 WSL 안에서 실행한다.
- 원격: Linux, PATH에서 실행 가능한 Python 3.8 이상과 rsync 3.1.3 이상, 비어 있지 않고 읽을 수 있는 `/etc/machine-id`. Python 표준 라이브러리만 사용하며 MigraFlow/PyYAML/Rich 설치는 필요 없다. POSIX 정책은 양쪽 rsync의 ACL/xattr 기능과 충분한 계정 권한이 필요하다.
- 키/agent 인증과 검증된 known_hosts가 필요하다. 비밀번호 입력, 미등록/변경된 호스트 키의 자동 승인, 호스트 키 검사 해제는 지원하지 않는다.
- Python/helper와 rsync를 실행할 수 있는 계정을 사용한다. SFTP 전용 또는 rrsync 강제 명령 계정은 현재 helper 실행과 호환하지 않는다.
- 원본 데이터는 원격 서버에서 읽기 전용으로 보호한다. 현재 자동 판정은 마운트 `ro`에 근거하므로 읽기 전용 계정이나 NAS 고유 스냅샷만으로 자동 판정하지 않는다. 개발용 보호 예외는 운영 NAS에서 사용하지 않는다.
- 원격 worker lease용 로컬 파일시스템 디렉터리를 endpoint 밖에 마련한다. run은 그곳에 worker.lock을 만든다. 원격 상태 위치와 데이터 루트는 바인드 마운트로도 겹치면 안 된다. endpoint `/`처럼 상태 경로를 분리할 수 없는 구성은 거부한다.

SSH 계정·주소·포트·키 경로는 실행 인스턴스의 `~/.ssh/config`에 작성한다. 실제 키와 자격 증명은 YAML·DB·로그·Git에 넣지 않는다.

```sshconfig
Host nas-target
    HostName 192.0.2.20
    User migration
    Port 22
    IdentityFile ~/.ssh/migraflow_ed25519
    IdentitiesOnly yes
```

예시 주소와 경로를 실제 환경에 맞춘다. 서버 관리자가 제공한 호스트 키 fingerprint를 신뢰할 수 있는 경로로 대조한 뒤 known_hosts를 준비한다. 프로그램은 BatchMode/StrictHostKeyChecking을 강제하고 인증 agent·X11·포트 전달을 비활성화한다. SSH 설정 파일은 실행 계정이 신뢰하는 로컬 운영 설정이어야 한다.

## YAML과 명령

```yaml
source:
  type: local
  path: /srv/migraflow/source
destination:
  type: rsync_ssh
  host: nas-target
  path: /volume1/migration/data
  remote_state_dir: /var/tmp/migraflow-worker
state_dir: /var/lib/migraflow/state
verification:
  algorithm: sha256
metadata:
  policy: basic
retry:
  max_attempts: 3
  initial_delay_seconds: 1
  max_delay_seconds: 30
```

host는 영문/숫자로 시작하는 단순 OpenSSH 별칭 또는 호스트명이다. `user@host`, 포트·셸 옵션·IPv6 리터럴은 받지 않으며 SSH config 별칭으로 지정한다. path/remote_state_dir는 `..` 없는 절대 POSIX 경로이고 로컬 YAML 위치에 상대 해석하지 않는다. remote_state_dir는 원격 lease만 저장하며 계획/파일별 결과/해시/보고서 DB는 실행 인스턴스의 state_dir에 남는다.

```sh
migraflow preflight --config migration.ssh.local.yml --json
migraflow plan --config migration.ssh.local.yml --json
migraflow run --config migration.ssh.local.yml --job PLAN_ID
migraflow resume --config migration.ssh.local.yml --job PLAN_ID
migraflow verify --config migration.ssh.local.yml --job PLAN_ID --json
migraflow plan --config migration.ssh.local.yml --baseline VERIFIED_ID
migraflow plan --config migration.ssh.local.yml --resume INTERRUPTED_SCAN_ID
```

pull은 source에 rsync_ssh 설정을 두고 destination에 local/mounted를 둔다. CLI로도 설정할 수 있다.

```sh
migraflow plan --source /srv/source --destination-type rsync_ssh \
  --destination-host nas-target --destination /volume1/migration/data \
  --destination-remote-state-dir /var/tmp/migraflow-worker --state-dir /var/lib/migraflow/state
```

같은 옵션을 run/resume/verify에도 제공하거나 YAML을 사용한다. host·경로·remote_state_dir·해시/메타데이터 정책을 바꾸면 새 계획이 필요하다. 기존 대상 파일은 검토·복구 수단을 확보한 `--overwrite` 작업만 교체한다. 과거 회차 승인을 증분에 승계하지 않는다. 삭제는 하지 않는다.

기본 preflight/plan은 양측 데이터를 수정하지 않고 원격 lease 디렉터리도 생성하지 않는다. 원격 쓰기 시험은 기존과 동일하게 명시적 `preflight --test-destination-write`에서만 수행하고 고유 시험 파일을 삭제한다. 일반 조회 atime은 OS 정책에 따라 변할 수 있다.

## 안전·재개·검증 계약

계획·스캔 재개·전송·증분·전체 검증은 기존 상태 엔진을 공유한다. RPC 디렉터리 페이지는 최대 500개, DB 처리는 fetchmany(500), 원격 해시는 1MiB 읽기 청크를 사용한다. 선택한 SHA-256/MD5를 원격에서 계산하고 파일 세대·양측 현재 해시를 대조한다. rsync 성공 메시지를 해시 완료로 해석하지 않는다.

SSH의 원격 셸 명령에는 고정 helper 코드만 들어간다. 사용자 루트는 JSON stdin, 파일명은 rsync `-s`와 NUL 목록/프로토콜로 전달한다. helper는 메모리에서 실행하며 원본에 프로그램을 업로드하지 않는다. 원격 Python은 isolated 모드와 bytecode 쓰기 비활성화로 데이터 경로의 동명 Python 파일을 import하지 않으며 UTF-8 파일시스템 인코딩으로 비UTF8 이름도 surrogateescape로 보존한다.

원격 machine-id, 마운트 ID/옵션/공유, 루트 dev/ino를 검사하며 RPC와 rsync 모두 열린 디렉터리를 작업 루트로 고정한다. 중첩 마운트·symlink·루트 교체는 차단한다. 양쪽 endpoint·조정자 state_dir·원격 remote_state_dir 모두를 machine-id/device와 마운트 physical path로 대조한다. 아직 없는 lease 경로도 기존 상위 디렉터리의 파일시스템을 사용해 비교하며 데이터·상태와의 동일/상위/하위·바인드 별칭 중첩을 임시 쓰기 시험보다 먼저 거부한다. 알려진 동일 NFS/SMB 공유 중첩도 확인한다. 다른 서버/공유 별칭으로 노출된 동일 저장소는 운영자가 분리 여부를 확인한다. 재연결 시 처음 확인한 endpoint와 lease 파일시스템 정체성을 전달하고 변경되면 거부한다.

로컬 감시 프로세스와 원격 worker는 각각 lease를 유지한다. 조정자 종료 시 SSH 데이터 채널의 EOF로 원격 rsync를 종료·회수한 뒤 원격 lease를 해제한다. lease가 살아 있으면 재개와 원격 메타데이터 쓰기를 거부한다. 네트워크 단절로 원격 종료 감지가 지연돼도 잠금 파일을 삭제하여 우회하지 않는다. 새 worker의 잠금 획득 실패는 쓰기 성공으로 처리하지 않는다.

알려진 일시 rsync/해시 읽기 오류는 기존 제한 백오프를 사용한다. SSH 해시 연결을 다시 열 때 원래 정체성을 확인한다. 인증·호스트 키·정책·원본 변경 오류는 자동 승인/우회하지 않는다. 스캔이나 사전 점검의 연결 오류는 실패로 끝나며 복구 후 같은 스캔 체크포인트 조건을 확인해 재개한다.

POSIX 정책과 별도 status 감시, cutover-check도 기존 계약을 따른다. 원격 해시는 실제 읽은 바이트 증가를 약 1초마다 전달하고 완료 응답의 최종 해시·세대를 대조한다. 바이트 증가만 무진행 제한을 갱신하며 반복 값·불완전 응답은 갱신하지 않는다. 기본 총 시간 상한은 없고, YAML `timeouts`의 IO/원격 해시 무진행 제한과 선택적인 총 시간 제한은 [README](../README.md#장시간-작업의-시간-제한)를 따른다. 전환 증빙은 외부 동결/승인을 인증하지 않으며 프로그램은 서비스를 전환하지 않는다.

## 검증과 남은 범위

실제 loopback OpenSSH 서버에서 push/pull, SHA-256/MD5, 인용부호·공백·줄바꿈·한글 이름과 루트, 검증 완료 재사용, 변조 탐지/복구, 증분, 게시 직후 중단, 원격 계정의 원본 무쓰기, 키 거부, 중첩/루트 교체, POSIX 속성, 일시 오류 재시도/재연결, 원격 lease 차단, 500개 페이지의 스캔 재개를 검사한다. 강제 종료 검사는 SSH push 중 조정자를 kill하고 양쪽 worker lease 해제 후 재개한다. mounted+SSH는 모의 마운트 정체성과 실제 SSH/rsync를 함께 사용한다.

Linux에서 설치된 sshd를 사용하거나 `MIGRAFLOW_TEST_SSHD`에 시험용 sshd 절대 경로를 지정해 실행한다. 테스트는 127.0.0.1의 임의 포트, 고유 임시 host/client 키와 known_hosts를 사용하고 종료 시 서버를 닫는다. /tmp 키를 읽는 시험 서버에 한해 StrictModes=no를 쓰며 실제 운영 서버의 설정이나 SSH client 호스트 키 검사는 완화하지 않는다. 일반 SSH 서버가 없으면 해당 통합 검사를 skip으로 기록한다.

```sh
MIGRAFLOW_TEST_SSHD=/usr/sbin/sshd python3 -m pytest -q tests/test_ssh.py
```

실제 NAS SSH 권한·read-only 스냅샷·ACL·단절·장시간 IO와 WAN/백만 단위 성능은 미검증이다. 메타데이터/안전 확인은 여러 RPC 왕복을 사용하므로 지연이 큰 네트워크에서 별도 측정해야 한다. Python 3.8/rsync 3.1.3은 지원 코드의 최소 조건이며 이번 실제 통합 시험 환경의 버전은 DEVELOPMENT.md에 구분해 기록한다. 운영 수용 시험은 [NAS_TESTING.md](NAS_TESTING.md)에 SSH 접속/원격 보호/lease 조건을 추가해 수행한다.

전송 옵션 근거: [rsync 공식 매뉴얼](https://download.samba.org/pub/rsync/rsync.1.html), [OpenSSH 설정](https://man.openbsd.org/ssh_config), [OpenSSH 원격 명령](https://man.openbsd.org/ssh).
