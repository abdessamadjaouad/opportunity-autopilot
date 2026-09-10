"""Deterministic selection from the reconciled bilingual fact library.

Opportunity text can influence relevance, never create a candidate credential.
Every displayed candidate statement retains its fact identifier in the result.
"""

import re
import unicodedata


FAMILIES = {
    "data_engineering": {
        "label": {"en": "DATA ENGINEERING", "fr": "INGÉNIERIE DATA"},
        "keywords": "Kafka PySpark Spark Airflow PostgreSQL SQL pipeline ETL ELT data warehouse",
        "projects": ["streaming_elt", "data_quality", "healthics"],
    },
    "ai_mlops": {
        "label": {"en": "AI & MLOPS ENGINEERING", "fr": "INGÉNIERIE IA & MLOPS"},
        "keywords": "FastAPI MLflow AI LLM evaluation judge benchmarking XGBoost BERT Docker Azure",
        "projects": ["bert", "cv_matching", "healthics"],
    },
    "software_devops": {
        "label": {"en": "SOFTWARE & DEVOPS ENGINEERING", "fr": "INGÉNIERIE LOGICIELLE & DEVOPS"},
        "keywords": "FastAPI Spring Boot Java React Angular PostgreSQL Docker CI/CD Pytest Azure",
        "projects": ["healthics", "streaming_elt", "cv_matching"],
    },
    "data_bi": {
        "label": {"en": "DATA ANALYTICS & BUSINESS INTELLIGENCE", "fr": "ANALYSE DE DONNÉES & BI"},
        "keywords": "Power BI DAX Power Query SQL PostgreSQL quality dashboard Grafana Excel",
        "projects": ["data_quality", "streaming_elt", "bert"],
    },
    "academic": {
        "label": {"en": "APPLIED AI, DATA SYSTEMS & IOT RESEARCH", "fr": "RECHERCHE APPLIQUÉE EN IA, DATA & IOT"},
        "keywords": "research experiment evaluation AI LLM BERT distributed IoT security Kyber512 LZ4",
        "projects": ["bert", "healthics", "streaming_elt"],
    },
}


def normalize(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)).casefold()


def tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize(value))) - {
        "the", "and", "for", "with", "from", "les", "des", "avec", "pour", "une", "sur", "de", "du", "le", "la"}


class ContentLibrary:
    def __init__(self, profile: dict, opportunity: dict, family: str, language: str):
        self.profile = profile
        self.opportunity = opportunity
        self.family = family
        self.language = language
        self.facts = {item["key"]: item for item in profile.get("facts", []) if isinstance(item, dict) and "key" in item}
        self.selected: dict[str, dict] = {}
        self.blockers: set[str] = set()
        self.warnings: set[str] = set()
        self.relevance = tokens(FAMILIES[family]["keywords"] + " " + opportunity.get("title", "") + " " +
                                opportunity.get("description", "")[:30000])

    def value(self, key: str, *, required: bool = False) -> dict | None:
        fact = self.facts.get(key)
        if fact is None or not isinstance(fact.get("value"), dict):
            if required:
                self.blockers.add("missing_profile_fact:" + key)
            return None
        identity = str(fact.get("id") or key)
        self.selected[identity] = fact
        if fact.get("status") != "confirmed":
            self.blockers.add("unconfirmed_profile_fact:" + identity)
        if not fact.get("sources") and not fact.get("owner_correction") and fact.get("status") != "confirmed":
            self.blockers.add("profile_fact_without_evidence:" + identity)
        return fact["value"]

    def localized(self, value: dict, field: str, key: str, *, required: bool = True):
        mapping = value.get(field)
        result = mapping.get(self.language) if isinstance(mapping, dict) else None
        if not result and required:
            self.blockers.add(f"translation_required:{key}:{field}:{self.language}")
        return result

    def header(self) -> dict:
        name = self.value("identity.name", required=True) or {}
        contact = self.value("identity.contact", required=True) or {}
        location = self.value("identity.location", required=True) or {}
        for field in ("phone", "email", "linkedin", "github"):
            if not contact.get(field):
                self.blockers.add("contact_missing:" + field)
        label = self.localized(location, "label", "identity.location") or ""
        return {"name": name.get("full") or "Profile confirmation required", "contact": contact,
                "location": label, "headline": FAMILIES[self.family]["label"][self.language]}

    def experiences(self, *, max_bullets: int = 2, limit: int = 3) -> list[dict]:
        result = []
        for key in ("experience.dxc", "experience.jesa", "experience.ocp"):
            if len(result) >= limit:
                break
            value = self.value(key)
            if not value:
                continue
            title = self.localized(value, "title", key)
            bullets = self.localized(value, "bullets", key) or []
            if not isinstance(bullets, list) or not all(isinstance(item, str) for item in bullets):
                self.blockers.add("invalid_profile_content:" + key)
                continue
            selected = sorted(sorted(range(len(bullets)), key=lambda index: (
                -len(tokens(bullets[index]) & self.relevance), index))[:max_bullets])
            chosen = [bullets[index] for index in selected]
            if value.get("employment_type") != "internship":
                self.blockers.add("employment_type_requires_review:" + key)
            if self.language == "fr" and any(not re.match(
                    r"^(Conception|Développement|Automatisation|Industrialisation|Amélioration|Optimisation|"
                    r"Création|Mise|Réalisation|Analyse|Évaluation|Intégration|Réduction)", bullet) for bullet in chosen):
                self.blockers.add("french_noun_style_review:" + key)
            result.append({"key": key, "title": title or "", "employer": value.get("employer", ""),
                           "location": self.localized(value, "location", key) or "",
                           "period": self.localized(value, "period_label", key) or "", "bullets": chosen})
        return result

    def education(self) -> list[dict]:
        result = []
        for key in ("education.master", "education.licence"):
            value = self.value(key)
            if not value:
                continue
            if value.get("award_confirmed") is not True:
                self.blockers.add("degree_award_unconfirmed:" + key)
            result.append({"key": key, "title": self.localized(value, "title", key) or "",
                           "institution": self.localized(value, "institution", key) or "",
                           "period": self.localized(value, "period_label", key) or ""})
        return result

    def projects(self, *, limit: int = 3) -> list[dict]:
        result = []
        preferred = FAMILIES[self.family]["projects"]
        for short_key in preferred[:limit]:
            key = "project." + short_key
            value = self.value(key)
            if not value:
                continue
            result.append({"key": key, "title": self.localized(value, "title", key) or "",
                           "description": self.localized(value, "description", key) or ""})
        return result

    def skills(self) -> list[str]:
        value = self.value("skills.technical") or {}
        items = value.get("technologies", [])
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            self.blockers.add("invalid_profile_content:skills.technical")
            return []
        # Advertised skills absent from the library cannot enter the output.
        ordered = sorted(enumerate(items), key=lambda item: (-len(tokens(item[1]) & self.relevance), item[0]))
        return [item for _, item in ordered[:22]]

    def languages(self) -> list[str]:
        value = self.value("identity.languages") or {}
        result = []
        for item in value.get("languages", []):
            label = item.get("label", {}).get(self.language)
            if label:
                result.append(label)
            else:
                self.blockers.add("translation_required:identity.languages:" + self.language)
        return result

    def research(self, *, required: bool = False) -> dict | None:
        value = self.value("research.pqc", required=required)
        if not value:
            return None
        return {"title": self.localized(value, "title", "research.pqc") or "",
                "description": self.localized(value, "description", "research.pqc") or "",
                "limitations": self.localized(value, "limitations", "research.pqc") or ""}

    def owner_document_text(self, kind: str) -> str | None:
        for answer in self.profile.get("answers", []):
            if (answer.get("key") == "document." + kind and answer.get("confirmed") is True
                    and answer.get("context") == self.opportunity.get("id")
                    and isinstance(answer.get("value"), str) and answer["value"].strip()):
                self.warnings.add("owner_authored_document:" + str(answer.get("id", kind)))
                return answer["value"].strip()
        return None
