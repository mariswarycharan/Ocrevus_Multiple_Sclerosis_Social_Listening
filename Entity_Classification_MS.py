#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
 MS / OCREVUS TWITTER ENTITY-BUCKET CLASSIFIER
 Fully rule-based: regex + fuzzy matching + weighted keyword scoring.
 No embeddings, no vector DB, no LLM.
==============================================================================

WHAT IT DOES
------------
1. clean_tweet(raw)      -> cleaned text (RT prefix, URLs, @handles removed,
                            whitespace collapsed; hashtags + emojis kept)
2. classify_post(clean)  -> exactly ONE of 5 entity buckets:

      1. Ocrevus Only            Ocrevus is mentioned, no competitor drug.
                                 (Posts that are a general MS story with
                                 Ocrevus in the background also land here.)
      2. Ocrevus vs. Competitor  Ocrevus AND at least one competitor drug.
      3. Competitor Only         Competitor drug(s) only, no Ocrevus.
                                 (General MS story + competitor lands here.)
      4. General MS (No Drug)    About multiple sclerosis, no in-scope drug.
                                 Includes posts that never say "MS" but are
                                 clearly about it (MS hug, Lhermitte's,
                                 lesions, DMT, relapse + neuro ...).
      5. Noise / Others          Not about MS and no in-scope drug
                                 (Microsoft, Mississippi, Master of Science,
                                 Ms. Smith, 200 ms, MS Dhoni, rituximab for
                                 lymphoma, generic chatter ...).

3. Drug Mentions column  -> every in-scope drug found in the post, reported
                            by BRAND name, comma separated. Our brand first,
                            then the competitors in alphabetical order.
4. Confidence column     -> 0.30 – 0.99, built from how the drug was detected
                            (exact / variant / misspelling / fuzzy) or how
                            explicit the MS evidence is, minus conflicting
                            (noise) evidence.
5. Reason column         -> plain-English explanation with the evidence.

HOW A DECISION IS MADE (per post)
---------------------------------
  a) Drug detection   : exact brand/generic names -> spelling variants
                        (repeated letters, l33t, s p a c e d, split words)
                        -> misspelling regexes -> fuzzy Damerau-Levenshtein
                        on every token (incl. hashtags) -> context-only short
                        forms ("tec", "ocr", "ritux"...).
  b) Validation       : weak matches (2-edit fuzzy, short forms) need MS or
                        treatment context; drugs used for a non-MS disease
                        (lymphoma, Crohn's...) are discarded.
  c) Bucket           : our + competitor  -> Ocrevus vs. Competitor
                        our only          -> Ocrevus Only
                        competitor only   -> Competitor Only
                        no drug           -> General MS  or  Noise / Others
  d) General MS vs Noise uses: strong MS terms, every "MS"/"ms" token
                        disambiguated from its neighbours, MS-specific
                        symptom/clinical "hints", non-MS noise meanings and
                        other-disease counter-evidence.

HOW TO RUN
----------
  Edit the variables at the top of main() (bottom of this file):
      INPUT_FILE, OUTPUT_FILE, TEXT_COLUMN, SHEET_NAME, ...
  then run:   python Entity_Classification_MS.py

  pip install pandas openpyxl rapidfuzz      (rapidfuzz optional but faster)

EVERYTHING YOU MAY WANT TO TUNE IS IN THE "CONFIGURATION" SECTION BELOW.
Keyword entries are regex fragments, matched case-insensitively with word
boundaries added automatically. A plain word works as-is. A SPACE inside an
entry means "optional space / hyphen / underscore", so "brain fog" also
matches "brain-fog" and "brainfog". In drug-context/comparison patterns you
can use the placeholders {DRUG} (any drug), {OUR} (our drug), {COMP}
(competitor).
==============================================================================
"""

from __future__ import annotations

import html
import logging
import math
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import pandas as pd
except ImportError:  # classification still works without pandas
    pd = None

try:
    from rapidfuzz.distance import DamerauLevenshtein as _RF_DL

    def _edit_distance(a: str, b: str, cutoff: int) -> int:
        return _RF_DL.distance(a, b, score_cutoff=cutoff)

except ImportError:  # pure-python fallback (Optimal String Alignment)

    def _edit_distance(a: str, b: str, cutoff: int) -> int:
        if abs(len(a) - len(b)) > cutoff:
            return cutoff + 1
        la, lb = len(a), len(b)
        prev2 = None
        prev = list(range(lb + 1))
        for i in range(1, la + 1):
            cur = [i] + [0] * lb
            row_min = cur[0]
            for j in range(1, lb + 1):
                cost = 0 if a[i - 1] == b[j - 1] else 1
                cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
                if (i > 1 and j > 1 and a[i - 1] == b[j - 2]
                        and a[i - 2] == b[j - 1] and prev2 is not None):
                    cur[j] = min(cur[j], prev2[j - 2] + 1)
                row_min = min(row_min, cur[j])
            if row_min > cutoff:
                return cutoff + 1
            prev2, prev = prev, cur
        return min(prev[lb], cutoff + 1)


log = logging.getLogger("ms_classifier")

# =============================================================================
# =============================  CONFIGURATION  ===============================
# =============================================================================

# ---------------------------------------------------------------- 1. I/O ----
OUR_BRAND_LABEL = "Ocrevus"          # used in bucket names and reasons

INPUT_TEXT_COLUMN: Optional[str] = None   # None = auto-detect
TEXT_COLUMN_CANDIDATES = [
    "post content", "twitter post content", "tweet content", "post_content",
    "tweet", "tweet text", "message", "text", "full_text", "content", "post",
    "body", "snippet",
]
CLEAN_TEXT_COLUMN = "Cleaned Post Content"
BUCKET_COLUMN = "Entity Bucket"
DRUG_MENTIONS_COLUMN = "Drug Mentions"
CONFIDENCE_COLUMN = "Confidence"
REASON_COLUMN = "Reason"
# Internal scores (MS Score, Noise Score ...) are NOT written to the output.
# Switch this on only if you want to tune thresholds.
WRITE_INTERNAL_SCORE_COLUMNS = False
LOW_CONFIDENCE_THRESHOLD = 0.75       # rows below go to "Low Confidence Review"
# v2: was 0.60 - no row ever scored below 0.60 and the 0.60-0.75 band was only
# ~54% accurate in the manual review, so that band now goes to review.
# Drugs used for a non-MS disease (e.g. "rituximab for lymphoma") are ignored
# for the bucket. Should they still be listed in "Drug Mentions"?
DRUG_MENTIONS_INCLUDE_NON_MS_USE = False

INPUT_COLUMNS_TO_KEEP: Optional[List[str]] = ['Post ID', 'Group_ID','Repeated_Count', 'SenderUserId', 'SenderScreenName' ,'SenderProfileLink', 'Sender Followers Count'
         , 'SenderInfluencerScore', 'SenderAge', 'SenderGender', 'CreatedTime','CountryCode','Message' ,'Sentiment',]

# ------------------------------------------------------------ 2. BUCKETS ----
BUCKETS = {
    "OUR_ONLY":    f"{OUR_BRAND_LABEL} Only",
    "OUR_VS_COMP": f"{OUR_BRAND_LABEL} vs. Competitor",
    "COMP_ONLY":   "Competitor Only",
    "GENERAL_MS":  "General MS (No Drug)",
    "NOISE":       "Noise / Others",
}

# -------------------------------------------------------------- 3. DRUGS ----
# The dictionary KEY is the brand name written to the "Drug Mentions" column.
# names              : exact spellings (brand, generic, sub-brands,
#                      biosimilars that roll up to the brand). Names of 6+
#                      letters are also found INSIDE words/hashtags
#                      (#OcrevusDay, ocrevusinfusion).
# misspelling_regex  : extra regexes for common misspellings / phonetic forms.
# context_names      : short forms / abbreviations that are only trusted when
#                      the post also carries MS or treatment context
#                      ("on tec for 3 yrs", "OCR infusion").
# ms_specific        : brand is (almost) only used for MS.
# requires_ms_context: drug is mostly used outside MS (e.g. rituximab), so it
#                      only counts when the post also carries an MS signal.
OUR_DRUGS: Dict[str, dict] = {
    "Ocrevus": {
        "names": ["ocrevus", "ocrelizumab", "zunovo", "ocrevus zunovo",
                  "ocrevus sc", "ocrevus subq", "ocrevus iv", "ocrev",
                  "ocrelizumab hyaluronidase", "ocrelizumab ocsq"],
        "misspelling_regex": [
            r"[oa][ck]{1,2}h?r[aeiouy]{1,2}v[aeiouy]{1,3}[sz]{1,3}",   # ocravus, okrevuz
            r"o[ck]r[aeiy]l[aeiy]?[sz][aeiou]{1,2}m[aeiou]?[bp]",     # ocrilizumab
            r"zun[ao]v[ao]",
            r"o[ck]r[ae]v[ae]?s",                                     # ocrevs, ocravs
        ],
        "context_names": ["ocr", "ocre", "ocrel", "ocz"],
        "ms_specific": True,
        "requires_ms_context": False,
    },
}

# The three named competitors
COMPETITOR_DRUGS: Dict[str, dict] = {
    "Kesimpta": {
        "names": ["kesimpta", "ofatumumab", "kesimpta pen", "kesimpta sensoready"],
        "misspelling_regex": [r"k[aei]s+[iey]m+[pb]?t+[ae]h?",
                              r"k[aei]s[iey]mp?a",
                              r"of+[ae]t[uo]m[uo]m[ae]b"],
        "context_names": ["kesi"],
        "ms_specific": True, "requires_ms_context": False,
    },
    "Briumvi": {
        "names": ["briumvi", "ublituximab"],
        "misspelling_regex": [r"br[iy]?[ou]?m+v[iy]e?", r"ubl[iy]t[uo]x[iy]m[ae]b"],
        "context_names": ["brium", "ubli"],
        "ms_specific": True, "requires_ms_context": False,
    },
    "Tysabri": {
        "names": ["tysabri", "natalizumab", "tyruko"],
        "misspelling_regex": [r"t[iy]s[ae]h?br[iy]e?", r"n[ae]t[ae]l[iy][sz][uo]m[ae]b"],
        "context_names": ["tysa", "tys", "tyz"],
        "ms_specific": False, "requires_ms_context": False,   # also Crohn's
    },
}

# Other MS disease-modifying therapies, counted as competitors when ON.
# If you set it to False, these names stop counting as competitors and are
# used only as strong MS signals.
INCLUDE_EXTENDED_COMPETITORS = True
EXTENDED_COMPETITOR_DRUGS: Dict[str, dict] = {
    "Tecfidera": {"names": ["tecfidera", "dimethyl fumarate"],
                  "misspelling_regex": [r"t[ei]c?k?[fh]?[iy]d[ae]ra", r"t[ei]k[fh][iy]d[ae]ra"],
                  "context_names": ["tec", "tecfi", "dmf"],
                  "ms_specific": True, "requires_ms_context": False},
    "Vumerity": {"names": ["vumerity", "diroximel fumarate"],
                 "misspelling_regex": [r"v[uo]m[ae]r[iy]t[iy]"],
                 "context_names": [],
                 "ms_specific": True, "requires_ms_context": False},
    "Bafiertam": {"names": ["bafiertam", "monomethyl fumarate"],
                  "misspelling_regex": [r"b[ae]f[iy]?[ae]rt[ae]m"],
                  "context_names": [],
                  "ms_specific": True, "requires_ms_context": False},
    "Gilenya": {"names": ["gilenya", "fingolimod", "tascenso"],
                "misspelling_regex": [r"g[iy]l[ae]n[iy]?y?a", r"f[iy]ng[oa]l[iy]m[oa]d"],
                "context_names": ["fingo"],
                "ms_specific": True, "requires_ms_context": False},
    "Mayzent": {"names": ["mayzent", "siponimod"],
                "misspelling_regex": [r"m[ae][iy]?z[ae]nt(?![a-z])", r"s[iy]p[oa]n[iy]m[oa]d"],
                "context_names": [],
                "ms_specific": True, "requires_ms_context": False},
    "Zeposia": {"names": ["zeposia", "ozanimod"],
                "misspelling_regex": [r"z[ae]p[oa]s[iy]a", r"[oa]z[ae]n[iy]m[oa]d"],
                "context_names": [],
                "ms_specific": False, "requires_ms_context": False},   # also UC
    "Ponvory": {"names": ["ponvory", "ponesimod"],
                "misspelling_regex": [r"p[oa]nv[oa]r[iy]"],
                "context_names": [],
                "ms_specific": True, "requires_ms_context": False},
    "Aubagio": {"names": ["aubagio", "teriflunomide"],
                "misspelling_regex": [r"a[uo]?b[ae]g[iy]o", r"t[ae]r[iy]fl[uo]n[oa]m[iy]de?"],
                "context_names": [],
                "ms_specific": True, "requires_ms_context": False},
    "Mavenclad": {"names": ["mavenclad", "cladribine"],
                  "misspelling_regex": [r"m[ae]v[ae]n ?cl[ae]d", r"cl[ae]dr[iy]b[iy]ne?"],
                  "context_names": ["maven"],
                  "ms_specific": True, "requires_ms_context": False},
    "Lemtrada": {"names": ["lemtrada", "alemtuzumab"],
                 "misspelling_regex": [r"l[ae]mt?r[ae]d[ae]", r"[ae]l[ae]mt[uo]z[uo]m[ae]b"],
                 "context_names": ["lemt"],
                 "ms_specific": False, "requires_ms_context": False},   # also Campath
    "Copaxone": {"names": ["copaxone", "glatiramer", "glatiramer acetate", "glatopa"],
                 "misspelling_regex": [r"c[ao]p[ae]x[oa]ne?", r"gl[ae]t[iy]r[ae]m[ae]r"],
                 "context_names": ["copax"],
                 "ms_specific": True, "requires_ms_context": False},
    "Avonex": {"names": ["avonex", "avonex pen"],
               "misspelling_regex": [r"av[oa]n[ae]x"],
               "context_names": [],
               "ms_specific": True, "requires_ms_context": False},
    "Rebif": {"names": ["rebif", "rebif rebidose"],
              "misspelling_regex": [r"reb[iy]ff?(?![a-z])"],
              "context_names": [],
              "ms_specific": True, "requires_ms_context": False},
    "Plegridy": {"names": ["plegridy", "peginterferon", "peginterferon beta 1a",
                           "peg interferon"],
                 "misspelling_regex": [r"pl[ae]gr[iy]d[iy]"],
                 "context_names": [],
                 "ms_specific": True, "requires_ms_context": False},
    "Betaseron": {"names": ["betaseron", "betaferon", "extavia", "interferon beta 1b"],
                  "misspelling_regex": [r"b[ae]t[ae][sf][ae]r[oa]n"],
                  "context_names": [],
                  "ms_specific": True, "requires_ms_context": False},
    # Unbranded interferon mentions cannot be tied to ONE brand (Avonex,
    # Rebif, Plegridy and Betaseron are all interferons), so they get this
    # label. Rename the key if your team prefers another wording.
    "Interferon beta (generic)": {
        "names": ["interferon beta", "beta interferon", "interferon beta 1a",
                  "interferon", "interferons", "ifn beta", "ifnb"],
        "misspelling_regex": [r"[iy]nt[ae]rf[ae]r[oa]ns?(?![a-z])"],
        # v2: bare "ifn" removed - in this data it was always IFN-gamma (a cytokine).
        # "ifn beta" / "ifnb" (names above) are still matched.
        "context_names": [],
        "ms_specific": False, "requires_ms_context": True},     # hepatitis, cancer
    "Rituxan": {"names": ["rituxan", "rituximab", "truxima", "ruxience",
                          "riabni", "mabthera", "ritux"],
                "misspelling_regex": [r"r[iy]t[uo]x[iy]m[ae]b", r"r[iy]t[uo]x[ae]n"],
                "context_names": ["rtx"],
                "ms_specific": False, "requires_ms_context": True},
}

# --------------------------------------------------------- 4. FUZZY MATCH ---
FUZZY_ENABLED = True
FUZZY_MIN_TOKEN_LEN = 5
FUZZY_MAX_DISTANCE_BY_LEN = [(6, 1), (9, 2), (99, 2)]  # (max name len, edits)
FUZZY_REQUIRE_SAME_FIRST_LETTER = True   # relaxed for 1-edit typos on 7+ letter names
FUZZY_TOKEN_BLOCKLIST = {   # real words / other drugs that look like ours
    # other monoclonal antibodies
    "omalizumab", "eculizumab", "ravulizumab", "vedolizumab", "certolizumab",
    "efalizumab", "inebilizumab", "satralizumab", "tocilizumab", "obinutuzumab",
    "belimumab", "anifrolumab", "daclizumab", "pembrolizumab", "nivolumab",
    "atezolizumab", "durvalumab", "trastuzumab", "pertuzumab", "bevacizumab",
    "ocaratuzumab", "sarilumab", "mepolizumab", "reslizumab", "benralizumab",
    "dupilumab", "ustekinumab", "secukinumab", "ixekizumab", "infliximab",
    "adalimumab", "cetuximab", "golimumab", "lecanemab", "aducanumab",
    "donanemab", "natalie", "natalia",
    # everyday words that sit 1-2 edits from a drug name
    "galena", "rebel", "rebid", "relief", "trauma", "sabrina", "kismet",
    "avocet", "ocelot", "orbits", "process", "thesis", "chorus", "rebus",
    "rebuff", "rebirth", "abacus", "scrabble", "tyranny", "tyrant", "maven",
    "mavens", "copay", "copays", "copper", "cooper", "avenue", "annex",
    "zeppelin", "ponder", "podcast", "mayhem", "lemonade", "escapade",
    "interior", "inferno", "interface", "interfere", "interfered",
    "interferes", "interfering", "intern", "internal", "cocoa", "copaxone",
    "abbott", "tribal", "tribune", "remedy", "comedy", "gilead", "glorious",
    # found by scanning a 300k-word English dictionary
    "betatron", "copaene", "extatic", "exuvia", "gileno", "gillenia",
    "interferent", "interferer", "interferant", "lestrad", "manent",
    "manrent", "margent", "mather", "mathers", "mathura", "mauther", "maynt",
    "mayten", "ochreous", "pleurisy", "ritual", "rituals", "trulia",
    "zenobia", "ocrea", "rebia", "redif", "ocher", "ochre", "ochres",
}

# ------------------------------------------------- 5. MS DISEASE KEYWORDS ---
# Strong, unambiguous MS terms (no bare "MS" here – bare MS is handled by the
# token disambiguator in section 6).
MS_STRONG_KEYWORDS: Dict[str, float] = {
    r"multiple sclerosis": 4.0,
    r"multiplesclerosis": 4.0,
    r"mult[iy]?p?l?e? ?s[ck]?h?l[ae]r[aeo]{1,2}s[iey]s": 4.0,   # misspellings
    r"rrms|ppms|spms|aspms|rpms|cis ms": 3.5,
    r"rms": 2.5,                     # weaker: also "RMS power", "RMS Titanic"
    r"relapsing remitting|relapsing ms|relapsing forms? of ms": 3.5,
    r"primary progressive|secondary progressive|progressive ms": 3.5,
    r"clinically isolated syndrome|radiologically isolated syndrome": 3.5,
    r"ms'?ers?|ms'?ies|pwms|ms related|msiversary|diagnosiversary": 3.5,
    r"ms(?:warriors?|fighters?|awareness\w*|aware|community|strong|life|sucks|"
    r"hug|society|research|moms?|mums?|dads?|twitter|spoonies?|crew|sisters?|"
    r"brothers?|brain|fatigue|diagnosis|journey|relapse|patients?|symptoms?|"
    r"family|friends|support|nurse|clinic|treatment|day|week|month|uk|trust|"
    r"chat|chats|bff|pals?|tribe|squad|walk|bike|cure|advocate\w*|"
    r"survivor\w*|lesions?|flare\w*|pain|legs|meds|dmts?)": 3.5,
    r"(?:livingwith|beat|end|cure|stop|fuck|kick|fight|fighting|battling|world|"
    r"kissgoodbyeto|wewillend|screw|hate|thisis)ms(?:day|week|month|awareness)?": 3.5,
    r"national ms society|nmss|msaa|ectrims|actrims|cmsc|msif|ms trust|"
    r"shift\.ms|overcoming ms|ms[- ]uk|mymsaa|ms society|ms canada|ms australia|"
    r"ms ireland|dmsg|ms foundation|msf|can do ms|ms association": 3.5,
    # other languages
    r"esclerosis multiple|esclerose multipla|sclerose en plaques|"
    r"sclerosi multipla|multiple sklerose|multiple sclerose|multipel sklerose|"
    r"multipel skleros|stwardnienie rozsiane|roztrousena skleroza|"
    r"rasseyannyy skleroz|рассеянный склероз|多発性硬化症|多发性硬化|"
    r"다발성 경화증|التصلب المتعدد|मल्टीपल स्क्लेरोसिस": 4.0,
}

# Weaker clinical / lifestyle hints that suggest MS without naming it.
# Used to rescue posts that never say "MS" (feelings, side effects, symptoms).
MS_HINT_KEYWORDS: Dict[str, float] = {
    # very MS-specific clinical words
    r"demyelinat\w*|remyelinat\w*|myelin": 2.0,
    r"optic neuritis|lhermitte'?s?|uhthoff'?s?|ms hug|edss|pseudo ?relapses?|"
    r"pseudo ?exacerbations?": 2.5,
    # v2: "therapies" was not matched by the old "(?:therapy|...)s?"
    r"disease modifying\w*|dmts?|high efficacy (?:therap(?:y|ies)|treatments?|dmts?)": 2.0,
    # v2: trial / clinical progression language (CAR-T, BTKi, anti-CD20 read-outs)
    r"(?:confirmed )?disability (?:scores?|progression|worsening|accumulation)|"
    r"progression independent of relapse|annuali[sz]ed relapse rates?": 1.5,
    r"(?:brain|spinal|spine|cervical|thoracic|new|active|enhancing|"
    r"gadolinium|gad|t1|t2|black hole|periventricular|juxtacortical) lesions?|"
    r"lesion load|lesions? on my (?:brain|spine|mri)": 2.0,
    r"oligoclonal bands?|mcdonald criteria|neurofilament|nfl levels?|"
    r"jc ?virus|jcv|pml risk|pml": 2.0,
    r"b ?cell depletion|anti ?cd20|cd20|btk inhibitors?|tolebrutinib|"
    r"fenebrutinib|evobrutinib|remibrutinib|frexalimab": 2.0,
    r"neuroimmunolog\w*|ahsct|hsct": 2.0,
    r"transverse myelitis": 1.5,   # v2: was 2.0 - TM is its own condition, must not decide MS alone
    r"wahls(?: protocol)?|swank diet|overcoming ms": 2.0,
    # MS hug / band-like sensation, symptom descriptions without the word MS
    r"squeez\w* (?:around |across )?(?:my )?(?:ribs?|rib cage|chest|torso|waist)|"
    r"band (?:around|across) my (?:chest|ribs?|waist|torso)|"
    r"(?:boa|corset) (?:constrictor|feeling)": 2.0,
    r"electric (?:shocks?|zaps?) (?:down|through|in) my (?:spine|back|neck|arms?|legs?)|"
    r"zaps? down my (?:spine|back)": 2.0,
    r"heat (?:makes|made|is making) (?:my )?(?:symptoms?|legs?|everything|fatigue) worse|"
    r"heat (?:intolerance|sensitivity)|cog fog|cognitive fog": 1.5,
    r"(?:can'?t|cannot|couldn'?t|can not) feel my (?:legs?|feet|foot|hands?|arms?|face|toes)": 1.5,
    r"(?:legs?|feet|hands?|arms?|face|toes|fingers) (?:went|go|going|are|is|feel|feels|"
    r"felt|keep going) (?:numb|dead|heavy|weak|tingly|buzzy|like jelly)": 1.5,
    r"spasticity|foot drop|drop foot|baclofen|tizanidine|ampyra|fampridine|"
    r"dalfampridine|fampyra": 1.5,
    r"solu ?medrol|iv steroids|steroid infusion|plasmapheresis|plasma exchange": 1.5,
    r"lumbar puncture|spinal tap": 1.5,
    r"bladder (?:urgency|issues|problems|retention)|urinary urgency|self ?cath\w*": 1.25,
    r"my neuro|neuro (?:appt|appointment|visit|nurse)|neurologists?|neurology|"
    r"neuro (?:said|says|wants|thinks|suggested|recommended)": 1.25,
    r"trigeminal neuralgia|double vision|pins and needles|numbness|tingling|"
    r"word finding|losing my words": 1.0,
    r"brain fog|invisible (?:illness|disability|disease)|autoimmune": 1.0,
    r"mri|mri scan|relapses?|relapsing|mobility aids?|rollator|walking stick": 1.0,
    r"central nervous system|nerve damage|neuropathic pain|nerve pain": 1.0,
    r"gabapentin|pregabalin|lyrica|modafinil|amantadine|ldn|low dose naltrexone": 0.75,
    r"fatigue|wheelchair|infusions?|spoonie|chronic illness|cane|"
    r"walking aid|mobility scooter": 0.5,
    r"numb|vertigo|bladder|tremors?|balance issues|dropping things|"
    r"heavy legs|weak legs|leg weakness|buzzing|vibrating feeling": 0.5,
}
HINT_SCORE_THRESHOLD = 2.5      # sum of hint weights needed (no MS word)
HINT_MIN_DISTINCT = 2           # ...from at least this many different terms
HINT_SINGLE_STRONG_WEIGHT = 2.0 # or ONE hint at least this strong

# Other diseases / situations that explain generic symptoms better than MS.
# Only used when MS is NOT named explicitly (hint-only posts).
OTHER_CONDITION_KEYWORDS: Dict[str, float] = {
    r"diabet\w*|diabetic neuropathy|blood sugar|insulin|a1c": 2.0,
    r"long ?covid|covid|post ?viral|vaccine side effects?": 1.5,
    r"fibromyalgia|fibro|me ?/ ?cfs|chronic fatigue syndrome|cfs|pots|"
    r"ehlers ?danlos|eds|hypermobil\w*": 2.0,
    r"lupus|rheumatoid|arthritis|sjogren'?s|psoriatic|ankylosing": 2.0,
    r"cancer|chemo\w*|tumou?rs?|oncolog\w*|radiation therapy": 2.0,
    r"pregnan\w*|trimester|postpartum|morning sickness": 1.5,
    r"stroke|tia|migraines?|parkinson'?s?|als|amyotrophic|lou gehrig'?s?|"
    r"guillain|gbs|lyme|myasthenia|epilep\w*|seizures?|alzheimer'?s?|dementia": 2.0,
    r"amyotrophic lateral sclerosis|lateral sclerosis|systemic sclerosis|"
    r"tuberous sclerosis|scleroderma|otosclerosis|atherosclerosis": 3.0,
    r"nmosd|neuromyelitis|mogad": 2.5,
    r"carpal tunnel|sciatica|herniated disc|slipped disc|pinched nerve|"
    r"b12 deficiency|anemia|anaemia|thyroid|hashimoto'?s?": 1.5,
    r"hangover|drunk|hungover|gym session|leg day|workout|marathon|"
    r"slept on my (?:arm|hand|leg)": 1.5,
    r"anxiety attack|panic attack": 1.0,
}

# -------------------------------------- 6. "MS" TOKEN DISAMBIGUATION LISTS ---
# For every standalone "MS"/"Ms"/"ms" the neighbours decide disease vs noise.
# STRONG neighbours decide "disease" immediately; WEAK neighbours (function
# words like "is", "and") only count when nothing noisy is nearby.
MS_DISEASE_NEXT_STRONG = {
    "warrior", "warriors", "fighter", "fighters", "community", "awareness",
    "society", "trust", "patient", "patients", "sufferer", "sufferers",
    "symptom", "symptoms", "fatigue", "hug", "diagnosis", "relapse", "relapses",
    "flare", "flares", "flareup", "flareups", "lesion", "lesions", "research",
    "researchers", "nurse", "nurses", "clinic", "specialist", "specialists",
    "neurologist", "neurologists", "center", "centre", "drug", "drugs",
    "medication", "medications", "meds", "treatment", "treatments", "therapy",
    "therapies", "dmt", "dmts", "life", "journey", "sucks", "brain", "walk",
    "mom", "mum", "dad", "family", "friends", "sister", "sisters", "crew",
    "related", "attack", "attacks", "exacerbation", "progression", "pain",
    "spasms", "spasticity", "fog", "legs", "diagnosed", "dx", "cure",
    "progressing", "progressive", "stole", "steals", "affects", "affected",
    "support", "charity", "ward", "unit", "neuro", "neurology", "survivor",
    "survivors", "advocate", "advocates", "advocacy", "kills", "warrior's",
    "hugs", "fatigued", "cognition", "cog", "bladder", "vision", "tremors",
    "disability", "mri", "scan", "infusion", "infusions", "remission",
    "diagnoses", "nerve", "nerves", "flaring", "flared", "symptomatic",
    "awareness-month", "aware", "strong", "sufferers", "pals", "tribe",
}
MS_DISEASE_NEXT_WEAK = {
    "is", "has", "was", "makes", "made", "took", "takes", "and", "or", "since",
    "for", "doesn't", "doesnt", "won't", "can", "can't", "isn't", "will",
    "day", "week", "month", "people", "folks", "care", "team", "group",
    "groups", "story", "stories", "really", "just", "hit", "hits", "gets",
    "got", "keeps", "means", "never", "always", "still", "but", "too",
    "because", "so", "then", "now", "this", "it", "isnt", "wont", "cant",
}
MS_DISEASE_PREV_STRONG = {
    "my", "with", "have", "having", "diagnosed", "dx", "dxd", "dx'd", "his",
    "her", "against", "fight", "fighting", "beat", "beating", "cure", "cured",
    "curing", "living", "progressive", "relapsing", "active", "pediatric",
    "paediatric", "aggressive", "benign", "treating", "treat", "treats",
    "managing", "manage", "mom's", "mum's", "dad's", "wife's", "husband's",
    "sister's", "brother's", "friend's", "suspected", "probable", "untreated",
    "advanced", "hate", "fuck", "fucking", "stupid", "damn", "bloody",
    "battling", "battle", "suffering", "suffer", "suffers", "lives",
    "remitting", "secondary", "primary", "tumefactive", "pwms", "cursed",
}
# v2: two-word look-back that is a STRONG disease context ("Ms Blair suffers
# from MS": "from" alone is only weak, so a title elsewhere used to win).
MS_DISEASE_PREV2_STRONG = {
    "suffers from", "suffering from", "suffer from", "suffered from",
    "diagnosed with", "living with", "lives with", "lived with", "battling with",
    "struggling with", "struggles with", "fighting with", "died from", "died of",
    "complications of", "complications from", "symptoms of", "diagnosis of",
}
MS_DISEASE_PREV_WEAK = {
    "has", "had", "our", "your", "their", "of", "about", "from", "like",
    "thanks", "because", "cause", "and", "world", "national", "walk", "bike",
    "stable", "early", "late", "onset", "new", "highly", "possible", "end",
    "ending", "f", "the", "for", "re", "regarding", "on", "to",
}
# Noise neighbours, grouped by meaning (the group name appears in the Reason)
MS_NOISE_NEXT_WORDS: Dict[str, Set[str]] = {
    "Microsoft product": {
        "teams", "office", "word", "excel", "powerpoint", "ppt", "outlook",
        "paint", "access", "sql", "dos", "azure", "windows", "edge", "store",
        "exchange", "sharepoint", "onenote", "onedrive", "forms", "project",
        "visio", "365", "copilot", "defender", "surface", "xbox", "flight",
        "fs", "fs2020", "fs2024", "dynamics", "authenticator", "publisher",
        "intune", "bing", "account", "login", "rewards", "points", "paint3d",
        "notepad", "wordpad", "license", "licence", "certification",
        "certified", "certificate", "exam", "azure-certified",
    },
    "a person / title (MS Dhoni, Ms. Marvel ...)": {
        "dhoni", "swaminathan", "subbulakshmi", "subbulaxmi", "ramaiah",
        "rachel", "marvel", "pacman", "pac", "universe", "america", "olympia",
        "world", "india", "earth", "senior", "teen", "dhonis",
    },
    "Master's degree (MS/MSc)": {
        "degree", "program", "programme", "programs", "admit", "admits",
        "admission", "admissions", "application", "applications", "student",
        "students", "course", "thesis", "scholarship", "aspirant",
        "aspirants", "abroad", "cs", "ds", "ortho", "orthopaedics",
        "orthopedics", "ent", "obg", "obgyn", "ophthalmology", "anatomy",
        "surgery", "gen", "graduate", "grad", "finance", "analytics", "mis",
        "ece", "eee", "ee", "mech", "civil", "bme", "cse",
        # v2: "it" and "me" removed - ordinary words ("MS. It is...", "have MS, it
        # could..."). Upper-case "MS IT" / "MS ME" are in NOISE_CASE_SENSITIVE.
    },
    "mild steel": {
        "pipe", "pipes", "rod", "rods", "sheet", "sheets", "plate", "plates",
        "angle", "angles", "channel", "channels", "beam", "beams", "bar",
        "bars", "wire", "steel", "gate", "gates", "grill", "tube", "tubes",
        "coil", "scrap", "fabrication", "structure", "flat", "flats",
        "billet", "billets", "railing", "shed",
    },
    "milliseconds / latency": {"latency", "delay", "ping", "response", "lag", "timeout"},
    "Mississippi (US state)": {
        "state", "st", "legislature", "gov", "governor", "senate", "senator",
        "delta", "coast", "gulf", "river", "rep", "hwy", "highway", "county",
        "voters", "primary", "election", "sos",
    },
    "chat slang ('miss')": {"u", "you", "ya", "yall", "uu", "youu", "yaa", "uuu"},
}
MS_NOISE_NEXT_BIGRAMS = {   # "MS in <subject>" = degree
    "in cs", "in computer", "in data", "in ds", "in ai", "in ml", "in usa",
    "in us", "in germany", "in canada", "in uk", "in australia", "in ireland",
    "in engineering", "in finance", "in analytics", "in mechanical",
    "in electrical", "in civil", "in biotech", "in physics", "in chemistry",
    "in maths", "in math", "in mathematics", "in business", "in management",
    "in information", "in it", "in statistics", "in economics", "in bme",
    "in biomedical", "in cybersecurity", "in software", "in robotics",
    "in europe", "in france", "in netherlands", "in sweden", "in singapore",
    "in nz", "in accounting", "in marketing", "in hr", "in supply",
    "from stanford", "from mit", "from cmu", "at stanford", "at mit",
    "at cmu", "from usc", "at usc", "from nyu", "at nyu",
}
MS_NOISE_PREV_WORDS: Dict[str, Set[str]] = {
    "mass spectrometry": {"lc", "gc", "tandem", "hplc", "uplc", "maldi", "esi", "icp", "ms"},
    "Mississippi (US state)": {
        "jackson", "oxford", "biloxi", "gulfport", "tupelo", "hattiesburg",
        "starkville", "meridian", "southaven", "columbus", "vicksburg",
        "natchez", "greenville", "olive", "brandon", "pascagoula", "madison",
        "ridgeland", "clinton", "pearl", "laurel", "corinth",
    },
    "Master's degree (MS/MSc)": {
        "pursuing", "pursue", "md", "mbbs", "neet", "msc", "mba", "phd",
        "btech", "mtech", "be", "bsc", "an", "pg", "doing", "completed",
        "finished", "integrated", "dual",
    },
    "a person / title (MS Dhoni, Ms. Marvel ...)": {
        "dear", "mr", "mrs", "miss", "thala", "captain", "mahi", "csk",
        "dhoni", "madam", "teacher", "mister",
    },
    "manuscript / publishing": {"via", "submitted", "revised", "draft", "accepted", "rejected"},
    "Microsoft product": {"using", "open", "install", "installed", "uninstall"},
}
# Words anywhere within MS_NOISE_WINDOW tokens of an MS token (without a
# STRONG disease neighbour) that turn it into noise.
MS_NOISE_WINDOW_WORDS: Dict[str, Set[str]] = {
    "Master's degree (MS/MSc)": {
        "admit", "admits", "admitted", "admission", "university", "uni",
        "college", "gre", "toefl", "ielts", "intake", "scholarship", "tu",
        "iit", "nit", "semester", "professor", "thesis", "graduate",
        "graduated", "grad", "visa", "f1", "sop", "lor", "gpa", "cgpa",
        "masters", "bachelors", "degree", "neet", "usmle",
    },
    "Microsoft / tech": {
        "microsoft", "windows", "laptop", "software", "install", "download",
        "crack", "license", "licence", "update", "hiring", "layoffs",
        "activision", "openai", "github", "linkedin", "bing", "nadella",
        "copilot", "chatgpt", "gpt", "xbox", "azure", "teams", "outlook",
        "office", "excel", "powerpoint", "pc", "os", "developer", "developers",
        "api", "cloud", "gates", "satya", "intern", "internship", "interview",
        "google", "apple", "amazon", "meta", "nvidia", "tesla",
    },
    "MS Dhoni / cricket": {"dhoni", "cricket", "ipl", "csk", "wicket", "sixer", "innings", "thala"},
    "Mississippi (US state)": {"mississippi", "alabama", "louisiana", "tennessee", "arkansas"},
    "school / Ms. title": {"teacher", "class", "homework", "classroom", "student", "students", "mr", "mrs"},
    "milliseconds / latency": {"latency", "ping", "fps", "server", "milliseconds", "frame", "frametime"},
    "manuscript / publishing": {"journal", "reviewers", "reviewer", "preprint", "arxiv", "editor", "revision", "manuscript", "submission", "accepted",
                                "rejected", "submitted", "resubmitted", "published"},
    "finance / stock ticker": {"stock", "shares", "nasdaq", "nyse", "earnings", "ticker", "portfolio"},
}
MS_NOISE_WINDOW = 4
W_MS_CONTEXT_DISEASE = 3.5    # "my MS", "MS fatigue"
W_MS_WEAK_CONTEXT = 2.0       # "MS is ...", "and MS"
W_MS_UPPER_DEFAULT = 2.5      # bare upper-case "MS" with no noise around
W_MS_LOWER_DEFAULT = 0.75     # bare lower-case "ms"
W_MS_TITLE_DEFAULT = 0.5      # bare "Ms"
W_MS_HASHTAG = 3.5            # "#MS"
W_MS_NOISE_OCCURRENCE = 3.0   # "MS Teams", "Ms. Smith", "Jackson MS"

# ----------------------------------------------------------- 7. NOISE --------
# Non-MS meanings of "MS" and non-medical contexts, grouped by meaning.
NOISE_KEYWORDS: Dict[str, Dict[str, float]] = {
    "Microsoft / tech": {
        r"microsoft|msft|msdn|msn|ms ?dos|ms ?office|ms ?word|ms ?excel|ms ?teams": 3.0,
        r"windows (?:10|11|update|laptop|pc)|xbox|azure|office ?365|sharepoint|"
        r"visual studio|satya nadella|teams meeting|teams call|game ?pass": 2.0,
        r"outlook|copilot|onedrive|powerpoint|excel sheet|spreadsheet|bing": 1.0,
    },
    "Mississippi (US state)": {
        r"mississippi|ole miss|hail state|starkville|biloxi|gulfport|hattiesburg|"
        r"tupelo|vicksburg|natchez|southaven|msleg|msgov|msstate|msu bulldogs": 3.0,
        r"ms ?\d{5}|governor reeves|tate reeves": 2.0,
    },
    "MS Dhoni / cricket": {
        r"dhoni|msd ?7?|chennai super kings|captain cool|thala|mahi": 3.0,
        r"csk|ipl|cricket|wicket|odi|t20|bcci|sixer|innings": 1.5,
    },
    "Master's degree (MS/MSc)": {
        r"master'?s degree|master of science|masters? in|masters? program\w*|"
        r"master of surgery|md ?/ ?ms|neet ?pg|mbbs|aiims|ms ortho|ms general surgery": 3.0,
        r"grad school|gre|toefl|ielts|sop|lor|f1 visa|admits?|intake|"
        r"fall (?:20)?\d\d|spring (?:20)?\d\d|university application\w*": 1.5,
    },
    "milliseconds / latency": {
        r"milliseconds?|msec|\d+(?:\.\d+)? ?ms(?! (?:patients?|warriors?|people|"
        r"cases|drugs?|diagnos\w*|community|society))": 2.5,
        r"latency|ping|fps|refresh rate|frame ?time|response time|lag spikes?": 1.5,
    },
    "mass spectrometry / lab": {
        r"mass spec\w*|lc ?ms|gc ?ms|ms ?/ ?ms|maldi|orbitrap|proteomic\w*|"
        r"metabolomic\w*|ms1|ms2|m/z": 3.0,
    },
    "Morgan Stanley / finance": {r"morgan stanley|\$ms|nyse ?: ?ms": 3.0},
    "Ms. (title) / pop culture": {
        r"ms\.? marvel|kamala khan|ms\.? pac ?man|ms rachel|ms\.? universe|"
        r"ms\.? olympia|ms\.? america|ms\.? world|ms\.? india": 3.0,
    },
    "manuscript / publishing": {r"manuscript|reviewer ?2|peer review|preprint": 0.75},
    "mitral stenosis (heart)": {r"mitral|rheumatic heart|valve replacement": 3.0},
    "mild steel": {r"mild steel|tmt bars?|ms pipes?|ms sheets?|ms plates?|fabrication work": 3.0},
    "gaming / anime": {
        r"mobile suit|gundam|zaku|maplestory": 2.0,
        r"msi (?:laptop|gaming|motherboard|monitor|gpu)|flight simulator|msfs": 2.0,
    },
    "addiction relapse": {r"sober|sobriety|alcoholic|addiction|rehab|aa meeting|clean for \d+": 1.0},
    "psychedelic DMT": {
        r"dmt (?:trip|vape|pen|cart|breakthrough|experience)|smok\w* dmt|"
        r"ayahuasca|psychedelic\w*|shrooms?|lsd|psilocybin|5 ?meo": 3.0,
    },
    "Pakistan politics (PML)": {r"pml ?n|pmln|pti|nawaz|shehbaz|imran khan|maryam nawaz": 3.0},
    "audio / electrical RMS": {
        r"rms (?:power|value|watts?|voltage|current|error|noise)|\d+ ?w(?:atts?)? rms|"
        r"rms titanic|titanic|amplifier|subwoofer": 3.0,
    },
}
# Case-sensitive noise patterns (run on original casing)
NOISE_CASE_SENSITIVE: Dict[str, Tuple[str, float]] = {
    r"\bMs\.\s?[A-Z][a-z]+": ("Ms. (title) / pop culture", 3.0),          # Ms. Smith
    r"\bMS-?0[1-4]\b": ("Mississippi (US state)", 1.5),                     # districts
    r"\bMS\s(?:in\s)?(?:IT|ME)\b": ("Master's degree (MS/MSc)", 3.0),        # v2: "MS IT", "MS in ME"
    r"\b[A-Z][a-z]+,\s?MS\b(?!\s+(?:patient|warrior|symptom|fatigue))": ("Mississippi (US state)", 1.0),
    r"\b(?:AL|AR|LA|TN|GA|FL|TX|AZ|OK|KY|MO)\s*(?:,|/|&|and)\s*MS\b|"
    r"\bMS\s*(?:,|/|&|and)\s*(?:AL|AR|LA|TN|GA|FL|TX|AZ|OK|KY|MO)\b": ("Mississippi (US state)", 3.0),
}
# Diseases where our / competitor molecules are used outside MS
NON_MS_INDICATION_KEYWORDS: Dict[str, float] = {
    r"lymphoma|leuk[ae]?mia|cll|non hodgkin|r ?chop|myeloma": 3.0,
    r"rheumatoid|vasculitis|granulomatosis|pemphigus|crohn'?s|crohns|ibd|"
    r"ulcerative colitis|colitis|nephrotic|membranous nephropathy|"
    r"myasthenia|arzerra|campath|itp|hepatitis|hep c": 3.0,
    r"nmosd|neuromyelitis|lupus|psoriasis|asthma|melanoma|kidney cancer": 1.5,
    r"chemo\w*|oncolog\w*|cancer|tumou?rs?": 1.5,
}
NON_MS_INDICATION_THRESHOLD = 2.5
# v2: for MS-specific brands the other disease must be LINKED to the brand
# ("ofatumumab for CLL", "lymphoma patients treated with ...").
OFF_LABEL_LINK_AFTER_RE = re.compile(
    r"^\W*(?:\([^)]{0,30}\)\W*)?(?:is |was |being |been )?(?:used |approved |given |prescribed )?"
    r"(?:for|in|to treat|treating|against)\b")
OFF_LABEL_LINK_BEFORE_RE = re.compile(
    r"(?:patients?|people|pts)?\s*(?:treated with|treatment with|therapy with|on|receiving|using)\W*$")
# v2: a short form ("tec", "ocr") needs at least this much treatment context
# within PROXIMITY_CHARS of it (unless another drug is reliably named).
SHORT_FORM_LOCAL_CONTEXT = 1.0

# ----------------------------------- 8. DRUG / TREATMENT CONTEXT ------------
# "The post is ABOUT treatment" – infusion, dose, side effects, cost ...
# Used to validate weak drug matches and to support the confidence score.
DRUG_FOCUS_KEYWORDS: Dict[str, float] = {
    r"infus\w*": 1.5,
    r"dos(?:e|es|ed|ing|age)|half dose|full dose|split dose|loading dose|\d+ ?mg": 1.5,
    r"inject\w*|pens?|auto ?injectors?|shots?|jabs?": 1.25,
    r"subcutaneous\w*|sub ?q|sc|under the skin|intravenous|iv": 1.25,
    r"every (?:six|6|four|4|two|2) (?:months|weeks)|twice a year|"
    r"once a month|monthly|biannual": 1.0,
    r"(?:hours?|day) in the chair|in the chair|infusion (?:center|centre|chair|nurse)": 1.0,
    r"premed\w*|benadryl|antihistamines?|tylenol|steroid premed": 1.25,
    r"side ?effects?|reactions?|rash|itch\w*|hives|headaches?|nausea|fever|"
    r"chills|flu ?like|hair loss|throat": 1.25,
    r"infections?|utis?|igg|igm|immunoglobulin\w*|b ?cells?|cd19|lymphocytes?|"
    r"hypogamma\w*|white (?:blood )?cells?|neutropenia": 1.0,
    r"vaccin\w*|booster": 0.75,
    r"approv\w*|fda|ema|mhra|nice|chmp|label|indication|launch\w*|rollout|"
    r"available now|now available": 2.0,
    r"price\w*|costs?|costly|costing|expensive|afford\w*|co ?pays?|insurance|insurer|"
    r"coverage|covered|prior auth\w*|formulary|denied|reimburs\w*|"
    r"patient assistance|copay card|medicare|medicaid": 1.5,
    r"efficacy|effective\w*|trials?|phase (?:3|iii|2|ii)|"
    r"oratorio|asclepios|ultimate trial|ensemble trial|real world data|long term data": 1.25,
    r"works?|working|helped|helping|did nothing": 0.75,
    r"no new (?:lesions?|activity|relapses?)|stable (?:mri|scans?)|neda|"
    r"no evidence of disease activity": 1.25,
    r"roche|genentech|novartis|biogen|tg therapeutics|tgtx|sandoz|sanofi|"
    r"bristol myers|celgene|teva|janssen|emd serono|merck kgaa": 1.5,
    r"sales|revenue|market share|biosimilar\w*|generics?|patents?": 1.5,
    r"prescri\w*|scripts?|refills?|pharmacy|specialty pharmacy": 1.0,
    r"start(?:ed|ing|s)? (?:on )?{DRUG}|begin\w* {DRUG}|my first {DRUG}": 1.5,
    r"{DRUG} (?:day|time|infusion|injection|shot|dose|pen|treatment|appointment)": 2.0,
    r"(?:on|taking|tried|try|started|use|using|had|take) {DRUG}": 1.5,
    r"(?:experiences?|thoughts|opinions?|questions?|advice|tips|info|reviews?)\s+"
    r"(?:on|with|about|re|regarding|of)\s+{DRUG}": 2.0,
}
PROXIMITY_CHARS = 60          # context words this close to a drug name...
PROXIMITY_BOOST = 1.5         # ...are multiplied by this

# "The post is about LIFE WITH MS" – used only to describe a drug post whose
# drug is background (it still goes to the drug bucket, see rules above).
GENERAL_MS_STORY_KEYWORDS: Dict[str, float] = {
    r"fatigue|exhaust\w*": 1.0,
    r"brain fog|cog fog|cognitive\w*|memory": 1.0,
    r"numb\w*|tingl\w*|pins and needles|nerve pain|neuropath\w*": 1.0,
    r"spasm\w*|spasticity|stiff\w*|cramps?": 1.0,
    r"balance|dizz\w*|vertigo|mobility|foot drop|weak legs|leg weakness": 0.75,
    r"bladder|bowel|incontinence": 1.0,
    r"depress\w*|anxiety|mental health|insomnia|mood": 1.0,
    r"living with|lives with|life with": 1.5,
    r"diagnos\w*|dx|dx'?d": 1.0,
    r"invisible (?:illness|disability|disease)|chronic (?:illness|disease|pain)|"
    r"spoonies?|spoons": 1.5,
    r"disabilit\w*|disabled|wheelchairs?|canes?|walking stick|rollators?": 1.0,
    r"caregiv\w*|carers?|caring for": 1.5,
    r"support groups?|community|awareness|fundrais\w*|charity": 1.5,
    r"(?:nobody|no one|people) (?:\w+ )?(?:understands?|believes?|gets? it|cares?)|"
    r"not believ\w*|gaslit|dismissed": 1.5,
    r"(?:hard|bad|good|rough|tough) days?": 1.0,
    r"the (?:hardest|worst|bigger|biggest|real|main|toughest) "
    r"(?:part|thing|issue|problem|struggle)": 1.5,
}
GENERAL_STORY_NOTE_SCORE = 3.0    # describe the post as "general MS story" above this

# --------------------------------- 9. COMPARISON / SWITCHING KEYWORDS -------
# Used for the reason + confidence of "Ocrevus vs. Competitor".
COMPARISON_KEYWORDS: Dict[str, float] = {
    r"{DRUG} ?(?:vs\.?|versus|v\.?|or|over|instead of|rather than|"
    r"compared (?:to|with)|than) ?{DRUG}": 3.0,
    r"from {DRUG} (?:to|onto|over to) {DRUG}": 3.0,
    r"switch\w*|swap\w*|chang(?:e|ed|ing) (?:to|from|over|meds|drugs|"
    r"treatments?|dmts?)": 2.0,
    r"vs\.?|versus|head to head|comparison|compar(?:e|ed|ing)": 2.0,
    r"better than|worse than|safer than|easier than|harder than|"
    r"more effective than|less effective than|prefer\w*": 2.0,
    r"instead of|rather than|over {DRUG}": 2.0,
    r"pros and cons|difference between|which (?:one|is better|is safer|"
    r"would you|should i|do you)|(?:decid\w*|choos\w*|pick\w*|torn) between|"
    r"weighing": 2.0,
    r"(?:chose|choose|picked|went with|going with|opted for) {DRUG}": 1.5,
    r"(?:stopp?ed|stopping|quit|came off|coming off|off of|discontinu\w*|"
    r"dropped) {DRUG}": 1.5,
    r"moved? (?:from|to|over to|onto) {DRUG}|go(?:ing)? (?:back )?(?:to|on) {DRUG}": 1.5,
    r"{DRUG} (?:is|was|seems|felt) (?:so much |much |way |a lot )?"
    r"(?:better|worse|easier|harder|safer|more|less)": 1.5,
    r"(?:tried|been through|went through|history of|failed|used to be on) {DRUG}": 1.0,
    r"then {DRUG}|{DRUG} ?(?:,|then|and now|now) ?{DRUG}": 1.0,
    r"neuro\w* (?:wants|suggested|recommended|recommends|says|said) "
    r"(?:me )?(?:to )?(?:switch|move|try|go on|start|change)": 1.5,
}
COMPARISON_THRESHOLD = 2.0

# ------------------------------------------------- 10. DECISION SETTINGS ----
MS_MIN_EVIDENCE = 2.0           # MS-disease score needed for "General MS"
NOISE_DOMINANCE = 1.0           # noise wins if noise >= MS evidence * this
MS_CONTEXT_FOR_AMBIGUOUS_DRUG = 1.0  # MS signal needed for rituximab etc.
WEAK_MATCH_MS_CONTEXT = 1.0     # fuzzy-2 / short-form drug needs this MS signal...
WEAK_MATCH_DRUG_CONTEXT = 2.0   # ...or this much treatment context

# How reliable each drug-detection method is (feeds the confidence score)
DETECTION_QUALITY = {
    "exact":   1.00,   # brand or generic name spelled correctly
    "variant": 0.93,   # repeated letters, l33t, spaced / split letters
    "regex":   0.88,   # known misspelling pattern
    "fuzzy1":  0.80,   # 1 typo
    "short":   0.72,   # abbreviation, confirmed by context
    "fuzzy2":  0.66,   # 2 typos, confirmed by context
}
DETECTION_LABEL = {
    "exact": "exact name", "variant": "spelling variant", "regex": "misspelling",
    "fuzzy1": "1-letter typo", "short": "short form", "fuzzy2": "2-letter typo",
}

STOPWORDS = set("""
a an the and or but if of at by for with about against between into through
during before after above below to from up down in out on off over under again
further then once here there when where why how all any both each few more most
other some such no nor not only own same so than too very s t can will just don
should now i me my myself we our ours ourselves you your yours yourself he him
his himself she her hers herself it its itself they them their theirs what which
who whom this that these those am is are was were be been being have has had
having do does did doing would could ought i'm you're he's she's it's we're
they're i've you've we've they've i'd you'd he'd she'd we'd they'd i'll you'll
he'll she'll we'll they'll isn't aren't wasn't weren't hasn't haven't hadn't
doesn't don't didn't won't wouldn't shan't shouldn't can't cannot couldn't
mustn't let's that's who's what's here's there's when's where's why's how's
im ive dont cant got get gets getting just really also still even much lot lots
like today day ourdrug compdrug
""".split())

# =============================================================================
# ===========================  END OF CONFIGURATION  ==========================
# =============================================================================


# ----------------------------------------------------------------------------
#  STEP 1 – CLEANING
# ----------------------------------------------------------------------------
_INVISIBLE_RE = re.compile(r"[​‌‍⁠﻿­᠎]")
_TRADEMARK_RE = re.compile(r"[™®©℠]")
_RT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:RT|MT|QT|Rt|rt)\b\s*:?\s*(?:@\w{1,50}\s*:?\s*)+)", re.UNICODE)
_RT_INLINE_RE = re.compile(r"\b(?:RT|MT)\s+@\w{1,50}:?", re.UNICODE)
_RT_LEFTOVER_RE = re.compile(r"^\s*RT\b\s*:?\s*")
_URL_RES = [
    re.compile(r"(?:https?|hxxps?)://\S+", re.I),
    re.compile(r"\b(?:https?|hxxps?):\S*", re.I),                 # truncated "https:/…"
    re.compile(r"\bwww\.\S+", re.I),
    re.compile(r"\b(?:pic\.x\.com|pic\.twitter\.com|t\.co|x\.com|twitter\.com|"
               r"mobile\.twitter\.com|bit\.ly|buff\.ly|ow\.ly|tinyurl\.com|"
               r"lnkd\.in|youtu\.be|fb\.me|dlvr\.it|ift\.tt|goo\.gl|tiny\.cc|"
               r"trib\.al|wp\.me|amzn\.to|instagr\.am)(?:/\S*)?", re.I),
    re.compile(r"\b[\w\-]+(?:\.[\w\-]+)*\.(?:com|org|net|gov|edu|io|co|uk|ly|"
               r"in|me|info|health|news)/\S*", re.I),               # domain/path
]
_MENTION_RE = re.compile(r"(?<![\w@.])@\w{1,50}", re.UNICODE)
_DANGLING_RE = [
    re.compile(r"\(\s*(?:via|cc|h/t|ht)?\s*:?\s*\)|\[\s*\]|\{\s*\}", re.I),
    re.compile(r"\b(?:via|cc|h/t|ht)\s*:?\s*$", re.I),
    re.compile(r"(?:^|\s)[:|\-–—]+(?=\s|$)"),
]
_LEADING_PUNCT_RE = re.compile(r"^[\s:;,.|\-–—>]+")
_MULTISPACE_RE = re.compile(r"\s+")


def clean_tweet(raw) -> str:
    """Clean a raw Twitter post. Keeps hashtags, emojis and original casing.

    Order:  decode/normalise -> 1. RT prefix -> 2. URLs -> 3. @handles
            -> 4. collapse spaces / tidy punctuation.
    """
    if raw is None:
        return ""
    if isinstance(raw, float) and math.isnan(raw):
        return ""
    text = str(raw)
    if not text.strip() or text.strip().lower() in {"nan", "none", "null"}:
        return ""

    # 0. decode & normalise
    for _ in range(2):                      # handles double-escaped "&amp;amp;"
        text = html.unescape(text)
    text = _TRADEMARK_RE.sub("", text)
    text = unicodedata.normalize("NFKC", text)   # 𝗢𝗰𝗿𝗲𝘃𝘂𝘀 -> Ocrevus, ＠ -> @
    text = _INVISIBLE_RE.sub("", text)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = text.replace("’", "'").replace("‘", "'").replace("ʼ", "'")

    # 1. "RT @user:" prefix (repeated chains too) + old-style inline RTs
    prev = None
    while prev != text:
        prev = text
        text = _RT_PREFIX_RE.sub("", text)
    text = _RT_INLINE_RE.sub(" ", text)
    text = _RT_LEFTOVER_RE.sub("", text)

    # 2. URLs
    for rx in _URL_RES:
        text = rx.sub(" ", text)

    # 3. @usernames (e-mail addresses are left intact)
    text = _MENTION_RE.sub(" ", text)

    # 4. tidy + collapse whitespace
    for rx in _DANGLING_RE:
        text = rx.sub(" ", text)
    text = _MULTISPACE_RE.sub(" ", text).strip()
    text = _LEADING_PUNCT_RE.sub("", text).strip()
    return text


# ----------------------------------------------------------------------------
#  Helpers
# ----------------------------------------------------------------------------
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
_WORD_RE = re.compile(r"[a-z][a-z'\-]*[a-z]|[a-z]")
_CASED_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")
_DRUG_TOKEN_RE = re.compile(r"\b(?:ourdrug|compdrug)\b")
_LEET_MAP = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s", "!": "i"}
_SPACED_LETTERS_RE = re.compile(r"(?<![a-z])(?:[a-z][ .\-*_]){4,14}[a-z](?![a-z])")
_RUN3_RE = re.compile(r"(.)\1{2,}")
_RUN2_RE = re.compile(r"(.)\1+")


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _expand_hashtags(text: str) -> str:
    """#MSWarrior -> 'MSWarrior MS Warrior'  (keeps the joined form too)."""
    def repl(m: re.Match) -> str:
        body = m.group(1)
        parts = _CAMEL_RE.findall(body)
        if len(parts) > 1:
            return f" {body} {' '.join(parts)} "
        return f" {body} "
    return _HASHTAG_RE.sub(repl, text)


def _deleet(norm: str) -> str:
    """Same-length l33t undo (0crevus -> ocrevus, k3simpta -> kesimpta).
    Only digits/symbols touching a letter are mapped, so spans stay aligned."""
    chars = list(norm)
    n = len(chars)
    for i, ch in enumerate(chars):
        if ch in _LEET_MAP:
            left = norm[i - 1] if i > 0 else " "
            right = norm[i + 1] if i + 1 < n else " "
            if left.isalpha() or right.isalpha():
                if left.isalpha() and right.isalpha() or (left.isalpha() and not right.isdigit()) \
                        or (right.isalpha() and not left.isdigit()):
                    chars[i] = _LEET_MAP[ch]
    return "".join(chars)


def _collapse_runs(tok: str) -> str:
    return _RUN2_RE.sub(r"\1", tok)


def _compile_fragment(fragment: str) -> str:
    """Keyword fragment -> regex with soft word boundaries."""
    frag = _strip_accents(fragment).replace(" ", r"[\s\-_]*")
    frag = (frag.replace("{DRUG}", r"(?:ourdrug|compdrug)")
                .replace("{OUR}", "ourdrug").replace("{COMP}", "compdrug"))
    return rf"(?<![a-z0-9])(?:{frag})(?![a-z0-9])"


class WeightedTerms:
    """A weighted keyword list compiled for fast scoring."""

    def __init__(self, terms: Dict[str, float], flags: int = re.I,
                 categories: Optional[Dict[str, str]] = None):
        self.items: List[Tuple[str, re.Pattern, float]] = []
        self.categories = categories or {}
        for frag, w in terms.items():
            try:
                self.items.append((frag, re.compile(_compile_fragment(frag), flags), float(w)))
            except re.error as exc:
                raise ValueError(f"Bad keyword regex {frag!r}: {exc}") from exc
        combined = "|".join(f"(?:{_compile_fragment(f)})" for f in terms) or r"(?!x)x"
        self.any = re.compile(combined, flags)

    def score(self, text: str, near: Optional[List[Tuple[int, int]]] = None,
              boost: float = 1.0, window: int = 0):
        """Return (score, max_weight, matched_terms). Each term counted once;
        a term within `window` chars of a span in `near` is multiplied by `boost`."""
        total, mx, hits, _ = self.score_detailed(text, near, boost, window)
        return total, mx, hits

    def score_detailed(self, text: str, near=None, boost: float = 1.0, window: int = 0):
        """Like score() but also returns [(category, matched_text, weight)]."""
        if not text or not self.any.search(text):
            return 0.0, 0.0, [], []
        total, mx, hits, detail = 0.0, 0.0, [], []
        for frag, rx, w in self.items:
            m_best = None
            for m in rx.finditer(text):
                m_best = m_best or m
                if near and any(m.start() - window <= e and m.end() + window >= s
                                for s, e in near):
                    m_best = m
                    break
            if m_best is None:
                continue
            ww = w
            if near and boost != 1.0 and any(
                    m_best.start() - window <= e and m_best.end() + window >= s
                    for s, e in near):
                ww = w * boost
            total += ww
            mx = max(mx, w)
            hit = m_best.group(0).strip()
            hits.append(hit)
            detail.append((self.categories.get(frag, ""), hit, ww))
        return total, mx, hits, detail


class CaseSensitiveTerms:
    def __init__(self, terms: Dict[str, Tuple[str, float]]):
        self.items = [(re.compile(p), cat, float(w)) for p, (cat, w) in terms.items()]

    def score(self, text: str):
        total, detail = 0.0, []
        for rx, cat, w in self.items:
            m = rx.search(text)
            if m:
                total += w
                detail.append((cat, m.group(0), w))
        return total, detail


def _flatten_categories(groups: Dict[str, Dict[str, float]]):
    flat, cats = {}, {}
    for cat, terms in groups.items():
        for frag, w in terms.items():
            flat[frag] = w
            cats[frag] = cat
    return flat, cats


def _word_to_category(groups: Dict[str, Set[str]]) -> Dict[str, str]:
    out = {}
    for cat, words in groups.items():
        for w in words:
            out.setdefault(w, cat)
    return out


def _readable(hit: str) -> str:
    """Turn masked placeholders back into readable words for the Reason text."""
    return (hit.replace("ourdrug", f"<{OUR_BRAND_LABEL}>")
               .replace("compdrug", "<competitor>"))


def _quote_list(items, limit: int = 5) -> str:
    seen, out = set(), []
    for it in items:
        k = it.lower()
        if k and k not in seen:
            seen.add(k)
            out.append(f"'{_readable(it)}'")
        if len(out) >= limit:
            break
    return ", ".join(out)


def format_drug_mentions(our: List[str], comp: List[str]) -> str:
    """Our brand(s) first, then competitors alphabetically, comma separated."""
    ours = sorted(set(our), key=str.casefold)
    comps = sorted(set(comp) - set(ours), key=str.casefold)
    return ", ".join(ours + comps)


# ----------------------------------------------------------------------------
#  Result object
# ----------------------------------------------------------------------------
@dataclass
class DrugMention:
    canonical: str
    group: str          # "our" | "comp"
    surface: str
    start: int
    end: int
    method: str         # exact | variant | regex | fuzzy1 | fuzzy2 | short

    @property
    def quality(self) -> float:
        return DETECTION_QUALITY.get(self.method, 0.6)


@dataclass
class ClassificationResult:
    bucket: str
    confidence: float
    our_drugs: List[str] = field(default_factory=list)
    competitor_drugs: List[str] = field(default_factory=list)
    drug_mentions: str = ""
    reason: str = ""
    # internal scores (not written to the output unless WRITE_INTERNAL_SCORE_COLUMNS)
    ms_score: float = 0.0
    hint_score: float = 0.0
    noise_score: float = 0.0
    drug_focus_score: float = 0.0
    general_story_score: float = 0.0
    comparison_score: float = 0.0

    def as_row(self, internal: bool = WRITE_INTERNAL_SCORE_COLUMNS) -> dict:
        row = {
            DRUG_MENTIONS_COLUMN: self.drug_mentions,
            CONFIDENCE_COLUMN: round(self.confidence, 2),
            REASON_COLUMN: self.reason,
        }
        if internal:
            row.update({
                "MS Score": round(self.ms_score, 2),
                "MS Hint Score": round(self.hint_score, 2),
                "Noise Score": round(self.noise_score, 2),
                "Drug Focus Score": round(self.drug_focus_score, 2),
                "General Story Score": round(self.general_story_score, 2),
                "Comparison Score": round(self.comparison_score, 2),
            })
        return row


def _clip(c: float, lo: float = 0.30, hi: float = 0.99) -> float:
    return float(min(hi, max(lo, c)))


def _level(c: float) -> str:
    return "high" if c >= 0.85 else ("medium" if c >= LOW_CONFIDENCE_THRESHOLD + 0.05 else "low")


# ----------------------------------------------------------------------------
#  CLASSIFIER
# ----------------------------------------------------------------------------
class EntityClassifier:
    """Rule-based entity-bucket classifier. All behaviour comes from the
    configuration dicts, so you can build a new instance for another brand."""

    def __init__(self,
                 our_drugs: Dict[str, dict] = None,
                 competitor_drugs: Dict[str, dict] = None,
                 extended_competitors: Optional[Dict[str, dict]] = None,
                 include_extended: bool = INCLUDE_EXTENDED_COMPETITORS,
                 buckets: Dict[str, str] = None):
        self.buckets = buckets or BUCKETS
        our_drugs = our_drugs if our_drugs is not None else OUR_DRUGS
        comp = dict(competitor_drugs if competitor_drugs is not None else COMPETITOR_DRUGS)
        ext = extended_competitors if extended_competitors is not None else EXTENDED_COMPETITOR_DRUGS
        ms_strong = dict(MS_STRONG_KEYWORDS)
        if include_extended:
            comp.update(ext)
        else:   # extended DMT names become strong MS signals instead
            for cfg in ext.values():
                if cfg.get("requires_ms_context"):
                    continue
                ms_strong["|".join(re.escape(n) for n in cfg["names"])] = 3.0

        self.drugs: Dict[str, dict] = {}
        for name, cfg in our_drugs.items():
            self.drugs[name] = {**cfg, "group": "our"}
        for name, cfg in comp.items():
            if name in self.drugs:
                raise ValueError(f"Drug {name} is in both our and competitor lists")
            self.drugs[name] = {**cfg, "group": "comp"}

        self._build_drug_matchers()
        self.ms_strong = WeightedTerms(ms_strong)
        self.ms_hints = WeightedTerms(MS_HINT_KEYWORDS)
        self.other_cond = WeightedTerms(OTHER_CONDITION_KEYWORDS)
        flat_noise, noise_cats = _flatten_categories(NOISE_KEYWORDS)
        self.noise = WeightedTerms(flat_noise, categories=noise_cats)
        self.noise_cs = CaseSensitiveTerms(NOISE_CASE_SENSITIVE)
        self.non_ms = WeightedTerms(NON_MS_INDICATION_KEYWORDS)
        self.focus = WeightedTerms(DRUG_FOCUS_KEYWORDS)
        self.general = WeightedTerms(GENERAL_MS_STORY_KEYWORDS)
        self.compare = WeightedTerms(COMPARISON_KEYWORDS)
        self._noise_next = _word_to_category(MS_NOISE_NEXT_WORDS)
        self._noise_prev = _word_to_category(MS_NOISE_PREV_WORDS)
        self._noise_window = _word_to_category(MS_NOISE_WINDOW_WORDS)
        self._multiple_next_re = re.compile(
            r"(?<![a-z])(m[a-z]{4,9})[\s\-_]*(s[a-z]{5,12})(?![a-z])")
        self._fuzzy_cache: Dict[str, Optional[Tuple[str, int]]] = {}

    # ------------------------------------------------------------ drugs ----
    def _build_drug_matchers(self):
        self._drug_exact: List[Tuple[str, re.Pattern]] = []
        self._drug_regex: List[Tuple[str, re.Pattern]] = []
        self._drug_short: List[Tuple[str, re.Pattern]] = []
        self._fuzzy_targets: List[Tuple[str, str]] = []
        self._exact_lookup: Dict[str, str] = {}      # name (no spaces) -> canon
        self._collapsed_lookup: Dict[str, str] = {}  # collapsed name -> canon
        for canon, cfg in self.drugs.items():
            parts = []
            for n in sorted(cfg["names"], key=len, reverse=True):
                n = n.lower().strip()
                joined = re.sub(r"[\s\-_]+", "", n)
                self._exact_lookup.setdefault(joined, canon)
                if len(joined) >= 6:
                    self._collapsed_lookup.setdefault(_collapse_runs(joined), canon)
                esc = re.escape(n).replace(r"\ ", r"[\s\-_]*")
                if len(joined) >= 6:
                    parts.append(rf"(?<![a-z]){esc}|{esc}")   # substring OK (#OcrevusDay)
                else:
                    parts.append(rf"(?<![a-z]){esc}(?![a-z])")
                if " " not in n and len(n) >= FUZZY_MIN_TOKEN_LEN:
                    self._fuzzy_targets.append((n, canon))
            self._drug_exact.append((canon, re.compile("|".join(parts), re.I)))
            if cfg.get("misspelling_regex"):
                rx = "|".join(f"(?:{r})" for r in cfg["misspelling_regex"])
                self._drug_regex.append((canon, re.compile(rf"(?<![a-z])(?:{rx})", re.I)))
            if cfg.get("context_names"):
                rx = "|".join(re.escape(s.lower()) for s in cfg["context_names"])
                self._drug_short.append((canon, re.compile(rf"(?<![a-z0-9])(?:{rx})(?![a-z0-9])", re.I)))

    def _fuzzy_lookup(self, token: str) -> Optional[Tuple[str, int]]:
        """Return (canonical, distance) for the closest drug name, or None."""
        if token in self._fuzzy_cache:
            return self._fuzzy_cache[token]
        result = None
        if (len(token) >= FUZZY_MIN_TOKEN_LEN and token not in FUZZY_TOKEN_BLOCKLIST
                and token not in STOPWORDS):
            best = (99, None)
            for name, canon in self._fuzzy_targets:
                maxd = next(d for L, d in FUZZY_MAX_DISTANCE_BY_LEN if len(name) <= L)
                if abs(len(token) - len(name)) > maxd:
                    continue
                same_first = token[0] == name[0]
                if FUZZY_REQUIRE_SAME_FIRST_LETTER and not same_first and len(name) < 7:
                    continue
                d = _edit_distance(token, name, maxd)
                if d > maxd:
                    continue
                if not same_first and d > 1:           # first-letter typo: 1 edit only
                    continue
                if d == 2 and token[:2] != name[:2]:
                    continue
                if d < best[0]:
                    best = (d, canon)
            if best[1] is not None:
                result = (best[1], best[0])
        self._fuzzy_cache[token] = result
        return result

    def detect_drugs(self, norm: str) -> List[DrugMention]:
        """Find every in-scope drug mention. `norm` is lower-cased text."""
        found: List[DrugMention] = []
        dl = _deleet(norm)                      # same length as norm

        def add(canon, s, e, method, surface=None):
            found.append(DrugMention(canon, self.drugs[canon]["group"],
                                     surface if surface is not None else norm[s:e],
                                     s, e, method))

        # 1. exact names (on original text, then on de-l33ted text)
        for canon, rx in self._drug_exact:
            for m in rx.finditer(norm):
                add(canon, m.start(), m.end(), "exact")
            if dl != norm:
                for m in rx.finditer(dl):
                    if norm[m.start():m.end()] != m.group(0):
                        add(canon, m.start(), m.end(), "variant")
        # 2. known misspelling patterns
        for canon, rx in self._drug_regex:
            for m in rx.finditer(dl):
                add(canon, m.start(), m.end(), "regex")
        # 3. s p a c e d / d.o.t.t.e.d letters
        for m in _SPACED_LETTERS_RE.finditer(dl):
            joined = re.sub(r"[^a-z]", "", m.group(0))
            canon = self._exact_lookup.get(joined) or self._collapsed_lookup.get(_collapse_runs(joined))
            if canon and len(joined) >= 5:
                add(canon, m.start(), m.end(), "variant")
        covered = [(d.start, d.end) for d in found]
        toks = [(m.group(0).strip("'-"), m.start(), m.end()) for m in _WORD_RE.finditer(dl)]
        # 4. split words: "ocre vus", "kes impta"
        for (t1, s1, _e1), (t2, _s2, e2) in zip(toks, toks[1:]):
            joined = t1 + t2
            if len(joined) >= 6 and joined in self._exact_lookup and \
                    not any(s <= s1 < e for s, e in covered):
                add(self._exact_lookup[joined], s1, e2, "variant")
        covered = [(d.start, d.end) for d in found]
        # 5. per-token: repeated letters, then fuzzy
        if FUZZY_ENABLED:
            for tok, s, e in toks:
                if len(tok) < FUZZY_MIN_TOKEN_LEN or tok in self._exact_lookup:
                    continue
                if any(cs <= s < ce for cs, ce in covered):
                    continue
                if tok in FUZZY_TOKEN_BLOCKLIST:
                    continue
                coll = _collapse_runs(tok)
                if coll != tok and coll in self._collapsed_lookup:
                    add(self._collapsed_lookup[coll], s, e, "variant", tok)
                    continue
                hit = self._fuzzy_lookup(tok)
                if hit is None and _RUN3_RE.search(tok):
                    hit = self._fuzzy_lookup(_RUN3_RE.sub(r"\1", tok))
                if hit:
                    canon, d = hit
                    add(canon, s, e, "fuzzy1" if d <= 1 else "fuzzy2", tok)
        # 6. short forms (validated later against context)
        covered = [(d.start, d.end) for d in found]
        for canon, rx in self._drug_short:
            for m in rx.finditer(dl):
                if not any(cs <= m.start() < ce for cs, ce in covered):
                    add(canon, m.start(), m.end(), "short")

        # keep the best non-overlapping spans: earliest start, then the most
        # reliable method, then the longest span
        order = list(DETECTION_QUALITY)
        found.sort(key=lambda d: (d.start, order.index(d.method), -(d.end - d.start)))
        merged: List[DrugMention] = []
        for d in found:
            if merged and d.start < merged[-1].end:
                last = merged[-1]
                # a longer, equally good span that starts here replaces a shorter one
                if d.start == last.start and d.end > last.end and \
                        order.index(d.method) <= order.index(last.method):
                    merged[-1] = d
                continue
            merged.append(d)
        return merged

    @staticmethod
    def _mask(norm: str, mentions: List[DrugMention]) -> str:
        out, last = [], 0
        for d in sorted(mentions, key=lambda x: x.start):
            if d.start < last:
                continue
            out.append(norm[last:d.start])
            out.append(" ourdrug " if d.group == "our" else " compdrug ")
            last = d.end
        out.append(norm[last:])
        return _MULTISPACE_RE.sub(" ", "".join(out)).strip()

    # ------------------------------------------------ MS token analysis ----
    def _ms_tokens(self, cased: str, raw_clean: str):
        """Classify every standalone MS/Ms/ms token.
        Returns (disease_score, noise_score, notes, noise_categories, kinds)."""
        disease, noise, notes, noise_cats, kinds = 0.0, 0.0, [], [], []
        dnotes: List[str] = []   # v2: disease-context notes only (used in the Reason)
        toks = [(m.group(0), m.start(), m.end()) for m in _CASED_TOKEN_RE.finditer(cased)]
        lowers = [t[0].lower() for t in toks]
        hashtag_ms = bool(re.search(r"#MS\b", raw_clean, re.I))
        for i, (tok, s, e) in enumerate(toks):
            base = lowers[i][:-2] if lowers[i].endswith("'s") else lowers[i]
            if base != "ms":
                continue
            tok_core = tok[:2]
            nxt = lowers[i + 1] if i + 1 < len(toks) else ""
            nxt2 = " ".join(lowers[i + 1:i + 3])
            nxt_raw = toks[i + 1][0] if i + 1 < len(toks) else ""
            prv = lowers[i - 1] if i > 0 else ""
            prv2 = " ".join(lowers[i - 2:i]) if i > 1 else ""   # v2: "suffers from"
            # digit directly before MS ("200 ms") only when nothing is between them
            prv_adjacent_digit = i > 0 and prv.isdigit() and cased[toks[i - 1][2]:s].strip() == ""
            window = set(lowers[max(0, i - MS_NOISE_WINDOW):i] + lowers[i + 1:i + 1 + MS_NOISE_WINDOW])
            window_hit = sorted(w for w in window if w in self._noise_window)
            followed_by_dot = e < len(cased) and cased[e] == "."
            looks_like_name = nxt_raw[:1].isupper() and nxt_raw[1:2].islower()

            if nxt in self._noise_next or nxt2 in MS_NOISE_NEXT_BIGRAMS:
                cat = self._noise_next.get(nxt, "Master's degree (MS/MSc)")
                noise += W_MS_NOISE_OCCURRENCE; notes.append(f"'{tok} {nxt_raw}'"); noise_cats.append(cat)
            elif nxt in MS_DISEASE_NEXT_STRONG or prv in MS_DISEASE_PREV_STRONG \
                    or prv2 in MS_DISEASE_PREV2_STRONG:
                disease += W_MS_CONTEXT_DISEASE
                if nxt in MS_DISEASE_NEXT_STRONG:
                    notes.append(f"'{tok} {nxt_raw}'")
                elif prv in MS_DISEASE_PREV_STRONG:
                    notes.append(f"'{toks[i-1][0]} {tok}'")
                else:
                    notes.append(f"'{toks[i-2][0]} {toks[i-1][0]} {tok}'")
                dnotes.append(notes[-1])
                kinds.append("context")
            elif prv in self._noise_prev or prv_adjacent_digit:
                cat = self._noise_prev.get(prv, "milliseconds / latency")
                noise += W_MS_NOISE_OCCURRENCE; notes.append(f"'{toks[i-1][0]} {tok}'"); noise_cats.append(cat)
            elif window_hit:
                noise += W_MS_NOISE_OCCURRENCE
                notes.append(f"'{tok}' near '{window_hit[0]}'")
                noise_cats.append(self._noise_window[window_hit[0]])
            elif (tok_core == "Ms" or tok_core == "ms") and followed_by_dot and looks_like_name:
                noise += W_MS_NOISE_OCCURRENCE; notes.append(f"'{tok}. {nxt_raw}'")
                noise_cats.append("Ms. (title) / pop culture")
            elif tok_core == "Ms" and looks_like_name and nxt not in MS_DISEASE_NEXT_WEAK:
                noise += W_MS_NOISE_OCCURRENCE; notes.append(f"'{tok} {nxt_raw}'")
                noise_cats.append("Ms. (title) / pop culture")
            elif nxt in MS_DISEASE_NEXT_WEAK or prv in MS_DISEASE_PREV_WEAK:
                disease += W_MS_WEAK_CONTEXT
                notes.append(f"'{tok} {nxt_raw}'" if nxt in MS_DISEASE_NEXT_WEAK else f"'{toks[i-1][0]} {tok}'")
                dnotes.append(notes[-1])
                kinds.append("weak")
            elif tok_core == "MS":
                disease += W_MS_HASHTAG if hashtag_ms else W_MS_UPPER_DEFAULT
                notes.append("'#MS'" if hashtag_ms else "'MS'")
                dnotes.append(notes[-1])
                kinds.append("hashtag" if hashtag_ms else "bare")
            elif tok_core == "Ms":
                disease += W_MS_TITLE_DEFAULT; notes.append("'Ms' (ambiguous)"); kinds.append("bare")
                dnotes.append(notes[-1])
            else:
                disease += W_MS_LOWER_DEFAULT; notes.append("'ms' (ambiguous)"); kinds.append("bare")
                dnotes.append(notes[-1])
        return disease, noise, notes, noise_cats, kinds, dnotes

    # v2 helpers ---------------------------------------------------------------
    def _local_focus(self, norm: str, d: "DrugMention") -> float:
        """Treatment-context score in a +-PROXIMITY_CHARS window around one mention."""
        tag = " ourdrug " if d.group == "our" else " compdrug "
        win = (norm[max(0, d.start - PROXIMITY_CHARS):d.start] + tag +
               norm[d.end:d.end + PROXIMITY_CHARS])
        return self.focus.score(win)[0]

    def _local_off_label_link(self, norm: str, d: "DrugMention") -> bool:
        """True when a non-MS disease is explicitly tied to THIS mention:
        '<drug> for/in/to treat <disease>' or '<disease> patients treated with <drug>'."""
        after = norm[d.end:d.end + PROXIMITY_CHARS]
        before = norm[max(0, d.start - PROXIMITY_CHARS):d.start]
        m = OFF_LABEL_LINK_AFTER_RE.match(after)
        if m and self.non_ms.score(after[m.end():])[0] >= NON_MS_INDICATION_THRESHOLD:
            return True
        m = OFF_LABEL_LINK_BEFORE_RE.search(before)
        return bool(m and self.non_ms.score(before[:m.start()][-PROXIMITY_CHARS:])[0]
                    >= NON_MS_INDICATION_THRESHOLD)

    def _sclerosis_fuzzy(self, norm: str) -> float:
        """Fuzzy 'multiple sclerosis' (mulitple sclerosus, multiple sclerossis)."""
        for m in self._multiple_next_re.finditer(norm):
            w1, w2 = m.group(1), m.group(2)
            if _edit_distance(w1, "multiple", 2) <= 2 and _edit_distance(w2, "sclerosis", 2) <= 2:
                return 4.0
        return 0.0

    # --------------------------------------------------------- classify ----
    def classify_post(self, clean_text: str) -> ClassificationResult:
        B = self.buckets
        if not clean_text or not clean_text.strip():
            return ClassificationResult(
                B["NOISE"], 0.99,
                reason=f"{B['NOISE']}: the post is empty after cleaning (only a link, "
                       f"a @handle or a retweet marker). Confidence 0.99 (high).")

        cased = _strip_accents(_expand_hashtags(clean_text))
        cased = _MULTISPACE_RE.sub(" ", cased).strip()
        norm = cased.lower()
        if not re.search(r"[a-z0-9Ѐ-￿]", norm):
            return ClassificationResult(
                B["NOISE"], 0.97,
                reason=f"{B['NOISE']}: the post contains no words (only emojis / "
                       f"punctuation). Confidence 0.97 (high).")

        # ---------------------------------------------------- MS evidence --
        strong, _, strong_hits = self.ms_strong.score(norm)
        if strong == 0:
            strong = self._sclerosis_fuzzy(norm)
            if strong:
                strong_hits = ["multiple sclerosis (misspelt)"]
        ms_tok_dis, ms_tok_noise, ms_notes, ms_noise_cats, ms_kinds, ms_dnotes = \
            self._ms_tokens(cased, clean_text)
        hint, hint_max, hint_hits = self.ms_hints.score(norm)

        # -------------------------------------------------- noise evidence --
        n_kw, _, _, noise_detail = self.noise.score_detailed(norm)
        n_cs, cs_detail = self.noise_cs.score(cased)
        noise_detail = noise_detail + cs_detail
        noise_total = n_kw + n_cs + ms_tok_noise
        bare_only = bool(ms_kinds) and all(k in ("bare", "weak") for k in ms_kinds)
        # A non-MS reading somewhere in the text makes a bare "MS" suspect
        if strong == 0 and bare_only and n_kw + n_cs >= 1.0:
            noise_total += 1.5
            ms_tok_dis = 0.0
            ms_dnotes = []
            ms_notes.append("(bare 'MS' discounted: non-MS meaning in text)")
        ms_evidence = strong + ms_tok_dis
        ms_signal_any = ms_evidence + hint

        # ----------------------------------------------------------- drugs --
        mentions = self.detect_drugs(norm)
        non_ms, _, non_ms_hits = self.non_ms.score(norm)
        dropped: List[Tuple[DrugMention, str]] = []

        # treatment context around the candidate drugs (for weak matches)
        masked0 = self._mask(norm, mentions)
        spans0 = [(m.start(), m.end()) for m in _DRUG_TOKEN_RE.finditer(masked0)]
        focus0, _, _ = self.focus.score(masked0, spans0, PROXIMITY_BOOST, PROXIMITY_CHARS)
        has_reliable = any(d.method in ("exact", "variant", "regex") for d in mentions)

        kept: List[DrugMention] = []
        for d in mentions:
            cfg = self.drugs[d.canonical]
            # (1) weak matches need context
            if d.method in ("fuzzy2", "short"):
                ctx_ok = (ms_signal_any >= WEAK_MATCH_MS_CONTEXT or focus0 >= WEAK_MATCH_DRUG_CONTEXT
                          or has_reliable)
                # v2: a SHORT form ("tec", "ocr") also needs treatment words right next
                # to it (or another reliably named drug). A general MS signal elsewhere
                # in a long post is not enough: "not 'cool tec.'" in a post that lists
                # "advanced multiple sclerosis" was read as Tecfidera.
                if d.method == "short" and not has_reliable:
                    ctx_ok = self._local_focus(norm, d) >= SHORT_FORM_LOCAL_CONTEXT
                if not ctx_ok or noise_total > ms_signal_any + focus0:
                    dropped.append((d, "weak match without MS/treatment context"))
                    continue
            if d.method == "fuzzy1" and noise_total >= 3.0 and ms_signal_any == 0 and focus0 < 1.0:
                dropped.append((d, "possible typo, but the post has a non-MS meaning"))
                continue
            # (2) drug used for a non-MS disease
            off_label = non_ms >= NON_MS_INDICATION_THRESHOLD and \
                non_ms > ms_signal_any * (1.0 if cfg.get("ms_specific") else 0.6)
            if cfg.get("ms_specific") and ms_signal_any > 0:
                off_label = False
            # v2: an MS-specific brand is only "off-label" when the other disease is
            # mentioned NEAR it. In a pharma earnings post "myeloma drug Darzalex and
            # Novartis's Kesimpta", the myeloma belongs to Darzalex, not Kesimpta.
            if off_label and cfg.get("ms_specific"):
                off_label = self._local_off_label_link(norm, d)
            if off_label:
                dropped.append((d, "used for a non-MS condition (" + ", ".join(non_ms_hits[:3]) + ")"))
                continue
            # (3) drug mostly used outside MS needs an MS signal
            if cfg.get("requires_ms_context") and (
                    ms_signal_any < MS_CONTEXT_FOR_AMBIGUOUS_DRUG or noise_total > ms_signal_any):
                dropped.append((d, "this molecule is mostly used outside MS and the post has no MS context"))
                continue
            kept.append(d)
        mentions = kept

        our = sorted({d.canonical for d in mentions if d.group == "our"}, key=str.casefold)
        comp = sorted({d.canonical for d in mentions if d.group == "comp"}, key=str.casefold)
        if DRUG_MENTIONS_INCLUDE_NON_MS_USE:
            our_all = our + [d.canonical for d, _ in dropped if d.group == "our"]
            comp_all = comp + [d.canonical for d, _ in dropped if d.group == "comp"]
            drug_col = format_drug_mentions(our_all, comp_all)
        else:
            drug_col = format_drug_mentions(our, comp)

        res = ClassificationResult(B["NOISE"], 0.5, our, comp, drug_col,
                                   ms_score=ms_evidence, hint_score=hint,
                                   noise_score=noise_total)

        noise_cats_all = [c for c, _, _ in noise_detail if c] + ms_noise_cats
        noise_examples = [h for _, h, _ in noise_detail]
        dropped_txt = ""
        if dropped:
            dropped_txt = " Ignored: " + "; ".join(
                f"'{d.surface}' ({d.canonical}) – {why}" for d, why in dropped[:3]) + "."

        if not mentions:
            return self._classify_no_drug(res, norm, strong, strong_hits, ms_evidence, ms_kinds,
                                          ms_notes, hint, hint_max, hint_hits, noise_total,
                                          noise_cats_all, noise_examples, dropped, dropped_txt,
                                          ms_dnotes)

        # ================= DRUG PRESENT → drug buckets ========================
        masked = self._mask(norm, mentions)
        drug_spans = [(m.start(), m.end()) for m in _DRUG_TOKEN_RE.finditer(masked)]
        focus, _, focus_hits = self.focus.score(masked, drug_spans, PROXIMITY_BOOST, PROXIMITY_CHARS)
        general, _, gen_hits = self.general.score(masked)
        compare, _, cmp_hits = self.compare.score(masked) if (our and comp) else (0.0, 0.0, [])
        res.drug_focus_score, res.general_story_score, res.comparison_score = focus, general, compare

        def best(group_names):
            ms_ = [d for d in mentions if d.canonical in group_names]
            q = max(d.quality for d in ms_)
            if len(ms_) >= 2:
                q = min(1.0, q + 0.04)            # independent repeated mentions
            return q, ms_

        def evidence_txt(names):
            parts = []
            for nm in names:
                ds = [d for d in mentions if d.canonical == nm]
                surf = sorted({(d.surface.strip(), d.method) for d in ds}, key=lambda x: DETECTION_QUALITY[x[1]],
                              reverse=True)
                shown = ", ".join(f"'{s}' = {DETECTION_LABEL[m]}" for s, m in surf[:2])
                parts.append(f"{nm} ({shown})")
            return "; ".join(parts)

        # confidence
        if our and comp:
            q_o, _ = best(our)
            q_c, _ = best(comp)
            q = min(q_o, q_c)
            specific = all(self.drugs[n].get("ms_specific") for n in our) or \
                any(self.drugs[n].get("ms_specific") for n in comp)
        elif our:
            q, _ = best(our)
            specific = any(self.drugs[n].get("ms_specific") for n in our)
        else:
            q, _ = best(comp)
            specific = any(self.drugs[n].get("ms_specific") for n in comp)
        support_raw = ms_signal_any + focus + (compare if (our and comp) else 0.0)
        support = math.tanh(support_raw / 4.0)
        base = 0.88 if specific else 0.78
        conf = q * (base + (0.99 - base) * support)
        weakest = min(mentions, key=lambda d: d.quality)
        drivers = ["all drug names spelled correctly" if weakest.method == "exact"
                   else f"weakest drug match is a {DETECTION_LABEL[weakest.method]}"]
        if support >= 0.5:
            drivers.append("clear MS / treatment context")
        elif support > 0:
            drivers.append("some MS / treatment context")
        else:
            drivers.append("no MS / treatment context words")
        if not specific:
            drivers.append("drug is also used outside MS")
        if noise_total > ms_signal_any and noise_total > 0:
            conf -= min(0.15, 0.03 * noise_total)
            drivers.append("conflicting non-MS words")
        if non_ms > 0:
            conf -= 0.06
            drivers.append("a non-MS disease is also mentioned")
        if our and comp and compare >= COMPARISON_THRESHOLD:
            conf += 0.02
        res.confidence = conf = _clip(conf)

        ctx_bits = []
        if strong_hits or ms_dnotes:   # v2: disease-context notes only (no noise notes)
            ctx_bits.append("MS named: " + _quote_list(strong_hits + [n.strip("'") for n in ms_dnotes], 3))
        if hint_hits:
            ctx_bits.append("MS clinical terms: " + _quote_list(hint_hits, 4))
        if focus_hits:
            ctx_bits.append("treatment context: " + _quote_list(focus_hits, 4))
        ctx_txt = (" Context – " + "; ".join(ctx_bits) + ".") if ctx_bits else ""
        story_txt = ""
        if general >= GENERAL_STORY_NOTE_SCORE and general > focus:
            story_txt = (f" The post is mostly a general MS story ({_quote_list(gen_hits, 3)}); "
                         f"the drug is background, but any in-scope drug mention decides the bucket.")

        if our and comp:
            res.bucket = B["OUR_VS_COMP"]
            if compare >= COMPARISON_THRESHOLD:
                cmp_txt = f" Comparison / switching language: {_quote_list(cmp_hits, 3)}."
            else:
                cmp_txt = " Both are mentioned together (no explicit comparison wording)."
            head = (f"{res.bucket}: our brand {evidence_txt(our)} AND competitor "
                    f"{evidence_txt(comp)} are both mentioned.{cmp_txt}")
        elif our:
            res.bucket = B["OUR_ONLY"]
            head = (f"{res.bucket}: {evidence_txt(our)} is mentioned and no competitor drug "
                    f"is mentioned.")
        else:
            res.bucket = B["COMP_ONLY"]
            head = (f"{res.bucket}: competitor drug(s) {evidence_txt(comp)} mentioned and "
                    f"{OUR_BRAND_LABEL} is not mentioned.")
        res.reason = (f"{head}{ctx_txt}{story_txt}{dropped_txt} Confidence {conf:.2f} "
                      f"({_level(conf)}): {', '.join(drivers)}.")
        return res

    # ------------------------------------------------------ no-drug path ----
    def _classify_no_drug(self, res, norm, strong, strong_hits, ms_evidence, ms_kinds, ms_notes,
                          hint, hint_max, hint_hits, noise_total, noise_cats, noise_examples,
                          dropped, dropped_txt, ms_dnotes=None):
        B = self.buckets
        other, _, other_hits = self.other_cond.score(norm)
        total_ms = ms_evidence + hint
        ms_named = [n for n in ms_notes if not n.startswith("(")]
        # v2: MS evidence quoted in the Reason = disease-context notes only
        ms_dis = list(ms_dnotes) if ms_dnotes is not None else ms_named
        noise_meaning = ""
        if noise_cats:
            top = max(set(noise_cats), key=noise_cats.count)
            noise_meaning = f"'MS'/keywords here point to {top}"
            if noise_examples or ms_named:
                noise_meaning += f" ({_quote_list(noise_examples + [n.strip(chr(39)) for n in ms_named if 'near' in n or ' ' in n.strip(chr(39))], 3)})"

        hint_ok = ((hint >= HINT_SCORE_THRESHOLD and len(hint_hits) >= HINT_MIN_DISTINCT)
                   or hint_max >= HINT_SINGLE_STRONG_WEIGHT)

        # 1) explicit MS
        if ms_evidence >= MS_MIN_EVIDENCE and ms_evidence + 0.5 * hint > noise_total * NOISE_DOMINANCE:
            res.bucket = B["GENERAL_MS"]
            if strong >= 3.5 or "hashtag" in ms_kinds:
                base, how = 0.88, "explicit MS term"
            elif "context" in ms_kinds:
                base, how = 0.85, "'MS' used in a disease context"
            elif strong > 0:
                base, how = 0.75, "MS abbreviation that can have other meanings"
            elif "weak" in ms_kinds:
                base, how = 0.72, "'MS' with weak disease context"
            else:
                base, how = 0.68, "bare upper-case 'MS' with nothing pointing elsewhere"
            margin = ms_evidence + 0.5 * hint - noise_total
            conf = base + (0.99 - base) * math.tanh(max(margin, 0.0) / 6.0)
            if noise_total > 0:
                conf -= min(0.20, 0.04 * noise_total)
            res.confidence = conf = _clip(conf)
            ev = []
            if strong_hits:
                ev.append("MS terms " + _quote_list(strong_hits, 4))
            if ms_dis:
                ev.append("MS usage " + ", ".join(ms_dis[:3]))
            if hint_hits:
                ev.append("supporting MS signals " + _quote_list(hint_hits, 4))
            extra = f" Weaker conflicting signal: {noise_meaning}." if noise_meaning else ""
            res.reason = (f"{B['GENERAL_MS']}: no {OUR_BRAND_LABEL} or competitor drug is mentioned and "
                          f"the post is about multiple sclerosis – " + "; ".join(ev) + f".{extra}"
                          f"{dropped_txt} Confidence {conf:.2f} ({_level(conf)}): {how}"
                          + (", plus conflicting non-MS words" if noise_total else "") + ".")
            return res

        # 2) MS not named, inferred from MS-specific clinical / symptom language
        if hint_ok and hint + ms_evidence > (noise_total + other) * NOISE_DOMINANCE:
            res.bucket = B["GENERAL_MS"]
            margin = hint + ms_evidence - noise_total - other
            conf = 0.55 + 0.27 * math.tanh(max(margin, 0.0) / 4.0) + (0.05 if hint_max >= 2.0 else 0.0)
            if ms_evidence > 0:
                conf += 0.04
            if other > 0:
                conf -= 0.08
            res.confidence = conf = _clip(conf, hi=0.88)
            extra = f" Other condition also mentioned: {_quote_list(other_hits, 2)}." if other_hits else ""
            weak_ms = f" A weak 'ms' mention ({', '.join(ms_dis[:2])}) supports it." if ms_dis else ""
            res.reason = (f"{B['GENERAL_MS']}: no drug is mentioned and 'multiple sclerosis' is not "
                          f"written out, but the post uses MS-specific clinical / symptom language: "
                          f"{_quote_list(hint_hits, 5)}.{weak_ms}{extra}{dropped_txt} Confidence {conf:.2f} "
                          f"({_level(conf)}): MS is inferred, not named.")
            return res

        # 3) weak "ms" plus some hints, nothing noisy
        if ms_evidence > 0 and ms_evidence + hint >= HINT_SCORE_THRESHOLD and noise_total == 0 and other == 0:
            res.bucket = B["GENERAL_MS"]
            conf = _clip(0.50 + 0.25 * math.tanh((ms_evidence + hint - 2) / 4.0), hi=0.80)
            res.confidence = conf
            res.reason = (f"{B['GENERAL_MS']}: no drug is mentioned; an ambiguous 'ms' "
                          f"({', '.join(ms_dis[:2])}) together with MS-related words "
                          f"{_quote_list(hint_hits, 4)} points to the disease.{dropped_txt} "
                          f"Confidence {conf:.2f} ({_level(conf)}): weak but consistent MS evidence.")
            return res

        # 4) NOISE / OTHERS
        res.bucket = B["NOISE"]
        if dropped and total_ms <= 0:
            conf = 0.88
            why = ("an in-scope drug name appears, but not in an MS sense, and nothing else "
                   "in the post relates to MS")
        elif total_ms <= 0 and noise_total <= 0:
            conf = 0.96 if other == 0 else 0.93
            why = "no in-scope drug, no MS term and no MS symptom / clinical word – unrelated content"
            if other_hits:
                why += f" (talks about {_quote_list(other_hits, 2)})"
        elif total_ms <= 0:
            conf = 0.86 + 0.12 * math.tanh(noise_total / 4.0)
            why = f"no in-scope drug and no MS-disease evidence; {noise_meaning or 'non-MS meaning'}"
        else:
            conf = 0.90 - 0.12 * total_ms + 0.04 * noise_total + 0.04 * other
            parts = []
            if noise_meaning:
                parts.append(noise_meaning)
            if other_hits:
                parts.append(f"symptoms are better explained by another condition ({_quote_list(other_hits, 2)})")
            weak = []
            if ms_named:
                weak.append("ambiguous " + ", ".join(ms_named[:2]))
            if hint_hits:
                weak.append("generic symptom words " + _quote_list(hint_hits, 3))
            parts.append("only weak MS-like signals (" + "; ".join(weak) + ") – not enough to call it MS"
                         if weak else "insufficient MS evidence")
            why = "no in-scope drug; " + "; ".join(parts)
        res.confidence = conf = _clip(conf, lo=0.40, hi=0.98)
        res.reason = (f"{B['NOISE']}: {why}.{dropped_txt} Confidence {conf:.2f} ({_level(conf)}).")
        return res


# ----------------------------------------------------------------------------
#  Module-level convenience functions
# ----------------------------------------------------------------------------
_DEFAULT_CLASSIFIER: Optional[EntityClassifier] = None


def get_classifier() -> EntityClassifier:
    global _DEFAULT_CLASSIFIER
    if _DEFAULT_CLASSIFIER is None:
        _DEFAULT_CLASSIFIER = EntityClassifier()
    return _DEFAULT_CLASSIFIER


def classify_post(clean_text: str) -> ClassificationResult:
    """Classify one CLEANED post into one bucket."""
    return get_classifier().classify_post(clean_text)


def classify_raw(raw_text) -> Tuple[str, ClassificationResult]:
    clean = clean_tweet(raw_text)
    return clean, classify_post(clean)


# ----------------------------------------------------------------------------
#  Excel / CSV pipeline
# ----------------------------------------------------------------------------
_OUTPUT_COLUMNS = [CLEAN_TEXT_COLUMN, BUCKET_COLUMN, DRUG_MENTIONS_COLUMN,
                   CONFIDENCE_COLUMN, REASON_COLUMN, "MS Score", "MS Hint Score",
                   "Noise Score", "Drug Focus Score", "General Story Score",
                   "Comparison Score", "Matched Our Drug", "Matched Competitors"]


def _is_text_dtype(series) -> bool:
    """True for object and string columns (pandas 1.x – 3.x)."""
    return pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)


def _detect_text_column(df, requested: Optional[str]) -> str:
    if requested:
        if requested in df.columns:
            return requested
        lower = {str(c).strip().lower(): c for c in df.columns}
        if requested.strip().lower() in lower:
            return lower[requested.strip().lower()]
        raise KeyError(f"Column {requested!r} not found. Columns: {list(df.columns)}")
    lower = {str(c).strip().lower(): c for c in df.columns}
    for cand in TEXT_COLUMN_CANDIDATES:
        if cand.lower() in lower and lower[cand.lower()] not in _OUTPUT_COLUMNS:
            return lower[cand.lower()]
    best, best_len = None, -1.0
    for c in df.columns:
        if c in _OUTPUT_COLUMNS:
            continue
        if _is_text_dtype(df[c]):
            avg = df[c].dropna().astype(str).str.len().mean()
            if avg and avg > best_len:
                best, best_len = c, avg
    if best is None:
        raise KeyError("Could not auto-detect the post text column; set TEXT_COLUMN in main()")
    return best


def process_dataframe(df, text_column: Optional[str] = None,
                      classifier: Optional[EntityClassifier] = None):
    clf = classifier or get_classifier()
    # re-running on an already classified file: drop old output columns first
    df = df.drop(columns=[c for c in _OUTPUT_COLUMNS if c in df.columns])
    col = _detect_text_column(df, text_column)
    log.info("Using text column: %r (%d rows)", col, len(df))
    t0 = time.time()
    cleaned, results = [], []
    for i, raw in enumerate(df[col].tolist(), 1):
        try:
            c = clean_tweet(raw)
            r = clf.classify_post(c)
        except Exception as exc:          # never let one bad row kill the run
            log.exception("Row %d failed: %s", i, exc)
            c = "" if raw is None else str(raw)
            r = ClassificationResult(BUCKETS["NOISE"], 0.30,
                                     reason=f"Processing error – please review manually ({exc}).")
        cleaned.append(c)
        results.append(r)
        if i % 2000 == 0:
            log.info("  %d / %d posts done", i, len(df))
    log.info("Classified %d posts in %.1fs", len(df), time.time() - t0)

        # Keep only the requested input columns (None / [] = keep all)
    if INPUT_COLUMNS_TO_KEEP:
        wanted = {str(c).strip().lower() for c in INPUT_COLUMNS_TO_KEEP}
        keep_cols = [c for c in df.columns if str(c).strip().lower() in wanted]
        missing = wanted - {str(c).strip().lower() for c in keep_cols}
        if missing:
            log.warning("INPUT_COLUMNS_TO_KEEP: columns not found in input: %s",
                        sorted(missing))
        out = df[keep_cols].copy()
    else:
        out = df.copy()
    # v2: output columns go AFTER all original columns (they used to be inserted
    # right after the text column, which pushed e.g. "Sentiment" behind "Reason").
    pos = len(out.columns)
    out.insert(pos, CLEAN_TEXT_COLUMN, cleaned)
    out.insert(pos + 1, BUCKET_COLUMN, [r.bucket for r in results])
    extra = pd.DataFrame([r.as_row() for r in results], index=out.index)
    for j, c in enumerate(extra.columns):
        out.insert(pos + 2 + j, c, extra[c])
    return out, results


_ILLEGAL_XLSX_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _excel_safe(df):
    """Remove characters Excel cannot store and cap cell length (32,767)."""
    df = df.copy()
    for c in df.columns:
        if _is_text_dtype(df[c]):
            df[c] = df[c].map(lambda v: _ILLEGAL_XLSX_RE.sub("", v)[:32000] if isinstance(v, str) else v)
    return df


def _no_formulas(ws):
    """Tweets starting with '=' must stay text, not become broken formulas."""
    for row in ws.iter_rows():
        for cell in row:
            if cell.data_type == "f":
                cell.data_type = "s"


def _autosize(ws, max_width=80):
    for col_cells in ws.columns:
        length = max((len(str(c.value)) if c.value is not None else 0) for c in col_cells[:200])
        ws.column_dimensions[col_cells[0].column_letter].width = min(max_width, max(10, length + 2))


def run_file(input_path: str, output_path: Optional[str] = None,
             text_column: Optional[str] = None, sheet: Optional[str] = None,
             max_rows: Optional[int] = None) -> str:
    if pd is None:
        raise RuntimeError("pandas is required for file processing: pip install pandas openpyxl")
    p = Path(input_path)
    if not p.exists():
        raise FileNotFoundError(f"Input file not found: {p.resolve()}")
    if p.suffix.lower() in (".csv", ".txt"):
        df = pd.read_csv(p, encoding_errors="replace", dtype=object)
    else:
        df = pd.read_excel(p, sheet_name=sheet if sheet is not None else 0, dtype=object)
    if max_rows:
        df = df.head(max_rows)

    out, results = process_dataframe(df, text_column)

    output_path = output_path or str(p.with_name(p.stem + "_classified.xlsx"))
    if output_path.lower().endswith(".csv"):
        out.to_csv(output_path, index=False, encoding="utf-8-sig")
        log.info("Saved -> %s", output_path)
        return output_path

    order = list(BUCKETS.values())
    counts = out[BUCKET_COLUMN].value_counts()
    summary = pd.DataFrame({"Entity Bucket": order,
                            "Posts": [int(counts.get(b, 0)) for b in order]})
    summary["Share %"] = (summary["Posts"] / max(1, len(out)) * 100).round(2)
    conf = pd.Series([r.confidence for r in results], index=out.index)
    summary["Avg Confidence"] = [round(conf[out[BUCKET_COLUMN] == b].mean(), 3)
                                 if counts.get(b, 0) else None for b in order]
    # posts per drug (a post naming two drugs counts for both)
    drug_counts: Dict[str, int] = {}
    for r in results:
        for d in filter(None, (x.strip() for x in r.drug_mentions.split(","))):
            drug_counts[d] = drug_counts.get(d, 0) + 1
    drug_names = sorted(drug_counts, key=lambda d: (d not in OUR_DRUGS, -drug_counts[d], d.casefold()))
    drug_summary = pd.DataFrame({"Drug (brand)": drug_names,
                                 "Posts mentioning": [drug_counts[d] for d in drug_names]})
    review = out[conf < LOW_CONFIDENCE_THRESHOLD]

    sheets = {"Classified": out, "Bucket Summary": summary,
              "Drug Mention Summary": drug_summary, "Low Confidence Review": review}
    with pd.ExcelWriter(output_path, engine="openpyxl") as xw:
        for name, frame in sheets.items():
            _excel_safe(frame).to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            _no_formulas(ws)
            ws.freeze_panes = "A2"
            _autosize(ws)
    log.info("Bucket counts:\n%s", summary.to_string(index=False))
    log.info("Low-confidence rows for review: %d", len(review))
    log.info("Saved -> %s", output_path)
    return output_path


# ----------------------------------------------------------------------------
#  Self-test   (case, expected bucket key, expected Drug Mentions or None)
# ----------------------------------------------------------------------------
SELFTEST_CASES: List[Tuple[str, str, Optional[str]]] = [
    # bucket definitions
    ("Had my 6-month Ocrevus infusion today, 4 hours in the chair, so tired after.", "OUR_ONLY", "Ocrevus"),
    ("Switching from Ocrevus to Kesimpta because I hate the infusion days.", "OUR_VS_COMP", "Ocrevus, Kesimpta"),
    ("Kesimpta monthly pen is easy, no more infusion chairs.", "COMP_ONLY", "Kesimpta"),
    ("Waited 8 months to see a neurologist. Brain fog is the worst part of MS.", "GENERAL_MS", ""),
    # formerly "General MS & Ocrevus" -> now Ocrevus Only
    ("Living with MS for 6 years, on Ocrevus now. The hardest part is people not believing my fatigue is real.",
     "OUR_ONLY", "Ocrevus"),
    # formerly "General MS, Ocrevus & Competitor" -> now Ocrevus vs. Competitor
    ("Been through Tecfidera, then Ocrevus. Honestly the bigger issue is that nobody treats the bladder problems.",
     "OUR_VS_COMP", "Ocrevus, Tecfidera"),
    # formerly "General MS & Competitor" -> now Competitor Only
    ("Diagnosed 10 years ago, been on Tysabri forever. The fatigue and heat are what really wreck my days, nobody understands",
     "COMP_ONLY", "Tysabri"),
    ("MS Teams keeps crashing during my meetings.", "NOISE", ""),
    # cleaning + handles
    ("RT @OcrevusNews: FDA approves Ocrevus Zunovo subcutaneous injection https://t.co/abc123", "OUR_ONLY", "Ocrevus"),
    ("RT @MS_Gaming: new update for MS Flight Simulator is out https://t.co/x", "NOISE", ""),
    # misspellings / variants / generics -> brand names
    ("my ocrevis infusion got pushed again, insurance denied it", "OUR_ONLY", "Ocrevus"),
    ("anyone on kesimta? the injections give me headaches", "COMP_ONLY", "Kesimpta"),
    ("tysarbi day, JCV test came back negative", "COMP_ONLY", "Tysabri"),
    ("Ocravus vs Briumvi, which one did your neuro recommend??", "OUR_VS_COMP", "Ocrevus, Briumvi"),
    ("started ocrelizumab last week after 3 yrs on natalizumab", "OUR_VS_COMP", "Ocrevus, Tysabri"),
    ("0CREVUS round 4 done 💪", "OUR_ONLY", "Ocrevus"),
    ("O c r e v u s infusion tomorrow, nervous", "OUR_ONLY", "Ocrevus"),
    ("ocreeevus day!!! #MSWarrior", "OUR_ONLY", "Ocrevus"),
    ("my neuro wants me on Kesimpta, Briumvi or Ocrevus. so confused", "OUR_VS_COMP", "Ocrevus, Briumvi, Kesimpta"),
    ("Tysabri and Copaxone and Gilenya all failed me", "COMP_ONLY", "Copaxone, Gilenya, Tysabri"),
    ("#OcrevusDay done ✅", "OUR_ONLY", "Ocrevus"),
    ("been on tec for 3 years with my MS, no relapses", "COMP_ONLY", "Tecfidera"),
    ("fingolimod made my heart rate drop on the first dose", "COMP_ONLY", "Gilenya"),
    # comparisons
    ("Chose Kesimpta over Ocrevus because I can do it at home", "OUR_VS_COMP", "Ocrevus, Kesimpta"),
    ("Is Briumvi better than Ocrevus? shorter infusion time", "OUR_VS_COMP", "Ocrevus, Briumvi"),
    # general MS, no drug (explicit and inferred)
    ("#MSWarrior day 3 of a relapse, legs are numb and the MS hug is back", "GENERAL_MS", ""),
    ("Multiple sclerosis awareness week starts today #MSAwareness", "GENERAL_MS", ""),
    ("new lesions on my MRI, neuro wants to talk about starting a DMT", "GENERAL_MS", ""),
    ("Lhermitte's sign again every time I bend my neck. pins and needles all down my arms", "GENERAL_MS", ""),
    ("my mom has ms and she is so strong", "GENERAL_MS", ""),
    ("Mulitple sclerosis research funding cut again", "GENERAL_MS", ""),
    ("that squeezing around my ribs is back and I can't feel my feet again. neurologist on monday",
     "GENERAL_MS", ""),
    ("MS is such a cruel disease, nobody sees the fatigue", "GENERAL_MS", ""),
    ("12 years since my RRMS diagnosis and still walking", "GENERAL_MS", ""),
    # noise / others
    ("Ms. Johnson gave us extra homework again", "NOISE", ""),
    ("Planning to do MS in CS from the US, GRE prep going on", "NOISE", ""),
    ("MS Dhoni finishing off in style again #CSK", "NOISE", ""),
    ("Ping is 200ms on this server, unplayable", "NOISE", ""),
    ("Driving through Jackson, MS tonight, storm warning", "NOISE", ""),
    ("Rituximab plus chemo for my dad's lymphoma, round 3 done", "NOISE", ""),
    ("Natalizumab for Crohn's disease, anyone tried it?", "NOISE", ""),
    ("lunch was great today", "NOISE", ""),
    ("LC-MS/MS results look clean for the metabolomics run", "NOISE", ""),
    ("buy MS pipes and MS sheets at best price", "NOISE", ""),
    ("MS is hiring 10,000 engineers for the Copilot team", "NOISE", ""),
    ("first DMT trip changed my life, ayahuasca next", "NOISE", ""),
    ("numbness and tingling in my feet, my diabetes is acting up again", "NOISE", ""),
    ("so tired today, fatigue is real after this week", "NOISE", ""),
    ("ms u so much babe", "NOISE", ""),
    ("our MS was accepted in the journal after 2 rounds of reviewers", "NOISE", ""),
    ("That kismet moment when you meet your idol", "NOISE", ""),
    ("1000W RMS amplifier for my subwoofer", "NOISE", ""),
    ("ALS is a brutal disease, amyotrophic lateral sclerosis took my uncle", "NOISE", ""),
    # tricky drug + MS
    ("Rituximab for MS is off label but my neuro says it works as well", "COMP_ONLY", "Rituxan"),
    ("#Ocrevus", "OUR_ONLY", "Ocrevus"),
    ("Roche Q3: Ocrevus sales up 9% on strong demand for Zunovo", "OUR_ONLY", "Ocrevus"),
    ("interferon for my hepatitis C finally finished", "NOISE", ""),
    ("15 years on interferon for my MS before switching", "COMP_ONLY", "Interferon beta (generic)"),
    ("Avonex shots every week made me feel like flu", "COMP_ONLY", "Avonex"),
    # ---- v2 regression cases (paraphrased from errors found in validation) ----
    # 'it' / 'me' are ordinary words, not "MS in IT" (Master's)
    ("Nerve damage from low B12 can mimic MS. It is a different disease entirely.", "GENERAL_MS", ""),
    ("If everyone around here has MS, it will cost billions.", "GENERAL_MS", ""),
    ("Got the admit for MS IT at the university, fall intake", "NOISE", ""),
    # a title elsewhere must not cancel "suffers from MS"
    ("Ms Carter suffers from MS and the airline ignored it", "GENERAL_MS", ""),
    # IFN-gamma is a cytokine, not interferon beta
    ("Patients showed lower IFN-γ and IL-17 levels; EBV is tied to multiple sclerosis", "GENERAL_MS", ""),
    # short forms need treatment words right next to them
    ("Wheelchair users with advanced multiple sclerosis can't ride it, it is just 'cool tec.' to them",
     "GENERAL_MS", ""),
    ("Two years on tec now, my neuro is happy", "COMP_ONLY", "Tecfidera"),
    # MS-specific brand is not off-label because another drug's disease is nearby
    ("Pharma sales were led by the multiple myeloma drug Darzalex and Novartis's Kesimpta",
     "COMP_ONLY", "Kesimpta"),
    ("ofatumumab for CLL patients, cycle 2 today", "NOISE", ""),
    # implicit MS: trial / progression language, "therapies" plural
    ("CAR-T readout: most participants had stable disability scores despite documented progression "
     "on high-efficacy therapies", "GENERAL_MS", ""),
    # transverse myelitis alone is not MS
    ("Remember when the first transverse myelitis cases after the shots were called coincidence?",
     "NOISE", ""),
]


def selftest(verbose: bool = True) -> bool:
    clf = get_classifier()
    ok = 0
    for raw, exp_key, exp_drugs in SELFTEST_CASES:
        clean = clean_tweet(raw)
        r = clf.classify_post(clean)
        exp = BUCKETS[exp_key]
        good = r.bucket == exp and (exp_drugs is None or r.drug_mentions == exp_drugs)
        ok += good
        if verbose or not good:
            mark = "PASS" if good else "FAIL"
            print(f"[{mark}] {r.bucket:<24} conf={r.confidence:.2f} drugs=[{r.drug_mentions}] | {clean}")
            if not good:
                print(f"        expected: {exp}  drugs=[{exp_drugs}]\n        reason  : {r.reason}")
    print(f"\n{ok}/{len(SELFTEST_CASES)} self-test cases passed")
    return ok == len(SELFTEST_CASES)


# ----------------------------------------------------------------------------
#  MAIN – set your inputs here (no command-line arguments needed)
# ----------------------------------------------------------------------------
def main() -> int:
    # ============================ YOUR INPUTS ============================
    INPUT_FILE = r"Output/Entire data output/Splited_unique_duplicate_twitter_data_MS.xlsx"   # .xlsx / .xls / .csv
    OUTPUT_FILE = r"Output/Entire data output/Unique_twiter_data_only_for_MS_classified.xlsx"
    TEXT_COLUMN = "Message"              # e.g. "Post Content"; None = auto-detect
    SHEET_NAME = "Unique"                # None -> first sheet
    MAX_ROWS = None                      # e.g. 100 for a quick trial run
    RUN_SELFTEST = False                 # True -> run built-in test cases only
    SINGLE_TEXT = None                   # e.g. "Had my Ocrevus infusion today"
    QUIET = False                        # True -> only warnings in the log
    # =====================================================================

    logging.basicConfig(level=logging.WARNING if QUIET else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if RUN_SELFTEST:
        return 0 if selftest() else 1
    if SINGLE_TEXT:
        clean, r = classify_raw(SINGLE_TEXT)
        print(f"Cleaned      : {clean}\nBucket       : {r.bucket}\n"
              f"Drug Mentions: {r.drug_mentions}\nConfidence   : {r.confidence:.2f}\n"
              f"Reason       : {r.reason}")
        return 0
    run_file(INPUT_FILE, OUTPUT_FILE, TEXT_COLUMN, SHEET_NAME, MAX_ROWS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
