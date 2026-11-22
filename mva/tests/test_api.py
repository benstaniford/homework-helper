"""Tests for the high-level MvaCanvas API (mocked HTTP)."""


def test_get_self(api):
    me = api.get_self()
    assert me.id == 8101
    assert me.name == "Ollie Student"


def test_get_courses(api):
    courses = api.get_courses()
    assert {c.id for c in courses} == {3517, 2799}
    bio = next(c for c in courses if c.id == 3517)
    assert bio.course_code == "10BioTer"


def test_get_todo(api):
    todo = api.get_todo()
    assert len(todo) == 1
    assert todo[0].assignment_id == 312378
    assert todo[0].source == "todo"
    assert todo[0].is_overdue  # due 2026-11-16, before the fixed 'now' in tests runs


def test_upcoming_homework_merges_and_sorts(api):
    hw = api.get_upcoming_homework()
    ids = [h.assignment_id for h in hw]

    # The outstanding to-do (past-due, unsubmitted) must be present...
    assert 312378 in ids
    # ...and the future-dated Biology assignment must be present...
    assert 400001 in ids
    # ...but submitted and null-dated assignments must NOT appear.
    assert 399000 not in ids            # already submitted
    assert 400002 not in ids            # no deadline
    assert 500001 not in ids            # Maths, no deadline

    # No duplicates even though 312378 is in both todo and the assignments list.
    assert ids.count(312378) == 1

    # Sorted soonest-first: the Nov to-do before the Jan assignment.
    assert ids.index(312378) < ids.index(400001)


def test_upcoming_homework_without_todo(api):
    hw = api.get_upcoming_homework(include_todo=False)
    ids = [h.assignment_id for h in hw]
    # Only the future-dated assignment survives.
    assert ids == [400001]


def test_get_modules_with_items(api):
    mods = api.get_modules(3517)
    wk11 = next(m for m in mods if m.id == 77961)
    assert wk11.state == "started"
    types = [it.type for it in wk11.items]
    assert "Assignment" in types and "Page" in types and "File" in types


def test_find_module_for_assignment(api):
    mod = api.find_module_for_assignment(3517, 400001)
    assert mod is not None
    assert mod.id == 77961


def test_get_current_module(api):
    mod = api.get_current_module(3517)
    assert mod is not None
    assert mod.id == 77961  # the 'started' week, re-fetched with items
    assert any(it.type == "Assignment" for it in mod.items)


def test_materials_for_homework(api):
    # Build the homework for the future Biology assignment, then get its materials.
    hw = next(h for h in api.get_upcoming_homework() if h.assignment_id == 400001)
    materials = api.get_materials_for_homework(hw)
    titles = [m.title for m in materials]

    # Should include the page, the file, and the task itself...
    assert "Osmosis notes" in titles
    assert "Practical worksheet.pdf" in titles
    # ...but exclude the SubHeader (layout only).
    assert "USEFUL RESOURCES" not in titles
    # Every returned material carries a usable link or page slug.
    for m in materials:
        assert m.html_url or m.page_url or m.url


def test_current_module_uses_week_commencing_not_state(mock_rsps, monkeypatch):
    """An old week with an unfinished task stays 'started'; the w/c date must win."""
    from datetime import datetime, timezone

    import responses

    from mva_canvas import api as api_mod
    from mva_canvas import MvaCanvas
    from tests.conftest import API, BASE_URL, TOKEN

    monkeypatch.setattr(api_mod, "now_utc", lambda: datetime(2026, 11, 22, 12, tzinfo=timezone.utc))
    # Newest week listed first, so the old week has the highest position.
    modules = [
        {"id": 3, "name": "WEEKS 11-12 - Genetics (w/c 30th November)", "position": 1, "state": "locked"},
        {"id": 2, "name": "WEEKS 9-10 - Osmosis practical (w/c 16th November)", "position": 2, "state": "started"},
        {"id": 1, "name": "WEEKS 1-2 - Viruses (w/c 7th Sept)", "position": 3, "state": "started"},
    ]
    mock_rsps.add(responses.GET, f"{API}/courses/9/modules", json=modules, status=200)
    mock_rsps.add(responses.GET, f"{API}/courses/9/modules/2", json=modules[1], status=200)

    mod = MvaCanvas(token=TOKEN, base_url=BASE_URL).get_current_module(9)
    assert mod.id == 2


def test_parse_week_start():
    from datetime import date

    from mva_canvas.models import parse_week_start as p

    today = date(2026, 11, 22)
    assert p("Osmosis (w/c 16th November)", today) == date(2026, 11, 16)
    assert p("Viruses (w/c 7th Sept)", today) == date(2026, 9, 7)
    assert p("Revision W/C 4 Jan", today) == date(2027, 1, 4)
    assert p("Week 3 w/c 21/09", today) == date(2026, 9, 21)
    assert p("Week 3 w/c 21.09.26", today) == date(2026, 9, 21)
    assert p("Week 1&2: Decimal Numbers", today) is None
    assert p("WEEKS 1-2 - Marking 3 Marks", today) is None
    assert p("Course Introduction", today) is None


def test_parse_week_start_variants():
    from datetime import date

    from mva_canvas.models import parse_week_start as p

    today = date(2026, 11, 22)
    for name in ["W.C. 16th November", "WC 16 Nov", "w/b 16th November", "week commencing 16th Nov",
                 "Week Beginning: Monday 16th November", "w/c Monday 16/11", "w/c 16th of November"]:
        assert p(name, today) == date(2026, 11, 16), name


def test_parse_week_start_real_module_names():
    """Module names as the MVA subject courses actually write them (Nov 2026)."""
    from datetime import date

    from mva_canvas.models import parse_week_start as p

    today = date(2026, 11, 22)
    cases = {
        "WEEKS 9-10 - The mole and ASSESSMENT  (Mon 16th Nov 2026)": date(2026, 11, 16),
        "MODULE 5: WEEKS 9-10: REVISION AND IN-CLASS SUBMIT (16th Nov)": date(2026, 11, 16),
        "WEEKS 9-10  How to write an analytical literature essay + first timed assessment "
        "(Monday 16 November, 2026)": date(2026, 11, 16),
        "WEEKS 5-6 - AN INSPECTOR CALLS - Act 1 (Monday October 5 2026)": date(2026, 10, 5),
        "Weeks 7-8 (Mon 19th Nov) Hazardous Environments (Mon 2 Nov 2026)": date(2026, 11, 2),
        "MODULE 5 - WEEKS 9-10 The methods of Nazi control- 16th Nov ": date(2026, 11, 16),
        "Week 9&10: Indices, Rearranging & Progress Check  (16 Nov 2026)": date(2026, 11, 16),
        "Monday 7th September, Weeks 1-2, MODULE 1- Creation, Stewardship": date(2026, 9, 7),
        "Weeks 9-10: Life After Death and the Purpose of Life - 16th November": date(2026, 11, 16),
        "WEEKS 5-6 - Biological molecules and enzymes  - (5th October 2025)": date(2026, 10, 5),
        "WEEKS 1-2 - INTRODUCTION & GODS (7th Sept)": date(2026, 9, 7),
        "Weeks 1-6 UNIT 1 : Understanding Mental Health": None,
        "Year 10 catch up subject topics": None,
    }
    for name, want in cases.items():
        assert p(name, today) == want, name
