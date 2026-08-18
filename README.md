# rig_tools — 카메라 시점 고정

매 세션 카메라를 분해해야 하는 리그에서, 재장착 후에도 **같은 시점을 재현**하기 위한 도구.

`act_colorsort` 데이터셋이 ep59→ep60에서 59px(화면 높이의 25%) 튄 것을 사후에야 발견했다.
이 도구를 수집 전에 돌리면 그 자리에서 막힌다.

| 파일 | 역할 |
|---|---|
| `rig_check.py` | 저장한 기준 화면과 현재 화면 비교. **메인 도구** |
| `rig_focus.py` | 렌즈 초점 맞출 때 선명도 실시간 표시 |
| `rig_common.py` | 공용 모듈 (직접 실행 안 함) |

---

## ⚠️ 먼저: 임계값 저장 (아직 안 됨)

현재 프로파일에 임계값이 없어서 기본값(20px / 0.80)으로 돌고 있다. **너무 느슨해서 카메라를 밀어도 통과한다.**
카메라가 기준 위치에 있는 상태에서 한 번 실행할 것:

```bash
python rig_tools/rig_check.py --camera /dev/video4 --profile top \
  --update --max-shift 2.2 --min-correlation 0.87
```

출력에 이 줄이 나와야 저장된 것:
```
limits    : shift <= 2.2, corr >= 0.87
```

> `--update`는 **기준 화면도 지금 화면으로 새로 저장**한다. 카메라가 원하는 위치에 있을 때만 실행할 것.

---

## 매 세션 사용법

### 1. 확인 (창 모드)

```bash
python rig_tools/rig_check.py --camera /dev/video4 --profile top
```

`레퍼런스 | 현재 | 오버레이` 3분할 창이 뜬다. 오버레이는 기준=초록, 현재=마젠타, **맞으면 회색**.

상단 배너가 **초록(OK)** 이 될 때까지 마운트를 미세조정하고 `q`로 종료.

키: `q`/`ESC` 종료, `SPACE` 현재 화면으로 기준 재저장(구도를 바꿨을 때만)

### 2. 수집

```bash
lerobot-record ...
```

### 게이트로 자동화

```bash
#!/usr/bin/env bash
set -e
python rig_tools/rig_check.py --camera /dev/video4 --profile top --quiet \
  || { echo "시점이 틀어졌습니다. 창 모드로 맞추고 다시 실행하세요."; exit 1; }
lerobot-record ...
```

---

## 명령어 정리

```bash
# 창 없이 판정만
python rig_tools/rig_check.py --camera /dev/video4 --profile top --no-display

# 종료코드만 (스크립트용). 0=통과, 1=드리프트
python rig_tools/rig_check.py --camera /dev/video4 --profile top --quiet

# JSON 출력
python rig_tools/rig_check.py --camera /dev/video4 --profile top --json

# 기준 재저장 (구도를 바꿨을 때)
python rig_tools/rig_check.py --camera /dev/video4 --profile top --update
```

`--camera`는 `/dev/video4`, 인덱스(`4`), **또는 이미지 파일**을 받는다.
이미지 파일이면 창 없이 동작하므로 이미 찍은 데이터셋 프레임도 검사할 수 있다.

---

## 현재 확정 세팅

| | 값 |
|---|---|
| 씬캠 | `/dev/video4` |
| 손목캠 | `/dev/video2` |
| 해상도 | 320 x 240 |
| ROI | `[0, 0, 320, 94]` (벽 + 바구니 밴드) |
| 임계값 | `--max-shift 2.2 --min-correlation 0.87` |

`/dev/video0`은 **노트북 내장 웹캠**이니 쓰지 말 것.
USB 재연결 시 번호가 바뀌므로, 자주 바뀌면 `/dev/v4l/by-id/` 경로를 쓰는 게 안전하다.

ROI와 임계값은 프로파일(`rig_tools/profiles/top/profile.json`)에 저장되므로
평소엔 `--profile top`만 쓰면 자동 적용된다.

---

## 비교 영역(ROI) 설정

비교할 영역을 `x,y,w,h`로 지정한다 (1 이하 값은 비율). **팔과 블록이 있는 영역을 빼는 게 핵심이다.**

실측 결과:

| 영역 | 같은 위치인데 FAIL(오탐) |
|---|---|
| 전체 화면 | 14개 중 **10개** |
| 바구니 밴드만 | **0개** |

### 여러 영역 지정

플래그를 반복하거나 `;`로 나누면 여러 영역을 쓸 수 있다.
팔이 지나가는 가운데를 건너뛰고 좌·우 정지 구조물만 보는 식이다.

```bash
# 좌우 두 곳만
--roi 0,0,0.3,0.65 --roi 0.7,0,0.3,0.65

# 한 줄로도 가능
--roi "0,0,0.3,0.65; 0.7,0,0.3,0.65"

# 픽셀로도 가능
--roi 0,0,96,156 --roi 224,0,96,156
```

판정은 **보수적으로** 합친다 — shift는 영역 중 최댓값, correlation은 최솟값. 한 영역만 틀어져도 FAIL이다.

출력에 영역별 내역이 따로 나오는데, 이게 원인 구분에 쓰인다:

```
windows     : 2  (worst values shown above)
  [0] [0, 0, 96, 156]     dx  +0.75 dy -52.01  shift 52.01  corr 0.175
  [1] [224, 0, 96, 156]   dx  -0.61 dy -42.59  shift 42.59  corr 0.697
```

- **모든 영역이 같이 움직임** → 카메라가 이동한 것
- **한 영역만 움직임** → 그 영역의 물건(바구니 등)이 밀린 것

창에는 각 영역이 번호와 함께 사각형으로 그려지고, 많이 틀어진 영역은 주황색으로 바뀐다.

구도를 바꾸면 영역도 다시 잡아야 한다. 옛 프로파일의 단일 `[x,y,w,h]` 형식도 그대로 읽힌다.

## 임계값 재측정

리그 구성을 바꿨으면 다시 재는 게 좋다.

```bash
python rig_tools/rig_check.py --camera /dev/video4 --profile top --suggest 15
```

30초 동안 15장을 샘플링한다. **그동안 팔과 블록을 실제로 움직여야 한다.**
정지 상태로 재면 지나치게 빡빡한 값이 나와서 수집 중에 계속 막힌다
(샘플이 전부 같으면 경고가 뜬다).

끝나면 저장 명령까지 출력된다.

---

## 손목캠 초점 (필요할 때만)

```bash
python rig_tools/rig_focus.py --camera /dev/video2 --target 40
```

`현재 영상 | 에지 영상` 창에 선명도와 peak가 표시된다. 렌즈를 돌려 peak가 더 안 오르는 지점을 찾는다.

측정 시 주의:
- **질감 있는 대상**을 봐야 한다. 빈 검은 테이블을 보면 초점과 무관하게 숫자가 낮다
- **실제 작업 거리**에서 재야 한다 (블록 놓는 자리에 인쇄물을 깔면 좋다)
- 절대 숫자보다 **peak 위치**가 중요하다

키: `r` peak 리셋, `s` 스냅샷, `q` 종료

---

## 트러블슈팅

**`error: cannot open camera '/dev/video4'`**
다른 프로세스가 카메라를 잡고 있다. 카메라는 한 번에 한 프로세스만 열 수 있다.

```bash
fuser -v /dev/video4          # 누가 잡고 있는지
```

보통 `lerobot-teleoperate`가 범인이다. 끄고 다시 실행할 것.
카메라 없이 텔레오퍼레이션하려면 `--robot.cameras` 인자를 빼면 된다(기본값이 빈 dict).

수집 시엔 `rig_check`(종료) → `lerobot-record`(카메라 인계) 순서라 겹치지 않는다.

**`ioctl(VIDIOC_QBUF): Bad file descriptor`**
OpenCV가 카메라를 닫을 때 내는 경고. 무시해도 된다.

**항상 OK만 뜬다**
임계값이 저장 안 됐을 수 있다. `profile.json`에 `max_shift`가 있는지 확인.
없으면 기본값 20px이라 웬만해선 안 걸린다.

**OK가 뜬다고 구도가 좋다는 뜻은 아니다**
`rig_check`는 **저장해둔 화면을 재현하는지만** 본다.
블록이 화면 밖으로 나가는 나쁜 구도를 저장했으면 계속 OK를 띄우면서 같은 문제를 반복한다.
구도 자체는 **네 귀퉁이 테스트**로 확인할 것 — 블록을 픽업 영역 최좌/최우/최앞/최뒤에 놓고
4번 다 화면에 온전히 잡히는지.

---

## 같이 해둘 것

- 책상에 마스킹테이프로 **패드 모서리 2곳, 로봇 베이스 2곳, 바구니 3개** 위치 표시
- 에피소드 메타에 **`session_id`** 기록 → 나중에 시점이 틀어진 세션만 골라 뺄 수 있다

> ArUco 마커 방식(`rig_calib.py`, `make_markers.py`)도 만들어 뒀지만 쓰지 않는다.
> 마커를 장패드에 붙이면 기준 프레임이 로봇 베이스가 아니라 패드가 되어버려서,
> 패드-로봇 상대 변화를 오히려 가린다. 테이블·바구니까지 매번 옮기게 되면 그때 다시 검토.
