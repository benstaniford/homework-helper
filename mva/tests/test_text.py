"""Tests for plain-text extraction and the content getters."""
import responses

from mva_canvas.text import html_to_text, pdf_to_text

from conftest import API


def test_html_to_text_keeps_links_and_structure():
    html = """<h2>Task</h2><p>Read the notes&nbsp;then <a href="https://bbc.example/x">watch this</a>.</p>
    <ul><li>Step one</li><li>Step two</li></ul><script>evil()</script>
    <iframe src="https://youtube.example/embed/1" title="Osmosis video"></iframe>
    <p><a href="#top">top</a> <img alt="diagram"></p>"""
    text = html_to_text(html)
    assert text.startswith("Task\n")
    assert "Read the notes then watch this (https://bbc.example/x)." in text
    assert "- Step one\n" in text and "- Step two" in text
    assert "evil" not in text
    assert "[embedded: Osmosis video (https://youtube.example/embed/1)]" in text
    assert "top [image: diagram]" in text


def test_html_to_text_empty():
    assert html_to_text(None) == "" and html_to_text("") == ""


def test_pdf_to_text_bad_input():
    assert pdf_to_text(b"not a pdf") == ""


def test_content_getters(api, mock_rsps):
    mock_rsps.add(responses.GET, f"{API}/courses/3517/pages/osmosis-notes",
                  json={"title": "Osmosis notes", "body": "<p>Water</p>"})
    mock_rsps.add(responses.GET, f"{API}/courses/3517/assignments/400001",
                  json={"id": 400001, "course_id": 3517, "name": "SUBMIT", "description": "<p>Do it</p>"})
    mock_rsps.add(responses.GET, f"{API}/courses/3539/discussion_topics/7",
                  json={"title": "Share", "message": "<p>Post</p>"})
    mock_rsps.add(responses.GET, f"{API}/files/555",
                  json={"display_name": "a.pdf", "url": "https://canvas.test/files/555/download"})
    mock_rsps.add(responses.GET, "https://canvas.test/files/555/download", body=b"x" * 100)
    assert api.get_page(3517, "osmosis-notes")["body"] == "<p>Water</p>"
    assert api.get_assignment(3517, 400001).description_html == "<p>Do it</p>"
    assert api.get_discussion(3539, 7)["message"] == "<p>Post</p>"
    meta = api.get_file(555)
    assert api.download(meta["url"], max_bytes=10) == b"x" * 10
