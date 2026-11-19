"""Accuracy gate: the checkers must keep agreeing with the hand-labeled cases."""

from evaluation.cases import ALL_CASES, SHOP_SPEC, Case
from evaluation.run_eval import evaluate, main, render, run_case

C, V = "contradicted", "verified"


def test_checkers_meet_accuracy_thresholds():
    ev = evaluate()
    assert len(ev.cases) >= 15
    assert not ev.problems, ev.problems
    totals = ev.totals
    assert totals[C].precision >= 0.95 and totals[C].recall >= 0.95
    assert totals["uncertain"].precision >= 0.9
    assert totals[V].recall >= 0.95 and totals[V].precision == 1.0


def test_case_names_are_unique_and_labeled():
    names = [c.name for c in ALL_CASES]
    assert len(names) == len(set(names))
    assert sum(1 for c in ALL_CASES if c.expected) >= 15


def doc_case(expected):
    return Case("t", spec=SHOP_SPEC, docs={"a.md": "### GET /usres\n\nx\n"}, expected=expected)


def test_harness_reports_missed_and_unexpected_findings():
    missed = run_case(doc_case([("endpoint", "`/usres`", C), ("endpoint", "nothing like this", C)]))
    assert missed.tallies[C].fn == 1 and any("missed" in p for p in missed.problems)
    unexpected = run_case(doc_case([]))  # the typo is a contradiction nobody labeled
    assert unexpected.tallies[C].fp == 1 and any("unexpected" in p for p in unexpected.problems)


def test_harness_reports_wrong_verdicts():
    wrong = run_case(doc_case([("endpoint", "`/usres`", V)]))
    assert wrong.tallies[V].fn == 1 and wrong.tallies[C].fp == 1
    assert any("expected verified" in p and "got contradicted" in p for p in wrong.problems)


def test_render_and_cli(capsys):
    text = render(evaluate())
    assert "contradicted" in text and "precision" in text
    assert main([]) == 0
    assert "Every finding matches the labels." in capsys.readouterr().out
    assert main(["--json"]) == 0


def test_harness_flags_false_confirmations():
    case = Case(
        "t",
        spec=SHOP_SPEC,
        docs={"a.md": "### GET /users\n\nx\n"},
        expected=[("endpoint", "`GET /users` exists", V)],
        never_verified=[("endpoint", "`GET /users`")],  # but the checker rightly verifies it
    )
    result = run_case(case)
    assert result.tallies[V].fp == 1 and any("false confirmation" in p for p in result.problems)
