"""Minimal HTML extraction built on the standard library only.

The AIS pages we need to read are plain server-rendered Rails templates, so a
single-pass ``html.parser`` sweep is enough to pull out everything the client
needs: the CSRF meta tag, form fields (including the hidden ones Rails
generates), anchor targets, and the ``data-sitekey`` / ``data-action``
attributes the site injects when it decides to arm reCAPTCHA.

Using the stdlib keeps this repo free of a BeautifulSoup/lxml dependency.
"""

from html.parser import HTMLParser
from typing import Dict, List, Optional
from urllib.parse import urljoin


class Field:
    """A single form control (``input``, ``select`` or ``textarea``)."""

    __slots__ = ("tag", "name", "value", "type", "id", "data_action", "form_id")

    def __init__(self, tag, name, value, type_, id_, data_action, form_id):
        self.tag = tag
        self.name = name
        self.value = value
        self.type = type_
        self.id = id_
        self.data_action = data_action
        self.form_id = form_id

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Field {self.tag} name={self.name!r} type={self.type!r} id={self.id!r}>"


class Form:
    """A ``<form>`` element and the controls found inside it."""

    __slots__ = ("id", "action", "method", "remote", "fields")

    def __init__(self, id_, action, method, remote):
        self.id = id_
        self.action = action
        self.method = (method or "get").lower()
        self.remote = remote
        self.fields: List[Field] = []

    def payload(self, include_submit: bool = True) -> Dict[str, str]:
        """Name/value pairs for every named control, ready to be POSTed.

        Controls without a ``name`` are skipped.  ``select`` elements come back
        with an empty string because the caller is expected to fill them in.
        """
        data: Dict[str, str] = {}
        for f in self.fields:
            if not f.name:
                continue
            if f.type in ("checkbox", "radio") and f.value is None:
                continue
            if f.type == "submit" and not include_submit:
                continue
            data[f.name] = f.value if f.value is not None else ""
        return data

    def field(self, name: str) -> Optional[Field]:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Form id={self.id!r} action={self.action!r} fields={len(self.fields)}>"


class Link:
    """An anchor with its resolved href and collapsed text."""

    __slots__ = ("href", "text")

    def __init__(self, href, text):
        self.href = href
        self.text = text

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Link {self.text!r} -> {self.href!r}>"


class Page(HTMLParser):
    """Parsed view of one HTML response."""

    def __init__(self, html: str, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.metas: Dict[str, str] = {}
        self.forms: List[Form] = []
        self.links: List[Link] = []
        # Elements carrying a data-sitekey, i.e. the captcha container once the
        # server decides to render it.
        self.captcha_hosts: List[Dict[str, str]] = []
        self.title = ""

        self._form_stack: List[Form] = []
        self._a_href: Optional[str] = None
        self._a_text: List[str] = []
        self._in_title = False
        self._select: Optional[Field] = None
        self._skip_depth = 0
        self._text_parts: List[str] = []

        self.feed(html)
        self.close()

    @property
    def text(self) -> str:
        """All visible text, whitespace-collapsed (script/style excluded)."""
        return " ".join("".join(self._text_parts).split())

    # -- helpers ---------------------------------------------------------

    @property
    def _current_form(self) -> Optional[Form]:
        return self._form_stack[-1] if self._form_stack else None

    def _resolve(self, href: str) -> str:
        if not href:
            return ""
        return urljoin(self.base_url, href) if self.base_url else href

    # -- HTMLParser hooks ------------------------------------------------

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)

        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
            return

        if tag == "meta":
            name = a.get("name") or a.get("property")
            if name:
                self.metas[name] = a.get("content", "")
            return

        if tag == "title":
            self._in_title = True
            return

        if tag == "form":
            self._form_stack.append(
                Form(a.get("id"), self._resolve(a.get("action", "")),
                     a.get("method"), a.get("data-remote"))
            )
            return

        if tag == "a":
            self._a_href = self._resolve(a.get("href", ""))
            self._a_text = []
            return

        if a.get("data-sitekey"):
            self.captcha_hosts.append({
                "tag": tag,
                "id": a.get("id", ""),
                "class": a.get("class", ""),
                "sitekey": a["data-sitekey"],
                "badge": a.get("data-badge", ""),
                "action": a.get("data-action", ""),
                "size": a.get("data-size", ""),
            })

        if tag in ("input", "textarea", "select"):
            field = Field(
                tag=tag,
                name=a.get("name"),
                value=a.get("value"),
                type_=(a.get("type") or ("select" if tag == "select" else "text")).lower(),
                id_=a.get("id"),
                data_action=a.get("data-action"),
                form_id=self._current_form.id if self._current_form else None,
            )
            # Unchecked checkboxes/radios must not contribute to the payload.
            if field.type in ("checkbox", "radio") and "checked" not in a:
                field.value = None
            if tag == "select":
                self._select = field
            if self._current_form is not None:
                self._current_form.fields.append(field)
            else:
                # Controls outside any form still matter (e.g. a stray
                # g-recaptcha-response); park them on a synthetic form.
                orphan = self._orphan_form()
                orphan.fields.append(field)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "form" and self._form_stack:
            self.forms.append(self._form_stack.pop())
        elif tag == "a" and self._a_href is not None:
            text = " ".join("".join(self._a_text).split())
            self.links.append(Link(self._a_href, text))
            self._a_href = None
            self._a_text = []
        elif tag == "title":
            self._in_title = False
        elif tag == "select":
            self._select = None

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._text_parts.append(data)
        if self._a_href is not None:
            self._a_text.append(data)
        if self._in_title:
            self.title += data

    def close(self):
        super().close()
        # Flush unbalanced <form> tags so nothing is lost on sloppy markup.
        while self._form_stack:
            self.forms.append(self._form_stack.pop())

    def _orphan_form(self) -> Form:
        for f in self.forms:
            if f.id == "__orphan__":
                return f
        orphan = Form("__orphan__", "", "get", None)
        self.forms.append(orphan)
        return orphan

    # -- public API ------------------------------------------------------

    def form_by_id(self, form_id: str) -> Optional[Form]:
        for f in self.forms:
            if f.id == form_id:
                return f
        return None

    def csrf_token(self) -> str:
        """The Rails CSRF token from ``<meta name="csrf-token">``."""
        return self.metas.get("csrf-token", "")

    def links_matching(self, predicate) -> List[Link]:
        return [l for l in self.links if predicate(l)]

    def find_field(self, field_id: str) -> Optional[Field]:
        for form in self.forms:
            for f in form.fields:
                if f.id == field_id:
                    return f
        return None

    def captcha_challenge(self) -> Optional[Dict[str, str]]:
        """Describe the reCAPTCHA challenge if the server armed one.

        Returns ``None`` when the page carries no captcha, which is the normal
        case.  When a challenge is present the dict contains the ``sitekey``,
        the v3 ``action`` (empty for v2), and ``invisible``/``version`` hints
        derived from how the site renders the widget.
        """
        response_field = self.find_field("g-recaptcha-response")
        host = self.captcha_hosts[0] if self.captcha_hosts else None

        if host is None and response_field is None:
            return None

        sitekey = host["sitekey"] if host else ""
        action = ""
        if response_field is not None and response_field.data_action:
            action = response_field.data_action
        elif host and host.get("action"):
            action = host["action"]

        size = (host or {}).get("size", "")
        # The site renders with size:'invisible' and drives it through
        # grecaptcha.execute(action), which is the v3 contract.  Absent an
        # action we fall back to treating it as v2.
        version = 3 if action else 2
        return {
            "sitekey": sitekey,
            "action": action,
            "version": version,
            "invisible": bool(action) or size == "invisible",
            "response_field": response_field.name if response_field else "g-recaptcha-response",
        }
