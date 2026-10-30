import pytest

from app.discovery.rules import apply_rules, graduates_too_late, locality

ML_DESC = "Join our machine learning team building recommendation models. Based in New York, NY."


def test_good_posting_passes():
    passed, reason = apply_rules("Machine Learning Intern", "New York, NY", ML_DESC)
    assert passed and reason == ""


def test_us_phrases_match_regardless_of_case():
    # "United States" / "USA" appear title-cased or upper-cased in real postings
    for location in ("United States", "Remote - USA", "Remote (US-Remote)"):
        passed, _ = apply_rules("Data Science Intern", location, ML_DESC)
        assert passed, location


def test_state_codes_stay_case_sensitive():
    # lowercase prose words that collide with state codes must not count as a US signal
    passed, reason = apply_rules(
        "ML Intern", "Toronto", "Work in ml research or on our platform with hi-tech tools."
    )
    assert not passed and reason == "geo:no-us-signal"


def test_no_early_career_signal():
    passed, reason = apply_rules("Machine Learning Engineer", "NY", ML_DESC)
    assert not passed and reason == "stage:no-early-career-signal"


def test_senior_term_rejects():
    passed, reason = apply_rules("Senior Machine Learning Intern Manager", "NY", ML_DESC)
    assert not passed and reason == "stage:senior-term-in-title"


def test_non_ai_field_rejects():
    passed, reason = apply_rules(
        "Accounting Intern", "New York, NY", "Support the finance team with bookkeeping."
    )
    assert not passed and reason == "field:no-ai-ml-ds-terms"


# an example configuration: graduating spring 2027, campus near Pittsburgh
LOCAL = locality(2027, "Pittsburgh, Monroeville")


def test_locality_needs_both_settings():
    assert locality(None, "Pittsburgh") is None
    assert locality(2027, " , ") is None


def test_school_term_onsite_outside_local_area_rejects():
    titles = ("Fall 2026 ML Intern", "ML Intern (Autumn '26)", "Spring 2027 Data Science Co-op")
    for title in titles:
        passed, reason = apply_rules(title, "New York, NY", ML_DESC, local=LOCAL)
        assert not passed, title
        assert reason == "geo:school-term-not-local-or-remote"
        assert apply_rules(title, "New York, NY", ML_DESC)[0]  # rule off without config


def test_school_term_local_or_remote_passes():
    assert apply_rules("Fall 2026 ML Intern", "Pittsburgh, PA", ML_DESC, local=LOCAL)[0]
    assert apply_rules("Spring 2027 ML Intern", "Monroeville, PA (Hybrid)", ML_DESC, local=LOCAL)[0]
    assert apply_rules("Fall 2026 ML Intern", "Remote - USA", ML_DESC, remote=True, local=LOCAL)[0]


def test_winter_term_and_feed_terms_count_as_school_terms():
    assert apply_rules("Winter 2027 ML Co-op", "Seattle, WA", ML_DESC, local=LOCAL) == (
        False,
        "geo:school-term-not-local-or-remote",
    )
    # a feed's term tags count, not just the title
    winter = "Winter 2026, Spring 2027"
    assert not apply_rules("ML Intern", "Seattle, WA", ML_DESC, terms=winter, local=LOCAL)[0]
    assert apply_rules("ML Intern", "Pittsburgh, PA", ML_DESC, terms="Spring 2027", local=LOCAL)[0]


def test_feed_terms_stand_in_for_an_early_career_title():
    assert apply_rules("Machine Learning Engineer", "NY", ML_DESC)[1] == (
        "stage:no-early-career-signal"
    )
    assert apply_rules("Machine Learning Engineer", "NY", ML_DESC, terms="New Grad")[0]
    # ...but never for senior titles
    assert not apply_rules("Senior ML Engineer", "NY", ML_DESC, terms="New Grad")[0]


def test_post_graduation_terms_pass_anywhere():
    # Summer 2027 and undated postings start after a spring 2027 graduation
    assert apply_rules("Summer 2027 Machine Learning Intern", "Seattle, WA", ML_DESC, local=LOCAL)[
        0
    ]
    assert apply_rules("Machine Learning Intern", "Seattle, WA", ML_DESC, local=LOCAL)[0]


def test_sponsorship_negation_rejects():
    for phrase in (
        "We are unable to sponsor visas for this role.",
        "Applicants must be a U.S. citizen.",
        "This position requires an active security clearance.",
        "We cannot consider candidates without sponsorship. No visa sponsorship is available.",
    ):
        passed, reason = apply_rules("ML Intern", "New York, NY", f"{ML_DESC} {phrase}")
        assert not passed, phrase
        assert reason == "sponsor:explicit-negation"


def test_field_stems_match_whole_words():
    for title in ("Data Scientist Intern", "Applied Scientist Intern", "Data Engineering Co-op"):
        assert apply_rules(title, "NY", "")[0], title


@pytest.mark.parametrize(
    "requirement,too_late",
    [
        ("Candidates must be graduating between December 2027 and May 2028.", True),
        ("Open to the Class of 2028.", True),
        ("Expected graduation: Spring 2029.", True),
        ("Expected graduation date between December 2026 and June 2027.", False),
        ("Graduating by May 2028 with a BS in CS.", False),  # an upper bound: earlier is fine
        ("Must have graduated in 2025 or later.", False),
        ("Recent graduates welcome; start date in September 2028.", False),  # not a grad date
        ("Strong Python skills.", False),
    ],
)
def test_graduation_windows_after_the_candidate(requirement, too_late):
    assert graduates_too_late(requirement, 2027) is too_late
    passed, reason = apply_rules(
        "ML Intern", "NY", f"{ML_DESC} {requirement}", graduation_year=2027
    )
    assert passed is not too_late
    assert apply_rules("ML Intern", "NY", f"{ML_DESC} {requirement}")[0]  # off without a year


def test_postdocs_are_not_early_career_even_from_a_new_grad_feed():
    for title in ("Machine Learning Postdoctoral Researcher", "Postdoc - Explainable AI"):
        assert apply_rules(title, "Livermore, CA", ML_DESC, terms="New Grad") == (
            False,
            "stage:senior-term-in-title",
        )
