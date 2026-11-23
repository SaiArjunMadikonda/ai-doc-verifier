"""Docs vs Dockerfile / compose / Helm / Terraform."""

import pytest

from infra_checks import compose_service_args
from project_checks import CONTRADICTED, UNCERTAIN, VERIFIED, verify_project


@pytest.fixture
def project(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 8080 9090/tcp\n")
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  web:\n    build: .\n    ports:\n      - '8080:8080'\n"
        "  worker:\n    image: shop/worker\n  redis:\n    ports: ['6379:6379']\n"
    )
    chart = tmp_path / "deploy" / "chart"
    chart.mkdir(parents=True)
    (chart / "values.yaml").write_text(
        "image:\n  tag: latest\nreplicaCount: 2\ningress:\n  enabled: false\n"
    )
    (chart / "Chart.yaml").write_text("name: shop\ndependencies:\n  - name: postgresql\n")
    (tmp_path / "main.tf").write_text('variable "region" {}\nvariable "instance_type" {}\n')
    docs = tmp_path / "docs"
    docs.mkdir()
    return tmp_path, docs


def run(project, text):
    root, docs = project
    (docs / "d.md").write_text(text)
    report = verify_project(root, [docs], checks=("infra",))
    return {(f.subject, f.verdict) for f in report.findings}, report


def test_ports(project):
    got, _ = run(
        project,
        "The container listens on port 8080 and metrics use port 9100.\n"
        "Open http://localhost:8080 or http://localhost:3000.\n"
        "A phone has port 22 open.\n",  # no infrastructure context: ignored
    )
    assert ("port 8080", VERIFIED) in got
    assert ("port 9100", CONTRADICTED) in got
    assert ("port 3000", UNCERTAIN) in got
    assert not any(s == "port 22" for s, _ in got)


def test_expose_and_publish_flags(project):
    got, _ = run(project, "```\nEXPOSE 9090\ndocker run -p 80:8081 shop\n```\n")
    assert ("port 9090", VERIFIED) in got and ("port 8081", CONTRADICTED) in got


def test_compose_services(project):
    got, report = run(
        project,
        "```bash\ndocker compose up web worker cache\n"
        "docker-compose logs -f -t 20 redis\n"
        "docker compose -f x.yml exec wrker sh\n```\n"
        "Start the `web` service and the `queue` service.\n",
    )
    assert ("service `web`", VERIFIED) in got and ("service `worker`", VERIFIED) in got
    assert ("service `cache`", CONTRADICTED) in got and ("service `queue`", CONTRADICTED) in got
    assert ("service `redis`", VERIFIED) in got
    assert not any("`20`" in s for s, _ in got)
    typo = next(f for f in report.findings if f.subject == "service `wrker`")
    assert typo.fix == "Use `worker`."


def test_compose_argument_parsing():
    assert compose_service_args("up -d --build web worker") == ["web", "worker"]
    assert compose_service_args("exec -T web ls -la") == ["web"]
    assert compose_service_args("-f a.yml --profile p run --rm worker bash") == ["worker"]
    assert compose_service_args("down -v") == []
    assert compose_service_args("up web && echo done") == ["web"]
    assert compose_service_args("up ./path") == []
    assert compose_service_args("explode web") == []


def test_helm_values(project):
    got, report = run(
        project,
        "```\nhelm install shop ./chart --set image.tag=1.2 --set image.tagg=3,replicaCount=3 "
        "--set postgresql.auth.password=x --set 'ingress.hosts[0].host=a.com'\n```\n"
        "Edit `replicaCount` in values.yaml and see `config.yaml` for more.\n",
    )
    assert ("value `image.tag`", VERIFIED) in got
    assert ("value `image.tagg`", CONTRADICTED) in got
    assert ("value `replicaCount`", VERIFIED) in got
    assert ("value `postgresql.auth.password`", UNCERTAIN) in got  # lives in a sub-chart
    assert ("value `ingress.hosts.host`", CONTRADICTED) in got
    assert not any("config.yaml" in s for s, _ in got)
    assert (
        next(f for f in report.findings if f.subject == "value `image.tagg`").fix
        == "Use `image.tag`."
    )


def test_terraform_variables(project):
    got, _ = run(
        project,
        "```\nterraform apply -var region=us-east-1 -var 'instance_size=s'\n"
        "export TF_VAR_zone=a\n```\nUses var.instance_type.\n",
    )
    assert ("variable `region`", VERIFIED) in got
    assert ("variable `instance_size`", CONTRADICTED) in got
    assert ("variable `zone`", CONTRADICTED) in got
    assert ("variable `instance_type`", VERIFIED) in got


def test_nothing_is_said_when_no_matching_file_exists(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "d.md").write_text(
        "Listens on port 8080 in the container. Run `docker compose up web`. -var region=x\n"
        "helm --set a.b=1\n"
    )
    report = verify_project(tmp_path, [tmp_path / "docs"], checks=("infra",))
    assert report.findings == []


def test_broken_yaml_is_skipped(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: [unclosed\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "d.md").write_text("Run docker compose up web in the container.\n")
    assert verify_project(tmp_path, [tmp_path / "docs"], checks=("infra",)).findings == []
