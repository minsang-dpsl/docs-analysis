"""질문 생성기.

- 4자리 NAICS 그룹마다 "하위 5자리 + none" 선택 질문 1개
- 국가 선택 질문 1개 (국가 + none)
- API 제약(질문 수, 옵션 수, ID 규칙, none 강제)을 요청 전에 검증
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .data import Country, Naics

NONE_ID = "none"
COUNTRY_QID = "country"
NAICS_QID_PREFIX = "naics_"

_STRIP_SUFFIXES = (" manufacturing", " merchant wholesalers", " merchant wholesaler")


@dataclass
class Option:
    id: str
    label: str
    description: str | None = None


@dataclass
class Question:
    id: str
    text: str
    options: list[Option] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": "choice",
            "text": self.text,
            "options": [
                {k: v for k, v in (("id", o.id), ("label", o.label), ("description", o.description)) if v}
                for o in self.options
            ],
        }


class QuestionError(Exception):
    pass


# ---------------------------------------------------------------- Index 설명
def build_index_description(
    terms: list[str], max_terms: int, max_chars: int, priority_terms: list[str] | None = None
) -> str | None:
    """5자리 코드의 색인 용어에서 상한 안에서 대표 용어를 골라 한 줄 설명으로 만듭니다.

    정렬: priority_terms 에 부분일치하는 용어 먼저, 그다음 짧은 용어 순.
    한계: 문서 내용을 모르는 상태의 정적 선택이라 매칭 용어가 밀릴 수 있습니다.
    그럴 때는 settings.yaml index.priority_terms 에 키워드를 추가하세요.
    """
    if not terms or max_terms <= 0 or max_chars <= 0:
        return None
    prio = [p.lower() for p in (priority_terms or [])]
    cleaned: list[str] = []
    seen: set[str] = set()
    for t in terms:
        s = t.strip()
        low = s.lower()
        for suf in _STRIP_SUFFIXES:
            if low.endswith(suf):
                s = s[: -len(suf)].strip(" ,")
                low = s.lower()
                break
        if s and low not in seen:
            seen.add(low)
            cleaned.append(s)

    def key(s: str):
        is_prio = any(p in s.lower() for p in prio)
        return (0 if is_prio else 1, len(s), s.lower())

    picked: list[str] = []
    used = len("e.g. ")
    for s in sorted(cleaned, key=key):
        add = len(s) + 2
        if len(picked) >= max_terms or used + add > max_chars:
            continue
        picked.append(s)
        used += add
    return ("e.g. " + "; ".join(picked)) if picked else None


# ---------------------------------------------------------------- 빌드
def build_questions(
    naics: Naics,
    countries: list[Country],
    prompts: dict,
    index: dict[str, list[str]] | None = None,
    index_cfg: dict | None = None,
) -> list[Question]:
    index_cfg = index_cfg or {}
    use_index = bool(index) and index_cfg.get("enabled", True)
    none_opt = Option(NONE_ID, prompts["none_label"], prompts.get("none_description"))
    questions: list[Question] = []
    for g4 in sorted(naics.groups4):
        kids = naics.children.get(g4, [])
        if not kids:
            continue
        opts = []
        for code in kids:
            desc = None
            if use_index:
                desc = build_index_description(
                    index.get(code, []),
                    index_cfg.get("max_terms", 6),
                    index_cfg.get("max_chars", 160),
                    index_cfg.get("priority_terms"),
                )
            opts.append(Option(code, naics.items5[code], desc))
        opts.append(none_opt)
        text = prompts["naics_question"].format(group_code=g4, group_title=naics.groups4[g4])
        questions.append(Question(NAICS_QID_PREFIX + g4, text, opts))

    c_opts = [Option(c.alpha2, c.name_en, c.name_ko or None) for c in countries] + [none_opt]
    questions.append(Question(COUNTRY_QID, prompts["country_question"], c_opts))
    return questions


# ---------------------------------------------------------------- 검증/해시
def validate_questions(questions: list[Question], limits: dict) -> None:
    """API 제약을 사전 검증합니다. 위반 시 QuestionError."""
    errors: list[str] = []
    id_re = re.compile(limits["id_regex"])
    if len(questions) > limits["max_questions"]:
        errors.append(f"질문 수 {len(questions)} > 한도 {limits['max_questions']}")
    qids = [q.id for q in questions]
    if len(set(qids)) != len(qids):
        errors.append("질문 ID 중복")
    for q in questions:
        if not id_re.match(q.id):
            errors.append(f"질문 ID 규칙 위반: {q.id}")
        if len(q.options) > limits["max_options_per_question"]:
            errors.append(f"{q.id}: 옵션 {len(q.options)}개 > 한도 {limits['max_options_per_question']}")
        oids = [o.id for o in q.options]
        if len(set(oids)) != len(oids):
            errors.append(f"{q.id}: 옵션 ID 중복")
        for oid in oids:
            if not id_re.match(oid):
                errors.append(f"{q.id}: 옵션 ID 규칙 위반: {oid}")
        if not oids or oids[-1] != NONE_ID:
            errors.append(f"{q.id}: 마지막 옵션이 none 이어야 합니다")
    if errors:
        raise QuestionError("; ".join(errors[:20]))


def questions_payload(questions: list[Question]) -> list[dict]:
    return [q.to_dict() for q in questions]


def questions_sha(questions: list[Question]) -> str:
    blob = json.dumps(questions_payload(questions), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def questions_chars(questions: list[Question]) -> int:
    return len(json.dumps(questions_payload(questions), ensure_ascii=False, separators=(",", ":")))
