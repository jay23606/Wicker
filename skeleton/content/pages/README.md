# Wiki pages

One file per starter wiki page. The **file path mirrors the page title**:
the first directory is the namespace, the rest is the page name, and
subpages nest further. So the directory tree *is* the page catalog — there's
no separate mapping file.

```
pages/Main Page.mediawiki                     -> Main Page
pages/Template/Example.mediawiki              -> Template:Example
pages/Form/Receipt.mediawiki                  -> Form:Receipt
pages/Project/Categories.mediawiki            -> Project:Categories
pages/Template/Dashboard/styles.css           -> Template:Dashboard/styles.css
```

`.mediawiki` is appended automatically from the title; files already ending
in `.css` or `.js` keep their extension (for TemplateStyles / gadget pages).

List the pages you want imported, in order, under `content.pages.list` in
[`../../wicker.yaml`](../../wicker.yaml). The filename is derived from the
title unless an entry overrides it:

```yaml
content:
  pages:
    dir: ./content/pages
    list:
      - Main Page
      - Template:Example
      - page: Form:Receipt
        file: forms/receipt.mediawiki     # explicit override of the derived name
```
