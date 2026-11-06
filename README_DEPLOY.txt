리버룸 — 인터넷 배포용 간편 묶음

[현재 상태]
이 ZIP은 업로드할 파일입니다. 아직 인터넷에 배포되지 않았습니다.
다운로드 링크는 친구의 게임 참가 링크가 아닙니다.
사이트 운영 계정 생성과 로그인, 배포 실행은 사용자 본인이 해야 합니다.
비밀번호나 로그인 인증 코드는 채팅에 보내지 마세요.

[이번 묶음이 더 간단한 이유]
카드 규칙과 웹 화면을 server.py 하나에 합쳤습니다.
engine.py, index.html, pygame이 따로 필요하지 않습니다.
아래 파일들을 압축을 풀어 같은 위치에 둡니다.
- server.py : 게임 전체와 웹 서버
- requirements.txt : 인터넷 호스팅 실행용 패키지
- render.yaml : 선택적으로 쓰는 자동 설정
- README_DEPLOY.txt : 이 안내

[1. GitHub 저장소 만들기]
1) 브라우저에서 https://github.com 으로 이동합니다.
2) 계정이 없으면 Sign up으로 가입하고 로그인합니다.
3) 새 저장소 만들기: https://github.com/new
4) Repository name에 river-room 입력
5) Private 선택. Add README를 켜서 저장소를 초기화합니다.
6) Create repository를 누릅니다.
7) 저장소에서 Add file → Upload files를 누릅니다.
8) ZIP을 푼 뒤 그 안의 위 4개 파일을 업로드합니다.
   ZIP 자체를 올리거나 폴더를 한 겹 더 만들지 마세요.
9) Commit changes를 눌러 저장합니다.
10) 저장소 파일 목록에 server.py와 requirements.txt가 바로 보이면 됩니다.

[2. Render에서 인터넷 서버 만들기]
1) https://dashboard.render.com 으로 이동합니다.
2) 계정을 만들고 로그인합니다. GitHub 로그인 방식도 사용할 수 있습니다.
3) New → Web Service 선택. Static Site가 아닙니다.
4) GitHub 저장소를 연결합니다.
   계정 연결/저장소 권한 요청이 나오면 위 river-room 저장소 접근을 허용합니다.
5) river-room 저장소를 선택합니다.
6) 다음 값을 입력합니다.

Name: river-room-holdem (중복이면 이름 끝에 숫자를 붙이세요.)
Language / Runtime: Python 3
Root Directory: 비워 둡니다.
Build Command: pip install -r requirements.txt
Start Command: gunicorn --workers 1 --threads 8 --timeout 30 --bind 0.0.0.0:$PORT server:app
Health Check Path: /healthz
Instance Type / Compute: Free (취미용 시험 기본값)

7) 화면에 표시되는 요금제/사용량 조건을 확인하고 Deploy Web Service를 누릅니다.
8) 빌드와 실행이 끝나 Live가 될 때까지 기다립니다.
9) Render가 발급한 https://...onrender.com 형식의 실제 서비스 주소를 엽니다.
   이것은 주소 형태 설명입니다. 이 문서가 실제 주소를 발급한 것은 아닙니다.

선택 사항: Render Blueprint 방식에서는 render.yaml을 읽어 설정할 수 있습니다.
초보자에게는 위 Web Service 수동 입력 방법을 권장합니다.

[3. 친구와 플레이]
1) 나도 반드시 Render가 발급한 HTTPS 주소로 접속합니다.
2) 닉네임 입력 → 방 만들고 친구 초대하기
3) 초대 링크 복사 또는 공유로 친구 한 명에게 전체 링크 전송
4) 친구가 브라우저에서 열고 닉네임 입력 → 참가
5) 두 사람 모두 게임 화면을 켜둔 상태로 플레이

기존 127.0.0.1 또는 192.168... 로 시작하는 초대 링크는 보내지 마세요.
주소의 #room=...&invite=... 부분까지 전부 보내야 합니다.
친구는 Pydroid나 파이썬을 설치할 필요가 없습니다.
외부 서버가 정상 실행 중이면 내 탭의 Pydroid를 켜둘 필요도 없습니다.

[중요한 제한]
- 2인용, 가상 칩 1,000개, 블라인드 10/20입니다. 결제/환전 기능 없음.
- 방과 칩은 서버 메모리에만 저장합니다. 재시작/재배포/절전 시 사라질 수 있습니다.
- 서버 인스턴스 1개, gunicorn worker 1개를 유지해야 합니다.
- Free Web Service는 외부 요청이 15분간 없으면 절전하며 다음 접속 시 다시 준비 시간이 생길 수 있습니다.
- 무료 서비스에는 사용량 한도가 있습니다. 실제 서비스 화면에서 조건을 확인하세요.
- 초대 링크는 입장 권한을 포함합니다. 신뢰하는 친구 한 명에게만 보내세요.
- 서버 소유자는 서버 메모리에 접근할 수 있습니다. 상용 보안감사나 대규모 부하 검증을 거치지 않은 시제품입니다.
- HTTPS 배포 시 암호화는 호스팅 서비스에서 처리합니다.
- 같은 기기와 같은 브라우저에 저장된 참가 정보로 재접속을 시도합니다.
- 시크릿 모드 종료나 사이트 데이터 삭제 후에는 기존 자리를 되찾지 못할 수 있습니다.

[잘 안 될 때]
- ModuleNotFoundError: server → Root Directory와 server.py 위치를 확인하세요.
- No module named gunicorn → Build Command와 requirements.txt를 확인하세요.
- 127.0.0.1 링크가 복사됨 → Render 웹 주소에서 새 방을 만드세요.
- 페이지가 한동안 준비 중 → 무료 서버가 절전에서 깨어나는 중일 수 있습니다.
- 재배포 후 기존 방이 사라짐 → 현재 버전은 메모리 저장 방식입니다. 새 방을 만드세요.

[검증 범위]
카드/베팅 규칙과 무작위 2,000판, API, 참가 토큰, 비공개 카드, 재접속 상태를 오프라인 테스트했습니다.
실제 인터넷 서버 생성/공개 주소 발급/태블릿 두 대 사이의 인터넷 대전은 아직 수행하지 않았습니다.

[공식 안내 — 2026-11-06 확인]
https://render.com/docs/deploy-flask
https://render.com/docs/free
