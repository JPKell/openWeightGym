"""Retain the submitted form in an error response without storing a draft anywhere."""

from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class FormList(HTMLParser):
    """HTML parser collecting forms and their submission destinations."""

    def __init__(self, html, url):
        """Initialize form discovery for the supplied HTML and page URL."""
        super().__init__(convert_charrefs=False)
        self.url, self.forms, self.current = url, [], None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        """Collect a form's destination and its named input fields."""
        attrs = dict(attrs)
        if tag == "form":
            self.current = {
                "action": urlsplit(
                    urljoin(self.url, attrs.get("action") or self.url)
                ).path,
                "method": attrs.get("method", "get").upper(),
                "id": attrs.get("id"),
                "names": set(),
                "hidden": {},
            }
            self.forms.append(self.current)
        elif self.current is not None and attrs.get("name"):
            self.current["names"].add(attrs["name"])
            if tag == "input" and attrs.get("type") == "hidden":
                self.current["hidden"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        """Finish collecting the current form at its closing tag."""
        if tag == "form":
            self.current = None


def submitted_form(html, url, action, method, values):
    """Distinguish forms sharing an address using their hidden action/profile identifiers."""
    forms = FormList(html, url).forms
    candidates = []
    for index, form in enumerate(forms):
        if form["action"] != action or form["method"] != method:
            continue
        if any(
            name in values and value not in values[name]
            for name, value in form["hidden"].items()
        ):
            continue
        candidates.append((len(form["names"] & values.keys()), index))
    if not candidates:
        return None, None
    _, chosen = max(candidates, key=lambda item: item[0])
    return chosen, forms[chosen]["id"]


def start_tag(tag, attrs):
    """Render an HTML opening tag with escaped attribute values."""
    return (
        "<"
        + tag
        + "".join(
            " "
            + name
            + ("" if value is None else '="' + escape(value, quote=True) + '"')
            for name, value in attrs.items()
        )
        + ">"
    )


class Refill(HTMLParser):
    """HTML parser restoring submitted fields without exposing protected inputs."""

    def __init__(self, html, values, chosen, form_id):
        """Initialize escaped field restoration for the selected submitted form."""
        super().__init__(convert_charrefs=False)
        self.values, self.chosen, self.form_id = values, chosen, form_id
        self.output, self.form_number, self.active = [], -1, False
        self.textarea, self.select, self.option = False, None, None
        self.feed(html)

    def handle_starttag(self, tag, pairs):
        """Restore permitted submitted input values while preserving markup."""
        attrs = dict(pairs)
        original = self.get_starttag_text()
        if tag == "form":
            self.form_number += 1
            self.active = self.form_number == self.chosen
        belongs = self.active or (self.form_id and attrs.get("form") == self.form_id)
        name = attrs.get("name")
        if belongs and name and "disabled" not in attrs:
            if tag == "input":
                kind = attrs.get("type", "text")
                if kind in ("checkbox", "radio"):
                    attrs.pop("checked", None)
                    if attrs.get("value", "on") in self.values.get(name, []):
                        attrs["checked"] = None
                    original = start_tag(tag, attrs)
                # A password is never written back into a page, whoever typed it.
                elif name in self.values and kind not in (
                    "password",
                    "file",
                    "submit",
                    "button",
                    "image",
                    "reset",
                ):
                    attrs["value"] = self.values[name][-1]
                    original = start_tag(tag, attrs)
            elif tag == "textarea" and name in self.values:
                self.textarea = True
                self.output.extend(
                    [original, escape(self.values[name][-1], quote=False)]
                )
                return
            elif tag == "select" and (name in self.values or "multiple" in attrs):
                self.select = {"wanted": self.values.get(name, []), "seen": set()}
        if tag == "option" and self.select is not None:
            self.option = {"attrs": attrs, "start": len(self.output), "text": []}
        self.output.append(original)

    def handle_startendtag(self, tag, attrs):
        """Process a self-closing input tag through the same refill rules."""
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        """Close the current refill field and preserve the ending tag."""
        if tag == "textarea" and self.textarea:
            self.textarea = False
        elif tag == "option" and self.option is not None:
            attrs = self.option["attrs"]
            value = attrs.get("value", unescape("".join(self.option["text"])).strip())
            self.select["seen"].add(value)
            attrs.pop("selected", None)
            if value in self.select["wanted"]:
                attrs["selected"] = None
            self.output[self.option["start"]] = start_tag("option", attrs)
            self.option = None
        elif tag == "select" and self.select is not None:
            for value in self.select["wanted"]:
                if value not in self.select["seen"]:
                    self.output.append(
                        start_tag("option", {"value": value, "selected": None})
                        + escape(value)
                        + "</option>"
                    )
            self.select = None
        if not self.textarea:
            self.output.append("</" + tag + ">")
        if tag == "form":
            self.active = False

    def handle_data(self, data):
        """Preserve text or replace the selected textarea and option contents."""
        if self.option is not None:
            self.option["text"].append(data)
        if not self.textarea:
            self.output.append(data)

    def handle_entityref(self, name):
        """Preserve a named HTML entity through the form parser."""
        self.handle_data("&" + name + ";")

    def handle_charref(self, name):
        """Preserve a numeric HTML character reference through the form parser."""
        self.handle_data("&#" + name + ";")

    def handle_comment(self, data):
        """Preserve comments outside a replaced textarea value."""
        if not self.textarea:
            self.output.append("<!--" + data + "-->")

    def handle_decl(self, data):
        """Preserve document declarations while rebuilding the form markup."""
        self.output.append("<!" + data + ">")


def refill(html, url, action, method, values):
    """Restore the submitted matching form and report whether it was found."""
    chosen, form_id = submitted_form(html, url, action, method, values)
    if chosen is None:
        return html, False
    return "".join(Refill(html, values, chosen, form_id).output), True


def error_message(html):
    """Extract plain error text; never copy executable markup into another response."""

    class Messages(HTMLParser):
        """HTML parser extracting visible form error messages."""

        def __init__(self):
            """Initialize error-message text collection and nested-element tracking."""
            super().__init__()
            self.depth, self.text = 0, []

        def handle_starttag(self, tag, attrs):
            """Begin collecting text within a visible error-message element."""
            attrs = dict(attrs)
            if self.depth:
                self.depth += 1
            elif (
                "alert-danger" in attrs.get("class", "").split()
                or attrs.get("role") == "alert"
            ):
                self.depth = 1

        def handle_endtag(self, tag):
            """Track the end of nested error-message markup."""
            if self.depth:
                self.depth -= 1

        def handle_data(self, text):
            """Collect visible text within the current error-message element."""
            if self.depth:
                self.text.append(text)

    parser = Messages()
    parser.feed(html)
    return " ".join("".join(parser.text).split())
