# 영업자료 PDF → 고객 업종(NAICS)·판매 국가 → ICP

영업자료 PDF(회사소개서, 카탈로그 등)를 페이지 단위로 의사결정 모델(gemini-decision-1)에 보내
"고객 업종(NAICS 5자리)"과 "판매/수출 국가"를 확률로 받고, 문서 단위 후보로 집계합니다.
그 결과로 두 방식의 ICP를 만들어 비교합니다.

- **A안**: NAICS 코드가 많이 몰린 쪽(상위 5개)만 Google Document AI로 OCR하고, NAICS 후보와 OCR 텍스트를 프롬프트와 함께 LLM에 넣어 ICP 생성
- **B안**: PDF를 통째로 같은 프롬프트와 함께 LLM에 넣어 ICP 생성
- **비교**: A안과 B안이 만든 ICP를 항목 단위로 비교(`compare`)

> 이 코드는 노트북 초기화로 원본이 사라진 뒤, 대화에서 공유된 요약 설명과 설계 결정을 바탕으로
> **새로 재구성한 것**입니다. 원본과 같지 않으며, 아래 "확인이 필요한 가정"을 먼저 점검하세요.

## 흐름

```
PDF → 1쪽씩 분할 → (NAICS 4자리 그룹별 질문 308개 + 국가 1개 + 대륙 1개)를 한 호출에 전송
    → 쪽별 확률(__pages/{n}.json) → 문서 집계(__result.json/csv)
    → ocr: __result.csv 에서 NAICS 코드가 많이 몰린 쪽 상위 5개 → Google Document AI → __ocr.txt
    → icp --mode a : NAICS 후보 + OCR 텍스트 + 프롬프트 → LLM → __icp_a.json   (A안)
    → icp --mode b : PDF 통째로 + 같은 프롬프트 → LLM → __icp_b.json            (B안)
    → compare      : A안과 B안 ICP 비교 → __compare.md / __compare.json
```

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env          # OPENROUTER_API_KEY 입력 (.env 는 커밋 금지)

python -m app estimate "samples/pdf/기업소개/BIOCERA_Company Introduction.pdf"          # API 호출 없이 토큰/비용 추정
python -m app run "samples/pdf/기업소개/BIOCERA_Company Introduction.pdf" --dry-run     # 요청 규모만 확인
python -m app run "samples/pdf/기업소개/BIOCERA_Company Introduction.pdf" --resume --concurrency 2
python -m app ocr "samples/pdf/기업소개/BIOCERA_Company Introduction.pdf" --dry-run     # OCR 대상 쪽과 예상 비용만 확인(호출 없음)
python -m app ocr "samples/pdf/기업소개/BIOCERA_Company Introduction.pdf"               # Document AI OCR (run 을 먼저 실행)
python -m app icp "samples/pdf/기업소개/BIOCERA_Company Introduction.pdf" --mode a      # A안 ICP (run, ocr 먼저)
python -m app icp "samples/pdf/기업소개/BIOCERA_Company Introduction.pdf" --mode b      # B안 ICP (PDF 통째로)
python -m app compare "samples/pdf/기업소개/BIOCERA_Company Introduction.pdf"           # A안/B안 비교
python -m app pipeline "samples/pdf/기업소개/BIOCERA_Company Introduction.pdf" --dry-run   # run, ocr 예상만 출력(호출 없음)
python -m app pipeline "samples/pdf/기업소개/BIOCERA_Company Introduction.pdf"             # run -> ocr -> icp A안을 한 번에
python -m pytest -q
```

결과는 `output/<PDF명>/` 아래에 저장됩니다
(`pages/{n}.json`, `__result.json`, `__result.csv`, `__run_log.jsonl`, `__ocr.txt`, `__ocr.json`, `__icp_a.json`, `__icp_b.json`, `__compare.json`, `__compare.md`).
`__result.csv`는 `kind,code,name,score,evidence_page` 열이며 순서는 NAICS(`naics`), 국가(`country`), 지역(`continent`) 순입니다.
각 `continent` 행 바로 아래에 그 지역에 속한 국가가 전부 `continent_country` 행으로 나오고, 개별 국가로도 집계된 국가는 점수가 앞쪽에 채워집니다(나머지는 점수 빈칸).
지역은 대륙을 동서남북으로 나눈 15개(`config/prompts.yaml`의 `continent_options`)이고, 국가의 소속은 `data/countries.csv`의 `region` 열입니다(초안, 검수 필요).
러시아처럼 여러 지역에 걸친 국가는 `region` 열에 `;`로 모두 적고(첫 번째가 우선), 결과에는 점수가 가장 높은 지역 아래에 한 번만 나옵니다.
`countries.csv`와 `__result.csv`는 엑셀에서 한글이 깨지지 않도록 BOM 포함 UTF-8이며, 읽을 때는 BOM 유무와 상관없이 처리합니다.
`--resume`은 PDF sha256과 질문 sha가 모두 같은 쪽 결과만 재사용하므로, 프롬프트나 Index 설정을 바꾸면 자동 무효화됩니다.

## 모듈

| 파일 | 역할 |
|---|---|
| `app/data.py` | xlsx(표준 라이브러리만), NAICS 개수 검증(308/689/195, 불일치 시 `CountMismatch`), Index File, 국가 CSV |
| `app/questions.py` | 질문 생성, none 강제, 질문/옵션 수·ID 규칙 사전 검증, 질문 sha, Index 설명 상한 |
| `app/backends.py` | Decisions 요청 빌더와 응답 파서(OpenRouter 공개 문서 기준, alpha). 사양이 바뀌면 이 파일만 수정 |
| `app/client.py` | 429/408/409/5xx·네트워크 오류만 지수 백오프 재시도, 안전 필터 400은 blocked, 에러 메시지에서 PDF 내용 제거 |
| `app/aggregate.py` | 쪽 요약(none 제외), 문서 집계(NAICS 쪽 최댓값 ≥ 0.3, 국가/지역 상대 25% + 절대 0.1 후 쪽 간 최댓값) |
| `app/select_pages.py` | ICP 입력 페이지 선택 규칙 |
| `app/icp.py` | A안/B안 ICP 입력 조립(NAICS 후보, OCR 텍스트, PDF), LLM 호출, JSON 파싱, 비용 계산 |
| `app/compare.py` | A안/B안 ICP 비교(항목 짝짓기, 규칙 점검, 나란히 보기 markdown). LLM 호출 없음 |
| `app/ocr.py` | OCR 대상 쪽 선택(결과 CSV 기준), Google Document AI 호출, 쪽별 텍스트 추출과 합치기 |
| `app/cli.py` | `run`, `estimate`, `select`, `icp`, `ocr`, `compare`, `pipeline` |

## 페이지 선택 규칙 (select)

> 참고용입니다. ICP 생성은 이제 이 규칙을 쓰지 않습니다(A안은 `ocr` 이 고른 쪽의 텍스트, B안은 PDF 전체).

`evidence_page`는 근거 인용이 아니라 "그 코드 확률이 가장 높게 나온 페이지"입니다.

1. 전체가 5쪽 이하이면 전부 사용
2. 강한 신호: score ≥ 0.7 인 코드가 있는 쪽(최고 점수순)
3. 폭넓은 커버: 쪽별 score 합 누적 80%까지. 2와 3의 합집합은 최대 6쪽
4. 앞쪽 2쪽(제품 정의)과 국가 1순위의 최고 확률 쪽을 추가

샘플 4건 결과(`tests/fixtures`) 기준: Hasung [1,2,3], Chaesy [1,2,6,11], BIOCERA [1,2,5,9,11],
Cheongcho [1,2,3,6,9,10,11,12,13](14쪽 중 9쪽, 신호가 흩어진 문서라 절감이 작음). 임계값은 모두 placeholder입니다.

## OCR (ocr)

`run` 이 만든 `__result.csv` 를 읽어, 이미지 기반 PDF처럼 텍스트가 없는 문서의 핵심 쪽만 Google Document AI 로 텍스트를 추출합니다.

```bash
python -m app ocr "<pdf>" --dry-run    # 선택된 쪽과 예상 비용만 출력, 호출 없음
python -m app ocr "<pdf>"              # 실제 OCR
python -m app ocr "<pdf>" --top 3      # 사용할 쪽 수 변경(기본 5, settings.ocr.top_pages)
python -m app ocr "<pdf>" --out <폴더> # run 에 --out 을 썼다면 같은 값을 지정
```

**쪽 선택 규칙** (결과 CSV 의 `kind=naics` 행만 사용)
1. `evidence_page` 별 NAICS 코드 개수가 많은 쪽 우선
2. 개수가 같으면 그 쪽 코드들의 score 합이 큰 쪽, 그래도 같으면 쪽 번호가 작은 쪽
3. 상위 `top_pages`(5)개. NAICS 가 나온 쪽이 그보다 적으면 있는 만큼만 사용

**준비**: `.env` 에 `GOOGLE_APPLICATION_CREDENTIALS`(서비스 계정 키 경로, 또는 `gcloud auth application-default login` 후 비움),
`DOCUMENTAI_PROJECT_ID`, `DOCUMENTAI_PROCESSOR_ID`(OCR 프로세서), `DOCUMENTAI_LOCATION`(프로세서 리전, 기본 `us`)을 채웁니다.
`.env.example` 은 `.gitignore` 의 `.env.*` 규칙에 걸려 있으니 값 이름은 이 문서를 참고하세요. 키 파일은 저장소 밖이나 `.secrets/` 에 두고 커밋하지 마세요.

**동작과 출력**
- 선택한 쪽만 원본에서 뽑아 PDF 하나로 합쳐 한 번에 보냅니다. 온라인 처리 한도(요청당 15쪽, 40MB)를 넘으면 쪽별 요청으로 나눕니다.
- `__ocr.txt`: 쪽 번호 오름차순, `===== Page N =====` 구분선으로 합친 텍스트 하나
- `__ocr.json`: 선택 근거(순위, 코드 수, score 합, 코드 목록), 요청 횟수, 글자 수, 쪽별 텍스트
- 터미널에 `[선택]`(순위), `[예상]`(쪽수와 비용), `[실제]`(소요 시간, 요청 횟수, 쪽별 글자 수)가 나오고, 텍스트가 빈 쪽은 경고합니다.
- 자격증명 환경 변수가 비어 있으면 호출 전에 어떤 변수가 없는지 오류로 알려 줍니다.

**주의**: 단가(`ocr.price_per_1000_pages: 1.5`)는 2차 출처 기준이라 공식 가격 페이지로 재확인이 필요합니다. 선택은 `__result.csv` 열 구성(`kind,code,name,score,evidence_page`)에 의존합니다.
테스트는 `tests/conftest.py` 가 `.env` 와 키를 격리해서 실제 API 를 호출하지 않습니다.

## ICP 생성과 A안/B안 비교 (icp, compare)

같은 문서로 두 방식의 ICP 를 만들어 비교하는 실험입니다. 두 안은 **모델(`icp.model`), system 프롬프트, temperature 가 같고 입력만 다릅니다.**

| | A안 | B안 |
|---|---|---|
| 입력 | `naics_candidates`(결과 CSV 의 NAICS 전체, 코드와 업종명만, 점수 순) + `document_text`(`__ocr.txt`) | PDF 전체 첨부, `naics_candidates` 는 `none provided` |
| PDF 첨부 | 없음 | 원본 바이트 그대로 |
| 선행 단계 | `run`, `ocr` | 없음 |
| 결과 | `__icp_a.json` | `__icp_b.json` |

```bash
python -m app icp "<pdf>" --mode a      # run, ocr 를 먼저 실행해야 함(__result.csv, __ocr.txt)
python -m app icp "<pdf>" --mode b
python -m app icp "<pdf>" --mode both   # 둘 다
python -m app compare "<pdf>"           # __compare.md(나란히 보기), __compare.json
```

- **B안은 모델이 PDF를 직접 읽도록 고정**합니다(`icp.pdf_engine: native`, OpenRouter `file-parser` 플러그인). 지정하지 않으면 모델이 네이티브를 지원하지 않을 때 OpenRouter가 조용히 `mistral-ocr`($2/1,000쪽)로 바꿔서 "모델이 PDF를 읽는다"는 B안의 정의가 달라지기 때문입니다. 네이티브 PDF는 입력 토큰으로 과금되고, 모델이 지원하지 않으면 400으로 실패합니다. A안은 PDF를 보내지 않으므로 영향이 없습니다.
- 프롬프트는 `config/prompts.yaml`(`icp_system` 공통, `icp_user` A안, `icp_user_b` B안), 출력 형식은 `{"icp": {industry_fit, expand_fit, role_fit, disqualify, contact_fit}}` 입니다.
- 터미널에 `[입력]`(후보 수, OCR 글자 수, 쪽 수 등)과 `[실제]`(소요 시간, 토큰, 비용)가 나옵니다. 비용은 응답의 `usage.cost` 를 우선 쓰고 없으면 `settings.icp.pricing` 으로 추정합니다.
- `__icp_*.json` 에는 사용한 입력 요약, usage, 비용, 파싱된 `icp`, 스키마 경고, 파싱 실패 시 `raw_text` 가 들어갑니다.
- **`compare` 는 LLM 을 호출하지 않습니다.** 필드별로 두 안의 항목을 짝지어 `일치`(토큰 순서까지 같음, 대소문자와 복수형 무시), `유사`(토큰 Jaccard ≥ `--threshold`, 기본 0.6), `A만`, `B만` 으로 나누고,
  프롬프트 규칙(최소 개수, industry_fit/expand_fit 중복, contact_fit 직급어와 2단어 초과, 항목 8단어 초과)을 점검합니다. 의미는 같고 표현이 크게 다른 항목은 짝지어지지 않으니 `__compare.md` 로 직접 확인하세요.

## 확인이 필요한 가정 (원본에서 복원하지 못한 부분)

1. **Decisions API 형식**: OpenRouter 공개 문서(`POST /api/alpha/decisions`, `state`에 file 파트, `questions`는 질문 ID 키 객체, 응답은 `answers[id].probabilities`)에 맞췄고, 사용자가 A안 파이프라인을 1회 실행해 문제 없음을 확인했습니다(문서 종류별 품질은 미확인). alpha라 형식이 바뀔 수 있음. `backend: google`의 decisions 호출은 미구현(`NotImplementedError`).
2. **모델 ID**: `openrouter.model`(google/gemini-decision-1)은 실존 확인됨. `icp.model`은 `anthropic/claude-haiku-5.5`(OpenRouter 모델 목록에서 ID와 PDF 입력 지원 확인, A안 실행은 사용자가 1회 확인, B안 실행은 미확인).
3. **API 제약값**(`limits`: 질문 수, 옵션 수, ID 규칙)은 placeholder. **단가**(`pricing`, 입력 $0.10/1M, 출력 0)는 Google AI Studio 문서 기준이며 실제 청구액은 응답의 `usage.cost`를 씁니다.
4. **프롬프트 문구**(`config/prompts.yaml`)는 새로 작성. 원본 문구와 다름.
5. **국가 한글명**(`data/countries.csv`)은 기억 기반이라 검수 필요. 195개국 = UN 193 + 바티칸 + 팔레스타인.
6. 원본의 `src/test_decisions_verN.py`(OCR 게이트 실험)와 기존 `output/` 쪽별 JSON은 복원되지 않음.
7. **Document AI(ocr)**: 사용자가 A안을 1회 실행해 문제 없음을 확인했습니다. 한도(15쪽/40MB)와 단가는 문서와 검색 요약 기준이고, 문서 종류별 텍스트 품질과 BMP 밖 문자의 구간 위치는 계속 눈으로 확인하세요.
8. **ICP(icp)**: A안(OCR 텍스트 + NAICS 후보) 실행은 사용자가 1회 확인했습니다. PDF 통째로 입력(B안)과 큰 PDF의 요청 크기는 아직 확인하지 못했고, 비교 기준(유사도 0.6 등)은 초안입니다.

## 비용 주의

질문 310개(옵션 합계 약 900개)를 쪽마다 보내므로 입력이 큽니다. `estimate` 기준 쪽당 입력 약 58k 토큰입니다(글자 수/3.5 추정이라 한국어 지시문은 실제 토큰이 더 많을 수 있음).
Index 설명(`index.max_chars: 160`, `max_terms: 6`)이 큰 비중을 차지합니다. `index.enabled: false`로 끄거나 상한을 낮추세요.
Index 설명은 문서를 모르는 상태에서 짧은 용어를 우선 고르므로, 정수기처럼 특정 용어가 중요하면
`index.priority_terms: ["water purification"]` 처럼 키워드를 추가하세요(33331에서 정수기 용어가 먼저 채워짐).

## 알려진 한계

- 의사결정 모델은 옵션 제목과 문서 문구의 근접도로 확률을 내므로, 옵션에 없는 업종(예: 정수기 제조사)은 낮게 나옵니다. B2B 부품 판매사는 최종 용도 업종과 직접 구매자 업종이 다를 수 있어, ICP 단계 프롬프트에 직접 구매자 기준 지침이 들어 있습니다.
- 국가 결과는 단위·약어 표기(예: 반지 사이즈 "KR 8-20")에도 반응할 수 있어, 점수가 낮거나 평평하면 ICP 힌트에 `reliability: LOW`로 표시됩니다.
- **국가 추출은 정확도가 낮은 편**이라 추후 개선 예정입니다. 그래서 OCR 쪽 선택(NAICS 코드 개수)과 ICP 입력(NAICS 후보, OCR 텍스트)은 국가 정보를 쓰지 않고, ICP 프롬프트도 국가와 지역을 출력하지 않습니다.
- 쪽 간 집계가 최댓값이라 여러 쪽에 약하게 퍼진 신호는 낮게 남습니다.

## 한 번에 실행 (pipeline)

`run` → `ocr` → `icp --mode a` 를 순서대로 실행하고, 마지막에 단계별 소요 시간과 비용 합계를 출력합니다.

```bash
python -m app pipeline "<pdf>"                 # 기본: A안 전체 흐름
python -m app pipeline "<pdf>" --resume        # run 에서 기존 쪽 결과 재사용(성공한 쪽은 재호출 안 함)
python -m app pipeline "<pdf>" --top 3         # OCR 쪽 수 변경
python -m app pipeline "<pdf>" --mode both     # A안 뒤에 B안(PDF 통째로)도 실행
python -m app pipeline "<pdf>" --mode b        # B안만(run, ocr 생략)
python -m app pipeline "<pdf>" --dry-run       # API 호출 없이 run/ocr 예상만 출력(icp 는 호출하지 않음)
```

- 앞 단계가 실패하면 즉시 중단하고 뒤 단계는 실행하지 않습니다(비용 낭비 방지). `run` 이 일부 쪽에서 실패했다면 `--resume` 으로 다시 실행하세요.
- 안전 필터로 차단된 쪽이 있으면 경고만 하고 계속 진행합니다.
- 요약의 `run` 비용은 이번 실행에서 새로 호출한 쪽의 `usage.cost` 합계(재사용한 쪽은 0)입니다. `ocr` 비용은 `settings.ocr.price_per_1000_pages` 로 계산한 추정치이며 `__ocr.json` 의 `ocr.cost_usd_est` 에도 저장됩니다.
- `--dry-run` 에서 `__result.csv` 가 아직 없으면 OCR 대상 쪽은 고르지 못하고 최대 비용만 표시합니다. 합산 예상 비용은 표시하지 않고 단계별 예상만 보여 줍니다.
- 이 명령은 실제 API 호출을 하므로 아직 실호출로 검증하지 않았습니다(단계 연결은 가짜 함수 테스트로만 확인).
