"""
Pharma Product Matcher
======================
Intelligent pharmaceutical domain NLP matcher for reconciling order sheets,
catalog reports, and procurement records.

Features:
1. Indian Pharma Typo & Glitch Handling:
   - Letter 'O' / 'o' substituted for digit '0' in strengths/volumes (e.g. 2OOML -> 200ML).
   - Concentration normalization: 10% <-> 100 (e.g., TUDIN - 10% <-> TUDIN-100).
2. Pharmaceutical Abbreviation Standardization:
   - E/D, E.D. <-> EYE DROP
   - DS, D.S, DRY SYP <-> DRY SYRUP
   - W.F.I, WFI <-> WATER FOR INJECTION
   - SOL, SOLN <-> SOLUTION
   - SUSP, SUSPEN <-> SUSPENSION
   - TAB, TABS <-> TABLET
   - CAP, CAPS, SOFTGEL <-> CAPSULE
   - INJ, INJECTION <-> INJECTION
3. Packaging & Noise Elimination:
   - ALU ALU, BLISTER, STRIP, BOX, CARTOON, CARTON, BTL
   - Packing expressions: 10*10, 10X1X10, 2X15, 3X10, etc.
4. Multi-tier Matching Pipeline:
   - Tier 1: Exact case-insensitive match
   - Tier 2: Canonical medicine key match
   - Tier 3: Pharma formulation key match (same form & volume)
   - Tier 4: Normalized brand root + strength token match
   - Tier 5: Composition / Salt keyword correlation
   - Tier 6: High-confidence fuzzy similarity (>= 85%) within same dosage form
"""
from __future__ import annotations
import re
from typing import Any
from backend.app.adapters.excel import canonical_medicine_key, pharma_canonical_key
from backend.app.models.entities import Product


def clean_pharma_name(name: str | None) -> str:
    """Normalizes pharmaceutical names, expanding standard abbreviations and eliminating packaging noise."""
    if not name:
        return ""
    s = str(name).strip().upper()

    # 1. Normalize units: M.L -> ML, M.G -> MG, G.M -> GM
    s = s.replace('M.L', 'ML').replace('M.G', 'MG').replace('G.M', 'GM')

    # 2. Fix letter 'O' used for digit '0' in strengths and volumes (e.g., 2OOML, 1OOMG, 5OO)
    s = re.sub(r'(\d+)O+([A-Z]*)', lambda m: m.group(1) + '0' * len(re.findall(r'O', m.group(0))) + m.group(2), s)
    s = re.sub(r'(\b[A-Z\-]+)2OO\b', r'\g<1>200', s)
    s = re.sub(r'(\b[A-Z\-]+)1OO\b', r'\g<1>100', s)
    s = re.sub(r'(\b[A-Z\-]+)5OO\b', r'\g<1>500', s)
    s = re.sub(r'\b2OOML\b', '200ML', s)
    s = re.sub(r'\b1OOML\b', '100ML', s)

    # 3. Standardize packaging & marketing noise:
    s = re.sub(r'\(W\.?F\.?I\.?\)|W\.?F\.?I\.?', ' WFI ', s)
    s = re.sub(r'\bWITH CARTOON\b|\bWITHOUT CARTON\b|\bWITH CARTON\b|\bWITHOUT CARTOON\b', ' ', s)
    s = re.sub(r'\bALU\s*ALU\b|\bBLISTER\b|\bSTRIP\b|\bSTP\b', ' ', s)
    s = re.sub(r'\[.*?\]|\(.*?\)', ' ', s)
    s = re.sub(r'\b\d+\s*[*xX]\s*\d+\s*[*xX]?\s*\d*\b', ' ', s)
    s = re.sub(r'\b\d+\'S\b', ' ', s)

    # 4. Standardize pharma abbreviations
    s = re.sub(r'[\s\-]+E[\/\.]?D\b|[\s\-]+EYE[\s\-]*DROPS?\b', ' EYE DROP ', s)
    s = re.sub(r'[\s\-]+D[\/\.]?S\b|[\s\-]+DRY[\s\-]*SYP\b|[\s\-]+DRY[\s\-]*SYRUP\b', ' DRY SYRUP ', s)
    s = re.sub(r'[\s\-]+SOLN?\b|[\s\-]+SOLUTION\b', ' SOLUTION ', s)
    s = re.sub(r'[\s\-]+SUSPEN?\b|[\s\-]+SUSPENSION\b', ' SUSPENSION ', s)
    s = re.sub(r'[\s\-]+SYP\b|[\s\-]+SYRUP\b', ' SYRUP ', s)
    s = re.sub(r'[\s\-]+TABS?\b|[\s\-]+TABLETS?\b', ' TABLET ', s)
    s = re.sub(r'[\s\-]+CAPS?U?(?:LES?)?\b|[\s\-]+SOFTGEL\b', ' CAPSULE ', s)
    s = re.sub(r'[\s\-]+INJ(?:ECTION)?\b', ' INJECTION ', s)
    s = re.sub(r'[\s\-]+OINT(?:MENT)?\b|[\s\-]+CREAM\b|[\s\-]+GEL\b', ' TOPICAL ', s)

    # 5. Standardize 10% -> 100 for topical antiseptics (e.g. Tudin 10% -> Tudin 100)
    s = re.sub(r'(\b[A-Z\-]+)\s*-\s*10%', r'\g<1> 100', s)

    # Strip trailing hyphens and dashes
    s = re.sub(r'[\s\-]+$', '', s)

    # Collapse internal spaces
    return re.sub(r'\s+', ' ', s).strip()


def extract_dosage_form(name: str) -> str:
    """Classifies dosage form category to avoid cross-form false positives (e.g., tablet matching syrup)."""
    s = (name or "").upper()
    if any(k in s for k in ['INJ', 'INJECTION', 'INFUSION']):
        return 'INJECTABLE'
    elif any(k in s for k in ['CREAM', 'OINT', 'OINTMENT', 'GEL', 'TOPICAL', 'SHAMPOO']):
        return 'TOPICAL'
    elif any(k in s for k in ['SYP', 'SYRUP', 'SUSP', 'SUSPEN', 'DROP', 'DROPS', 'SOLUTION', 'SOLN', 'LOTION', 'ML', 'LTR']):
        return 'LIQUID'
    elif any(k in s for k in ['CAP', 'CAPSULE', 'SOFTGEL']):
        return 'CAPSULE'
    elif any(k in s for k in ['TAB', 'TABLET']):
        return 'TABLET'
    return 'GENERAL'


def fuzzy_similarity_ratio(s1: str, s2: str) -> float:
    """Fast character-level similarity ratio without external C-dependencies."""
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0

    # Token overlap score
    t1 = set(s1.split())
    t2 = set(s2.split())
    if not t1 or not t2:
        return 0.0
    intersection = len(t1 & t2)
    token_score = (2.0 * intersection) / (len(t1) + len(t2))

    # Longest common subsequence length ratio
    l1, l2 = len(s1), len(s2)
    prev = [0] * (l2 + 1)
    for c1 in s1:
        curr = [0] * (l2 + 1)
        for j, c2 in enumerate(s2):
            if c1 == c2:
                curr[j + 1] = prev[j] + 1
            else:
                curr[j + 1] = max(curr[j], prev[j + 1])
        prev = curr
    lcs = prev[l2]
    char_score = (2.0 * lcs) / (l1 + l2)

    return 0.6 * token_score + 0.4 * char_score


class PharmaProductMatcher:
    """
    High-accuracy pharmaceutical entity resolver.
    Matches uploaded order names to database catalog products.
    """

    def __init__(self, products: list[Product]) -> None:
        self.products = products
        self._index: list[dict[str, Any]] = []
        self._build_index()

    def _build_index(self) -> None:
        for p in self.products:
            p_name = p.product_name or ""
            clean_name = clean_pharma_name(p_name)
            ck = canonical_medicine_key(p_name)
            pk = pharma_canonical_key(p_name)
            form = extract_dosage_form(p_name)
            tokens = set(re.findall(r'[A-Z0-9]+', clean_name))

            self._index.append({
                'product': p,
                'raw': p_name.upper().strip(),
                'clean': clean_name,
                'ck': ck,
                'pk': pk,
                'form': form,
                'tokens': tokens,
            })

    def match(self, target_name: str, composition: str = "") -> tuple[Product | None, str, float]:
        """
        Finds the best matching catalog product for target_name.

        Returns:
            tuple of (Matched Product or None, Match Method Description, Confidence Score [0.0 - 1.0])
        """
        if not target_name:
            return None, "Empty name", 0.0

        raw_target = str(target_name).strip().upper()
        clean_target = clean_pharma_name(target_name)
        target_cks = {k for k in [canonical_medicine_key(target_name), canonical_medicine_key(clean_target)] if k}
        target_pks = {k for k in [pharma_canonical_key(target_name), pharma_canonical_key(clean_target)] if k}
        target_form = extract_dosage_form(target_name)
        target_tokens = set(re.findall(r'[A-Z0-9]+', clean_target))

        # ── Tier 1: Exact Case-Insensitive String Match ──────────────────────
        for item in self._index:
            if item['raw'] == raw_target:
                return item['product'], "Exact Match", 1.0

        # ── Tier 2: Canonical Medicine Key Match ─────────────────────────────
        for item in self._index:
            if item['ck'] and item['ck'] in target_cks:
                # Check dosage form compatibility if forms are explicitly stated
                if target_form == 'GENERAL' or item['form'] == 'GENERAL' or target_form == item['form'] or {target_form, item['form']} == {'TOPICAL', 'LIQUID'}:
                    return item['product'], "Canonical Medicine Key", 0.98

        # Canonical prefix match (e.g., BRAXIN100 <-> BRAXIN) within same dosage form
        for item in self._index:
            if not item['ck'] or target_form != item['form']:
                continue
            for ck in target_cks:
                if len(ck) >= 5 and len(item['ck']) >= 5:
                    if ck.startswith(item['ck']) or item['ck'].startswith(ck):
                        return item['product'], "Canonical Prefix Match", 0.91

        # ── Tier 3: Pharma Formulation Key Match ────────────────────────────
        for item in self._index:
            if item['pk'] and item['pk'] in target_pks:
                return item['product'], "Pharma Formulation Key", 0.95

        # ── Tier 4: Clean Pharma Name Substring / Exact Clean Match ──────────
        for item in self._index:
            if item['clean'] == clean_target:
                return item['product'], "Normalized Pharma Name", 0.93

        # ── Tier 5: Strong Token Match (Brand + Strength + Form) ─────────────
        # Find best candidate by token overlap, ensuring compatible dosage form
        best_candidate = None
        best_score = 0.0

        for item in self._index:
            # Prevent matching tablets with syrups or drops (allow gel cross-compatibility)
            if target_form != 'GENERAL' and item['form'] != 'GENERAL' and target_form != item['form']:
                if {target_form, item['form']} != {'TOPICAL', 'LIQUID'}:
                    continue

            # Token overlap
            common = len(target_tokens & item['tokens'])
            if not common:
                continue

            # Proportion of target tokens covered
            target_coverage = common / len(target_tokens) if target_tokens else 0.0
            catalog_coverage = common / len(item['tokens']) if item['tokens'] else 0.0
            combined = 0.7 * target_coverage + 0.3 * catalog_coverage

            if combined > best_score:
                best_score = combined
                best_candidate = item['product']

        if best_candidate and best_score >= 0.75:
            return best_candidate, f"Token Match ({best_score:.0%})", round(best_score, 2)

        # ── Tier 6: High-Confidence Fuzzy Similarity ─────────────────────────
        fuzzy_candidate = None
        fuzzy_score = 0.0

        for item in self._index:
            if target_form != 'GENERAL' and item['form'] != 'GENERAL' and target_form != item['form']:
                if {target_form, item['form']} != {'TOPICAL', 'LIQUID'}:
                    continue

            sim = fuzzy_similarity_ratio(clean_target, item['clean'])
            if sim > fuzzy_score:
                fuzzy_score = sim
                fuzzy_candidate = item['product']

        if fuzzy_candidate and fuzzy_score >= 0.82:
            return fuzzy_candidate, f"Fuzzy NLP ({fuzzy_score:.0%})", round(fuzzy_score, 2)

        # ── Tier 7: Active Salt / Composition Search Fallback ────────────────
        if composition and len(composition) > 5:
            comp_clean = clean_pharma_name(composition)
            comp_tokens = set(re.findall(r'[A-Z0-9]+', comp_clean))
            # Find product with overlapping salt tokens and similar target brand prefix
            for item in self._index:
                if target_form != 'GENERAL' and item['form'] != 'GENERAL' and target_form != item['form']:
                    continue
                # Brand prefix match (first 4 chars of target name)
                prefix = clean_target[:4]
                if len(prefix) >= 3 and prefix in item['clean']:
                    common_salt = len(comp_tokens & item['tokens'])
                    if common_salt >= 1:
                        return item['product'], "Composition Salt Match", 0.80

        return None, "No Match", 0.0
