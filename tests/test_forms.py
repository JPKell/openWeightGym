"""A form that was refused comes back with what was typed in it, and only in it."""

from weightroom.forms import error_message, refill

PAGE = """<main id="page-content"><div class="alert alert-danger">Bad <b>interval</b>.</div>
<form method="post" action="/thing"><input type="hidden" name="do" value="model"><input name="alias" value="old"></form>
<form method="post" action="/thing"><input type="hidden" name="do" value="save">
<input name="url" value="http://old"><input type="password" name="secret" value="">
<input type="checkbox" name="enabled" checked><input type="checkbox" name="extra">
<input type="radio" name="mode" value="a" checked><input type="radio" name="mode" value="b">
<select name="level"><option selected>normal</option><option>sensitive</option></select>
<textarea name="notes">old notes</textarea></form></main>"""


def test_the_form_that_was_sent_is_filled_in_again_and_no_other():
    sent = {"do": ["save"], "url": ['http://typed/?a="1"&b'], "secret": ["hunter2"], "extra": ["on"], "mode": ["b"], "level": ["sensitive"], "notes": ["<b>typed</b>"]}
    page, found = refill(PAGE, "http://site/thing", "/thing", "POST", sent)
    assert found
    assert 'name="alias" value="old"' in page  # the other form at the same address, told apart by its hidden field
    assert 'value="http://typed/?a=&quot;1&quot;&amp;b"' in page  # text, written so that it cannot end its attribute
    assert "hunter2" not in page  # a password is never sent back
    assert '<input type="checkbox" name="enabled">' in page and '<input type="checkbox" name="extra" checked>' in page  # a box left empty stays empty
    assert '<input type="radio" name="mode" value="a">' in page and '<input type="radio" name="mode" value="b" checked>' in page
    assert "<option>normal</option><option selected>sensitive</option>" in page
    assert '<textarea name="notes">&lt;b&gt;typed&lt;/b&gt;</textarea>' in page  # what was typed is text, never markup
    assert refill(PAGE, "http://site/thing", "/other", "POST", sent) == (PAGE, False)  # no form goes to that address
    assert error_message(PAGE) == "Bad interval."
