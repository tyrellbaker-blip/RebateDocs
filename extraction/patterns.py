"""
Centralized patterns and lookups.

- MONEY_REGEX: dollar amounts like "$3,500" or "750".
- LABEL_LEXICON: known headings + synonyms (helps detect sections).
- MODEL_NORMALIZER: lowercase variants -> canonical model names.
- MODEL_KEYS: quick allowlist for model detection (lowercase prefixes).

These live here so extraction rules stay readable and we change patterns in one place.
"""


# Money strings like "$3,500" or "$500 - $1,500"
# NOTE: single backslashes—do not double-escape!
MONEY_REGEX = r'(?i)\$\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?'

# Canonical program names -> synonyms we might see in headings/tables.
# Add synonyms you encounter—detection is substring-based, so be liberal.
LABEL_LEXICON = {
    "retail customer bonus": {
        "syn": [
            "retail customer bonus", "customer bonus",
            "retail customer bonus – ev", "retail customer bonus - ev"
        ]
    },
    "dealer bonus": {
        "syn": ["dealer bonus", "lease dealer bonus", "dealer bonus - ev", "lease dealer bonus - ev"]
    },
    "apr customer bonus": {
        "syn": ["apr customer bonus", "apr customer bonus – ev", "apr customer bonus - ev"]
    },
    "loyalty bonus": {
        "syn": ["loyalty bonus", "loyalty code bonus", "tiguan loyalty code bonus"]
    },
    "final payout": {
        "syn": ["final payout", "final payout bonus", "final pay"]
    },
    "target achievement bonus": {
        "syn": ["target achievement bonus", "target achievement", "tab", "payment per unit"]
    },
    "vfi program": {
        "syn": ["vfi program", "volkswagen fleet incentive", "fleet incentive", "dealer cash"]
    },
    # Generic catch-all that still classifies content as “rebate”
    "bonus": {"syn": ["bonus", "rebate", "customer rebate"]},
}

# Normalize model strings → canonical display form
# Keys are lowercase; values are desired output.
MODEL_NORMALIZER = {
    "id.4": "ID.4",
    "id 4": "ID.4",
    "id. buzz": "ID. Buzz",
    "id buzz": "ID. Buzz",
    "tiguan": "Tiguan",
    "atlas": "Atlas",
    "atlas cross sport": "Atlas Cross Sport",
    "taos": "Taos",
    "golf gti": "Golf GTI",
    "jetta gli": "Jetta GLI",
    "jetta": "Jetta",
    "atlas peak edition": "Atlas Peak Edition",
}

# Helpful for quick membership checks (lowercase)
MODEL_KEYS = list(MODEL_NORMALIZER.keys())

