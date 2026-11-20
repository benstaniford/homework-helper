"""
Example: print the student's upcoming/outstanding homework and the materials for
the most urgent piece. Run after setting up .env with CANVAS_TOKEN.

    .venv/Scripts/python.exe examples/show_homework.py
"""
from mva_canvas import MvaCanvas


def main():
    api = MvaCanvas.from_env()

    me = api.get_self()
    print(f"Logged in as: {me.name} (id {me.id})\n")

    print("Active courses:")
    for c in api.get_courses():
        print(f"  - {c.course_code:14} {c.name}")
    print()

    homework = api.get_upcoming_homework()
    if not homework:
        print("No outstanding or upcoming homework found. 🎉")
        return

    print(f"Outstanding / upcoming homework ({len(homework)}):")
    for hw in homework:
        due = hw.due_at.strftime("%a %d %b %Y") if hw.due_at else "no deadline"
        flag = " (OVERDUE)" if hw.is_overdue else ""
        print(f"  [{due}]{flag}  {hw.course_name}: {hw.title}")
    print()

    top = homework[0]
    print(f"Materials for the most urgent task — {top.title!r}:")
    for item in api.get_materials_for_homework(top):
        print(f"  [{item.type}] {item.title}")
        link = item.html_url or item.url
        if link:
            print(f"        {link}")


if __name__ == "__main__":
    main()
