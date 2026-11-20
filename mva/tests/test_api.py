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
