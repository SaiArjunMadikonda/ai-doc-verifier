"""The bundled demo project must keep producing the documented findings."""

from pathlib import Path

import docaudit
from api_contract import CONTRADICTED

DEMO = Path(__file__).resolve().parent.parent / "examples" / "shop-demo"


def test_demo_planted_mistakes_are_all_found():
    result = docaudit.audit(DEMO, [DEMO / "docs"], code_path=str(DEMO))
    bad = [f.message for f in result.findings if f.verdict == CONTRADICTED]
    assert len(bad) == 19
    expected = [
        "Parameter `offset`",
        "Parameter `email`: docs say optional",
        "Parameter `age`: docs say type `string`",
        "Response `409`",
        "Docs say `PUT`",
        "Response `400`",
        "path `/invoices`",
        "body field `nickname`",
        "required body field `qty`",
        "`SHOP_DATABASE`",
        "`SHOP_API_TOKEN`",
        "CLI flag `--format`",
        "`build_orders()`",
        "`deploy.md`",
        "Python 3.9+",
        "port 9000",
        "compose service `queue`",
        "Helm value `image.tags`",
        "Terraform variable `instance_size`",
    ]
    for needle in expected:
        assert any(needle in m for m in bad), needle


def test_demo_expected_output_is_current():
    result = docaudit.audit(DEMO, [DEMO / "docs"], code_path=str(DEMO))
    committed = (DEMO / "EXPECTED_OUTPUT.md").read_text(encoding="utf-8")
    assert committed == docaudit.render_audit_markdown(result) + "\n"
