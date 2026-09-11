# 시외버스 잔여좌석 감시 (txbus.t-money.co.kr)

매진된 배차에 좌석이 다시 생기면 Gmail로 알려준다. 10분마다 launchd가 `watch.py check`를 실행한다.

## 1. 터미널 코드 찾기

```sh
/usr/bin/python3 watch.py find 춘천            # 출발지 후보
/usr/bin/python3 watch.py arrivals 0511601 춘천 # 그 출발지에서 갈 수 있는 도착지
```

도착지는 출발지마다 갈 수 있는 곳이 정해져 있다. 반드시 `arrivals`로 확인한 코드를 쓴다.
(고속버스 노선은 이 사이트가 아니라 kobus.co.kr 소관이라 여기서 조회되지 않는다.)

## 2. Gmail 앱 비밀번호 발급

1. <https://myaccount.google.com/security> → **2단계 인증** 켜기 (안 켜면 앱 비밀번호 메뉴가 안 보인다)
2. <https://myaccount.google.com/apppasswords> → 이름 아무거나 입력 → 16자리 비밀번호 발급
3. 그 16자리를 `config.json`의 `app_password`에 넣는다 (공백은 있어도 되고 없어도 된다)

> 회사 Google Workspace 계정은 관리자가 앱 비밀번호를 막아둔 경우가 있다.
> 막혀 있으면 개인 Gmail 계정을 발신용으로 쓰고 `to`만 회사 주소로 두면 된다.

## 3. 설정

```sh
cp config.example.json config.json
```

```json
{
  "routes": [
    {
      "name": "동서울→춘천",
      "depr_cd": "0511601",
      "arvl_cd": "2443501",
      "dates": ["2026-09-12"],
      "time_from": "00:00",
      "time_to": "23:59",
      "min_seats": 1
    }
  ],
  "gmail": {
    "user": "보내는주소@gmail.com",
    "app_password": "abcd efgh ijkl mnop",
    "to": "받는주소@gmail.com"
  },
  "notify_macos": true
}
```

- `routes`는 여러 개를 넣어도 된다. `dates`도 여러 날짜를 넣을 수 있다.
- `time_from` / `time_to`로 원하는 시간대만 감시한다.
- `min_seats`: 이 좌석 수 이상 남았을 때만 알린다 (2명이서 가면 `2`).
- `to`는 문자열 대신 `["a@x.com", "b@y.com"]` 배열도 된다.
- 지난 날짜는 자동으로 건너뛴다.

## 4. 동작 확인

```sh
/usr/bin/python3 watch.py show            # 지금 배차 상태를 그대로 출력
/usr/bin/python3 watch.py test-mail       # 메일 설정만 점검
/usr/bin/python3 watch.py check --dry-run # 메일 없이 판정 로직만 확인
```

## 5. 10분마다 자동 실행 등록

```sh
cp com.iyunhui.txbus-watch.plist ~/Library/LaunchAgents/
launchctl load  ~/Library/LaunchAgents/com.iyunhui.txbus-watch.plist
```

해제:

```sh
launchctl unload ~/Library/LaunchAgents/com.iyunhui.txbus-watch.plist
rm ~/Library/LaunchAgents/com.iyunhui.txbus-watch.plist
```

로그: `tail -f ~/txbus-watch/watch.log`

## 알림 규칙

- **첫 실행**은 알림을 보내지 않고 현재 상태만 저장한다. 이미 여유 있는 편까지 전부 메일이 오는 걸 막기 위해서다.
- 그 다음부터는 **"좌석 없음 → 좌석 있음"으로 바뀌는 순간에만** 1회 보낸다. 좌석이 계속 남아 있어도 다시 보내지 않는다.
- 다시 매진됐다가 또 풀리면 그때 다시 보낸다.
- 메일 발송에 실패하면 상태를 되돌려서 다음 조회(10분 뒤)에 재시도한다.
- 상태는 `state.json`에 저장된다. 처음부터 다시 하려면 이 파일을 지운다.

## 6. 맥이 꺼져 있어도 돌아가게: GitHub Actions

launchd는 맥이 켜져 있을 때만 돈다. 맥을 꺼둬도 감시하려면 GitHub Actions(무료, 상시 실행)로 옮긴다.

**전제**: 라우트 정보(`config.ci.json`)는 비밀이 아니라 그대로 커밋한다. Gmail 계정/비밀번호는
절대 커밋하지 않고, GitHub 저장소의 **암호화된 Secrets**로만 넘긴다 — 저장소를 public으로 둬도
비밀번호는 노출되지 않는다. (private 저장소는 무료 한도가 월 2,000분인데, 10분 간격이면
한 달에 한도에 근접할 수 있어 이 리포는 **public**을 권장한다. 코드/노선 정보 자체는 민감하지
않다.)

1. github.com에서 새 저장소 생성 (예: `txbus-watch`, Public 권장, README 없이 빈 저장소로)
2. 이 폴더를 그 저장소에 push (터미널에서 직접 실행, 비밀번호는 여기 안 들어간다):
   ```sh
   cd ~/txbus-watch
   git init -b main   # 이미 git 저장소면 생략
   git add .
   git commit -m "시외버스 좌석 감시"
   git remote add origin git@github.com:<본인계정>/txbus-watch.git
   git push -u origin main
   ```
3. 저장소 페이지 → **Settings → Secrets and variables → Actions → New repository secret**
   에서 아래 3개를 등록 (`gmail_password.rtf`에 적어둔 값 그대로 복사해서 넣으면 된다):
   - `GMAIL_USER` — 보내는/받는 Gmail 주소
   - `GMAIL_APP_PASSWORD` — 16자리 앱 비밀번호
   - `GMAIL_TO` — 받을 주소 (여러 명이면 쉼표로 구분)
4. **Actions** 탭 → 워크플로 선택 → **Run workflow**로 1회 수동 실행해서 정상 동작 확인
5. 이후 10분마다 자동 실행된다. 로그는 Actions 탭에서 각 실행 기록으로 확인 가능.

노선/날짜/시간대를 바꾸려면 `config.ci.json`을 수정해서 다시 push하면 된다.

**로컬 launchd와 중복 알림 주의**: GitHub Actions가 정상 동작하는 걸 확인했으면, 같은 알림이
두 번 오지 않도록 맥의 launchd는 꺼두는 걸 권장한다.
```sh
launchctl unload ~/Library/LaunchAgents/com.iyunhui.txbus-watch.plist
```

## 알아둘 점

- launchd는 **Mac이 깨어 있을 때만** 실행된다. 잠자기 중에는 건너뛰고, 깨어나면 바로 한 번 실행된다.
  밤새 감시하려면 시스템 설정 → 배터리/잠자기에서 잠자기를 꺼두거나 전원을 연결해 둔다.
- 10분 주기는 사이트에 부담이 없는 수준이다. 더 짧게 줄이지 않는 편이 좋다.
- 이 도구는 **알림만** 보낸다. 예매는 직접 사이트에서 해야 한다.
