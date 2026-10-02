"""Language whitelist with zh_Hant as the default.

The SDK forwards language codes without validation. This mirrors
notebooklm-py's CLI language code list so MCP calls fail early on typos.
"""

from __future__ import annotations

DEFAULT_LANGUAGE = "zh_Hant"

SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "zh_Hans": "中文（简体）",
    "zh_Hant": "中文（繁體）",
    "es": "Español",
    "es_419": "Español (Latinoamérica)",
    "es_MX": "Español (México)",
    "hi": "हिन्दी",
    "ar_001": "العربية",
    "ar_eg": "العربية (مصر)",
    "pt_BR": "Português (Brasil)",
    "pt_PT": "Português (Portugal)",
    "bn": "বাংলা",
    "ru": "Русский",
    "ja": "日本語",
    "pa": "ਪੰਜਾਬੀ",
    "de": "Deutsch",
    "jv": "Basa Jawa",
    "ko": "한국어",
    "fr": "Français",
    "fr_CA": "Français (Canada)",
    "te": "తెలుగు",
    "vi": "Tiếng Việt",
    "mr": "मराठी",
    "ta": "தமிழ்",
    "tr": "Türkçe",
    "ur": "اردو",
    "it": "Italiano",
    "th": "ไทย",
    "gu": "ગુજરાતી",
    "fa": "فارسی",
    "pl": "Polski",
    "uk": "Українська",
    "ml": "മലയാളം",
    "kn": "ಕನ್ನಡ",
    "or": "ଓଡ଼ିଆ",
    "my": "မြန်မာဘာသာ",
    "sw": "Kiswahili",
    "nl_NL": "Nederlands",
    "ro": "Română",
    "hu": "Magyar",
    "el": "Ελληνικά",
    "cs": "Čeština",
    "sv": "Svenska",
    "be": "Беларуская",
    "bg": "Български",
    "hr": "Hrvatski",
    "sk": "Slovenčina",
    "da": "Dansk",
    "fi": "Suomi",
    "nb_NO": "Norsk Bokmål",
    "nn_NO": "Norsk Nynorsk",
    "he": "עברית",
    "iw": "עברית",
    "id": "Bahasa Indonesia",
    "ms": "Bahasa Melayu",
    "fil": "Filipino",
    "ceb": "Cebuano",
    "sr": "Српски",
    "sl": "Slovenščina",
    "sq": "Shqip",
    "mk": "Македонски",
    "lt": "Lietuvių",
    "lv": "Latviešu",
    "et": "Eesti",
    "hy": "Հայերեն",
    "ka": "ქართული",
    "az": "Azərbaycanca",
    "af": "Afrikaans",
    "am": "አማርኛ",
    "eu": "Euskara",
    "ca": "Català",
    "gl": "Galego",
    "is": "Íslenska",
    "la": "Latina",
    "ne": "नेपाली",
    "ps": "پښتو",
    "sd": "سنڌي",
    "si": "සිංහල",
    "ht": "Kreyòl Ayisyen",
    "kok": "कोंकणी",
    "mai": "मैथिली",
}


def resolve_language(code: str | None) -> str:
    """Return a validated language code, defaulting to zh_Hant."""
    if code is None:
        return DEFAULT_LANGUAGE
    if code in SUPPORTED_LANGUAGES:
        return code
    hint = ""
    if "-" in code:
        hint = " Use underscore form (e.g. 'zh_Hant', not 'zh-TW')."
    raise ValueError(
        f"Unknown language code {code!r}.{hint} Supported: {', '.join(SUPPORTED_LANGUAGES)}"
    )
