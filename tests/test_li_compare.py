import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "li_compare", Path(__file__).resolve().parents[1] / "scripts" / "li_compare.py")
lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lc)


def _card(i: str, title: str, co: str) -> str:
    return (f'<li><div class="base-card" data-entity-urn="urn:li:jobPosting:{i}">'
            f'<h3 class="base-search-card__title">\n  {title}\n</h3>'
            f'<h4 class="base-search-card__subtitle"><a href="#">{co}</a></h4>'
            f'<span class="job-search-card__location"> Seoul </span></div></li>')


def test_parse_guest_cards_in_order():
    page = _card("111111111", "AI Engineer &amp; Researcher", "Acme") + _card("222222222", "ML Ops", "Beta")
    cards = lc.parse_guest_cards(page)
    assert [c["id"] for c in cards] == ["111111111", "222222222"]
    assert cards[0] == {"id": "111111111", "title": "AI Engineer & Researcher", "company": "Acme", "location": "Seoul"}


def test_parse_login_snippet_tsv_and_raw_urls():
    tsv = "333333333\tpromoted\tAI Engineer\tAcme\n444444444\t\tML Ops\tBeta\n333333333\t\tdup\t"
    rows = lc.parse_login(tsv)
    assert [r["id"] for r in rows] == ["333333333", "444444444"]
    assert rows[0]["promoted"] and rows[0]["title"] == "AI Engineer" and not rows[1]["promoted"]
    raw = ("https://www.linkedin.com/jobs/view/ai-engineer-at-acme-555555555/?x=1\n"
           "https://www.linkedin.com/jobs/search/?currentJobId=666666666&keywords=ai")
    assert [r["id"] for r in lc.parse_login(raw)] == ["555555555", "666666666"]


def test_report_counts_overlap_and_promoted():
    guest = [{"id": i, "title": f"g{i}", "company": ""} for i in ("1", "2", "3", "4")]
    login = [{"id": "1"}, {"id": "2"}, {"id": "9", "promoted": True}, {"id": "8"}]
    out = lc.report(guest, login)
    assert "both 2" in out
    assert "login results also in guest: 2/4 = 50%" in out
    assert "excluding promoted:        2/3 = 67%" in out
    assert "9  ?  [promoted]" in out and "3  g3" in out


def test_params_maps_filters():
    assert lc.params(" AI ", "Seoul", "week", "remote") == {
        "keywords": "AI", "location": "Seoul", "f_TPR": "r604800", "f_WT": "2"}
