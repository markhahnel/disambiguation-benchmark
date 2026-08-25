"""Stratum-routing heuristics against realistic multilingual strings."""

from disambig.signals import dominant_non_latin_script, looks_multi_affiliation, token_signals


def test_chinese_detected() -> None:
    assert dominant_non_latin_script("中国科学院物理研究所, 北京") == "chinese"


def test_japanese_kana_detected() -> None:
    assert dominant_non_latin_script("東京大学大学院理学系研究科です") == "japanese"


def test_korean_detected() -> None:
    assert dominant_non_latin_script("서울대학교 자연과학대학") == "korean"


def test_cyrillic_detected() -> None:
    assert dominant_non_latin_script("Московский государственный университет") == "russian_cyrillic"


def test_arabic_detected() -> None:
    assert dominant_non_latin_script("جامعة الملك سعود، الرياض") == "arabic"


def test_thai_detected() -> None:
    assert dominant_non_latin_script("จุฬาลงกรณ์มหาวิทยาลัย") == "thai"


def test_latin_string_returns_none() -> None:
    assert dominant_non_latin_script("Department of Physics, University of Oxford") is None


def test_mostly_latin_with_accents_returns_none() -> None:
    assert dominant_non_latin_script("Université de Montréal, Québec") is None


def test_latin_majority_with_cjk_fragment_returns_none() -> None:
    text = "Institute of Physics, Chinese Academy of Sciences (物理研究所), Beijing, China"
    assert dominant_non_latin_script(text) is None


def test_empty_and_symbol_only_strings() -> None:
    assert dominant_non_latin_script("") is None
    assert dominant_non_latin_script("123 --- !!!") is None


def test_multi_affiliation_numbered() -> None:
    assert looks_multi_affiliation(
        "1. University of Oslo; 2. Oslo University Hospital, Norway"
    )


def test_multi_affiliation_semicolon_two_institutions() -> None:
    assert looks_multi_affiliation(
        "Institute of Cancer Research, London; University College London, UK"
    )


def test_single_affiliation_with_commas_is_not_multi() -> None:
    assert not looks_multi_affiliation(
        "Department of Chemistry, University of Cambridge, Cambridge, UK"
    )


def test_hospital_tokens_multilingual() -> None:
    assert token_signals("Hôpital Necker-Enfants Malades, Paris")["hospital"]
    assert token_signals("東京大学医学部附属病院")["hospital"]
    assert token_signals("Guy's and St Thomas' NHS Trust")["hospital"]
    assert not token_signals("University of Oxford")["hospital"]


def test_government_and_company_tokens() -> None:
    assert token_signals("Max-Planck-Institut für Kohlenforschung")["government"]
    assert token_signals("National Institute of Standards and Technology")["government"]
    assert token_signals("Pfizer Inc., Groton, CT")["company"]
    assert not token_signals("University of Leeds")["company"]


def test_contains_normalized_ignores_case_and_diacritics() -> None:
    from disambig.signals import contains_normalized

    assert contains_normalized(
        "Institut des Systèmes, Université Pierre-et-Marie-Curie - CNRS",
        "Universite Pierre et Marie Curie",
    )
    assert contains_normalized("UNIVERSITÉ PARIS DIDEROT", "paris diderot")
    assert not contains_normalized("Sorbonne Université, Paris", "Paris Diderot")
