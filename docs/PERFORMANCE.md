# 성능 개선과 검증 기록

## 2026-10-22 상태 집계와 SSH 개선

WSL Python 3.14.4, Linux /tmp, 실제 loopback SSH로 기존 표본과 비교했다. 7바이트 파일 10개의 계획 RPC는 34→14회, run의 전송·전체 검증 RPC는 678→448회(약 34% 감소)다. hash 30회는 유지한다. 최종 run 구성은 check 20, hash 30, hello 1, identity 9, idle 1, mount 247, stat 130, utime 10이다. 이는 helper 호출 수이며 rsync 패킷 수·네트워크 처리량이 아니다. 최신 마운트/루트/링크 검사와 전체 해시 비교를 생략하지 않는다.

SQLite schema v6는 행 변경과 같은 트랜잭션에서 목록·검증 상태 카운터를 유지한다. 스캔 완료 시 삭제 후보 count/sample을 저장하고 기준/현재 목록 변경 시 무효화한다. 최초 마이그레이션은 기존 목록과 결과를 한 번 집계한다. 읽기 전용 구형 DB는 기존 쿼리를 사용하며 마이그레이션하지 않는다.

`tools/benchmark_summary.py`로 각 20만 행인 기준/현재 목록(동일 경로, 파일 size=1, file_results 없음)을 비교했다. 네 번 조회의 중앙값이며 삽입 시간은 조회에서 제외했다.

| 항목 | 기존 집계 | v6 집계 |
|---|---:|---:|
| 증분 summary | 140.69ms | 0.043ms |
| 40만 행 삽입·계획 완료 처리 | 1.01초 | 2.56초 |
| DB 파일 | 39,882,752B | 39,882,752B |
| 측정 종료 WAL | 40,301,872B | 40,248,312B |

읽기 비용 감소와 함께 쓰기 증폭이 발생한다. 이 합성 표본은 하나의 큰 삽입 트랜잭션을 사용하며 실제 500개 배치의 WAL 크기·처리량을 뜻하지 않는다. 파일 결과가 많은 실제 전송과 콜드 캐시, 백만 파일/RSS/NAS/WAN은 별도 측정 대상이다. 기존 방식부터 실행한 단일 비교이므로 절대 시간이나 배율을 운영 보장으로 사용하지 않는다.

재현 명령(Linux 의존성 설치 환경):

```sh
python3 tools/benchmark_summary.py --directory /tmp/migraflow-summary-benchmark --rows 200000
```

매 실행은 새 하위 디렉터리를 만들고 측정 DB를 보존한다. 기존 사용자 데이터는 덮어쓰지 않는다. 구형 비교 DB는 전용 임시 표본에서만 트리거를 제거해 이전 집계 경로를 실행한다.

2026-10-14 기준. 실험은 생성한 로컬 데이터만 사용했다. 실제 NAS는 미준비이며 백만 단위 수용 시험과 처리량 SLA는 아직 확정하지 않았다.

2026-10-21 추가된 SSH push/pull의 처리량은 이 표에 포함하지 않는다. SSH는 메타데이터·안전 확인의 RPC 왕복이 있으므로 [SSH.md](SSH.md)와 실제 NAS/WAN 조건에서 별도 측정한다. 아래 소스 fingerprint와 검사 수는 당시 로컬 최적화의 기록이다.

## 적용한 변경

- 매번 새 `/proc/self/mountinfo`를 읽고 원문이 같을 때만 파싱 결과 1개를 재사용한다. 가장 긴 마운트 경계부터 찾고 반환 사본을 사용한다. 마운트 해제/재연결 정보를 오래 캐시하지 않는다.
- 파일/조상마다 한 번의 lstat로 symlink/junction을 판별한다. 중복 stat와 이미 검사한 루트의 반복 resolve를 줄인다. 경로·파일 세대 결과 자체는 캐시하지 않는다.
- 상태 집계는 기존 action 인덱스로 파일 수·바이트를 함께 계산하고 검증 상태 건수를 재사용한다. result_status, 희소 오류용 result_errors, 디렉터리 역순용 entry_directories, attempt_outcomes 인덱스를 추가한다.
- 500개 배치·큐와 1MiB 해시 청크를 유지한다. 원본/대상 해시, stat/fstat, 마운트·루트·링크 점검, WAL FULL 동기화와 worker lease를 생략하지 않는다. 병렬 해시나 검증 생략 정책을 추가하지 않았다.

## 측정 조건

Windows 11 build 26300/Python 3.12.10 + WSL rsync와 WSL Linux kernel 6.18.33.2/Python 3.14.4/rsync 3.4.1에서 실행했다. Windows 표본은 NTFS, Linux 표본은 WSL 내부 /tmp 파일시스템이다. SHA-256, metadata=basic, 300개 × 1KiB 파일/10개 그룹/중첩 폴더/3회 실행의 중앙값이다. run은 전송 전 점검·전송·배치 검증·마지막 전체 검증을 포함한다. incremental은 기존 전체 파일의 현재 해시 확인·복사 생략·전체 최종 검증이며 새 증분 plan 생성 시간은 포함하지 않는다. status는 열린 read-only DB에서 summary 함수만 10회씩 측정하며 CLI 기동 비용은 제외한다.

tracemalloc으로 Python 할당을 추적했으므로 계측 비용이 포함된다. peak_python_bytes는 프로세스 RSS, SQLite C 캐시, OS 캐시나 rsync 메모리가 아니다. 동일 호스트의 다른 작업과 OS 캐시 부하를 통제하지 않았으며 엄밀한 격리 성능 보장은 아니다. 하드웨어 예산과 RSS는 이 실험에서 수집하지 않았다.

개선 전 소스 fingerprint는 `4431f23ac12747a21c5ca3729463706edca7bae081096f9ce8aa59d83c1eda06`, 아래 개선 후 측정 fingerprint는 `71710b08eacd38c416a58197b0f03ce3ee15885a1309892994def5d00a95a49a`다. fingerprint는 tools/benchmark.py가 파일명과 src/migraflow/*.py 바이트를 해싱한 값이며 Git commit ID가 아니다. 이후 감시 표시/미완료 보고서 기록 보완은 영향 범위 회귀 검사로 확인했다.

### Linux 로컬, 300개 파일

| 단계 | 개선 전(초) | 개선 후(초) | 시간 감소 |
|---|---:|---:|---:|
| plan | 0.562126 | 0.080290 | 85.7% |
| run | 10.108545 | 1.563770 | 84.5% |
| verify | 2.633535 | 0.399482 | 84.8% |
| 동일 파일 증분 실행 | 7.278635 | 1.010911 | 86.1% |
| summary | 0.000434 | 0.000331 | 23.9% |

개선 후 단계별 최대 Python 할당량: plan 2,724,232B, run 1,731,001B, verify 1,520,694B, incremental 1,565,129B. 기존 할당량과 비슷하며 목록 전체를 메모리에 올리지 않았다. Linux 개선 전 원본 측정 경로는 /tmp/migraflow-benchmarks-before/run-wsfqlyw1, 개선 후는 /tmp/migraflow-final-benchmarks/run-ffi2dxs5였다. /tmp는 영구 보관 위치가 아니며 표의 측정 결과를 본 문서에 기록한다.

### Windows Python + WSL rsync, 300개 파일

| 단계 | 개선 전(초) | 개선 후(초) | 시간 감소 |
|---|---:|---:|---:|
| plan | 0.315644 | 0.238433 | 24.5% |
| run | 14.126475 | 10.768703 | 23.8% |
| verify | 2.601287 | 1.578104 | 39.3% |
| 동일 파일 증분 실행 | 7.035945 | 3.854400 | 45.2% |
| summary | 0.000622 | 0.000447 | 28.2% |

개선 후 단계별 최대 Python 할당량: plan 51,267B, run 1,463,168B, verify 1,269,979B, incremental 2,262,232B. 원본 JSON은 Git에서 제외된 work/benchmarks-before-windows/run-zyd1w6ll/result.json과 work/benchmarks-final-windows/run-0ocufb2p/result.json에 있다. WSL worker 기동·감시 비용이 포함되어 Linux 직접 실행과 절대 시간을 비교해 NAS 성능을 결론내리지 않는다.

### 제한된 배치의 1만 파일 표본

같은 Linux 환경에서 10,000 × 1KiB 파일, 1회, tracemalloc 사용으로 plan 2.731초, run 53.359초, verify 13.917초, 동일 파일 증분 35.029초를 측정했다. summary 10회 중앙값은 0.008968초였다. 다른 회귀 검사와 동시 실행한 참고값이며 처리량 비교용 격리 실험이 아니다.

단계별 최대 Python 할당량은 plan 2,721,589B, run 1,742,523B, verify 1,573,468B, incremental 1,680,280B로 최대 약 2.60MiB였다. 전체 10,000개 검증과 같은 파일 재사용을 확인했다. 이 결과를 100만~1000만 파일·전체 RSS·DB/WAL 공간·실제 NAS 성능 달성으로 표시하지 않는다.

## DB 인덱스 비용

Windows SQLite 합성 표본 50,000 entries, 20 jobs와 attempts 이력에서 새 집계 방식은 양쪽에 동일하게 두고 인덱스만 비교했다. summary 10회 중앙값, 디렉터리 fetchmany(500), (plan_id,path) PK 업데이트 950개를 측정했다.

| 항목 | 새 인덱스 없음 | 새 인덱스 있음 |
|---|---:|---:|
| summary | 0.028772초 | 0.009794초 |
| 디렉터리 조회 | 0.007411초 | 0.005032초 |
| 상태 업데이트 | 0.005641초 | 0.004566초 |
| 논리 DB 크기 | 19,148,800B | 20,750,336B |

summary 시간은 약 66% 줄고 DB 공간은 약 8.4% 증가했다. 이 표본에서 업데이트 시간은 악화하지 않았으나 인덱스는 쓰기/삽입 비용이 있으므로 일반적으로 무비용이라고 해석하지 않는다. 논리 DB 크기는 page_count × page_size로 WAL 크기를 포함하지 않는다. 기존 DB 최초 인덱스 생성 시간과 대규모 공간은 별도 점검한다. 원본은 work/db-benchmarks-release/run-rz92r44q/result.json이다.

## 재현과 회귀 검사

생성 데이터는 고유 시험 폴더에 남으며 자동 삭제하지 않는다. 운영 source/destination를 지정하는 도구가 아니다.

```powershell
python tools/benchmark.py --directory work/benchmarks --files 300 --rounds 3 --memory
python tools/benchmark_db.py --directory work/db-benchmarks --rows 50000
python -m pytest -q --basetemp=work/pytest-current
```

Linux/WSL 안에서 의존성을 설치한 Python으로 실행한다. Linux 파일시스템 표본은 Windows 저장소의 work 대신 Linux 시험 디렉터리를 지정한다.

```sh
python3 tools/benchmark.py --directory /tmp/migraflow-benchmarks --files 300 --rounds 3 --memory
python3 tools/benchmark.py --directory /tmp/migraflow-scale --files 10000 --rounds 1 --memory
python3 -m pytest -q --basetemp=/tmp/migraflow-tests
```

측정 raw JSON의 환경/fingerprint/초 단위 개별 값과 cProfile 상위 함수를 함께 확인한다. 통제된 비교는 같은 의존성·호스트·표본·계측 조건으로 버전별 중앙값을 측정하고 순서를 교차해 캐시 편향을 줄인다. 실제 성능 평가에는 tracemalloc 없는 시간과 OS별 RSS/DB/WAL/NAS 부하도 추가한다.

전체 회귀 결과는 Linux 211 passed, Windows+WSL 187 passed/24 skipped였다. 제외 항목은 Linux 전용 mounted/POSIX와 Windows 파일명/링크 제약이며 실제 NAS 시험을 통과한 것으로 계산하지 않는다. 이후 감시 처리율과 보고서 502개 추가 항목 스트리밍/조사 취소에 대한 회귀 검사도 추가했다. 최종 영향 범위 결과는 DEVELOPMENT.md에 기록한다.

다음 수용 시험은 [NAS_TESTING.md](NAS_TESTING.md)를 따른다. 현재 1만 파일 표본은 100만~1000만 규모를 대신하지 않는다.

## 전환 보고서 대기 큐 보강 (2026-10-21)

report_queue(done)의 임시 인덱스를 추가했다. 완료 행을 남기는 기존 방식에서 pending 조회마다 누적 완료 행을 전체 스캔하는 비용을 줄인다. 인덱스 생성·삽입·done 변경의 저장 공간/쓰기 비용이 추가되며 큐 DROP 시 함께 제거된다. 영속 schema는 v5 그대로다.

Windows Python 3.12의 메모리 SQLite에서 동일 큐의 SELECT/UPDATE를 반복했다. 아래 수치는 100 VM 스텝마다 호출한 progress callback 횟수이며 인덱스 생성·초기 삽입·파일 IO·디렉터리 조사 시간은 포함하지 않는다.

| 디렉터리 행 | 인덱스 없음 | pending 인덱스 |
|---|---:|---:|
| 1,000 | 15,335 | 410 |
| 2,000 | 60,670 | 820 |
| 10,000 | 미측정 | 4,100 |

EXPLAIN QUERY PLAN은 SCAN에서 `SEARCH report_queue USING INDEX report_queue_pending (done=?)`로 바뀌었다. 실제 보고서 통합 회귀는 큐 처리 중의 조회 계획을 검사하고 디렉터리 80개의 추가 항목 전체 저장과 출력 샘플 20개를 확인한다. 기존 502개 추가 항목 저장/조사 중단 검사도 유지한다. 실제 NAS/RSS/백만 단위 성능 수치는 아니다.
