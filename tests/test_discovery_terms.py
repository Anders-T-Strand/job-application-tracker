import pytest

from app.discovery.terms import bucket, labels, school_terms


@pytest.mark.parametrize(
    "title,terms,description,expected",
    [
        # explicit feed terms
        ("ML Engineer Intern", "Winter 2026, Spring 2027, Summer 2027", "", "final_term"),
        ("ML Engineer Intern", "Summer 2027", "", "later"),
        ("Data Scientist I", "New Grad", "", "new_grad"),
        ("2027 Graduate Intern", "Internship", "", "unspecified"),  # an internship, no season
        # titles
        ("Data Science Co-op - Spring 2027", None, "", "final_term"),
        ("AI Intern (Winter '27)", None, "", "final_term"),
        ("Machine Learning Intern - Summer 2027", None, "", "later"),
        ("Software Engineer, New Grad 2027", None, "", "new_grad"),
        ("Machine Learning Engineer - Early Career", None, "", "new_grad"),
        # an ATS posting naming its season in the description
        ("Machine Learning Intern", None, "Join us for Summer 2027 in Austin.", "later"),
        ("Machine Learning Intern", None, "A January 2027 start, 6 months.", "final_term"),
        ("Machine Learning Intern", None, "Build models.", "unspecified"),
        # the title wins over a description that mentions graduation timing
        ("ML Intern - Summer 2027", None, "Open to Spring 2027 graduates.", "later"),
    ],
)
def test_bucket(title, terms, description, expected):
    assert bucket(title, terms, description, year=2027) == expected


def test_cycle_follows_the_graduation_year():
    assert bucket("ML Intern - Spring 2028", year=2028) == "final_term"
    assert bucket("ML Intern - Spring 2027", year=2028) == "unspecified"  # already past
    assert bucket("ML Intern - Summer 2028", year=2028) == "later"
    assert labels(2028)["final_term"] == "Winter/Spring '28"
    assert school_terms(2028).search("Fall 2027 co-op")
    assert not school_terms(2028).search("Summer 2028 internship")


def test_without_a_graduation_year_only_new_grad_is_told_apart():
    assert bucket("ML Intern - Spring 2027") == "unspecified"
    assert bucket("Data Scientist, New Grad") == "new_grad"
