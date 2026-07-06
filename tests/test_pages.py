"""Tests for starter-page title -> file path derivation.

The on-disk layout under content.pages.dir mirrors the wiki: the namespace
becomes a directory, the rest of the title is kept verbatim (spaces, subpage
slashes), so the path reads like the page title.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import wicker  # noqa: E402


class TestPageFilename:
    f = staticmethod(wicker.Deploy._page_filename)

    def test_namespaced_page(self):
        assert self.f("Template:Expense") == "Template/Expense.mediawiki"
        assert self.f("Form:Income") == "Form/Income.mediawiki"
        assert self.f("Project:Expense categories") == \
            "Project/Expense categories.mediawiki"

    def test_main_namespace_page_sits_at_root(self):
        assert self.f("Main Page") == "Main Page.mediawiki"

    def test_subpage_slash_preserved(self):
        assert self.f("Template:User receipts row expense") == \
            "Template/User receipts row expense.mediawiki"
        assert self.f("MediaWiki:Pf formedit edittitle") == \
            "MediaWiki/Pf formedit edittitle.mediawiki"

    def test_css_js_subpages_keep_their_extension(self):
        # namespace ':' -> dir, subpage '/' kept, no .mediawiki appended
        assert self.f("Template:Receipt dashboard/styles.css") == \
            "Template/Receipt dashboard/styles.css"
        assert self.f("Template:User receipts/styles.css") == \
            "Template/User receipts/styles.css"
        assert self.f("Gadget:foo.js") == "Gadget/foo.js"
