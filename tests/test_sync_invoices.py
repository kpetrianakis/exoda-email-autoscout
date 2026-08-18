"""Tests for the deterministic parts of the invoice sync.

Run:  python -m unittest discover -s tests -v

These cover the logic that used to live in the LLM prompt, where it had no
way to be verified: attachment filtering, vendor matching, filename
sanitising, link-invoice extraction, and report rendering. The counting
itself is structurally correct now (counters increment where the action
happens), so what's worth testing is the classification feeding them.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from sync_invoices import (  # noqa: E402
    detect_link_invoice,
    fmt_date,
    match_vendor,
    render_html,
    render_plain,
    safe_filename,
    should_skip_attachment,
)

VENDORS = [
    {"match": "anthropic.com", "name": "Anthropic"},
    {"match": "stripe.com", "name": "Stripe"},
    {"match": "accountant@example.com", "name": "Accountant"},
]


class TestAttachmentFilter(unittest.TestCase):
    def test_skips_decorative_and_boilerplate(self):
        for name in ["logo.png", "signature.jpg", "Terms_of_Service_el_gr.html",
                     "smime.p7s", "event.ics", "card.vcf"]:
            self.assertTrue(should_skip_attachment(name), name)

    def test_keeps_real_documents(self):
        for name in ["Invoice-123.pdf", "receipt.pdf", "τιμολόγιο.pdf",
                     "statement.PDF", "invoice.xlsx"]:
            self.assertFalse(should_skip_attachment(name), name)

    def test_keeps_invoice_named_file_despite_skipped_extension(self):
        # An HTML invoice is still an invoice; the extension rule must not
        # override an explicit filename.
        self.assertFalse(should_skip_attachment("invoice_2026.html"))
        self.assertFalse(should_skip_attachment("ΤΙΜΟΛΟΓΙΟ.html"))


class TestVendorMatching(unittest.TestCase):
    def test_matches_known_vendors_case_insensitively(self):
        self.assertEqual(match_vendor("Invoice+Statements@MAIL.ANTHROPIC.COM", VENDORS), "Anthropic")
        self.assertEqual(match_vendor("invoice+acct_x@stripe.com", VENDORS), "Stripe")
        self.assertEqual(match_vendor("accountant@example.com", VENDORS), "Accountant")

    def test_derives_name_for_unknown_sender(self):
        self.assertEqual(match_vendor("billing@example.com", VENDORS), "Example")
        self.assertEqual(match_vendor("noreply@googleplay.google.com", VENDORS), "Googleplay")


class TestFilenameSanitising(unittest.TestCase):
    def test_replaces_characters_windows_rejects(self):
        self.assertEqual(safe_filename('a/b\\c:d*e?f"g<h>i|j'), "a-b-c-d-e-f-g-h-i-j")

    def test_preserves_greek(self):
        self.assertEqual(safe_filename("Τιμολόγιο_2026.pdf"), "Τιμολόγιο_2026.pdf")


class TestLinkInvoiceDetection(unittest.TestCase):
    def test_english(self):
        url, amount, phrase = detect_link_invoice(
            "Please download your invoice here: https://billing.acme.com/i/1 Total $49.99")
        self.assertEqual(url, "https://billing.acme.com/i/1")
        self.assertEqual(amount, "$49.99")
        self.assertIsNotNone(phrase)

    def test_greek(self):
        url, amount, _ = detect_link_invoice(
            "Κατεβάστε το τιμολόγιό σας: https://elorus.gr/i/9 Σύνολο 24,80 EUR")
        self.assertEqual(url, "https://elorus.gr/i/9")
        self.assertEqual(amount, "24,80 EUR")

    def test_rejects_non_http_schemes(self):
        # An extracted URL becomes a clickable link in the report, so anything
        # that isn't plain http(s) must never make it through.
        url, _, phrase = detect_link_invoice("download your invoice javascript:alert(1)")
        self.assertIsNone(url)
        self.assertIsNotNone(phrase)

    def test_ignores_unrelated_mail(self):
        self.assertEqual(detect_link_invoice("Your package shipped https://t.example.com/x"),
                         (None, None, None))

    def test_strips_trailing_punctuation(self):
        url, _, _ = detect_link_invoice("download your invoice at https://a.com/i/1.")
        self.assertEqual(url, "https://a.com/i/1")

    def test_real_greek_wording_from_the_mailbox(self):
        # Actual ExampleCo phrasing; the earlier fixed-phrase list missed it
        # because Greek inflects the verb away from the imperative.
        url, _, phrase = detect_link_invoice(
            "Μπορείτε να κατεβάσετε ή να λάβετε τα τιμολόγια σας "
            "https://portal.exampleco.com/invoice/313648")
        self.assertEqual(url, "https://portal.exampleco.com/invoice/313648")
        self.assertIsNotNone(phrase)

    def test_prefers_document_link_over_boilerplate(self):
        url, _, _ = detect_link_invoice(
            "download your invoice https://acme.com/unsubscribe "
            "https://acme.com/invoices/8891.pdf")
        self.assertEqual(url, "https://acme.com/invoices/8891.pdf")

    def test_prefers_path_over_bare_marketing_domain(self):
        url, _, _ = detect_link_invoice(
            "κατεβάσετε τα τιμολόγια https://www.example.gr "
            "https://portal.exampleco.com/invoice/1")
        self.assertEqual(url, "https://portal.exampleco.com/invoice/1")

    def test_ignores_the_mails_own_stylesheet(self):
        # A <link rel=stylesheet> href was once reported as the invoice
        # download link; only <a> hrefs count, and asset URLs are penalised.
        url, _, _ = detect_link_invoice(
            "κατεβάσετε τα τιμολόγια https://www.exampleco.com/css/email.css")
        self.assertIsNone(url)

    def test_prefers_customer_portal_over_marketing_site(self):
        url, _, _ = detect_link_invoice(
            "κατεβάσετε τα τιμολόγιά σας https://www.example.gr https://users.example.gr")
        self.assertEqual(url, "https://users.example.gr")

    def test_document_path_beats_portal_root(self):
        url, _, _ = detect_link_invoice(
            "download your invoice https://users.acme.com "
            "https://acme.com/invoices/8891.pdf")
        self.assertEqual(url, "https://acme.com/invoices/8891.pdf")

    def test_returns_no_url_when_only_junk_links_exist(self):
        # Better to fall back to the Gmail thread link than to publish an
        # unsubscribe URL labelled as the invoice.
        url, _, phrase = detect_link_invoice(
            "download your invoice https://a.com/unsubscribe")
        self.assertIsNone(url)
        self.assertIsNotNone(phrase)


def _ctx(**over):
    base = {"today": "2026-08-18", "since": "2026-08-15", "year": "2026",
            "new": [], "duplicates": [], "flagged": [], "errors": [],
            "cost_line": "Run cost: $0.00"}
    base.update(over)
    return base


class TestDateFormatting(unittest.TestCase):
    def test_human_readable_no_leading_zero(self):
        self.assertEqual(fmt_date("2026-06-12"), "12 June 2026")
        self.assertEqual(fmt_date("2026-08-03"), "3 August 2026")

    def test_passes_through_unparseable(self):
        self.assertEqual(fmt_date("not-a-date"), "not-a-date")


class TestRendering(unittest.TestCase):
    def test_empty_sections_are_omitted(self):
        html = render_html(_ctx())
        self.assertNotIn("New invoices", html)
        self.assertNotIn("Duplicates skipped", html)
        self.assertIn("Run cost", html)

    def test_rows_actually_render(self):
        # The bug that motivated this rewrite: tiles present, rows missing.
        rows = [{"date": "2026-08-17", "vendor": "Stripe",
                 "sender": "billing@stripe.com", "filename": "Invoice-1.pdf",
                 "subject": "Your Stripe invoice",
                 "thread_link": "https://mail.google.com/mail/u/0/#inbox/abc"}]
        html = render_html(_ctx(duplicates=rows))
        self.assertIn("Invoice-1.pdf", html)
        self.assertIn("billing@stripe.com", html)
        self.assertIn("Duplicates skipped", html)
        self.assertIn("Invoice-1.pdf", render_plain(_ctx(duplicates=rows)))

    def test_large_duplicate_list_collapses_to_a_count(self):
        rows = [{"date": "2026-08-17", "vendor": "V", "sender": "a@b.com",
                 "filename": f"f{i}.pdf", "subject": "s",
                 "thread_link": "https://mail.google.com/x"} for i in range(11)]
        html = render_html(_ctx(duplicates=rows))
        self.assertIn("11 duplicates skipped", html)
        self.assertNotIn("f10.pdf", html)

    def test_every_row_gets_an_open_link_and_subject(self):
        rows = [{"date": "2026-08-17", "vendor": "Stripe", "sender": "b@s.com",
                 "filename": "i.pdf", "subject": "Your invoice",
                 "thread_link": "https://mail.google.com/mail/u/0/#inbox/abc"}]
        for key in ("new", "duplicates"):
            html = render_html(_ctx(**{key: rows}))
            self.assertIn("#inbox/abc", html, key)
            self.assertIn("Your invoice", html, key)

    def test_untrusted_content_is_escaped(self):
        rows = [{"date": "2026-08-17", "vendor": "<script>alert(1)</script>",
                 "sender": "a@b.com", "filename": 'x&"y.pdf',
                 "subject": "<b>subj</b>",
                 "thread_link": "https://mail.google.com/mail/u/0/#inbox/abc"}]
        html = render_html(_ctx(new=rows))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp;", html)

    def test_download_url_is_shown_as_visible_text(self):
        flagged = [{"date": "2026-08-16", "vendor": "Acme", "sender": "a@b.com",
                    "subject": "Your order", "thread_link": "https://mail.google.com/x",
                    "download_url": "https://acme.com/inv/1",
                    "amount": "$10.00", "phrase": "download your invoice"}]
        html = render_html(_ctx(flagged=flagged))
        # Present as href AND as readable text, so the destination can be
        # checked before clicking.
        self.assertIn('href="https://acme.com/inv/1"', html)
        self.assertIn("Download: https://acme.com/inv/1", html)


if __name__ == "__main__":
    unittest.main()
