"""데이터 로더: Census NAICS xlsx(표준 라이브러리만 사용), Index File, 국가 CSV."""
from __future__ import annotations

import csv
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


class CountMismatch(Exception):
    """기대 개수와 실제 개수가 다를 때."""


# ---------------------------------------------------------------- xlsx 읽기
def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_sheet_rows(path: str | Path, sheet_index: int = 1) -> list[list[str | None]]:
    """첫 번째(기본) 시트를 행 목록으로 읽습니다. strict/transitional OOXML 모두 지원."""
    with zipfile.ZipFile(path) as z:
        wb_root = ET.fromstring(z.read("xl/workbook.xml"))
        ns = wb_root.tag.split("}")[0] + "}"
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sroot.findall(ns + "si"):
                shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
        sheet_name = f"xl/worksheets/sheet{sheet_index}.xml"
        sheet = ET.fromstring(z.read(sheet_name))
        rows: list[list[str | None]] = []
        for row in sheet.iter(ns + "row"):
            cells: dict[int, str | None] = {}
            for c in row.findall(ns + "c"):
                idx = _col_index(c.get("r"))
                t = c.get("t")
                if t == "inlineStr":
                    is_ = c.find(ns + "is")
                    val = "".join(x.text or "" for x in is_.iter(ns + "t")) if is_ is not None else None
                else:
                    v = c.find(ns + "v")
                    if v is None or v.text is None:
                        val = None
                    elif t == "s":
                        val = shared[int(v.text)]
                    else:
                        val = v.text
                cells[idx] = val
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(i) for i in range(width)])
            else:
                rows.append([])
        return rows


# ---------------------------------------------------------------- NAICS
@dataclass
class Naics:
    groups4: dict[str, str] = field(default_factory=dict)      # 4자리 코드 -> 제목
    items5: dict[str, str] = field(default_factory=dict)       # 5자리 코드 -> 제목
    children: dict[str, list[str]] = field(default_factory=dict)  # 4자리 -> 5자리 목록(코드순)
    titles_ko: dict[str, str] = field(default_factory=dict)    # 선택: naics_ko.csv


def load_naics(path: str | Path) -> Naics:
    rows = read_sheet_rows(path)
    n = Naics()
    for r in rows[1:]:
        if len(r) < 3 or not r[1] or not r[2]:
            continue
        code = str(r[1]).strip()
        title = str(r[2]).strip()
        if not code.isdigit():  # 31-33, 44-45, 48-49 같은 범위 코드 제외
            continue
        if len(code) == 4:
            n.groups4[code] = title
        elif len(code) == 5:
            n.items5[code] = title
    children: dict[str, list[str]] = defaultdict(list)
    for code in sorted(n.items5):
        children[code[:4]].append(code)
    n.children = dict(children)
    return n


def load_naics_ko(path: str | Path | None, naics: Naics) -> None:
    """선택 파일 naics_ko.csv (code,title_ko). 없으면 무시."""
    if not path or not Path(path).exists():
        return
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = (row.get("code") or "").strip()
            ko = (row.get("title_ko") or "").strip()
            if code and ko:
                naics.titles_ko[code] = ko


def load_index(path: str | Path) -> dict[str, list[str]]:
    """Index File -> {5자리 코드: [색인 용어...]}. '******' 참조 행은 제외."""
    rows = read_sheet_rows(path)
    out: dict[str, list[str]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for r in rows[1:]:
        if len(r) < 2 or not r[0] or not r[1]:
            continue
        code = str(r[0]).strip()
        if not (code.isdigit() and len(code) == 6):
            continue
        term = re.sub(r"\s+", " ", str(r[1])).strip()
        key = (code[:5], term.lower())
        if key in seen:
            continue
        seen.add(key)
        out[code[:5]].append(term)
    return dict(out)


# ---------------------------------------------------------------- 국가
@dataclass
class Country:
    alpha2: str
    name_en: str
    name_ko: str
    region: str = ""  # prompts.yaml continent_options 의 id. 여러 지역에 걸치면 ';' 로 구분(첫 번째가 우선)

    @property
    def regions(self) -> list[str]:
        return [r.strip() for r in self.region.split(";") if r.strip()]


def load_countries(path: str | Path) -> list[Country]:
    with open(path, encoding="utf-8-sig", newline="") as f:  # BOM 이 있어도 없어도 읽음
        return [
            Country(
                r["alpha2"].strip().upper(), r["name_en"].strip(), (r.get("name_ko") or "").strip(),
                (r.get("region") or "").strip(),
            )
            for r in csv.DictReader(f)
        ]


def validate_regions(countries: list[Country], region_ids: list[str], allow_mismatch: bool = False) -> list[str]:
    """모든 국가가 정의된 지역 하나에 속하는지 확인합니다. region_ids 가 비어 있으면(대륙 질문 없음) 건너뜁니다."""
    if not region_ids:
        return []
    ids = set(region_ids)
    bad = [c.alpha2 for c in countries if not c.regions or not set(c.regions) <= ids]
    empty = sorted(ids - {r for c in countries for r in c.regions})
    problems = []
    if bad:
        problems.append(f"지역이 없거나 정의되지 않은 국가 {len(bad)}개: {bad[:10]}")
    if empty:
        problems.append(f"소속 국가가 없는 지역: {empty}")
    if problems and not allow_mismatch:
        raise CountMismatch("; ".join(problems))
    return problems


# ---------------------------------------------------------------- 검증
def validate_counts(naics: Naics, countries: list[Country], expected: dict, allow_mismatch: bool = False) -> list[str]:
    actual = {
        "naics4": len(naics.groups4),
        "naics5": len(naics.items5),
        "countries": len(countries),
    }
    problems = [
        f"{k}: 기대 {expected[k]}, 실제 {actual[k]}" for k in actual if k in expected and actual[k] != expected[k]
    ]
    codes = [c.alpha2 for c in countries]
    if len(set(codes)) != len(codes):
        problems.append("countries: alpha2 중복이 있습니다")
    orphan = [c for c in naics.items5 if c[:4] not in naics.groups4]
    if orphan:
        problems.append(f"상위 4자리가 없는 5자리 코드 {len(orphan)}개: {orphan[:5]}")
    if problems and not allow_mismatch:
        raise CountMismatch("; ".join(problems))
    return problems
