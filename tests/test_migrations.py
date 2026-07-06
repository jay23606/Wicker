"""Tests for upgrade migrations — entry selection (applicable_migrations),
the template-calls transform ops (mwparserfromhell-based), schema validation
of the migrations section, and the runner's enumeration/dispatch (SQL and
wiki I/O stubbed).
"""

import pytest
import yaml

import wicker
from conftest import make_upgrade, minimal_manifest


# --------------------------------------------------------------------------- #
# applicable_migrations
# --------------------------------------------------------------------------- #

def _entry(to, steps=None):
    return {"to": to, "steps": steps or [{"kind": "update"}]}


class TestApplicableMigrations:
    def test_filters_and_orders(self):
        entries = [_entry("3.0.0"), _entry("1.5.0"), _entry("2.0.0"),
                   _entry("0.9.0")]
        run = wicker.applicable_migrations(entries, "1.0.0", "3.0.0")
        assert [e["to"] for e in run] == ["1.5.0", "2.0.0", "3.0.0"]

    def test_none_is_empty(self):
        assert wicker.applicable_migrations(None, "1.0.0", "2.0.0") == []

    def test_duplicate_to_refused(self):
        with pytest.raises(wicker.BuildError, match="duplicate"):
            wicker.applicable_migrations(
                [_entry("2.0.0"), _entry("2.0.0")], "1.0.0", "2.0.0")

    def test_beyond_target_refused(self):
        with pytest.raises(wicker.BuildError, match="beyond"):
            wicker.applicable_migrations(
                [_entry("2.1.0")], "1.0.0", "2.0.0")


class TestNormalizeTemplateName:
    def test_variants(self):
        f = wicker.normalize_template_name
        assert f("expense") == "Expense"
        assert f(" Expense_report\n") == "Expense report"
        assert f("Expense") == "Expense"


# --------------------------------------------------------------------------- #
# transform_template_calls
# --------------------------------------------------------------------------- #

NAMES = {"Expense", "Template:Expense"}

BLOCK = ("intro\n"
         "{{Expense\n"
         "| cost = 45.00\n"
         "| category = Food\n"
         "| note = has {{tick}} inside\n"
         "}}\n"
         "inline {{expense|cost=3|category=Misc|legacy=1}} tail\n"
         "other {{Income|cost=9}} stays\n")


def xform(text, transforms, names=NAMES):
    return wicker.transform_template_calls(text, names, transforms)


class TestTransforms:
    def test_rename_param_both_styles_case_insensitive_name(self):
        new, notes = xform(BLOCK, [{"op": "rename-param",
                                    "from": "cost", "to": "amount"}])
        assert notes == []
        assert "| amount = 45.00" in new          # padding preserved
        assert "{{expense|amount=3" in new        # lowercase call matched
        assert "{{Income|cost=9}}" in new         # other template untouched

    def test_rename_preserves_nested_template_in_value(self):
        new, _ = xform(BLOCK, [{"op": "rename-param",
                                "from": "note", "to": "comment"}])
        assert "| comment = has {{tick}} inside" in new

    def test_rename_skipped_when_target_exists(self):
        text = "{{Expense|cost=1|amount=2}}\n"
        new, notes = xform(text, [{"op": "rename-param",
                                   "from": "cost", "to": "amount"}])
        assert new is None
        assert "both 'cost' and 'amount' present" in notes[0]

    def test_add_param_literal_and_if_absent(self):
        new, _ = xform("{{Expense|cost=1}} {{Expense|cost=2|currency=EUR}}\n",
                       [{"op": "add-param", "name": "currency",
                         "value": "USD"}])
        assert "{{Expense|cost=1|currency=USD}}" in new
        assert "currency=EUR" in new              # existing value untouched

    def test_add_param_from_other_param(self):
        new, _ = xform("{{Expense|cost=7}}\n",
                       [{"op": "add-param", "name": "orig_cost",
                         "from": "cost"}])
        assert "orig_cost=7" in new

    def test_add_param_from_missing_source_skipped(self):
        new, notes = xform("{{Expense|category=Food}}\n",
                           [{"op": "add-param", "name": "orig_cost",
                             "from": "cost"}])
        assert new is None and notes == []

    def test_remove_param(self):
        new, _ = xform(BLOCK, [{"op": "remove-param", "name": "legacy"}])
        assert "legacy" not in new

    def test_map_values_with_padding_and_report(self):
        new, notes = xform(BLOCK, [{"op": "map-values", "name": "category",
                                    "map": {"Food": "Meals"}}])
        assert "| category = Meals\n" in new      # padding preserved
        assert notes == ["map-values: category='Misc' not in the map; kept"]

    def test_map_values_unmapped_keep_is_silent(self):
        new, notes = xform(BLOCK, [{"op": "map-values", "name": "category",
                                    "map": {"Food": "Meals"},
                                    "unmapped": "keep"}])
        assert new is not None and notes == []

    def test_map_values_rerun_is_quiet(self):
        # A value already in the map's range is not re-reported.
        new, notes = xform("{{Expense|category=Meals}}\n",
                           [{"op": "map-values", "name": "category",
                             "map": {"Food": "Meals"}}])
        assert new is None and notes == []

    def test_unchanged_returns_none(self):
        new, notes = xform("no calls here\n",
                           [{"op": "remove-param", "name": "x"}])
        assert new is None and notes == []

    def test_idempotent_rerun(self):
        tr = [{"op": "rename-param", "from": "cost", "to": "amount"},
              {"op": "add-param", "name": "currency", "value": "USD"}]
        once, _ = xform(BLOCK, tr)
        again, notes = xform(once, tr)
        assert again is None and notes == []


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

MIGRATIONS = [{
    "to": "2.0.0",
    "description": "Expense rework",
    "steps": [
        {"kind": "template-calls", "template": "Expense",
         "transforms": [
             {"op": "rename-param", "from": "cost", "to": "amount"},
             {"op": "add-param", "name": "currency", "value": "USD"},
             {"op": "remove-param", "name": "legacy"},
             {"op": "map-values", "name": "category",
              "map": {"Food": "Meals"}, "unmapped": "report"},
         ]},
        {"kind": "maintenance", "extension": "Cargo",
         "script": "cargoRecreateData.php", "args": ["--table=Expenses"]},
        {"kind": "update"},
    ],
}]


class TestSchema:
    def test_valid_migrations_accepted(self):
        wicker.validate_manifest(
            minimal_manifest(version="2.0.0", migrations=MIGRATIONS))

    def test_bad_op_rejected(self):
        bad = [{"to": "2.0.0", "steps": [
            {"kind": "template-calls", "template": "X",
             "transforms": [{"op": "explode"}]}]}]
        with pytest.raises(wicker.BuildError):
            wicker.validate_manifest(
                minimal_manifest(version="2.0.0", migrations=bad))

    def test_add_param_needs_value_or_from(self):
        bad = [{"to": "2.0.0", "steps": [
            {"kind": "template-calls", "template": "X",
             "transforms": [{"op": "add-param", "name": "n"}]}]}]
        with pytest.raises(wicker.BuildError):
            wicker.validate_manifest(
                minimal_manifest(version="2.0.0", migrations=bad))


# --------------------------------------------------------------------------- #
# Runner (SQL + wiki I/O stubbed)
# --------------------------------------------------------------------------- #

def _runner(tmp_path, monkeypatch, live_pages, transclusions, redirects=(),
            namespaces=None):
    manifest = minimal_manifest(
        version="2.0.0", migrations=MIGRATIONS,
        content={"pages": {"dir": "./content/pages",
                           "list": ["Template:Expense"]}})
    (tmp_path / "wicker.yaml").write_text(yaml.safe_dump(manifest))
    up = make_upgrade(tmp_path, manifest, dry_run=False)
    up.old_version = "1.0.0"
    up.migrations = wicker.applicable_migrations(
        manifest["migrations"], "1.0.0", "2.0.0")

    def sql(query):
        if "templatelinks" in query:
            return [{"page_namespace": ns, "page_title": t}
                    for ns, t in transclusions]
        if "redirect" in query:
            return [{"page_title": t} for t in redirects]
        raise AssertionError(query)

    monkeypatch.setattr(up, "_sql_json", sql)
    monkeypatch.setattr(up, "_wiki_namespaces", lambda: namespaces or {})
    monkeypatch.setattr(up, "_live_page_text",
                        lambda title: live_pages.get(title))
    edits = []
    monkeypatch.setattr(
        up, "_mw_edit",
        lambda title, src, summary=None:
            edits.append((title, src.read_text(), summary)))
    actions = []
    monkeypatch.setattr(
        up, "run_actions",
        lambda steps, phase: actions.append((steps[0]["kind"], phase)))
    return up, edits, actions


class TestRunner:
    def test_end_to_end(self, tmp_path, monkeypatch):
        live = {
            "March groceries": "{{Expense|cost=12.50|category=Food}}\n",
            "Old style": "{{Spesa|cost=3|category=Rent}}\n",   # via redirect
            "Template:Expense": "shipped — must be excluded\n",
        }
        up, edits, actions = _runner(
            tmp_path, monkeypatch, live,
            transclusions=[(0, "March_groceries"), (0, "Old_style"),
                           (10, "Expense"), (120, "Weird_ns_page")],
            redirects=["Spesa"])
        results = up.run_migrations()

        assert [t for t, _, _ in edits] == ["March groceries", "Old style"]
        text = edits[0][1]
        assert "amount=12.50" in text and "category=Meals" in text \
            and "currency=USD" in text
        assert "Spesa|amount=3" in edits[1][1]     # redirect call rewritten
        assert edits[0][2] == "app 2.0.0 migration (wicker)"
        # the maintenance + update steps were dispatched as actions
        assert actions == [("maintenance", "migration to 2.0.0"),
                           ("update", "migration to 2.0.0")]
        r = results[0]
        assert r["edited"] == 2 and r["unchanged"] == 0
        # Rent unmapped (reported) + unknown namespace note
        assert any("Rent" in n for n in r["notes"])
        assert any("namespace 120" in n for n in r["notes"])

    def test_custom_namespace_resolved_from_wiki(self, tmp_path, monkeypatch):
        live = {
            "Expense:2026 Rent": "{{Expense|cost=900|category=Rent housing}}\n",
        }
        up, edits, _ = _runner(
            tmp_path, monkeypatch, live,
            transclusions=[(3000, "2026_Rent")],
            namespaces={3000: "Expense"})
        results = up.run_migrations()

        assert [t for t, _, _ in edits] == ["Expense:2026 Rent"]
        assert results[0]["edited"] == 1
        assert not any("namespace 3000" in n for n in results[0]["notes"])

    def test_dry_run_previews_only(self, tmp_path, monkeypatch, capsys):
        up, edits, actions = _runner(tmp_path, monkeypatch, {},
                                     transclusions=[(0, "Anything")])
        up.dry_run = True
        assert up.run_migrations() == []
        assert edits == [] and actions == []
        out = capsys.readouterr().out
        assert "Migration entries that would run" in out
        assert "template-calls Template:Expense" in out


class TestNameNormalization:

    def test_lowercase_prefixed_call_is_matched(self):
        text = "{{template:expense|category=Food}}\n"
        new, notes = wicker.transform_template_calls(
            text, {"Expense", "Template:Expense"},
            [{"op": "rename-param", "from": "category", "to": "expense_category"}])
        assert new is not None and "expense_category=Food" in new

    def test_underscore_shipped_title_still_skipped(self, tmp_path, monkeypatch):
        # Manifest lists "Main Page"; the DB row yields "Main_Page" — the
        # normalized comparison must still classify it as shipped.
        assert wicker.normalize_title("Main_Page") == \
            wicker.normalize_title("Main Page")
        assert wicker.normalize_title("expense") == "Expense"
