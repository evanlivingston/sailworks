"""The documentation anchor checker must match GitHub's heading slugs."""

from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import check_doc_anchors as anchors


def write(root, name, text):
    path = Path(root) / name
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


class TestSlug(unittest.TestCase):
    def test_punctuation_is_stripped_and_spaces_become_hyphens(self):
        self.assertEqual(
            anchors.slug("E2c contract: GPU field / selection"),
            "e2c-contract-gpu-field--selection",
        )

    def test_backticks_and_link_text_are_normalised(self):
        self.assertEqual(
            anchors.slug("The `Residency` and [the budget](x.md)"),
            "the-residency-and-the-budget",
        )


class TestHeadings(unittest.TestCase):
    def headings(self, text):
        with tempfile.TemporaryDirectory() as temporary:
            return anchors.headings(write(temporary, "doc.md", text))

    def test_atx_levels_and_trailing_hashes(self):
        found = self.headings("""
            # One
            ## Two ##
            ### Three
            """)
        self.assertEqual(found, {"one", "two", "three"})

    def test_duplicate_headings_are_suffixed_like_github(self):
        found = self.headings("""
            ## Repeat
            ## Repeat
            ## Repeat
            """)
        self.assertEqual(found, {"repeat", "repeat-1", "repeat-2"})

    def test_headings_inside_fenced_code_are_ignored(self):
        found = self.headings("""
            ## Real

            ```python
            # not a heading
            ## also not a heading
            ```

            ~~~
            # still not a heading
            ~~~

            ## After
            """)
        self.assertEqual(found, {"real", "after"})

    def test_setext_headings(self):
        found = self.headings("""
            Setext one
            ==========

            Setext two
            ----------
            """)
        self.assertEqual(found, {"setext-one", "setext-two"})

    def test_table_delimiter_is_not_a_setext_heading(self):
        found = self.headings("""
            | a | b |
            | --- | --- |
            """)
        self.assertEqual(found, set())


class TestMain(unittest.TestCase):
    def test_missing_anchor_exits_nonzero_and_prints_the_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = write(temporary, "a.md", "[x](b.md#missing)\n")
            write(temporary, "b.md", "# Present\n")
            self.assertEqual(anchors.main([str(source)]), 1)

    def test_existing_anchor_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = write(temporary, "a.md", "[x](b.md#present)\n")
            write(temporary, "b.md", "# Present\n")
            self.assertEqual(anchors.main([str(source)]), 0)

    def test_same_document_anchor(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = write(temporary, "a.md", "# Here\n\n[x](#here)\n")
            self.assertEqual(anchors.main([str(source)]), 0)

    def test_relative_path_does_not_raise_on_a_missing_anchor(self):
        # Regression: relative_to(ROOT) raised ValueError for relative inputs,
        # turning a reported failure into a traceback.
        with tempfile.TemporaryDirectory() as temporary:
            source = write(temporary, "a.md", "[x](b.md#missing)\n")
            write(temporary, "b.md", "# Present\n")
            previous = Path.cwd()
            import os

            os.chdir(temporary)
            try:
                self.assertEqual(anchors.main(["a.md"]), 1)
            finally:
                os.chdir(previous)

    def test_unknown_path_exits_two(self):
        self.assertEqual(anchors.main(["/nonexistent/path.md"]), 2)


if __name__ == "__main__":
    unittest.main()
