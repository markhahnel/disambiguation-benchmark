"""Email redaction: local part removed, institutional domain kept, counts exact."""

from disambig.redact import redact_emails, redact_many


def test_publisher_electronic_address_suffix() -> None:
    prefix = "Faculty of Medicine, University of Barcelona, Spain. Electronic address: "
    redacted, count = redact_emails(prefix + "sergio.b@gmail.com")
    assert redacted == prefix + "redacted@gmail.com"
    assert count == 1


def test_institutional_domain_is_kept_as_a_cue() -> None:
    redacted, count = redact_emails("Dept of Neuroscience, KU Leuven. jill.kries@kuleuven.be")
    assert redacted.endswith("redacted@kuleuven.be")
    assert count == 1


def test_multiple_addresses_and_plus_tags() -> None:
    text = "a.b+tag@mail.example.org; C_D%x@sub.domain.ac.uk"
    redacted, count = redact_emails(text)
    assert redacted == "redacted@mail.example.org; redacted@sub.domain.ac.uk"
    assert count == 2


def test_no_address_is_untouched() -> None:
    text = "Institute of Physics, Chinese Academy of Sciences, Beijing 100190"
    assert redact_emails(text) == (text, 0)


def test_at_sign_without_a_domain_is_not_an_email() -> None:
    text = "Research @ Scale Lab, University of Nowhere"
    assert redact_emails(text) == (text, 0)


def test_cjk_and_rtl_text_survive_untouched() -> None:
    text = "中国科学院物理研究所 · جامعة الملك سعود · name@ksu.edu.sa"
    redacted, count = redact_emails(text)
    assert redacted.startswith("中国科学院物理研究所 · جامعة الملك سعود · ")
    assert redacted.endswith("redacted@ksu.edu.sa")
    assert count == 1


def test_redact_many_counts_across_strings() -> None:
    out, total = redact_many(["x@y.org", "plain", "p@q.edu and r@s.edu"])
    assert out == ["redacted@y.org", "plain", "redacted@q.edu and redacted@s.edu"]
    assert total == 3
