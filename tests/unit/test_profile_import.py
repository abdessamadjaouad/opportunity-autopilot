from datetime import datetime
from hashlib import sha256
from pathlib import Path

from app.profile import build_profile_evidence
from app.profile.importer import Source, _research, latex_text


FR_CV = r"""\begin{document}
\begin{center}
Abdessamad JAOUAD
+212 679 075 431 | abdessamadjaouad0@gmail.com
linkedin.com/in/abdessamadjaouad | github.com/abdessamadjaouad
Casablanca, Maroc
\end{center}
\cvEntry{Ingénieur IA - Stage projet de fin d'études}{Février \textendash{} Août 2026}
{DXC Technology Maroc}{Rabat, Maroc}
\begin{cvitems}
\item Conception d'AI Sandbox avec FastAPI et PostgreSQL.
\item Développement de suites d'évaluation LLM-as-a-judge.
\end{cvitems}
\cvEntry{Data Scientist - Stage d'été}{Juillet \textendash{} Septembre 2025}
{JESA S.A.}{Casablanca, Maroc}
\begin{cvitems}
\item Automatisation avec XGBoost et précision de 95\% sur un jeu de test synthétique distinct.
\end{cvitems}
\cvEducation{Master Big Data et IoT}{2024 \textendash{} 2026}{ENSAM Casablanca}
\cvEducation{Licence Systèmes d'information}{2020 \textendash{} 2024}{FST Settat}
\begin{cvitems}
\item \projecttitle{Pipeline ELT temps réel (2025) :} conception avec Kafka et Airflow.
\item \projecttitle{Data Quality (2025) :} nettoyage avec Power BI.
\end{cvitems}
\skilllabel{Langages :} Python, SQL, Java
\skilllabel{Certifications :} AWS Cloud Foundations (2026)
\skilllabel{Langues :} arabe maternel · français courant · anglais courant
\skilllabel{Recherche :} auteur et présentateur d'un article à FET'26.
\end{document}
"""

EN_CV = r"""\begin{document}
\begin{center}
Abdessamad JAOUAD
Casablanca, Morocco
\end{center}
\cvEntry{AI Engineer - Final-Year Project Internship}{February \textendash{} August 2026}
{DXC Technology Morocco}{Rabat, Morocco}
\begin{cvitems}
\item Designed AI Sandbox with FastAPI and PostgreSQL.
\item Developed LLM-as-a-judge evaluation suites.
\end{cvitems}
\cvEntry{Data Scientist - Summer Internship}{July \textendash{} September 2025}
{JESA S.A.}{Casablanca, Morocco}
\begin{cvitems}
\item Automated with XGBoost, reaching 95\% accuracy on a separate synthetic test set.
\end{cvitems}
\cvEducation{Master Big Data and IoT}{2024 \textendash{} 2026}{ENSAM Casablanca}
\cvEducation{Bachelor's Information Systems}{2020 \textendash{} 2024}{FST Settat}
\begin{cvitems}
\item \projecttitle{Real-Time ELT Pipeline (2025):} designed with Kafka and Airflow.
\end{cvitems}
\skilllabel{Programming :} Python, SQL, Java
\skilllabel{Languages:} Arabic (native) · French (fluent) · English (fluent)
\end{document}
"""


def write(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def facts_by_key(result: dict) -> dict:
    return {fact["key"]: fact for fact in result["facts"]}


def test_empty_portfolio_does_not_synthesize_candidate_or_history(tmp_path):
    result = build_profile_evidence(tmp_path)
    assert result["name"] == "Profile awaiting import"
    assert result["history"] == []
    assert all(f["value"] is None and f["sources"] == [] for f in result["facts"])
    assert all(f["status"] == "unconfirmed" for f in result["facts"])
    assert result["warnings"]


def test_content_is_bilingual_and_every_claim_has_source_hash_and_location(tmp_path):
    write(tmp_path, "CV_Abdessamad_Jaouad.tex", FR_CV)
    write(tmp_path, "CV_Abdessamad_Jaouad_EN.tex", EN_CV)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    result = build_profile_evidence(tmp_path)
    facts = facts_by_key(result)
    assert facts["experience.dxc"]["value"]["period"] == {
        "start": "2026-02", "end": "2026-08", "precision": "month"}
    assert set(facts["experience.dxc"]["value"]["bullets"]) == {"en", "fr"}
    assert facts["experience.dxc"]["employment_type"] == "internship"
    assert "95%" in facts["experience.jesa"]["value"]["bullets"]["en"][0]
    assert "synthetic test set" in facts["experience.jesa"]["value"]["bullets"]["en"][0]
    assert "synthétique distinct" in facts["experience.jesa"]["value"]["bullets"]["fr"][0]
    assert facts["project.data_quality"]["value"]["description"]["fr"].startswith("Nettoyage")
    assert facts["education.master"]["value"]["award_confirmed"] is False
    assert facts["education.master"]["value"]["award_date"] is None
    assert facts["identity.contact"]["value"]["github"] == "github.com/abdessamadjaouad"
    assert len(facts["identity.languages"]["value"]["languages"]) == 3
    assert all(language["certification"] is None
               for language in facts["identity.languages"]["value"]["languages"])
    for fact in result["facts"]:
        assert fact["status"] != "confirmed"
        for source in fact["sources"]:
            path = tmp_path / source["path"]
            assert source["sha256"] == sha256(path.read_bytes()).hexdigest()
            assert source["excerpt"] in path.read_text()
            assert source["location"].startswith("lines ")
            assert datetime.fromisoformat(source["extracted_at"]).tzinfo is not None
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir()}


def test_conflicts_keep_recent_values_and_actual_older_evidence(tmp_path):
    write(tmp_path, "CV_Abdessamad_Jaouad.tex", FR_CV)
    write(tmp_path, "profile-desc.md", """# Profile
**DXC Technology Morocco** · Casablanca · Feb 2026 – June 2026
**Master Big Data** | 2024–2026
**Licence Information Systems** | 2021–2024
Published research paper.
""")
    write(tmp_path, "old-resumes-assets/CV_JAOUAD_Abdessamad_Combined.md",
          "JESA S.A. | June – Sept 2025\n")
    result = build_profile_evidence(tmp_path)
    facts = facts_by_key(result)
    dxc = facts["experience.dxc"]
    assert dxc["status"] == "conflicting"
    assert dxc["value"]["period"]["end"] == "2026-08"
    assert {a["field"] for a in dxc["value"]["alternatives"]} == {"period", "location"}
    assert facts["experience.jesa"]["status"] == "conflicting"
    licence = facts["education.licence"]
    assert licence["value"]["start_year"] == 2020
    assert [a["value"] for a in licence["value"]["alternatives"]] == [2021]
    publication = facts["research.publication_status"]
    assert publication["status"] == "conflicting"
    assert publication["value"]["status"] is None
    assert publication["value"]["doi"] is None


def test_changed_content_is_read_from_source_and_missing_bullets_are_not_reused(tmp_path):
    write(tmp_path, "CV_Abdessamad_Jaouad.tex", FR_CV)
    before = facts_by_key(build_profile_evidence(tmp_path))["experience.dxc"]
    updated = FR_CV.replace("Août 2026", "Septembre 2026").replace(
        r"\item Développement de suites d'évaluation LLM-as-a-judge.", "")
    write(tmp_path, "CV_Abdessamad_Jaouad.tex", updated)
    after = facts_by_key(build_profile_evidence(tmp_path))["experience.dxc"]
    assert after["value"]["period"]["end"] == "2026-09"
    assert len(after["value"]["bullets"]["fr"]) == 1
    assert before["sources"][0]["sha256"] != after["sources"][0]["sha256"]


def test_non_internship_title_cannot_gain_internship_type(tmp_path):
    write(tmp_path, "CV_Abdessamad_Jaouad.tex", FR_CV.replace(
        "Ingénieur IA - Stage projet de fin d'études", "Ingénieur IA"))
    fact = facts_by_key(build_profile_evidence(tmp_path))["experience.dxc"]
    assert fact["employment_type"] == "unknown"


def test_signed_drafts_survive_without_subject_and_never_become_sent(tmp_path):
    write(tmp_path, "email.txt", """Objet : Candidature – Junior DevOps Engineer
Bonjour,
Renault IT Managed Services.
Abdessamad Jaouad
""")
    write(tmp_path, "motiv-letters/cover_letter.tex", r"""\begin{document}
Emirates Group
\textbf{Subject: Application for Junior Data Scientist}
Dear Hiring Team,
Abdessamad Jaouad
\end{document}""")
    write(tmp_path, "offres-stage-pfe/Atos_Cover_Letter.tex", r"""\begin{document}
Atos
Madame, Monsieur,
Je présente ma candidature.
JAOUAD Abdessamad
\end{document}""")
    write(tmp_path, "job-offers/example.txt", "Saved advert only. No sent application evidence.")
    write(tmp_path, "motiv-letters/unrelated_cover_letter.tex", "Subject: Another person's draft")
    result = build_profile_evidence(tmp_path)
    assert len(result["history"]) == 3
    assert all(h["state"] == "drafting" for h in result["history"])
    assert all(h["history_type"] == "local_draft" for h in result["history"])
    assert not any(h.get("url") for h in result["history"])
    assert any(h["employer"] == "Emirates Group" for h in result["history"])
    assert any(h["title"].startswith("Local application draft") for h in result["history"])


def test_missing_onboarding_answers_never_become_inferred_rights(tmp_path):
    write(tmp_path, "CV_Abdessamad_Jaouad.tex", FR_CV)
    facts = facts_by_key(build_profile_evidence(tmp_path))
    assert facts["identity.location"]["value"]["country_code"] == "MA"
    for key in ("citizenship", "work_authorizations", "annotation_experience", "salary_funding", "references"):
        assert facts["onboarding." + key]["value"] is None
        assert facts["onboarding." + key]["sources"] == []
    assert facts["certifications.fr"]["value"]["professional_equivalence"] is None


def test_invalid_pdf_and_external_symlink_are_skipped_without_abandoning_import(tmp_path):
    root = tmp_path / "portfolio"
    root.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("PRIVATE OUTSIDE CONTENT")
    (root / "CV_Abdessamad_Jaouad.tex").symlink_to(secret)
    write(root, "a185-jaouad final.pdf", "not a PDF")
    write(root, "CV_Abdessamad_Jaouad_EN.tex", EN_CV)
    result = build_profile_evidence(root)
    assert any("outside portfolio" in warning for warning in result["warnings"])
    assert any("Could not extract" in warning for warning in result["warnings"])
    assert "experience.dxc" in facts_by_key(result)
    assert "PRIVATE OUTSIDE CONTENT" not in str(result)


def test_research_summary_requires_all_reviewed_claims_and_never_sets_publication():
    partial = Source("paper.pdf", "Abdessamad Jaouad LZ4 compression with Kyber512 ESP32-DevKitC 1,440 bytes N=100",
                     "a" * 64, "2026-09-09T00:00:00+00:00", ("First page",))
    facts = []
    _research([], partial, facts)
    assert facts[0]["key"] == "research.paper_review"
    full = Source("paper.pdf", partial.text + " AES-GCM upper-bound estimates side-channel hardening "
                  "multi-platform comparison", "b" * 64, partial.extracted_at, (partial.text,))
    facts = []
    _research([], full, facts)
    assert facts[0]["key"] == "research.pqc"
    assert facts[0]["value"]["publication_status"] is None
    assert "1,440-byte" in facts[0]["value"]["description"]["en"]
    assert "upper-bound estimates" in facts[0]["value"]["limitations"]["en"]
    assert facts[0]["sources"][0]["location"] == "page 1"


def test_tex_is_read_as_text_without_execution_or_layout_arguments():
    literal = r"\textcolor{accent}{Conception} avec \href{https://example.test}{Python} : 95\,\%."
    assert latex_text(literal) == "Conception avec Python : 95 %."
    # Dangerous commands have no interpreter here; their text cannot run anything.
    assert "touch" in latex_text(r"\write18{touch /tmp/should-never-exist}")
