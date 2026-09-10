import re
import unicodedata
from datetime import date


def folded(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value)) if not unicodedata.combining(c)).casefold()


def _fact(profile, key, *, confirmed=True):
    for fact in profile.get("facts", []):
        if fact.get("key") == key and (not confirmed or fact.get("status") == "confirmed"):
            return fact.get("value")
    return None


def _answer(profile, key, context="global"):
    for answer in profile.get("answers", []):
        if (answer.get("key") == key and answer.get("context", "global") in {context, "global"}
                and answer.get("confirmed") is True):
            return answer.get("value")
    return _fact(profile, "onboarding." + key)


def extract_requirements(opportunity):
    """A conservative parser; unrecognized mandatory clauses remain explicit unknowns."""
    if "requirements" in opportunity:
        return [{**item, "mandatory": item.get("mandatory") is True} for item in opportunity["requirements"]]
    requirements = []
    description = opportunity.get("description", "")
    for sentence in re.split(r"[\n.;]+", description):
        text = folded(sentence)
        if not text.strip():
            continue
        preferred = bool(re.search(r"preferred|nice to have|bonus|souhaite|un plus|ideally", text))
        mandatory = bool(re.search(r"must|required|mandatory|minimum|at least|essential|exige|obligatoire|au moins", text)) and not preferred
        kind, value = None, None
        if re.search(r"ph\.?d|doctorate|doctorat", text) and (mandatory or preferred):
            kind, value = "degree", "phd"
        elif re.search(r"master|msc|bac\s*\+\s*5", text) and (mandatory or preferred):
            kind, value = "degree", "master"
        elif re.search(r"bachelor|licence|bsc", text) and (mandatory or preferred):
            kind, value = "degree", "bachelor"
        elif re.search(r"ielts|toefl|language certificate|certificat.*langue", text):
            kind, value, mandatory = "language_certificate", sentence.strip(), not preferred
        elif re.search(r"gpa|grade equivalence|equivalence.*note", text):
            kind, value = "grade_equivalence", sentence.strip()
        elif re.search(r"\d+\+?\s*(?:years|ans)", text) and (mandatory or preferred):
            kind, value = "experience", int(re.search(r"(\d+)\+?\s*(?:years|ans)", text).group(1))
        elif re.search(r"right to work|work authori|autorisation de travail", text) and mandatory:
            kind, value = "work_authorization", opportunity.get("country", "")
        elif re.search(r"resident|residant|based in|resider", text) and mandatory:
            kind, value = "residence", opportunity.get("country", "")
        elif re.search(r"citizens only|citizenship|nationalite", text) and mandatory:
            kind, value = "citizenship", sentence.strip()
        elif re.search(r"english|french|francais|anglais", text) and (mandatory or preferred):
            kind, value = "language", "fr" if re.search(r"french|francais", text) else "en"
        elif mandatory:
            kind, value = "unparsed_mandatory", sentence.strip()
        if kind:
            requirements.append({"kind": kind, "value": value, "mandatory": mandatory,
                "evidence": sentence.strip(), "source_url": opportunity.get("url", "")})
    if not any(x["mandatory"] for x in requirements):
        requirements.append({"kind": "requirements_review", "value": None, "mandatory": True,
            "evidence": "No complete reviewed list of mandatory requirements is available"})
    return requirements


def evaluate_requirement(requirement, profile, opportunity):
    kind, value = requirement.get("kind"), requirement.get("value")
    result, reason, facts = "unknown", "Missing confirmed evidence", []
    skills = _fact(profile, "skills.technical") or {}
    preferences = profile.get("candidate", {}).get("preferences", {})
    if kind == "degree":
        degree_levels = {"bachelor": 1, "licence": 1, "master": 2, "phd": 3}
        confirmed_levels = []
        for level in ("licence", "master", "phd"):
            degree = _fact(profile, f"education.{level}")
            if degree and degree.get("award_confirmed") is True:
                confirmed_levels.append(degree_levels[level])
                facts.append(f"education.{level}")
        highest = _answer(profile, "highest_degree")
        if highest in degree_levels:
            confirmed_levels.append(degree_levels[highest])
            facts.append("answer:highest_degree")
        if confirmed_levels and value in degree_levels:
            result = "pass" if max(confirmed_levels) >= degree_levels[value] else "fail"
            reason = "Confirmed awarded degree compared with the required level; no formal equivalence inferred"
    elif kind == "skill":
        known = [folded(x) for x in skills.get("technologies", [])]
        facts = ["skills.technical"]
        if known:
            result = "pass" if folded(value) in known else "unknown"
            reason = "Skill present in confirmed CV evidence" if result == "pass" else "Required skill is not documented"
    elif kind == "experience":
        years = _answer(profile, "postgraduate_fulltime_years")
        facts = ["answer:postgraduate_fulltime_years"]
        if type(years) in (int, float) and type(value) in (int, float):
            result = "pass" if years >= value else "fail"
            reason = "Confirmed full-time postgraduate experience; internships excluded"
    elif kind == "language":
        languages = (_fact(profile, "identity.languages") or {}).get("languages", [])
        match = next((x for x in languages if x["code"] == value), None)
        facts = ["identity.languages"]
        if match:
            result = "pass" if match.get("level") in {"native", "fluent"} else "unknown"
            reason = "Confirmed self-reported fluency; not a language-test certificate"
    elif kind in {"language_certificate", "grade_equivalence", "citizenship"}:
        confirmed = _answer(profile, kind, opportunity.get("id", "global"))
        facts = [f"answer:{kind}"]
        if isinstance(confirmed, dict) and confirmed.get("meets_requirement") is True and confirmed.get("evidence"):
            result, reason = "pass", "Owner confirmed evidence for this requirement"
        elif isinstance(confirmed, dict) and confirmed.get("meets_requirement") is False:
            result, reason = "fail", "Owner confirmed requirement is not met"
    elif kind == "work_authorization":
        rights = preferences.get("work_authorizations")
        if rights is None:
            rights = _answer(profile, "work_authorizations")
        facts = ["preferences.work_authorizations"]
        if isinstance(rights, list):
            result = "pass" if value in rights else "fail"
            reason = "Owner-declared existing work rights; residence does not confer authorization"
    elif kind == "residence":
        location = _fact(profile, "identity.location") or {}
        country = location.get("country_code") or location.get("country")
        country = "MA" if country in {"Morocco", "Maroc"} else country
        facts = ["identity.location"]
        if country and value:
            result = "pass" if country == value else "fail"
            reason = "Current confirmed residence compared with resident-only restriction"
    elif kind == "funding":
        funding = opportunity.get("funding")
        if funding in {"funded", "self_funded", "unpaid"}:
            result = "pass" if funding == "funded" else "fail"
            reason = "Explicit funding terms in the listing"
    elif kind == "document":
        supplied = profile.get("attachments", [])
        if any(x.get("kind") == value for x in supplied):
            result, reason = "pass", "Required owner-supplied document is available; disclosure gate remains separate"
            facts = [f"attachment:{value}"]
        else:
            reason = f"Required {value} is missing"
    elif kind == "start_date":
        available = _answer(profile, "available_start_date")
        if available and value:
            try:
                result = "pass" if date.fromisoformat(available) <= date.fromisoformat(value) else "fail"
                reason = "Owner-confirmed start date compared to mandatory deadline"
                facts = ["answer:available_start_date"]
            except (ValueError, TypeError):
                pass
    elif kind in {"consent", "assessment", "fee", "references", "authorship"}:
        reason = "Consequential requirement needs owner action; no automatic consent, payment or assessment"
    return {**requirement, "result": result, "reason": reason, "fact_keys": facts}


def assess(profile, opportunity):
    description = folded(opportunity.get("description", ""))
    title = folded(opportunity.get("title", ""))
    preferences = profile.get("candidate", {}).get("preferences", {})
    requirements = extract_requirements(opportunity)
    evaluated = [evaluate_requirement(r, profile, opportunity) for r in requirements]
    exclusions = []
    if re.search(r"\bsenior\b|\barchitect\b|\bprincipal\b|\blead\b|\bstaff engineer\b", title):
        exclusions.append("seniority_outside_target")
    country = opportunity.get("country", "")
    if country in preferences.get("excluded_countries", ["MA"]):
        exclusions.append("country_excluded")
    track = opportunity.get("track", "data_ai")
    if track == "income_only" and "income_only" not in preferences.get("tracks", []):
        exclusions.append("income_only_lane_disabled")
    funding = opportunity.get("funding")
    if track == "academic":
        if re.search(r"self.funded|unpaid|non remunere|sans financement", description):
            funding = "self_funded"
        elif re.search(r"fully.funded|funded phd|financement garanti|these financee", description):
            funding = "funded"
        evaluated.append(evaluate_requirement({"kind": "funding", "value": "funded", "mandatory": True,
            "evidence": opportunity.get("funding_evidence") or "Funding terms require explicit verification"},
            profile, {**opportunity, "funding": funding}))
    relocation, relocation_evidence = "unknown", "No verified relocation or sponsorship statement"
    owner_evidence = opportunity.get("owner_review", {}).get("relocation_evidence", "")
    relocation_text = description + " " + folded(owner_evidence)
    if country == "MA" or re.search(r"morocco.only|maroc uniquement|no relocation|cannot sponsor|no visa sponsorship", relocation_text):
        relocation, relocation_evidence = "fail", "Listing explicitly excludes the required relocation route"
    elif re.search(r"visa sponsorship (?:available|provided|offered)|relocation (?:support|assistance|provided)|aide a la relocation|prise en charge du visa", relocation_text):
        relocation, relocation_evidence = "pass", owner_evidence or "Listing explicitly offers visa/relocation support"
    if any(r["kind"] == "residence" and r["result"] == "fail" and r["mandatory"] for r in evaluated):
        relocation, relocation_evidence = "fail", "Role restricts remote work to existing residents"
    if preferences.get("unknown_sponsorship") == "exclude" and relocation == "unknown":
        exclusions.append("unknown_sponsorship_excluded_by_owner")
    skills = _fact(profile, "skills.technical", confirmed=False) or {}
    matched = [x for x in skills.get("technologies", []) if re.search(r"(?<!\w)" + re.escape(folded(x)) + r"(?!\w)", description)]
    fit = "strong" if len(matched) >= 3 else "plausible" if matched else "weak"
    research = _fact(profile, "research.pqc", confirmed=False)
    research_fit = "plausible" if track == "academic" and (research or matched) else "weak"
    mandatory = [r for r in evaluated if r.get("mandatory") is True]
    eligibility = "fail" if exclusions or any(r["result"] == "fail" for r in mandatory) else (
        "unknown" if any(r["result"] == "unknown" for r in mandatory) else "pass")
    blockers = [f"requirement:{r['kind']}" for r in mandatory if r["result"] == "unknown"]
    if relocation == "unknown":
        blockers.append("relocation_unknown")
    if opportunity.get("deadline_text") and not opportunity.get("deadline_timezone"):
        blockers.append("deadline_timezone_unknown")
    if re.search(r"do not use ai|ai assistance.*prohibited|sans assistance.*ia|no ai.generated", description):
        blockers.append("ai_assistance_prohibited")
    return {"eligibility": eligibility, "relocation": relocation, "relocation_evidence": relocation_evidence,
        "fit": fit, "technical_fit": fit, "research_fit": research_fit, "matched_skills": matched,
        "requirements": evaluated, "exclusions": exclusions, "blockers": sorted(set(blockers)),
        "gaps": [r["reason"] for r in mandatory if r["result"] != "pass"],
        "recommendation": "skip" if eligibility == "fail" or relocation == "fail" else "prepare_for_review",
        "uncertainty": "Explicit unknown requirements require owner evidence",
        "engine_version": "deterministic-v1", "probability": None}
