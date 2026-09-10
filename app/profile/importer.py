"""Import the owner's portfolio without treating its drafts as verified facts.

The recent cvEntry/cvEducation source files supply the provisional content library.
Older CVs and profile documents supply conflicting evidence, never priority by mtime.
Only a small known LaTeX vocabulary is read; no TeX or embedded command is executed.
Unrecognized layouts remain reviewable source warnings, not invented content.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
import unicodedata


RECENT_CVS = {"fr": "CV_Abdessamad_Jaouad.tex", "en": "CV_Abdessamad_Jaouad_EN.tex"}
PROFILE_FILES = ("profile-desc.md", "ABDESSAMAD_JAOUAD_COMPLETE_PROFILE.md")
LEGACY_FILES = (
    "CV_JAOUAD_RESUME_FR.tex", "CV_JAOUAD_RESUME.tex", "CV_JAOUAD_DataEngineer.tex",
    "new-cv.tex", "new-cv-fr.tex", "new-cv-software.tex", "new-cv-software-fr.tex",
    "abdessamad_jaouad_resume.tex", "abdessamad_jaouad_resume_visual.tex",
    "abdessamad_jaouad_cv.tex", "abdessamad_jaouad_cv_emirates.tex",
    "old-resumes-assets/CV_JAOUAD_Abdessamad_Combined.md",
    "old-resumes-assets/CV_JAOUAD_Abdessamad_Combined.tex",
    "old-resumes-assets/CV_JAOUAD_Abdessamad_FR.tex",
    "old-resumes-assets/CV_JAOUAD_DataAnalyst_DXC.tex",
)
MAX_SOURCE_BYTES = 12 * 1024 * 1024
MONTHS = {
    "january": 1, "janvier": 1, "jan": 1,
    "february": 2, "fevrier": 2, "feb": 2,
    "march": 3, "mars": 3, "mar": 3,
    "april": 4, "avril": 4, "apr": 4,
    "may": 5, "mai": 5,
    "june": 6, "juin": 6, "jun": 6,
    "july": 7, "juillet": 7, "jul": 7,
    "august": 8, "aout": 8, "aug": 8,
    "september": 9, "septembre": 9, "sept": 9, "sep": 9,
    "october": 10, "octobre": 10, "oct": 10,
    "november": 11, "novembre": 11, "nov": 11,
    "december": 12, "decembre": 12, "dec": 12,
}
TECHNOLOGIES = (
    "Python", "SQL", "Java", "Bash", "FastAPI", "Flask", "Spring Boot", "React", "Angular",
    "Kafka", "PySpark", "Spark", "Airflow", "Hadoop", "HDFS", "HBase", "Pandas",
    "PostgreSQL", "PostGIS", "MongoDB", "Redis", "MinIO", "MLflow", "Docker", "Nginx",
    "Azure", "AWS", "Git", "CI/CD", "Linux", "Pytest", "Vitest", "Power BI", "DAX",
    "Power Query", "Grafana", "scikit-learn", "XGBoost", "LightGBM", "BERT", "Transformers",
    "LangChain", "CrewAI", "Groq", "PyTorch", "TensorFlow", "Excel", "VBA", "LZ4", "Kyber512",
)


def _fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)).lower()


def _braced(text: str, start: int) -> tuple[str, int] | None:
    """Read balanced literal braces with bounded work and escaped-brace handling."""
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != "{":
        return None
    depth, index = 1, start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
        index += 1
    return None


def _calls(text: str, macro: str, argc: int) -> list[tuple[list[str], int, int]]:
    result = []
    for match in re.finditer(r"\\" + re.escape(macro) + r"\b", text):
        args, cursor = [], match.end()
        for _ in range(argc):
            item = _braced(text, cursor)
            if item is None:
                break
            arg, cursor = item
            args.append(arg)
        if len(args) == argc:
            result.append((args, match.start(), cursor))
    return result


def latex_text(value: str) -> str:
    """Readable literal text from the owner's known templates, never a TeX engine."""
    value = re.sub(r"(?m)(?<!\\)%.*$", "", value)
    for command, count, keep in (("href", 2, 1), ("textcolor", 2, 1)):
        for args, start, end in reversed(_calls(value, command, count)):
            value = value[:start] + args[keep] + value[end:]
    # Layout arguments are not candidate content.
    for command, count in (("fontsize", 2), ("vspace", 1), ("hspace", 1), ("color", 1),
                           ("begin", 1), ("end", 1)):
        for _, start, end in reversed(_calls(value, command, count)):
            value = value[:start] + " " + value[end:]
    value = re.sub(r"\\(?:textendash|textemdash)\s*(?:\{\})?", "–", value)
    value = re.sub(r"\\(?:enspace|quad|qquad|hfill|textbar)\b", " | ", value)
    value = re.sub(r"\\\\(?:\[[^\]]*\])?", "\n", value)
    value = value.replace(r"\%", "%").replace(r"\&", "&").replace(r"\_", "_")
    value = value.replace(r"\,", " ").replace(r"\;", " ")
    value = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", "", value)
    value = value.replace("{", "").replace("}", "").replace("~", " ")
    return re.sub(r"[ \t]+", " ", value).strip()


@dataclass(frozen=True)
class Source:
    path: str
    text: str
    digest: str
    extracted_at: str
    pages: tuple[str, ...] = ()

    def evidence(self, start: int = 0, end: int | None = None) -> dict:
        end = min(end if end is not None else start + 1600, len(self.text))
        if self.pages:
            offset, page_number = 0, 1
            for index, page in enumerate(self.pages):
                if start < offset + len(page) + 1:
                    page_number = index + 1
                    break
                offset += len(page) + 1
            location = f"page {page_number}"
        else:
            first = self.text.count("\n", 0, start) + 1
            last = self.text.count("\n", 0, end) + 1
            location = f"lines {first}–{last}"
        return {"path": self.path, "sha256": self.digest, "location": location,
                "excerpt": self.text[start:end], "extracted_at": self.extracted_at}

    def matching(self, pattern: str, *, context: int = 0) -> list[dict]:
        return [self.evidence(max(0, match.start() - context), min(len(self.text), match.end() + context))
                for match in re.finditer(pattern, self.text, re.I | re.M)]


def _read_source(root: Path, relative: str, extracted_at: str, warnings: list[str]) -> Source | None:
    path = root / relative
    if not path.exists():
        return None
    if not path.resolve().is_relative_to(root) or not path.is_file():
        warnings.append(f"Skipped source outside portfolio or not a regular file: {relative}")
        return None
    if path.stat().st_size > MAX_SOURCE_BYTES:
        warnings.append(f"Source exceeds import size limit: {relative}")
        return None
    try:
        raw = path.read_bytes()
        pages: tuple[str, ...] = ()
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(raw))
            if reader.is_encrypted or len(reader.pages) > 50:
                warnings.append(f"Encrypted or oversized PDF needs manual import: {relative}")
                return None
            pages = tuple(page.extract_text() or "" for page in reader.pages)
            content = "\n".join(pages)
        else:
            content = raw.decode("utf-8")
        return Source(relative, content, sha256(raw).hexdigest(), extracted_at, pages)
    except Exception as exc:
        # A damaged historical attachment cannot stop the rest of the profile.
        warnings.append(f"Could not extract {relative}: {type(exc).__name__}")
        return None


def _fact(key: str, label: str, value, sources: list[dict], category: str,
          status: str = "unconfirmed", **extra) -> dict:
    unique = {(s["path"], s["location"], s["excerpt"]): s for s in sources}
    return {"key": key, "label": label, "value": value, "status": status,
            "sources": list(unique.values()), "category": category, **extra}


def _period(text: str) -> dict | None:
    plain = _fold(latex_text(text)).replace("--", "–")
    years = re.findall(r"\b(?:19|20)\d{2}\b", plain)
    month_pattern = r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b"
    months = re.findall(month_pattern, plain)
    if not years or len(months) < 2:
        return None
    return {"start": f"{years[0]}-{MONTHS[months[0]]:02d}",
            "end": f"{years[-1]}-{MONTHS[months[1]]:02d}", "precision": "month"}


def _technologies(text: str) -> list[str]:
    return [item for item in TECHNOLOGIES
            if re.search(r"(?<!\w)" + re.escape(item) + r"(?!\w)", text, re.I)]


def _body(source: Source) -> tuple[str, int]:
    marker = r"\begin{document}"
    start = source.text.find(marker)
    return (source.text[start + len(marker):], start + len(marker)) if start >= 0 else (source.text, 0)


def _recent_content(recent: dict[str, Source], facts: list[dict], warnings: list[str]) -> None:
    experiences, education, projects = {}, {}, {}
    skill_values, skill_sources = {}, []
    for language, source in recent.items():
        body, offset = _body(source)
        entries = _calls(body, "cvEntry", 4)
        if not entries:
            warnings.append(f"No recognized experience layout in {source.path}; owner review required")
        for args, start, end in entries:
            title, dates, employer, location = map(latex_text, args)
            folded = _fold(employer)
            key = next((key for key in ("dxc", "jesa", "ocp") if key in folded), None)
            if not key:
                warnings.append(f"Unmapped experience in {source.path}: {employer[:100]}")
                continue
            block_end = body.find(r"\end{cvitems}", end)
            if block_end < 0:
                warnings.append(f"Incomplete experience content in {source.path}: {key}")
                continue
            block = body[end:block_end]
            bullets = [latex_text(b) for b in re.split(r"\\item\s+", block)[1:]]
            entry = experiences.setdefault(key, {"title": {}, "employer": employer,
                                                "location": {}, "period_label": {}, "bullets": {},
                                                "period": _period(dates), "employment_type": "internship",
                                                "alternatives": [], "sources": []})
            # Internship classification requires an explicit source marker.
            is_internship = bool(re.search(r"\b(stage|internship|intern)\b", title, re.I))
            if not is_internship:
                entry["employment_type"] = "unknown"
            for field, value in (("title", title), ("location", location),
                                 ("period_label", dates), ("bullets", bullets)):
                entry[field][language] = value
            period = _period(dates)
            if entry["period"] != period:
                entry["alternatives"].append({"field": "period", "value": period, "source_path": source.path})
            entry["sources"].append(source.evidence(offset + start, offset + block_end))
        for args, start, end in _calls(body, "cvEducation", 3):
            title, years, institution = map(latex_text, args)
            key = "master" if "master" in _fold(title) else "licence" if re.search(
                r"licence|bachelor", title, re.I) else None
            if not key:
                continue
            year_values = re.findall(r"\b(?:19|20)\d{2}\b", years)
            entry = education.setdefault(key, {"title": {}, "institution": {}, "period_label": {},
                                               "start_year": int(year_values[0]) if year_values else None,
                                               "end_year": int(year_values[-1]) if year_values else None,
                                               "award_confirmed": False, "award_date": None,
                                               "formal_equivalence": None, "alternatives": [], "sources": []})
            entry["title"][language] = title
            entry["institution"][language] = institution
            entry["period_label"][language] = years
            if year_values and entry["start_year"] != int(year_values[0]):
                entry["alternatives"].append({"field": "start_year", "value": int(year_values[0]),
                                               "source_path": source.path})
            entry["sources"].append(source.evidence(offset + start, offset + end))
        for args, start, end in _calls(body, "projecttitle", 1):
            title = latex_text(args[0]).rstrip(" :")
            folded = _fold(title)
            key = next((key for key, terms in (
                ("streaming_elt", ("elt", "pipeline")), ("data_quality", ("data quality",)),
                ("healthics", ("healthics",)), ("bert", ("bert",)),
                ("cv_matching", ("matching", "cv/job")),
            ) if any(term in folded for term in terms)), None)
            if not key:
                continue
            next_item = re.search(r"\\(?:item|end)\b", body[end:])
            block_end = end + next_item.start() if next_item else len(body)
            description = latex_text(body[end:block_end]).lstrip(" :")
            # The requested French style begins descriptions with a noun.
            if language == "fr":
                noun_prefixes = {"nettoyage": "Nettoyage", "fine-tuning": "Adaptation",
                                 "orchestration": "Orchestration", "conception": "Conception"}
                for prefix, noun in noun_prefixes.items():
                    if description.startswith(prefix):
                        description = noun + description[len(prefix):]
                        break
            entry = projects.setdefault(key, {"title": {}, "description": {}, "sources": []})
            entry["title"][language] = title
            entry["description"][language] = description
            entry["sources"].append(source.evidence(offset + start, offset + block_end))
        for args, start, end in _calls(body, "skilllabel", 1):
            label = latex_text(args[0]).rstrip(" :")
            line_end = body.find("\n", end)
            line_end = line_end if line_end >= 0 else len(body)
            value = latex_text(body[end:line_end]).lstrip(" :")
            if any(term in _fold(label) for term in ("langues", "languages", "research", "recherche",
                                                      "activities", "engagements")):
                continue
            if "certification" in _fold(label):
                facts.append(_fact(f"certifications.{language}", f"Certifications ({language.upper()})",
                                   {"language": language, "text": value, "professional_equivalence": None},
                                   [source.evidence(offset + start, offset + line_end)], "certifications"))
                continue
            skill_values.setdefault(language, []).append({"label": label, "text": value})
            skill_sources.append(source.evidence(offset + start, offset + line_end))
    for key, entry in experiences.items():
        evidence = entry.pop("sources")
        entry["technologies"] = _technologies(" ".join(" ".join(items) for items in entry["bullets"].values()))
        facts.append(_fact(f"experience.{key}", f"{entry['employer']} — internship evidence", entry, evidence,
                           "experience", "conflicting" if entry["alternatives"] else "unconfirmed",
                           employment_type=entry["employment_type"]))
    for key, entry in education.items():
        evidence = entry.pop("sources")
        facts.append(_fact(f"education.{key}", "Master's education" if key == "master" else "Licence education",
                           entry, evidence, "education", "conflicting" if entry["alternatives"] else "unconfirmed"))
    for key, entry in projects.items():
        evidence = entry.pop("sources")
        entry["technologies"] = _technologies(" ".join(entry["description"].values()))
        facts.append(_fact(f"project.{key}", next(iter(entry["title"].values())), entry, evidence, "project"))
    if skill_values:
        facts.append(_fact("skills.technical", "Technical skills in recent CVs",
                           {"content": skill_values, "technologies": _technologies(
                               " ".join(s["excerpt"] for s in skill_sources))}, skill_sources, "skills"))


def _identity(recent: dict[str, Source], profiles: list[Source], facts: list[dict]) -> str:
    selected = next(iter(recent.values()), profiles[0] if profiles else None)
    if selected is None:
        return "Profile awaiting import"
    body, offset = _body(selected)
    if selected.path.endswith(".tex"):
        header_end = body.find(r"\end{center}")
        header = body[:header_end] if header_end >= 0 else body[:1800]
    else:
        header = body[:body.find("## Professional") if "## Professional" in body else 1600]
    name_match = re.search(r"Abdessamad\s+Jaouad", header, re.I)
    name = name_match.group(0) if name_match else "Profile awaiting import"
    if name_match:
        facts.append(_fact("identity.name", "Full name", {"full": name},
                           [selected.evidence(offset + name_match.start(), offset + name_match.end())], "identity"))
    contact, sources = {}, []
    patterns = {
        "phone": r"\+\d[\d ]{8,20}\d", "email": r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        "linkedin": r"linkedin\.com/in/[A-Za-z0-9_-]+", "github": r"github\.com/[A-Za-z0-9_-]+",
    }
    for field, pattern in patterns.items():
        match = re.search(pattern, header)
        if match:
            contact[field] = match.group(0).strip()
            sources.append(selected.evidence(offset + match.start(), offset + match.end()))
    if contact:
        facts.append(_fact("identity.contact", "Contact block", contact, sources, "identity"))
    location_match = re.search(r"Casablanca,\s*(Maroc|Morocco)", header)
    if location_match:
        facts.append(_fact("identity.location", "Current residence",
                           {"city": "Casablanca", "country_code": "MA",
                            "label": {"fr": "Casablanca, Maroc", "en": "Casablanca, Morocco"}},
                           [selected.evidence(offset + location_match.start(), offset + location_match.end())],
                           "identity"))
    language_sources = []
    for source in [*recent.values(), *profiles]:
        language_sources.extend(source.matching(r"^.*(?:Arabic \(native\)|arabe maternel|\*\*Languages:).*$"))
    if language_sources:
        # Each claimed level must actually occur together in its source line.
        texts = " ".join(s["excerpt"] for s in language_sources)
        languages = []
        for code, name_en, name_fr, level_en, level_fr in (
            ("ar", "Arabic", "arabe", "native", "maternel"),
            ("fr", "French", "français", "fluent", "courant"),
            ("en", "English", "anglais", "fluent", "courant"),
        ):
            if re.search(rf"(?:{name_en}\s*\({level_en}\)|{name_fr}\s+{level_fr})", texts, re.I):
                languages.append({"code": code, "language": name_en, "level": level_en,
                                  "certification": None, "label": {"en": f"{name_en} ({level_en})",
                                                                   "fr": f"{name_fr} {level_fr}"}})
        if languages:
            facts.append(_fact("identity.languages", "Languages (self-reported, no test certification)",
                               {"languages": languages}, language_sources, "identity"))
    return name


def _legacy_conflicts(facts: list[dict], sources: list[Source]) -> None:
    by_key = {fact["key"]: fact for fact in facts}
    for source in sources:
        lines = source.text.splitlines(keepends=True)
        offset = 0
        for index, line in enumerate(lines):
            plain = latex_text(line)
            for company in ("dxc", "jesa", "ocp"):
                key = f"experience.{company}"
                if key not in by_key or company not in _fold(plain):
                    continue
                # CV macros may place dates on the immediately preceding line.
                context = "".join(lines[max(0, index - 1):index + 2])
                parsed = _period(context)
                fact, value = by_key[key], by_key[key]["value"]
                evidence = source.evidence(max(0, offset - len(lines[index - 1]) if index else 0),
                                           offset + len(line) + (len(lines[index + 1]) if index + 1 < len(lines) else 0))
                if parsed and parsed != value["period"]:
                    alternative = {"field": "period", "value": parsed, "source_path": source.path}
                    if alternative not in value["alternatives"]:
                        value["alternatives"].append(alternative)
                        fact["sources"].append(evidence)
                        fact["status"] = "conflicting"
                # Avoid interpreting the candidate's own header city as job location.
                if company == "dxc" and re.search(r"DXC[^\n]*Casablanca", plain, re.I):
                    preferred = " ".join(value["location"].values())
                    if "Casablanca" not in preferred:
                        alternative = {"field": "location", "value": "Casablanca", "source_path": source.path}
                        if alternative not in value["alternatives"]:
                            value["alternatives"].append(alternative)
                            fact["sources"].append(evidence)
                            fact["status"] = "conflicting"
            licence = by_key.get("education.licence")
            if licence and re.search(r"licence|bachelor|FST.*Settat", plain, re.I):
                # A preceding Master's row must never supply a Licence's dates.
                context = line
                years_pattern = r"\b(20\d{2})\s*(?:–|--?|—)\s*(20\d{2})\b"
                years = re.search(years_pattern, plain)
                if years is None and re.search(r"FST.*Settat", plain, re.I) and index:
                    previous = latex_text(lines[index - 1])
                    if re.search(r"licence|bachelor", previous, re.I):
                        context = lines[index - 1] + line
                        years = re.search(years_pattern, previous)
                if years and int(years.group(1)) != licence["value"]["start_year"]:
                    alternative = {"field": "start_year", "value": int(years.group(1)), "source_path": source.path}
                    if alternative not in licence["value"]["alternatives"]:
                        licence["value"]["alternatives"].append(alternative)
                        licence["sources"].append(source.evidence(offset, offset + len(context)))
                        licence["status"] = "conflicting"
            offset += len(line)


def _research(sources: list[Source], paper: Source | None, facts: list[dict]) -> None:
    status_sources, claims = [], []
    for source in sources:
        evidence = source.matching(r"^.*(?:author and presenter|auteur et pr.sentateur|accepted paper|"
                                   r"papier accept.|published (?:research )?paper|\*\*Author & Presenter).*$", context=80)
        if evidence:
            status_sources.extend(evidence)
            for item in evidence:
                text = item["excerpt"].lower()
                for claim, pattern in (("published", "published"), ("accepted", "accepted|accepté"),
                                       ("presented", "presenter|présentateur"), ("authored", "author|auteur")):
                    if re.search(pattern, text) and claim not in claims:
                        claims.append(claim)
    if status_sources:
        facts.append(_fact("research.publication_status", "Research authorship / acceptance / publication",
                           {"status": None, "source_claims": claims, "doi": None,
                            "note": "Confirm each distinct stage; a local paper is not proof of publication."},
                           status_sources, "research", "conflicting" if "published" in claims else "unconfirmed"))
    if paper is None:
        return
    # Curated summary requires the exact reviewed evidence clauses. An edited paper
    # is retained for review without silently reusing claims no longer supported.
    required = ("LZ4 compression with Kyber512", "ESP32-DevKitC", "1,440 bytes", "N=100",
                "AES-GCM", "upper-bound estimates", "side-channel hardening", "multi-platform comparison")
    compact = re.sub(r"\s+", " ", paper.text)
    if not all(clause in compact for clause in required) or "Abdessamad Jaouad" not in paper.text:
        facts.append(_fact("research.paper_review", "Research paper needs extraction review",
                           {"source_path": paper.path, "review_required": True}, [paper.evidence()], "research"))
        return
    conclusion = paper.text.find("V. CONCLUSION")
    sources = [paper.evidence(0, min(3000, len(paper.text)))]
    if conclusion >= 0:
        sources.append(paper.evidence(conclusion, min(conclusion + 3300, len(paper.text))))
    facts.append(_fact("research.pqc", "Applied IoT security research",
                       {"title": {"en": "Reducing Post-Quantum Cryptography Overhead in IoT Networks Using an "
                                         "Epoch-Based Compression Approach",
                                  "fr": "Réduction du coût de la cryptographie post-quantique dans les réseaux IoT"},
                        "description": {
                            "en": "Experimental study of epoch batching, LZ4 compression and Kyber512 on ESP32; "
                                  "comparison against an equally compressed ECDH baseline. The fixed 1,440-byte "
                                  "PQC penalty per epoch remains; batching spreads that cost across messages.",
                            "fr": "Étude expérimentale du regroupement par époque, de LZ4 et de Kyber512 sur ESP32 ; "
                                  "comparaison avec ECDH à compression égale. Conservation d'un surcoût fixe de "
                                  "1 440 octets par époque, réparti entre les messages grâce au regroupement."},
                        "limitations": {
                            "en": "Energy values are upper-bound estimates; direct energy profiling, side-channel "
                                  "hardening and evaluation on other devices remain future work.",
                            "fr": "Estimation majorante de l'énergie ; mesure directe, protection contre les canaux "
                                  "auxiliaires et évaluation sur d'autres appareils restant à réaliser."},
                        "technologies": ["LZ4", "Kyber512", "ESP32", "AES-GCM"],
                        "publication_status": None, "doi": None, "source_path": paper.path}, sources, "research"))


def _history(root: Path, extracted_at: str, warnings: list[str]) -> list[dict]:
    paths = ["email.txt"]
    for directory in ("motiv-letters", "offres-stage-pfe"):
        folder = root / directory
        if not folder.exists() or not folder.resolve().is_relative_to(root):
            continue
        for path in sorted(folder.iterdir()):
            folded = _fold(path.name)
            if path.suffix.lower() in (".tex", ".txt") and any(
                    word in folded for word in ("cover_letter", "lettre_motiv", "cover-letter")):
                paths.append(str(path.relative_to(root)))
    result = []
    for relative in paths:
        source = _read_source(root, relative, extracted_at, warnings)
        if source is None:
            continue
        body, _ = _body(source)
        content = latex_text(body)
        if not re.search(r"Abdessamad\s+Jaouad|Jaouad\s+Abdessamad", content, re.I):
            warnings.append(f"Draft not attributed to candidate, skipped: {relative}")
            continue
        subject = re.search(r"(?:^|\n)\s*(?:Objet|Subject)\s*:\s*([^\n]+)", content, re.I)
        title = (subject.group(1).strip().rstrip(".") if subject else
                 "Local application draft — " + Path(relative).stem.replace("_", " "))[:250]
        if not subject:
            warnings.append(f"Draft retained with archival title; no explicit subject: {relative}")
        # Recipient identification uses only the address block, before the subject.
        salutation = re.search(r"(?:^|\n)\s*(?:Madame|Dear|Monsieur|Bonjour)", content, re.I)
        header_end = subject.start() if subject else salutation.start() if salutation else 0
        before_subject = content[:header_end]
        known_employers = ("Emirates Group", "Bank Al-Maghrib", "Orange Business", "ALTEN", "Citech",
                           "Deloitte", "Capgemini", "CGI", "ONCF", "Safari Retail Group", "Cyclad", "Vulcane",
                           "Smile", "Inetum", "UM6P", "Stellantis", "SAP", "Atos", "SUEZ", "Salafin", "BMCE",
                           "Dassault", "VisShopAI", "The Game Changer", "Extra Immobilien", "Segula")
        employer = next((item for item in known_employers if re.search(
            r"\b" + re.escape(item) + r"\b", before_subject, re.I)), "Employer not established in draft header")
        # Email draft explicitly identifies a programme but not the hiring entity.
        if relative == "email.txt" and "Renault IT Managed Services" in content:
            employer = "Renault IT Managed Services (programme; hiring entity unconfirmed)"
        result.append({"title": title, "employer": employer, "description": content[:16000],
                       "source_path": relative, "source_sha256": source.digest, "state": "drafting",
                       "evidence": [source.evidence()], "history_type": "local_draft",
                       "note": "Imported local writing; no delivery, receipt or submission evidence."})
    return result


def build_profile_evidence(portfolio_root: Path) -> dict:
    """Read portfolio evidence and return JSON-serializable, unconfirmed facts.

    Does not write files, call a network/model/provider, use file modification times,
    or mark any application sent. Empty/missing files never become candidate facts.
    Every source reference is relative to the explicitly supplied portfolio root.
    """
    root = Path(portfolio_root).resolve()
    warnings: list[str] = []
    extracted_at = datetime.now(timezone.utc).isoformat()
    recent = {}
    for language, path in RECENT_CVS.items():
        source = _read_source(root, path, extracted_at, warnings)
        if source:
            recent[language] = source
    profiles = [source for path in PROFILE_FILES
                if (source := _read_source(root, path, extracted_at, warnings))]
    legacy = [source for path in LEGACY_FILES
              if (source := _read_source(root, path, extracted_at, warnings))]
    facts: list[dict] = []
    name = _identity(recent, profiles, facts)
    _recent_content(recent, facts, warnings)
    _legacy_conflicts(facts, profiles + legacy)
    paper = _read_source(root, "a185-jaouad final.pdf", extracted_at, warnings)
    _research([*recent.values(), *profiles, *legacy], paper, facts)
    # Companion PDFs are independent evidence artifacts, not assumptions of parity.
    for language, filename in RECENT_CVS.items():
        pdf = _read_source(root, filename.replace(".tex", ".pdf"), extracted_at, warnings)
        if pdf:
            facts.append(_fact(f"source.cv_pdf.{language}", f"Recent {language.upper()} CV PDF",
                               {"source_path": pdf.path, "pages": len(pdf.pages),
                                "content_role": "reference_layout", "parity_confirmed": False},
                               [pdf.evidence()], "source"))
    for key, label in (
        ("citizenship", "Actual citizenship(s)"), ("work_authorizations", "Current work authorizations"),
        ("degree_completion", "Degree completion, exact awards and transcript availability"),
        ("language_tests", "Available language-test results"), ("annotation_experience", "Annotation employer, dates and tasks"),
        ("countries", "Preferred and excluded countries"), ("tracks_contracts", "Accepted tracks and contract types"),
        ("salary_funding", "Salary and funding floors, with currency and period"),
        ("relocation_start", "Relocation assistance and acceptable start-date window"),
        ("unknown_sponsorship", "Research/review preference for unknown sponsorship"),
        ("references", "Reference contacts and disclosure permission"),
    ):
        facts.append(_fact(f"onboarding.{key}", label, None, [], "onboarding"))
    if not recent:
        warnings.append("Recent CV source files are missing; no experience/content library was synthesized.")
    return {"name": name, "facts": facts, "history": _history(root, extracted_at, warnings),
            "warnings": warnings}
