# 실제 NAS 수용 시험

2026-10-14: 실제 Linux/WSL NAS 마운트 환경은 사용자 확인 결과 아직 미준비다. 아래 절차는 준비용이며 수행 완료 기록이 아니다. 현재 mounted 회귀 검사는 모의 정체성과 실제 로컬 rsync를 사용한다.

2026-10-21: 마운트 없는 SSH push/pull도 추가했다. SSH 환경은 [SSH.md](SSH.md)의 host key·원격 Python/rsync·read-only·lease 조건을 먼저 준비한다. 아래 마운트 제거 시험은 mounted 경로에 적용하며 SSH에는 네트워크 단절·원격 루트/마운트 교체·원격 lease 유지와 재개 시험을 추가한다. 실제 NAS SSH 시험도 아직 수행하지 않았다.

## 준비와 기록

Linux/WSL 안에서 Python 3.12 이상과 rsync를 설치하고 Python CLI도 그 안에서 실행한다. Windows Python의 네트워크 드라이브/UNC 지원으로 우회하지 않는다. 실제 NFS 또는 SMB 마운트는 OS/관리자가 구성한다. 자격 증명은 OS의 제한된 credentials 파일 등으로 관리하며 YAML·DB·로그·Git에 넣지 않는다.

원본은 전용 시험 데이터의 read-only 스냅샷 또는 읽기 전용 마운트와 최소 권한 계정으로 보호한다. 운영 서비스의 소스 변경과 MigraFlow 접근 권한을 구분한다. 대상은 시험 전용 경로와 복구 가능한 스냅샷을 사용한다. 상태 DB는 양쪽 endpoint 밖의 로컬 디스크에 둔다. source/destination 루트는 미리 존재해야 한다.

기록 항목: NAS 모델/펌웨어, NFS/SMB 버전과 옵션, 클라이언트 OS/kernel/Python/rsync, numeric UID/GID 매핑, POSIX/고유 ACL 방식, 시간 정밀도, 스냅샷, 네트워크 속도, 시험 경로와 공간. `findmnt --target /mnt/source` 및 `/mnt/destination`에서 source/옵션/마운트 ID를 확인하고 예상 공유 문자열을 정확히 사용한다.

```yaml
source:
  type: mounted
  path: /mnt/source/test-data
  mount_source: nas-source:/export/data
destination:
  type: mounted
  path: /mnt/destination/test-data
  mount_source: nas-target:/export/data
state_dir: /var/lib/migraflow/test-state
verification:
  algorithm: sha256
metadata:
  policy: basic
```

SMB의 예상 source는 예를 들어 `//nas-source/share`다. 두 endpoint는 독립적으로 local/mounted를 지정한다. local→mounted, mounted→local, mounted→mounted 조합별로 시험한다. 설정을 바꾸면 새 계획이 필요하다.

## 작은 표본의 실제 시험

1. 빈 파일, 작은 파일 다수, 큰 파일, 깊은 폴더, 빈 디렉터리, 공백·한글·줄바꿈 파일명, 기존 대상/추가 항목을 시험 전용 트리에 준비한다. 원본의 내용 해시·mtime·mode·owner·필수 ACL/xattr 기준 목록을 외부 도구로 저장한다. 조회 atime은 마운트 정책에 따라 바뀔 수 있다.
2. `migraflow preflight --config nas.local.yml --json`으로 마운트 정체성·원본 read-only·공간을 확인한다. 별도 의도한 대상 쓰기 시험에만 `--test-destination-write`를 추가한다. NAS 읽기 권한/rename/fsync/mtime는 실제 시험으로 확인한다.
3. `migraflow plan --config nas.local.yml --json`으로 BLOCKED와 덮어쓰기를 검토한다. 전용 대상 또는 복구 수단을 확보한 명시적 작업에만 run의 `--overwrite`를 사용한다. 운영 NAS에서 `--allow-unprotected-source`를 사용하지 않는다.
4. `migraflow run --config nas.local.yml --job ID`를 실행하고 별도 터미널에서 `status --state-dir /var/lib/migraflow/test-state --job ID --watch`로 관찰한다. 완료 후 `verify --config nas.local.yml --job ID --json`과 외부 기준 목록으로 소스 무변경/대상 일치를 확인한다.
5. 스캔 중 Ctrl+C 후 `plan --resume SCAN_ID`, 전송/검증 중 Ctrl+C 후 `resume --job ID`를 실행한다. 강제 종료는 시험 프로세스에만 수행하고 worker lease가 끝날 때까지 기다린다. 검증 완료 파일의 inode/쓰기 횟수와 DB 체크포인트로 재복사 생략을 확인한다. 잠금 파일을 삭제하여 우회하지 않는다.
6. 동일 size/mtime의 대상 1바이트 변조·대상 누락을 만들고 verify 실패 및 허용된 resume 복구를 확인한다. 원본 변경은 개발용 다른 시험 소스에서만 만들고 기존 계획 거부·새 계획 요구를 확인한다.
7. 최초 검증 ID를 `plan --baseline ID`에 지정한다. 신규/변경 파일만 쓰고 같은 파일은 재사용하는지 확인한다. 원본 삭제/이름 변경의 옛 대상은 삭제되지 않아야 한다. 추가 항목은 cutover-check 보고서에서 검토한다.

## 연결·마운트·메타데이터 시험

네트워크 단절과 mount 제거를 구분한다. 시험용 NAS/네트워크만 일시 단절하고 transient IO의 제한 횟수·백오프를 확인한다. NFS hard IO는 커널에서 오래 대기할 수 있으므로 프로세스 종료가 즉시 완료된다고 가정하지 않는다.

시험용 mount 제거·공유 변경·다른 공유 재연결은 계획 전/스캔/전송/검증 각각에서 실패해야 한다. 노출된 빈 로컬 디렉터리에는 새 데이터가 쓰이지 않아야 한다. remount로 mount ID가 바뀌면 이전 계획을 재개하지 않고 새 full plan을 만든다. 운영 공유를 강제 해제하지 않는다.

필수 POSIX ACL/xattr가 양쪽에서 표현되는 환경만 metadata.policy=posix를 선택한다. source mode·UID/GID·named/default ACL·user xattr, 여러 배치의 쓰기 불가 디렉터리, target 변조를 시험한다. NAS root_squash/SMB 계정 매핑과 권한으로 owner/group/ACL 복사가 실패하면 실패로 처리한다. Windows/NAS 고유 ACL, endpoint 루트 권한, hardlink/symlink와 atime은 현재 보존 범위가 아니며 별도 검토/이전 절차가 필요하다.

## 규모와 최종 전환

작은 표본 → 1만 → 10만 → 100만 → 1000만 파일 순으로 CPU/RSS, scan/transfer/verify 처리량, DB+WAL/인덱스 공간, 대상 임시 공간, 중단 후 재개 시간을 측정한다. [PERFORMANCE.md](PERFORMANCE.md)의 로컬 합성값을 NAS 처리량으로 외삽하지 않는다. 처리량·메모리·복구 시간 SLA를 먼저 확정한다.

최종 증분 시 소스 서비스와 대상 쓰기를 모두 동결하고 최신 회차를 전체 검증한다. cutover-check에 동결·복구 스냅샷·담당자 승인·권한 검토 참조를 기록한다. 추가 항목이 있으면 유지/삭제의 작업별 정책을 검토하며 프로그램은 삭제하지 않는다. READY 보고서도 외부 증빙의 진위를 인증하거나 서비스를 전환하지 않는다.

담당자가 업무 읽기/쓰기·권한·원복 연습과 미승인 차이 0을 확인한 뒤 연결을 변경한다. 새 대상의 쓰기 시작 시점을 기록하고 원본 보존 정책을 유지한다. 새 쓰기 이후 단순 연결 복원으로 변경분을 버리지 않도록 롤백 절차를 마련한다. 검사별 명령/ID·exit code·보고서·환경·실패/skip·해결 내역을 남겨 실제 NAS 수용 완료 여부를 판단한다.
