# 영업자료 PDF → 고객 업종(NAICS)·판매 국가 → ICP

영업자료 PDF(회사소개서, 카탈로그 등)를 페이지 단위로 의사결정 모델(gemini-decision-1)에 보내
"고객 업종(NAICS 5자리)"과 "판매/수출 국가"를 확률로 받고, 문서 단위 후보로 집계한 뒤,
선택한 페이지만 LLM에 보내 ICP를 추출합니다.

> 이 코드는 노트북 초기화로 원본이 사라진 뒤, 대화에서 공유된 요약 설명과 설계 결정을 바탕으로
> **새로 재구성한 것**입니다. 원본과 같지 않으며, 아래 "확인이 필요한 가정"을 먼저 점검하세요.

## 흐름

```
PDF → 1쪽씩 분할 → (NAICS 4자리 그룹별 질문 308개 + 국가 질문 1개)를 한 호출에 전송
    → 쪽별 확률(__pages/{n}.json) → 문서 집계(__result.json/csv)
    → select: ICP 입력 페이지 선택 → icp: 선택한 페이지 원문 + 힌트로 ICP JSON 생성
```

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env          # OPENROUTER_API_KEY 입력 (.env 는 커밋 금지)

python -m app estimate samples/BIOCERA_Company_Introduction.pdf          # API 호출 없이 토큰/비용 추정
python -m app run samples/BIOCERA_Company_Introduction.pdf --dry-run     # 요청 규모만 확인
python -m app run samples/BIOCERA_Company_Introduction.pdf --resume --concurrency 2
python -m app select samples/BIOCERA_Company_Introduction.pdf            # ICP 입력 페이지 선택
python -m app icp samples/BIOCERA_Company_Introduction.pdf               # ICP 추출
python -m pytest -q
```

결과는 `output/<output_dir_name>/<PDF명>/` 아래에 저장됩니다
(`pages/{n}.json`, `__result.json`, `__result.csv`, `__run_log.jsonl`, `__pages_selected.json`, `__icp.json`).
`--resume`은 PDF sha256과 질문 sha가 모두 같은 쪽 결과만 재사용하므로, 프롬프트나 Index 설정을 바꾸면 자동 무효화됩니다.

## 모듈

| 파일 | 역할 |
|---|---|
| `app/data.py` | xlsx(표준 라이브러리만), NAICS 개수 검증(308/689/195, 불일치 시 `CountMismatch`), Index File, 국가 CSV |
| `app/questions.py` | 질문 생성, none 강제, 질문/옵션 수·ID 규칙 사전 검증, 질문 sha, Index 설명 상한 |
| `app/backends.py` | **요청 JSON 빌더와 응답 파서(추정 형식)**. 실제 API 사양이 다르면 이 파일만 수정 |
| `app/client.py` | 429/408/409/5xx·네트워크 오류만 지수 백오프 재시도, 안전 필터 400은 blocked, 에러 메시지에서 PDF 내용 제거 |
| `app/aggregate.py` | 쪽 요약(none 제외), 문서 집계(NAICS 쪽 최댓값 ≥ 0.3, 국가 상대 25% + 절대 0.05 후 쪽 간 최댓값) |
| `app/select_pages.py` | ICP 입력 페이지 선택 규칙 |
| `app/icp.py` | 힌트 구성, ICP 프롬프트, JSON 파싱 |
| `app/cli.py` | `run`, `estimate`, `select`, `icp` |

## 페이지 선택 규칙 (select)

`evidence_page`는 근거 인용이 아니라 "그 코드 확률이 가장 높게 나온 페이지"입니다.

1. 전체가 5쪽 이하이면 전부 사용
2. 강한 신호: score ≥ 0.7 인 코드가 있는 쪽(최고 점수순)
3. 폭넓은 커버: 쪽별 score 합 누적 80%까지. 2와 3의 합집합은 최대 6쪽
4. 앞쪽 2쪽(제품 정의)과 국가 1순위의 최고 확률 쪽을 추가

샘플 4건 결과(`tests/fixtures`) 기준: Hasung [1,2,3], Chaesy [1,2,6,11], BIOCERA [1,2,5,9,11],
Cheongcho [1,2,3,6,9,10,11,12,13](14쪽 중 9쪽, 신호가 흩어진 문서라 절감이 작음). 임계값은 모두 placeholder입니다.

## 확인이 필요한 가정 (원본에서 복원하지 못한 부분)

1. **Decisions API 요청/응답 형식**: `app/backends.py`의 `ASSUMED` 표시. 실제 형식으로 교체 필요. `backend: google`의 decisions 호출은 미구현(`NotImplementedError`).
2. **모델 ID**: `settings.yaml`의 `openrouter.model`, `icp.model`은 placeholder.
3. **API 제약값**(`limits`: 질문 수, 옵션 수, ID 규칙)과 **단가**(`pricing`)는 placeholder.
4. **프롬프트 문구**(`config/prompts.yaml`)는 새로 작성. 원본 문구와 다름.
5. **국가 한글명**(`data/countries.csv`)은 기억 기반이라 검수 필요. 195개국 = UN 193 + 바티칸 + 팔레스타인.
6. 원본의 `src/test_decisions_verN.py`(OCR 게이트 실험)와 기존 `output/` 쪽별 JSON은 복원되지 않음.

## 비용 주의

질문 309개(옵션 합계 약 900개)를 쪽마다 보내므로 입력이 큽니다. `estimate` 기준 쪽당 입력 약 77k 토큰이며,
Index 설명(`index.max_chars: 160`, `max_terms: 6`)이 그중 약 26k 토큰을 차지합니다. `index.enabled: false`로 끄거나 상한을 낮추세요.
Index 설명은 문서를 모르는 상태에서 짧은 용어를 우선 고르므로, 정수기처럼 특정 용어가 중요하면
`index.priority_terms: ["water purification"]` 처럼 키워드를 추가하세요(33331에서 정수기 용어가 먼저 채워짐).

## 알려진 한계

- 의사결정 모델은 옵션 제목과 문서 문구의 근접도로 확률을 내므로, 옵션에 없는 업종(예: 정수기 제조사)은 낮게 나옵니다. B2B 부품 판매사는 최종 용도 업종과 직접 구매자 업종이 다를 수 있어, ICP 단계 프롬프트에 직접 구매자 기준 지침이 들어 있습니다.
- 국가 결과는 단위·약어 표기(예: 반지 사이즈 "KR 8-20")에도 반응할 수 있어, 점수가 낮거나 평평하면 ICP 힌트에 `reliability: LOW`로 표시됩니다.
- 쪽 간 집계가 최댓값이라 여러 쪽에 약하게 퍼진 신호는 낮게 남습니다.
