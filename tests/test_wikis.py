"""Tests for the required wikis: list — primary-wiki resolution (name +
domain), farm resolution, and the `canasta add` calls for the non-primary
wikis.

`wikis:` is required. The first entry is the primary: its id names the
instance. The rest are farm wikis (added via `canasta add`). Domains are not
declared in the manifest — they are supplied at deploy time via
`-n/--domain-name` (one per wiki, in order); `_resolve_wikis` maps them onto
the wikis and injects each wiki's `url`.
"""

import argparse

import pytest

from conftest import make_deploy, minimal_manifest
import wicker


def _deploy(tmp_path, *, domain_name="localhost", **sections):
    return make_deploy(tmp_path, minimal_manifest(**sections),
                       domain_name=domain_name)


class TestResolveWikis:
    def test_primary_supplies_name_and_domain(self, tmp_path):
        d = _deploy(tmp_path, wikis=[{"id": "www"}], domain_name="example.com")
        assert d.wikis == [{"id": "www", "url": "example.com"}]
        assert d.wiki == "www"
        assert d.domain == "example.com"

    def test_list_primary_is_first(self, tmp_path):
        d = _deploy(tmp_path, wikis=[{"id": "www"}, {"id": "docs"}],
                    domain_name="localhost,localhost/docs")
        assert d.wiki == "www"
        assert [w["id"] for w in d.wikis] == ["www", "docs"]

    def test_missing_wikis_raises(self, tmp_path):
        # The manifest is the only source of the wiki ids.
        m = minimal_manifest()
        del m["wikis"]
        with pytest.raises(wicker.BuildError, match="at least one wiki"):
            make_deploy(tmp_path, m)

    def test_domain_count_mismatch_raises(self, tmp_path):
        with pytest.raises(wicker.BuildError,
                           match="declares 2 wiki.*needs 2 comma-separated"):
            _deploy(tmp_path, wikis=[{"id": "www"}, {"id": "docs"}],
                    domain_name="localhost")

    def test_empty_domain_raises(self, tmp_path):
        with pytest.raises(wicker.BuildError, match="empty domain"):
            _deploy(tmp_path, wikis=[{"id": "www"}], domain_name="")

    def test_sync_code_needs_no_domain(self, tmp_path):
        # sync-code passes no -n; _resolve_wikis tolerates the missing value
        # (it never uses a wiki url), leaving the domain unset.
        args = argparse.Namespace(
            id="t", path=str(tmp_path), dry_run=True, skip_build=False,
            var=None, secret=None, orchestrator="compose", storage_class=None,
            access_mode=None, ingress_class=None, skip_tls=False,
            base_image=None)  # deliberately no domain_name
        d = wicker.Deploy(minimal_manifest(), tmp_path, args)
        assert d.wiki == "main"
        assert d.domain is None


class TestAddWikis:
    def test_single_wiki_adds_nothing(self, tmp_path, capsys):
        make_deploy(tmp_path).add_wikis()
        assert "canasta add" not in capsys.readouterr().out

    def test_farm_adds_the_non_primary_wikis(self, tmp_path, capsys):
        d = _deploy(tmp_path, wikis=[
            {"id": "www"},
            {"id": "docs", "siteName": "Docs"},
            {"id": "team"}],
            domain_name="localhost,localhost/docs,localhost/team")
        d.add_wikis()
        out = capsys.readouterr().out
        # primary is not re-added
        assert "-w www" not in out
        assert "canasta add -i t -w docs -u localhost/docs -t Docs" in out
        assert "canasta add -i t -w team -u localhost/team" in out
        # no siteName -> no -t for team
        assert "-w team -u localhost/team -t" not in out

    def test_create_uses_primary_name_and_domain(self, tmp_path, capsys):
        d = _deploy(tmp_path, wikis=[{"id": "www"}, {"id": "docs"}],
                    domain_name="example.com,example.com/docs")
        d.canasta_create(images={})
        out = capsys.readouterr().out
        assert "-w www" in out
        assert "-n example.com" in out

    def test_create_passes_primary_sitename(self, tmp_path, capsys):
        d = _deploy(tmp_path, wikis=[
            {"id": "main", "siteName": "Receipt Scanner"}])
        d.canasta_create(images={})
        assert "-t" in capsys.readouterr().out.split()

    def test_create_omits_sitename_when_absent(self, tmp_path, capsys):
        d = _deploy(tmp_path, wikis=[{"id": "main"}])
        d.canasta_create(images={})
        assert " -t " not in capsys.readouterr().out
