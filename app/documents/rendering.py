"""Render immutable, requested-only documents in a network-disabled TeX sandbox."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata

from pypdf import PdfReader

from .content import ContentLibrary, FAMILIES


TEMPLATE_VERSION = "lato-fact-library-v1"
GENERATED_KINDS = {"cv", "cover_letter", "motivation_text", "research_statement", "proposal"}
LABELS = {
    "en": {"experience": "EXPERIENCE", "education": "EDUCATION", "projects": "SELECTED PROJECTS",
           "skills": "TECHNICAL SKILLS", "languages": "LANGUAGES", "research": "APPLIED RESEARCH",
           "draft": "DRAFT · REVIEW REQUIRED · NOT APPROVED FOR SUBMISSION",
           "cover_letter": "APPLICATION LETTER", "research_statement": "RESEARCH STATEMENT", "proposal": "RESEARCH PROPOSAL"},
    "fr": {"experience": "EXPÉRIENCES", "education": "FORMATION", "projects": "PROJETS SÉLECTIONNÉS",
           "skills": "COMPÉTENCES TECHNIQUES", "languages": "LANGUES", "research": "RECHERCHE APPLIQUÉE",
           "draft": "BROUILLON · À VÉRIFIER · NON APPROUVÉ POUR ENVOI",
           "cover_letter": "LETTRE DE CANDIDATURE", "research_statement": "PROJET DE PARCOURS DE RECHERCHE",
           "proposal": "PROPOSITION DE RECHERCHE"},
}


def latex_escape(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Document content must be text")
    if len(value) > 32000 or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError("Document content exceeds limits or contains control characters")
    value = unicodedata.normalize("NFC", value).replace("\u00a0", " ").replace("\u202f", " ")
    mapping = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&",
               "#": r"\#", "%": r"\%", "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
               "–": r"\textendash{}", "—": r"\textemdash{}", "’": "'", "‘": "'", "“": "``", "”": "''",
               "\n": " ", "\t": " "}
    return "".join(mapping.get(character, character) for character in value)


def _slug(text: str, limit: int = 55) -> str:
    ascii_text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-")[:limit] or "application"


def _header(header: dict, language: str, draft: bool) -> str:
    contact = header["contact"]
    line = r"\begin{center}" + "\n"
    line += r"{\fontsize{21}{22}\selectfont\bfseries\color{accent} " + latex_escape(header["name"]) + r"}\par" + "\n"
    line += r"\vspace{2pt}{\fontsize{10.2}{11.2}\selectfont " + latex_escape(header["headline"]) + r"}\par" + "\n"
    line += r"\vspace{3pt}" + latex_escape(contact.get("phone", "")) + " | " + latex_escape(contact.get("email", "")) + r"\\" + "\n"
    line += latex_escape(contact.get("linkedin", "")) + " | " + latex_escape(contact.get("github", "")) + r"\\" + "\n"
    line += latex_escape(header["location"]) + "\n" + r"\end{center}\vspace{-7pt}" + "\n"
    if draft:
        line += r"{\color{muted}\fontsize{9}{10}\selectfont " + latex_escape(LABELS[language]["draft"]) + r"}\par" + "\n"
    return line


def _cv(library: ContentLibrary) -> tuple[dict, str, list[str]]:
    header = library.header()
    experiences = library.experiences()
    education = library.education()
    projects = library.projects(limit=2 if library.family == "academic" else 3)
    skills = library.skills()
    languages = library.languages()
    research = library.research() if library.family == "academic" else None
    language = library.language
    body, expected = [], [header["name"], *header["contact"].values(), header["location"]]
    if not experiences:
        library.blockers.add("missing_experience_content")
    if not education:
        library.blockers.add("missing_education_content")
    if experiences:
        body.append(r"\docsection{" + LABELS[language]["experience"] + "}")
        for item in experiences:
            fields = [item["title"], item["period"], item["employer"], item["location"]]
            expected.extend(fields + item["bullets"])
            body.append(r"\docentry" + "".join("{" + latex_escape(value) + "}" for value in fields))
            body.append(r"\begin{docitems}")
            body.extend(r"\item " + latex_escape(bullet) for bullet in item["bullets"])
            body.append(r"\end{docitems}")
    if education:
        body.append(r"\docsection{" + LABELS[language]["education"] + "}")
        for item in education:
            expected.extend([item["title"], item["institution"], item["period"]])
            body.append(r"{\bfseries " + latex_escape(item["title"]) + r"}\hfill " + latex_escape(item["period"]) + r"\par")
            body.append(r"{\itshape " + latex_escape(item["institution"]) + r"}\par\vspace{2pt}")
    if research:
        body.append(r"\docsection{" + LABELS[language]["research"] + "}")
        body.append(latex_escape(research["description"]) + r"\par " + latex_escape(research["limitations"]))
        expected.extend([research["description"], research["limitations"]])
    if projects:
        body.append(r"\docsection{" + LABELS[language]["projects"] + "}")
        body.append(r"\begin{docitems}")
        for item in projects:
            body.append(r"\item {\color{accent}" + latex_escape(item["title"]) + " :} " + latex_escape(item["description"]))
            expected.extend([item["title"], item["description"]])
        body.append(r"\end{docitems}")
    if skills:
        body.append(r"\docsection{" + LABELS[language]["skills"] + "}")
        body.append(latex_escape(" · ".join(skills)) + r"\par")
        expected.extend(skills)
    if languages:
        body.append(r"\docsection{" + LABELS[language]["languages"] + "}")
        body.append(latex_escape(" · ".join(languages)))
        expected.extend(languages)
    return header, "\n".join(body), expected


def _prose(library: ContentLibrary, kind: str) -> tuple[dict, list[str]]:
    header = library.header()
    title, employer = library.opportunity.get("title", ""), library.opportunity.get("employer", "")
    if not title or not employer:
        library.blockers.add("opportunity_identity_missing")
    if kind == "proposal":
        owner_text = library.owner_document_text(kind)
        if not owner_text:
            library.blockers.add("owner_proposal_required")
            return header, []
        return header, [paragraph.strip() for paragraph in owner_text.split("\n\n") if paragraph.strip()]
    if kind == "research_statement":
        research = library.research(required=True)
        if not research:
            return header, []
        opening = (f"Research statement for {title}, {employer}." if library.language == "en" else
                   f"Parcours de recherche pour {title}, {employer}.")
        paragraphs = [opening, research["description"], research["limitations"]]
        # A factual prior project supplies context; no new scientific result or degree is invented.
        projects = library.projects(limit=1)
        if projects:
            paragraphs.append(projects[0]["description"])
        return header, paragraphs
    selected = library.experiences(max_bullets=1, limit=2)
    if not selected:
        library.blockers.add("missing_experience_content")
    if library.language == "en":
        paragraphs = [f"I am applying for the {title} position at {employer}."]
        paragraphs.extend(item["employer"] + ": " + " ".join(item["bullets"]) for item in selected)
        paragraphs.append("I would welcome the opportunity to discuss how this experience relates to the role.")
    else:
        paragraphs = [f"Je vous adresse ma candidature au poste de {title} chez {employer}."]
        paragraphs.extend(item["employer"] + " : " + " ".join(item["bullets"]) for item in selected)
        paragraphs.append("Je serais heureux de vous présenter mes réalisations et d'échanger sur les missions du poste.")
    return header, paragraphs


def sandbox_command(work_dir: Path) -> list[str]:
    """Expose the pinned TeX installation and exactly one work directory, no home/mail/CVs."""
    if not shutil.which("bwrap") or not shutil.which("pdflatex"):
        raise RuntimeError("Network-disabled TeX sandbox is not configured")
    command = [shutil.which("bwrap"), "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
               "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib", "--symlink", "usr/lib", "/lib64",
               "--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for path in ("/etc/texmf", "/var/lib/texmf", "/etc/fonts", "/var/cache/fontconfig"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    command += ["--bind", str(work_dir.resolve()), "/work", "--chdir", "/work",
                "--setenv", "PATH", "/usr/bin", "--setenv", "HOME", "/tmp",
                "--setenv", "TEXMFHOME", "/nonexistent", "--setenv", "TEXMFVAR", "/tmp/texmf-var",
                "--setenv", "TEXMFCONFIG", "/tmp/texmf-config", "--setenv", "openin_any", "p",
                "--setenv", "openout_any", "p", "--setenv", "SOURCE_DATE_EPOCH", "1788825600",
                "/usr/bin/pdflatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error",
                "-file-line-error", "document.tex"]
    return command


def _compile(source: str, output_dir: Path, basename: str, sandbox: str) -> tuple[Path | None, list[str], str]:
    if sandbox != "bubblewrap":
        return None, ["tex_sandbox_unavailable"], ""
    try:
        with tempfile.TemporaryDirectory(prefix=".tex-work-", dir=output_dir) as directory:
            work_dir = Path(directory)
            (work_dir / "document.tex").write_text(source, encoding="utf-8")
            process = subprocess.run(sandbox_command(work_dir), capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=45, check=False)
            log_path = work_dir / "document.log"
            log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            pdf_path = work_dir / "document.pdf"
            if process.returncode != 0 or not pdf_path.is_file():
                # Never place compiler output (which can contain private document text) in API/log errors.
                error = "tex_sandbox_unavailable" if "bwrap:" in process.stderr else "tex_compilation_failed"
                return None, [error], log
            destination = output_dir / (basename + ".pdf")
            with destination.open("xb") as file:
                file.write(pdf_path.read_bytes())
            destination.chmod(0o600)
            return destination, [], log
    except subprocess.TimeoutExpired:
        return None, ["tex_compile_timeout"], ""
    except RuntimeError:
        return None, ["tex_sandbox_unavailable"], ""


def _text_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("’", "'").replace("‘", "'")
    value = value.replace("–", "-").replace("—", "-").replace("\u00ad", "")
    return re.sub(r"\s+", "", value).casefold()


def validate_pdf(path: Path, *, expected: list[str], max_pages: int, max_bytes: int, tex_log: str) -> dict:
    errors, warnings, font_sizes, fonts = [], [], [], set()
    try:
        reader = PdfReader(path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if not 1 <= len(reader.pages) <= max_pages:
            errors.append("page_limit_exceeded")
        if path.stat().st_size > max_bytes:
            errors.append("file_size_limit_exceeded")
        if len(text.strip()) < 30:
            errors.append("pdf_text_missing")
        for index, fragment in enumerate(expected):
            if fragment and _text_key(fragment) not in _text_key(text):
                errors.append("pdf_text_mismatch:" + str(index))
        root = reader.trailer["/Root"]
        if root.get("/OpenAction") or root.get("/AA") or root.get("/Names"):
            errors.append("pdf_active_content")
        for page in reader.pages:
            if page.get("/Annots"):
                errors.append("pdf_annotations_not_allowed")
            for font in (page.get("/Resources", {}).get("/Font", {}) or {}).values():
                font = font.get_object()
                name = str(font.get("/BaseFont", ""))
                fonts.add(name)
                if "Lato" not in name:
                    errors.append("unexpected_pdf_font")
            page.extract_text(visitor_text=lambda text, cm, tm, font, size: font_sizes.append(float(size))
                              if text.strip() else None)
        if not fonts:
            errors.append("pdf_fonts_missing")
        if font_sizes and min(font_sizes) < 8.95:
            errors.append("font_below_readable_minimum")
        overflows = re.findall(r"Overfull \\[hv]box \(([0-9.]+)pt too (?:wide|high)\)", tex_log)
        if any(float(value) > 0.5 for value in overflows):
            errors.append("layout_overflow")
        return {"errors": sorted(set(errors)), "warnings": warnings, "pages": len(reader.pages),
                "text_characters": len(text), "fonts": sorted(fonts),
                "minimum_font_pt": min(font_sizes) if font_sizes else None}
    except Exception:
        return {"errors": ["pdf_validation_failed"], "warnings": [], "pages": None}


def _artifact(path: Path, kind: str, mime_type: str) -> dict:
    return {"path": str(path.resolve()), "filename": path.name, "mime_type": mime_type,
            "sha256": sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size, "kind": kind}


def _write_private(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8") as file:
        file.write(content)
    path.chmod(0o600)


def build_documents(profile: dict, opportunity: dict, output_dir: Path, *, family: str, language: str,
                    requested_documents: list[dict], academic_page_limit: int = 1,
                    tex_sandbox: str = "bubblewrap") -> dict:
    """Prepare drafts or validated artifacts, never communicate with a recipient.

    Unconfirmed claims visibly mark every rendered artifact as a draft and block
    dispatch. Unsupported required documents become resumable human blockers.
    A failed sandbox never triggers a direct pdflatex fallback.
    """
    if family not in FAMILIES or language not in LABELS:
        raise ValueError("Unsupported document family or language")
    if type(academic_page_limit) is not int or academic_page_limit not in (1, 2):
        raise ValueError("Academic page limit requires a supported standing-policy value")
    if not isinstance(requested_documents, list) or len(requested_documents) > 12:
        raise ValueError("Invalid requested document list")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    template = (Path(__file__).parent / "templates" / "base.tex").read_text(encoding="utf-8")
    artifacts, errors, checks, text_answers = [], [], {}, {}
    all_blockers, all_warnings, selected = set(), set(), set()
    seen = set()
    for requirement in requested_documents:
        if not isinstance(requirement, dict) or not isinstance(requirement.get("kind"), str):
            raise ValueError("Invalid document requirement")
        kind = requirement["kind"]
        if kind in seen:
            raise ValueError("Duplicate requested document kind")
        seen.add(kind)
        for field in ("max_words", "max_pages", "max_bytes"):
            if field in requirement and (type(requirement[field]) is not int or requirement[field] <= 0):
                raise ValueError("Document limits must be positive integers")
        if kind not in GENERATED_KINDS:
            if requirement.get("required") is True:
                all_blockers.add("missing_document:" + kind)
            else:
                all_warnings.add("optional_document_not_generated:" + kind)
            continue
        library = ContentLibrary(profile, opportunity, family, language)
        expected, text, body = [], "", ""
        if kind == "cv":
            header, body, expected = _cv(library)
            text = " ".join(expected)
        else:
            header, paragraphs = _prose(library, kind)
            text = "\n\n".join(paragraphs)
            expected = [header["name"], *paragraphs]
            if kind != "motivation_text":
                heading = LABELS[language][kind]
                body = r"\docsection{" + heading + "}\n"
                reference = str(opportunity.get("external_id") or "")
                if reference:
                    body += latex_escape("Reference: " + reference) + r"\par\vspace{6pt}" + "\n"
                    expected.append(reference)
                body += "\n".join(latex_escape(paragraph) + r"\par\vspace{8pt}" for paragraph in paragraphs)
                body += r"\vspace{4pt}" + latex_escape(header["name"])
        if requirement.get("max_words") and text and len(re.findall(r"\S+", text)) > requirement["max_words"]:
            library.blockers.add("word_limit_exceeded:" + kind)
        if kind == "motivation_text" and requirement.get("max_bytes") and len(text.encode("utf-8")) > requirement["max_bytes"]:
            library.blockers.add("text_field_size_limit_exceeded:" + kind)
        if kind in {"proposal", "research_statement", "motivation_text"} and not text:
            all_blockers.update(library.blockers)
            all_warnings.update(library.warnings)
            selected.update(library.selected)
            continue
        draft = bool(library.blockers)
        basename = "_".join((_slug(header["name"], 35), _slug(opportunity.get("title", "application"), 40),
                             _slug(opportunity.get("application_id") or opportunity.get("id", "local"), 16), kind, language))
        if kind == "motivation_text":
            # A form field gets copyable text, never an invented PDF upload.
            text_answers[kind] = text
            path = output_dir / (basename + ".txt")
            marker = LABELS[language]["draft"] + "\n\n" if draft else ""
            _write_private(path, marker + text + "\n")
            artifacts.append(_artifact(path, kind, "text/plain"))
            checks[kind] = {"words": len(re.findall(r"\S+", text)), "format": "plain_text", "draft": draft}
        else:
            source = template.replace("@@BODY@@", _header(header, language, draft) + "\n" + body)
            source_path = output_dir / (basename + ".tex")
            _write_private(source_path, source)
            artifacts.append(_artifact(source_path, kind + "_source", "application/x-tex"))
            pdf, compile_errors, log = _compile(source, output_dir, basename, tex_sandbox)
            errors.extend(kind + ":" + error for error in compile_errors)
            if compile_errors:
                library.blockers.add("document_render_failed:" + kind)
            if pdf:
                page_limit = academic_page_limit if family == "academic" else 1
                page_limit = min(page_limit, requirement.get("max_pages", page_limit))
                check = validate_pdf(pdf, expected=expected, max_pages=page_limit,
                                     max_bytes=requirement.get("max_bytes", 5 * 1024 * 1024), tex_log=log)
                checks[kind] = {**check, "draft": draft}
                errors.extend(kind + ":" + error for error in check["errors"])
                all_warnings.update(kind + ":" + warning for warning in check["warnings"])
                artifacts.append(_artifact(pdf, kind, "application/pdf"))
        all_blockers.update(library.blockers)
        all_warnings.update(library.warnings)
        selected.update(library.selected)
    return {"artifacts": artifacts,
            "validation": {"valid": not errors and not all_blockers, "errors": sorted(set(errors)),
                           "warnings": sorted(all_warnings), "blockers": sorted(all_blockers), "checks": checks},
            "selected_fact_ids": sorted(selected), "text_answers": text_answers, "template_version": TEMPLATE_VERSION}
