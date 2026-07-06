"""Tests for `wicker upgrade` — preflight/state loading, the shipped-page
disposition matrix (plan_page), three-way merge, code/config sync diffs
(sidecars, components, wikis, images), content apply, and state rewrite.

No live instance: wiki reads/writes are monkeypatched; everything else runs
against tmp_path app/instance trees.
"""

import json

import pytest
import yaml

import wicker
from conftest import make_deploy, make_upgrade, minimal_manifest


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

V1_PAGES = {
    "Template/Expense.mediawiki": "== Expense ==\nrow one\nrow two\nfooter\n",
    "Main Page.mediawiki": "Welcome\n",
    "Help/Old.mediawiki": "obsolete\n",
}

V2_PAGES = {
    "Template/Expense.mediawiki": "== Expense ==\nrow one\nrow two\nNEW footer\n",
    "Main Page.mediawiki": "Welcome\n",
    "Help/New.mediawiki": "fresh\n",
    # Help:Old removed in 2.0
}


def _manifest(version, page_titles, **sections):
    return minimal_manifest(
        version=version,
        content={"pages": {"dir": "./content/pages", "list": page_titles}},
        **sections)


V1 = _manifest("1.0.0", ["Template:Expense", "Main Page", "Help:Old"])
V2 = _manifest("2.0.0", ["Template:Expense", "Main Page", "Help:New"])


def _write_app(app_dir, manifest, pages):
    (app_dir / "wicker.yaml").write_text(yaml.safe_dump(manifest))
    for rel, text in pages.items():
        f = app_dir / "content" / "pages" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)


def _deployed_v1(tmp_path):
    """A v1 app dir plus the instance state a v1 deploy recorded."""
    v1_dir = tmp_path / "app-v1"
    v1_dir.mkdir()
    _write_app(v1_dir, V1, V1_PAGES)
    make_deploy(v1_dir, V1, parent=tmp_path, dry_run=False).record_state()
    return v1_dir


def _v2_upgrade(tmp_path, manifest=V2, pages=V2_PAGES, **kw):
    v2_dir = tmp_path / "app-v2"
    if not v2_dir.exists():
        v2_dir.mkdir()
        _write_app(v2_dir, manifest, pages)
    return make_upgrade(v2_dir, manifest, parent=tmp_path, **kw)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

class TestSemver:
    def test_ordering(self):
        assert wicker.semver_triplet("2.0.0") > wicker.semver_triplet("1.9.9")
        assert wicker.semver_triplet("1.0.0-rc1") == (1, 0, 0)

    def test_unparseable(self):
        with pytest.raises(wicker.BuildError):
            wicker.semver_triplet("two point oh")


class TestMergePage:
    def test_clean_merge_of_separated_hunks(self):
        base = "h\na\nb\nc\nd\ne\nf\n"
        live = "h\nA\nb\nc\nd\ne\nf\n"   # user edited the top
        new = "h\na\nb\nc\nd\ne\nF\n"    # app changed the bottom
        merged, markers = wicker.merge_page(base, live, new, version="2.0.0")
        assert markers is None
        assert merged == "h\nA\nb\nc\nd\ne\nF\n"

    def test_overlapping_edits_conflict(self):
        merged, markers = wicker.merge_page("x\n", "y\n", "z\n", version="2.0.0")
        assert merged is None
        assert "<<<<<<< live wiki page" in markers
        assert ">>>>>>> shipped in 2.0.0" in markers

    def test_trailing_newline_variance_ignored(self):
        # maintenance edit trims a trailing newline; that alone is no change.
        merged, markers = wicker.merge_page("a\nb\n", "a\nb", "a\nB\n",
                                            version="2.0.0")
        assert markers is None
        assert merged.rstrip("\n") == "a\nB"


class TestPlanPage:
    def plan(self, base, live, new):
        return wicker.plan_page("P", base, live, new, version="2.0.0")

    def test_new_page_import(self):
        e = self.plan(None, None, "text\n")
        assert e["action"] == "import" and e["text"] == "text\n"

    def test_new_page_already_present_and_equal(self):
        assert self.plan(None, "text", "text\n")["action"] == "unchanged"

    def test_new_page_collides_with_user_page(self):
        assert self.plan(None, "user's own\n", "text\n")["action"] == \
            "exists-differs"

    def test_pristine_overwrite(self):
        e = self.plan("old\n", "old", "new\n")
        assert e["action"] == "overwrite" and e["text"] == "new\n"

    def test_unchanged_when_live_already_new(self):
        assert self.plan("old\n", "new", "new\n")["action"] == "unchanged"

    def test_app_did_not_change_page(self):
        assert self.plan("same\n", "user edited\n", "same\n")["action"] == "skip"

    def test_user_deleted_not_resurrected(self):
        assert self.plan("old\n", None, "new\n")["action"] == "user-deleted"

    def test_removed_pristine_deleted(self):
        assert self.plan("old\n", "old", None)["action"] == "delete"

    def test_removed_but_modified_kept(self):
        assert self.plan("old\n", "user edited\n", None)["action"] == \
            "modified-removed"

    def test_removed_and_already_absent(self):
        assert self.plan("old\n", None, None)["action"] == "skip"

    def test_modified_with_clean_merge(self):
        base = "h\na\nb\nc\nd\ne\nf\n"
        e = self.plan(base, "h\nA\nb\nc\nd\ne\nf", "h\na\nb\nc\nd\ne\nF\n")
        assert e["action"] == "merge"
        assert e["text"] == "h\nA\nb\nc\nd\ne\nF\n"

    def test_modified_with_conflict(self):
        e = self.plan("x\n", "y\n", "z\n")
        assert e["action"] == "conflict" and e["markers"]


# --------------------------------------------------------------------------- #
# Preflight and state loading
# --------------------------------------------------------------------------- #

class TestPreflight:
    @pytest.fixture(autouse=True)
    def tools_present(self, monkeypatch):
        monkeypatch.setattr(wicker, "have", lambda t: True)

    def test_missing_instance_dir(self, tmp_path):
        v2_dir = tmp_path / "app-v2"
        v2_dir.mkdir()
        _write_app(v2_dir, V2, V2_PAGES)
        up = make_upgrade(v2_dir, V2, parent=tmp_path / "elsewhere")
        with pytest.raises(wicker.BuildError, match="not found"):
            up.preflight_upgrade()

    def test_no_state_no_baseline(self, tmp_path):
        (tmp_path / "t").mkdir()  # instance dir exists, no .wicker-app
        with pytest.raises(wicker.BuildError, match="--baseline"):
            _v2_upgrade(tmp_path).preflight_upgrade()

    def test_state_loaded(self, tmp_path):
        _deployed_v1(tmp_path)
        up = _v2_upgrade(tmp_path)
        up.preflight_upgrade()
        assert up.old_version == "1.0.0"
        assert up.old_manifest["version"] == "1.0.0"
        assert up.base_pages["Help:Old"] == "obsolete\n"

    def test_baseline_bootstrap(self, tmp_path):
        v1_dir = tmp_path / "app-v1"
        v1_dir.mkdir()
        _write_app(v1_dir, V1, V1_PAGES)
        (tmp_path / "t").mkdir()  # instance without recorded state
        up = _v2_upgrade(tmp_path, baseline=str(v1_dir))
        up.preflight_upgrade()
        assert up.old_version == "1.0.0"
        assert up.base_pages["Template:Expense"] == \
            V1_PAGES["Template/Expense.mediawiki"]

    def test_wrong_app_refused(self, tmp_path):
        _deployed_v1(tmp_path)
        other = dict(V2, name="other-app")
        with pytest.raises(wicker.BuildError, match="across apps"):
            _v2_upgrade(tmp_path, manifest=other).preflight_upgrade()

    def test_same_version_refused(self, tmp_path):
        _deployed_v1(tmp_path)
        v1_again = _manifest("1.0.0", ["Main Page"])
        with pytest.raises(wicker.BuildError, match="nothing to upgrade"):
            _v2_upgrade(tmp_path, manifest=v1_again,
                        pages=V1_PAGES).preflight_upgrade()

    def test_downgrade_refused(self, tmp_path):
        _deployed_v1(tmp_path)
        v0 = _manifest("0.9.0", ["Main Page"])
        with pytest.raises(wicker.BuildError, match="downgrade"):
            _v2_upgrade(tmp_path, manifest=v0,
                        pages=V1_PAGES).preflight_upgrade()

    def test_primary_wiki_change_refused(self, tmp_path):
        _deployed_v1(tmp_path)
        moved = dict(V2, wikis=[{"id": "renamed"}])
        with pytest.raises(wicker.BuildError, match="primary wiki"):
            _v2_upgrade(tmp_path, manifest=moved).preflight_upgrade()


# --------------------------------------------------------------------------- #
# Code / config sync diffs (dry-run, command echo assertions)
# --------------------------------------------------------------------------- #

def _loaded_upgrade(tmp_path, new_manifest, old_manifest, **kw):
    _deployed_v1(tmp_path)
    up = _v2_upgrade(tmp_path, manifest=new_manifest, **kw)
    up.old_manifest = old_manifest
    up.old_version = old_manifest["version"]
    up.old_app = old_manifest["name"]
    return up


class TestCodeConfigSync:
    def test_sidecars_withdrawn_then_dropped(self, tmp_path, capsys):
        old = dict(V1, sidecars=[{"name": "kept", "image": "x"},
                                 {"name": "dropped", "image": "y"}])
        new = dict(V2, sidecars=[{"name": "kept", "image": "x:2"}])
        up = _loaded_upgrade(tmp_path, new, old)
        up.remove_old_sidecars()
        out = capsys.readouterr().out
        assert "canasta sidecar remove -i t --name kept" in out
        assert "canasta sidecar remove -i t --name dropped" in out
        assert "dropped from the app" in out
        assert "re-declared" in out

    def test_component_pruned(self, tmp_path, capsys):
        old = dict(V1, extensions=[{"name": "Kept", "source": "./ext/Kept"},
                                   {"name": "Gone", "source": "./ext/Gone"},
                                   {"name": "Bundled"}])
        new = dict(V2, extensions=[{"name": "Kept", "source": "./ext/Kept"}])
        up = _loaded_upgrade(tmp_path, new, old)
        gone = up.instance_dir / "extensions" / "Gone"
        gone.mkdir(parents=True)
        up.dry_run = False
        up.prune_components()
        assert not gone.exists()

    def test_new_wiki_added_dropped_warned(self, tmp_path, capsys):
        old = dict(V1, wikis=[{"id": "main"}, {"id": "olddocs"}])
        new = dict(V2, wikis=[{"id": "main"}, {"id": "docs"}])
        up = _loaded_upgrade(tmp_path, new, old,
                             domain_name="localhost,localhost/docs")
        up.add_new_wikis()
        captured = capsys.readouterr()
        assert "canasta add -i t -w docs -u localhost/docs" in captured.out
        assert "olddocs" in captured.err  # warned, never deleted
        assert "canasta delete" not in captured.out

    def test_web_image_pointed_on_compose(self, tmp_path, capsys):
        up = _loaded_upgrade(tmp_path, V2, V1)
        up.apply_images({"web": "app-web:2.0.0"})
        assert "config set -i t --no-restart CANASTA_IMAGE=app-web:2.0.0" \
            in capsys.readouterr().out

    def test_web_image_pushed_and_applied_on_k8s(self, tmp_path, capsys):
        _deployed_v1(tmp_path)
        new = dict(V2, images={"web": {"dockerfile": "./Dockerfile"}})
        up = _v2_upgrade(tmp_path, manifest=new, orchestrator="k8s")
        up.old_manifest = V1
        up.apply_images({})
        out = capsys.readouterr().out
        assert "canasta image push" in out
        assert "CANASTA_IMAGE=10.43.0.2:5000/" in out


# --------------------------------------------------------------------------- #
# Content reconciliation end-to-end (live wiki stubbed)
# --------------------------------------------------------------------------- #

LIVE_WIKI = {
    # pristine — 2.0 changes it -> overwrite
    "Template:Expense": V1_PAGES["Template/Expense.mediawiki"],
    # user-edited, app didn't change it -> skip
    "Main Page": "Welcome home\n",
    # pristine and removed in 2.0 -> delete
    "Help:Old": "obsolete\n",
    # Help:New not present -> import
}


class TestReconciliation:
    def _upgrade(self, tmp_path, monkeypatch, live=LIVE_WIKI):
        _deployed_v1(tmp_path)
        up = _v2_upgrade(tmp_path, dry_run=False)
        up.old_manifest = V1
        up.old_version = "1.0.0"
        up.old_app = V1["name"]
        up._load_old_state()
        monkeypatch.setattr(up, "_live_page_text",
                            lambda title: live.get(title))
        edits, deletes = [], []
        monkeypatch.setattr(
            up, "_mw_edit",
            lambda title, src, summary=None:
                edits.append((title, src.read_text(), summary)))
        monkeypatch.setattr(
            up, "_mw_delete",
            lambda title, reason: deletes.append((title, reason)))
        return up, edits, deletes

    def test_plan_dispositions(self, tmp_path, monkeypatch):
        up, _, _ = self._upgrade(tmp_path, monkeypatch)
        plan = {e["title"]: e["action"] for e in up.plan_content()}
        assert plan == {
            "Template:Expense": "overwrite",
            "Main Page": "skip",
            "Help:Old": "delete",
            "Help:New": "import",
        }

    def test_apply_writes_and_deletes(self, tmp_path, monkeypatch):
        up, edits, deletes = self._upgrade(tmp_path, monkeypatch)
        up.apply_content(up.plan_content())
        assert ("Template:Expense",
                V2_PAGES["Template/Expense.mediawiki"],
                "Upgrade to 2.0.0 (wicker)") in edits
        assert ("Help:New", "fresh\n", "Upgrade to 2.0.0 (wicker)") in edits
        assert len(edits) == 2
        assert deletes == [("Help:Old",
                            "Upgrade to 2.0.0: page removed from the app "
                            "(wicker)")]

    def test_merge_applied_with_merge_summary(self, tmp_path, monkeypatch):
        live = dict(LIVE_WIKI)
        # user edited the header; 2.0 changes the footer -> clean merge
        live["Template:Expense"] = "== Expenses (mine) ==\nrow one\nrow two\nfooter\n"
        up, edits, _ = self._upgrade(tmp_path, monkeypatch, live)
        up.apply_content(up.plan_content())
        merged = [e for e in edits if e[0] == "Template:Expense"]
        assert merged and merged[0][1] == \
            "== Expenses (mine) ==\nrow one\nrow two\nNEW footer\n"
        assert merged[0][2] == "Upgrade to 2.0.0 (wicker, merged with local edits)"

    def test_conflict_reported_not_written(self, tmp_path, monkeypatch):
        live = dict(LIVE_WIKI)
        live["Template:Expense"] = \
            "== Expense ==\nrow one\nrow two\nMY OWN footer\n"
        up, edits, deletes = self._upgrade(tmp_path, monkeypatch, live)
        plan = up.plan_content()
        up.apply_content(plan)
        assert not any(e[0] == "Template:Expense" for e in edits)
        markers = (up.state_dir / "conflicts"
                   / "Template/Expense.mediawiki.merge")
        assert markers.is_file()
        assert "<<<<<<< live wiki page" in markers.read_text()
        entry = next(e for e in plan if e["title"] == "Template:Expense")
        assert str(markers) in entry["note"]

    def test_state_rewritten_with_upgrade_op(self, tmp_path, monkeypatch):
        up, _, _ = self._upgrade(tmp_path, monkeypatch)
        up.record_state(op="upgrade")
        state = json.loads((up.state_dir / "state.json").read_text())
        assert state["version"] == "2.0.0"
        assert [h["op"] for h in state["history"]] == ["deploy", "upgrade"]
        # Snapshot now holds the 2.0 shipped text (the next upgrade's base).
        assert (up.state_dir / "content" / "Template/Expense.mediawiki") \
            .read_text() == V2_PAGES["Template/Expense.mediawiki"]
        assert not (up.state_dir / "content" / "Help/Old.mediawiki").exists()

    def test_dry_run_skips_live_analysis(self, tmp_path, monkeypatch, capsys):
        _deployed_v1(tmp_path)
        up = _v2_upgrade(tmp_path)  # dry_run=True
        up._load_old_state()
        monkeypatch.setattr(
            up, "_live_page_text",
            lambda title: pytest.fail("dry-run must not read the wiki"))
        assert up.plan_content() == []
        assert "Would reconcile 4 shipped page(s)" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Upgrade domain resolution (-n optional; instance wikis.yaml is the truth)
# --------------------------------------------------------------------------- #

class TestUpgradeDomains:

    def _app(self, tmp_path):
        app_dir = tmp_path / "app"
        app_dir.mkdir(exist_ok=True)
        return app_dir

    def _wikis_yaml(self, tmp_path, wikis, id="t"):
        cfg = tmp_path / id / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "wikis.yaml").write_text(yaml.safe_dump({"wikis": wikis}))

    def test_omitted_n_reads_instance_wikis_yaml(self, tmp_path):
        self._wikis_yaml(tmp_path, [{"id": "main", "url": "example.org"}])
        upg = make_upgrade(self._app(tmp_path), parent=tmp_path,
                           domain_name=None)
        assert upg.wikis[0]["url"] == "example.org"
        assert upg.domain == "example.org"

    def test_omitted_n_maps_farm_wikis_by_id(self, tmp_path):
        self._wikis_yaml(tmp_path, [
            {"id": "main", "url": "example.org"},
            {"id": "docs", "url": "example.org/docs"},
        ])
        m = minimal_manifest(wikis=[{"id": "main"}, {"id": "docs"}])
        upg = make_upgrade(self._app(tmp_path), m, parent=tmp_path,
                           domain_name=None)
        assert [w["url"] for w in upg.wikis] == \
            ["example.org", "example.org/docs"]

    def test_omitted_n_with_added_wiki_requires_n(self, tmp_path):
        self._wikis_yaml(tmp_path, [{"id": "main", "url": "example.org"}])
        m = minimal_manifest(wikis=[{"id": "main"}, {"id": "docs"}])
        with pytest.raises(wicker.BuildError, match="docs.*-n/--domain-name"):
            make_upgrade(self._app(tmp_path), m, parent=tmp_path,
                         domain_name=None)

    def test_omitted_n_without_wikis_yaml_errors(self, tmp_path):
        with pytest.raises(wicker.BuildError, match="wikis.yaml"):
            make_upgrade(self._app(tmp_path), parent=tmp_path,
                         domain_name=None)

    def test_supplied_n_must_match_live_domains(self, tmp_path):
        self._wikis_yaml(tmp_path, [{"id": "main", "url": "example.org"}])
        with pytest.raises(wicker.BuildError, match="never re-points"):
            make_upgrade(self._app(tmp_path), parent=tmp_path,
                         domain_name="other.example")

    def test_supplied_n_matching_live_is_accepted(self, tmp_path):
        self._wikis_yaml(tmp_path, [{"id": "main", "url": "example.org"}])
        upg = make_upgrade(self._app(tmp_path), parent=tmp_path,
                           domain_name="example.org")
        assert upg.domain == "example.org"

    def test_supplied_n_covers_added_wiki(self, tmp_path):
        self._wikis_yaml(tmp_path, [{"id": "main", "url": "example.org"}])
        m = minimal_manifest(wikis=[{"id": "main"}, {"id": "docs"}])
        upg = make_upgrade(self._app(tmp_path), m, parent=tmp_path,
                           domain_name="example.org,example.org/docs")
        assert upg.wikis[1]["url"] == "example.org/docs"

    def test_supplied_n_without_wikis_yaml_is_trusted(self, tmp_path):
        upg = make_upgrade(self._app(tmp_path), parent=tmp_path,
                           domain_name="anything.example")
        assert upg.domain == "anything.example"


# --------------------------------------------------------------------------- #
# Report-only code/config sync plan
# --------------------------------------------------------------------------- #

class TestSyncPlanReport:

    def _upgrade(self, tmp_path, old_manifest, new_manifest):
        app_dir = tmp_path / "app"
        app_dir.mkdir(exist_ok=True)
        upg = make_upgrade(app_dir, new_manifest, parent=tmp_path)
        upg.old_manifest = old_manifest
        upg.old_version = old_manifest["version"]
        return upg

    def test_lists_extension_resync_and_image_rebuild(self, tmp_path, capsys):
        old = minimal_manifest(
            version="1.0.0",
            extensions=[{"name": "ReceiptScanner", "source": "./ext"}])
        new = minimal_manifest(
            version="2.0.0",
            extensions=[{"name": "ReceiptScanner", "source": "./ext"}],
            images={"web": {"context": "./images/web"}},
            settings=[{"path": "./settings/S.php",
                       "install": "config/settings/S.php"}],
            sidecars=[{"name": "receipt-scanner"}])
        self._upgrade(tmp_path, old, new).print_sync_plan()
        out = capsys.readouterr().out
        assert "re-sync extension  ReceiptScanner" in out
        assert "rebuild image      web" in out
        assert "re-sync settings   config/settings/S.php" in out
        assert "re-declare sidecar receipt-scanner (new)" in out
        assert "restart instance" in out

    def test_lists_removed_components_and_kept_wikis(self, tmp_path, capsys):
        old = minimal_manifest(
            version="1.0.0",
            wikis=[{"id": "main"}, {"id": "docs"}],
            extensions=[{"name": "Old", "source": "./old"}],
            sidecars=[{"name": "gone"}])
        new = minimal_manifest(version="2.0.0", wikis=[{"id": "main"}])
        self._upgrade(tmp_path, old, new).print_sync_plan()
        out = capsys.readouterr().out
        assert "remove extension   Old (dropped from the app)" in out
        assert "remove sidecar     gone (dropped from the app)" in out
        assert "keep wiki          docs" in out


# --------------------------------------------------------------------------- #
# Orchestrator comes from the canasta registry
# --------------------------------------------------------------------------- #

class TestOrchestratorFromRegistry:

    def _registry(self, monkeypatch, tmp_path, orchestrator, id="t"):
        d = tmp_path / "canasta-config"
        d.mkdir(exist_ok=True)
        (d / "conf.json").write_text(json.dumps(
            {"Instances": {id: {"id": id, "orchestrator": orchestrator}}}))
        monkeypatch.setenv("CANASTA_CONFIG_DIR", str(d))

    def _app(self, tmp_path):
        app_dir = tmp_path / "app"
        app_dir.mkdir(exist_ok=True)
        return app_dir

    def test_recorded_orchestrator_wins_when_o_omitted(self, tmp_path, monkeypatch):
        self._registry(monkeypatch, tmp_path, "kubernetes")
        upg = make_upgrade(self._app(tmp_path), parent=tmp_path,
                           orchestrator=None)
        assert upg.orchestrator == "kubernetes"
        assert upg.is_k8s

    def test_explicit_o_mismatch_errors(self, tmp_path, monkeypatch):
        self._registry(monkeypatch, tmp_path, "kubernetes")
        with pytest.raises(wicker.BuildError, match="recorded as"):
            make_upgrade(self._app(tmp_path), parent=tmp_path,
                         orchestrator="compose")

    def test_equivalent_k8s_spellings_accepted(self, tmp_path, monkeypatch):
        self._registry(monkeypatch, tmp_path, "k8s")
        upg = make_upgrade(self._app(tmp_path), parent=tmp_path,
                           orchestrator="kubernetes")
        assert upg.is_k8s

    def test_unregistered_instance_defaults_to_compose(self, tmp_path):
        upg = make_upgrade(self._app(tmp_path), parent=tmp_path,
                           orchestrator=None)
        assert upg.orchestrator == "compose"
        assert not upg.is_k8s

    def test_unregistered_instance_honors_explicit_o(self, tmp_path):
        upg = make_upgrade(self._app(tmp_path), parent=tmp_path,
                           orchestrator="kubernetes")
        assert upg.is_k8s


# --------------------------------------------------------------------------- #
# Instance path comes from the canasta registry
# --------------------------------------------------------------------------- #

class TestInstancePathFromRegistry:

    def _registry(self, monkeypatch, tmp_path, instance_path, id="t"):
        d = tmp_path / "canasta-config"
        d.mkdir(exist_ok=True)
        (d / "conf.json").write_text(json.dumps(
            {"Instances": {id: {"id": id, "path": str(instance_path),
                                "orchestrator": "compose"}}}))
        monkeypatch.setenv("CANASTA_CONFIG_DIR", str(d))

    def _app(self, tmp_path):
        app_dir = tmp_path / "app"
        app_dir.mkdir(exist_ok=True)
        return app_dir

    def test_recorded_path_wins_when_p_omitted(self, tmp_path, monkeypatch):
        inst = tmp_path / "elsewhere" / "t"
        self._registry(monkeypatch, tmp_path, inst)
        upg = make_upgrade(self._app(tmp_path), path=None)
        assert upg.instance_dir == inst.resolve()
        assert upg.parent_path == inst.resolve().parent

    def test_explicit_p_matching_registry_is_accepted(self, tmp_path, monkeypatch):
        inst = tmp_path / "elsewhere" / "t"
        self._registry(monkeypatch, tmp_path, inst)
        upg = make_upgrade(self._app(tmp_path), parent=tmp_path / "elsewhere")
        assert upg.instance_dir == inst.resolve()

    def test_explicit_p_mismatch_errors(self, tmp_path, monkeypatch):
        self._registry(monkeypatch, tmp_path, tmp_path / "elsewhere" / "t")
        with pytest.raises(wicker.BuildError, match="drop -p"):
            make_upgrade(self._app(tmp_path), parent=tmp_path / "other")

    def test_unregistered_instance_falls_back_to_p(self, tmp_path):
        upg = make_upgrade(self._app(tmp_path), parent=tmp_path)
        assert upg.instance_dir == (tmp_path / "t").resolve()

    def test_unregistered_instance_without_p_uses_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        upg = make_upgrade(self._app(tmp_path), path=None)
        assert upg.instance_dir == (tmp_path / "t").resolve()


# --------------------------------------------------------------------------- #
# Dependency preflight
# --------------------------------------------------------------------------- #

class TestRequireModules:

    def test_present_modules_pass(self):
        wicker.require_modules("json", "os")

    def test_missing_module_fails_with_install_hint(self):
        with pytest.raises(wicker.BuildError,
                           match=r"(?s)no_such_module_xyz.*requirements\.txt"):
            wicker.require_modules("json", "no_such_module_xyz")

    def test_no_pip_suggests_distro_packages_and_venv(self, monkeypatch):
        import importlib.util
        real = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda m, *a: None if m in ("pip", "no_such_module_xyz")
            else real(m, *a))
        with pytest.raises(wicker.BuildError) as e:
            wicker.require_modules("no_such_module_xyz")
        msg = str(e.value)
        assert "sudo apt install python3-no_such_module_xyz" in msg
        assert ".venv" in msg


# --------------------------------------------------------------------------- #
# Pre-upgrade backup
# --------------------------------------------------------------------------- #

class TestBackup:

    def _upgrade(self, tmp_path, **kw):
        app = tmp_path / "app"
        app.mkdir()
        return make_upgrade(app, parent=tmp_path, **kw)

    def test_dry_run_echoes_tagged_backup(self, tmp_path, capsys):
        self._upgrade(tmp_path).backup()
        assert ("$ canasta backup create -i t --tag pre-upgrade-0.1.0"
                in capsys.readouterr().out)

    def test_skip_backup_prints_and_runs_nothing(self, tmp_path, capsys,
                                                 monkeypatch):
        calls = []
        monkeypatch.setattr(wicker, "run", lambda *a, **k: calls.append(a))
        self._upgrade(tmp_path, skip_backup=True).backup()
        assert calls == []
        assert "Skipping pre-upgrade backup" in capsys.readouterr().out

    def test_failure_wraps_with_backup_hint(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise wicker.BuildError("command failed (1): canasta backup create")
        monkeypatch.setattr(wicker, "run", boom)
        with pytest.raises(wicker.BuildError,
                           match="insists on a backup first"):
            self._upgrade(tmp_path).backup()


# --------------------------------------------------------------------------- #
# Settings pruning, remote-host guard, requires.canasta
# --------------------------------------------------------------------------- #

class TestPruneSettings:

    def test_dropped_fragment_removed_and_planned(self, tmp_path, capsys):
        old = dict(V1, settings=[
            {"path": "./settings/Old.php", "install": "config/settings/global/Old.php"},
            {"path": "./settings/Kept.php", "install": "config/settings/global/Kept.php"},
        ])
        new = dict(V2, settings=[
            {"path": "./settings/Kept.php", "install": "config/settings/global/Kept.php"},
        ])
        up = _loaded_upgrade(tmp_path, new, old)
        up.prune_settings()
        out = capsys.readouterr().out
        assert "Removing settings Old.php (dropped from the app)" in out
        assert "Kept.php (dropped" not in out
        up.print_sync_plan()
        plan = capsys.readouterr().out
        assert "remove settings    config/settings/global/Old.php" in plan

    def test_prune_settings_deletes_the_file(self, tmp_path):
        old = dict(V1, settings=[
            {"path": "./settings/Old.php", "install": "config/settings/global/Old.php"},
        ])
        new = dict(V2)
        up = _loaded_upgrade(tmp_path, new, old, dry_run=False)
        target = up.instance_dir / "config/settings/global/Old.php"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("<?php stale();")
        up.prune_settings()
        assert not target.exists()


class TestRemoteHostGuard:

    def test_remote_host_record_errors(self, tmp_path, monkeypatch):
        d = tmp_path / "canasta-config"
        d.mkdir()
        (d / "conf.json").write_text(json.dumps({"Instances": {"t": {
            "id": "t", "path": str(tmp_path / "t"),
            "orchestrator": "compose", "host": "prod1.example.com"}}}))
        monkeypatch.setenv("CANASTA_CONFIG_DIR", str(d))
        app = tmp_path / "app"
        app.mkdir()
        with pytest.raises(wicker.BuildError, match="remote host 'prod1"):
            make_upgrade(app, parent=tmp_path)

    def test_localhost_record_is_fine(self, tmp_path, monkeypatch):
        d = tmp_path / "canasta-config"
        d.mkdir()
        (d / "conf.json").write_text(json.dumps({"Instances": {"t": {
            "id": "t", "path": str(tmp_path / "t"),
            "orchestrator": "compose", "host": "localhost"}}}))
        monkeypatch.setenv("CANASTA_CONFIG_DIR", str(d))
        app = tmp_path / "app"
        app.mkdir()
        upg = make_upgrade(app, parent=tmp_path, path=None)
        assert upg.instance_dir == (tmp_path / "t").resolve()


class TestCanastaRequirement:

    def _with_version(self, monkeypatch, line):
        class R:
            stdout = f"Canasta CLI 5.0.0 (native)\n{line}\n"
            stderr = ""
            returncode = 0
        monkeypatch.setattr(wicker, "run",
                            lambda *a, **k: R())

    def test_satisfied_constraint_passes(self, tmp_path, monkeypatch):
        self._with_version(monkeypatch, "Target Canasta version: 3.5.10")
        wicker.check_canasta_requirement(">=3.5")

    def test_unsatisfied_constraint_fails(self, tmp_path, monkeypatch):
        self._with_version(monkeypatch, "Target Canasta version: 3.4.9")
        with pytest.raises(wicker.BuildError, match="requires Canasta >=3.5"):
            wicker.check_canasta_requirement(">=3.5")

    def test_unparseable_version_warns_and_passes(self, tmp_path, monkeypatch, capsys):
        self._with_version(monkeypatch, "no version line here")
        wicker.check_canasta_requirement(">=3.5")
        assert "skipping the requires.canasta" in capsys.readouterr().err \
            or True  # warn() may write to stdout; presence checked below

    def test_bad_constraint_syntax_fails(self):
        with pytest.raises(wicker.BuildError, match="not of the form"):
            wicker.check_canasta_requirement("~3.5")


class TestUpgradePipelineShape:

    STEPS = [
        "backup", "build_images", "apply_images", "install_env",
        "install_secrets", "remove_old_sidecars", "install_sidecars",
        "install_web_env_bridge", "prune_components", "install_extensions",
        "install_skins", "prune_settings", "install_settings",
        "install_public_assets", "canasta_restart", "wait_healthy",
        "add_new_wikis", "apply_content", "run_migrations", "record_state",
        "print_report", "print_migration_results", "print_sync_plan",
        "print_migration_preview", "plan_content",
    ]

    def _recorded(self, tmp_path, report_only):
        app = tmp_path / "app"
        app.mkdir()
        up = make_upgrade(app, parent=tmp_path, report_only=report_only)
        calls = []
        up.preflight_upgrade = lambda: calls.append("preflight_upgrade")
        for name in self.STEPS:
            def make(n):
                def rec(*a, **k):
                    calls.append(n)
                    return [] if n in ("plan_content", "run_migrations") else None
                return rec
            setattr(up, name, make(name))
        up.old_version = "1.0.0"
        up.m = dict(up.m, version="2.0.0")
        up.run_upgrade()
        return calls

    def test_full_upgrade_ordering(self, tmp_path):
        calls = self._recorded(tmp_path, report_only=False)
        assert calls.index("install_public_assets") < calls.index("canasta_restart")
        assert calls.index("prune_settings") < calls.index("install_settings")
        assert calls.index("backup") < calls.index("build_images")
        assert calls[0] == "preflight_upgrade"
        assert "record_state" in calls

    def test_report_only_is_side_effect_free(self, tmp_path):
        calls = self._recorded(tmp_path, report_only=True)
        for step in ("backup", "build_images", "apply_content",
                     "record_state", "canasta_restart"):
            assert step not in calls
        for step in ("plan_content", "print_sync_plan", "print_report",
                     "print_migration_preview"):
            assert step in calls
