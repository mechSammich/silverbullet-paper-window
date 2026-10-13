import unittest

import sb_eink as sb
from sb_eink import TaskAmbiguous, TaskNotFound, parse_tasks, toggle_task


class ParseTests(unittest.TestCase):
    def test_only_unchecked(self):
        page = "- [ ] a\n- [x] b\n* [ ] c\n  - [ ] d\nnot a task\n"
        self.assertEqual([t.text for t in parse_tasks(page)], ["a", "c", "d"])

    def test_tag_filter_and_display(self):
        page = "- [ ] a #eink\n- [ ] b\n- [ ] c #einkx\n"
        tasks = parse_tasks(page, "#eink")
        self.assertEqual([t.text for t in tasks], ["a #eink"])
        self.assertEqual(tasks[0].display, "a")

    def test_crlf(self):
        self.assertEqual([t.text for t in parse_tasks("- [ ] a\r\n- [ ] b\r\n")], ["a", "b"])


class ToggleTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(toggle_task("- [ ] a\n- [ ] b\n", "b"), "- [ ] a\n- [x] b\n")

    def test_duplicates(self):
        with self.assertRaises(TaskAmbiguous):
            toggle_task("- [ ] a\n- [ ] a\n", "a")

    def test_checked_duplicate_not_ambiguous(self):
        self.assertEqual(toggle_task("- [x] a\n- [ ] a\n", "a"), "- [x] a\n- [x] a\n")

    def test_missing(self):
        with self.assertRaises(TaskNotFound):
            toggle_task("- [ ] a\n", "zzz")

    def test_already_checked_is_missing(self):
        with self.assertRaises(TaskNotFound):
            toggle_task("- [x] a\n", "a")

    def test_tag_required(self):
        with self.assertRaises(TaskNotFound):
            toggle_task("- [ ] a\n", "a", "#eink")

    def test_crlf_preserved(self):
        page = "# T\r\n- [ ] a\r\n- [ ] b\r\nend\r\n"
        self.assertEqual(toggle_task(page, "a"), "# T\r\n- [x] a\r\n- [ ] b\r\nend\r\n")

    def test_mixed_endings(self):
        page = "- [ ] a\r\n- [ ] b\n"
        self.assertEqual(toggle_task(page, "b"), "- [ ] a\r\n- [x] b\n")

    def test_no_trailing_newline(self):
        self.assertEqual(toggle_task("x\n- [ ] a", "a"), "x\n- [x] a")

    def test_indent_and_star(self):
        self.assertEqual(toggle_task("  * [ ] a\n", "a"), "  * [x] a\n")

    def test_non_utf8_bytes_roundtrip(self):
        raw = b"- [ ] a\n\xff\xfe\n"
        page = sb.decode(raw)
        self.assertEqual(toggle_task(page, "a").encode("utf-8", "surrogateescape"),
                         b"- [x] a\n\xff\xfe\n")


class FakeSource:
    def __init__(self, body, etag):
        self.body, self.etag, self.puts = body, etag, []

    def get_page(self):
        return self.body, self.etag

    def put_page(self, data, etag):
        self.puts.append((data, etag))


class CheckOffTests(unittest.TestCase):
    def test_put_uses_etag(self):
        src = FakeSource(b"- [ ] a\n", '"sha256:1"')
        sb.check_off(src, "", "a")
        self.assertEqual(src.puts, [(b"- [x] a\n", '"sha256:1"')])

    def test_no_etag_rechecks_and_detects_change(self):
        class Changing(FakeSource):
            def get_page(self):
                self.body += b"x"
                return self.body, None

        src = Changing(b"- [ ] a\n", None)
        with self.assertRaises(sb.ConflictError):
            sb.check_off(src, "", "a")
        self.assertEqual(src.puts, [])

    def test_dry_run_does_not_write(self):
        src = FakeSource(b"- [ ] a\n", None)
        sb.check_off(src, "", "a", dry_run=True)
        self.assertEqual(src.puts, [])


if __name__ == "__main__":
    unittest.main()
